/**
 * Global macro helpers and the rendered comparison table (CAP-MAC-05/06/09). Values are synthetic.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { listFunctions } from '../../ui/functions';
import { ComparisonTable } from './GlobalMacro';
import { cellText, chartSeries, parseCountries, periodLabel, yearTicks, type GlobalComparison, type GlobalSeries } from './global';
import { macroRoute, parseMacroRoute } from './model';
import './index';

const col = (column_id: string, label: string) => ({
  column_id,
  indicator: 'policy_rate',
  provider: 'bis',
  dataset: 'bis.cbpol',
  series_code: 'WS_CBPOL:M',
  label,
  unit: 'percent',
  frequency: 'M',
  definition: 'def',
  attribution: 'Source: BIS',
});

const country = (code: string, name: string, euro: boolean) => ({ code, iso3: code, name, currency: euro ? 'EUR' : 'USD', euro_area: euro, eu_member: euro, venues: [] });

describe('global macro helpers', () => {
  it('labels periods by kind', () => {
    expect(periodLabel({ period_kind: 'YEAR', period_end: '2025-12-31' })).toBe('2025');
    expect(periodLabel({ period_kind: 'MONTH', period_end: '2026-09-30' })).toBe('2026-09');
    expect(periodLabel({ period_kind: 'QUARTER', period_end: '2026-09-30' })).toBe('2026-Q3');
  });

  it('never shows a missing cell as zero', () => {
    expect(cellText({ column_id: 'x', dataset: 'd', provider: 'p', status: 'NOT_COVERED', value: null })).toBe('NOT_COVERED');
    expect(cellText(undefined)).toBe('NOT_INGESTED');
    expect(cellText({ column_id: 'x', dataset: 'd', provider: 'p', status: 'OK', value: '0.00' })).toBe('0.00');
  });

  it('keeps known countries in registry order and round-trips the route', () => {
    expect(parseCountries('de, us,XX', ['US', 'IN', 'DE'])).toEqual(['US', 'DE']);
    const o = parseMacroRoute(macroRoute({ as_of: '2026-10-23', col: 'oecd_cpi_yoy', countries: 'US,DE' }));
    expect(o).toEqual({ as_of: '2026-10-23', col: 'oecd_cpi_yoy', countries: 'US,DE' });
  });

  it('charts each OK series with gaps for missing values and exact labels', () => {
    const r: GlobalSeries = {
      as_of: '2026-10-23T00:00:00Z',
      column: col('bis_policy_rate', 'Policy rate'),
      series: [
        {
          country: 'DE',
          area: 'XM',
          status: 'OK',
          points: [
            { period_start: '2026-08-01', period_end: '2026-08-31', period_kind: 'MONTH', value: '2.00', missing_reason: null, unit: 'percent', obs_id: 7, available_time: 't', flags: [] },
            { period_start: '2026-09-01', period_end: '2026-09-30', period_kind: 'MONTH', value: null, missing_reason: 'NOT_REPORTED', unit: 'percent', obs_id: 8, available_time: 't', flags: [] },
          ],
        },
        { country: 'FR', area: 'XM', status: 'SAME_AS', points: [] },
      ],
    };
    const s = chartSeries(r);
    expect(s).toHaveLength(1);
    expect(s[0]!.label).toBe('DE (XM)');
    expect(s[0]!.points[1]!.y).toBeNull();
    expect(s[0]!.points[0]!.label).toContain('2.00 percent (obs 7)');
    expect(yearTicks(s).map((t) => t.label)).toEqual(['2026', '2027']);
  });

  it('registers the ECST command-line function', () => {
    expect(listFunctions([]).some((f) => f.mnemonic === 'ECST' && f.workspace === 'macro')).toBe(true);
  });
});

describe('comparison table', () => {
  it('shows exact values with period and the reason for anything missing', () => {
    const c: GlobalComparison = {
      as_of: '2026-10-23T12:00:00Z',
      columns: [col('bis_policy_rate', 'Policy rate, % end of month (BIS)'), col('oecd_unemployment', 'Unemployment (OECD)')],
      rows: [
        {
          country: country('DE', 'Germany', true),
          cells: {
            bis_policy_rate: { column_id: 'bis_policy_rate', dataset: 'bis.cbpol', provider: 'bis', status: 'OK', value: '2.00', area: 'XM', period_kind: 'MONTH', period_end: '2026-09-30', available_time: 't', obs_id: 5, flags: ['area_rate:XM'], note: 'Euro area rate (applies to Germany)' },
            oecd_unemployment: { column_id: 'oecd_unemployment', dataset: 'oecd.sdmx_macro', provider: 'oecd', status: 'NOT_INGESTED', value: null, reason: 'not ingested' },
          },
        },
        {
          country: country('IN', 'India', false),
          cells: {
            bis_policy_rate: { column_id: 'bis_policy_rate', dataset: 'bis.cbpol', provider: 'bis', status: 'OK', value: '5.50', area: 'IN', period_kind: 'MONTH', period_end: '2026-09-30', available_time: 't', obs_id: 6, flags: [] },
            oecd_unemployment: { column_id: 'oecd_unemployment', dataset: 'oecd.sdmx_macro', provider: 'oecd', status: 'NOT_COVERED', value: null, reason: 'does not cover India' },
          },
        },
      ],
      datasets: {},
      fred: { status: 'INACTIVE', key_file: '~/.unknown/secrets/fred_api_key' },
      blocked_sources: [],
      note: 'one column per source',
    };
    const html = renderToStaticMarkup(createElement(ComparisonTable, { c }));
    expect(html).toContain('2.00');
    expect(html).toContain('2026-09');
    expect(html).toContain('XM');
    expect(html).toContain('NOT_COVERED');
    expect(html).toContain('NOT_INGESTED');
    expect(html).toContain('euro area');
    expect(html).not.toMatch(/>0<|>0\.00</);
  });
});
