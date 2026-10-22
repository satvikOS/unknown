/**
 * Query cache keyed by endpoint + params. Each entry is refetched when the state stream's version
 * changes (any new seq, gap, reset or reconnect) while at least one view is subscribed to it, and
 * optionally on a polling interval for live process state that is not in the event log (quotes,
 * health, preflight). Data is never modified locally.
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

interface Entry {
  key: string;
  endpoint: string;
  params: Record<string, unknown> | undefined;
  state: QueryState;
  listeners: Map<() => void, number | undefined>;
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
    pollMs?: number,
  ): () => void {
    const e = this.entry(endpoint, params);
    const first = e.listeners.size === 0;
    e.listeners.set(listener, pollMs);
    this.updatePolling(e);
    if (first) this.request(e);
    return () => {
      e.listeners.delete(listener);
      this.updatePolling(e);
    };
  }

  /** Mark every subscribed entry stale and refetch it (called on every stream version bump). */
  invalidateAll(): void {
    for (const e of this.entries.values()) {
      if (e.listeners.size > 0) this.request(e);
      else e.wanted += 1; // refetched on next subscribe anyway
    }
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
    for (const ms of e.listeners.values()) {
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
        const seq = this.currentSeq();
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
