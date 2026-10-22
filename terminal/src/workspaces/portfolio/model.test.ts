/** Portfolio model. Shapes follow GET /api/portfolio/*; every number below is a TEST FIXTURE. */
import { describe, expect, it } from 'vitest';
import {
  allocationParamErrors,
  backtestDays,
  exactBars,
  exactShares,
  fmtFloatMoney,
  fmtNum,
  fmtPct,
  irrStatus,
  kupiecVerdict,
  linePath,
  ofKind,
  parsePortfolioRoute,
  scopeOptions,
  sessionOfScope,
  stressParamError,
  thresholdRows,
  twrStatus,
  unavailableReason,
  weightPct,
  weightRows,
  whatIfErrors,
  whatIfStale,
} from './model';

describe('routes and scopes', () => {
  it('parses the tab and falls back to holdings', () => {
    expect(parsePortfolioRoute([])).toEqual({ tab: 'holdings' });
    expect(parsePortfolioRoute(['risk'])).toEqual({ tab: 'risk' });
    expect(parsePortfolioRoute(['nope'])).toEqual({ tab: 'holdings' });
  });

  it('offers account, system and each system owner once, never outside', () => {
    const opts = scopeOptions(['outside', 'unallocated', 'session:S1', 'protected:S0'], ['S1', 'S2']);
    expect(opts.map((o) => o.value)).toEqual([
      'account',
      'system',
      'owner:unallocated',
      'owner:buffer',
      'owner:session:S1',
      'owner:protected:S0',
      'owner:session:S2',
    ]);
    expect(opts.find((o) => o.value === 'owner:session:S1')?.label).toBe('Session S1');
    expect(sessionOfScope('owner:session:S1')).toBe('S1');
    expect(sessionOfScope('system')).toBeNull();
  });

  it('reads kinds and the no-history answer', () => {
    const body = { available: false, kind: 'performance', reason: 'no valuation snapshots yet: run `python -m unknown.analytics.snapshots record --account <key>`' };
    expect(ofKind(body, 'performance')).toBe(body);
    expect(ofKind(body, 'pnl')).toBeNull();
    expect(unavailableReason(ofKind(body, 'performance'))).toMatch(/snapshots record/);
    expect(unavailableReason({ kind: 'performance', twr: 0.01 })).toBeNull();
  });
});

describe('statistical formatting (display only)', () => {
  it('formats fractions as percentages with an explicit sign when asked', () => {
    expect(fmtPct(0.008080808)).toBe('0.81%');
    expect(fmtPct(0.008080808, 2, true)).toBe('+0.81%');
    expect(fmtPct(-0.004020100502512563)).toBe('−0.40%');
    expect(fmtPct(0)).toBe('0.00%');
    expect(fmtPct(-0.00001)).toBe('0.00%');
    expect(fmtPct(null)).toBe('—');
    expect(fmtNum(1.7199374787803063, 2)).toBe('1.72');
    expect(fmtNum(-0.5, 1)).toBe('−0.5');
    expect(fmtFloatMoney(0.4017543859649123, 'USD')).toBe('0.40 USD');
  });

  it('formats exact decimal weights without floats', () => {
    expect(weightPct('0.15276851')).toBe('15.28%');
    expect(weightPct('-166.08791209')).toBe('−16608.79%');
    expect(weightPct(null)).toBe('—');
  });
});

describe('IRR uniqueness and TWR', () => {
  it('shows a rate only for a unique root', () => {
    expect(irrStatus({ irr_annualized: 0.12, period_mwr: 0.12, days: 400, reason: null })).toMatchObject({ flag: 'unique', annualized: 0.12, tone: 'ok' });
    expect(irrStatus({ irr_annualized: null, period_mwr: 0.008, days: 24, reason: 'period of 24 days is shorter than one year: only the period MWR is shown (GIPS 2.A.12)' })).toMatchObject({
      flag: 'unique_short_period',
      periodMwr: 0.008,
      annualized: null,
    });
    expect(irrStatus({ irr_annualized: null, reason: '3 sign changes: the money-weighted return is not unique' })).toMatchObject({ flag: 'not_unique', tone: 'bad', periodMwr: null });
    expect(irrStatus({ irr_annualized: null, reason: 'no rate in (-99.9999%, 1e8%) solves the cash-flow equation' })).toMatchObject({ flag: 'no_solution' });
    expect(irrStatus(undefined)).toMatchObject({ flag: 'undefined' });
  });

  it('reads the TWR, its annualization reason and chain problems', () => {
    expect(twrStatus({ twr: 0.0081, twr_annualized: null, twr_annualized_reason: 'period of 24 days is shorter than one year' })).toMatchObject({
      twr: 0.0081,
      annualized: null,
      annualizedReason: 'period of 24 days is shorter than one year',
    });
    expect(twrStatus({ twr: null, reason: 'a sub-period return is undefined', problems: [{ start: 'a', end: 'b', reason: 'BMV + CF is not positive' }] }).problems).toEqual([
      { start: 'a', end: 'b', reason: 'BMV + CF is not positive' },
    ]);
  });

  it('explains a Kupiec result in the right direction', () => {
    expect(kupiecVerdict({ exceptions: 0, expected_exceptions: 1.9, observations: 19, p_value: 0.0454 })).toMatchObject({ tone: 'warn', text: expect.stringMatching(/too conservative/) });
    expect(kupiecVerdict({ exceptions: 6, expected_exceptions: 1.9, observations: 19, p_value: 0.01 })).toMatchObject({ tone: 'bad', text: expect.stringMatching(/understates losses/) });
    expect(kupiecVerdict({ exceptions: 2, expected_exceptions: 1.9, observations: 19, p_value: 0.9 })?.tone).toBe('ok');
    expect(kupiecVerdict({ exceptions: 0 })?.tone).toBe('neutral');
    expect(kupiecVerdict(null)).toBeNull();
  });
});

