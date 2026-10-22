import { describe, expect, it } from 'vitest';
import type { Bucket, EventRecord, Overview, Position, Session } from '../../api/types';
import { bpsToPercent, moneyText, pctText } from './format';
import { checkMandateInput, minimumFeeEnvelope } from './mandateInput';
import {
  blockersFromDetails,
  describeSessionBlocker,
  expectedEntryBlockers,
  expectedStartBlockers,
  explainFunding,
  matchesFilter,
  SESSION_STATES,
  sessionActions,
  sessionPnl,
  sessionRequiredReturn,
  successorIndex,
} from './model';
import { eventPhase, eventSummary, mergeTimeline, sessionMilestones } from './timeline';
import { formatDurationS, readSessionPolicy } from './policy';

// ------------------------------------------------------------------ fixtures (shapes from a live core)

const SID = 'ses-01a16401-0042-7bbc-8dba-3649a06c97f7';

function session(over: Partial<Session> = {}): Session {
  return {
    session_id: SID,
    account_key: 'SIM-1',
    state: 'WAITING_FUNDS',
    mandate_hash: 'dc78',
    predecessor_id: null,
    currency: 'USD',
    seed: '100.00',
    target: '105.00',
    outcome: null,
    created_at_ns: 1792679149634260000,
    funded_at_ns: null,
    started_at_ns: null,
    ended_at_ns: null,
    completed_at_ns: null,
    settled_at_ns: null,
    wind_down_reason: null,
    wind_down_evidence: null,
    wind_down_attempts: {},
    funding: {
      status: 'WAITING',
      reason: 'STOP_LATCHED:NEW_ACCOUNT',
      shortfall: null,
      note: 'funded only from reconciled, settled, unallocated cash; protected and unsettled proceeds are never used',
    },
    high_water: '100.00',
    protected_value: null,
    terminal_equity: null,
    resumes_used: 0,
    review_return_state: null,
    blockers: [],
    suspend_reason: null,
    mandate: null,
    ...over,
  };
}

let seq = 0;
function ev(type: string, info: Record<string, unknown> = {}, over: Partial<EventRecord> = {}): EventRecord {
  seq += 1;
  return {
    seq,
    event_id: `e${seq}`,
    type,
    schema_version: 1,
    command_id: `cmd${seq}`,
    correlation_id: `cmd${seq}`,
    principal_id: 'owner',
    account_key: 'SIM-1',
    session_id: SID,
    order_id: null,
    instrument_id: null,
    event_time_ns: null,
    receipt_time_ns: 1_000_000_000 * seq,
    recorded_time_ns: 1_000_000_000 * seq,
    writer_epoch: 1,
    payload_hash: 'p',
    chain_hash: 'c'.repeat(64),
    info,
    changed_sections: ['sessions'],
    ...over,
  };
}

function bucket(owner: string, over: Partial<Bucket> = {}): Bucket {
  return {
    owner,
    currency: 'USD',
    settled_cash: '0.000000',
    receivable_unsettled: '0.000000',
    payable_unsettled: '0.000000',
    fees_accrued: '0.000000',
    liquid_value: '0.000000',
    available_cash: '0.000000',
    holds: '0.000000',
    inventory_cost: '0.000000',
    system_owned: true,
    ...over,
  };
}

function position(over: Partial<Position> = {}): Position {
  return {
    owner: `session:${SID}`,
    instrument_id: 'FIX-EQ-1',
    symbol: 'FIXA',
    currency: 'USD',
    quantity: '2.000000',
    cost_basis: '40.000000',
    lots: 1,
    mark: '21.5',
    market_value: '43.000000',
    unrealized: '3.000000',
    mark_basis: 'bid from fixture-market age 120 ms FIXTURE',
    mark_fixture: true,
    ...over,
  };
}

// ------------------------------------------------------------------ actions

