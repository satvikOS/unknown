/**
 * IBKR market-data status (backend api/routes/marketdata.py `status`, broker/ibkr/marketdata.py
 * `status_snapshot`). Parsed defensively: a missing field is shown as missing, never guessed. The
 * status file is written by the market-data session; the API joins it with the core's quotes.
 */
import type { Tone } from '../../ui/primitives';

type Rec = Record<string, unknown>;
const isRec = (x: unknown): x is Rec => typeof x === 'object' && x !== null && !Array.isArray(x);
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const bool = (v: unknown): boolean | null => (typeof v === 'boolean' ? v : null);
const strList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);

/** IBKR entitlement error codes and the state the session records (marketdata.py ENTITLEMENT_ERRORS). */
export const ENTITLEMENT_CODES: Readonly<Record<number, string>> = {
  354: 'NOT_SUBSCRIBED',
  10089: 'API_SUBSCRIPTION_REQUIRED',
  10090: 'PARTIALLY_SUBSCRIBED',
  10186: 'NOT_SUBSCRIBED_DELAYED_DISABLED',
  10197: 'COMPETING_SESSION',
};

export interface Connection {
  connected: boolean | null;
  verified_paper: boolean | null;
  reason: string | null;
  upstream_down: string | null;
  data_farms_down: string[];
  client_id: number | null;
  account: string | null;
  host: string | null;
  evidence_level: string | null;
}

export interface Unavailable {
  code: number | null;
  state: string;
  cause: string;
  text: string;
  at_ns: number | null;
}

export interface Analytics {
  quote_state: string | null;
  mid: string | null;
  spread: string | null;
  spread_bps: string | null;
  age_ms: number | null;
  stale: Record<string, boolean> | null;
  stale_missing_reason: string | null;
  missing_reason: string | null;
  live: boolean | null;
  live_reason: string | null;
  currency: string | null;
}

export interface Subscription {
  instrument_id: string;
  req_id: number | null;
  current: boolean;
  status: string;
  live: boolean;
  live_reason: string | null;
  reported_status: string | null;
  requested_type: string | null;
  delivered_type: number | null;
  delivered_type_name: string | null;
  source: string | null;
  consolidation: string;
  feed: { consolidation: string | null; label: string | null; evidence: string | null; observed_on: string | null } | null;
  unavailable: Unavailable | null;
  connection_stale: string | null;
  resubscribe_required: boolean;
  cancel_required: boolean;
  quote_state: string | null;
  age_ms: number | null;
  ticks: number | null;
  con_id: number | null;
  warnings: string[];
  errors: { at_ns: number | null; code: number | null; text: string }[];
  fields: { name: string; present: boolean; null_reason: string | null }[];
  analytics: Analytics | null;
  analytics_missing_reason: string | null;
  sampling_mode: string | null;
}

export interface AuditEntry {
  at_ns: number | null;
  instrument_id: string | null;
  kind: string;
  detail: string;
}

export interface MarketDataStatus {
  available: boolean;
  unavailable_reason: string | null;
  evidence_level: string | null;
  status_file: string | null;
  now_ns: number | null;
  status_file_age_ms: number | null;
  status_file_stale: boolean;
  status_file_stale_reason: string | null;
  connection: Connection | null;
  line_budget: { lines: number | null; tws_reserved_lines: number | null; capacity: number | null; in_use: number | null; headroom: number | null; refused_locally: number | null; formula: string | null } | null;
  pacing: { max_per_second: number | null; reserved: number | null; in_window: number | null; violations: number | null } | null;
  requested_market_data_type: string | null;
  entitlement_error_rate: { value: string | null; numerator: number | null; denominator: number | null; formula: string | null; missing_reason: string | null } | null;
  freshness_budgets_ms: Record<string, number>;
  subscriptions: Subscription[];
  audit: AuditEntry[];
  anomalies: AuditEntry[];
  storage: string | null;
  written_at_ns: number | null;
}

function parseConnection(v: unknown): Connection | null {
  if (!isRec(v)) return null;
  return {
    connected: bool(v.connected),
    verified_paper: bool(v.verified_paper),
    reason: str(v.reason),
    upstream_down: str(v.upstream_down),
    data_farms_down: strList(v.data_farms_down),
    client_id: num(v.client_id),
    account: str(v.account),
    host: str(v.host),
    evidence_level: str(v.evidence_level),
  };
}

function parseAnalytics(v: unknown): Analytics | null {
  if (!isRec(v)) return null;
  const stale = isRec(v.stale) ? Object.fromEntries(Object.entries(v.stale).filter(([, b]) => typeof b === 'boolean')) as Record<string, boolean> : null;
  const inputs = isRec(v.inputs) ? v.inputs : {};
  return {
    quote_state: str(v.quote_state),
    mid: str(v.mid),
    spread: str(v.spread),
    spread_bps: str(v.spread_bps),
    age_ms: num(v.age_ms),
    stale,
    stale_missing_reason: str(v.stale_missing_reason),
    missing_reason: str(v.missing_reason),
    live: bool(v.live),
    live_reason: str(v.live_reason),
    currency: str(inputs.currency),
  };
}

