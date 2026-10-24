import { describe, expect, it } from 'vitest';
import type { Order } from '../api/types';
import type { Quote } from '../state/marketdata';
import { prepareExport, type RightsRegistry } from './export';
import { watchlistExport } from '../workspaces/monitor/exportTables';
import { newsExport } from '../workspaces/news/exportTables';
import type { NewsItem } from '../workspaces/news/model';
import { blotterExport } from '../workspaces/orders/exportTables';
import { positionsExport } from '../workspaces/portfolio/exportTables';

const REG: RightsRegistry = {
  registry: 'AVAILABLE',
  rule: 'r',
  records: {
    alpaca: { name: 'Alpaca', verified_on: 'd', terms: [], datasets: { 'alpaca.stock_stream': { allowed_uses: ['display', 'storage'], rights_source: 's', rights_verified_on: 'd' } } },
    'sec-edgar': { name: 'SEC', verified_on: 'd', terms: [], datasets: { 'sec.latest_filings_atom': { allowed_uses: ['storage'], rights_source: 's', rights_verified_on: 'd' } } },
  },
  aliases: { 'marketdata:ALPACA': { record: 'alpaca', dataset: 'alpaca.stock_stream' }, 'news:sec-filings': { record: 'sec-edgar', dataset: 'sec.latest_filings_atom' } },
};
const AT = new Date('2026-10-24T12:00:00Z');

const quote = (source: string, data_type: Quote['data_type']): Quote =>
  ({ key: 'AAPL.XNAS', seq: 1, bid: '1.00', ask: '1.01', bid_size: '1', ask_size: '1', last: '1.005', last_size: '1', volume: '10', open: null, high: null, low: null, prev_close: '1.00', currency: 'USD', source, data_type, ts_source_ns: 1, ts_recv_ns: 1 }) as Quote;

describe('grid exports', () => {
  it('positions: ledger columns always, marks only from a source whose rights allow it', () => {
    const body = { as_of_ns: 1_792_850_000_000_000_000, core_seq: 7 };
    const pos = (source: string, delayed: boolean) => ({ owner: 'session:S1', instrument_id: 'X', symbol: 'X', currency: 'USD', quantity: '4', cost_basis: '40.00', mark: { price: '10.00', status: 'fresh', source, delayed }, market_value: '40.000000' });
    const ok = prepareExport(positionsExport(body, [pos('ALPACA', true)], { scope: 'system' }), REG, AT);
    expect(ok.headers).toContain('Market value');
    expect(ok.excluded).toEqual([]);
    const ibkr = prepareExport(positionsExport(body, [pos('IBKR', true)], { scope: 'system' }), REG, AT);
    expect(ibkr.headers).toContain('Quantity');
    expect(ibkr.headers).not.toContain('Mark');
    expect(ibkr.excluded.map((e) => e.column)).toEqual(['Mark', 'Mark status', 'Market value', 'Unrealized', 'Weight of scope NAV']);
    const fixture = prepareExport(positionsExport(body, [{ ...pos('ALPACA', true), fixture: true }], {}), REG, AT);
    expect(fixture.headers).not.toContain('Mark');
  });

  it('watchlist: delayed quotes from a recorded source export, real-time exchange data does not', () => {
    const list = { id: 'l', name: 'Main', keys: ['AAPL.XNAS'] };
    const delayed = prepareExport(watchlistExport(list, [{ key: 'AAPL.XNAS', quote: quote('ALPACA', 'DELAYED'), status: undefined }], AT.getTime()), REG, AT);
    expect(delayed.headers).toContain('Bid');
    const live = prepareExport(watchlistExport(list, [{ key: 'AAPL.XNAS', quote: quote('ALPACA', 'REALTIME'), status: undefined }], AT.getTime()), REG, AT);
    expect(live.headers).not.toContain('Bid');
    expect(live.excluded[0]!.reason).toContain('real-time exchange data');
    expect(live.rows[0]![0]!.text).toBe('AAPL.XNAS');
  });

  it('news: publisher fields by the source record; a formula-like headline is inert', () => {
    const item = { id: 5, source_id: 'sec-filings', first_seen: '2026-10-24T11:00:00Z', published: null, form: '8-K', accession: 'a', title: '=cmd|x', link: null, entities: [{ scheme: 'SEC_CIK', value: '320193' }], duplicate_of: null } as unknown as NewsItem;
    const p = prepareExport(newsExport([item], '2026-10-24T12:00:00Z', { view: 'tape' }), REG, AT);
    const title = p.headers.indexOf('Title');
    expect(p.rows[0]![title]!.text).toBe("'=cmd|x");
    const other = prepareExport(newsExport([{ ...item, source_id: 'unrecorded' }], null, {}), REG, AT);
    expect(other.headers).not.toContain('Title');
  });

  it('orders: every column is owner data and survives a missing registry', () => {
    const o = { order_id: 'O1', session_id: 'S1', instrument_id: 'X', side: 'BUY', order_type: 'LMT', time_in_force: 'DAY', quantity: '4', limit_price: '10.00', currency: 'USD', state: 'FILLED', cancel_state: 'NONE', cum_qty: '4', leaves_qty: '0', avg_price: null, notional_filled: '40.00', fees_estimated: '1.00', fees_final: '1.00', broker_order_id: null, accepted_at_ns: 1_792_850_000_000_000_000, submitted_at_ns: null, terminal_at_ns: null, purpose: 'ENTRY', reason: null } as unknown as Order;
    const p = prepareExport(blotterExport([o], new Map(), { orders: 'all' }), null, AT);
    expect(p.excluded).toEqual([]);
    expect(p.headers).toContain('Average price status');
    expect(p.rows[0]![p.headers.indexOf('Accepted')]!.text).toBe('2026-10-24T13:53:20.000Z');
  });
});
