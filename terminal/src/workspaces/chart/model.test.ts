/**
 * Chart model, stream adapter, annotations, registration and licence attribution. Every price is a TEST FIXTURE.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { findWorkspace } from '../registry';
import { loadAnnotations, newAnnotation, saveAnnotations, type KeyValueStore } from './annotations';
import { eventSourceFeed, parseStreamMessage } from './feed';
import { addDecimal, applyTick, commonStart, parseBarsResponse, percentFrom, sourceLabel, SYNTHETIC_TEXT, type ChartBar, type Tick } from './model';

const S = 1_000_000_000n;
const T0 = 1_792_000_000n * S; // fixture instant
const raw = (over: Record<string, unknown> = {}) => ({
  key: 'FIX.XNYS',
  interval: '5m',
  available: true,
  source: 'BROKERX',
  data_type: 'DELAYED',
  label: null,
  currency: 'USD',
  adjustment: 'unadjusted',
  session: { calendar_id: 'XNYS', timezone: 'America/New_York', notes: ['n1'] },
  bars: [
    { start_ns: (T0 + 300n * S).toString(), end_ns: (T0 + 600n * S).toString(), open: '10.00', high: '10.50', low: '9.90', close: '10.20', volume: '100', partial: false, complete: true },
    { start_ns: T0.toString(), end_ns: (T0 + 300n * S).toString(), open: '9.80', high: '10.10', low: '9.70', close: '10.00', volume: '50', partial: false, complete: true },
    { start_ns: 'bad', end_ns: '1', open: '1', high: '1', low: '1', close: '1' },
  ],
  gaps: [{ start_ns: '1', end_ns: '2', missing_buckets: 2 }],
  live: { interval_ns: (300n * S).toString(), session_open_ns: T0.toString(), session_close_ns: (T0 + 1800n * S).toString() },
  sources_considered: [{ source: 'BROKERX', configured: true, covers: true, reason: 'stored' }],
  ...over,
});

const tick = (price: string, at: bigint, over: Partial<Tick> = {}): Tick => ({ kind: 'trade', price, size: '5', tsNs: at.toString(), dataType: 'REALTIME', ...over });

describe('parseBarsResponse', () => {
  it('sorts, drops malformed bars and keeps exact strings', () => {
    const r = parseBarsResponse(raw())!;
    expect(r.bars.map((b) => b.close)).toEqual(['10.00', '10.20']);
    expect(r.bars[0]!.time).toBe(1_792_000_000);
    expect(r.gaps).toEqual([{ startNs: '1', endNs: '2', missing: 2 }]);
    expect(r.notes).toEqual(['n1']);
    expect(sourceLabel(r)).toBe('BROKERX · DELAYED');
  });

  it('never shows synthetic data without its label, and fails closed on an unknown data type', () => {
    expect(sourceLabel(parseBarsResponse(raw({ source: 'SYNTHETIC', data_type: 'SYNTHETIC' }))!)).toBe(SYNTHETIC_TEXT);
    const odd = parseBarsResponse(raw({ data_type: 'MYSTERY' }))!;
    expect(odd.dataType).toBe('SYNTHETIC');
    expect(odd.label).toBe(SYNTHETIC_TEXT);
  });
});

describe('applyTick (stream contract trade/quote events)', () => {
  const r = parseBarsResponse(raw())!;
  const bars: ChartBar[] = r.bars;

  it('updates the last bar exactly and adds trade volume', () => {
    const out = applyTick(bars, r.live, 'DELAYED', tick('10.75', T0 + 400n * S, { size: '2.5' }));
    const last = out.bars[1]!;
    expect([last.high, last.low, last.close, last.volume, last.live]).toEqual(['10.75', '9.90', '10.75', '102.5', true]);
    const q = applyTick(out.bars, r.live, 'DELAYED', tick('9.50', T0 + 450n * S, { kind: 'quote', size: null }));
    expect([q.bars[1]!.low, q.bars[1]!.close, q.bars[1]!.volume]).toEqual(['9.50', '9.50', '102.5']);
  });

  it('opens the next session-anchored bucket from a trade and marks it in progress', () => {
    const out = applyTick(bars, r.live, 'DELAYED', tick('11', T0 + 1000n * S));
    expect(out.bars).toHaveLength(3);
    const b = out.bars[2]!;
    expect([b.startNs, b.endNs, b.open, b.complete, b.partial]).toEqual([(T0 + 900n * S).toString(), (T0 + 1200n * S).toString(), '11', false, true]);
    expect(out.bars[1]!.complete).toBe(true);
  });

  it('asks for a refetch past the session and ignores stale or mixed-type ticks', () => {
    expect(applyTick(bars, r.live, 'DELAYED', tick('11', T0 + 5000n * S)).needsRefetch).toBe(true);
    expect(applyTick(bars, r.live, 'DELAYED', tick('11', T0 - S)).changed).toBe(false);
    expect(applyTick(bars, r.live, 'DELAYED', tick('11', T0 + 400n * S, { dataType: 'SYNTHETIC' })).changed).toBe(false);
    expect(applyTick(bars, r.live, 'SYNTHETIC', tick('11', T0 + 400n * S)).changed).toBe(false);
    expect(applyTick(bars, r.live, 'DELAYED', tick('11', T0 + 5000n * S, { kind: 'quote' })).needsRefetch).toBe(false);
  });

  it('adds decimals exactly', () => {
    expect(addDecimal('0.1', '0.2')).toBe('0.3');
    expect(addDecimal('100', '-100.25')).toBe('-0.25');
    expect(addDecimal('999999999999999999.9', '0.1')).toBe('1000000000000000000.0');
  });
});

describe('comparison rebasing', () => {
  const ser = (pts: [number, number][]) => pts.map(([time, close]) => ({ time, open: close, high: close, low: close, close, volume: null }));
  it('rebases both series to percent from the common start', () => {
    const a = ser([[1, 50], [2, 100], [3, 110]]);
    const b = ser([[2, 20], [3, 18]]);
    const t0 = commonStart(a, b)!;
    expect(t0).toBe(2);
    expect(percentFrom(a, t0).map((p) => p.value)).toEqual([0, 10.000000000000009]);
    expect(percentFrom(b, t0).map((p) => +p.value.toFixed(6))).toEqual([0, -10]);
  });
});

describe('stream adapter', () => {
  it('parses trade, quote and status messages per the contract', () => {
    const trade = parseStreamMessage('trade', JSON.stringify({ key: 'K', seq: 1, price: '1.5', size: '10', ts_source_ns: '5', ts_recv_ns: '6', source: 'BROKERX', data_type: 'REALTIME' }));
    expect(trade).toEqual({ kind: 'tick', key: 'K', source: 'BROKERX', tick: { kind: 'trade', price: '1.5', size: '10', tsNs: '5', dataType: 'REALTIME' } });
    const quote = parseStreamMessage('quote', JSON.stringify({ key: 'K', last: '1.6', ts_source_ns: null, ts_recv_ns: '7', data_type: 'DELAYED' }));
    expect(quote && quote.kind === 'tick' ? [quote.tick.kind, quote.tick.tsNs] : null).toEqual(['quote', '7']);
    expect(parseStreamMessage('quote', JSON.stringify({ key: 'K', last: null, ts_recv_ns: '7' }))).toBeNull();
    expect(parseStreamMessage('status', JSON.stringify({ key: 'K', state: 'NOT_ENTITLED', reason: 'x' }))).toEqual({ kind: 'status', status: { key: 'K', state: 'NOT_ENTITLED', reason: 'x' } });
    expect(parseStreamMessage('trade', '{nope')).toBeNull();
  });

  it('subscribes over EventSource and reports a closed stream as unavailable', () => {
    const made: { url: string; fire: (t: string, d: unknown) => void; es: { onerror: (() => void) | null; readyState: number; closed: boolean } }[] = [];
    class FakeES {
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      readyState = 0;
      closed = false;
      private fns = new Map<string, (e: { data: string }) => void>();
      constructor(url: string) {
        made.push({ url, fire: (t, d) => this.fns.get(t)?.({ data: JSON.stringify(d) }), es: this });
      }
      addEventListener(t: string, fn: (e: { data: string }) => void) {
        this.fns.set(t, fn);
      }
      close() {
        this.closed = true;
      }
    }
    const ticks: string[] = [];
    const states: string[] = [];
    const off = eventSourceFeed('/api/marketdata/stream', FakeES).subscribe(['SAP.XETR'], {
      onTick: (k, t) => ticks.push(`${k}:${t.price}`),
      onState: (s) => states.push(s),
    });
    expect(made[0]!.url).toBe('/api/marketdata/stream?keys=SAP.XETR');
    made[0]!.fire('trade', { key: 'SAP.XETR', price: '200.1', size: '1', ts_recv_ns: '9', data_type: 'DELAYED' });
    made[0]!.es.readyState = 2;
    made[0]!.es.onerror?.();
    off();
    expect([ticks, states, made[0]!.es.closed]).toEqual([['SAP.XETR:200.1'], ['connecting', 'unavailable'], true]);
  });
});

describe('annotations', () => {
  it('persist per security and survive a throwing or missing store', () => {
    const mem = new Map<string, string>();
    const store: KeyValueStore = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => void mem.set(k, v) };
    const a = newAnnotation('hline', '101.25', null, 'resistance', 1);
    expect(saveAnnotations('A.XNYS', [a], store)).toBe(true);
    expect(loadAnnotations('A.XNYS', store)).toEqual([a]);
    expect(loadAnnotations('B.XNYS', store)).toEqual([]);
    mem.set('unknown.chart.annotations.v1.A.XNYS', '[{"id":1},"x"]');
    expect(loadAnnotations('A.XNYS', store)).toEqual([]);
    const broken: KeyValueStore = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('quota');
      },
    };
    expect(loadAnnotations('A.XNYS', broken)).toEqual([]);
    expect(saveAnnotations('A.XNYS', [a], broken)).toBe(false);
    expect(saveAnnotations('A.XNYS', [a], null)).toBe(false);
  });
});

describe('registration and licence attribution', () => {
  it('is registered as the chart workspace', () => {
    const w = findWorkspace('chart');
    expect([w?.title, w?.shortcut]).toEqual(['Chart', 'g h']);
  });

  it('shows the Lightweight Charts NOTICE attribution and a tradingview.com link on the chart page', () => {
    const src = readFileSync(new URL('./ChartWorkspace.tsx', import.meta.url), 'utf8');
    expect(src).toContain("ATTRIBUTION_NOTICE = 'TradingView Lightweight Charts™ Copyright (c) 2026 TradingView, Inc.'");
    expect(src).toContain("ATTRIBUTION_URL = 'https://www.tradingview.com/'");
    expect(src).toContain('<a href={ATTRIBUTION_URL}');
    expect(src).toContain('attributionLogo: true');
  });
});