describe('sessionActions mirrors the core state guards (sessions.py)', () => {
  const kinds = (state: string) => sessionActions(state).map((a) => a.kind);

  it('offers exactly the commands the core accepts in each state', () => {
    expect(kinds('REQUESTED')).toEqual(['amend_session_request', 'cancel_session']);
    expect(kinds('WAITING_FUNDS')).toEqual(['amend_session_request', 'cancel_session']);
    expect(kinds('READY')).toEqual(['start_session', 'cancel_session']);
    expect(kinds('RUNNING')).toEqual(['suspend_session', 'end_session']);
    expect(kinds('SUSPENDED')).toEqual(['resume_session', 'end_session']);
    for (const s of ['WINDING_DOWN', 'RECONCILING', 'COMPLETED_UNSETTLED', 'COMPLETED_SETTLED', 'CLOSED', 'CANCELLED', 'FAILED_REVIEW', 'BOGUS']) {
      expect(kinds(s)).toEqual([]);
    }
  });

  it('asks for confirmation before consequential commands only', () => {
    const confirm = Object.fromEntries(['READY', 'RUNNING', 'SUSPENDED', 'WAITING_FUNDS'].flatMap((s) => sessionActions(s)).map((a) => [a.kind, a.confirm]));
    expect(confirm).toEqual({
      start_session: true,
      cancel_session: true,
      suspend_session: false, // the safe direction, like STOP
      end_session: true,
      resume_session: true,
      amend_session_request: false, // has its own form
    });
  });

  it('describes end_session as wind-down with no liquidation under a stop', () => {
    const end = sessionActions('RUNNING').find((a) => a.kind === 'end_session')!;
    expect(end.tone).toBe('danger');
    expect(end.effect).toMatch(/OWNER_ENDED/);
    expect(end.effect).toMatch(/latched stop nothing is sold/);
  });
});

describe('state filters', () => {
  it('place every state in "all" and finished states outside "open"', () => {
    for (const s of SESSION_STATES) expect(matchesFilter(s, 'all')).toBe(true);
    expect(matchesFilter('CLOSED', 'open')).toBe(false);
    expect(matchesFilter('COMPLETED_UNSETTLED', 'open')).toBe(true);
    expect(matchesFilter('WAITING_FUNDS', 'unfunded')).toBe(true);
    expect(matchesFilter('RUNNING', 'unfunded')).toBe(false);
  });
});

// ------------------------------------------------------------------ funding

describe('explainFunding states exactly why a session lacks funding', () => {
  it('names the latched stop and its reason', () => {
    const f = explainFunding(session().funding, 'WAITING_FUNDS');
    expect(f.status).toBe('WAITING');
    expect(f.code).toBe('STOP_LATCHED:NEW_ACCOUNT');
    expect(f.detail).toContain('latch reason NEW_ACCOUNT');
    expect(f.resolution).toMatch(/START/);
    expect(f.note).toMatch(/protected and unsettled proceeds are never used/);
  });

  it('carries the shortfall for insufficient cash', () => {
    const f = explainFunding(
      { status: 'WAITING', reason: 'INSUFFICIENT_UNALLOCATED_SETTLED_CASH', shortfall: '25.000000', note: 'n' },
      'WAITING_FUNDS',
    );
    expect(f.label).toBe('waiting: insufficient cash');
    expect(f.shortfall).toBe('25.000000');
    expect(f.detail).toMatch(/operational buffer/);
    expect(f.detail).toMatch(/Protected proceeds and unsettled proceeds are never used/);
  });

  it('covers reconciliation and exceptions, and unknown reasons verbatim', () => {
    expect(explainFunding({ status: 'WAITING', reason: 'RECONCILIATION_REQUIRED' }, 'WAITING_FUNDS').title).toMatch(/reconciliation required/);
    expect(explainFunding({ status: 'WAITING', reason: 'BLOCKING_EXCEPTIONS' }, 'WAITING_FUNDS').title).toMatch(/exceptions/);
    expect(explainFunding({ status: 'WAITING', reason: 'SOMETHING_NEW:x' }, 'WAITING_FUNDS').detail).toContain('SOMETHING_NEW:x');
  });

  it('does not claim a cancelled session is still waiting', () => {
    const f = explainFunding(session().funding, 'CANCELLED');
    expect(f.status).toBe('NOT_APPLICABLE');
    expect(f.detail).toMatch(/^No longer waiting \(session is CANCELLED\)/);
  });

  it('reports funded and unevaluated sessions', () => {
    expect(explainFunding({ status: 'FUNDED', amount: '100.00' }, 'READY')).toMatchObject({ status: 'FUNDED', tone: 'ok' });
    expect(explainFunding(null, 'REQUESTED').status).toBe('NOT_EVALUATED');
  });
});

