/**
 * State stream: subscribes to GET /api/stream (Server-Sent Events, ADR-0013) and turns sequence
 * notifications into a `version` that every visible query depends on.
 *
 * The server sends `event: seq` with `{"seq": n}` whenever the event log's latest sequence changes
 * (several events can coalesce into one notification). The client never applies events itself; it
 * refetches snapshots. The version therefore bumps on:
 *   - a new seq (`seq`), a jump of more than one (`gap`), a lower seq than before (`reset`, e.g. a
 *     restored database), a reconnect after an error (`reconnect`), and a manual refresh (`manual`).
 */

export const EVENT_SOURCE_CONNECTING = 0;
export const EVENT_SOURCE_OPEN = 1;
export const EVENT_SOURCE_CLOSED = 2;

/** The subset of the browser EventSource the store uses (injectable for tests). */
export interface EventSourceLike {
  readonly readyState: number;
  onopen: ((ev: Event) => void) | null;
  onerror: ((ev: Event) => void) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export type StreamStatus = 'idle' | 'connecting' | 'live' | 'reconnecting';
export type RefetchReason = 'seq' | 'gap' | 'reset' | 'reconnect' | 'manual';

export interface StreamSnapshot {
  status: StreamStatus;
  /** Latest event-log sequence number seen, or null before the first notification. */
  seq: number | null;
  /** Bumps whenever every visible snapshot must be refetched. */
  version: number;
  lastReason: RefetchReason | null;
  connects: number;
  reconnects: number;
  gaps: number;
  resets: number;
  lastMessageAtMs: number | null;
  lastError: string | null;
}

export interface StreamOptions {
  url?: string;
  createEventSource?: EventSourceFactory;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  /** Called when the browser gives up on the connection (e.g. HTTP 401): check the session. */
  onConnectionRefused?: () => void;
  /** Backoff for manual reconnects after a refused connection. */
  minBackoffMs?: number;
  maxBackoffMs?: number;
}

type Listener = () => void;

export class StreamStore {
  private snapshot: StreamSnapshot = {
    status: 'idle',
    seq: null,
    version: 0,
    lastReason: null,
    connects: 0,
    reconnects: 0,
    gaps: 0,
    resets: 0,
    lastMessageAtMs: null,
    lastError: null,
  };
  private readonly listeners = new Set<Listener>();
  private source: EventSourceLike | null = null;
  private hadError = false;
  private retryHandle: unknown = null;
  private backoffMs: number;
  private readonly waiters: { seq: number; resolve: () => void }[] = [];
  private readonly url: string;
  private readonly createEventSource: EventSourceFactory;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;
  private readonly onConnectionRefused: (() => void) | undefined;
  private readonly minBackoffMs: number;
  private readonly maxBackoffMs: number;

  constructor(options: StreamOptions = {}) {
    this.url = options.url ?? '/api/stream';
    this.createEventSource =
      options.createEventSource ?? ((url) => new EventSource(url, { withCredentials: true }) as EventSourceLike);
    this.now = options.now ?? (() => Date.now());
    this.setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = options.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
    this.onConnectionRefused = options.onConnectionRefused;
    this.minBackoffMs = options.minBackoffMs ?? 1000;
    this.maxBackoffMs = options.maxBackoffMs ?? 30_000;
    this.backoffMs = this.minBackoffMs;
  }

  // ------------------------------------------------------------- external store protocol

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): StreamSnapshot => this.snapshot;

