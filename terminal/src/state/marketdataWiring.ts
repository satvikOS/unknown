/**
 * Connects the shared market-data client (state/marketdata.ts) to the parts that were built against interfaces:
 * the shell's status bar (ui/streamHealth.ts), the header ticker tape (ui/headerWidgets.tsx) and the chart's
 * MarketDataFeed (workspaces/chart/feed.ts). One EventSource serves all of them.
 */
import { setChartFeed } from '../workspaces/chart/ChartWorkspace';
import type { FeedHandlers, MarketDataFeed } from '../workspaces/chart/feed';
import { TickerTape } from '../workspaces/monitor/TickerTape';
import { registerHeaderWidget } from '../ui/headerWidgets';
import { registerStreamHealthProvider, type StreamHealth as ShellHealth } from '../ui/streamHealth';
import type { DataType } from '../ui/live';
import { marketDataClient, type MarketDataClient, type StreamHealth } from './marketdata';

const STALE_AFTER_MS = 15_000;   // three missed 5 s heartbeats

/** The client's health in the shell's status-bar shape. */
export function toShellHealth(h: StreamHealth, nowMs: number): ShellHealth {
  const silent = h.lastEventMs === null || nowMs - h.lastEventMs > STALE_AFTER_MS;
  const state: ShellHealth['state'] =
    h.state === 'live' ? (silent ? 'stale' : 'live') : h.state === 'idle' ? 'down' : 'connecting';
  const sources = Object.entries(h.sources).flatMap(([source, types]) =>
    types.map((dataType) => ({ source, dataType: dataType as DataType, keys: h.keys })),
  );
  const measured = h.receiptP50Ms !== null || h.receiptP99Ms !== null;
  return {
    state,
    sources,
    latencyMs: measured ? { p50: h.receiptP50Ms, p99: h.receiptP99Ms } : null,
    lastEventMs: h.lastEventMs,
    detail: h.gaps || h.overflows ? `${h.gaps} gaps, ${h.overflows} overflows, ${h.resyncs} resyncs` : undefined,
  };
}

/** A chart feed over the shared client: each new trade (by seq) and each changed `last` becomes a tick. */
export function clientChartFeed(client: MarketDataClient): MarketDataFeed {
  return {
    subscribe(keys: readonly string[], handlers: FeedHandlers): () => void {
      const lastTradeSeq = new Map<string, number>();
      const lastQuoteSeq = new Map<string, number>();
      for (const k of keys) lastTradeSeq.set(k, client.tradesFor(k)[0]?.seq ?? -1);
      handlers.onState?.('connecting');
      const unsubscribe = client.subscribe(keys, (changed) => {
        handlers.onState?.('open');
        for (const key of changed) {
          const st = client.status(key);
          if (st && st.state !== 'SUBSCRIBED') handlers.onStatus?.({ key, state: st.state, reason: st.reason ?? null });
          const seen = lastTradeSeq.get(key) ?? -1;
          const fresh = client.tradesFor(key).filter((t) => t.seq > seen).reverse();   // oldest first
          for (const t of fresh) {
            const ts = t.ts_source_ns ?? t.ts_recv_ns;
            if (ts === null) continue;
            handlers.onTick(key, { kind: 'trade', price: t.price, size: t.size, tsNs: String(ts), dataType: t.data_type }, t.source);
          }
          const newest = fresh[fresh.length - 1];
          if (newest) lastTradeSeq.set(key, newest.seq);
          const q = client.quote(key);
          if (q && q.last !== null && q.seq !== lastQuoteSeq.get(key)) {
            lastQuoteSeq.set(key, q.seq);
            const ts = q.ts_source_ns ?? q.ts_recv_ns;
            if (ts !== null && !fresh.length) {
              handlers.onTick(key, { kind: 'quote', price: q.last, size: null, tsNs: String(ts), dataType: q.data_type }, q.source);
            }
          }
        }
      });
      return unsubscribe;
    },
  };
}

let installed = false;

/** Install the wiring once at startup (main.tsx). */
export function installMarketDataWiring(client: MarketDataClient = marketDataClient()): void {
  if (installed) return;
  installed = true;
  registerStreamHealthProvider({
    subscribe: (l) => client.subscribeHealth(l),
    getSnapshot: (() => {
      let lastIn: StreamHealth | null = null;
      let lastOut: ShellHealth | null = null;
      return () => {
        const h = client.health();
        if (h !== lastIn || lastOut === null) {
          lastIn = h;
          lastOut = toShellHealth(h, Date.now());
        }
        return lastOut;
      };
    })(),
  });
  registerHeaderWidget({ id: 'ticker-tape', label: 'Ticker tape', component: TickerTape, order: 10 });
  setChartFeed(clientChartFeed(client));
}
