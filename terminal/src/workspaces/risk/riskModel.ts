/**
 * Pure risk read models for the Risk workspace. Every figure is derived exactly (./exact.ts) from API
 * values and follows the core's own definitions, cited per function, so the terminal never invents a
 * semantic. Derived figures are labelled in the UI as terminal estimates with their basis.
 */
import type { Bucket, Order, Position, QuoteBook, RiskState } from '../../api/types';
import { compareDecimal, isDecimal } from '../../format/decimal';
import { addDecimal, clampAtZero, maxDecimal, mulDecimal, ratioBps, subDecimal, sumDecimals } from '../../format/exact';

/** core/ledger.py is_system_owner: value that belongs to the system's authorized capital. */
export function isSystemOwner(owner: string): boolean {
  return owner === 'unallocated' || owner === 'buffer' || owner.startsWith('session:') || owner.startsWith('protected:');
}

/** "session:ses-1" -> "ses-1"; null for any other owner. */
export function sessionOfOwner(owner: string): string | null {
  return owner.startsWith('session:') ? owner.slice('session:'.length) : null;
}

/** Instrument multipliers keyed by instrument id (decimal strings from /api/instruments). */
export type Multipliers = Readonly<Record<string, string | undefined>>;

function multiplierIsOne(m: string | undefined): boolean {
  return m !== undefined && isDecimal(m) && compareDecimal(m, '1') === 0;
}

// ------------------------------------------------------------------ marked P&L estimate

export type MarkProblem = 'no quote' | 'no bid' | 'delayed' | 'stale' | 'multiplier unknown' | 'multiplier not 1';

export interface StaleMark {
  owner: string;
  instrument_id: string;
  problem: MarkProblem;
}

export interface MarkedEstimate {
  /** complete: every system position has a fresh bid; incomplete: at least one does not. */
  status: 'complete' | 'incomplete';
  /** Sum of system-owned buckets' liquid value (cash + receivable + payable + fees accrued). */
  liquid: string;
  /** Sum of system-owned positions' market value at bid (fresh marks only). */
  positions: string;
  /** liquid + positions; null when incomplete (the core fails closed and does not value). */
  systemValue: string | null;
  /** systemValue - authorized_total; null when incomplete. */
  pnl: string | null;
  stale: StaleMark[];
  /** At least one mark used came from fixture (simulation-only) quotes. */
  fixture: boolean;
}

export interface EstimateInput {
  buckets: readonly Bucket[];
  positions: readonly Position[];
  quotes: QuoteBook;
  /** Milliseconds since the quote book arrived; added to each quote's `age_ms`. */
  quotesElapsedMs: number;
  authorizedTotal: string;
  currency: string;
  quoteMaxAgeMs: number;
  multipliers: Multipliers;
}

/**
 * The core's account valuation (core/handlers/valuation.py): system value is the liquid value of every
 * system-owned bucket plus open lots marked at a fresh, non-delayed bid; P&L is system value minus
 * the net capital authorized into the system. If any system position lacks a fresh bid the valuation
 * is incomplete and no P&L is produced (the core fails closed in the same case).
 *
 * Position market values are the API's (`quantity x bid`, floored at the ledger scale). The positions
 * endpoint does not apply the instrument multiplier, so a position whose multiplier is not exactly 1
 * makes the estimate incomplete rather than wrong.
 */
export function estimateMarkedPnl(input: EstimateInput): MarkedEstimate {
  const ccy = input.currency;
  const liquid = sumDecimals(input.buckets.filter((b) => b.system_owned && b.currency === ccy && isDecimal(b.liquid_value)).map((b) => b.liquid_value));
  const stale: StaleMark[] = [];
  const values: string[] = [];
  let fixture = false;
  for (const p of input.positions) {
    if (!isSystemOwner(p.owner) || p.currency !== ccy) continue;
    const q = input.quotes[p.instrument_id];
    const mult = input.multipliers[p.instrument_id];
    let problem: MarkProblem | null = null;
    if (!q) problem = 'no quote';
    else if (q.bid === null) problem = 'no bid';
    else if (q.delayed) problem = 'delayed';
    else if (q.age_ms + Math.max(0, input.quotesElapsedMs) > input.quoteMaxAgeMs) problem = 'stale';
    else if (mult === undefined) problem = 'multiplier unknown';
    else if (!multiplierIsOne(mult)) problem = 'multiplier not 1';
    else if (p.market_value === null || !isDecimal(p.market_value)) problem = 'no bid';
    if (problem) stale.push({ owner: p.owner, instrument_id: p.instrument_id, problem });
    else {
      values.push(p.market_value as string);
      fixture ||= Boolean(q?.fixture || p.mark_fixture);
    }
  }
  const positions = sumDecimals(values);
  if (stale.length) return { status: 'incomplete', liquid, positions, systemValue: null, pnl: null, stale, fixture };
  const systemValue = addDecimal(liquid, positions);
  return { status: 'complete', liquid, positions, systemValue, pnl: subDecimal(systemValue, input.authorizedTotal), stale, fixture };
}

