/** React bindings for the runtime stores (context + useSyncExternalStore; no state libraries). */
import { createContext, useCallback, useContext, useRef, useSyncExternalStore, type ReactNode } from 'react';
import type { CommandKind, CommandPayload, Endpoint, ParamsOf, ResponseOf } from '../api/types';
import type { AuthSnapshot } from './auth';
import { lookupNote, type TrackedCommand } from './commands';
import type { Notice } from './notices';
import { queryKey, type QueryState } from './queries';
import type { Route } from './router';
import type { Runtime } from './runtime';
import type { StepUpSnapshot } from './stepup';
import type { StreamSnapshot } from './stream';

const RuntimeContext = createContext<Runtime | null>(null);

export function RuntimeProvider({ runtime, children }: { runtime: Runtime; children: ReactNode }) {
  return <RuntimeContext.Provider value={runtime}>{children}</RuntimeContext.Provider>;
}

export function useRuntime(): Runtime {
  const rt = useContext(RuntimeContext);
  if (!rt) throw new Error('useRuntime must be used inside <RuntimeProvider>');
  return rt;
}

export function useStream(): StreamSnapshot {
  const { stream } = useRuntime();
  return useSyncExternalStore(stream.subscribe, stream.getSnapshot);
}

export function useAuth(): AuthSnapshot {
  const { auth } = useRuntime();
  return useSyncExternalStore(auth.subscribe, auth.getSnapshot);
}

export function useRoute(): Route {
  const { router } = useRuntime();
  return useSyncExternalStore(router.subscribe, router.getSnapshot);
}

export function useCommands(): readonly TrackedCommand[] {
  const { commands } = useRuntime();
  return useSyncExternalStore(commands.subscribe, commands.getSnapshot);
}

export function useNotices(): readonly Notice[] {
  const { notices } = useRuntime();
  return useSyncExternalStore(notices.subscribe, notices.getSnapshot);
}

export function useStepUp(): StepUpSnapshot {
  const { stepUp } = useRuntime();
  return useSyncExternalStore(stepUp.subscribe, stepUp.getSnapshot);
}

// ------------------------------------------------------------------ queries

export interface QueryOptions {
  /** Also refetch on this interval (ms) while mounted: for live process state outside the event log. */
  pollMs?: number;
}

export interface QueryResult<T> extends QueryState<T> {
  /** Cache key (endpoint + sorted params). */
  key: string | null;
  refetch: () => void;
}

const DISABLED: QueryState<never> = {
  status: 'idle',
  data: undefined,
  error: null,
  fetching: false,
  fetchedAtMs: null,
  seqAtFetch: null,
};

/**
 * Subscribe a view to a GET endpoint. Refetches whenever the state stream's version changes (new
 * seq, gap, reset, reconnect) and on `pollMs` if given. Pass `null` as the endpoint to disable.
 *
 *   const overview = useQuery('/api/overview');
 *   const open = useQuery('/api/orders', { state: 'open' });
 */
export function useQuery<E extends Endpoint>(
  endpoint: E | null,
  params?: ParamsOf<E>,
  options: QueryOptions = {},
): QueryResult<ResponseOf<E>> {
  const { queries } = useRuntime();
  const key = endpoint ? queryKey(endpoint, params as Record<string, unknown> | undefined) : null;
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const pollMs = options.pollMs;
  const subscribe = useCallback(
    (listener: () => void) =>
      endpoint && key
        ? queries.subscribe(endpoint, paramsRef.current as Record<string, unknown> | undefined, listener, pollMs)
        : () => undefined,
    // `key` captures endpoint + params; params are read through a ref so a new object identity does not resubscribe.
    [queries, key, pollMs],
  );
  const getSnapshot = useCallback(() => (key ? queries.getState(key) : DISABLED), [queries, key]);
  const state = useSyncExternalStore(subscribe, getSnapshot) as QueryState<ResponseOf<E>>;
  const refetch = useCallback(() => {
    if (key) queries.refetch(key);
  }, [queries, key]);
  return { ...state, key, refetch };
}

// ------------------------------------------------------------------ commands

