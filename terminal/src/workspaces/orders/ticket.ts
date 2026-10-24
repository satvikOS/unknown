/**
 * Order ticket logic for owner manual intents (`submit_intent`, core/handlers/orders.py).
 *
 * The terminal validates only what makes the form usable: required fields, decimal syntax and an id
 * the core accepts. Tick size, quantity increment and session state are reported as advisory hints.
 * Required cash, fees, limits, collars and every other risk decision belong to the core's pretrade
 * check (risk/pretrade.py): nothing here estimates money. Quantities and prices stay decimal strings;
 * the only arithmetic is exact integer arithmetic on their digits (BigInt), never floating point.
 */
import type { Instrument, Session } from '../../api/types';
import { compareDecimal, parseDecimal } from '../../format/decimal';

export type Side = 'BUY' | 'SELL';

export interface TicketInput {
  sessionId: string;
  instrumentId: string;
  side: Side | '';
  quantity: string;
  limitPrice: string;
  /** Seconds after sending at which the intent expires if not yet transmitted (default 60). */
  deadlineSeconds: string;
}

export type TicketField = keyof TicketInput;

export const DEFAULT_DEADLINE_SECONDS = 60;
export const MIN_DEADLINE_SECONDS = 5;
export const MAX_DEADLINE_SECONDS = 3600;

export const EMPTY_TICKET: TicketInput = {
  sessionId: '',
  instrumentId: '',
  side: '',
  quantity: '',
  limitPrice: '',
  deadlineSeconds: String(DEFAULT_DEADLINE_SECONDS),
};

/** domain/ids.py validate_id: the core faults (it does not reject) on any other shape. */
export const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,159}$/;

/** Session states in which the core's SESSION_STATE check passes (risk/pretrade.py). */
export const ENTRY_SESSION_STATES: readonly string[] = ['RUNNING'];
export const REDUCE_SESSION_STATES: readonly string[] = ['RUNNING', 'SUSPENDED', 'WINDING_DOWN'];

const UNSIGNED_DECIMAL_RE = /^\d+(\.\d+)?$/;

/** A plain unsigned decimal ("1", "9.96", "0.0001") that is greater than zero. */
export function isPositiveDecimal(value: string): boolean {
  const v = value.trim();
  if (!UNSIGNED_DECIMAL_RE.test(v)) return false;
  const p = parseDecimal(v);
  return p !== null && !(p.int === '0' && p.frac === '');
}

/** Scaled integer digits of an unsigned decimal at `scale` fraction digits (exact). */
function scaled(value: string, scale: number): bigint {
  const p = parseDecimal(value.trim());
  if (!p) throw new Error(`not a decimal string: ${JSON.stringify(value)}`);
  const digits = p.int + p.frac.padEnd(scale, '0');
  const n = BigInt(digits);
  return p.negative ? -n : n;
}

/**
 * Exact `value` is a whole multiple of `step` (both decimal strings). Returns null when either is
 * malformed or the step is not positive. Mirrors domain/fixed.py Fixed.is_multiple_of.
 */
export function isMultipleOf(value: string, step: string): boolean | null {
  const pv = parseDecimal(value.trim());
  const ps = parseDecimal(step.trim());
  if (!pv || !ps) return null;
  if (ps.negative || (ps.int === '0' && ps.frac === '')) return null;
  const scale = Math.max(pv.frac.length, ps.frac.length);
  return scaled(value, scale) % scaled(step, scale) === 0n;
}

/**
 * Tick size for a price under the instrument's tick rules: the last rule (in stored order) whose
 * `from_price` is at or below the price, else the first rule. Mirrors domain/instruments.py tick_for.
 */
export function tickFor(price: string, rules: readonly { from_price: string; tick: string }[]): string | null {
  const first = rules[0];
  if (!first) return null;
  let tick = first.tick;
  for (const rule of rules) {
    try {
      if (compareDecimal(price, rule.from_price) >= 0) tick = rule.tick;
    } catch {
      return null;
    }
  }
  return tick;
}

export interface TicketContext {
  session?: Session | undefined;
  instrument?: Instrument | undefined;
}

export interface TicketIssues {
  /** Problems that prevent sending (the form cannot build a payload the core can read). */
  errors: Partial<Record<TicketField, string>>;
  /** Advisory: the core will decide, and will probably reject. Shown, never enforced. */
  hints: Partial<Record<TicketField, string>>;
}

export function hasErrors(issues: TicketIssues): boolean {
  return Object.keys(issues.errors).length > 0;
}

/** Parses the deadline field: whole seconds within [MIN, MAX], else null. */
export function parseDeadlineSeconds(value: string): number | null {
  const v = value.trim();
  if (!/^\d{1,5}$/.test(v)) return null;
  const n = Number(v); // at most five digits: exact
  return n >= MIN_DEADLINE_SECONDS && n <= MAX_DEADLINE_SECONDS ? n : null;
}

