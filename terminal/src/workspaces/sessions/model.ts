/**
 * Sessions workspace logic (pure; unit tested). Mirrors the core's session state machine in
 * backend/src/unknown/core/handlers/sessions.py (ADR-0008, SES-01..SES-12). The core remains the
 * authority: these functions only decide what to show and which commands to offer; every command is
 * re-checked by the core, which may still reject it.
 */
import type { Bucket, EventRecord, Overview, Position, Session, SessionFunding, SessionState } from '../../api/types';
import { describeBlocker } from '../../controls/model';
import type { MoneyKind, Tone } from '../../ui/primitives';
import { cmpFixed, fixedToString, parseFixed, requiredReturnPct, subFixed, sumDecimals } from './decimalMath';

// ------------------------------------------------------------------ states

export const SESSION_STATES: readonly SessionState[] = [
  'REQUESTED',
  'WAITING_FUNDS',
  'READY',
  'RUNNING',
  'SUSPENDED',
  'WINDING_DOWN',
  'RECONCILING',
  'COMPLETED_UNSETTLED',
  'COMPLETED_SETTLED',
  'CLOSED',
  'CANCELLED',
  'FAILED_REVIEW',
];

/** One-line meaning of each state, shown beside the state pill. */
export const STATE_MEANING: Record<SessionState, string> = {
  REQUESTED: 'Request recorded; funding not yet evaluated.',
  WAITING_FUNDS: 'Not funded: waiting for eligible cash (see the funding reason).',
  READY: 'Funded from unallocated cash; not started.',
  RUNNING: 'Running: new entries permitted within the mandate.',
  SUSPENDED: 'Suspended: no new entries; open buy orders were cancelled.',
  WINDING_DOWN: 'Winding down: buys cancelled, holdings sold with limit orders.',
  RECONCILING: 'Flat; awaiting final fees and a clean reconciliation.',
  COMPLETED_UNSETTLED: 'Target reached; proceeds protected; settlement pending.',
  COMPLETED_SETTLED: 'Target reached; protected proceeds settled.',
  CLOSED: 'Ended without reaching the target; capital returned to unallocated.',
  CANCELLED: 'Cancelled before it started.',
  FAILED_REVIEW: 'Frozen for owner review of an accounting anomaly.',
};

export function stateMeaning(state: string): string {
  return (STATE_MEANING as Record<string, string>)[state] ?? 'State reported by the core.';
}

export type StateFilter = 'open' | 'unfunded' | 'active' | 'closing' | 'finished' | 'all';

export const STATE_FILTERS: readonly { id: StateFilter; label: string; states: readonly SessionState[] | null }[] = [
  {
    id: 'open',
    label: 'Open',
    states: ['REQUESTED', 'WAITING_FUNDS', 'READY', 'RUNNING', 'SUSPENDED', 'WINDING_DOWN', 'RECONCILING', 'FAILED_REVIEW', 'COMPLETED_UNSETTLED'],
  },
  { id: 'unfunded', label: 'Unfunded', states: ['REQUESTED', 'WAITING_FUNDS'] },
  { id: 'active', label: 'Ready or active', states: ['READY', 'RUNNING', 'SUSPENDED', 'FAILED_REVIEW'] },
  { id: 'closing', label: 'Closing', states: ['WINDING_DOWN', 'RECONCILING', 'COMPLETED_UNSETTLED'] },
  { id: 'finished', label: 'Finished', states: ['COMPLETED_SETTLED', 'CLOSED', 'CANCELLED'] },
  { id: 'all', label: 'All', states: null },
];

export function matchesFilter(state: string, filter: StateFilter): boolean {
  const f = STATE_FILTERS.find((x) => x.id === filter);
  return !f || f.states === null || (f.states as readonly string[]).includes(state);
}

/** States in which the session's own bucket holds its capital (funded and not yet released). */
const LIVE_CAPITAL_STATES: ReadonlySet<string> = new Set(['READY', 'RUNNING', 'SUSPENDED', 'WINDING_DOWN', 'RECONCILING', 'FAILED_REVIEW']);
const TERMINAL_RESULT_STATES: ReadonlySet<string> = new Set(['COMPLETED_UNSETTLED', 'COMPLETED_SETTLED', 'CLOSED']);

// ------------------------------------------------------------------ actions

export type SessionCommandKind =
  | 'amend_session_request'
  | 'cancel_session'
  | 'start_session'
  | 'suspend_session'
  | 'resume_session'
  | 'end_session';

