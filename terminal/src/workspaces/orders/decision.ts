/**
 * Pretrade decision records (risk/pretrade.py Decision.to_json) as the terminal receives them:
 *   - a rejected `submit_intent` returns `body.details.decision` and `body.details.quote`;
 *   - an accepted one records them in the order's `OrderAccepted` event (`info.decision`,
 *     `info.quote`, `info.required_cash`), which GET /api/orders/{id} returns in `timeline`.
 * Everything here parses `unknown` defensively: a missing or malformed record is reported as absent,
 * never invented.
 */
import type { EventRecord, Quote, Rejection } from '../../api/types';

export interface PretradeCheck {
  check: string;
  passed: boolean;
  /** The core's text for this rule. For a passing check it can describe the failing condition. */
  detail: string;
}

export interface PretradeDecision {
  approved: boolean;
  /** "APPROVED" or the first failing check (the decisive one). */
  code: string;
  checks: PretradeCheck[];
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

export function parseCheck(x: unknown): PretradeCheck | null {
  if (!isRecord(x) || typeof x.check !== 'string' || typeof x.passed !== 'boolean') return null;
  return { check: x.check, passed: x.passed, detail: typeof x.detail === 'string' ? x.detail : '' };
}

export function parseDecision(x: unknown): PretradeDecision | null {
  if (!isRecord(x) || typeof x.approved !== 'boolean' || typeof x.code !== 'string' || !Array.isArray(x.checks)) return null;
  const checks: PretradeCheck[] = [];
  for (const c of x.checks) {
    const parsed = parseCheck(c);
    if (!parsed) return null;
    checks.push(parsed);
  }
  return { approved: x.approved, code: x.code, checks };
}

const NULLABLE_DECIMAL = (v: unknown) => v === null || typeof v === 'string';

/** market/quotes.py Quote.to_json, as recorded with a decision. */
export function parseQuote(x: unknown): Quote | null {
  if (!isRecord(x)) return null;
  if (typeof x.instrument_id !== 'string' || typeof x.source !== 'string' || typeof x.currency !== 'string') return null;
  if (!NULLABLE_DECIMAL(x.bid) || !NULLABLE_DECIMAL(x.ask) || !NULLABLE_DECIMAL(x.last)) return null;
  if (typeof x.age_ms !== 'number') return null;
  return {
    instrument_id: x.instrument_id,
    bid: (x.bid as string | null) ?? null,
    ask: (x.ask as string | null) ?? null,
    last: (x.last as string | null) ?? null,
    currency: x.currency,
    source: x.source,
    event_time_ns: typeof x.event_time_ns === 'number' ? x.event_time_ns : null,
    receipt_time_ns: typeof x.receipt_time_ns === 'number' ? x.receipt_time_ns : 0,
    age_ms: x.age_ms,
    delayed: x.delayed === true,
    fixture: x.fixture === true,
  };
}

export interface DecisionRecord {
  decision: PretradeDecision | null;
  /** The quote the core evaluated, as it was at decision time. */
  quote: Quote | null;
  /** Cash the core required for a BUY (notional + fee envelope), decimal string; null otherwise. */
  requiredCash: string | null;
  /** Event seq carrying the record (accepted orders only). */
  seq: number | null;
}

/** Decision carried by a rejected command (`body.details`). Rejections before pretrade carry none. */
export function decisionFromRejection(rejection: Rejection | null | undefined): DecisionRecord {
  const details = rejection && isRecord(rejection.details) ? rejection.details : {};
  return {
    decision: parseDecision(details.decision),
    quote: parseQuote(details.quote),
    requiredCash: null,
    seq: null,
  };
}

/** Decision recorded when the core accepted the order (the `OrderAccepted` event in its timeline). */
export function decisionFromTimeline(timeline: readonly EventRecord[] | null | undefined): DecisionRecord | null {
  const ev = (timeline ?? []).find((e) => e.type === 'OrderAccepted');
  if (!ev) return null;
  const info = isRecord(ev.info) ? ev.info : {};
  return {
    decision: parseDecision(info.decision),
    quote: parseQuote(info.quote),
    requiredCash: typeof info.required_cash === 'string' ? info.required_cash : null,
    seq: ev.seq,
  };
}

export interface DecisionSummary {
  total: number;
  passed: number;
  failed: number;
  /** The first failing check: the core reports it as the rejection code. */
  decisive: PretradeCheck | null;
}

export function summarizeDecision(d: PretradeDecision): DecisionSummary {
  const failed = d.checks.filter((c) => !c.passed);
  return { total: d.checks.length, passed: d.checks.length - failed.length, failed: failed.length, decisive: failed[0] ?? null };
}

/**
 * Plain-language reason for rejection codes raised before the pretrade evaluation (no check list).
 * Pretrade codes are check names and are explained by the check table instead.
 */
const PRE_PRETRADE: Record<string, string> = {
  UNKNOWN_SESSION: 'The session id is not known to the core.',
  UNKNOWN_ACCOUNT: 'The account is not registered with the core.',
  UNKNOWN_MANDATE: 'The session’s mandate record is missing.',
  SESSION_ACCOUNT_MISMATCH: 'The session belongs to a different account.',
  DUPLICATE_ORDER_ID: 'An order with this id already exists; a new ticket gets a new id.',
  INVALID_FIELD: 'A field of the intent is malformed.',
  UNAUTHORIZED: 'This principal may not issue the command.',
  DEADLINE_EXPIRED: 'The command deadline passed before the core executed it.',
  UNKNOWN_ORDER: 'The order id is not known to the core.',
  COMMAND_ID_CONFLICT: 'The command id already names a different command (HTTP 409), which keeps its own result.',
};

export function explainRejectionCode(code: string): string | null {
  return PRE_PRETRADE[code] ?? null;
}
