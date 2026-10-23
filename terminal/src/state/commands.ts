/**
 * Command tracker: the only path from a control to the core. No optimistic updates (ADR-0013):
 *   1. `sending`   the durable command id is shown immediately;
 *   2. `pending`   the core recorded a decision; we wait until the state stream reports the seq of
 *                  the events it wrote, which refetches every visible snapshot;
 *   3. `applied`   that seq was observed (or the command was a duplicate with nothing new to wait for);
 *   or `rejected`  (a deterministic refusal with a code: the core's stored rejection, or the API's 409
 *                  COMMAND_ID_CONFLICT, which is definitive: this request was not executed),
 *   `failed`       (refused before the core, or the outcome is unknown), or
 *   `cancelled`    (step-up prompt dismissed; nothing was sent to the core).
 *
 * Unknown outcomes (CLAUDE.md: "Query before retrying"): when a send ends without an answer (network
 * failure, 5xx), the tracker asks GET /api/commands/<id> for the stored result before anything offers a
 * retry. A stored result is adopted as if the response had arrived (pending/applied or rejected); "not
 * recorded" says whether that is definitive (the core finished every earlier request) or provisional (the
 * core did not answer). `retry` queries again before it resends, always with the same id and payload; a
 * resend that never reaches the core (step-up dismissed, refused by the API) leaves the id an unknown outcome.
 */
import {
  ApiError,
  CommandTransportError,
  StepUpCancelledError,
  StepUpRequiredError,
  commandNotFound,
  describeError,
} from '../api/client';
import type { CommandKind, CommandPayload, CommandResponse, CommandResult, Rejection } from '../api/types';
import { isRejection } from '../api/types';

export type TrackedStatus = 'sending' | 'pending' | 'applied' | 'rejected' | 'failed' | 'cancelled';

/** The stored-result lookup that follows an unknown outcome (GET /api/commands/<id>). */
export type CommandLookup =
  | { state: 'querying' }
  /** A result was stored: the record's status now reflects it. `source` is who answered (core or database). */
  | { state: 'found'; source: 'core' | 'database'; atMs: number }
  /** Nothing stored. `definitive`: the core finished every request it had received, so it was not executed. */
  | { state: 'not_recorded'; definitive: boolean; message: string; atMs: number }
  /** The lookup itself failed (API unreachable, 5xx): the outcome stays unknown. */
  | { state: 'failed'; message: string; atMs: number };

export interface TrackedCommand {
  commandId: string;
  kind: CommandKind;
  /** Owner-facing description, e.g. "Stop: block new entries". */
  label: string;
  status: TrackedStatus;
  startedAtMs: number;
  settledAtMs: number | null;
  response: CommandResponse | null;
  rejection: Rejection | null;
  /** Highest event seq the command wrote; `applied` once the stream reaches it. */
  awaitSeq: number | null;
  error: string | null;
  /** For `failed`: true when the command may have reached the core (retry with the same id). */
  outcomeUnknown: boolean;
  /** For an unknown outcome: the stored-result lookup, its answer, or null when none was needed. */
  lookup: CommandLookup | null;
  /** The payload, kept so a failed command can be retried with the same id and payload. */
  payload: unknown;
}

export interface CommandSender {
  sendCommand<K extends CommandKind>(
    kind: K,
    payload: CommandPayload<K>,
    options?: { commandId?: string },
  ): Promise<CommandResponse>;
  /** GET /api/commands/<id>: the stored result; throws ApiError 404 COMMAND_NOT_FOUND when nothing is stored. */
  commandResult(commandId: string): Promise<CommandResult>;
}

export interface SeqSource {
  waitForSeq(seq: number): Promise<void>;
  refreshAll(): void;
  isLive(): boolean;
}

export interface CommandTrackerOptions {
  newCommandId: (kind: CommandKind) => string;
  now?: () => number;
  /** Keep at most this many settled commands in the list. */
  history?: number;
}

type Listener = () => void;

/** A failed command that may be sent again with the same id: the outcome is unknown, or it was not executed. */
export function canRetry(t: TrackedCommand): boolean {
  if (t.status !== 'failed' || t.lookup?.state === 'querying') return false;
  return t.outcomeUnknown || (t.lookup?.state === 'not_recorded' && t.lookup.definitive);
}

/** An unknown outcome whose stored result can be asked for again (the last lookup did not settle it). */
export function canRecheck(t: TrackedCommand): boolean {
  return t.status === 'failed' && t.outcomeUnknown && t.lookup?.state !== 'querying';
}

/** Status words for a failed command: what is known about whether it ran. */
export function failedText(t: TrackedCommand): string {
  if (t.lookup?.state === 'querying') return 'outcome unknown; checking the stored result';
  if (t.lookup?.state === 'not_recorded' && t.lookup.definitive) return 'not executed';
  return t.outcomeUnknown ? 'outcome unknown' : 'refused by the API';
}

