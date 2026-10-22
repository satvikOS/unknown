import { describe, expect, it } from 'vitest';
import { findWorkspace } from '../registry';
import { crossRows, curveSeries, fxRows, isDate, macroRoute, parseMacroRoute, tenorTicks, type CurvePoint, type FxResponse } from './model';

const pt = (tenor: string, value: string, obs: number): CurvePoint => ({
  tenor,
  years: '1',
  value_pct: value,
  obs_id: obs,
  available_time: '2026-10-21T22:30:00.000000000Z',
  publication_id: 'ust-par:2026-10-21',
  flags: [],
});

describe('macro registration and route', () => {
  it('is registered as Markets & macro on g m', () => {
    expect(findWorkspace('macro')).toMatchObject({ title: 'Markets & macro', shortcut: 'g m', order: 120 });
  });

  it('round-trips its options', () => {
    const o = { as_of: '2026-10-21T22:00:00Z', date: '2026-10-20', compare: '2026-10-21', base: 'USD' };
    expect(parseMacroRoute(macroRoute(o))).toEqual(o);
    expect(parseMacroRoute(['date=', 'nonsense', 'fx_date=2026-10-20'])).toEqual({ fx_date: '2026-10-20' });
    expect(macroRoute({})).toEqual([]);
  });

  it('validates calendar dates', () => {
    expect(isDate('2026-10-21')).toBe(true);
    expect(isDate('2026-02-30')).toBe(false);
    expect(isDate('2026-10-21T00:00:00Z')).toBe(false);
  });
});

describe('curve chart series', () => {
  it('keeps a missing tenor as a gap at its position (never interpolated)', () => {
    const s = curveSeries([pt('1M', '4.31', 1), pt('2M', '4.28', 3), pt('30Y', '4.62', 14)]);
    expect(s).toHaveLength(14);
    expect(s[0]).toMatchObject({ x: 0, y: 4.31 });
    expect(s[1]).toMatchObject({ x: 1, y: null, label: '1.5M: missing' });
    expect(s[2]).toMatchObject({ x: 2, y: 4.28 });
    expect(s[13]).toMatchObject({ x: 13, y: 4.62 });
    expect(s[0]!.label).toContain('4.31 percent (obs 1');
  });

  it('labels every tenor, appending unknown ones at the end', () => {
    const ticks = tenorTicks([pt('10Y', '4.2', 1)], [pt('25Y', '4.5', 2)]);
    expect(ticks.map((t) => t.label)).toEqual(['1M', '1.5M', '2M', '3M', '4M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '20Y', '30Y', '25Y']);
    expect(curveSeries([pt('25Y', '4.5', 2)])[14]).toMatchObject({ x: 14, y: 4.5 });
  });
});

describe('reference rates', () => {
  const fx: FxResponse = {
    status: 'OK',
    as_of: 'x',
    requested_date: null,
    stale: [],
    unavailable: [],
    dataset: 'ecb.exr_reference',
    attribution: 'a',
    use_note: 'u',
    rates: {
      USD: { currency: 'USD', rate: '1.1634', orientation: 'USD per 1 EUR', reference_date: '2026-10-21', obs_id: 1, available_time: 't', flags: [] },
      CHF: { currency: 'CHF', rate: '0.9302', orientation: 'CHF per 1 EUR', reference_date: '2026-10-21', obs_id: 2, available_time: 't', flags: [] },
    },
    normalized: {
      base: 'USD',
      rates: {
        JPY: { value: '0.0065', meaning: 'USD per 1 JPY', formula: 'f', inputs: [1, 3], reference_dates: ['2026-10-21'], flags: [], label: 'derived', rounding: 'r' },
        EUR: { value: '1.1634000000', meaning: 'USD per 1 EUR', formula: 'f', inputs: [1], reference_dates: ['2026-10-21'], flags: [], label: 'derived', rounding: 'r' },
      },
    },
  };
  it('sorts published rates by currency and puts EUR first among crosses', () => {
    expect(fxRows(fx).map((r) => r.currency)).toEqual(['CHF', 'USD']);
    expect(crossRows(fx).map((r) => r.currency)).toEqual(['EUR', 'JPY']);
    expect(crossRows({ ...fx, normalized: { base: 'XXX', status: 'UNAVAILABLE', reason: 'no ECB rate for base currency XXX' } })).toEqual([]);
  });
});