describe('exact chart geometry', () => {
  it('scales bars to the largest magnitude on decimal strings', () => {
    expect(exactBars([{ key: 'a', value: '0.250000' }, { key: 'b', value: '-0.500000' }, { key: 'c', value: '0' }, { key: 'd', value: null }])).toEqual([
      { key: 'a', value: '0.250000', bps: 5000, negative: false },
      { key: 'b', value: '-0.500000', bps: 10000, negative: true },
      { key: 'c', value: '0', bps: 0, negative: false },
    ]);
    expect(exactBars([{ key: 'z', value: '0.00' }])).toEqual([{ key: 'z', value: '0.00', bps: 0, negative: false }]);
  });

  it('splits a positive total into exact shares and reports negative parts', () => {
    const r = exactShares([
      { key: 'unallocated', value: '200.000000' },
      { key: 'session:S1', value: '99.800000' },
      { key: 'outside', value: '0.000000' },
      { key: 'exception:x', value: '-1.00' },
    ]);
    expect(r.parts).toEqual([
      { key: 'unallocated', value: '200.000000', bps: 6671 },
      { key: 'session:S1', value: '99.800000', bps: 3328 },
    ]);
    expect(r.excluded).toEqual(['exception:x']);
  });

  it('draws a line path in its box and centres a flat series', () => {
    const g = linePath([1000, 1002.5, 1001], 104, 44, 2);
    expect(g.min).toBe(1000);
    expect(g.max).toBe(1002.5);
    expect(g.points[0]).toMatchObject({ x: 2, y: 42 });
    expect(g.points[1]).toMatchObject({ x: 52, y: 2 });
    expect(g.d.startsWith('M2 42 L52 2')).toBe(true);
    expect(linePath([5, 5], 10, 10).points.map((p) => p.y)).toEqual([5, 5]);
    expect(linePath([], 10, 10).d).toBe('');
  });

  it('reads backtest days with exceptions', () => {
    expect(backtestDays({ days: [{ date: '2026-10-26', exception: true, realized_loss: 0.5, var_forecast: 0.4 }, 'x'] })).toEqual([
      { date: '2026-10-26', forecast: 0.4, realized: 0.5, exception: true },
    ]);
  });
});

describe('stress, what-if and allocation inputs', () => {
  it('reads threshold rows from either equity or system P&L forms', () => {
    const rows = thresholdRows({
      thresholds: [
        { threshold: 'session_drawdown_trigger', owner: 'session:S1', level: '98.00', crossed: true, equity_before: '99.800000', equity_after: '95.220000', loss_beyond_threshold: '2.780000' },
        { threshold: 'cumulative_loss_allowance', level: '15.00', crossed: false, system_pnl_after: '-4.780000', loss_after: '4.780000' },
      ],
    });
    expect(rows[0]).toMatchObject({ crossed: true, before: '99.800000', after: '95.220000', beyond: '2.780000' });
    expect(rows[1]).toMatchObject({ crossed: false, before: null, after: '-4.780000', owner: null });
  });

  it('validates scenario parameters as the backend parses them', () => {
    expect(stressParamError('overnight_gap', '')).toBeNull();
    expect(stressParamError('overnight_gap', '-0.10')).toBeNull();
    expect(stressParamError('overnight_gap', '-1')).toMatch(/strictly between/);
    expect(stressParamError('correlated_selloff', '20%')).toMatch(/decimal fraction/);
    expect(stressParamError('stale_quote', '0')).toMatch(/Between 1/);
    expect(stressParamError('stale_quote', '600')).toBeNull();
    expect(stressParamError('fee_revision', '0')).toMatch(/positive/);
    expect(stressParamError('fee_revision', '1.5')).toBeNull();
  });

  it('validates the what-if intent and detects a stale result', () => {
    expect(whatIfErrors({ session: '', instrument: '', side: 'BUY', quantity: '0', limit: '-1' })).toEqual({
      session: 'Choose a session.',
      instrument: 'Choose an instrument.',
      quantity: 'Enter a positive decimal quantity.',
      limit: 'Enter a positive decimal limit price.',
    });
    expect(whatIfErrors({ session: 'S1', instrument: 'FIX-EQ-1', side: 'SELL', quantity: '1', limit: '9.02' })).toEqual({});
    expect(whatIfStale(19, 19)).toBe(false);
    expect(whatIfStale(19, 20)).toBe(true);
    expect(whatIfStale(null, 20)).toBeNull();
  });

  it('merges current and target weights by instrument', () => {
    const rows = weightRows({
      universe: ['FIX-EQ-1'],
      baselines: { equal_weight: { weights: { 'FIX-EQ-1': 0.95 } }, inverse_volatility: { weights: {} } },
      mean_variance: { weights: { 'FIX-EQ-1': 0.9500000000000001 }, current_weights: { 'FIX-EQ-1': 0.4589 } },
    });
    expect(rows).toEqual([{ instrument_id: 'FIX-EQ-1', current: 0.4589, equal_weight: 0.95, inverse_volatility: null, mean_variance: 0.9500000000000001 }]);
    expect(allocationParamErrors({ cash_reserve: '0.05', floor: '', cap: '1.5', eta: 'x', tau: '' })).toEqual({ cap: 'A decimal fraction between 0 and 1.', eta: 'A non-negative number.' });
  });
});