// ------------------------------------------------------------------ blockers

describe('blockers', () => {
  it('describes wind-down blockers per instrument', () => {
    const b = describeSessionBlocker('FIX-EQ-1:STOP_LATCHED (no automatic liquidation under a stop; on_stop_liquidation=NONE)');
    expect(b.instrument).toBe('FIX-EQ-1');
    expect(b.title).toMatch(/stop is latched/);
    expect(describeSessionBlocker('FIX-EQ-2:NO_FRESH_BID')).toMatchObject({ instrument: 'FIX-EQ-2', title: 'No fresh bid' });
  });

  it('describes session-level blockers with their subject', () => {
    expect(describeSessionBlocker('ACTIVE_SESSION_LIMIT:ses-9').title).toBe('Another session is active (ses-9)');
    expect(describeSessionBlocker('STOP_LATCHED:NEW_ACCOUNT').instrument).toBeNull();
    expect(describeSessionBlocker('FEES_NOT_FINAL:2').title).toBe('Fees not final (2)');
    expect(describeSessionBlocker('EXECUTION_OWNERSHIP_NOT_HELD').title).toBe('Execution ownership not held');
    // Falls back to the control panel's descriptions, then to the code itself.
    expect(describeSessionBlocker('VALUATION_STALE').title).toBe('Valuation stale');
    expect(describeSessionBlocker('WHOLLY_NEW_CODE').title).toBe('Wholly new code');
  });

  it('reads blockers from a rejection', () => {
    expect(blockersFromDetails({ blockers: ['STOP_LATCHED:x', 3, 'EXECUTION_OWNERSHIP_NOT_HELD'] })).toEqual(['STOP_LATCHED:x', 'EXECUTION_OWNERSHIP_NOT_HELD']);
    expect(blockersFromDetails(undefined)).toEqual([]);
    expect(blockersFromDetails({ blockers: 'x' })).toEqual([]);
  });

  it('previews start blockers from the overview', () => {
    const o = {
      control: { stop_latched: true, latch_reason: 'NEW_ACCOUNT', resume_requires_owner: false, resume_reason: null, needs_reconciliation: false },
      risk: { breach: null, valuation_ok: true },
      exceptions_open: [{ blocking: true }],
      ownership: { status: 'HELD' },
      sessions: [
        { session_id: SID, state: 'READY' },
        { session_id: 'ses-other', state: 'SUSPENDED' },
      ],
    } as unknown as Overview;
    expect(expectedEntryBlockers(o)).toEqual(['STOP_LATCHED:NEW_ACCOUNT', 'BLOCKING_EXCEPTIONS']);
    expect(expectedStartBlockers(o, SID)).toEqual(['STOP_LATCHED:NEW_ACCOUNT', 'BLOCKING_EXCEPTIONS', 'ACTIVE_SESSION_LIMIT:ses-other']);
    const clear = { ...o, control: { ...o.control!, stop_latched: false }, exceptions_open: [], ownership: null, sessions: [] } as unknown as Overview;
    expect(expectedStartBlockers(clear, SID)).toEqual(['EXECUTION_OWNERSHIP_NOT_HELD']);
    expect(expectedStartBlockers(undefined, SID)).toEqual([]);
  });
});

