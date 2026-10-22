/**
 * Session event timeline: phases and lifecycle milestones (goal observed, wind-down,
 * reconciliation, completion and settlement are separate steps; book Ch. 22).
 */
import type { EventRecord, Session } from '../../api/types';
import { moneyText } from './format';
import { explainFunding } from './model';

export type Phase =
  | 'request'
  | 'funding'
  | 'start'
  | 'running'
  | 'goal'
  | 'wind_down'
  | 'reconciliation'
  | 'completion'
  | 'settlement'
  | 'review'
  | 'trading'
  | 'other';

export const PHASE_LABEL: Record<Phase, string> = {
  request: 'Request',
  funding: 'Funding',
  start: 'Start',
  running: 'Running',
  goal: 'Goal observed',
  wind_down: 'Wind-down',
  reconciliation: 'Reconciliation',
  completion: 'Completion',
  settlement: 'Settlement',
  review: 'Review',
  trading: 'Orders and fills',
  other: 'Other',
};

const PHASE_BY_TYPE: Record<string, Phase> = {
  SessionRequested: 'request',
  SuccessorRequested: 'request',
  SessionMandateAmended: 'request',
  SessionAwaitingFunds: 'funding',
  SessionFunded: 'funding',
  SessionStartDeferred: 'start',
  SessionStarted: 'start',
  SessionSuspended: 'running',
  SessionResumed: 'running',
  SessionValued: 'running',
  SessionResumedBelowTarget: 'running',
  SessionWindDownProgress: 'wind_down',
  SessionReconciling: 'reconciliation',
  SessionReconcilingBlocked: 'reconciliation',
  SessionCompleted: 'completion',
  SessionClosed: 'completion',
  SessionCancelled: 'completion',
  SuccessorNotCreated: 'completion',
  SessionSettled: 'settlement',
  SettlementConfirmed: 'settlement',
  SettlementLate: 'settlement',
  SessionFailedReview: 'review',
  SessionReviewResolved: 'review',
};

export function eventPhase(ev: Pick<EventRecord, 'type' | 'info'>): Phase {
  if (ev.type === 'SessionWindDownStarted') return ev.info.reason === 'TARGET_OBSERVED' ? 'goal' : 'wind_down';
  const p = PHASE_BY_TYPE[ev.type];
  if (p) return p;
  if (/^(Order|Execution|Commission|Fee|Outbox|TerminalOrder|TerminalStatus|Unmapped|External|Orphan)/.test(ev.type)) return 'trading';
  return 'other';
}

export type MilestoneState = 'done' | 'current' | 'pending' | 'skipped';

export interface Milestone {
  id: 'requested' | 'funded' | 'started' | 'goal_observed' | 'wind_down' | 'reconciliation' | 'completion' | 'settlement';
  label: string;
  state: MilestoneState;
  atNs: number | null;
  seq: number | null;
  detail: string;
}

function lastOf(timeline: readonly EventRecord[], pred: (e: EventRecord) => boolean): EventRecord | undefined {
  for (let i = timeline.length - 1; i >= 0; i--) {
    const e = timeline[i]!;
    if (pred(e)) return e;
  }
  return undefined;
}

function optMoney(v: string | null, currency: string): string {
  return v ? moneyText(v, currency) : 'not recorded';
}

