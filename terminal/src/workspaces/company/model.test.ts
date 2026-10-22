import { describe, expect, it } from 'vitest';
import { WORKSPACES, findWorkspace } from '../registry';
import {
  EMPTY_DCF,
  carriedOptions,
  cellCensus,
  companyRoute,
  dcfParams,
  isRefused,
  isUnavailable,
  parseCompanyRoute,
  parsePeerList,
  priceParams,
  priceProblems,
  sameDecimal,
  statementOrder,
  unitLabel,
  type StatementResponse,
} from './model';

describe('company registration', () => {
  it('is registered with its own shortcut and order', () => {
    const w = findWorkspace('company');
    expect(w).toMatchObject({ title: 'Company', shortcut: 'g e', order: 110 });
    expect(WORKSPACES.filter((x) => x.shortcut === 'g e')).toHaveLength(1);
  });
});

describe('company route', () => {
  it('parses the search view, an issuer, its tab and options', () => {
    expect(parseCompanyRoute([])).toEqual({ cik: null, tab: 'tearsheet', opts: {} });
    expect(parseCompanyRoute(['search', 'q=Fixture Widgets', 'as_of=2026-08-15'])).toEqual({ cik: null, tab: 'tearsheet', opts: { q: 'Fixture Widgets', as_of: '2026-08-15' } });
    expect(parseCompanyRoute(['9000001'])).toEqual({ cik: 9000001, tab: 'tearsheet', opts: {} });
    expect(parseCompanyRoute(['9000001', 'statements', 'basis=Q', 'periods=4', 'junk', 'bogus=1', 'as_of='])).toEqual({
      cik: 9000001,
      tab: 'statements',
      opts: { basis: 'Q', periods: '4' },
    });
    // a value may itself contain "=" or ":" (instants)
    expect(parseCompanyRoute(['9000001', 'metrics', 'price_time=2026-10-21T20:00:00Z']).opts.price_time).toBe('2026-10-21T20:00:00Z');
    expect(parseCompanyRoute(['0']).cik).toBeNull();
    expect(parseCompanyRoute(['AAPL']).cik).toBeNull();
  });

  it('builds routes that parse back to the same view', () => {
    const r = { cik: 9000001, tab: 'dcf' as const, opts: { as_of: '2026-08-15', price: '50.00', price_currency: 'USD', price_source: 'x', price_time: '2026-08-14T20:00:00Z' } };
    expect(parseCompanyRoute(companyRoute(r))).toEqual(r);
    expect(companyRoute({ cik: null, opts: {} })).toEqual([]);
    expect(companyRoute({ cik: null, opts: { q: 'abc' } })).toEqual(['search', 'q=abc']);
  });

  it('carries as-of and the price input across issuers, not the search text or tab options', () => {
    expect(carriedOptions({ q: 'x', as_of: 'a', basis: 'Q', price: '1', price_currency: 'USD', price_source: 's', price_time: 't', peers: '1' })).toEqual({
      as_of: 'a',
      price: '1',
      price_currency: 'USD',
      price_source: 's',
      price_time: 't',
    });
  });
});

describe('price input', () => {
  const ok = { price: '50.00', price_currency: 'usd', price_source: 'FIXTURE price', price_time: '2026-10-21T20:00:00Z' };
  it('requires currency, source and a zoned time with a price', () => {
    expect(priceProblems(ok)).toEqual([]);
    expect(priceProblems({ ...ok, price: '' , price_source: '' })).toEqual([]); // no price: nothing required
    expect(priceProblems({ ...ok, price_source: ' ' })).toEqual(['price source is required (where the price was observed)']);
    expect(priceProblems({ ...ok, price_time: '2026-10-21T20:00:00' })).toEqual(['price time must include a time zone, e.g. 2026-10-21T20:00:00Z']);
    expect(priceProblems({ ...ok, price: '50,00', price_currency: 'US' })).toHaveLength(2);
  });

  it('passes all four parameters, upper-casing the currency, or none', () => {
    expect(priceParams({ price: '50', price_currency: 'usd', price_source: 's', price_time: 't' })).toEqual({ price: '50', price_currency: 'USD', price_source: 's', price_time: 't' });
    expect(priceParams({ as_of: '2026-01-01' })).toEqual({});
  });

  it('reads explicit peer lists as CIKs', () => {
    expect(parsePeerList('9000002, CIK9000003 9000004')).toEqual({ ok: true, value: '9000002,9000003,9000004' });
    expect(parsePeerList('9000002, AAPL')).toEqual({ ok: false, error: 'not a CIK: AAPL' });
  });
});

