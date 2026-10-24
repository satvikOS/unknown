import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ResultsView } from './ScreenerWorkspace';
import { EMPTY_DRAFT, displayValue, fromDefinition, newCriterion, parseScreenerRoute, toDefinition, type FieldInfo, type FieldValue, type RunResponse } from './model';

const F = (field: string, unit: FieldInfo['unit']): FieldInfo => ({
  field, label: field, unit, sources: ['sec'], kind: 'metric', formula: `${field}@1`, basis: 'TTM', nm_rule: null, needs_price: unit === 'price',
});
const FIELDS = [F('gross_margin', 'ratio'), F('revenue_ttm', 'money'), F('pe_ttm', 'x'), F('price', 'price')];
const val = (value: string | null, extra: Partial<FieldValue> = {}): FieldValue => ({
  value, status: value === null ? 'MISSING' : 'OK', unit: null, currency: null, formula: 'x@1', period: 'TTM to 2026-06-27', input_obs_ids: [1, 2], flags: [], ...extra,
});

describe('screener model', () => {
  it('builds a definition from the draft and names the first problem', () => {
    expect(toDefinition({ ...EMPTY_DRAFT, sources: { sec: false, esef: false } }, FIELDS)).toEqual({ error: expect.stringContaining('universe source') });
    const bad = { ...EMPTY_DRAFT, criteria: [{ ...newCriterion(FIELDS[0]!), value: '40%' }] };
    expect(toDefinition(bad, FIELDS)).toEqual({ error: expect.stringContaining('plain decimal') });
    const noCur = { ...EMPTY_DRAFT, criteria: [{ ...newCriterion(FIELDS[1]!), value: '1000', currency: '' }] };
    expect(toDefinition(noCur, FIELDS)).toEqual({ error: expect.stringContaining('currency') });
    const between = { ...EMPTY_DRAFT, criteria: [{ ...newCriterion(FIELDS[2]!), op: 'between' as const, value: '5' }] };
    expect(toDefinition(between, FIELDS)).toEqual({ error: expect.stringContaining('upper value') });
    const ok = toDefinition({ ...EMPTY_DRAFT, sicFrom: '3500', sortField: 'revenue_ttm', columns: ['pe_ttm', 'nope'],
      criteria: [{ ...newCriterion(FIELDS[1]!), value: ' 1000000 ', currency: 'usd' }, { ...newCriterion(FIELDS[0]!), value: '0.4', missing: 'include' }] }, FIELDS);
    expect(ok).toEqual({ definition: {
      universe: { sources: ['sec', 'esef'], sic_from: 3500 },
      criteria: [{ field: 'revenue_ttm', op: 'gte', value: '1000000', missing: 'exclude', nm: 'exclude', currency: 'USD' },
        { field: 'gross_margin', op: 'gte', value: '0.4', missing: 'include', nm: 'exclude' }],
      sort: { field: 'revenue_ttm', direction: 'desc' }, limit: 200, columns: ['pe_ttm'],
    } });
    if ('definition' in ok) expect(toDefinition(fromDefinition(ok.definition), FIELDS)).toEqual(ok);
  });

  it('formats values by unit and leaves a missing value to the reason marker', () => {
    expect(displayValue(val('0.4865291556'), 'ratio')).toBe('48.65 %');
    expect(displayValue(val('31.256'), 'x')).toBe('31.26x');
    expect(displayValue(val('416161000000.4', { currency: 'USD' }), 'money')).toBe('416,161,000,000 USD');
    expect(displayValue(val('250', { currency: 'USD' }), 'price')).toBe('250.00 USD');
    expect(displayValue(val(null), 'ratio')).toBeNull();
    expect(parseScreenerRoute(['run', 'run-abc'])).toEqual({ runId: 'run-abc' });
    expect(parseScreenerRoute([])).toEqual({ runId: null });
  });
});

describe('screener results view', () => {
  const r: RunResponse = {
    as_of: '2026-09-01T00:00:00.000000000Z', knowledge_cutoff: '2026-12-02T00:00:00.000000000Z', truncated: false, columns: ['gross_margin', 'pe_ttm'],
    counts: { PASS: 1, UNKNOWN: 1, FAIL: 2, EXCLUDED: 1 }, excluded_by_reason: { 'gross_margin: NOT_APPLICABLE': 1 },
    results: [
      { entity_id: 'SEC-CIK-0009000001', name: 'Fixture Widgets Inc (synthetic)', source: 'sec', cik: 9000001, lei: null, ticker: 'FXW', exchange: 'Nasdaq',
        sic: '3571', country: null, outcome: 'PASS', has_data_at_as_of: true, checks: [], price: null,
        values: { gross_margin: val('0.4'), pe_ttm: val(null, { status: 'NO_PRICE', reason: 'NO_SOURCE', detail: 'no owner price input' }) } },
      { entity_id: 'SEC-CIK-0009000004', name: 'Fixture Loss Maker (synthetic)', source: 'sec', cik: 9000004, lei: null, ticker: 'FXLM', exchange: 'Nasdaq',
        sic: '3571', country: null, outcome: 'UNKNOWN', has_data_at_as_of: true, checks: [], price: null,
        values: { gross_margin: val('0.3'), pe_ttm: val(null, { status: 'NM', reason: 'NOT_APPLICABLE', detail: 'NM if net income TTM <= 0' }) } },
    ],
    excluded: [{ entity_id: 'ESEF-LEI-X', name: 'Fixture Europa SE (synthetic)', source: 'esef', cik: null, lei: 'X', ticker: null, exchange: null, sic: null,
      country: 'FR', has_data_at_as_of: true, reasons: [{ field: 'gross_margin', reason: 'NOT_APPLICABLE', detail: 'ESEF issuers file annual reports only' }] }],
    manifest: { engine: 'screener@1', definition: { universe: { sources: ['sec'] }, criteria: [], sort: null, limit: 200, columns: [] }, definition_hash: 'd'.repeat(64),
      as_of: '', knowledge_cutoff: '', universe: { count: 5, by_source: { sec: 4, esef: 1 } }, price_inputs: {}, formulas: [],
      data_versions: { 'sec.companyfacts': { observations: 1200, max_obs_id: 1300, latest_available: '2026-08-01T00:00:00.000000000Z' } } },
    result_hash: 'r'.repeat(64), computed_at: '', live_quotes: 'not used: run with an as-of time',
    coverage: { universe: 5, with_data_at_as_of: 5, without_data_at_as_of: 0, note: 'The universe is the issuers held in this research store' },
  };

  it('shows counts, reasons for missing and NM values, excluded rows and the manifest', () => {
    const html = renderToStaticMarkup(createElement(ResultsView, { r, fields: FIELDS }));
    expect(html).toContain('1 pass');
    expect(html).toContain('2 fail');
    expect(html).toContain('40.00 %');
    expect(html).toContain('data-reason="NO_SOURCE"');
    expect(html).toContain('data-reason="NOT_APPLICABLE"');
    expect(html).toContain(' NM');
    expect(html).toContain('gross_margin: NOT_APPLICABLE (1)');
    expect(html).toContain('ESEF issuers file annual reports only');
    expect(html).toContain('d'.repeat(64));
    expect(html).toContain('1200 observations');
    expect(html).toContain('not used: run with an as-of time');
    expect(html).not.toMatch(/>0\.00 %</);
  });

  it('says when nothing passes instead of showing an empty grid', () => {
    const html = renderToStaticMarkup(createElement(ResultsView, { r: { ...r, results: [] }, fields: FIELDS }));
    expect(html).toContain('No issuer passes');
    expect(html).not.toContain('<table');
  });
});
