import { describe, expect, it } from 'vitest';
import type { ExceptionCase, Execution, HealthResponse } from '../../api/types';
import {
  allowedActions,
  describeDetail,
  estimatedFeeExecutions,
  exceptionCounts,
  healthChecks,
  healthLimits,
  normalizeSignedAmount,
  parseOpsRoute,
  reportedShortfall,
  runSummary,
  suggestedCashFlow,
  validateResolve,
  worstTone,
} from './model';

const NOW = 1_792_679_130_812_578_000;

function health(core: Partial<NonNullable<Extract<HealthResponse['core'], { now_ns: number }>>> | { error: string }, apiDeltaNs = 200_000): HealthResponse {
  if ('error' in core) return { core, api_time_ns: NOW, latest_seq: 13 };
  return {
    core: {
      broker_connected: true,
      broker_authenticated: true,
      broker_server_time_ns: NOW - 94_000,
      disk_free_mb: 74937,
      detail: 'simulated broker fixture',
      now_ns: NOW,
      cycle_errors: 0,
      provider: 'simulated',
      account_mode: 'SIMULATED',
      ...core,
    },
    api_time_ns: NOW + apiDeltaNs,
    latest_seq: 13,
  };
}

const LIMITS = healthLimits({ max_clock_skew_ms: 2000, min_free_disk_mb: 1024 });

function openCase(over: Partial<ExceptionCase> = {}): ExceptionCase {
  return {
    case_id: 'case-1',
    account_key: 'SIM-1',
    kind: 'CASH_MISMATCH',
    blocking: true,
    status: 'OPEN',
    severity: 'high',
    opened_at_ns: NOW,
    session_id: null,
    order_id: null,
    owner_hint: null,
    detail: { currency: 'USD', broker_settled: '312.340000', ledger_cash: '300.000000', due_settlements_net: '0.000000', unexplained: '12.340000' },
    key: 'recon:CASH_MISMATCH:USD',
    resolution: null,
    ...over,
  };
}

describe('parseOpsRoute', () => {
  it('reads the tab and entity id, defaulting to status', () => {
    expect(parseOpsRoute([])).toEqual({ tab: 'status', id: null });
    expect(parseOpsRoute(['exceptions', 'case-9'])).toEqual({ tab: 'exceptions', id: 'case-9' });
    expect(parseOpsRoute(['reconciliation'])).toEqual({ tab: 'reconciliation', id: null });
    expect(parseOpsRoute(['nonsense', 'x'])).toEqual({ tab: 'status', id: null });
  });
});

describe('healthChecks', () => {
  it('reports a healthy simulated core with every check ok', () => {
    const checks = healthChecks(health({}), LIMITS);
    expect(checks.map((c) => c.id)).toEqual(['core', 'broker_connection', 'broker_auth', 'clock_skew', 'api_clock', 'disk', 'core_loop']);
    expect(checks.every((c) => c.tone === 'ok')).toBe(true);
    expect(checks.find((c) => c.id === 'clock_skew')?.status).toBe('0 ms');
    expect(checks.find((c) => c.id === 'disk')?.status).toBe('74,937 MB');
  });

  it('reports an unreachable core as a single bad check with the API message', () => {
    const checks = healthChecks(health({ error: 'core unreachable: [Errno 2]' }), LIMITS);
    expect(checks).toHaveLength(1);
    expect(checks[0]).toMatchObject({ id: 'core', tone: 'bad', status: 'unreachable' });
    expect(checks[0]?.detail).toContain('[Errno 2]');
  });

  it('flags skew beyond the policy limit, low disk and repeated cycle failures', () => {
    const checks = healthChecks(
      health({ broker_server_time_ns: NOW + 2_500_000_000, disk_free_mb: 900, cycle_errors: 3, broker_authenticated: false }),
      LIMITS,
    );
    const by = Object.fromEntries(checks.map((c) => [c.id, c]));
    expect(by.clock_skew).toMatchObject({ tone: 'bad', status: '+2,500 ms' });
    expect(by.disk).toMatchObject({ tone: 'bad' });
    expect(by.disk?.detail).toContain('DISK_SPACE');
    expect(by.core_loop).toMatchObject({ tone: 'bad', status: '3 consecutive failures' });
    expect(by.broker_auth).toMatchObject({ tone: 'bad', status: 'authentication required' });
    expect(worstTone(checks.map((c) => c.tone))).toBe('bad');
  });

  it('warns on disk within twice the minimum and on a single cycle failure; neutral without a policy', () => {
    const checks = healthChecks(health({ disk_free_mb: 1500, cycle_errors: 1, broker_server_time_ns: null }), LIMITS);
    const by = Object.fromEntries(checks.map((c) => [c.id, c]));
    expect(by.disk?.tone).toBe('warn');
    expect(by.core_loop?.tone).toBe('warn');
    expect(by.clock_skew).toMatchObject({ tone: 'neutral', status: 'not reported' });
    const noPolicy = healthChecks(health({}), healthLimits(null));
    expect(noPolicy.find((c) => c.id === 'disk')?.tone).toBe('neutral');
  });

  it('accepts numeric-string limits and ignores junk', () => {
    expect(healthLimits({ max_clock_skew_ms: '1500', min_free_disk_mb: 'lots' })).toEqual({ maxClockSkewMs: 1500, minFreeDiskMb: null });
  });
});

