/**
 * Query cache keyed by endpoint + params. Each entry is refetched when the state stream's version
 * changes (any new seq, gap, reset or reconnect) while at least one view is subscribed to it, and
 * optionally on a polling interval for live process state that is not in the event log (quotes,
 * health, preflight). Data is never modified locally.
 *
 * Data outside the core's event log (the research store and registry, valuation snapshots, the
 * capability register) does not change when the core writes an event, so a subscriber can say
 * `seqDriven: false`: a new seq (or a gap) does not refetch it. It is still fetched when a view first
 * subscribes, on its own Refresh, on a manual "refresh all", on a stream reconnect (the API may have
 * restarted) and on a reset (a restore), and on `pollMs`. An entry is seq-driven while any of its
 * subscribers wants that, and its `seqAtFetch` is null otherwise: its data says nothing about a seq.
 *
 * Ordering rule: a fetch that started before a version bump may return pre-bump data, so a bump that
 * arrives while a fetch is in flight schedules exactly one more fetch after it completes.
 */

export type QueryStatus = 'idle' | 'loading' | 'success' | 'error';

export interface QueryState<T = unknown> {
  status: QueryStatus;
  data: T | undefined;
  error: unknown;
  /** A request is in flight (initial load or refetch). Previous data stays visible meanwhile. */
  fetching: boolean;
  /** When the current data arrived (browser clock, ms). */
  fetchedAtMs: number | null;
  /** Stream seq when the fetch that produced `data` started: the data reflects at least this seq. */
  seqAtFetch: number | null;
}

export type QueryFetcher = (endpoint: string, params: Record<string, unknown> | undefined) => Promise<unknown>;

/** One subscriber's wishes. */
export interface SubscribeOptions {
  /** Also refetch on this interval (ms). */
  pollMs?: number;
  /** False: the data is not in the core's event log, so a new stream seq does not refetch it (default true). */
  seqDriven?: boolean;
}

/** Why every subscribed entry is being refetched: `core` (a new seq or a gap) skips entries that are not seq-driven. */
export type InvalidationScope = 'core' | 'all';

interface Entry {
  key: string;
  endpoint: string;
  params: Record<string, unknown> | undefined;
  state: QueryState;
  listeners: Map<() => void, SubscribeOptions>;
  /** Increments for every reason this entry must be fetched again. */
  wanted: number;
  /** `wanted` at the start of the in-flight fetch. */
  inflightFor: number;
  inflight: Promise<void> | null;
  pollHandle: unknown;
  pollMs: number | undefined;
}

const IDLE: QueryState = {
  status: 'idle',
  data: undefined,
  error: null,
  fetching: false,
  fetchedAtMs: null,
  seqAtFetch: null,
};

export interface QueryCacheOptions {
  fetcher: QueryFetcher;
  /** Current stream seq (for `seqAtFetch`). */
  currentSeq?: () => number | null;
  now?: () => number;
  setInterval?: (fn: () => void, ms: number) => unknown;
  clearInterval?: (handle: unknown) => void;
}

export function queryKey(endpoint: string, params?: Record<string, unknown>): string {
  if (!params) return endpoint;
  const keys = Object.keys(params)
    .filter((k) => params[k] !== undefined && params[k] !== null && params[k] !== '')
    .sort();
  return keys.length ? `${endpoint}?${keys.map((k) => `${k}=${String(params[k])}`).join('&')}` : endpoint;
}

export class QueryCache {
  private readonly entries = new Map<string, Entry>();
  private readonly fetcher: QueryFetcher;
  private readonly currentSeq: () => number | null;
  private readonly now: () => number;
  private readonly setIntervalFn: (fn: () => void, ms: number) => unknown;
  private readonly clearIntervalFn: (handle: unknown) => void;

  constructor(options: QueryCacheOptions) {
    this.fetcher = options.fetcher;
    this.currentSeq = options.currentSeq ?? (() => null);
    this.now = options.now ?? (() => Date.now());
    this.setIntervalFn = options.setInterval ?? ((fn, ms) => setInterval(fn, ms));
    this.clearIntervalFn = options.clearInterval ?? ((h) => clearInterval(h as ReturnType<typeof setInterval>));
  }

  private entry(endpoint: string, params?: Record<string, unknown>): Entry {
    const key = queryKey(endpoint, params);
    let e = this.entries.get(key);
    if (!e) {
      e = {
        key,
        endpoint,
        params,
        state: IDLE,
        listeners: new Map(),
        wanted: 0,
        inflightFor: 0,
        inflight: null,
        pollHandle: null,
        pollMs: undefined,
      };
      this.entries.set(key, e);
    }
    return e;
  }