// ------------------------------------------------------------------ lineage and required return

describe('lineage and required return', () => {
  it('indexes successors from predecessor links', () => {
    const m = successorIndex([
      { session_id: 'a', predecessor_id: null },
      { session_id: 'b', predecessor_id: 'a' },
      { session_id: 'c', predecessor_id: 'b' },
    ]);
    expect(m.get('a')).toBe('b');
    expect(m.get('b')).toBe('c');
    expect(m.get('c')).toBeUndefined();
  });

  it('uses the core rule and the recorded figure when the mandate was not amended', () => {
    const t = [ev('SessionRequested', { required_net_return_pct: '5.0000' })];
    expect(sessionRequiredReturn(session(), t)).toEqual({ pct: '5.0000', recordedPct: '5.0000' });
    const amended = [...t, ev('SessionMandateAmended', { previous: 'x' })];
    expect(sessionRequiredReturn(session({ seed: '1', target: '10000' }), amended)).toEqual({ pct: '999900.0000', recordedPct: null });
    expect(pctText('999900.0000')).toBe('999,900.00%');
    expect(pctText('5.0000')).toBe('5.00%');
    expect(pctText('33.3300')).toBe('33.33%');
  });
});

// ------------------------------------------------------------------ mandate form

describe('checkMandateInput', () => {
  const defaults = { seed: '100.00', target: '105.00' };
  const policy = { currency: 'USD', ledgerScale: 6, authorizedCapitalLimit: '300.00', feeMinimum: '1.00', feeEstimateMarginBps: 2000 };

  it('uses template values for empty fields and sends only real overrides', () => {
    const c = checkMandateInput({ seed: '', target: '' }, defaults, policy);
    expect(c.ok).toBe(true);
    expect(c.overrides).toEqual({});
    expect(c.requiredPct).toBe('5.0000');
    const o = checkMandateInput({ seed: ' 50 ', target: '' }, defaults, policy);
    expect(o.overrides).toEqual({ seed: '50' });
    expect(o.requiredPct).toBe('110.0000');
  });

  it('shows the $1 -> $10,000 request with its required return and fee infeasibility', () => {
    const c = checkMandateInput({ seed: '1', target: '10000' }, defaults, policy);
    expect(c.ok).toBe(true); // the core accepts it; it is infeasible, not invalid
    expect(c.requiredPct).toBe('999900.0000');
    const bad = c.notes.find((n) => n.tone === 'bad');
    expect(bad?.text).toMatch(/^Infeasible: every buy reserves a fee envelope of at least 1\.20 USD/);
  });

  it('reports the fee envelope share of a feasible seed', () => {
    const c = checkMandateInput({ seed: '', target: '' }, defaults, policy);
    expect(c.notes.find((n) => n.text.startsWith('Each buy'))?.text).toContain('1.20% of this seed');
  });

  it('rejects malformed, non-positive, over-precise and inverted amounts', () => {
    const errs = (seed: string, target: string) => checkMandateInput({ seed, target }, defaults, policy).errors.map((e) => `${e.field}: ${e.message}`);
    expect(errs('1,000', '')[0]).toMatch(/^seed: Seed must be a plain amount/);
    expect(errs('-5', '')[0]).toMatch(/plain amount/);
    expect(errs('0', '')).toContain('seed: Seed must be greater than zero.');
    expect(errs('1.0000001', '')[0]).toMatch(/more than 6 decimal places/);
    expect(errs('', '100.00')[0]).toMatch(/^target: Target terminal equity must exceed the seed/);
    expect(errs('300.01', '400')[0]).toMatch(/authorized capital limit of 300\.00 USD/);
    expect(errs('300', '400')).toEqual([]);
  });

  it('previews funding from unallocated cash less the buffer and the control state', () => {
    const ok = checkMandateInput({ seed: '', target: '' }, defaults, policy, { unallocatedAvailable: '300.000000', operationalBuffer: '5.00' });
    expect(ok.notes.find((n) => n.tone === 'ok')?.text).toMatch(/covers this seed now/);
    const short = checkMandateInput({ seed: '296', target: '297' }, defaults, policy, { unallocatedAvailable: '300.000000', operationalBuffer: '5.00' });
    expect(short.notes.find((n) => n.text.startsWith('Unallocated'))?.text).toMatch(/shortfall of 1\.00 USD/);
    const stop = checkMandateInput({ seed: '', target: '' }, defaults, policy, { stopLatched: true });
    expect(stop.notes[0]?.text ?? '').toBe('Each buy reserves at least 1.20 USD for fees (policy minimum 1.00 USD plus 20% margin): 1.20% of this seed.');
    expect(stop.notes.some((n) => /stop is latched/.test(n.text))).toBe(true);
  });

  it('computes the reservation envelope like risk/fees.py', () => {
    expect(minimumFeeEnvelope('1.00', 2000, 6)).toBe('1.200000');
    expect(minimumFeeEnvelope('0.35', 333, 6)).toBe('0.361655'); // 0.35 * 0.0333 = 0.011655
    expect(minimumFeeEnvelope('0.000001', 1, 6)).toBe('0.000002'); // margin rounds up
    expect(minimumFeeEnvelope('x', 1, 6)).toBeNull();
    expect(bpsToPercent(2000)).toBe('20%');
    expect(bpsToPercent(25)).toBe('0.25%');
    expect(bpsToPercent(0)).toBe('0%');
    expect(moneyText('1234567.5', 'USD')).toBe('1,234,567.50 USD');
  });
});