describe('describeDetail', () => {
  it('labels decimal values as money only when the record states a currency', () => {
    const fields = describeDetail(openCase().detail);
    expect(fields.find((f) => f.key === 'unexplained')).toEqual({ key: 'unexplained', label: 'Unexplained', kind: 'money', value: '12.340000', currency: 'USD' });
    expect(fields.find((f) => f.key === 'currency')).toMatchObject({ kind: 'text', value: 'USD' });
  });

  it('treats position comparisons and fill counts as quantities, ids as links, *_ns as times', () => {
    const pos = describeDetail({ instrument_id: 'FIX-EQ-1', broker: '10', ledger: '9' }, { kind: 'POSITION_MISMATCH' });
    expect(pos).toEqual([
      { key: 'instrument_id', label: 'Instrument id', kind: 'id', value: 'FIX-EQ-1', workspace: 'instruments' },
      { key: 'broker', label: 'Broker', kind: 'quantity', value: '10' },
      { key: 'ledger', label: 'Ledger', kind: 'quantity', value: '9' },
    ]);
    const missing = describeDetail({ order_id: 'ord-1', broker_filled: '5', local: '3', expected_at_ns: NOW, lookup: { found: null }, note: 'x', ok: false });
    expect(missing.map((f) => f.kind)).toEqual(['id', 'quantity', 'quantity', 'time', 'json', 'text', 'bool']);
  });

  it('never invents a currency: an amount without one stays an unlabelled decimal', () => {
    const late = describeDetail({ settlement_id: 'stl-1', expected_date: '2026-10-20', owner: 'session:ses-1', amount: '41.990000' });
    expect(late.find((f) => f.key === 'amount')).toEqual({ key: 'amount', label: 'Amount', kind: 'decimal', value: '41.990000' });
    expect(late.find((f) => f.key === 'expected_date')).toMatchObject({ kind: 'text' });
  });

  it('handles empty and missing records', () => {
    expect(describeDetail(null)).toEqual([]);
    expect(describeDetail({ reason: '' })).toEqual([{ key: 'reason', label: 'Reason', kind: 'none' }]);
  });
});