// ------------------------------------------------------------------ loss budgets

/**
 * Loss from the P&L high-water mark (valuation.py: hwm = max(recorded hwm, pnl); loss = hwm - pnl).
 * Earlier gains never refill the allowance.
 */
export function lossFromHighWater(recordedHighWater: string, pnl: string): string {
  const hwm = maxDecimal(recordedHighWater, pnl);
  return clampAtZero(subDecimal(hwm, pnl));
}

/** Loss since the first valuation of the risk day (valuation.py: day_start_pnl - pnl), never below zero. */
export function dailyLoss(dayStartPnl: string, pnl: string): string {
  return clampAtZero(subDecimal(dayStartPnl, pnl));
}

export type BudgetState = 'within' | 'approaching' | 'reached';

export interface BudgetUse {
  used: string;
  limit: string;
  remaining: string;
  /** used / limit in basis points, rounded UP so usage is never under-reported; null if limit <= 0. */
  bps: bigint | null;
  /** `reached` uses the core's comparison (used >= limit); `approaching` from half the limit. */
  state: BudgetState;
}

export const APPROACHING_BPS = 5_000n;

export function budgetUse(used: string, limit: string): BudgetUse {
  const bps = ratioBps(used, limit, 'ceil');
  const reached = compareDecimal(used, limit) >= 0;
  return {
    used,
    limit,
    remaining: clampAtZero(subDecimal(limit, used)),
    bps,
    state: reached ? 'reached' : bps !== null && bps >= APPROACHING_BPS ? 'approaching' : 'within',
  };
}

/** Exposure against a cap: `reached` when strictly above (the core's check is `after <= max`). */
export function capUse(used: string, cap: string): BudgetUse {
  const bps = ratioBps(used, cap, 'ceil');
  const over = compareDecimal(used, cap) > 0;
  return {
    used,
    limit: cap,
    remaining: clampAtZero(subDecimal(cap, used)),
    bps,
    state: over ? 'reached' : bps !== null && bps >= APPROACHING_BPS ? 'approaching' : 'within',
  };
}

// ------------------------------------------------------------------ risk day

/** Calendar date ("2026-10-22") at `ms` in an IANA time zone; null if the zone is unknown. */
export function riskDayAt(timeZone: string, ms: number): string | null {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(ms));
    const get = (t: string) => parts.find((p) => p.type === t)?.value;
    const y = get('year');
    const m = get('month');
    const d = get('day');
    return y && m && d ? `${y}-${m}-${d}` : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ exposure

export type ExposureBasis = 'bid' | 'cost';

export interface ExposureLine {
  owner: string;
  instrument_id: string;
  symbol: string | null;
  basis: ExposureBasis;
  value: string;
  /** The bid behind `value` came from fixture (simulation-only) quotes. */
  fixture: boolean;
}

export interface OpenBuyLine {
  order_id: string;
  session_id: string;
  instrument_id: string;
  /** leaves x limit x multiplier, exact. */
  commitment: string;
}

export interface GrossExposure {
  /** null when a multiplier is unknown or not 1 for a position (the positions API omits it). */
  total: string | null;
  positionsAtBid: string;
  positionsAtCost: string;
  openBuys: string;
  lines: ExposureLine[];
  buys: OpenBuyLine[];
  unresolved: { instrument_id: string; reason: string }[];
}

/**
 * Gross exposure as the core's pre-trade check defines it (core/handlers/orders.py _gross_exposure):
 * every system-owned lot at bid when a quote has a bid (at cost otherwise), plus the remaining
 * commitment of every open BUY order (leaves x limit x multiplier). The core rounds each order up to
 * the ledger scale; the exact product here is never larger than that by more than one ledger unit.
 */