export interface SessionAction {
  kind: SessionCommandKind;
  label: string;
  /** What the command does, shown in the confirmation and as the button description. */
  effect: string;
  /** Destructive or exposure-changing: ask for confirmation first. */
  confirm: boolean;
  tone: 'default' | 'danger' | 'primary';
}

const ACTIONS: Record<SessionCommandKind, Omit<SessionAction, 'kind'> & { states: readonly SessionState[] }> = {
  start_session: {
    label: 'Start session',
    effect:
      'Allows new entries for this session within its mandate. The core refuses if entries are blocked (stop, reconciliation, exceptions, loss budget), execution ownership is not held, or another session is active.',
    confirm: true,
    tone: 'primary',
    states: ['READY'],
  },
  amend_session_request: {
    label: 'Amend request',
    effect: 'Replaces the seed and target of this unfunded request with a new mandate (new mandate hash), then re-evaluates funding.',
    confirm: false,
    tone: 'default',
    states: ['REQUESTED', 'WAITING_FUNDS'],
  },
  suspend_session: {
    label: 'Suspend',
    effect: 'Blocks new entries for this session and cancels its open buy orders. Holdings are kept; nothing is sold.',
    confirm: false,
    tone: 'default',
    states: ['RUNNING'],
  },
  resume_session: {
    label: 'Resume',
    effect: 'Allows new entries again. The core refuses while account entries are blocked.',
    confirm: true,
    tone: 'primary',
    states: ['SUSPENDED'],
  },
  end_session: {
    label: 'End session',
    effect:
      'Starts wind-down: cancels open buys and sells every holding with limit orders at the bid less the policy collar, then reconciles. The outcome is OWNER_ENDED, remaining capital returns to unallocated, and no successor is created. Under a latched stop nothing is sold automatically.',
    confirm: true,
    tone: 'danger',
    states: ['RUNNING', 'SUSPENDED'],
  },
  cancel_session: {
    label: 'Cancel session',
    effect: 'Cancels a session that has not started. A funded (READY) session returns its seed to unallocated cash.',
    confirm: true,
    tone: 'danger',
    states: ['REQUESTED', 'WAITING_FUNDS', 'READY'],
  },
};

const ACTION_ORDER: readonly SessionCommandKind[] = [
  'start_session',
  'resume_session',
  'amend_session_request',
  'suspend_session',
  'end_session',
  'cancel_session',
];

/** Commands the core accepts in this state (sessions.py state guards), in display order. */
export function sessionActions(state: string): SessionAction[] {
  return ACTION_ORDER.filter((k) => (ACTIONS[k].states as readonly string[]).includes(state)).map((kind) => {
    const { states: _states, ...rest } = ACTIONS[kind];
    return { kind, ...rest };
  });
}

export function actionInfo(kind: SessionCommandKind): SessionAction {
  const { states: _states, ...rest } = ACTIONS[kind];
  return { kind, ...rest };
}

// ------------------------------------------------------------------ funding

export interface FundingExplanation {
  status: 'FUNDED' | 'WAITING' | 'NOT_EVALUATED' | 'NOT_APPLICABLE' | 'OTHER';
  tone: Tone;
  /** Short status label for tables. */
  label: string;
  /** Raw reason code from the core, if any. */
  code: string | null;
  title: string;
  /** Exact cause, including the core's own subject (e.g. the stop's latch reason). */
  detail: string;
  /** What resolves it. */
  resolution: string | null;
  shortfall: string | null;
  /** The core's note, verbatim. */
  note: string | null;
}

/**
 * Why a session is or is not funded (core/handlers/sessions.py try_fund). A waiting session is
 * funded only from reconciled, settled, unallocated cash; protected and unsettled proceeds are
 * never used, so a successor never inherits its predecessor's proceeds.
 */