describe('validateResolve', () => {
  it('requires a note for every action', () => {
    const r = validateResolve(openCase(), { action: 'acknowledge', note: '   ', currency: '', amount: '' });
    expect(r.ok).toBe(false);
    expect(r.errors.note).toMatch(/note is required/);
    expect(r.payload).toBeNull();
  });

  it('builds an acknowledge payload with the trimmed note', () => {
    const r = validateResolve(openCase({ kind: 'RECONCILIATION_INCOMPLETE' }), { action: 'acknowledge', note: ' checked broker statement ', currency: 'USD', amount: '5' });
    expect(r.ok).toBe(true);
    expect(r.payload).toEqual({ case_id: 'case-1', action: 'acknowledge', note: 'checked broker statement' });
    expect(r.effect).toMatch(/Nothing is booked/);
  });

  it('offers external cash flows only for CASH_MISMATCH', () => {
    expect(allowedActions({ kind: 'CASH_MISMATCH' })).toEqual(['acknowledge', 'record_external_cash_flow']);
    expect(allowedActions({ kind: 'POSITION_MISMATCH' })).toEqual(['acknowledge']);
    const r = validateResolve(openCase({ kind: 'POSITION_MISMATCH' }), { action: 'record_external_cash_flow', note: 'n', currency: 'USD', amount: '1' });
    expect(r.ok).toBe(false);
    expect(r.errors.action).toMatch(/CASH_MISMATCH/);
  });

  it('builds a deposit payload with the exact signed amount and describes the booking', () => {
    const r = validateResolve(openCase(), { action: 'record_external_cash_flow', note: 'wire received', currency: 'usd', amount: '+12.340000' });
    expect(r.ok).toBe(true);
    expect(r.payload).toEqual({ case_id: 'case-1', action: 'record_external_cash_flow', note: 'wire received', currency: 'USD', amount: '12.340000' });
    expect(r.effect).toContain('deposit of 12.34 USD');
    expect(r.effect).toContain('CASH +12.34 USD');
  });

  it('describes withdrawals and keeps the minus sign in the payload', () => {
    const r = validateResolve(openCase(), { action: 'record_external_cash_flow', note: 'fee debit', currency: 'USD', amount: '-0.5' });
    expect(r.payload).toMatchObject({ amount: '-0.5' });
    expect(r.effect).toContain('withdrawal of 0.50 USD');
  });

  it('rejects zero, malformed, comma-formatted and over-precise amounts and bad currencies', () => {
    const base = { action: 'record_external_cash_flow' as const, note: 'n', currency: 'USD' };
    expect(validateResolve(openCase(), { ...base, amount: '0.000' }).errors.amount).toMatch(/cannot be zero/);
    expect(validateResolve(openCase(), { ...base, amount: '12.3.4' }).errors.amount).toMatch(/signed amount/);
    expect(validateResolve(openCase(), { ...base, amount: '1,250.00' }).errors.amount).toMatch(/no thousands separators/);
    expect(validateResolve(openCase(), { ...base, amount: '1.0000001' }).errors.amount).toMatch(/6 decimal places/);
    expect(validateResolve(openCase(), { ...base, amount: '1', currency: 'US' }).errors.currency).toMatch(/three-letter/);
  });

  it('covers an overcommitment from a chosen source and explains acknowledgement is refused while short', () => {
    const over = openCase({ kind: 'OVERCOMMITTED', detail: { owner: 'session:ses-1', currency: 'USD', shortfall: '1.250000', ref: 'x' } });
    expect(allowedActions(over)).toEqual(['cover_shortfall', 'acknowledge']);
    expect(reportedShortfall(over)).toEqual({ currency: 'USD', amount: '1.250000' });
    const missing = validateResolve(over, { action: 'cover_shortfall', note: 'n', currency: '', amount: '', fromOwner: '' });
    expect(missing.errors.fromOwner).toMatch(/unallocated, buffer or outside/);
    const r = validateResolve(over, { action: 'cover_shortfall', note: 'fund it', currency: '', amount: '', fromOwner: 'unallocated' });
    expect(r.payload).toEqual({ case_id: 'case-1', action: 'cover_shortfall', note: 'fund it', from_owner: 'unallocated' });
    expect(r.effect).toContain('1.25 USD');
    expect(validateResolve(over, { action: 'acknowledge', note: 'n', currency: '', amount: '' }).effect).toMatch(/SHORTFALL_UNCOVERED/);
    expect(validateResolve(openCase(), { action: 'cover_shortfall', note: 'n', currency: '', amount: '', fromOwner: 'outside' }).errors.action).toMatch(
      /OVERCOMMITTED/,
    );
  });

  it('refuses cases that are no longer open', () => {
    const r = validateResolve(openCase({ status: 'RESOLVED' }), { action: 'acknowledge', note: 'n', currency: '', amount: '' });
    expect(r.ok).toBe(false);
    expect(r.errors.action).toMatch(/RESOLVED/);
  });

  it('normalizes signed amounts without touching digits', () => {
    expect(normalizeSignedAmount(' +0012.50 ')).toBe('0012.50');
    expect(normalizeSignedAmount('-3')).toBe('-3');
    expect(normalizeSignedAmount('1e3')).toBeNull();
    expect(normalizeSignedAmount('.5')).toBeNull();
  });
});

describe('suggestedCashFlow and counts', () => {
  it('suggests the core-reported unexplained amount for cash mismatches only', () => {
    expect(suggestedCashFlow(openCase())).toEqual({ currency: 'USD', amount: '12.340000' });
    expect(suggestedCashFlow(openCase({ kind: 'POSITION_MISMATCH' }))).toBeNull();
    expect(suggestedCashFlow(openCase({ detail: { currency: 'USD', unexplained: '0.000000' } }))).toBeNull();
  });

  it('counts open, blocking and critical cases', () => {
    expect(
      exceptionCounts([
        openCase(),
        openCase({ case_id: 'b', blocking: false, severity: 'critical' }),
        openCase({ case_id: 'c', status: 'RESOLVED' }),
      ]),
    ).toEqual({ open: 2, blocking: 1, critical: 1 });
  });
});

describe('runSummary and fees', () => {
  it('summarizes differences by blocking and kind', () => {
    expect(
      runSummary({
        differences: [
          { kind: 'CASH_MISMATCH', blocking: true, detail: {} },
          { kind: 'UNSETTLED_DIFFERENCE', blocking: false, detail: {} },
          { kind: 'CASH_MISMATCH', blocking: true, detail: {} },
        ],
      }),
    ).toEqual({ blocking: 2, nonBlocking: 1, kinds: ['CASH_MISMATCH', 'UNSETTLED_DIFFERENCE'] });
  });

  it('selects executions whose fee is still estimated', () => {
    const ex = (k: string, fee_status: string) => ({ exec_key: k, fee_status }) as unknown as Execution;
    expect(estimatedFeeExecutions([ex('a', 'ESTIMATED'), ex('b', 'FINAL'), ex('c', 'ACCEPTED_ESTIMATE'), ex('d', 'EXTERNAL')]).map((e) => e.exec_key)).toEqual(['a']);
  });
});