export function grossExposure(positions: readonly Position[], openOrders: readonly Order[], currency: string, multipliers: Multipliers): GrossExposure {
  const lines: ExposureLine[] = [];
  const buys: OpenBuyLine[] = [];
  const unresolved: { instrument_id: string; reason: string }[] = [];
  for (const p of positions) {
    if (!isSystemOwner(p.owner) || p.currency !== currency) continue;
    const mult = multipliers[p.instrument_id];
    const hasMark = p.market_value !== null && isDecimal(p.market_value);
    if (hasMark && !multiplierIsOne(mult)) {
      unresolved.push({ instrument_id: p.instrument_id, reason: mult === undefined ? 'multiplier unknown' : `multiplier ${mult}` });
      continue;
    }
    lines.push({
      owner: p.owner,
      instrument_id: p.instrument_id,
      symbol: p.symbol,
      basis: hasMark ? 'bid' : 'cost',
      value: hasMark ? (p.market_value as string) : p.cost_basis,
      fixture: hasMark && Boolean(p.mark_fixture),
    });
  }
  const OPEN = new Set(['RESERVED', 'SUBMIT_PENDING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN']);
  for (const o of openOrders) {
    if (o.side !== 'BUY' || o.currency !== currency || !OPEN.has(o.state)) continue;
    const mult = multipliers[o.instrument_id];
    if (mult === undefined || !isDecimal(mult)) {
      unresolved.push({ instrument_id: o.instrument_id, reason: `order ${o.order_id}: multiplier unknown` });
      continue;
    }
    buys.push({
      order_id: o.order_id,
      session_id: o.session_id,
      instrument_id: o.instrument_id,
      commitment: mulDecimal(mulDecimal(o.leaves_qty, o.limit_price), mult),
    });
  }
  const positionsAtBid = sumDecimals(lines.filter((l) => l.basis === 'bid').map((l) => l.value));
  const positionsAtCost = sumDecimals(lines.filter((l) => l.basis === 'cost').map((l) => l.value));
  const openBuys = sumDecimals(buys.map((b) => b.commitment));
  return {
    total: unresolved.length ? null : sumDecimals([positionsAtBid, positionsAtCost, openBuys]),
    positionsAtBid,
    positionsAtCost,
    openBuys,
    lines,
    buys,
    unresolved,
  };
}

// ------------------------------------------------------------------ concentration

export interface Concentration {
  owner: string;
  session_id: string;
  instrument_id: string;
  value: string;
  basis: ExposureBasis;
  sessionEquity: string;
  /** value / session equity in bps (rounded up); null when equity is not positive. */
  bps: bigint | null;
}

/**
 * Current position size against the session's equity estimate (orders.py _session_equity_estimate:
 * the session bucket's liquid value plus its lots at bid, else at cost). The core checks the position
 * AFTER a new order at its limit price; this is the current figure for context.
 */
export function concentrations(positions: readonly Position[], buckets: readonly Bucket[], currency: string): Concentration[] {
  const out: Concentration[] = [];
  const valueOf = (p: Position): { value: string; basis: ExposureBasis } =>
    p.market_value !== null && isDecimal(p.market_value) ? { value: p.market_value, basis: 'bid' } : { value: p.cost_basis, basis: 'cost' };
  const byOwner = new Map<string, Position[]>();
  for (const p of positions) {
    if (p.currency !== currency || !sessionOfOwner(p.owner)) continue;
    byOwner.set(p.owner, [...(byOwner.get(p.owner) ?? []), p]);
  }
  for (const [owner, list] of byOwner) {
    const liquid = buckets.find((b) => b.owner === owner && b.currency === currency)?.liquid_value ?? '0';
    const equity = sumDecimals([isDecimal(liquid) ? liquid : '0', ...list.map((p) => valueOf(p).value)]);
    for (const p of list) {
      const v = valueOf(p);
      out.push({
        owner,
        session_id: sessionOfOwner(owner) as string,
        instrument_id: p.instrument_id,
        value: v.value,
        basis: v.basis,
        sessionEquity: equity,
        bps: ratioBps(v.value, equity, 'ceil'),
      });
    }
  }
  return out;
}

// ------------------------------------------------------------------ recorded state

/** Fields of the recorded risk row whose change makes the core write it (valuation.py decision_changed). */
export const RECORDED_ON_CHANGE = ['valuation_ok', 'breach', 'risk_day', 'pnl_high_water', 'stale_instruments'] as const;

export function breachLabel(breach: string | null | undefined): string {
  switch (breach) {
    case null:
    case undefined:
      return 'no breach';
    case 'CUMULATIVE_LOSS_FROM_HIGH_WATER':
      return 'cumulative loss from high-water reached the allowance';
    case 'DAILY_LOSS':
      return 'daily loss reached the trigger';
    default:
      return breach;
  }
}

/** True when the recorded row carries a decision that blocks new exposure. */
export function recordedBlocks(risk: RiskState | null | undefined): string[] {
  if (!risk) return [];
  const out: string[] = [];
  if (risk.breach) out.push(`LOSS_BUDGET:${risk.breach}`);
  if (risk.valuation_ok === false) out.push('VALUATION_STALE');
  return out;
}