/** One line on the stored-result lookup for the command's status view, or null when there was none. */
export function lookupNote(t: TrackedCommand): string | null {
  const l = t.lookup;
  if (!l) return null;
  switch (l.state) {
    case 'querying':
      return 'Outcome unknown: asking the API for the stored result before offering a retry.';
    case 'found':
      return `Outcome recovered from the stored result (GET /api/commands/<id>, answered by the ${l.source}); nothing was resent.`;
    case 'not_recorded':
      return l.definitive
        ? 'Not executed: the core has no record of this command id after finishing every earlier request. Resending it with the same id is safe.'
        : `Still unknown: ${l.message}`;
    case 'failed':
      return `Outcome still unknown: the stored result could not be read (${l.message}). A resend uses the same id, so the core acts at most once.`;
  }
}

export class CommandTracker {
  private items: TrackedCommand[] = [];
  private readonly listeners = new Set<Listener>();
  private readonly now: () => number;
  private readonly history: number;

  constructor(
    private readonly client: CommandSender,
    private readonly seq: SeqSource,
    private readonly options: CommandTrackerOptions,
  ) {
    this.now = options.now ?? (() => Date.now());
    this.history = options.history ?? 50;
  }

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): readonly TrackedCommand[] => this.items;

  get(commandId: string): TrackedCommand | undefined {
    return this.items.find((c) => c.commandId === commandId);
  }

  /** Most recent command of a kind (e.g. the latest stop), if any. */
  latest(kind: CommandKind): TrackedCommand | undefined {
    return this.items.find((c) => c.kind === kind);
  }

  private upsert(cmd: TrackedCommand): void {
    const rest = this.items.filter((c) => c.commandId !== cmd.commandId);
    this.items = [cmd, ...rest].slice(0, this.history);
    for (const l of [...this.listeners]) l();
  }

  private patch(commandId: string, patch: Partial<TrackedCommand>): TrackedCommand | undefined {
    const cur = this.get(commandId);
    if (!cur) return undefined;
    const next = { ...cur, ...patch };
    this.items = this.items.map((c) => (c.commandId === commandId ? next : c));
    for (const l of [...this.listeners]) l();
    return next;
  }

  /**
   * Send a command and track it to completion. Resolves with the tracked record once the core's
   * decision is known (status `pending`, `rejected`, `failed` or `cancelled`); the `applied`
   * transition happens later when the stream catches up. An unknown outcome resolves after the
   * stored-result lookup has answered.
   */
  send<K extends CommandKind>(kind: K, payload: CommandPayload<K>, opts: { label: string; commandId?: string }): Promise<TrackedCommand> {
    return this.dispatch(kind, payload, opts, null);
  }

  /**
   * `send`, and for `retry` the resend of a command whose earlier attempt's outcome is still unknown (`prior`).
   * A resend that never reaches the core (the step-up prompt was dismissed, or the API refused it before
   * forwarding) says nothing about that earlier attempt: the id stays an unknown outcome, with its last lookup,
   * instead of becoming `cancelled` or a plain refusal (CLAUDE.md: unknown outcomes remain unknown).
   */
  private async dispatch<K extends CommandKind>(
    kind: K,
    payload: CommandPayload<K>,
    opts: { label: string; commandId?: string },
    prior: TrackedCommand | null,
  ): Promise<TrackedCommand> {
    const commandId = opts.commandId ?? this.options.newCommandId(kind);
    const stillUnknown = (why: string): TrackedCommand =>
      this.patch(commandId, {
        status: 'failed',
        settledAtMs: this.now(),
        outcomeUnknown: true,
        lookup: prior?.lookup ?? null,
        error: `${why} The first attempt's outcome is still unknown; a resend uses the same id, so the core acts at most once.`,
      })!;
    this.upsert({
      commandId,
      kind,
      label: opts.label,
      status: 'sending',
      startedAtMs: this.now(),
      settledAtMs: null,
      response: null,
      rejection: null,
      awaitSeq: null,
      error: null,
      outcomeUnknown: false,
      lookup: null,
      payload,
    });
    let response: CommandResponse;
    try {
      response = await this.client.sendCommand(kind, payload, { commandId });
    } catch (err) {
      if (err instanceof StepUpCancelledError) {
        if (prior?.outcomeUnknown) return stillUnknown('The resend was not sent (re-authentication was cancelled).');
        return this.patch(commandId, { status: 'cancelled', settledAtMs: this.now(), error: err.message })!;
      }
      if (err instanceof CommandTransportError && err.conflict) {
        // 409: the id already names a command with another payload. That command keeps its own result; this
        // request was not executed. A definitive refusal, shown with the API's code (never retried as is).
        const code = err.code ?? 'CONFLICT';
        const message = err.serverMessage ?? err.message;
        const body = err.cause instanceof ApiError ? err.cause.body : null;
        const details: Record<string, unknown> = { http_status: 409, outcome: 'NOT_EXECUTED' };
        if (body && typeof body === 'object' && 'difference' in body) details.difference = (body as { difference: unknown }).difference;
        return this.patch(commandId, {
          status: 'rejected',
          rejection: { code, message, details },
          settledAtMs: this.now(),
          error: `${code}: ${message}`,
          outcomeUnknown: false,
        })!;
      }
      const outcomeUnknown = err instanceof CommandTransportError ? err.outcomeUnknown : !(err instanceof StepUpRequiredError);
      if (!outcomeUnknown && prior?.outcomeUnknown) {
        const detail = err instanceof CommandTransportError ? (err.serverMessage ?? err.message) : describeError(err);
        return stillUnknown(`The resend was refused before reaching the core: ${detail}.`);
      }
      const failed = this.patch(commandId, {
        status: 'failed',
        settledAtMs: this.now(),
        error: describeError(err),
        outcomeUnknown,
      })!;
      if (!outcomeUnknown) return failed;
      // The outcome may have changed state; look again rather than assume, and ask for the stored result.
      this.seq.refreshAll();
      return this.lookUp(commandId);
    }
    return this.settle(commandId, response);
  }

  /** Apply the core's decision (from the POST's answer, or from the stored result after an unknown outcome). */
  private settle(commandId: string, response: CommandResponse, extra: Partial<TrackedCommand> = {}): TrackedCommand {
    const events = response.events ?? [];
    const awaitSeq = events.length ? Math.max(...events) : null;
    if (!response.accepted) {
      const rejection = isRejection(response.body)
        ? response.body
        : { code: 'REJECTED', message: JSON.stringify(response.body), details: {} };
      return this.patch(commandId, {
        ...extra,
        status: 'rejected',
        response,
        rejection,
        awaitSeq,
        settledAtMs: this.now(),
        error: `${rejection.code}${rejection.message ? `: ${rejection.message}` : ''}`,
      })!;
    }
    if (awaitSeq === null) {
      // Duplicate (stored result returned again) or a command that wrote nothing: refetch once.
      this.seq.refreshAll();
      return this.patch(commandId, { ...extra, status: 'applied', response, settledAtMs: this.now() })!;
    }
    const pending = this.patch(commandId, { ...extra, status: 'pending', response, awaitSeq })!;
    if (!this.seq.isLive()) this.seq.refreshAll();
    void this.seq.waitForSeq(awaitSeq).then(() => {
      const cur = this.get(commandId);
      if (cur && cur.status === 'pending') this.patch(commandId, { status: 'applied', settledAtMs: this.now() });
    });
    return pending;
  }

  /**
   * Ask the API for the stored result of a command whose outcome is unknown, and adopt it. Resolves with the
   * updated record: pending/applied/rejected when a result is stored, still `failed` otherwise.
   */
  private async lookUp(commandId: string): Promise<TrackedCommand> {
    const cur = this.patch(commandId, { lookup: { state: 'querying' } });
    if (!cur) throw new Error(`command ${commandId} is not tracked`);
    let stored: CommandResult;
    try {
      stored = await this.client.commandResult(commandId);
    } catch (err) {
      const missing = commandNotFound(err);
      if (missing) {
        return this.patch(commandId, {
          outcomeUnknown: !missing.definitive,
          lookup: { state: 'not_recorded', definitive: missing.definitive, message: missing.message, atMs: this.now() },
          error: missing.definitive ? `${cur.kind} ${commandId} was not executed: the core has no record of it.` : cur.error,
        })!;
      }
      return this.patch(commandId, { lookup: { state: 'failed', message: describeError(err), atMs: this.now() } })!;
    }
    if (stored.kind !== cur.kind) {
      // Never adopt another command's result: the id names a different kind (a defect worth showing as is).
      return this.patch(commandId, {
        lookup: { state: 'failed', message: `the stored command ${commandId} is a ${stored.kind}, not a ${cur.kind}`, atMs: this.now() },
      })!;
    }
    const response: CommandResponse = {
      command_id: stored.command_id,
      accepted: stored.accepted,
      duplicate: true,
      body: stored.body,
      events: stored.events,
    };
    return this.settle(commandId, response, {
      error: null,
      outcomeUnknown: false,
      lookup: { state: 'found', source: stored.source, atMs: this.now() },
    });
  }

  /**
   * Ask again for the stored result of a command whose outcome is still unknown (no resend). Returns null when
   * the record is not an unknown outcome.
   */
  recheck(commandId: string): Promise<TrackedCommand> | null {
    const cur = this.get(commandId);
    if (!cur || !canRecheck(cur)) return null;
    return this.lookUp(commandId);
  }

  /**
   * Retry a failed command with the SAME id and payload (safe: the core records each id once). When the outcome
   * is unknown, the stored result is queried first and adopted if it exists; only otherwise is the command resent.
   */
  retry(commandId: string): Promise<TrackedCommand> | null {
    const cur = this.get(commandId);
    if (!cur || cur.status !== 'failed' || cur.lookup?.state === 'querying') return null;
    // `prior`: the record as the latest lookup left it; while its outcome is unknown, a resend that is not sent or
    // is refused before the core leaves it unknown (see `dispatch`).
    const resend = (prior: TrackedCommand) =>
      this.dispatch(cur.kind, cur.payload as CommandPayload<typeof cur.kind>, { label: cur.label, commandId }, prior);
    if (!cur.outcomeUnknown) return resend(cur);
    return this.lookUp(commandId).then((t) => (t.status === 'failed' ? resend(t) : t));
  }

  clear(): void {
    this.items = [];
    for (const l of [...this.listeners]) l();
  }
}
