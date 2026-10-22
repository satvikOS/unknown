import { describe, expect, it } from 'vitest';
import type { ControlState, PreflightResponse, StopProgress } from '../api/types';
import { describeBlocker, entryState, rejectionBlockers, residualCount, startAvailability, stopSteps } from './model';

const control = (over: Partial<ControlState> = {}): ControlState => ({
  account_key: 'SIM-1',
  stop_latched: false,
  latch_reason: null,
  latched_by: null,
  latched_at_ns: null,
  latch_command_id: null,
  resume_requires_owner: false,
  resume_reason: null,
  needs_reconciliation: false,
  reconciled_epoch: 1,
  last_clean_reconciliation_ns: null,
  broker_connected: true,
  broker_authenticated: true,
  last_start: null,
  imported: true,
  ...over,
});

const progress = (over: Partial<StopProgress> = {}): StopProgress => ({
  entries_blocked: true,
  open_buys: 0,
  cancels_pending: 0,
  unknown_orders: 0,
  open_sells: 0,
  residual_positions: {},
  reconciled_flat: true,
  ...over,
});

const preflight = (blockers: string[]): PreflightResponse => ({ blockers, evidence: {}, stop_progress: progress() });

describe('entryState', () => {
  it('stop latched is STOPPED and start applies', () => {
    expect(entryState(control({ stop_latched: true, latch_reason: 'NEW_ACCOUNT' }))).toMatchObject({
      label: 'STOPPED',
      tone: 'bad',
      startApplies: true,
    });
  });
  it('a resume hold without a stop is HELD and start applies', () => {
    expect(entryState(control({ resume_requires_owner: true, resume_reason: 'PROCESS_RESTART' }))).toMatchObject({
      label: 'HELD',
      startApplies: true,
    });
  });
  it('blocking exceptions and loss breaches block without offering start', () => {
    const ex = { exceptions_open: [{ blocking: true } as never], risk: null };
    expect(entryState(control(), ex)).toMatchObject({ label: 'BLOCKED', startApplies: false });
    expect(entryState(control(), { exceptions_open: [], risk: { breach: 'DAILY_LOSS' } as never })).toMatchObject({ label: 'BLOCKED' });
    expect(entryState(control(), { exceptions_open: [], risk: null })).toMatchObject({ label: 'ENTRIES PERMITTED', tone: 'ok' });
    expect(entryState(null).label).toBe('CONTROL UNKNOWN');
  });
});

describe('stopSteps', () => {
  it('never reports flat unless the server says reconciled_flat', () => {
    const steps = stopSteps(progress({ reconciled_flat: false }));
    expect(steps.find((s) => s.id === 'reconciled_flat')).toMatchObject({ state: 'pending', value: 'not confirmed' });
  });

  it('shows outstanding cancels, unknown orders and residual positions', () => {
    const p = progress({
      open_buys: 2,
      cancels_pending: 1,
      unknown_orders: 1,
      residual_positions: { 'session:s1': { 'FIX-EQ-1': '10', 'FIX-EQ-2': '3' }, protected: { 'FIX-EQ-1': '1' } },
      reconciled_flat: false,
    });
    expect(residualCount(p)).toBe(3);
    const byId = Object.fromEntries(stopSteps(p).map((s) => [s.id, s]));
    expect(byId.cancels).toMatchObject({ state: 'pending', value: '2 open buys, 1 cancel pending' });
    expect(byId.unknown_orders?.state).toBe('attention');
    expect(byId.residual_positions).toMatchObject({ state: 'attention', value: '3 open (not sold automatically)' });
  });

  it('a clean stop is all done', () => {
    expect(stopSteps(progress()).every((s) => s.state === 'done')).toBe(true);
  });
});

describe('describeBlocker', () => {
  it('splits code and subject and explains known causes', () => {
    expect(describeBlocker('MARKET_DATA_STALE:FIX-EQ-1')).toMatchObject({ subject: 'FIX-EQ-1', title: 'Market data stale', stepUp: false });
    expect(describeBlocker('OWNER_REAUTHENTICATION_REQUIRED').stepUp).toBe(true);
    expect(describeBlocker('LIVE_NOT_AUTHORIZED: deployment record gates unresolved (GATE-01)').title).toBe('Live trading not authorized');
    expect(describeBlocker('SOMETHING_NEW').title).toBe('Something new');
  });
});

describe('startAvailability', () => {
  it('is enabled only when start applies and every blocker is resolvable by step-up', () => {
    const latched = control({ stop_latched: true });
    expect(startAvailability(latched, preflight([]), 'SIM-1')).toMatchObject({ enabled: true, needsStepUp: false });
    expect(startAvailability(latched, preflight(['OWNER_REAUTHENTICATION_REQUIRED']), 'SIM-1')).toMatchObject({
      enabled: true,
      needsStepUp: true,
    });
    expect(startAvailability(latched, preflight(['OWNER_REAUTHENTICATION_REQUIRED', 'RECONCILIATION_REQUIRED']), 'SIM-1')).toMatchObject({
      enabled: false,
      needsStepUp: true,
    });
  });
  it('is disabled with a reason when nothing is latched or data is missing', () => {
    expect(startAvailability(control(), preflight([]), 'SIM-1').enabled).toBe(false);
    expect(startAvailability(control({ stop_latched: true }), null, 'SIM-1').reason).toBe('Preflight not loaded.');
    expect(startAvailability(control({ stop_latched: true }), preflight([]), null).enabled).toBe(false);
  });
});

it('reads blockers from a PREFLIGHT_FAILED rejection', () => {
  expect(rejectionBlockers({ blockers: ['A', 3, 'B'] })).toEqual(['A', 'B']);
  expect(rejectionBlockers(undefined)).toEqual([]);
});