export function explainFunding(funding: SessionFunding | null | undefined, state: string): FundingExplanation {
  const note = funding?.note ?? null;
  if (!funding) {
    return {
      status: 'NOT_EVALUATED',
      tone: 'neutral',
      label: 'not evaluated',
      code: null,
      title: 'Funding not evaluated yet',
      detail: 'The core evaluates funding when the request is recorded and on every session tick.',
      resolution: null,
      shortfall: null,
      note,
    };
  }
  if (funding.status === 'FUNDED') {
    return {
      status: 'FUNDED',
      tone: 'ok',
      label: 'funded',
      code: null,
      title: 'Funded',
      detail: 'The seed was transferred from unallocated settled cash into this session’s bucket.',
      resolution: null,
      shortfall: null,
      note,
    };
  }
  if (funding.status !== 'WAITING') {
    return {
      status: 'OTHER',
      tone: 'neutral',
      label: funding.status.toLowerCase(),
      code: funding.reason ?? null,
      title: `Funding status ${funding.status}`,
      detail: funding.reason ?? 'No reason recorded.',
      resolution: null,
      shortfall: funding.shortfall ?? null,
      note,
    };
  }
  const code = funding.reason ?? '';
  const i = code.indexOf(':');
  const base = i >= 0 ? code.slice(0, i) : code;
  const subject = i >= 0 ? code.slice(i + 1) : null;
  const waitingStill = state === 'REQUESTED' || state === 'WAITING_FUNDS';
  const common = { status: waitingStill ? ('WAITING' as const) : ('NOT_APPLICABLE' as const), code: code || null, shortfall: funding.shortfall ?? null, note };
  const prefix = waitingStill ? '' : `No longer waiting (session is ${state}). Last reason: `;
  switch (base) {
    case 'STOP_LATCHED':
      return {
        ...common,
        tone: waitingStill ? 'warn' : 'neutral',
        label: 'waiting: stop latched',
        title: 'Waiting: a stop is latched',
        detail: `${prefix}No session is funded while the account stop is latched${subject ? ` (latch reason ${subject})` : ''}.`,
        resolution: 'Review the stop, then request START from the control panel (passphrase required). Funding is retried on the next session tick.',
      };
    case 'RECONCILIATION_REQUIRED':
      return {
        ...common,
        tone: waitingStill ? 'warn' : 'neutral',
        label: 'waiting: reconciliation',
        title: 'Waiting: reconciliation required',
        detail: `${prefix}There is no clean reconciliation in the current process epoch, so unallocated cash is not yet confirmed.`,
        resolution: 'Wait for a clean reconciliation and resolve any differences it reports.',
      };
    case 'BLOCKING_EXCEPTIONS':
      return {
        ...common,
        tone: waitingStill ? 'warn' : 'neutral',
        label: 'waiting: exceptions',
        title: 'Waiting: blocking exceptions open',
        detail: `${prefix}Blocking exception cases are open for this account; no session is funded until they are resolved.`,
        resolution: 'Investigate and resolve each blocking exception case.',
      };
    case 'INSUFFICIENT_UNALLOCATED_SETTLED_CASH':
      return {
        ...common,
        tone: waitingStill ? 'warn' : 'neutral',
        label: 'waiting: insufficient cash',
        title: 'Waiting: not enough unallocated settled cash',
        detail: `${prefix}Unallocated settled cash, less the policy operational buffer, is below the seed${funding.shortfall ? '; the shortfall is shown' : ''}. Protected proceeds and unsettled proceeds are never used.`,
        resolution: 'Authorize more capital, wait for other sessions to close and return capital, or wait for pending settlements to settle.',
      };
    default:
      return {
        ...common,
        tone: waitingStill ? 'warn' : 'neutral',
        label: `waiting: ${code || 'no reason'}`,
        title: 'Waiting for funds',
        detail: `${prefix}${code || 'The core did not record a reason.'}`,
        resolution: null,
      };
  }
}

// ------------------------------------------------------------------ blockers

export interface SessionBlockerInfo {
  code: string;
  title: string;
  resolution: string;
  /** Instrument the blocker applies to (wind-down blockers), if any. */
  instrument: string | null;
}

const WIND_DOWN_BLOCKERS: Record<string, { title: string; resolution: string }> = {
  STOP_LATCHED: {
    title: 'Not sold: a stop is latched',
    resolution: 'Stop liquidation policy is NONE: holdings are never sold automatically under a stop. Clear the stop with START to let wind-down proceed.',
  },
  MAX_ATTEMPTS: {
    title: 'Wind-down attempts exhausted',
    resolution: 'The policy limit on wind-down sell attempts was reached for this instrument; owner review is needed.',
  },
  NO_FRESH_BID: {
    title: 'No fresh bid',
    resolution: 'A fresh bid quote is needed to price the wind-down sell; restore market data for this instrument.',
  },
  BELOW_MINIMUM: {
    title: 'Below minimum size or price',
    resolution: 'The remaining quantity or collared price rounds to zero; owner review is needed.',
  },
};