// ------------------------------------------------------------------ P&L

describe('sessionPnl', () => {
  it('derives marked equity, realized and unrealized exactly for a live session', () => {
    const s = session({ state: 'RUNNING' });
    // Funded 100.00; bought 2 @ 20.00 (+ fee 1.00 accrued) -> cash 60, fees -1, inventory 40.
    const b = bucket(`session:${SID}`, { settled_cash: '60.000000', fees_accrued: '-1.000000', liquid_value: '59.000000', inventory_cost: '40.000000' });
    const p = sessionPnl(s, b, [position(), position({ owner: 'unallocated' })]);
    expect(p).toMatchObject({
      basis: 'live',
      liquid: '59.000000',
      marketValue: '43.000000',
      markedEquity: '102.000000',
      realizedNet: '-1.000000',
      unrealized: '3.000000',
      changeVsSeed: '2.000000',
      toTarget: '3.000000',
      fixtureMarks: true,
      unmarked: [],
    });
  });

  it('refuses to show equity when a holding has no mark', () => {
    const p = sessionPnl(session({ state: 'RUNNING' }), bucket(`session:${SID}`, { liquid_value: '59', inventory_cost: '40' }), [
      position({ instrument_id: 'FIX-EQ-2', market_value: null, unrealized: null, mark: null, mark_fixture: false }),
    ]);
    expect(p.markedEquity).toBeNull();
    expect(p.changeVsSeed).toBeNull();
    expect(p.unmarked).toEqual(['FIX-EQ-2']);
    expect(p.realizedNet).toBe('-1.00');
  });

  it('uses the recorded terminal equity once finished, and nothing before funding', () => {
    expect(sessionPnl(session({ state: 'CLOSED', terminal_equity: '97.250000' }), undefined, []).changeVsSeed).toBe('-2.750000');
    expect(sessionPnl(session({ state: 'COMPLETED_SETTLED', terminal_equity: '105.10' }), undefined, []).changeVsSeed).toBe('5.10');
    expect(sessionPnl(session({ state: 'WAITING_FUNDS' }), undefined, []).basis).toBe('unfunded');
    expect(sessionPnl(session({ state: 'RUNNING' }), undefined, []).basis).toBe('unavailable');
  });

  it('never treats unloaded positions as no holdings', () => {
    const b = bucket(`session:${SID}`, { liquid_value: '16.650000', inventory_cost: '82.350000' });
    expect(sessionPnl(session({ state: 'RUNNING' }), b, null)).toMatchObject({ basis: 'unavailable', markedEquity: null });
  });

  it('refuses to combine balances and holdings read on different sides of a fill', () => {
    // Bucket already shows the buy (inventory 82.35) but the positions read predates it.
    const b = bucket(`session:${SID}`, { liquid_value: '16.650000', inventory_cost: '82.350000' });
    const p = sessionPnl(session({ state: 'RUNNING' }), b, []);
    expect(p.basis).toBe('inconsistent');
    expect(p.markedEquity).toBeNull();
    expect(p.changeVsSeed).toBeNull();
    // Same holdings at a different scale are consistent.
    const ok = sessionPnl(session({ state: 'RUNNING' }), b, [position({ cost_basis: '82.35', market_value: '81.54', unrealized: '-0.81' })]);
    expect(ok).toMatchObject({ basis: 'live', markedEquity: '98.190000', changeVsSeed: '-1.810000', realizedNet: '-1.000000', unrealized: '-0.81' });
  });
});

