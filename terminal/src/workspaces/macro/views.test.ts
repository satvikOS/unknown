/**
 * Rendered Markets & macro views (react-dom/server, no browser): what the owner reads, checked as text.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CurveTable } from './MacroWorkspace';
import type { CurvePoint, CurveResponse } from './model';

const point = (tenor: string, years: string, value_pct: string, obs_id: number): CurvePoint => ({
  tenor,
  years,
  value_pct,
  obs_id,
  available_time: '2026-10-21T22:30:00.000000000Z',
  publication_id: 'ust-par:x',
  flags: [],
});

const curve = (curve_date: string, points: CurvePoint[], missing: string[]): CurveResponse => ({
  status: missing.length ? 'PARTIAL' : 'OK',
  curve: 'UST par nominal',
  curve_date,
  as_of: '2026-10-22T12:00:00.000000000Z',
  unit: 'percent',
  points,
  missing_tenors: missing,
  slope_10y_2y: null,
  dataset: 'ust.par_yield_curve',
});

describe('par curve table', () => {
  it('keeps the comparison value of a tenor the main curve does not carry', () => {
    const main = curve('2026-10-21', [point('1M', '1/12', '4.31', 2943)], ['1.5M']);
    const cmp = curve('2026-10-20', [point('1M', '1/12', '4.31', 2929), point('1.5M', '1.5/12', '4.30', 2930)], []);
    const html = renderToStaticMarkup(createElement(CurveTable, { c: main, k: cmp }));
    const row = /<tr><th scope="row" class="mono">1\.5M<\/th>(.*?)<\/tr>/.exec(html)?.[1] ?? '';
    expect(row).toContain('4.30');
    expect(row).toContain('missing on 2026-10-21');
    expect(row).toContain('obs 2930');
  });

  it('without a comparison, a missing tenor is a row that says so (never interpolated)', () => {
    const main = curve('2026-10-21', [point('1M', '1/12', '4.31', 2943)], ['1.5M']);
    const html = renderToStaticMarkup(createElement(CurveTable, { c: main, k: null }));
    expect(html).toContain('missing on 2026-10-21 (not published or not available at as of); not interpolated');
  });
});
