import { describe, expect, it } from 'vitest';
import { cleanParams, apiPath } from './api';
import { cumulativeReturns, extent, labelIndices, linePath, linear, maxDrawdown, niceTicks, segments } from './chartMath';
import { POPULATE, checkInstant, cikOf, extractCommands, formatFractionPct, formatStat, isCurrencyCode, isoToMs, populateCommands, ratioAsPercent, shiftDecimal } from './format';

describe('exact decimal shifting', () => {
  it('moves the decimal point without rounding', () => {
    expect(shiftDecimal('0.3878787879', 2)).toBe('38.78787879');
    expect(shiftDecimal('0.19', 2)).toBe('19');
    expect(shiftDecimal('1.5', 3)).toBe('1500');
    expect(shiftDecimal('-0.000125', 2)).toBe('-0.0125');
    expect(shiftDecimal('36.00', -2)).toBe('0.36');
    expect(shiftDecimal('4.24', -2)).toBe('0.0424');
    expect(shiftDecimal('0', 2)).toBe('0');
    expect(shiftDecimal('-0.00', 2)).toBe('0');
    expect(shiftDecimal('abc', 2)).toBeNull();
  });

  it('shows a ratio as an exact percentage', () => {
    expect(ratioAsPercent('0.1900000000')).toBe('19 %');
    expect(ratioAsPercent('1.0121212121')).toBe('101.21212121 %');
    expect(ratioAsPercent('12.5')).toBe('1,250 %');
  });

  it('recognises currency units only for three upper-case letters', () => {
    expect(isCurrencyCode('USD')).toBe(true);
    expect(isCurrencyCode('USD/shares')).toBe(false);
    expect(isCurrencyCode('ratio')).toBe(false);
    expect(isCurrencyCode(null)).toBe(false);
  });
});

describe('instants', () => {
  it('reads nanosecond API instants at millisecond precision', () => {
    expect(isoToMs('2026-10-22T16:17:52.134721000Z')).toBe(Date.UTC(2026, 9, 22, 16, 17, 52, 134));
    expect(isoToMs('2026-10-21T22:30:00Z')).toBe(Date.UTC(2026, 9, 21, 22, 30));
    expect(isoToMs('2026-10-21T22:30:00.5+02:00')).toBe(Date.UTC(2026, 9, 21, 20, 30, 0, 500));
    expect(isoToMs('2026-10-21')).toBeNull();
    expect(isoToMs(null)).toBeNull();
  });

  it('applies the API as-of rule: a date, or an instant with a zone', () => {
    expect(checkInstant('')).toEqual({ ok: true, value: '' });
    expect(checkInstant(' 2026-08-15 ')).toEqual({ ok: true, value: '2026-08-15' });
    expect(checkInstant('2026-08-15T20:00:00Z')).toEqual({ ok: true, value: '2026-08-15T20:00:00Z' });
    expect(checkInstant('2026-08-15T20:00:00.123456789+02:00').ok).toBe(true);
    const noZone = checkInstant('2026-08-15T10:00:00');
    expect(noZone.ok).toBe(false);
    if (!noZone.ok) expect(noZone.error).toMatch(/must include a time zone/);
    expect(checkInstant('2026-02-30').ok).toBe(false);
    expect(checkInstant('yesterday').ok).toBe(false);
    const named = checkInstant('2026-13-01', 'price time');
    if (!named.ok) expect(named.error).toMatch(/^price time/);
  });
});