// ------------------------------------------------------------------ timeline

describe('timeline phases and milestones', () => {
  it('separates goal observed from other wind-downs', () => {
    expect(eventPhase(ev('SessionWindDownStarted', { reason: 'TARGET_OBSERVED' }))).toBe('goal');
    expect(eventPhase(ev('SessionWindDownStarted', { reason: 'OWNER_ENDED' }))).toBe('wind_down');
    expect(eventPhase(ev('SessionReconcilingBlocked'))).toBe('reconciliation');
    expect(eventPhase(ev('SessionSettled'))).toBe('settlement');
    expect(eventPhase(ev('OrderAccepted'))).toBe('trading');
    expect(eventPhase(ev('ExecutionRecorded'))).toBe('trading');
    expect(eventPhase(ev('SuccessorRequested'))).toBe('request');
    expect(eventPhase(ev('Mystery'))).toBe('other');
  });

  it('builds the full successful lifecycle with distinct timestamps', () => {
    const t = [
      ev('SessionRequested', { required_net_return_pct: '5.0000' }),
      ev('SessionFunded', { amount: { amount: '100.00', currency: 'USD' }, source: 'unallocated' }),
      ev('SessionStarted', { by: 'owner' }),
      ev('OrderAccepted', {}, { order_id: 'o1' }),
      ev('SessionWindDownStarted', { reason: 'TARGET_OBSERVED', evidence: { equity: '105.20', target: '105.00' } }),
      ev('SessionReconciling', {}),
      ev('SessionReconcilingBlocked', { blockers: ['FEES_NOT_FINAL:1'] }),
      ev('SessionCompleted', { terminal_equity: '105.10' }),
      ev('SessionSettled', {}),
    ];
    const s = session({
      state: 'COMPLETED_SETTLED',
      funded_at_ns: t[1]!.recorded_time_ns,
      started_at_ns: t[2]!.recorded_time_ns,
      ended_at_ns: t[4]!.recorded_time_ns,
      completed_at_ns: t[7]!.recorded_time_ns,
      settled_at_ns: t[8]!.recorded_time_ns,
      outcome: 'TARGET_REACHED',
      terminal_equity: '105.10',
      protected_value: '105.10',
      wind_down_reason: 'TARGET_OBSERVED',
    });
    const ms = sessionMilestones(s, t);
    expect(ms.map((m) => [m.id, m.state])).toEqual([
      ['requested', 'done'],
      ['funded', 'done'],
      ['started', 'done'],
      ['goal_observed', 'done'],
      ['wind_down', 'done'],
      ['reconciliation', 'done'],
      ['completion', 'done'],
      ['settlement', 'done'],
    ]);
    const at = Object.fromEntries(ms.map((m) => [m.id, m.atNs]));
    expect(at.goal_observed).toBe(t[4]!.recorded_time_ns);
    expect(at.reconciliation).toBe(t[5]!.recorded_time_ns);
    expect(at.completion).toBe(t[7]!.recorded_time_ns);
    expect(at.settlement).toBe(t[8]!.recorded_time_ns);
    expect(ms.find((m) => m.id === 'goal_observed')!.detail).toBe(
      'Marked equity 105.20 USD reached the target 105.00 USD. Observation on marks is not completion.',
    );
    expect(ms.find((m) => m.id === 'completion')!.detail).toMatch(/105\.10 USD protected as completed proceeds/);
  });

  it('marks goal observed as skipped when the owner ended the session', () => {
    const t = [ev('SessionRequested'), ev('SessionFunded'), ev('SessionStarted'), ev('SessionWindDownStarted', { reason: 'OWNER_ENDED' }), ev('SessionReconciling'), ev('SessionClosed', { outcome: 'OWNER_ENDED', terminal_equity: '99.00' })];
    const s = session({ state: 'CLOSED', outcome: 'OWNER_ENDED', terminal_equity: '99.000000', funded_at_ns: 1, started_at_ns: 2, ended_at_ns: 3 });
    const ms = Object.fromEntries(sessionMilestones(s, t).map((m) => [m.id, m]));
    expect(ms.goal_observed!.state).toBe('skipped');
    expect(ms.goal_observed!.detail).toMatch(/ended for OWNER_ENDED/);
    expect(ms.completion!.label).toBe('Closed');
    expect(ms.completion!.atNs).toBe(t[5]!.recorded_time_ns); // the close, not ended_at_ns (wind-down start)
    expect(ms.wind_down!.atNs).toBe(3);
    expect(ms.completion!.detail).toMatch(/Outcome OWNER_ENDED; terminal equity 99.00 USD returned to unallocated/);
    expect(ms.settlement!.state).toBe('skipped');
  });

  it('keeps a waiting session at funding with the exact reason, and a completed-unsettled one at settlement', () => {
    const waiting = Object.fromEntries(sessionMilestones(session(), [ev('SessionRequested'), ev('SessionAwaitingFunds')]).map((m) => [m.id, m]));
    expect(waiting.funded!.state).toBe('current');
    expect(waiting.funded!.detail).toMatch(/latch reason NEW_ACCOUNT/);
    expect(waiting.goal_observed!.state).toBe('skipped');
    const unsettled = sessionMilestones(session({ state: 'COMPLETED_UNSETTLED', funded_at_ns: 1, started_at_ns: 2 }), [ev('SessionCompleted')]);
    expect(unsettled.find((m) => m.id === 'settlement')!.state).toBe('current');
  });

  it('shows reconciling blockers that arrived after the reconciling event', () => {
    const t = [ev('SessionRequested'), ev('SessionWindDownStarted', { reason: 'EXPIRED' }), ev('SessionReconciling'), ev('SessionReconcilingBlocked', { blockers: ['CLEAN_RECONCILIATION_AFTER_LAST_EXECUTION_REQUIRED'] })];
    const ms = sessionMilestones(session({ state: 'RECONCILING', funded_at_ns: 1, started_at_ns: 2 }), t);
    expect(ms.find((m) => m.id === 'reconciliation')).toMatchObject({ state: 'current', detail: 'Flat; waiting on CLEAN_RECONCILIATION_AFTER_LAST_EXECUTION_REQUIRED.' });
  });

  it('merges timeline pages by seq without duplicates', () => {
    const a = ev('A');
    const b = ev('B');
    const c = ev('C');
    expect(mergeTimeline([a, b], [b, c], undefined).map((e) => e.type)).toEqual(['A', 'B', 'C']);
  });

  it('summarizes event info', () => {
    expect(eventSummary(ev('SessionRequested', { required_net_return_pct: '5.0000' }))).toBe('required net return % 5.0000');
    expect(eventSummary(ev('SessionAwaitingFunds', { reason: 'STOP_LATCHED:X', shortfall: null }))).toBe('reason STOP_LATCHED:X');
    expect(eventSummary(ev('SessionFunded', { amount: { amount: '100.00', currency: 'USD' }, source: 'unallocated' }))).toBe('amount 100.00 USD · source unallocated');
    expect(eventSummary(ev('SessionReconcilingBlocked', { blockers: ['A', 'B'] }))).toBe('blockers A, B');
  });
});