export type SendCommand = <K extends CommandKind>(
  kind: K,
  payload: CommandPayload<K>,
  opts: { label: string; commandId?: string },
) => Promise<TrackedCommand>;

/**
 * Send an owner command through the tracker (pending -> applied on the stream; never optimistic).
 * Rejections and failures are also announced in the notice area with the command id.
 */
export function useSendCommand(): SendCommand {
  const { commands, notices } = useRuntime();
  return useCallback<SendCommand>(
    async (kind, payload, opts) => {
      const t = await commands.send(kind, payload, opts);
      if (t.status === 'rejected') {
        notices.push({ tone: 'error', title: `${opts.label}: rejected`, detail: t.error ?? undefined, commandId: t.commandId });
      } else if (t.status === 'failed') {
        // An unknown outcome has already been looked up (GET /api/commands/<id>): say what that found.
        const notExecuted = t.lookup?.state === 'not_recorded' && t.lookup.definitive;
        const detail = [t.error, lookupNote(t)].filter(Boolean).join(' ');
        notices.push({ tone: 'error', title: `${opts.label}: ${notExecuted ? 'not executed' : 'not confirmed'}`, detail: detail || undefined, commandId: t.commandId });
      } else if (t.status === 'cancelled') {
        notices.push({ tone: 'warn', title: `${opts.label}: cancelled`, detail: 'Re-authentication was cancelled; nothing was sent to the core.', commandId: t.commandId });
      } else if (t.status === 'pending') {
        const noticeId = notices.push(
          {
            tone: 'info',
            title: `${opts.label}: recorded by the core`,
            detail:
              t.lookup?.state === 'found'
                ? 'The response was lost; the stored result says the core recorded it (nothing was resent). Waiting for the state stream to confirm.'
                : 'Waiting for the state stream to confirm.',
            commandId: t.commandId,
          },
          60_000,
        );
        let unsubscribe: (() => void) | null = null;
        let settled = false;
        const check = () => {
          const cur = commands.get(t.commandId);
          if (settled || !cur || cur.status === 'pending') return;
          settled = true;
          unsubscribe?.();
          notices.dismiss(noticeId);
          if (cur.status === 'applied') {
            notices.push({
              tone: 'ok',
              title: `${opts.label}: applied`,
              detail: `State seq ${cur.awaitSeq ?? '?'} observed; views refreshed.`,
              commandId: cur.commandId,
            });
          }
        };
        unsubscribe = commands.subscribe(check);
        check(); // the stream may already have reached the command's seq
        if (settled) unsubscribe();
      } else {
        const detail =
          t.lookup?.state === 'found'
            ? 'The response was lost; the stored result says the core applied it (nothing was resent).'
            : 'Duplicate command id: the core returned its stored result.';
        notices.push({ tone: 'ok', title: `${opts.label}: applied`, detail, commandId: t.commandId });
      }
      return t;
    },
    [commands, notices],
  );
}

/** The tracked record for one command id (re-renders as it moves sending -> pending -> applied). */
export function useTrackedCommand(commandId: string | null | undefined): TrackedCommand | undefined {
  const items = useCommands();
  return commandId ? items.find((c) => c.commandId === commandId) : undefined;
}

// ------------------------------------------------------------------ clock

class Ticker {
  private now = Date.now();
  private readonly listeners = new Set<() => void>();
  private handle: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly ms: number) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (!this.handle) {
      this.now = Date.now();
      this.handle = setInterval(() => {
        this.now = Date.now();
        for (const l of [...this.listeners]) l();
      }, this.ms);
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0 && this.handle) {
        clearInterval(this.handle);
        this.handle = null;
      }
    };
  };

  getSnapshot = (): number => this.now;
}

const tickers = new Map<number, Ticker>();

/** Browser time in ms, re-rendering every `intervalMs` (shared timer per interval). */
export function useNow(intervalMs = 1000): number {
  let t = tickers.get(intervalMs);
  if (!t) {
    t = new Ticker(intervalMs);
    tickers.set(intervalMs, t);
  }
  return useSyncExternalStore(t.subscribe, t.getSnapshot);
}
