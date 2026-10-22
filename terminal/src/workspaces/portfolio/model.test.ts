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
  sentence,
  sentences,
  sessionOfScope,
  shortOwnerLabel,
  shortSessionId,
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

  it('shortens long session ids for narrow labels only', () => {
    const id = 'ses-01a164fe-c610-752e-b019-255e2ff6c578';
    expect(shortOwnerLabel(`session:${id}`)).toBe('Session ses-01a164fe…');
    expect(shortOwnerLabel('session:S1')).toBe('Session S1');
    expect(shortOwnerLabel('unallocated')).toBe('Unallocated');
    expect(shortSessionId('ses-01a164fe-c6')).toBe('ses-01a164fe…');
    expect(shortSessionId('ses-01a164fe-c')).toBe('ses-01a164fe-c'); // cutting two characters would not save space
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

  it('shows backend fragments as sentences', () => {
    expect(sentence('estimated and final fees shown separately')).toBe('Estimated and final fees shown separately.');
    expect(sentence('already a sentence.')).toBe('Already a sentence.');
    expect(sentence(null)).toBe('');
    expect(sentences(['gains positive', null, '', 'fees (ch09)'])).toBe('Gains positive. Fees (ch09)');
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
    // The 5% is the test's significance level, not the VaR confidence level (which is 95% or 99%).
    const ok = kupiecVerdict({ exceptions: 2, expected_exceptions: 1.9, observations: 19, p_value: 0.9 })?.text ?? '';
    expect(ok).toContain('Not rejected at the 5% significance level');
    expect(ok).not.toMatch(/confidence level at 5%/);
    expect(kupiecVerdict({ exceptions: 6, expected_exceptions: 1.9, observations: 19, p_value: 0.01 })?.text).toContain('Rejected at the 5% significance level.');
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
    const rows = thresholdRows(
      {
        pnl_by_owner: [{ owner: 'session:S1', currency: 'USD', pnl: '-4.580000' }],
        thresholds: [
          { threshold: 'session_drawdown_trigger', owner: 'session:S1', level: '98.00', crossed: true, equity_before: '99.800000', equity_after: '95.220000', loss_beyond_threshold: '2.780000' },
          { threshold: 'cumulative_loss_allowance', level: '15.00', crossed: false, system_pnl_after: '-4.780000', loss_after: '4.780000', pnl_high_water: '0' },
        ],
      },
      'USD',
    );
    expect(rows[0]).toMatchObject({ crossed: true, measure: 'equity', currency: 'USD', before: '99.800000', after: '95.220000', loss: null, beyond: '2.780000' });
    // The level of an account row is compared with the loss, not with the P&L after.
    expect(rows[1]).toMatchObject({ crossed: false, measure: 'loss', currency: 'USD', before: null, after: '-4.780000', loss: '4.780000', owner: null });
    expect(rows[1]?.notes).toEqual(['System P&L high-water mark 0.00 USD']);
  });

  it('never shows an undecided threshold as not crossed, and never guesses a currency', () => {
    const [unpriced] = thresholdRows({
      thresholds: [{ threshold: 'daily_loss_trigger', level: '3.00', crossed: null, reason: 'system value unavailable: unpriced FIX-EQ-9' }],
    });
    expect(unpriced).toMatchObject({ crossed: null, reason: 'system value unavailable: unpriced FIX-EQ-9', currency: null, measure: 'loss' });
    // A session owner with P&L in two currencies: the row's currency is ambiguous, so none is shown.
    const [two] = thresholdRows(
      {
        pnl_by_owner: [
          { owner: 'session:S2', currency: 'USD', pnl: '-1' },
          { owner: 'session:S2', currency: 'EUR', pnl: '-1' },
        ],
        thresholds: [{ threshold: 'session_max_loss', owner: 'session:S2', level: '95.00', crossed: false, equity_after: '97.92' }],
      },
      'USD',
    );
    expect(two?.currency).toBeNull();
  });

  it('keeps the daily window facts the backend computed', () => {
    const [daily] = thresholdRows(
      {
        thresholds: [
          {
            threshold: 'daily_loss_trigger',
            level: '3.00',
            crossed: false,
            system_pnl_before: '-1.090000',
            system_pnl_after: '-2.080000',
            risk_day: '2026-10-22',
            day_start_pnl: '-2.080000',
            day_start_source: 'none yet on this risk day: the scenario valuation would be the first and sets the day start',
            loss_after: '0.000000',
            loss_since_last_core_valuation: '3.500000',
            crossed_if_window_started_at_last_core_valuation: true,
            marks_note: 'includes stale last marks for FIX-EQ-2',
          },
        ],
      },
      'USD',
    );
    expect(daily?.loss).toBe('0.000000');
    expect(daily?.notes).toEqual([
      'Risk day 2026-10-22 starts at system P&L −2.08 USD: none yet on this risk day: the scenario valuation would be the first and sets the day start',
      'Measured from the last core valuation instead, the loss would be 3.50 USD, which would cross the level (that window is an owner decision, not the core rule)',
      'includes stale last marks for FIX-EQ-2',
    ]);
  });

  it('validates scenario parameters as the backend parses them', () => {
    expect(stressParamError('overnight_gap', '')).toBeNull();
    expect(stressParamError('overnight_gap', '-0.10')).toBeNull();
    expect(stressParamError('overnight_gap', '-1')).toMatch(/greater than -1/);
    expect(stressParamError('overnight_gap', '-1.5')).toMatch(/greater than -1/);
    expect(stressParamError('overnight_gap', '-0.999')).toBeNull();
    // The backend accepts any gap up (stress.overnight_gap refuses only g <= -1).
    expect(stressParamError('overnight_gap', '1.5')).toBeNull();
    expect(stressParamError('correlated_selloff', '1')).toBeNull();
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