function infoText(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

/**
 * The session lifecycle as separate milestones: requested, funded, started, goal observed,
 * wind-down, reconciliation, completion and settlement (book Ch. 22: the timeline separates goal
 * observed, wind-down, reconciliation, completed proceeds and settlement). Row timestamps are the
 * core's own; event seqs link each milestone to the event log.
 */
export function sessionMilestones(session: Session, timeline: readonly EventRecord[]): Milestone[] {
  const st = session.state;
  const requested = timeline.find((e) => e.type === 'SessionRequested' || e.type === 'SuccessorRequested');
  const funded = lastOf(timeline, (e) => e.type === 'SessionFunded');
  const started = lastOf(timeline, (e) => e.type === 'SessionStarted');
  const goal = lastOf(timeline, (e) => e.type === 'SessionWindDownStarted' && e.info.reason === 'TARGET_OBSERVED');
  const wind = lastOf(timeline, (e) => e.type === 'SessionWindDownStarted');
  const recon = lastOf(timeline, (e) => e.type === 'SessionReconciling');
  const reconBlocked = lastOf(timeline, (e) => e.type === 'SessionReconcilingBlocked');
  const completed = lastOf(timeline, (e) => e.type === 'SessionCompleted');
  const closed = lastOf(timeline, (e) => e.type === 'SessionClosed');
  const cancelled = lastOf(timeline, (e) => e.type === 'SessionCancelled');
  const settled = lastOf(timeline, (e) => e.type === 'SessionSettled');
  const cancelledState = st === 'CANCELLED';
  const unstarted = st === 'REQUESTED' || st === 'WAITING_FUNDS' || st === 'READY' || cancelledState;
  const t = (row: number | null | undefined, ev: EventRecord | undefined) => row ?? ev?.recorded_time_ns ?? null;

  const out: Milestone[] = [];
  out.push({
    id: 'requested',
    label: 'Requested',
    state: 'done',
    atNs: t(session.created_at_ns, requested),
    seq: requested?.seq ?? null,
    detail: session.predecessor_id ? `Successor of ${session.predecessor_id}; mandate frozen from the active template at creation.` : 'Owner request recorded.',
  });

  const fundedState: MilestoneState = session.funded_at_ns || funded ? 'done' : cancelledState ? 'skipped' : 'current';
  out.push({
    id: 'funded',
    label: 'Funded',
    state: fundedState,
    atNs: t(session.funded_at_ns, funded),
    seq: funded?.seq ?? null,
    detail:
      fundedState === 'done'
        ? `Seed ${moneyText(session.seed, session.currency)} moved from unallocated cash.`
        : fundedState === 'skipped'
          ? 'Cancelled before funding.'
          : explainFunding(session.funding, st).detail,
  });

  const startedState: MilestoneState = session.started_at_ns || started ? 'done' : cancelledState ? 'skipped' : st === 'READY' ? 'current' : 'pending';
  out.push({
    id: 'started',
    label: 'Started',
    state: startedState,
    atNs: t(session.started_at_ns, started),
    seq: started?.seq ?? null,
    detail:
      startedState === 'done'
        ? `Started by ${infoText(started?.info.by) ?? started?.principal_id ?? 'the core'}.`
        : startedState === 'skipped'
          ? 'Never started.'
          : session.blockers.length
            ? `Start deferred: ${session.blockers.join(', ')}.`
            : 'Not started.',
  });

  const goalTarget = goal ? infoText((goal.info.evidence as Record<string, unknown> | undefined)?.target) : null;
  const goalEquity = goal ? infoText((goal.info.evidence as Record<string, unknown> | undefined)?.equity) : null;
  const ended = wind && wind.info.reason !== 'TARGET_OBSERVED';
  out.push({
    id: 'goal_observed',
    label: 'Goal observed',
    state: goal ? 'done' : unstarted || ended || st === 'CLOSED' ? 'skipped' : 'pending',
    atNs: goal?.recorded_time_ns ?? null,
    seq: goal?.seq ?? null,
    detail: goal
      ? `Marked equity ${goalEquity ? moneyText(goalEquity, session.currency) : 'unknown'} reached the target ${moneyText(goalTarget ?? session.target, session.currency)}. Observation on marks is not completion.`
      : ended
        ? `Not observed: the session ended for ${infoText(wind?.info.reason) ?? 'another reason'}.`
        : unstarted
          ? 'Not applicable before the session runs.'
          : 'Not observed yet: the target is a mandate, not a forecast.',
  });

  const windState: MilestoneState = wind ? (st === 'WINDING_DOWN' ? 'current' : 'done') : unstarted ? 'skipped' : 'pending';
  out.push({
    id: 'wind_down',
    label: 'Wind-down',
    state: windState,
    atNs: wind ? (session.ended_at_ns ?? wind.recorded_time_ns) : null,
    seq: wind?.seq ?? null,
    detail: wind
      ? `Reason ${infoText(wind.info.reason) ?? session.wind_down_reason ?? 'unknown'}.${st === 'WINDING_DOWN' && session.blockers.length ? ` Blockers: ${session.blockers.join(', ')}.` : ''}`
      : unstarted
        ? 'Not applicable.'
        : 'Not started.',
  });

  const reconState: MilestoneState = recon ? (st === 'RECONCILING' ? 'current' : 'done') : unstarted ? 'skipped' : 'pending';
  const reconBlockers = reconBlocked && (!recon || reconBlocked.seq > recon.seq) ? (reconBlocked.info.blockers as unknown) : null;
  out.push({
    id: 'reconciliation',
    label: 'Reconciliation',
    state: reconState,
    atNs: recon?.recorded_time_ns ?? null,
    seq: recon?.seq ?? null,
    detail: recon
      ? st === 'RECONCILING'
        ? Array.isArray(reconBlockers) && reconBlockers.length
          ? `Flat; waiting on ${reconBlockers.join(', ')}.`
          : 'Flat with no open orders; awaiting final fees and a clean reconciliation.'
        : 'Flat and reconciled.'
      : unstarted
        ? 'Not applicable.'
        : 'Not reached.',
  });

  let completion: Milestone;
  if (completed) {
    completion = {
      id: 'completion',
      label: 'Completed',
      state: 'done',
      atNs: session.completed_at_ns ?? completed.recorded_time_ns,
      seq: completed.seq,
      detail: `Target reached. Terminal equity ${optMoney(session.terminal_equity ?? infoText(completed.info.terminal_equity), session.currency)} protected as completed proceeds; no transfer is made.`,
    };
  } else if (closed) {
    completion = {
      id: 'completion',
      label: 'Closed',
      state: 'done',
      // ended_at_ns keeps the wind-down start; the close itself is the SessionClosed event.
      atNs: closed.recorded_time_ns,
      seq: closed.seq,
      detail: `Outcome ${session.outcome ?? infoText(closed.info.outcome) ?? 'not recorded'}; terminal equity ${optMoney(session.terminal_equity ?? infoText(closed.info.terminal_equity), session.currency)} returned to unallocated. No successor.`,
    };
  } else if (cancelled) {
    completion = {
      id: 'completion',
      label: 'Cancelled',
      state: 'done',
      atNs: cancelled.recorded_time_ns,
      seq: cancelled.seq,
      detail: 'Cancelled before start.',
    };
  } else {
    completion = { id: 'completion', label: 'Completion', state: 'pending', atNs: null, seq: null, detail: 'Not completed.' };
  }
  out.push(completion);

  let settlement: Milestone;
  if (st === 'COMPLETED_SETTLED') {
    settlement = {
      id: 'settlement',
      label: 'Settled',
      state: 'done',
      atNs: t(session.settled_at_ns, settled),
      seq: settled?.seq ?? completed?.seq ?? null,
      detail: settled ? 'Every protected receivable confirmed settled.' : 'Nothing was pending settlement at completion.',
    };
  } else if (st === 'COMPLETED_UNSETTLED') {
    settlement = {
      id: 'settlement',
      label: 'Settlement',
      state: 'current',
      atNs: null,
      seq: null,
      detail: 'Protected proceeds include receivables that have not settled yet.',
    };
  } else if (st === 'CLOSED' || st === 'CANCELLED') {
    settlement = {
      id: 'settlement',
      label: 'Settlement',
      state: 'skipped',
      atNs: null,
      seq: null,
      detail: st === 'CLOSED' ? 'Not tracked for the session: pending settlements moved to unallocated with the capital.' : 'Not applicable.',
    };
  } else {
    settlement = { id: 'settlement', label: 'Settlement', state: 'pending', atNs: null, seq: null, detail: 'Not reached.' };
  }
  out.push(settlement);
  return out;
}

/** Merge timeline pages by seq (dedupe, ascending). */
export function mergeTimeline(...pages: readonly (readonly EventRecord[] | undefined)[]): EventRecord[] {
  const bySeq = new Map<number, EventRecord>();
  for (const page of pages) for (const e of page ?? []) bySeq.set(e.seq, e);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/** session_detail returns at most this many timeline events (the first ones, by seq). */
export const DETAIL_TIMELINE_LIMIT = 500;
/** /api/events caps a page at this many events. */
export const EVENTS_PAGE_LIMIT = 2000;

/** One-line summary of an event's info for the timeline table. */
export function eventSummary(ev: Pick<EventRecord, 'type' | 'info'>): string {
  const i = ev.info;
  const parts: string[] = [];
  const pick = (k: string, label = k) => {
    const v = i[k];
    if (v === null || v === undefined || v === '') return;
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') parts.push(`${label} ${String(v)}`);
    else if (Array.isArray(v)) parts.push(`${label} ${v.length ? v.join(', ') : 'none'}`);
  };
  switch (ev.type) {
    case 'SessionRequested':
      pick('required_net_return_pct', 'required net return %');
      break;
    case 'SessionMandateAmended':
      pick('previous', 'previous mandate');
      break;
    case 'SessionAwaitingFunds':
      pick('reason');
      pick('shortfall');
      break;
    case 'SessionFunded': {
      const amt = i.amount as { amount?: string; currency?: string } | undefined;
      if (amt?.amount) parts.push(`amount ${amt.amount} ${amt.currency ?? ''}`.trim());
      pick('source');
      break;
    }
    case 'SessionWindDownStarted':
      pick('reason');
      pick('note');
      break;
    default:
      for (const k of ['reason', 'outcome', 'terminal_equity', 'equity', 'blockers', 'by', 'predecessor', 'case_id', 'note']) pick(k);
  }
  return parts.join(' · ');
}
