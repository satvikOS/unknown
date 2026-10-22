/**
 * Command tracker: the only path from a control to the core. No optimistic updates (ADR-0013):
 *   1. `sending`   the durable command id is shown immediately;
 *   2. `pending`   the core recorded a decision; we wait until the state stream reports the seq of
 *                  the events it wrote, which refetches every visible snapshot;
 *   3. `applied`   that seq was observed (or the command was a duplicate with nothing new to wait for);
 *   or `rejected`  (deterministic refusal with a code), `failed` (transport; outcome may be unknown),
 *   or `cancelled` (step-up prompt dismissed; nothing was sent to the core).
 */
import {
  CommandTransportError,
  StepUpCancelledError,
  StepUpRequiredError,
  describeError,
} from '../api/client';
import type { CommandKind, CommandPayload, CommandResponse, Rejection } from '../api/types';
import { isRejection } from '../api/types';

export type TrackedStatus = 'sending' | 'pending' | 'applied' | 'rejected' | 'failed' | 'cancelled';

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
  /** The payload, kept so a failed command can be retried with the same id and payload. */
  payload: unknown;
}

export interface CommandSender {
  sendCommand<K extends CommandKind>(
    kind: K,
    payload: CommandPayload<K>,
    options?: { commandId?: string },
  ): Promise<CommandResponse>;
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
   * transition happens later when the stream catches up.
   */
  async send<K extends CommandKind>(
    kind: K,
    payload: CommandPayload<K>,
    opts: { label: string; commandId?: string },
  ): Promise<TrackedCommand> {
    const commandId = opts.commandId ?? this.options.newCommandId(kind);
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
      payload,
    });
    let response: CommandResponse;
    try {
      response = await this.client.sendCommand(kind, payload, { commandId });
    } catch (err) {
      if (err instanceof StepUpCancelledError) {
        return this.patch(commandId, { status: 'cancelled', settledAtMs: this.now(), error: err.message })!;
      }
      const outcomeUnknown = err instanceof CommandTransportError ? err.outcomeUnknown : !(err instanceof StepUpRequiredError);
      const failed = this.patch(commandId, {
        status: 'failed',
        settledAtMs: this.now(),
        error: describeError(err),
        outcomeUnknown,
      })!;
      // The outcome may have changed state; look again rather than assume.
      if (outcomeUnknown) this.seq.refreshAll();
      return failed;
    }
    const events = response.events ?? [];
    const awaitSeq = events.length ? Math.max(...events) : null;
    if (!response.accepted) {
      const rejection = isRejection(response.body)
        ? response.body
        : { code: 'REJECTED', message: JSON.stringify(response.body), details: {} };
      return this.patch(commandId, {
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
      return this.patch(commandId, { status: 'applied', response, settledAtMs: this.now() })!;
    }
    const pending = this.patch(commandId, { status: 'pending', response, awaitSeq })!;
    if (!this.seq.isLive()) this.seq.refreshAll();
    void this.seq.waitForSeq(awaitSeq).then(() => {
      const cur = this.get(commandId);
      if (cur && cur.status === 'pending') this.patch(commandId, { status: 'applied', settledAtMs: this.now() });
    });
    return pending;
  }

  /** Retry a failed command with the SAME id and payload (safe: the core records each id once). */
  retry(commandId: string): Promise<TrackedCommand> | null {
    const cur = this.get(commandId);
    if (!cur || cur.status !== 'failed') return null;
    return this.send(cur.kind, cur.payload as CommandPayload<typeof cur.kind>, { label: cur.label, commandId });
  }

  clear(): void {
    this.items = [];
    for (const l of [...this.listeners]) l();
  }
}