export function validateTicket(input: TicketInput, ctx: TicketContext = {}): TicketIssues {
  const errors: TicketIssues['errors'] = {};
  const hints: TicketIssues['hints'] = {};
  const { session, instrument } = ctx;

  if (!input.sessionId) errors.sessionId = 'Choose the session that owns this order.';
  else if (!session) errors.sessionId = 'This session is not in the current session list.';
  else if (!session.mandate?.policy_hash) errors.sessionId = 'This session has no mandate policy hash, so an intent cannot name its policy.';

  if (!input.instrumentId) errors.instrumentId = 'Choose an instrument.';
  else if (!instrument) errors.instrumentId = 'This instrument is not in the instrument master.';

  if (input.side !== 'BUY' && input.side !== 'SELL') errors.side = 'Choose BUY or SELL.';

  const qty = input.quantity.trim();
  if (!qty) errors.quantity = 'Enter a quantity.';
  else if (!isPositiveDecimal(qty)) errors.quantity = 'Quantity must be a positive decimal number such as 10 (no sign, no separators).';

  const px = input.limitPrice.trim();
  if (!px) errors.limitPrice = 'Enter a limit price.';
  else if (!isPositiveDecimal(px)) errors.limitPrice = 'Limit price must be a positive decimal number such as 9.95 (no sign, no separators).';

  if (parseDeadlineSeconds(input.deadlineSeconds) === null) {
    errors.deadlineSeconds = `Deadline must be whole seconds from ${MIN_DEADLINE_SECONDS} to ${MAX_DEADLINE_SECONDS}.`;
  }

  // Advisory hints mirroring the core's checks; the core repeats every one of them.
  if (instrument && !errors.quantity) {
    const notes: string[] = [];
    try {
      if (compareDecimal(qty, instrument.min_quantity) < 0) notes.push(`below the minimum ${instrument.min_quantity}`);
    } catch {
      /* malformed instrument data: leave it to the core */
    }
    if (isMultipleOf(qty, instrument.quantity_increment) === false) notes.push(`not a multiple of the increment ${instrument.quantity_increment}`);
    if (notes.length) hints.quantity = `Quantity is ${notes.join(' and ')}; the core's QUANTITY_INCREMENT check will reject it.`;
  }
  if (instrument && !errors.limitPrice) {
    const tick = tickFor(px, instrument.tick_rules);
    if (tick && isMultipleOf(px, tick) === false) {
      hints.limitPrice = `Price is not a multiple of the tick ${tick} at this level; the core's PRICE_INCREMENT check will reject it.`;
    }
  }
  const instrumentNotes: string[] = [];
  if (instrument && instrument.status !== 'ACTIVE') {
    instrumentNotes.push(`Instrument status is ${instrument.status}; the core's INSTRUMENT_STATUS check will reject it.`);
  }
  if (session && instrument && session.currency !== instrument.currency) {
    instrumentNotes.push(`Instrument trades in ${instrument.currency} but the session is in ${session.currency}; the core's CURRENCY check will reject it.`);
  }
  if (instrumentNotes.length) hints.instrumentId = instrumentNotes.join(' ');
  if (session && (input.side === 'BUY' || input.side === 'SELL')) {
    const allowed = input.side === 'BUY' ? ENTRY_SESSION_STATES : REDUCE_SESSION_STATES;
    if (!allowed.includes(session.state)) {
      hints.sessionId = `Session is ${session.state}; a ${input.side} needs ${allowed.join(' or ')} (SESSION_STATE check).`;
    }
  }
  return { errors, hints };
}

/**
 * Intent deadline in server nanoseconds: `seconds` after the server's current time, rounded UP to a
 * whole second. Whole seconds times 1e9 are multiples of 2^9 below 2^61, so the double is exact and
 * `JSON.stringify` writes the integer digits the core reads as a Python int (`type(...) is int`).
 */
export function deadlineNsFrom(serverNowMs: number, seconds: number): number {
  const s = Math.ceil((serverNowMs + seconds * 1000) / 1000);
  return s * 1_000_000_000;
}

function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  globalThis.crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Client order id for an owner manual intent ("own-" + 20 hex). It never contains "/". */
export function newOrderId(hex: (bytes: number) => string = randomHex): string {
  return `own-${hex(10)}`;
}

/** The exact payload sent as `submit_intent` (core/handlers/orders.py submit_intent). */
export interface SubmitIntentPayload {
  [key: string]: unknown;
  account_key: string;
  session_id: string;
  order_id: string;
  instrument_id: string;
  side: Side;
  quantity: string;
  limit_price: string;
  order_type: 'LIMIT';
  time_in_force: 'DAY';
  deadline_ns: number;
  policy_hash: string;
}

/** Builds the payload; throws if the ticket has errors (call `validateTicket` first). */
export function buildIntentPayload(
  input: TicketInput,
  session: Session,
  orderId: string,
  deadlineNs: number,
): SubmitIntentPayload {
  if (input.side !== 'BUY' && input.side !== 'SELL') throw new Error('side is required');
  if (!ID_RE.test(orderId)) throw new Error(`order id ${JSON.stringify(orderId)} is not a valid core identifier`);
  const policyHash = session.mandate?.policy_hash;
  if (!policyHash) throw new Error('session has no mandate policy hash');
  if (!isPositiveDecimal(input.quantity) || !isPositiveDecimal(input.limitPrice)) throw new Error('quantity and limit price must be positive decimals');
  if (!Number.isInteger(deadlineNs) || deadlineNs <= 0) throw new Error('deadline must be a positive integer of nanoseconds');
  return {
    account_key: session.account_key,
    session_id: session.session_id,
    order_id: orderId,
    instrument_id: input.instrumentId,
    side: input.side,
    quantity: input.quantity.trim(),
    limit_price: input.limitPrice.trim(),
    order_type: 'LIMIT',
    time_in_force: 'DAY',
    deadline_ns: deadlineNs,
    policy_hash: policyHash,
  };
}

/** The purpose the core derives when the intent names none (BUY opens, SELL reduces). */
export function derivedPurpose(side: Side | ''): string {
  return side === 'BUY' ? 'ENTRY' : side === 'SELL' ? 'EXIT' : 'choose a side first';
}

/** Default session for the ticket: the first RUNNING session, else the first session listed. */
export function defaultSessionId(sessions: readonly Session[]): string {
  return (sessions.find((s) => s.state === 'RUNNING') ?? sessions[0])?.session_id ?? '';
}