const SESSION_BLOCKERS: Record<string, { title: string; resolution: string }> = {
  ACTIVE_SESSION_LIMIT: {
    title: 'Another session is active',
    resolution: 'Only one session per account may be active. End or complete the named session first.',
  },
  EXECUTION_OWNERSHIP_NOT_HELD: {
    title: 'Execution ownership not held',
    resolution: 'Acquire execution ownership for this deployment.',
  },
  STOP_LATCHED: { title: 'Stop latched', resolution: 'Request START from the control panel after reviewing preflight.' },
  RESUME_HOLD: { title: 'Resume hold active', resolution: 'Request START from the control panel to release the hold.' },
  LOSS_BUDGET: { title: 'Loss budget breached', resolution: 'The account loss budget blocks new exposure; review the breach.' },
  FEES_NOT_FINAL: {
    title: 'Fees not final',
    resolution: 'Wait for final commission reports, or accept the fee estimate for each execution.',
  },
  CLEAN_RECONCILIATION_AFTER_LAST_EXECUTION_REQUIRED: {
    title: 'Clean reconciliation after the last execution required',
    resolution: 'Wait for the next clean reconciliation that starts after the session’s last execution.',
  },
  SESSION_EXCEPTIONS_OPEN: {
    title: 'Session exceptions open',
    resolution: 'Resolve the blocking exception cases attributed to this session.',
  },
  NOT_FLAT: { title: 'Not flat', resolution: 'The session still holds positions or open orders.' },
};

/** Describe a session blocker code (start, resume, wind-down or reconciling blockers). */
export function describeSessionBlocker(code: string): SessionBlockerInfo {
  const m = /^([^:]+):([A-Z_]+)(.*)$/.exec(code);
  if (m && m[2] && WIND_DOWN_BLOCKERS[m[2]] && !SESSION_BLOCKERS[m[1] ?? '']) {
    const known = WIND_DOWN_BLOCKERS[m[2]]!;
    return { code, title: known.title, resolution: known.resolution, instrument: m[1] ?? null };
  }
  const i = code.indexOf(':');
  const base = i >= 0 ? code.slice(0, i) : code;
  const subject = i >= 0 ? code.slice(i + 1) : '';
  const known = SESSION_BLOCKERS[base];
  if (known) {
    return { code, title: subject ? `${known.title} (${subject})` : known.title, resolution: known.resolution, instrument: null };
  }
  const generic = describeBlocker(code);
  return { code, title: generic.title, resolution: generic.resolution, instrument: null };
}

/** Blocker codes carried by a rejection's details (START_BLOCKED / RESUME_BLOCKED). */
export function blockersFromDetails(details: unknown): string[] {
  if (!details || typeof details !== 'object') return [];
  const b = (details as { blockers?: unknown }).blockers;
  return Array.isArray(b) ? b.filter((x): x is string => typeof x === 'string') : [];
}

/** States that count toward the core's one-active-session limit (sessions.py ACTIVE). */
export const ACTIVE_STATES: ReadonlySet<string> = new Set(['RUNNING', 'SUSPENDED', 'WINDING_DOWN', 'RECONCILING', 'FAILED_REVIEW']);

/**
 * Account-level entry blockers the core would report now (common.py entries_permitted), as far as
 * the overview shows them. A preview only: the core evaluates its own state when the command runs.
 */
export function expectedEntryBlockers(o: Pick<Overview, 'control' | 'risk' | 'exceptions_open'> | null | undefined): string[] {
  if (!o?.control) return [];
  const out: string[] = [];
  const c = o.control;
  if (c.stop_latched) out.push(`STOP_LATCHED:${c.latch_reason ?? 'None'}`);
  if (c.resume_requires_owner) out.push(`RESUME_HOLD:${c.resume_reason ?? 'None'}`);
  if (c.needs_reconciliation) out.push('RECONCILIATION_REQUIRED');
  if (o.exceptions_open.some((e) => e.blocking)) out.push('BLOCKING_EXCEPTIONS');
  if (o.risk?.breach) out.push(`LOSS_BUDGET:${o.risk.breach}`);
  if (o.risk && o.risk.valuation_ok === false) out.push('VALUATION_STALE');
  return out;
}

/** Start blockers the core would report now for this session (sessions.py start_blockers), as a preview. */
export function expectedStartBlockers(
  o: Pick<Overview, 'control' | 'risk' | 'exceptions_open' | 'ownership' | 'sessions'> | null | undefined,
  sessionId: string,
): string[] {
  if (!o?.control) return [];
  const out = expectedEntryBlockers(o);
  if (!o.ownership || o.ownership.status !== 'HELD') out.push('EXECUTION_OWNERSHIP_NOT_HELD');
  const other = o.sessions.find((s) => s.session_id !== sessionId && ACTIVE_STATES.has(s.state));
  if (other) out.push(`ACTIVE_SESSION_LIMIT:${other.session_id}`);
  return out;
}