function parseUnavailable(v: unknown): Unavailable | null {
  if (!isRec(v)) return null;
  return { code: num(v.code), state: str(v.state) ?? 'UNKNOWN', cause: str(v.cause) ?? '', text: str(v.text) ?? '', at_ns: num(v.at_ns) };
}

export function parseSubscription(v: unknown): Subscription | null {
  if (!isRec(v) || typeof v.instrument_id !== 'string' || typeof v.status !== 'string') return null;
  const feed = isRec(v.feed) ? { consolidation: str(v.feed.consolidation), label: str(v.feed.label), evidence: str(v.feed.evidence), observed_on: str(v.feed.observed_on) } : null;
  const contract = isRec(v.contract) ? v.contract : {};
  return {
    instrument_id: v.instrument_id,
    req_id: num(v.req_id),
    current: v.current !== false,
    status: v.status,
    live: v.live === true,
    live_reason: str(v.live_reason),
    reported_status: str(v.reported_status),
    requested_type: str(v.requested_type),
    delivered_type: num(v.delivered_type),
    delivered_type_name: str(v.delivered_type_name),
    source: str(v.source),
    consolidation: str(v.consolidation) ?? 'unknown',
    feed,
    unavailable: parseUnavailable(v.unavailable),
    connection_stale: str(v.connection_stale),
    resubscribe_required: v.resubscribe_required === true,
    cancel_required: v.cancel_required === true,
    quote_state: str(v.quote_state),
    age_ms: num(v.age_ms),
    ticks: num(v.ticks),
    con_id: num(contract.conId),
    warnings: strList(v.warnings),
    errors: Array.isArray(v.errors) ? v.errors.filter(isRec).map((e) => ({ at_ns: num(e.at_ns), code: num(e.code), text: str(e.text) ?? '' })) : [],
    fields: isRec(v.fields)
      ? Object.entries(v.fields)
          .filter((e): e is [string, Rec] => isRec(e[1]))
          .map(([name, f]) => ({ name, present: f.present === true, null_reason: str(f.null_reason) }))
      : [],
    analytics: parseAnalytics(v.analytics),
    analytics_missing_reason: str(v.analytics_missing_reason),
    sampling_mode: str(v.sampling_mode),
  };
}

function parseAudit(v: unknown): AuditEntry[] {
  return Array.isArray(v) ? v.filter(isRec).map((a) => ({ at_ns: num(a.at_ns), instrument_id: str(a.instrument_id), kind: str(a.kind) ?? '', detail: str(a.detail) ?? '' })) : [];
}

export function parseStatus(v: unknown): MarketDataStatus | null {
  if (!isRec(v) || typeof v.available !== 'boolean') return null;
  const lb = isRec(v.line_budget) ? v.line_budget : null;
  const pc = isRec(v.pacing) ? v.pacing : null;
  const er = isRec(v.entitlement_error_rate) ? v.entitlement_error_rate : null;
  const budgets = isRec(v.freshness_budgets_ms)
    ? (Object.fromEntries(Object.entries(v.freshness_budgets_ms).filter(([, b]) => typeof b === 'number')) as Record<string, number>)
    : {};
  return {
    available: v.available,
    unavailable_reason: str(v.unavailable_reason),
    evidence_level: str(v.evidence_level),
    status_file: str(v.status_file),
    now_ns: num(v.now_ns),
    status_file_age_ms: num(v.status_file_age_ms),
    status_file_stale: v.status_file_stale === true,
    status_file_stale_reason: str(v.status_file_stale_reason),
    connection: parseConnection(v.connection),
    line_budget: lb
      ? { lines: num(lb.lines), tws_reserved_lines: num(lb.tws_reserved_lines), capacity: num(lb.capacity), in_use: num(lb.in_use), headroom: num(lb.headroom), refused_locally: num(lb.refused_locally), formula: str(lb.formula) }
      : null,
    pacing: pc ? { max_per_second: num(pc.max_per_second), reserved: num(pc.reserved), in_window: num(pc.in_window), violations: num(pc.violations) } : null,
    requested_market_data_type: str(v.requested_market_data_type),
    entitlement_error_rate: er ? { value: str(er.value), numerator: num(er.numerator), denominator: num(er.denominator), formula: str(er.formula), missing_reason: str(er.missing_reason) } : null,
    freshness_budgets_ms: budgets,
    subscriptions: Array.isArray(v.subscriptions) ? v.subscriptions.map(parseSubscription).filter((s): s is Subscription => s !== null) : [],
    audit: parseAudit(v.audit),
    anomalies: parseAudit(v.anomalies),
    storage: str(v.storage),
    written_at_ns: num(v.written_at_ns),
  };
}

// ------------------------------------------------------------------ presentation

