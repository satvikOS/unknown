import { describe, expect, it } from 'vitest';
import { parseSecurityKey } from '../ui/link';
import { MarketDataClient, type StreamHealth } from './marketdata';
import { clientChartFeed, toShellHealth } from './marketdataWiring';

function client(): MarketDataClient {
  const es = { addEventListener() {}, close() {}, onopen: null, onerror: null, readyState: 1 };
  return new MarketDataClient({
    createEventSource: () => es as never,
    fetchJson: async () => ({ quotes: [], status: [] }),
    schedule: (cb) => cb(),
    timer: () => 0,
    reopenDelayMs: 0,
  });
}

const trade = (seq: number, price: string) =>
  JSON.stringify({ key: 'AAPL.XNAS', seq, price, size: '10', exchange: null, conditions: [], source: 'TEST',
    data_type: 'REALTIME', ts_source_ns: String(1_700_000_000_000_000_000 + seq), ts_recv_ns: '1', ts_pub_ns: '1' });

describe('market-data wiring', () => {
  it('feeds each new trade to the chart once, oldest first, with its source and data type', () => {
    const c = client();
    const ticks: string[] = [];
    const off = clientChartFeed(c).subscribe(['AAPL.XNAS'], {
      onTick: (key, t, source) => ticks.push(`${key} ${t.kind} ${t.price} ${t.dataType} ${source}`),
    });
    c.onEvent('trade', trade(1, '190.00'));
    c.onEvent('trade', trade(2, '190.05'));
    c.flush();
    c.onEvent('trade', trade(2, '190.05'));   // redelivered: no second tick
    c.flush();
    expect(ticks).toEqual(['AAPL.XNAS trade 190.00 REALTIME TEST', 'AAPL.XNAS trade 190.05 REALTIME TEST']);
    off();
  });

  it('maps client health to the status bar, stale after missed heartbeats, latency only when measured', () => {
    const h: StreamHealth = { state: 'live', keys: 3, events: 9, lastEventMs: 1_000, lastHeartbeatMs: 1_000, gaps: 0,
      resyncs: 0, overflows: 0, receiptP50Ms: null, receiptP99Ms: null, sources: { IBKR: ['REALTIME', 'DELAYED'] } };
    const live = toShellHealth(h, 2_000);
    expect(live.state).toBe('live');
    expect(live.latencyMs).toBeNull();
    expect(live.sources.map((s) => `${s.source}:${s.dataType}`)).toEqual(['IBKR:REALTIME', 'IBKR:DELAYED']);
    expect(toShellHealth(h, 60_000).state).toBe('stale');
    expect(toShellHealth({ ...h, receiptP50Ms: 1.5, receiptP99Ms: 4 }, 2_000).latencyMs).toEqual({ p50: 1.5, p99: 4 });
  });

  it('accepts every four-character MIC, not only those starting with X', () => {
    expect(parseSecurityKey('SPY.ARCX')).toEqual({ symbol: 'SPY', mic: 'ARCX' });
    expect(parseSecurityKey('AAPL.XNAS')).toEqual({ symbol: 'AAPL', mic: 'XNAS' });
    expect(parseSecurityKey('BRK.B')).toEqual({ symbol: 'BRK.B', mic: null });
  });
});