// ------------------------------------------------------------------ links

/** predecessor_id -> successor session id, from the session list itself. */
export function successorIndex(sessions: readonly Pick<Session, 'session_id' | 'predecessor_id'>[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const s of sessions) if (s.predecessor_id) out.set(s.predecessor_id, s.session_id);
  return out;
}

// ------------------------------------------------------------------ required return

export interface RequiredReturn {
  /** Percent string exactly as the core computes it, e.g. "5.0000". */
  pct: string | null;
  /** The core's own recorded value (SessionRequested info), when the timeline carries it. */
  recordedPct: string | null;
}

/** Required net return for a session: computed by the core's rule, plus the recorded figure if any. */
export function sessionRequiredReturn(session: Pick<Session, 'seed' | 'target' | 'mandate_hash'>, timeline?: readonly EventRecord[]): RequiredReturn {
  const pct = requiredReturnPct(session.seed, session.target);
  let recordedPct: string | null = null;
  if (timeline) {
    // Only the request event records the figure; an amendment replaces the mandate without it.
    const amended = timeline.some((e) => e.type === 'SessionMandateAmended');
    const req = timeline.find((e) => e.type === 'SessionRequested');
    const v = req?.info.required_net_return_pct;
    if (!amended && typeof v === 'string') recordedPct = v;
  }
  return { pct, recordedPct };
}

// ------------------------------------------------------------------ P&L

export interface SessionPnl {
  /**
   * live: from the session bucket and its marked holdings; terminal: the recorded terminal equity;
   * unfunded: no capital allocated; unavailable: a source is not loaded; inconsistent: the bucket's
   * inventory cost and the holdings' cost basis disagree (read at different moments), so nothing is shown.
   */
  basis: 'live' | 'terminal' | 'unfunded' | 'unavailable' | 'inconsistent';
  /** Liquid value of the session bucket: cash + receivable + payable + fees accrued (server figure). */
  liquid: string | null;
  inventoryCost: string | null;
  /** Sum of position market values at bid; null if any holding lacks a mark. */
  marketValue: string | null;
  /** liquid + market value. */
  markedEquity: string | null;
  /** liquid + inventory cost - seed: realized result net of fees so far. */
  realizedNet: string | null;
  /** Sum of position unrealized P&L at bid; null if any holding lacks a mark. */
  unrealized: string | null;
  /** markedEquity (live) or terminal equity (finished) minus seed. */
  changeVsSeed: string | null;
  /** target - markedEquity (live): how far the mandate's target is. */
  toTarget: string | null;
  unmarked: string[];
  fixtureMarks: boolean;
}

const EMPTY_PNL: Omit<SessionPnl, 'basis'> = {
  liquid: null,
  inventoryCost: null,
  marketValue: null,
  markedEquity: null,
  realizedNet: null,
  unrealized: null,
  changeVsSeed: null,
  toTarget: null,
  unmarked: [],
  fixtureMarks: false,
};

/**
 * Session result from server figures, exactly. Live sessions use the session's own bucket
 * (/api/buckets) and its positions marked to bid (/api/positions); finished sessions use the
 * terminal equity the core recorded. Nothing here is a forecast.
 */