// ------------------------------------------------------------------ policy

describe('readSessionPolicy', () => {
  it('reads the simulated default policy fields with type checks', () => {
    const p = readSessionPolicy({
      policy_hash: '554d',
      name: 'simulated-default-v1',
      body: {
        schema_version: 1,
        name: 'simulated-default-v1',
        mode: 'SIMULATED',
        currency: 'USD',
        eligibility: {},
        account_limits: { authorized_capital: '300.00', operational_buffer: '5.00', max_order_notional: '100.00' },
        session_template: {
          seed: '100.00',
          target_terminal_equity: '105.00',
          strategy_eligibility: ['owner-manual'],
          session_drawdown_trigger_bps: 200,
          session_max_loss: '5.00',
          auto_start: true,
          expires_after_s: null,
          below_target_after_target_observed: 'CLOSE',
          max_resumes: 0,
        },
        successor: { create_on_success: true, mandate_source: 'ACTIVE_TEMPLATE_AT_CREATION', reinvest_protected_proceeds: false },
        wind_down: { order_style: 'LIMIT_AT_BID_MINUS_COLLAR', collar_bps: 50, reprice_after_s: 300, max_attempts: 5 },
        on_stop_liquidation: 'NONE',
        auto_resume_after_clean_reconciliation: false,
        fees: { model: 'PER_SHARE', per_share: '0.005', minimum: '1.00', maximum_bps_of_notional: 100, estimate_margin_bps: 2000 },
        settlement_calendar: 'WEEKDAYS',
      },
    });
    expect(p.template).toMatchObject({ seed: '100.00', target: '105.00', drawdownTriggerBps: 200, autoStart: true, expiresAfterS: null, maxResumes: 0 });
    expect(p.limits).toEqual({ authorizedCapital: '300.00', operationalBuffer: '5.00', maxOrderNotional: '100.00' });
    expect(p.fees).toMatchObject({ minimum: '1.00', estimateMarginBps: 2000 });
    expect(p.successor).toEqual({ createOnSuccess: true, mandateSource: 'ACTIVE_TEMPLATE_AT_CREATION', reinvestProtected: false });
    expect(p.windDown.collarBps).toBe(50);
    expect(p.onStopLiquidation).toBe('NONE');
  });

  it('leaves mistyped fields undefined instead of guessing', () => {
    const p = readSessionPolicy({ policy_hash: 'h', name: 'n', body: { session_template: { seed: 100, session_drawdown_trigger_bps: '200', expires_after_s: 1.5 } } as never });
    expect(p.template.seed).toBeUndefined();
    expect(p.template.drawdownTriggerBps).toBeUndefined();
    expect(p.template.expiresAfterS).toBeUndefined();
    expect(p.currency).toBe('');
  });

  it('formats policy durations', () => {
    expect(formatDurationS(90)).toBe('90 s');
    expect(formatDurationS(300)).toBe('5 m');
    expect(formatDurationS(5400)).toBe('90 m');
    expect(formatDurationS(86_400 * 3 + 3600)).toBe('3 d 1 h');
  });
});