describe('DCF parameters', () => {
  it('builds the explicit-forecast CAPM request', () => {
    const r = dcfParams({ ...EMPTY_DCF, g: '0.02', fcff: '100, 110 120', beta: '1.0', erp: '0.05', kd: '0.05', we: '0.8', wd: '0.2', tax: '0.21' });
    expect(r).toEqual({ ok: true, params: { g: '0.02', fcff: '100,110,120', rf_tenor: '10Y', beta: '1.0', erp: '0.05', kd: '0.05', tax: '0.21', we: '0.8', wd: '0.2' } });
  });

  it('builds the growth path with a given WACC and overrides', () => {
    const r = dcfParams({ ...EMPTY_DCF, g: '0.02', flows: 'growth', fcff_growth: '0.03', years: '5', rate: 'wacc', wacc: '0.09', step_g: '0.0025', allow_financial: true });
    expect(r).toEqual({ ok: true, params: { g: '0.02', fcff_growth: '0.03', years: '5', wacc: '0.09', step_g: '0.0025', allow_financial: '1' } });
  });

  it('passes an explicit risk-free rate and its currency', () => {
    const r = dcfParams({ ...EMPTY_DCF, g: '0.02', fcff: '1', rf_mode: 'explicit', rf: '0.04', rate_currency: 'eur' });
    expect(r.ok && r.params).toMatchObject({ rf: '0.04', rate_currency: 'EUR' });
    expect(r.ok && 'rf_tenor' in r.params).toBe(false);
  });

  it('checks syntax only and leaves sufficiency to the API (REFUSED with its reason)', () => {
    const r = dcfParams({ ...EMPTY_DCF, g: '', fcff: '100, abc', flows: 'explicit' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.errors).toEqual(['g is required', 'fcff: not decimal numbers: abc']);
    const y = dcfParams({ ...EMPTY_DCF, g: '0.02', flows: 'growth', fcff_growth: '0.03', years: '31' });
    expect(!y.ok && y.errors).toEqual(['years must be a whole number from 1 to 30']);
    // missing beta / erp / kd is the API's call, not the form's
    expect(dcfParams({ ...EMPTY_DCF, g: '0.02', fcff: '1' }).ok).toBe(true);
  });

  it('recognises REFUSED and UNAVAILABLE bodies', () => {
    expect(isRefused({ status: 'REFUSED', reason: 'WACC (0.09) must exceed terminal growth g (0.10) (book eq. 17.1)' })).toBe(true);
    expect(isUnavailable({ status: 'UNAVAILABLE', reason: 'research store not initialised' })).toBe(true);
    expect(isUnavailable({ status: 'UNAVAILABLE' })).toBe(false);
    expect(isRefused({ model: 'dcf_fcff_gordon@1' })).toBe(false);
  });

  it('compares decimal strings exactly', () => {
    expect(sameDecimal('0.020', '0.02')).toBe(true);
    expect(sameDecimal('0.0818200000', '0.08182')).toBe(true);
    expect(sameDecimal('0.0818200001', '0.08182')).toBe(false);
    expect(sameDecimal(null, '1')).toBe(false);
  });
});

describe('statements', () => {
  const period = { label: 'FY2025', kind: 'FY', start: '2024-09-29', end: '2025-09-27', days: 364, fiscal_year: 2025, fiscal_quarter: null, fiscal_start: '2024-09-29', flags: [] };
  const cell = (value: string | null, method: string, missing: string | null = null) => ({
    item: 'x',
    period,
    value,
    unit: 'USD',
    method,
    rule: 'std-map@1:x',
    concept: null,
    formula: null,
    missing_reason: missing,
    detail: null,
    not_reported: [],
    flags: [],
    obs_ids: [],
    latest_input_available_time: null,
  });
  const resp = {
    statements: {
      shares: [{ item: 's', label: 'S', unit: 'shares', rule: '', note: '', cells: [cell('1', 'REPORTED')] }],
      income: [{ item: 'r', label: 'R', unit: 'USD', rule: '', note: '', cells: [cell('450000', 'REPORTED'), cell(null, 'MISSING', 'NOT_REPORTED')] }],
      extra_section: [{ item: 'e', label: 'E', unit: 'USD', rule: '', note: '', cells: [cell(null, 'MISSING', 'NOT_DERIVABLE'), cell(null, 'MISSING', 'NOT_REPORTED')] }],
    },
  } as unknown as StatementResponse;

  it('orders known statements first and keeps unknown ones', () => {
    expect(statementOrder(resp.statements)).toEqual(['income', 'shares', 'extra_section']);
  });

  it('counts cells by method and missing reason', () => {
    expect(cellCensus(resp)).toEqual({
      total: 5,
      methods: [
        ['MISSING', 3],
        ['REPORTED', 2],
      ],
      missing: [
        ['NOT_REPORTED', 2],
        ['NOT_DERIVABLE', 1],
      ],
    });
  });

  it('labels per-share units', () => {
    expect(unitLabel('USD/shares')).toBe('USD per share');
    expect(unitLabel('shares')).toBe('shares');
    expect(unitLabel('USD')).toBe('USD');
  });
});