export function sessionPnl(
  session: Pick<Session, 'session_id' | 'state' | 'seed' | 'target' | 'terminal_equity'>,
  bucket: Bucket | undefined,
  /** null while /api/positions has not loaded: never treat "not loaded" as "no holdings". */
  positions: readonly Position[] | null,
): SessionPnl {
  if (TERMINAL_RESULT_STATES.has(session.state)) {
    const change = session.terminal_equity !== null ? subOrNull(session.terminal_equity, session.seed) : null;
    return { ...EMPTY_PNL, basis: 'terminal', changeVsSeed: change };
  }
  if (!LIVE_CAPITAL_STATES.has(session.state)) return { ...EMPTY_PNL, basis: 'unfunded' };
  if (!bucket || positions === null) return { ...EMPTY_PNL, basis: 'unavailable' };
  const own = positions.filter((p) => p.owner === `session:${session.session_id}`);
  // The ledger's inventory cost and the open lots' cost basis describe the same holdings; if they
  // differ, the two reads straddle a fill, and combining them would misstate equity.
  const lotCost = sumDecimals(own.map((p) => p.cost_basis));
  const invCost = parseFixed(bucket.inventory_cost);
  if (lotCost === null || !invCost || cmpFixed(parseFixed(lotCost)!, invCost) !== 0) {
    return { ...EMPTY_PNL, basis: 'inconsistent', liquid: bucket.liquid_value, inventoryCost: bucket.inventory_cost };
  }
  const unmarked = own.filter((p) => p.market_value === null || p.unrealized === null).map((p) => p.instrument_id);
  const marketValue = unmarked.length ? null : sumDecimals(own.map((p) => p.market_value as string));
  const unrealized = unmarked.length ? null : sumDecimals(own.map((p) => p.unrealized as string));
  const realizedNet = sumDecimals([bucket.liquid_value, bucket.inventory_cost, negate(session.seed)]);
  const markedEquity = marketValue !== null ? sumDecimals([bucket.liquid_value, marketValue]) : null;
  return {
    basis: 'live',
    liquid: bucket.liquid_value,
    inventoryCost: bucket.inventory_cost,
    marketValue,
    markedEquity,
    realizedNet,
    unrealized,
    changeVsSeed: markedEquity !== null ? subOrNull(markedEquity, session.seed) : null,
    toTarget: markedEquity !== null ? subOrNull(session.target, markedEquity) : null,
    unmarked,
    fixtureMarks: own.some((p) => p.mark_fixture === true),
  };
}

function subOrNull(a: string, b: string): string | null {
  const x = parseFixed(a);
  const y = parseFixed(b);
  return x && y ? fixedToString(subFixed(x, y)) : null;
}

function negate(v: string): string {
  const f = parseFixed(v);
  return f ? fixedToString({ m: -f.m, s: f.s }) : v;
}

// ------------------------------------------------------------------ balances

export interface AccountCodeInfo {
  label: string;
  kind?: MoneyKind;
  note: string;
}

export const ACCOUNT_CODES: Record<string, AccountCodeInfo> = {
  CASH: { label: 'Cash', kind: 'settled', note: 'Settled cash.' },
  RECEIVABLE: { label: 'Receivable', kind: 'unsettled', note: 'Sale proceeds not yet settled (net of estimated sell fees).' },
  PAYABLE: { label: 'Payable', kind: 'unsettled', note: 'Purchase amounts not yet settled (negative).' },
  FEES_ACCRUED: { label: 'Fees accrued', kind: 'estimated', note: 'Estimated fees not yet final (negative).' },
  INVENTORY: { label: 'Inventory at cost', kind: 'cost', note: 'Cost of open lots.' },
  REALIZED: { label: 'Realized result', kind: 'realized', note: 'Income account: a gain is a credit (negative ledger balance).' },
  COMMISSION: { label: 'Commission', kind: 'realized', note: 'Final commission expense (debit).' },
  FEE_ESTIMATE: { label: 'Fee estimate', kind: 'estimated', note: 'Estimated fee expense pending the final report (debit).' },
  OTHER_FEES: { label: 'Other fees', kind: 'realized', note: 'Other fee expense (debit).' },
  DIVIDEND: { label: 'Dividends', kind: 'realized', note: 'Dividend income (credit).' },
  WITHHOLDING: { label: 'Withholding', kind: 'realized', note: 'Tax withheld (debit).' },
  INTEREST: { label: 'Interest', kind: 'realized', note: 'Interest (credit for income).' },
  ROUNDING: { label: 'Rounding', note: 'Settlement rounding differences.' },
  SUSPENSE: { label: 'Suspense', note: 'Amounts awaiting classification.' },
  OPENING: { label: 'Opening balance', note: 'Opening import offset.' },
  CONTRIBUTED: { label: 'Contributed capital', note: 'Owner contributions (credit).' },
};

export function accountCodeInfo(code: string): AccountCodeInfo {
  return ACCOUNT_CODES[code] ?? { label: code, note: 'Ledger account code.' };
}

/** Order for the balances table: session bucket first, then protected; liquid codes first. */
export function balanceSortKey(owner: string, code: string): string {
  const ownerRank = owner.startsWith('session:') ? '0' : owner.startsWith('protected:') ? '1' : '2';
  const codes = Object.keys(ACCOUNT_CODES);
  const idx = codes.indexOf(code);
  return `${ownerRank}:${String(idx < 0 ? 99 : idx).padStart(2, '0')}:${code}`;
}

export function ownerRole(owner: string): 'session' | 'protected' | 'other' {
  return owner.startsWith('session:') ? 'session' : owner.startsWith('protected:') ? 'protected' : 'other';
}