  private set(patch: Partial<StreamSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const l of [...this.listeners]) l();
  }

  // ------------------------------------------------------------- lifecycle

  start(): void {
    if (this.source || this.retryHandle !== null) return;
    this.connect();
  }

  stop(): void {
    if (this.retryHandle !== null) {
      this.clearTimer(this.retryHandle);
      this.retryHandle = null;
    }
    this.source?.close();
    this.source = null;
    this.hadError = false;
    this.backoffMs = this.minBackoffMs;
    this.set({ status: 'idle' });
  }

  /** Force every visible query to refetch (e.g. the owner pressed refresh). */
  refreshAll(): void {
    this.bump('manual');
  }

  /** Resolves once the stream has reported a sequence number >= `seq`. */
  waitForSeq(seq: number): Promise<void> {
    const cur = this.snapshot.seq;
    if (cur !== null && cur >= seq) return Promise.resolve();
    return new Promise((resolve) => this.waiters.push({ seq, resolve }));
  }

  private connect(): void {
    this.retryHandle = null;
    const first = this.snapshot.connects === 0 && !this.hadError;
    this.set({ status: first ? 'connecting' : 'reconnecting' });
    let es: EventSourceLike;
    try {
      es = this.createEventSource(this.url);
    } catch (err) {
      this.set({ lastError: err instanceof Error ? err.message : String(err) });
      this.scheduleRetry();
      return;
    }
    this.source = es;
    es.onopen = () => this.handleOpen();
    es.onerror = () => this.handleError(es);
    es.addEventListener('seq', (ev) => this.handleMessage(ev));
  }

  private scheduleRetry(): void {
    const delay = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, this.maxBackoffMs);
    this.retryHandle = this.setTimer(() => this.connect(), delay);
  }

  /** Exposed for tests; called by the EventSource. */
  handleOpen(): void {
    const reconnect = this.hadError || this.snapshot.connects > 0;
    this.hadError = false;
    this.backoffMs = this.minBackoffMs;
    this.set({
      status: 'live',
      connects: this.snapshot.connects + 1,
      reconnects: this.snapshot.reconnects + (reconnect ? 1 : 0),
      lastError: null,
    });
    if (reconnect) this.bump('reconnect');
  }

  handleError(es: EventSourceLike): void {
    this.hadError = true;
    if (es.readyState === EVENT_SOURCE_CLOSED) {
      // The browser will not retry (HTTP error such as 401/503): reconnect ourselves with backoff.
      if (this.source === es) this.source = null;
      es.close();
      this.set({ status: 'reconnecting', lastError: 'state stream refused or closed' });
      this.onConnectionRefused?.();
      if (this.retryHandle === null) this.scheduleRetry();
      return;
    }
    // CONNECTING: the browser retries on its own.
    this.set({ status: 'reconnecting', lastError: 'state stream interrupted; reconnecting' });
  }

  handleMessage(ev: { data: unknown }): void {
    let seq: number | null = null;
    try {
      const parsed = JSON.parse(String(ev.data)) as { seq?: unknown };
      if (typeof parsed.seq === 'number' && Number.isInteger(parsed.seq) && parsed.seq >= 0) seq = parsed.seq;
    } catch {
      seq = null;
    }
    if (seq === null) {
      this.set({ lastError: `malformed seq notification: ${String(ev.data).slice(0, 80)}` });
      return;
    }
    this.applySeq(seq);
  }

  /** Core sequencing rule (pure state transition, unit tested). */
  applySeq(seq: number): void {
    const prev = this.snapshot.seq;
    const at = this.now();
    if (prev !== null && seq === prev) {
      this.set({ lastMessageAtMs: at });
      return;
    }
    let reason: RefetchReason = 'seq';
    const patch: Partial<StreamSnapshot> = { seq, lastMessageAtMs: at };
    if (prev !== null && seq < prev) {
      reason = 'reset';
      patch.resets = this.snapshot.resets + 1;
    } else if (prev !== null && seq > prev + 1) {
      reason = 'gap';
      patch.gaps = this.snapshot.gaps + 1;
    }
    this.snapshot = { ...this.snapshot, ...patch };
    this.bump(reason);
    this.releaseWaiters(seq, reason === 'reset');
  }

  private releaseWaiters(seq: number, all: boolean): void {
    for (let i = this.waiters.length - 1; i >= 0; i--) {
      const w = this.waiters[i];
      if (w && (all || seq >= w.seq)) {
        this.waiters.splice(i, 1);
        w.resolve();
      }
    }
  }

  private bump(reason: RefetchReason): void {
    this.set({ version: this.snapshot.version + 1, lastReason: reason });
  }
}
