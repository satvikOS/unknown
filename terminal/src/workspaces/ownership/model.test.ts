import { describe, expect, it } from 'vitest';
import { changeTone, filterInsiders, fractionPct, holderMatches, parseOwnershipRoute, roundDecimal, type Holder, type InsiderTxn } from './model';

const txn = (category: string): InsiderTxn => ({
  obs_id: 1, accession: 'a', form: '4', filing_date: '2026-09-11', available_time: '2026-09-11T21:00:00.000000000Z', insider: 'Doe Jane (FIXTURE)',
  role: 'Officer', table: 'non_derivative', kind: 'transaction', security: 'Common Stock', date: '2026-09-10', code: 'S', code_label: 'open market or private sale',
  category, shares: '1000', price: '200.50', price_reason: null, acquired_disposed: 'D', shares_after: '50000', direct_indirect: 'D', nature: null,
  superseded_by_amendment: false, links: { filing_index: null, document: null },
});

describe('ownership model', () => {
  it('parses the route: key upper-cased, unknown tab falls back to institutional', () => {
    expect(parseOwnershipRoute(['aapl.xnas', 'insiders'])).toEqual({ key: 'AAPL.XNAS', tab: 'insiders' });
    expect(parseOwnershipRoute(['320193', 'nope'])).toEqual({ key: '320193', tab: 'institutional' });
    expect(parseOwnershipRoute([])).toEqual({ key: null, tab: 'institutional' });
  });

  it('rounds exactly for display and shows fractions as percentages', () => {
    expect(roundDecimal('0.0796890911', 4)).toBe('0.0797');
    expect(roundDecimal('-1.005', 2)).toBe('-1.01');
    expect(roundDecimal('12', 2)).toBe('12');
    expect(fractionPct('0.0796890911')).toBe('7.97 %');
    expect(fractionPct(null)).toBeNull();
    expect(fractionPct('not a number')).toBeNull();
  });

  it('tones changes and filters rows', () => {
    expect(changeTone('ENTRY')).toBe('ok');
    expect(changeTone('EXIT')).toBe('bad');
    expect(changeTone('NO_PRIOR_REPORT')).toBe('info');
    const rows = [txn('open_market_sale'), txn('other'), txn('holding'), txn('open_market_purchase')];
    expect(filterInsiders(rows, 'open_market').map((r) => r.category)).toEqual(['open_market_sale', 'open_market_purchase']);
    expect(filterInsiders(rows, 'all')).toHaveLength(4);
    const h = { manager: 'BlackRock, Inc.', manager_cik: 2012383 } as Holder;
    expect(holderMatches(h, 'black')).toBe(true);
    expect(holderMatches(h, '2012')).toBe(true);
    expect(holderMatches(h, 'vanguard')).toBe(false);
  });
});
