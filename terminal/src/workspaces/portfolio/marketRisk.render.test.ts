/**
 * Rendered market risk (react-dom/server). The body follows analytics/market_risk.py; every id, bar and amount is a
 * TEST FIXTURE. A figure without data renders the em dash with the server's reason, never a number.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Figure, MarketRiskBody } from './MarketRiskTab';
import type { Rec } from './model';

const fig = (value: number | null, reason: string | null = null, detail: string | null = null): Rec => ({ value, formula: 'mrisk.beta@1', reason, detail, n: 70, inputs_as_of: { 'FIX-EQ-1': '2026-10-22', 'SPY.ARCX': '2026-10-22' } });

const body: Rec = {
  kind: 'market_risk',
  as_of_ns: 1_792_850_000_000_000_000,
  benchmark: { key: 'SPY.ARCX', provider: 'IBKR', closes: 71, first_date: '2026-07-01', last_date: '2026-10-22', reason: null },
  benchmark_vol: { ...fig(0.18), formula: 'mrisk.hist_vol@1' },
  instruments: [
    { instrument_id: 'FIX-EQ-1', currency: 'USD', quantity: '4', market_value: '40.000000', bars: { provider: 'IBKR', closes: 71, last_date: '2026-10-22', note: null }, hist_vol: fig(0.36), beta: fig(2), correlation: fig(1) },
    { instrument_id: 'FIX-EQ-2', currency: 'USD', quantity: '3', market_value: '24.000000', bars: { provider: null, reason: 'NO_DATA', detail: 'no stored daily bars' }, hist_vol: fig(null, 'NO_DATA', 'no stored daily bars'), beta: fig(null, 'NO_DATA', '5 date-aligned daily returns; at least 30 are required'), correlation: fig(null, 'NO_DATA') },
  ],
  portfolio: { USD: { currency: 'USD', priced_market_value: '64.000000', portfolio_beta: { ...fig(null, 'NO_DATA', 'no beta for FIX-EQ-2'), coverage: '0.62500000' }, portfolio_vol: fig(null, 'NO_DATA') } },
  concentration: { USD: { currency: 'USD', gross: '64.000000', by_issuer: [{ issuer: 'unknown issuer', gross: '64.000000', share_of_gross: '1.00000000', weight_of_denominator: null }], by_owner_bucket: [{ owner_bucket: 'session', gross: '64.000000', share_of_gross: '1.00000000' }] } },
  scenarios: [{ shock: '-0.10', formula: 'mrisk.scenario@1', uniform: { pnl_by_currency: { USD: '-6.400000' }, not_revalued: [] }, beta: { pnl_by_currency: { USD: '-8.000000' }, not_revalued: [{ instrument_id: 'FIX-EQ-2' }] } }],
  correlation_matrix: { pairs: [], note: '' },
  lineage: { formulas: ['mrisk.beta@1'], units: 'u', source_dates: {}, missing: [], notes: [] },
};

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

describe('market risk view', () => {
  it('shows a figure with its formula and inputs as-of, or the reason', () => {
    const ok = renderToStaticMarkup(createElement(Figure, { f: fig(2) }));
    expect(ok).toContain('2.000');
    expect(ok).toContain('mrisk.beta@1 · n = 70 · inputs as of FIX-EQ-1 2026-10-22, SPY.ARCX 2026-10-22');
    const none = renderToStaticMarkup(createElement(Figure, { f: fig(null, 'NO_DATA', 'only 5 returns') }));
    expect(none).toContain('data-reason="NO_DATA"');
    expect(text(none)).not.toMatch(/\d\.\d{3}/);
  });

  it('renders positions without bars as unavailable and leaves them out of the beta scenario', () => {
    const html = renderToStaticMarkup(createElement(MarketRiskBody, { mr: body, expo: null, formulas: new Map() }));
    const t = text(html);
    expect(t).toContain('Benchmark SPY.ARCX');
    expect(t).toContain('IBKR · 71 closes to 2026-10-22');
    expect(t).toContain('1 not revalued');
    expect(t).toContain('unknown issuer');
    expect(html).toContain('beta coverage');
  });
});