describe('commands named by unavailable reasons', () => {
  it('finds parenthesised, back-quoted and semicolon-separated commands verbatim', () => {
    expect(extractCommands('research store not initialised: ingest an issuer first (python -m unknown.data.sec ingest --cik <CIK>)')).toEqual([
      'python -m unknown.data.sec ingest --cik <CIK>',
    ]);
    expect(
      extractCommands(
        'research store not initialised: ingest macro series first (python -m unknown.data.sources.treasury ingest --month YYYYMM; python -m unknown.data.sources.ecb ingest --last 1)',
      ),
    ).toEqual(['python -m unknown.data.sources.treasury ingest --month YYYYMM', 'python -m unknown.data.sources.ecb ingest --last 1']);
    expect(extractCommands('research registry not found at /x/experiments.sqlite; record an experiment or run `python -m unknown.research.study` first')).toEqual([
      'python -m unknown.research.study',
    ]);
    expect(extractCommands('SEC-CIK-0000001234 is not ingested; run python -m unknown.data.sec ingest --cik 1234')).toEqual([
      'python -m unknown.data.sec ingest --cik 1234',
    ]);
    expect(extractCommands('no Treasury par yield for tenor 10Y available at the as-of time (ingest with python -m unknown.data.sources.treasury)')).toEqual([
      'python -m unknown.data.sources.treasury',
    ]);
    expect(extractCommands('no licensed event calendar is subscribed')).toEqual([]);
    expect(extractCommands(null)).toEqual([]);
  });
});

describe('populate commands under an unavailable reason', () => {
  it('keeps the commands the reason names first, then adds the documented ones once', () => {
    const noStore = 'research store not initialised: ingest macro series first (python -m unknown.data.sources.treasury ingest --month YYYYMM; python -m unknown.data.sources.ecb ingest --last 1)';
    expect(populateCommands(noStore, [POPULATE.treasury, POPULATE.ecb])).toEqual([POPULATE.treasury, POPULATE.ecb]);
    expect(populateCommands(noStore)).toEqual([POPULATE.treasury, POPULATE.ecb]);
  });

  it('adds the documented command when the reason names none', () => {
    expect(populateCommands('no Treasury par curve available at the as-of time (not ingested or not yet published)', [POPULATE.treasury])).toEqual([
      'python -m unknown.data.sources.treasury ingest --month YYYYMM',
    ]);
    expect(populateCommands('no ECB reference rates available at the as-of time (not ingested or not yet published)', [POPULATE.ecb])).toEqual([
      'python -m unknown.data.sources.ecb ingest --last 1',
    ]);
    expect(populateCommands('no licensed event calendar is subscribed')).toEqual([]);
  });

  it('replaces a partial command with the complete documented one', () => {
    const rf = 'no Treasury par yield for tenor 10Y available at the as-of time (ingest with python -m unknown.data.sources.treasury)';
    expect(populateCommands(rf, [POPULATE.treasury])).toEqual(['python -m unknown.data.sources.treasury ingest --month YYYYMM']);
    // Without a documented command the reason's own text is kept verbatim.
    expect(populateCommands(rf)).toEqual(['python -m unknown.data.sources.treasury']);
  });

  it('documents the same commands the backend reasons name', () => {
    expect(POPULATE).toEqual({
      sec: 'python -m unknown.data.sec ingest --cik <CIK>',
      treasury: 'python -m unknown.data.sources.treasury ingest --month YYYYMM',
      ecb: 'python -m unknown.data.sources.ecb ingest --last 1',
      study: 'python -m unknown.research.study',
    });
  });
});

describe('float statistics', () => {
  it('formats significant digits without exponent noise', () => {
    expect(formatStat(0.12969269934230668)).toBe('0.1297');
    expect(formatStat(-6.446693601710043e-5)).toBe('-0.00006447');
    expect(formatStat(2.058807775922482, 3)).toBe('2.06');
    expect(formatStat(524)).toBe('524');
    expect(formatStat(1e-9)).toBe('1.000e-9');
    expect(formatStat(0)).toBe('0');
    expect(formatStat(null)).toBe('—');
  });

  it('formats fractions as percentages', () => {
    expect(formatFractionPct(0.35073660373999993)).toBe('35.07 %');
    expect(formatFractionPct(-0.0723)).toBe('-7.23 %');
    expect(formatFractionPct(-0.00001)).toBe('0.00 %');
    expect(formatFractionPct(undefined)).toBe('—');
  });

  it('reads CIKs from entity ids', () => {
    expect(cikOf('SEC-CIK-0009000001')).toBe(9000001);
    expect(cikOf('320193')).toBe(320193);
    expect(cikOf('CIK0000320193')).toBe(320193);
    expect(cikOf('AAPL')).toBeNull();
    expect(cikOf('0')).toBeNull();
  });
});