/** Subscription status (marketdata.py _Subscription.status, plus the API's STATUS_FILE_STALE). */
export function statusTone(status: string): Tone {
  switch (status) {
    case 'LIVE':
      return 'ok';
    case 'PENDING':
      return 'info';
    case 'FROZEN':
    case 'DELAYED':
    case 'DELAYED_FROZEN':
    case 'STALE':
    case 'STATUS_FILE_STALE':
    case 'TYPE_UNREPORTED':
      return 'warn';
    case 'UNAVAILABLE':
    case 'TYPE_MISMATCH':
      return 'bad';
    default:
      return 'neutral';
  }
}

export interface Plain {
  label: string;
  detail: string;
  tone: Tone;
}

/** The consolidation label in words: only "consolidated" is the national best bid/offer. */
export function consolidationText(c: string): Plain {
  switch (c) {
    case 'consolidated':
      return { label: 'consolidated', detail: 'Consolidated feed (all US exchanges).', tone: 'ok' };
    case 'non_consolidated':
      return {
        label: 'non-consolidated',
        detail: 'Real time but not the national best bid/offer. It validates SIMULATED and PAPER orders; LIVE requires a consolidated feed (QUOTE_CONSOLIDATED).',
        tone: 'warn',
      };
    default:
      return { label: 'unknown', detail: 'Consolidation not asserted (delayed, unreported or unavailable data, or no feed label configured).', tone: 'neutral' };
  }
}

/** Requested versus delivered type: IBKR reports what it actually delivered, which can differ. */
export function deliveredVsRequested(s: Pick<Subscription, 'requested_type' | 'delivered_type_name'>): { differs: boolean; text: string } {
  const req = s.requested_type ?? 'unknown';
  const got = s.delivered_type_name ?? 'unreported';
  if (got === req) return { differs: false, text: got };
  return { differs: true, text: `${got} (requested ${req})` };
}

export function connectionSummary(c: Connection | null, fileStale: boolean): Plain {
  if (!c) return { label: 'no connection record', detail: 'The status file has no connection block.', tone: 'bad' };
  if (fileStale) return { label: 'status stale', detail: 'The session has not rewritten its status recently; it may have stopped. Nothing is reported live.', tone: 'warn' };
  if (c.connected && c.verified_paper && !c.upstream_down && c.data_farms_down.length === 0) {
    return { label: 'connected · paper verified', detail: 'Socket up and the PAPER identity verified.', tone: 'ok' };
  }
  if (c.connected && c.verified_paper) {
    const why = c.upstream_down ? `upstream down (${c.upstream_down})` : `data farms down: ${c.data_farms_down.join(', ')}`;
    return { label: 'connected · degraded', detail: why, tone: 'warn' };
  }
  if (c.connected) return { label: 'connected · not verified', detail: c.reason ?? 'PAPER identity not verified', tone: 'warn' };
  return { label: 'not connected', detail: c.reason ?? 'NOT_CONNECTED', tone: 'bad' };
}

export interface EntitlementIssue {
  instrument_id: string;
  code: number | null;
  state: string;
  cause: string;
  blocking: boolean;
}

/** Entitlement and availability problems per subscription: unavailable states and partial-subscription (10090) warnings. */
export function entitlementIssues(subs: readonly Subscription[]): EntitlementIssue[] {
  const out: EntitlementIssue[] = [];
  for (const s of subs) {
    // Every unavailable state blocks the subscription: entitlement codes, the line limit (101) and request errors.
    if (s.unavailable) {
      out.push({ instrument_id: s.instrument_id, code: s.unavailable.code, state: s.unavailable.state, cause: s.unavailable.cause || s.unavailable.text, blocking: true });
    }
    for (const w of s.warnings) {
      const m = /^IBKR (\d+): (.*)$/.exec(w);
      if (m && Number(m[1]) === 10090) out.push({ instrument_id: s.instrument_id, code: 10090, state: 'PARTIALLY_SUBSCRIBED', cause: m[2] ?? w, blocking: false });
    }
  }
  return out;
}

export interface Counts {
  total: number;
  live: number;
  delayed: number;
  unavailable: number;
  other: number;
}

/** Current subscriptions by state. A stale status file hides each status behind STATUS_FILE_STALE; the
 *  session's last reported status is counted instead, while "live" stays false (fail closed). */
export function countSubscriptions(subs: readonly Subscription[]): Counts {
  const cur = subs.filter((s) => s.current);
  const st = (s: Subscription) => s.reported_status ?? s.status;
  const delayed = cur.filter((s) => st(s) === 'DELAYED' || st(s) === 'DELAYED_FROZEN').length;
  const live = cur.filter((s) => s.live).length;
  const unavailable = cur.filter((s) => st(s) === 'UNAVAILABLE').length;
  return { total: cur.length, live, delayed, unavailable, other: cur.length - live - delayed - unavailable };
}

/** Line budget use as a percentage of capacity (integers only; display). */
export function lineUsePercent(lb: MarketDataStatus['line_budget']): number | null {
  if (!lb || lb.capacity === null || lb.in_use === null || lb.capacity <= 0) return null;
  return Math.min(100, Math.round((lb.in_use / lb.capacity) * 100));
}
