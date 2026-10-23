/**
 * Market-data client (docs/design/marketdata-stream-contract.md, ADR-0018). Broker-agnostic: it knows sources only
 * by the `source` and `data_type` each event carries.
 *
 * - One EventSource for the whole terminal: GET /api/marketdata/stream?keys=<union of subscribed keys>.
 * - Ref-counted subscriptions: panels call subscribe(keys, listener); the stream reopens (debounced) only when the
 *   union of keys changes.
 * - Notifications are batched to one per animation frame: listeners get the set of keys that changed.
 * - A seq gap (per key and event type) triggers a snapshot resync (quotes) or a time & sales re-read (trades).
 * - Stream health (state, heartbeat age, gaps, resyncs, browser receipt latency, sources seen) is exposed for the
 *   header and the monitor through useStreamHealth() / MarketDataProvider.
 */
import { createContext, createElement, useContext, useSyncExternalStore, type ReactNode } from 'react';
import type { EventSourceFactory, EventSourceLike } from './stream';

export type DataType = 'REALTIME' | 'DELAYED' | 'FROZEN' | 'SYNTHETIC';
export type StatusState = 'SUBSCRIBED' | 'UNAVAILABLE' | 'NOT_ENTITLED' | 'LINE_BUDGET' | 'STALE' | 'ENDED';
type NsValue = string | number | null;

export interface Quote {
  key: string;
  seq: number;
  bid: string | null;
  ask: string | null;
  bid_size: string | null;
  ask_size: string | null;
  last: string | null;
  last_size: string | null;
  volume: string | null;
  open: string | null;
  high: string | null;
  low: string | null;
  prev_close: string | null;
  currency: string | null;
  source: string;
  data_type: DataType;
  feed?: string | null;
  size_unit?: string | null;
  ts_source_ns: NsValue;
  ts_recv_ns: NsValue;
  ts_pub_ns: NsValue;
}

export interface Trade {
  key: string;
  seq: number;
  price: string;
  size: string | null;
  exchange: string | null;
  conditions: string[];
  source: string;
  data_type: DataType;
  feed?: string | null;
  ts_source_ns: NsValue;
  ts_recv_ns: NsValue;
  ts_pub_ns?: NsValue;
}

export interface DepthLevel {
  price: string;
  size: string | null;
  mm: string | null;
}

export interface DepthEvent {
  key: string;
  seq: number;
  side: 'bid' | 'ask';
  levels: DepthLevel[];
  source: string;
  data_type: DataType;
  ts_recv_ns: NsValue;
}

export interface KeyStatus {
  key: string;
  state: StatusState;
  reason: string;
}

export type StreamState = 'idle' | 'connecting' | 'live' | 'reconnecting';

export interface StreamHealth {
  state: StreamState;
  keys: number;
  events: number;
  lastEventMs: number | null;
  lastHeartbeatMs: number | null;
  gaps: number;
  resyncs: number;
  overflows: number;
  /** Browser receipt minus ts_pub_ns over the most recent quotes and trades (ms; same-host clocks assumed). */
  receiptP50Ms: number | null;
  receiptP99Ms: number | null;
  /** Data types seen per source, e.g. { SYNTHETIC: ['SYNTHETIC'], IBKR: ['REALTIME', 'DELAYED'] }. */
  sources: Record<string, DataType[]>;
}

export type Listener = (changed: ReadonlySet<string>) => void;
export type FetchJson = (url: string) => Promise<unknown>;

export interface MarketDataOptions {
  url?: string;
  createEventSource?: EventSourceFactory;
  fetchJson?: FetchJson;
  /** Frame scheduler for batched notifications (default requestAnimationFrame, or a 16 ms timer). */
  schedule?: (cb: () => void) => void;
  now?: () => number;
  /** Delay before the stream reopens after the key set changed (ms). */
  reopenDelayMs?: number;
  timer?: (cb: () => void, ms: number) => unknown;
  tradesKept?: number;
}

const LATENCY_WINDOW = 2048;

function nsToMs(ns: NsValue): number | null {
  if (ns === null || ns === undefined) return null;
  const n = typeof ns === 'number' ? ns : Number(ns);
  return Number.isFinite(n) ? n / 1e6 : null;
}

function pct(sorted: number[], p: number): number | null {
  if (!sorted.length) return null;
  const rank = Math.ceil((p / 100) * sorted.length);
  return sorted[Math.max(rank, 1) - 1] ?? null;
}

