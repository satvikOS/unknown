/** Market-data client: one EventSource, ref-counted keys, frame-batched notifications, seq-gap resync, health.
 * Every price is test-only data (source TEST, data type REALTIME). */
import { describe, expect, it } from 'vitest';
import { MarketDataClient, type Quote } from './marketdata';
import type { EventSourceLike } from './stream';

class FakeES implements EventSourceLike {
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;
  readonly handlers = new Map<string, (ev: MessageEvent) => void>();
  constructor(readonly url: string) {}
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    this.handlers.set(type, listener);
  }
  close(): void {
    this.closed = true;
  }
  emit(type: string, data: object): void {
    this.handlers.get(type)?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

function quote(key: string, seq: number, over: Partial<Quote> = {}): Quote {
  return {
    key, seq, bid: '10.00', ask: '10.02', bid_size: '100', ask_size: '200', last: '10.01', last_size: '100',
    volume: '1000', open: null, high: null, low: null, prev_close: '9.00', currency: 'USD', source: 'TEST',
    data_type: 'REALTIME', ts_source_ns: null, ts_recv_ns: '1700000000000000000', ts_pub_ns: '1700000000000000000',
    ...over,
  };
}

function setup() {
  const sources: FakeES[] = [];
  const frames: (() => void)[] = [];
  const timers: (() => void)[] = [];
  const fetched: string[] = [];
  let snapshot: unknown = { quotes: [] };
  const client = new MarketDataClient({
    createEventSource: (url) => {
      const es = new FakeES(url);
      sources.push(es);
      return es;
    },
    schedule: (cb) => frames.push(cb),
    timer: (cb) => timers.push(cb),
    fetchJson: async (url) => {
      fetched.push(url);
      return snapshot;
    },
    now: () => 1_700_000_000_005,
  });
  return {
    client, sources, fetched,
    setSnapshot: (s: unknown) => (snapshot = s),
    runTimers: () => timers.splice(0).forEach((t) => t()),
    runFrames: () => frames.splice(0).forEach((f) => f()),
  };
}

describe('market-data client', () => {
  it('opens one stream for the union of ref-counted keys and reopens only when the union changes', () => {
    const t = setup();
    const offA = t.client.subscribe(['AAPL.XNAS', 'MSFT.XNAS'], () => undefined);
    const offB = t.client.subscribe(['AAPL.XNAS'], () => undefined);
    t.runTimers();
    expect(t.sources).toHaveLength(1);
    expect(t.sources[0]?.url).toBe('/api/marketdata/stream?keys=AAPL.XNAS,MSFT.XNAS');
    offB();
    t.runTimers();
    expect(t.sources).toHaveLength(1);
    offA();
    t.runTimers();
    expect(t.sources[0]?.closed).toBe(true);
    expect(t.client.health().state).toBe('idle');
  });

  it('batches notifications to one per frame with the changed keys', () => {
    const t = setup();
    const calls: string[][] = [];
    t.client.subscribe(['AAPL.XNAS', 'MSFT.XNAS'], (changed) => calls.push([...changed].sort()));
    t.runTimers();
    const es = t.sources[0] as FakeES;
    es.emit('quote', quote('AAPL.XNAS', 1));
    es.emit('quote', quote('AAPL.XNAS', 2, { bid: '10.01' }));
    es.emit('quote', quote('MSFT.XNAS', 1));
    expect(calls).toHaveLength(0);
    t.runFrames();
    expect(calls).toEqual([['AAPL.XNAS', 'MSFT.XNAS']]);
    expect(t.client.quote('AAPL.XNAS')?.bid).toBe('10.01');
    expect(t.client.health().sources).toEqual({ TEST: ['REALTIME'] });
  });

  it('resyncs from the snapshot on a quote seq gap and ignores older quotes', async () => {
    const t = setup();
    t.client.subscribe(['AAPL.XNAS'], () => undefined);
    t.runTimers();
    const es = t.sources[0] as FakeES;
    es.emit('quote', quote('AAPL.XNAS', 1));
    t.setSnapshot({ quotes: [quote('AAPL.XNAS', 9, { bid: '11.00' })] });
    es.emit('quote', quote('AAPL.XNAS', 5, { bid: '10.50' }));
    expect(t.client.health().gaps).toBe(1);
    t.runFrames();
    await Promise.resolve();
    await Promise.resolve();
    expect(t.fetched).toEqual(['/api/marketdata/snapshot?keys=AAPL.XNAS']);
    expect(t.client.quote('AAPL.XNAS')?.bid).toBe('11.00');
    es.emit('quote', quote('AAPL.XNAS', 8, { bid: '9.99' }));
    expect(t.client.quote('AAPL.XNAS')?.bid).toBe('11.00');
  });

  it('keeps trades newest first, re-reads time & sales on a trade gap, and counts overflow notices', async () => {
    const t = setup();
    t.client.subscribe(['AAPL.XNAS'], () => undefined);
    t.runTimers();
    const es = t.sources[0] as FakeES;
    const trade = (seq: number) => ({ key: 'AAPL.XNAS', seq, price: '10.00', size: '1', exchange: 'X', conditions: [],
      source: 'TEST', data_type: 'REALTIME', ts_source_ns: null, ts_recv_ns: '1', ts_pub_ns: '1' });
    es.emit('trade', trade(1));
    es.emit('trade', trade(2));
    expect(t.client.tradesFor('AAPL.XNAS').map((x) => x.seq)).toEqual([2, 1]);
    t.setSnapshot({ trades: [trade(3), trade(4)] });
    es.emit('trade', trade(5));
    await Promise.resolve();
    await Promise.resolve();
    expect(t.fetched[0]).toBe('/api/marketdata/trades?key=AAPL.XNAS&limit=200');
    expect(t.client.tradesFor('AAPL.XNAS').map((x) => x.seq)).toEqual([5, 4, 3, 2, 1]);
    es.emit('status', { key: 'AAPL.XNAS', state: 'STALE', reason: '3 trade(s) not delivered: buffer full' });
    expect(t.client.health().overflows).toBe(1);
    expect(t.client.status('AAPL.XNAS')?.state).toBe('STALE');
  });

  it('never stores an event of a non-market data type: the key is in ERROR until real data arrives', async () => {
    const t = setup();
    t.client.subscribe(['AAPL.XNAS', 'MSFT.XNAS'], () => undefined);
    t.runTimers();
    const es = t.sources[0] as FakeES;
    es.emit('quote', quote('AAPL.XNAS', 1, { source: 'GEN', data_type: 'SYNTHETIC' as never }));
    expect(t.client.quote('AAPL.XNAS')).toBeUndefined();
    expect(t.client.status('AAPL.XNAS')).toEqual({
      key: 'AAPL.XNAS', state: 'ERROR', reason: 'Refused quote from GEN: data type SYNTHETIC is not real market data.',
    });
    es.emit('trade', { key: 'MSFT.XNAS', seq: 1, price: '1', size: '1', exchange: null, conditions: [], source: 'GEN', ts_recv_ns: '1' });
    expect(t.client.tradesFor('MSFT.XNAS')).toEqual([]);
    expect(t.client.status('MSFT.XNAS')?.reason).toBe('Refused trade from GEN: data type missing is not real market data.');
    expect(t.client.health().sources).toEqual({});
    es.emit('quote', quote('AAPL.XNAS', 2));
    expect(t.client.quote('AAPL.XNAS')?.data_type).toBe('REALTIME');
    expect(t.client.status('AAPL.XNAS')).toBeUndefined();
    // A snapshot resync refuses the same way.
    t.setSnapshot({ quotes: [quote('AAPL.XNAS', 9, { bid: '11.00', data_type: 'FIXTURE' as never })] });
    es.emit('quote', quote('AAPL.XNAS', 5));
    t.runFrames();
    await Promise.resolve();
    await Promise.resolve();
    expect(t.client.quote('AAPL.XNAS')?.bid).toBe('10.00');
    expect(t.client.status('AAPL.XNAS')?.state).toBe('ERROR');
  });

  it('reports stream state and heartbeats through the health provider', () => {
    const t = setup();
    let notified = 0;
    t.client.subscribeHealth(() => (notified += 1));
    t.client.subscribe(['JPM.XNYS'], () => undefined);
    t.runTimers();
    const es = t.sources[0] as FakeES;
    expect(t.client.health().state).toBe('connecting');
    es.onopen?.(new Event('open'));
    expect(t.client.health().state).toBe('live');
    es.emit('heartbeat', { ts_pub_ns: '1700000000000000000' });
    expect(t.client.health().lastHeartbeatMs).toBe(1_700_000_000_005);
    es.onerror?.(new Event('error'));
    expect(t.client.health().state).toBe('reconnecting');
    expect(notified).toBeGreaterThan(2);
  });
});
