/**
 * The chart's view of the market-data stream (docs/design/marketdata-stream-contract.md). The shared client
 * (src/state/marketdata.ts) is built elsewhere; the chart depends only on `MarketDataFeed`, so the coordinator can
 * swap `eventSourceFeed` for an adapter over the shared client without touching the chart.
 */
import type { Tick } from './model';

export type FeedState = 'connecting' | 'open' | 'unavailable';

export interface FeedStatus {
  key: string;
  state: string;
  reason: string | null;
}

export interface FeedHandlers {
  onTick: (key: string, tick: Tick, source: string | null) => void;
  onStatus?: (status: FeedStatus) => void;
  onState?: (state: FeedState) => void;
}

export interface MarketDataFeed {
  /** Subscribe to keys; returns the unsubscribe function. */
  subscribe(keys: readonly string[], handlers: FeedHandlers): () => void;
}

/** One SSE message to a tick (trade or quote with `last`), a status, or nothing. */
export function parseStreamMessage(
  type: string,
  data: string,
): { kind: 'tick'; key: string; tick: Tick; source: string | null } | { kind: 'status'; status: FeedStatus } | null {
  let d: Record<string, unknown>;
  try {
    d = JSON.parse(data) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (!d || typeof d !== 'object' || typeof d.key !== 'string') return null;
  const ts = (typeof d.ts_source_ns === 'string' || typeof d.ts_source_ns === 'number' ? d.ts_source_ns : d.ts_recv_ns) ?? null;
  const tsNs = ts === null ? null : String(ts);
  const dataType = typeof d.data_type === 'string' ? d.data_type : 'SYNTHETIC';
  const source = typeof d.source === 'string' ? d.source : null;
  if (type === 'trade' && typeof d.price === 'string' && tsNs) {
    return { kind: 'tick', key: d.key, source, tick: { kind: 'trade', price: d.price, size: typeof d.size === 'string' ? d.size : null, tsNs, dataType } };
  }
  if (type === 'quote' && typeof d.last === 'string' && tsNs) {
    return { kind: 'tick', key: d.key, source, tick: { kind: 'quote', price: d.last, size: null, tsNs, dataType } };
  }
  if (type === 'status' && typeof d.state === 'string') {
    return { kind: 'status', status: { key: d.key, state: d.state, reason: typeof d.reason === 'string' ? d.reason : null } };
  }
  return null;
}

type EventSourceCtor = new (url: string) => {
  addEventListener(type: string, fn: (e: { data: string }) => void): void;
  onopen: (() => void) | null;
  onerror: (() => void) | null;
  readyState: number;
  close(): void;
};

/** Minimal adapter: one EventSource per subscription on GET /api/marketdata/stream (same-origin owner cookie). */
export function eventSourceFeed(path = '/api/marketdata/stream', Ctor?: EventSourceCtor): MarketDataFeed {
  return {
    subscribe(keys, handlers) {
      const ES = Ctor ?? (globalThis as unknown as { EventSource?: EventSourceCtor }).EventSource;
      if (!ES || keys.length === 0) {
        handlers.onState?.('unavailable');
        return () => {};
      }
      handlers.onState?.('connecting');
      const es = new ES(`${path}?keys=${keys.map(encodeURIComponent).join(',')}`);
      const on = (type: string) => (e: { data: string }) => {
        const m = parseStreamMessage(type, e.data);
        if (m?.kind === 'tick') handlers.onTick(m.key, m.tick, m.source);
        else if (m?.kind === 'status') handlers.onStatus?.(m.status);
      };
      for (const t of ['trade', 'quote', 'status']) es.addEventListener(t, on(t));
      es.onopen = () => handlers.onState?.('open');
      // readyState 2 = CLOSED: the browser will not retry (e.g. the stream route does not exist yet).
      es.onerror = () => handlers.onState?.(es.readyState === 2 ? 'unavailable' : 'connecting');
      return () => es.close();
    },
  };
}
