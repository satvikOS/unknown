/** Rendered monitor panels (react-dom/server, no DOM) fed by a client with SYNTHETIC fixture events. */
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
    source: 'SYNTHETIC', data_type: 'SYNTHETIC', feed: 'synthetic', ts_source_ns: null,
    ts_recv_ns: '1761220800000000000', ts_pub_ns: '1761220800000000000',
  }));
  c.onEvent('status', JSON.stringify({ key: 'MSFT.XNAS', state: 'LINE_BUDGET', reason: 'ALPACA: 30 of 30 lines in use' }));
  c.onEvent('trade', JSON.stringify({
    key: 'AAPL.XNAS', seq: 1, price: '187.21', size: '100', exchange: 'SYNTH', conditions: ['SYNTHETIC'],
    source: 'SYNTHETIC', data_type: 'SYNTHETIC', ts_source_ns: null, ts_recv_ns: '1761220800000000000',
  }));
  c.onEvent('depth', JSON.stringify({
    key: 'AAPL.XNAS', seq: 1, side: 'bid', levels: [{ price: '187.20', size: '300', mm: null },
      { price: '187.19', size: '200', mm: null }], source: 'SYNTHETIC', data_type: 'SYNTHETIC', ts_recv_ns: '1',
  }));
  return c;
}

function render(node: ReturnType<typeof createElement>, c = client()): string {
  return renderToStaticMarkup(createElement(MarketDataProvider, { client: c, children: node }));
}

describe('monitor rendering', () => {
  it('registers Monitor (g w, 52)', () => {
    expect(findWorkspace('monitor')).toMatchObject({ title: 'Monitor', shortcut: 'g w', order: 52 });
  });

  it('shows quotes with change, sizes and the SYNTHETIC label, and line-budget refusals per row', () => {
    const html = render(createElement(WatchlistGrid, {
      list: { id: 'l', name: 'Test', keys: ['AAPL.XNAS', 'MSFT.XNAS'] }, selected: 'AAPL.XNAS',
      onRemove: () => undefined, onMove: () => undefined,
    }));
    expect(html).toContain('187.21');
    expect(html).toContain('2.21');
    expect(html).toContain('1.19%');
    expect(html).toContain('123,456');
    expect(html).toContain('>SYNTHETIC<');
    expect(html).toContain('not market data');
    expect(html).toContain('LINE BUDGET');
    expect(html).toContain('30 of 30 lines in use');
  });

  it('renders time & sales, depth with cumulative size, and the ticker tape', () => {
    expect(render(createElement(TimeAndSales, { securityKey: 'AAPL.XNAS' }))).toContain('SYNTH');
    const depth = render(createElement(DepthPanel, { securityKey: 'AAPL.XNAS' }));
    expect(depth).toContain('187.19');
    expect(depth).toContain('>500<');
    expect(render(createElement(DepthPanel, { securityKey: 'JPM.XNYS' }))).toContain('No depth for JPM.XNYS');
    const tape = render(createElement(TickerTape, { keys: ['AAPL.XNAS'] }));
    expect(tape).toContain('Ticker tape');
    expect(tape).toContain('1.19%');
    expect(tape).toContain('SYNTHETIC');
  });
});