  getState(key: string): QueryState {
    return this.entries.get(key)?.state ?? IDLE;
  }

  /**
   * Register a view's interest. The first subscriber triggers a fetch (cached data, if any, stays
   * visible while it runs). Returns the unsubscribe function.
   */
  subscribe(
    endpoint: string,
    params: Record<string, unknown> | undefined,
    listener: () => void,
    options: number | SubscribeOptions = {},
  ): () => void {
    const e = this.entry(endpoint, params);
    const first = e.listeners.size === 0;
    e.listeners.set(listener, typeof options === 'number' ? { pollMs: options } : options);
    this.updatePolling(e);
    if (first) this.request(e);
    return () => {
      e.listeners.delete(listener);
      this.updatePolling(e);
    };
  }

  /** Mark every subscribed entry stale and refetch it (a reconnect, a reset, a manual refresh). */
  invalidateAll(): void {
    this.invalidate('all');
  }

  /**
   * Refetch the subscribed entries a stream version bump concerns: every one for `all`; for `core` (a new seq or
   * a gap) only those a subscriber marked seq-driven.
   */
  invalidate(scope: InvalidationScope): void {
    for (const e of this.entries.values()) {
      if (e.listeners.size > 0) {
        if (scope === 'all' || this.seqDriven(e)) this.request(e);
      } else {
        e.wanted += 1; // refetched on next subscribe anyway
      }
    }
  }

  /** Whether a new core seq should refetch this entry: true while any subscriber wants that (and when none says). */
  private seqDriven(e: Entry): boolean {
    if (e.listeners.size === 0) return true;
    for (const o of e.listeners.values()) if (o.seqDriven !== false) return true;
    return false;
  }

  /** Refetch one key now (e.g. a view's refresh button). */
  refetch(key: string): void {
    const e = this.entries.get(key);
    if (e) this.request(e);
  }

  /** Fetch outside any subscription (e.g. the palette) and cache the result. */
  async fetchNow(endpoint: string, params?: Record<string, unknown>): Promise<unknown> {
    const e = this.entry(endpoint, params);
    this.request(e);
    while (e.inflight) await e.inflight;
    if (e.state.status === 'error') throw e.state.error;
    return e.state.data;
  }

  /** Drop everything (logout). */
  clear(): void {
    for (const e of this.entries.values()) {
      if (e.pollHandle !== null) this.clearIntervalFn(e.pollHandle);
    }
    this.entries.clear();
  }

  private updatePolling(e: Entry): void {
    let min: number | undefined;
    for (const { pollMs: ms } of e.listeners.values()) {
      if (ms !== undefined && ms > 0 && (min === undefined || ms < min)) min = ms;
    }
    if (min === e.pollMs && (min === undefined || e.pollHandle !== null)) return;
    if (e.pollHandle !== null) this.clearIntervalFn(e.pollHandle);
    e.pollHandle = null;
    e.pollMs = min;
    if (min !== undefined) e.pollHandle = this.setIntervalFn(() => this.request(e), min);
  }

  private notify(e: Entry, patch: Partial<QueryState>): void {
    e.state = { ...e.state, ...patch };
    for (const l of [...e.listeners.keys()]) l();
  }

  private request(e: Entry): void {
    e.wanted += 1;
    if (e.inflight) return; // the in-flight fetch re-runs when it completes (see run())
    e.inflight = this.run(e);
  }

  private async run(e: Entry): Promise<void> {
    try {
      while (e.inflightFor < e.wanted) {
        e.inflightFor = e.wanted;
        // Data outside the event log reflects no seq: say so rather than label it with an unrelated one.
        const seq = this.seqDriven(e) ? this.currentSeq() : null;
        this.notify(e, { fetching: true, status: e.state.status === 'idle' ? 'loading' : e.state.status });
        try {
          const data = await this.fetcher(e.endpoint, e.params);
          this.notify(e, {
            status: 'success',
            data,
            error: null,
            fetching: false,
            fetchedAtMs: this.now(),
            seqAtFetch: seq,
          });
        } catch (error) {
          this.notify(e, { status: 'error', error, fetching: false });
        }
        // Only keep going while someone is still looking (or a direct fetchNow is waiting).
        if (e.listeners.size === 0 && e.inflightFor < e.wanted) {
          // Leave `wanted` ahead so the next subscriber refetches.
          break;
        }
      }
    } finally {
      e.inflight = null;
    }
  }
}