describe('query parameters and paths', () => {
  it('drops empty parameters and trims text', () => {
    expect(cleanParams({ as_of: ' 2026-08-15 ', basis: 'FY', price: '', known_at: undefined, n: 0, x: null })).toEqual({ as_of: '2026-08-15', basis: 'FY', n: 0 });
    expect(cleanParams({ a: '' })).toBeUndefined();
    expect(cleanParams(undefined)).toBeUndefined();
  });

  it('encodes path segments like the core client (":" stays literal)', () => {
    expect(apiPath('research', 'trials', 'FS1-TREND:T00001')).toBe('/api/research/trials/FS1-TREND:T00001');
    expect(apiPath('company', 9000001, 'metrics', 'gross_margin')).toBe('/api/company/9000001/metrics/gross_margin');
    expect(apiPath('research', 'experiments', 'a b')).toBe('/api/research/experiments/a%20b');
  });
});

describe('chart geometry', () => {
  it('computes extents over finite values only', () => {
    expect(extent([3, null, -1, Number.NaN, 2])).toEqual({ min: -1, max: 3 });
    expect(extent([null, undefined])).toBeNull();
  });

  it('chooses round ticks that cover the range', () => {
    expect(niceTicks(3.84, 4.62, 5)).toEqual([3.8, 4, 4.2, 4.4, 4.6, 4.8]);
    expect(niceTicks(0, 1, 5)).toEqual([0, 0.25, 0.5, 0.75, 1]);
    const flat = niceTicks(5, 5);
    expect(flat[0]).toBeLessThan(5);
    expect(flat[flat.length - 1]).toBeGreaterThan(5);
    expect(niceTicks(Number.NaN, 1)).toEqual([]);
  });

  it('maps linearly and handles a zero-width domain', () => {
    const s = linear([0, 10], [100, 0]);
    expect(s(0)).toBe(100);
    expect(s(5)).toBe(50);
    expect(linear([2, 2], [0, 10])(2)).toBe(5);
  });

  it('splits lines at gaps instead of interpolating', () => {
    const segs = segments([
      { x: 0, y: 4.31 },
      { x: 1, y: null },
      { x: 2, y: 4.28 },
      { x: 3, y: 4.24 },
    ]);
    expect(segs).toEqual([[{ x: 0, y: 4.31 }], [{ x: 2, y: 4.28 }, { x: 3, y: 4.24 }]]);
    expect(linePath([{ x: 1.04, y: 2 }, { x: 3, y: 4.06 }])).toBe('M1 2 L3 4.1');
  });

  it('compounds simple returns and finds the drawdown', () => {
    const path = cumulativeReturns([
      ['2021-01-05', 0.1],
      ['2021-01-06', -0.5],
      ['2021-01-07', 0.2],
    ]);
    expect(path.map((p) => p.date)).toEqual(['2021-01-05', '2021-01-06', '2021-01-07']);
    expect(path[0]!.value).toBeCloseTo(0.1, 12);
    expect(path[1]!.value).toBeCloseTo(-0.45, 12);
    expect(path[2]!.value).toBeCloseTo(-0.34, 12);
    expect(maxDrawdown(path)).toBeCloseTo(0.5, 12);
    expect(cumulativeReturns([['d1', 0.1], ['d2', Number.NaN], ['d3', 0.1]])).toHaveLength(1);
  });

  it('thins axis labels evenly and keeps both ends', () => {
    expect(labelIndices(3, 5)).toEqual([0, 1, 2]);
    expect(labelIndices(100, 5)).toEqual([0, 25, 50, 74, 99]);
    expect(labelIndices(0, 5)).toEqual([]);
  });
});
