/**
 * Rendered monitor panels (react-dom/server, no DOM) fed by a client with test-only events (source TEST, data type
 * REALTIME). A missing value renders as an em dash with its reason; an event of a non-market data type is an error.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { findWorkspace } from '../registry';
import { MarketDataClient, MarketDataProvider } from '../../state/marketdata';
import { DepthPanel, TimeAndSales, WatchlistGrid } from './panels';
import { TickerTape } from './TickerTape';

function client(): MarketDataClient {
  const c = new MarketDataClient({ schedule: () => undefined, timer: () => undefined, now: () => 0 });
  c.onEvent('quote', JSON.stringify({
    key: 'AAPL.XNAS', seq: 1, bid: '187.20', ask: '187.22', bid_size: '300', ask_size: '400', last: '187.21',
    last_size: '100', volume: '123456', open: null, high: null, low: null, prev_close: '185.00', currency: 'USD',
    source: 'TEST', data_type: 'REALTIME', feed: 'test', ts_source_ns: null,
    ts_recv_ns: '1761220800000000000', ts_pub_ns: '1761220800000000000',
  }));
  c.onEvent('status', JSON.stringify({ key: 'MSFT.XNAS', state: 'LINE_BUDGET', reason: 'ALPACA: 30 of 30 lines in use' }));
  c.onEvent('status', JSON.stringify({ key: 'NVDA.XNAS', state: 'NOT_ENTITLED', reason: 'no market-data subscription' }));
  c.onEvent('quote', JSON.stringify({
    key: 'META.XNAS', seq: 1, bid: '500.00', ask: '500.10', bid_size: null, ask_size: null, last: '500.05',
    last_size: null, volume: null, open: null, high: null, low: null, prev_close: null, currency: 'USD',
    source: 'GEN', data_type: 'SYNTHETIC', ts_source_ns: null, ts_recv_ns: '1', ts_pub_ns: '1',
  }));
  c.onEvent('trade', JSON.stringify({
    key: 'AAPL.XNAS', seq: 1, price: '187.21', size: '100', exchange: 'XNAS', conditions: ['@'],
    source: 'TEST', data_type: 'REALTIME', ts_source_ns: null, ts_recv_ns: '1761220800000000000',
  }));
  c.onEvent('depth', JSON.stringify({
    key: 'AAPL.XNAS', seq: 1, side: 'bid', levels: [{ price: '187.20', size: '300', mm: null },
      { price: '187.19', size: '200', mm: null }], source: 'TEST', data_type: 'REALTIME', ts_recv_ns: '1',
  }));
  return c;
}

function render(node: ReturnType<typeof createElement>, c = client()): string {
  return renderToStaticMarkup(createElement(MarketDataProvider, { client: c, children: node }));
}

const grid = (keys: string[]) =>
  render(createElement(WatchlistGrid, { list: { id: 'l', name: 'Test', keys }, selected: keys[0] ?? null, onRemove: () => undefined, onMove: () => undefined }));

describe('monitor rendering', () => {
  it('registers Monitor (g w, 52)', () => {
    expect(findWorkspace('monitor')).toMatchObject({ title: 'Monitor', shortcut: 'g w', order: 52 });
  });

  it('shows quotes with change, sizes and the source/data-type badge, and line-budget refusals per row', () => {
    const html = grid(['AAPL.XNAS', 'MSFT.XNAS']);
    expect(html).toContain('187.21');
    expect(html).toContain('2.21');
    expect(html).toContain('1.19%');
    expect(html).toContain('123,456');
    expect(html).toContain('>TEST REALTIME<');
    expect(html).toContain('LINE BUDGET');
    expect(html).toContain('30 of 30 lines in use');
    expect(html).toContain('data-reason="LINE_BUDGET"');
  });

  it('renders every missing cell as an em dash with its reason, never zero or blank', () => {
    const html = grid(['NVDA.XNAS', 'JPM.XNYS']);
    expect(html).toContain('data-reason="NOT_ENTITLED"');
    expect(html).toContain('no market-data subscription');
    expect(html).toContain('data-reason="NOT_REPORTED"');
    expect(html).toContain('No quote received yet.');
    expect(html).not.toMatch(/<td class="mon-num[^"]*"><span[^>]*>0<\/span>/);
    expect(html).not.toContain('N/A');
  });

  it('never displays an event of a non-market data type: the row is an error', () => {
    const html = grid(['META.XNAS']);
    expect(html).not.toContain('500.05');
    expect(html).toContain('>ERR<');
    expect(html).toContain('data type SYNTHETIC is not real market data');
  });

  it('renders time & sales, depth with cumulative size, and the ticker tape', () => {
    expect(render(createElement(TimeAndSales, { securityKey: 'AAPL.XNAS' }))).toContain('XNAS');
    const depth = render(createElement(DepthPanel, { securityKey: 'AAPL.XNAS' }));
    expect(depth).toContain('187.19');
    expect(depth).toContain('>500<');
    expect(render(createElement(DepthPanel, { securityKey: 'JPM.XNYS' }))).toContain('No depth for JPM.XNYS');
    const tape = render(createElement(TickerTape, { keys: ['AAPL.XNAS', 'META.XNAS'] }));
    expect(tape).toContain('Ticker tape');
    expect(tape).toContain('1.19%');
    expect(tape).toContain('>ERR<');
    expect(tape).not.toContain('500.05');
  });
});