function defaultSchedule(cb: () => void): void {
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => cb());
  else setTimeout(cb, 16);
}

async function defaultFetchJson(url: string): Promise<unknown> {
  const r = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

export class MarketDataClient {
  private readonly url: string;
  private readonly createEventSource: EventSourceFactory;
  private readonly fetchJson: FetchJson;
  private readonly schedule: (cb: () => void) => void;
  private readonly now: () => number;
  private readonly reopenDelayMs: number;
  private readonly timer: (cb: () => void, ms: number) => unknown;
  private readonly tradesKept: number;

  private readonly refs = new Map<string, number>();
  private readonly listeners = new Map<Listener, Set<string>>();
  private readonly healthListeners = new Set<() => void>();
  private readonly quotes = new Map<string, Quote>();
  private readonly trades = new Map<string, Trade[]>();
  private readonly depthBook = new Map<string, { bid: DepthEvent | null; ask: DepthEvent | null }>();
  private readonly statuses = new Map<string, KeyStatus>();
  private readonly lastSeq = new Map<string, number>();
  private readonly dirty = new Set<string>();
  private readonly pendingResync = new Set<string>();
  private readonly latency: number[] = [];
  private source: EventSourceLike | null = null;
  private openKeys = '';
  private flushScheduled = false;
  private reopenScheduled = false;
  private healthState: StreamHealth = {
    state: 'idle', keys: 0, events: 0, lastEventMs: null, lastHeartbeatMs: null, gaps: 0, resyncs: 0, overflows: 0,
    receiptP50Ms: null, receiptP99Ms: null, sources: {},
  };

  constructor(options: MarketDataOptions = {}) {
    this.url = options.url ?? '/api/marketdata/stream';
    this.createEventSource =
      options.createEventSource ?? ((url) => new EventSource(url, { withCredentials: true }) as EventSourceLike);
    this.fetchJson = options.fetchJson ?? defaultFetchJson;
    this.schedule = options.schedule ?? defaultSchedule;
    this.now = options.now ?? (() => Date.now());
    this.reopenDelayMs = options.reopenDelayMs ?? 150;
    this.timer = options.timer ?? ((cb, ms) => setTimeout(cb, ms));
    this.tradesKept = options.tradesKept ?? 500;
  }

  // ---------------------------------------------------------------- subscriptions
  subscribe(keys: readonly string[], listener: Listener): () => void {
    const own = new Set(keys.filter((k) => k));
    let added = false;
    for (const k of own) {
      const n = this.refs.get(k) ?? 0;
      this.refs.set(k, n + 1);
      if (n === 0) added = true;
    }
    this.listeners.set(listener, own);
    if (added) this.scheduleReopen();
    let done = false;
    return () => {
      if (done) return;
      done = true;
      this.listeners.delete(listener);
      let removed = false;
      for (const k of own) {
        const n = (this.refs.get(k) ?? 1) - 1;
        if (n <= 0) {
          this.refs.delete(k);
          removed = true;
        } else this.refs.set(k, n);
      }
      if (removed) this.scheduleReopen();
    };
  }

  /** Keys with at least one subscriber, sorted (what the stream asks for). */
  subscribedKeys(): string[] {
    return [...this.refs.keys()].sort();
  }

  private scheduleReopen(): void {
    if (this.reopenScheduled) return;
    this.reopenScheduled = true;
    this.timer(() => {
      this.reopenScheduled = false;
      this.reopen();
    }, this.reopenDelayMs);
  }

  /** Open (or reopen) the single EventSource for the current key union; closes it when no key is subscribed. */
  reopen(): void {
    const keys = this.subscribedKeys();
    const joined = keys.join(',');
    if (joined === this.openKeys && this.source) return;
    this.close();
    this.openKeys = joined;
    this.setHealth({ keys: keys.length });
    if (!keys.length) {
      this.setHealth({ state: 'idle' });
      return;
    }
    this.lastSeq.clear();             // a new stream starts each key at its current seq
    const es = this.createEventSource(`${this.url}?keys=${keys.map(encodeURIComponent).join(',')}`);
    this.source = es;
    this.setHealth({ state: 'connecting' });
    es.onopen = () => this.setHealth({ state: 'live' });
    es.onerror = () => {
      this.lastSeq.clear();
      this.setHealth({ state: 'reconnecting' });
    };
    for (const name of ['quote', 'trade', 'depth', 'status', 'heartbeat'] as const) {
      es.addEventListener(name, (ev: MessageEvent) => this.onEvent(name, ev.data as string));
    }
  }

  close(): void {
    this.source?.close();
    this.source = null;
    this.openKeys = '';
  }

  // ---------------------------------------------------------------- reads
  quote(key: string): Quote | undefined {
    return this.quotes.get(key);
  }

  status(key: string): KeyStatus | undefined {
    return this.statuses.get(key);
  }

  tradesFor(key: string): readonly Trade[] {
    return this.trades.get(key) ?? [];
  }

  depth(key: string): { bid: DepthEvent | null; ask: DepthEvent | null } {
    return this.depthBook.get(key) ?? { bid: null, ask: null };
  }

  health(): StreamHealth {
    return this.healthState;
  }

  subscribeHealth(listener: () => void): () => void {
    this.healthListeners.add(listener);
    return () => this.healthListeners.delete(listener);
  }

  // ---------------------------------------------------------------- events
  /** Apply one SSE event (public for tests and for replaying recorded streams). */
  onEvent(name: string, raw: string): void {
    const now = this.now();
    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch {
      return;
    }
    const patch: Partial<StreamHealth> = { events: this.healthState.events + 1, lastEventMs: now };
    if (name === 'heartbeat') {
      this.setHealth({ ...patch, lastHeartbeatMs: now, state: 'live' });
      return;
    }
    const ev = data as { key?: string; seq?: number; ts_pub_ns?: NsValue; source?: string; data_type?: DataType };
    const key = ev.key;
    if (typeof key !== 'string') return;
    if (name === 'quote' || name === 'trade' || name === 'depth') {
      const seqKey = `${name === 'depth' ? `depth:${(data as DepthEvent).side}` : name}|${key}`;
      const seq = ev.seq ?? 0;
      const last = this.lastSeq.get(seqKey);
      // Depth seq counts both sides together, so only quote and trade gaps are checked.
      if (last !== undefined && name !== 'depth' && seq > last + 1) {
        patch.gaps = (this.healthState.gaps ?? 0) + 1;
        this.resync(key, name);
      }
      if (last !== undefined && seq <= last && name === 'quote') return;   // stale (a resync already moved past it)
      this.lastSeq.set(seqKey, seq);
      if (ev.source && ev.data_type) patch.sources = this.withSource(ev.source, ev.data_type);
      const pubMs = nsToMs(ev.ts_pub_ns ?? null);
      if (pubMs !== null && name !== 'depth') this.recordLatency(now - pubMs);
    }
    if (name === 'quote') this.quotes.set(key, data as Quote);
    else if (name === 'trade') this.pushTrades(key, [data as Trade]);
    else if (name === 'depth') {
      const d = data as DepthEvent;
      const book = this.depthBook.get(key) ?? { bid: null, ask: null };
      book[d.side] = d;
      this.depthBook.set(key, book);
    } else if (name === 'status') {
      const s = data as KeyStatus;
      this.statuses.set(key, s);
      if (s.state === 'STALE' && /not delivered/.test(s.reason)) patch.overflows = this.healthState.overflows + 1;
    }
    this.setHealth(patch, false);
    this.markDirty(key);
  }

  private withSource(source: string, dataType: DataType): Record<string, DataType[]> {
    const cur = this.healthState.sources[source];
    if (cur?.includes(dataType)) return this.healthState.sources;
    return { ...this.healthState.sources, [source]: [...(cur ?? []), dataType] };
  }

  private pushTrades(key: string, incoming: Trade[]): void {
    const list = this.trades.get(key) ?? [];
    const merged = [...incoming.slice().reverse(), ...list];
    const seen = new Set<number>();
    const out: Trade[] = [];
    for (const t of merged.sort((a, b) => b.seq - a.seq)) {
      if (seen.has(t.seq)) continue;
      seen.add(t.seq);
      out.push(t);
      if (out.length >= this.tradesKept) break;
    }
    this.trades.set(key, out);
  }

  private recordLatency(ms: number): void {
    this.latency.push(ms);
    if (this.latency.length > LATENCY_WINDOW) this.latency.splice(0, this.latency.length - LATENCY_WINDOW);
  }

  private resync(key: string, kind: string): void {
    this.setHealth({ resyncs: this.healthState.resyncs + 1 }, false);
    if (kind === 'trade') {
      void this.fetchJson(`/api/marketdata/trades?key=${encodeURIComponent(key)}&limit=200`)
        .then((body) => {
          const rows = (body as { trades?: Trade[] }).trades ?? [];
          this.pushTrades(key, rows.map((r) => ({ ...r, seq: Number(r.seq) })));
          this.markDirty(key);
        })
        .catch(() => undefined);
      return;
    }
    this.pendingResync.add(key);
    if (this.pendingResync.size > 1) return;       // one snapshot request per frame for all gapped keys
    this.schedule(() => {
      const keys = [...this.pendingResync];
      this.pendingResync.clear();
      void this.fetchJson(`/api/marketdata/snapshot?keys=${keys.map(encodeURIComponent).join(',')}`)
        .then((body) => {
          for (const q of (body as { quotes?: Quote[] }).quotes ?? []) {
            const cur = this.quotes.get(q.key);
            if (!cur || q.seq >= cur.seq) {
              this.quotes.set(q.key, q);
              this.lastSeq.set(`quote|${q.key}`, Math.max(q.seq, cur?.seq ?? 0));
              this.markDirty(q.key);
            }
          }
        })
        .catch(() => undefined);
    });
  }

  private markDirty(key: string): void {
    this.dirty.add(key);
    if (this.flushScheduled) return;
    this.flushScheduled = true;
    this.schedule(() => this.flush());
  }

  /** Deliver one batched notification per listener whose keys changed (one call per animation frame). */
  flush(): void {
    this.flushScheduled = false;
    if (!this.dirty.size) return;
    const changed = new Set(this.dirty);
    this.dirty.clear();
    const sorted = [...this.latency].sort((a, b) => a - b);
    this.setHealth({ receiptP50Ms: pct(sorted, 50), receiptP99Ms: pct(sorted, 99) });
    for (const [listener, keys] of [...this.listeners]) {
      for (const k of keys) {
        if (changed.has(k)) {
          listener(changed);
          break;
        }
      }
    }
  }

  private setHealth(patch: Partial<StreamHealth>, notify = true): void {
    this.healthState = { ...this.healthState, ...patch };
    if (notify) for (const l of [...this.healthListeners]) l();
  }
}

// ------------------------------------------------------------------ React bindings
let singleton: MarketDataClient | null = null;

/** The terminal-wide client (created on first use). */
export function marketDataClient(): MarketDataClient {
  singleton ??= new MarketDataClient();
  return singleton;
}

const MarketDataContext = createContext<MarketDataClient | null>(null);

/** Supplies a client (tests, storybook-style previews); without it hooks use the terminal-wide client. */
export function MarketDataProvider({ client, children }: { client: MarketDataClient; children: ReactNode }) {
  return createElement(MarketDataContext.Provider, { value: client }, children);
}

export function useMarketData(): MarketDataClient {
  return useContext(MarketDataContext) ?? marketDataClient();
}

/** Stream health for the header and the monitor's status line. */
export function useStreamHealth(): StreamHealth {
  const client = useMarketData();
  return useSyncExternalStore(
    (l) => client.subscribeHealth(l),
    () => client.health(),
    () => client.health(),
  );
}

/**
 * Subscribe a component to keys; returns a version number that changes (at most once per frame) when any of them
 * changed. Read values with client.quote(key) etc. during render.
 */
export function useMarketDataKeys(keys: readonly string[]): number {
  const client = useMarketData();
  const joined = [...new Set(keys)].sort().join(',');
  const store = getKeyStore(client, joined);
  return useSyncExternalStore(store.subscribe, store.get, store.get);
}

const keyStores = new WeakMap<MarketDataClient, Map<string, { subscribe: (l: () => void) => () => void; get: () => number }>>();

function getKeyStore(client: MarketDataClient, joined: string) {
  let perClient = keyStores.get(client);
  if (!perClient) {
    perClient = new Map();
    keyStores.set(client, perClient);
  }
  let store = perClient.get(joined);
  if (!store) {
    let version = 0;
    const keys = joined ? joined.split(',') : [];
    const map = perClient;
    let users = 0;
    store = {
      get: () => version,
      subscribe: (l: () => void) => {
        users += 1;
        const off = client.subscribe(keys, () => {
          version += 1;
          l();
        });
        return () => {
          off();
          users -= 1;
          if (users === 0) map.delete(joined);
        };
      },
    };
    perClient.set(joined, store);
  }
  return store;
}
