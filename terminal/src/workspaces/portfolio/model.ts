/**
 * Portfolio analytics (backend api/routes/portfolio.py over unknown/analytics/*; CAP-PORT-01..13).
 *
 * Money in these responses is an exact decimal string with its currency, and the terminal keeps it
 * that way (comparisons and chart lengths use BigInt ratios). Returns, volatilities, VaR/ES,
 * weights from the optimiser and test statistics are statistical floats computed by the backend
 * from exact inputs; they are displayed as such and never used as money.
 */
import { compareDecimal, formatDecimal, formatMoney, isDecimal } from '../../format/decimal';
import { addDecimal, ratioBps } from '../../format/exact';
import type { Tone } from '../../ui/primitives';
import type { QueryOptions } from '../../state/react';
import { PORTFOLIO_TABS, type PortfolioTab } from './tabs';

export { PORTFOLIO_TABS, type PortfolioTab };

/**
 * For routes that read only the valuation snapshot store (written by the owner's `snapshots record` command)
 * or code: snapshots, performance, the VaR backtest and the formula list. A new core seq changes nothing in
 * them, so it does not refetch them (state/queries.ts `seqDriven`); opening the tab and Refresh do. Holdings,
 * P&L, exposure, VaR and allocation read the core's ledger or quotes and stay seq-driven.
 */
export const SNAPSHOT_STORE_QUERY: QueryOptions = { seqDriven: false };

// ------------------------------------------------------------------ routes

export function parsePortfolioRoute(rest: readonly string[]): { tab: PortfolioTab } {
  const t = rest[0];
  return { tab: PORTFOLIO_TABS.some((x) => x.id === t) ? (t as PortfolioTab) : 'holdings' };
}

// ------------------------------------------------------------------ generic access

export type Rec = Record<string, unknown>;
export const isRec = (x: unknown): x is Rec => typeof x === 'object' && x !== null && !Array.isArray(x);
export const dec = (v: unknown): string | null => (isDecimal(v) ? v : null);
export const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
export const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
export const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
export const recs = (v: unknown): Rec[] => arr(v).filter(isRec);

/**
 * The body when it is an object of the expected `kind`; null otherwise. A route that has no history
 * answers `{kind, available: false, reason}`, which is returned as is (callers show the reason).
 */
export function ofKind(body: unknown, kind: string): Rec | null {
  return isRec(body) && body.kind === kind ? body : null;
}

/** `{available: false, reason}` answers (no snapshot store yet). */
export function unavailableReason(body: Rec | null): string | null {
  return body && body.available === false ? (str(body.reason) ?? 'unavailable (no reason given)') : null;
}

// ------------------------------------------------------------------ scopes

export interface ScopeOption {
  value: string;
  label: string;
}

/** account, system, and every system owner the holdings report or the session list knows. */
export function scopeOptions(owners: readonly string[], sessionIds: readonly string[]): ScopeOption[] {
  const out: ScopeOption[] = [
    { value: 'account', label: 'Account: every owner, including outside' },
    { value: 'system', label: 'System: authorized capital' },
  ];
  const seen = new Set<string>();
  const add = (owner: string) => {
    if (seen.has(owner) || owner === 'outside' || owner === 'external') return;
    seen.add(owner);
    out.push({ value: `owner:${owner}`, label: ownerLabel(owner) });
  };
  for (const o of ['unallocated', 'buffer', ...owners]) add(o);
  for (const s of sessionIds) add(`session:${s}`);
  return out;
}

export function ownerLabel(owner: string): string {
  if (owner.startsWith('session:')) return `Session ${owner.slice('session:'.length)}`;
  if (owner.startsWith('protected:')) return `Protected proceeds ${owner.slice('protected:'.length)}`;
  if (owner.startsWith('exception:')) return `Exception ${owner.slice('exception:'.length)}`;
  return owner.charAt(0).toUpperCase() + owner.slice(1);
}

/** A session id cut for narrow places (bars, cards); the full id goes in a title. */
export function shortSessionId(id: string, head = 12): string {
  return id.length > head + 2 ? `${id.slice(0, head)}…` : id;
}

/** `ownerLabel` with a shortened session id, for bar labels and other narrow places. */
export function shortOwnerLabel(owner: string): string {
  return owner.startsWith('session:') ? `Session ${shortSessionId(owner.slice('session:'.length))}` : ownerLabel(owner);
}

export function sessionOfScope(scope: string): string | null {
  return scope.startsWith('owner:session:') ? scope.slice('owner:session:'.length) : null;
}

// ------------------------------------------------------------------ valuation status

/**
 * Tone of a valuation or mark status (analytics/holdings.py `owner_summary` and `scope_totals`:
 * complete | stale_marks | unpriced_positions; analytics/marks.py: fresh | stale | unpriced). An
 * unpriced position leaves NAV unavailable, so it is bad; an unknown status is neutral, never ok.
 */
export function valuationTone(s: string | null): Tone {
  switch (s) {
    case 'complete':
    case 'fresh':
      return 'ok';
    case 'stale_marks':
    case 'stale':
      return 'warn';
    case 'unpriced_positions':
    case 'unpriced':
      return 'bad';
    default:
      return 'neutral';
  }
}

// ------------------------------------------------------------------ statistical formatting

/** A statistical fraction as a percentage ("0.0081" -> "+0.81%"). Display only. */
export function fmtPct(x: number | null | undefined, digits = 2, signed = false): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—';
  const v = (x * 100).toFixed(digits);
  const neg = v.startsWith('-');
  const body = neg ? v.slice(1) : v;
  if (/^0\.?0*$/.test(body)) return `${body}%`;
  return `${neg ? '−' : signed ? '+' : ''}${body}%`;
}

/** Backend notes are lower-case fragments ("estimated and final fees shown separately"); show them as sentences. */
export function sentence(text: string | null | undefined): string {
  const t = (text ?? '').trim();
  if (!t) return '';
  const s = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?)]$/.test(s) ? s : `${s}.`;
}

/** Several fragments joined as sentences. */
export function sentences(parts: readonly (string | null | undefined)[]): string {
  return parts
    .map((p) => sentence(p))
    .filter(Boolean)
    .join(' ');
}

export function fmtNum(x: number | null | undefined, digits = 4): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—';
  const v = x.toFixed(digits);
  return v.startsWith('-') ? `−${v.slice(1)}` : v;
}

/** A float amount in a currency (VaR, ES): statistical, rounded for display, labelled by the caller. */
export function fmtFloatMoney(x: number | null | undefined, currency: string, digits = 2): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—';
  return `${fmtNum(x, digits)} ${currency}`;
}

/** A decimal-string weight (8 places) as a percentage with two decimals, exactly. */
export function weightPct(w: string | null | undefined): string {
  if (!isDecimal(w)) return '—';
  const bps = ratioBps(w, '1', 'half-even');
  if (bps === null) return '—';
  const neg = bps < 0n;
  const abs = neg ? -bps : bps;
  return `${neg ? '−' : ''}${(abs / 100n).toString()}.${(abs % 100n).toString().padStart(2, '0')}%`;
}

// ------------------------------------------------------------------ performance

export type IrrFlag = 'unique' | 'unique_short_period' | 'not_unique' | 'no_solution' | 'undefined';

export interface IrrStatus {
  flag: IrrFlag;
  label: string;
  tone: Tone;
  annualized: number | null;
  periodMwr: number | null;
  reason: string | null;
}

/**
 * The money-weighted return and whether its root is unique (performance.irr). A rate is shown only
 * when exactly one root solves the cash-flow equation; several sign changes or none are flagged.
 */
export function irrStatus(mw: unknown): IrrStatus {
  const m = isRec(mw) ? mw : {};
  const annualized = num(m.irr_annualized);
  const periodMwr = num(m.period_mwr);
  const reason = str(m.reason);
  if (annualized !== null) return { flag: 'unique', label: 'unique root', tone: 'ok', annualized, periodMwr, reason };
  if (reason && /not unique/i.test(reason)) return { flag: 'not_unique', label: 'not unique', tone: 'bad', annualized: null, periodMwr: null, reason };
  if (reason && /no rate/i.test(reason)) return { flag: 'no_solution', label: 'no solution', tone: 'bad', annualized: null, periodMwr: null, reason };
  if (periodMwr !== null) return { flag: 'unique_short_period', label: 'unique root, period only', tone: 'ok', annualized: null, periodMwr, reason };
  return { flag: 'undefined', label: 'undefined', tone: 'warn', annualized: null, periodMwr: null, reason: reason ?? 'not computed' };
}

export interface TwrStatus {
  twr: number | null;
  annualized: number | null;
  annualizedReason: string | null;
  reason: string | null;
  problems: { start: string; end: string; reason: string }[];
}

export function twrStatus(perf: Rec): TwrStatus {
  return {
    twr: num(perf.twr),
    annualized: num(perf.twr_annualized),
    annualizedReason: str(perf.twr_annualized_reason),
    reason: str(perf.reason),
    problems: recs(perf.problems).map((p) => ({ start: str(p.start) ?? '?', end: str(p.end) ?? '?', reason: str(p.reason) ?? '' })),
  };
}

/** Kupiec proportion-of-failures result in words, with the direction of any rejection. */
export function kupiecVerdict(k: unknown, level = 0.05): { text: string; tone: Tone } | null {
  if (!isRec(k)) return null;
  const p = num(k.p_value);
  const x = num(k.exceptions);
  const e = num(k.expected_exceptions);
  const n = num(k.observations);
  if (p === null || x === null || e === null || n === null) return { text: 'Kupiec test not computed (too few observations).', tone: 'neutral' };
  const head = `${x} exception(s) in ${n} day(s); about ${e.toFixed(2)} expected. Kupiec POF p = ${p.toFixed(3)}.`;
  const sig = `${level * 100}% significance level`;
  if (p >= level) return { text: `${head} Not rejected at the ${sig}: the exception rate is consistent with the VaR confidence level.`, tone: 'ok' };
  return x < e
    ? { text: `${head} Rejected at the ${sig}. Fewer exceptions than expected: the VaR may be too conservative.`, tone: 'warn' }
    : { text: `${head} Rejected at the ${sig}. More exceptions than expected: the VaR understates losses.`, tone: 'bad' };
}

// ------------------------------------------------------------------ exact bar geometry

export interface Bar {
  key: string;
  value: string;
  /** |value| / max |value| in basis points (0..10000), exact (floor). */
  bps: number;
  negative: boolean;
}

/** Bars scaled to the largest magnitude, computed on BigInt decimal ratios (no float money). */
export function exactBars(items: readonly { key: string; value: string | null }[]): Bar[] {
  const vals = items.filter((i): i is { key: string; value: string } => isDecimal(i.value));
  let max: string | null = null;
  for (const v of vals) {
    const a = v.value.startsWith('-') ? v.value.slice(1) : v.value;
    if (max === null || compareDecimal(a, max) > 0) max = a;
  }
  return vals.map((v) => {
    const a = v.value.startsWith('-') ? v.value.slice(1) : v.value;
    const bps = max && compareDecimal(max, '0') > 0 ? Number(ratioBps(a, max, 'floor') ?? 0n) : 0;
    return { key: v.key, value: v.value, bps, negative: v.value.startsWith('-') && compareDecimal(a, '0') !== 0 };
  });
}

/** Shares of a positive total in basis points (floor), for a composition bar. Negative parts are excluded and reported. */
export function exactShares(items: readonly { key: string; value: string | null }[]): { parts: { key: string; value: string; bps: number }[]; excluded: string[] } {
  const positive = items.filter((i): i is { key: string; value: string } => isDecimal(i.value) && compareDecimal(i.value, '0') > 0);
  const excluded = items.filter((i) => !isDecimal(i.value) || compareDecimal(i.value, '0') < 0).map((i) => i.key);
  let total = '0';
  for (const p of positive) total = addDecimal(total, p.value);
  return { parts: positive.map((p) => ({ key: p.key, value: p.value, bps: Number(ratioBps(p.value, total, 'floor') ?? 0n) })), excluded };
}

// ------------------------------------------------------------------ line geometry (statistical series)

export interface LineGeometry {
  d: string;
  points: { x: number; y: number; v: number; i: number }[];
  min: number;
  max: number;
}

/** SVG path for a numeric series in a w x h box with padding; flat series are centred. */
export function linePath(values: readonly number[], w: number, h: number, pad = 4): LineGeometry {
  const finite = values.filter((v) => Number.isFinite(v));
  if (finite.length === 0) return { d: '', points: [], min: 0, max: 0 };
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = max - min;
  const n = values.length;
  const points = values.map((v, i) => {
    const x = n === 1 ? w / 2 : pad + (i * (w - 2 * pad)) / (n - 1);
    const y = span === 0 ? h / 2 : pad + ((max - v) * (h - 2 * pad)) / span;
    return { x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100, v, i };
  });
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${p.x} ${p.y}`).join(' ');
  return { d, points, min, max };
}

// ------------------------------------------------------------------ VaR backtest

export interface BacktestDay {
  date: string;
  forecast: number | null;
  realized: number | null;
  exception: boolean;
}

export function backtestDays(bt: Rec): BacktestDay[] {
  return recs(bt.days).map((d) => ({ date: str(d.date) ?? '?', forecast: num(d.var_forecast), realized: num(d.realized_loss), exception: d.exception === true }));
}

// ------------------------------------------------------------------ stress

export interface ThresholdRow {
  threshold: string;
  /**
   * What the level is compared with. `equity`: a session's equity after the scenario, crossed at or
   * below the level. `loss`: an account loss (from the risk day's start, or from the P&L high-water
   * mark), crossed at or above the level.
   */
  measure: 'equity' | 'loss';
  owner: string | null;
  /** The currency of the row's amounts when it is known exactly; null when the response does not say. */
  currency: string | null;
  level: string | null;
  /** null when the backend could not decide (for example an unpriced position); `reason` says why. */
  crossed: boolean | null;
  reason: string | null;
  /** Session rows: equity before and after. Account rows: system P&L before and after (losses negative). */
  before: string | null;
  after: string | null;
  /** Account rows: the loss the level is compared with. */
  loss: string | null;
  beyond: string | null;
  consequence: string | null;
  caveat: string | null;
  /** The backend's further facts for the row: where the daily window starts, another window, stale marks. */
  notes: string[];
}

/**
 * Loss-threshold rows of a stress result (analytics/stress.py `_thresholds`). Session rows are keyed by
 * the same (owner, currency) pairs as `pnl_by_owner`, so their currency is that owner's single currency
 * there. Account rows are in the account's base currency, which the caller passes (the threshold rows
 * themselves carry no currency).
 */
export function thresholdRows(body: Rec, baseCurrency: string | null = null): ThresholdRow[] {
  const ownerCurrencies = new Map<string, Set<string>>();
  for (const p of recs(body.pnl_by_owner)) {
    const o = str(p.owner);
    const c = str(p.currency);
    if (o && c) ownerCurrencies.set(o, (ownerCurrencies.get(o) ?? new Set<string>()).add(c));
  }
  return recs(body.thresholds).map((t) => {
    const owner = str(t.owner);
    const ccys = owner ? ownerCurrencies.get(owner) : undefined;
    const currency = owner ? (ccys && ccys.size === 1 ? ([...ccys][0] ?? null) : null) : baseCurrency;
    const notes: string[] = [];
    const amount = (v: string | null) => (v === null ? '?' : currency ? formatMoney(v, currency) : `${formatDecimal(v, { minFraction: 2 })} (currency not reported)`);
    if (dec(t.day_start_pnl) !== null) notes.push(`Risk day ${str(t.risk_day) ?? '?'} starts at system P&L ${amount(dec(t.day_start_pnl))}: ${str(t.day_start_source) ?? 'source not given'}`);
    if (dec(t.loss_since_last_core_valuation) !== null) {
      const alt = t.crossed_if_window_started_at_last_core_valuation;
      notes.push(
        `Measured from the last core valuation instead, the loss would be ${amount(dec(t.loss_since_last_core_valuation))}` +
          (typeof alt === 'boolean' ? `, which ${alt ? 'would cross' : 'would not cross'} the level` : '') +
          ' (that window is an owner decision, not the core rule)',
      );
    }
    if (dec(t.pnl_high_water) !== null) notes.push(`System P&L high-water mark ${amount(dec(t.pnl_high_water))}`);
    if (str(t.marks_note)) notes.push(str(t.marks_note) ?? '');
    return {
      threshold: str(t.threshold) ?? '?',
      measure: owner !== null ? 'equity' : 'loss',
      owner,
      currency,
      level: dec(t.level),
      crossed: typeof t.crossed === 'boolean' ? t.crossed : null,
      reason: str(t.reason),
      before: dec(t.equity_before) ?? dec(t.system_pnl_before),
      after: dec(t.equity_after) ?? dec(t.system_pnl_after),
      loss: dec(t.loss_after),
      beyond: dec(t.loss_beyond_threshold),
      consequence: str(t.consequence),
      caveat: str(t.caveat),
      notes,
    };
  });
}

export const STRESS_SCENARIOS = [
  { id: 'overnight_gap', label: 'Overnight gap', param: 'gap', paramLabel: 'Gap (fraction, e.g. -0.10)', def: '-0.10' },
  { id: 'correlated_selloff', label: 'Correlated sell-off', param: 'shock', paramLabel: 'Market shock (fraction, e.g. -0.20)', def: '-0.20' },
  { id: 'stale_quote', label: 'Stale quotes', param: 'delay_s', paramLabel: 'No quote for (seconds)', def: '600' },
  { id: 'fee_revision', label: 'Fee revision', param: 'factor', paramLabel: 'Fee factor (e.g. 2)', def: '2' },
] as const;
export type StressScenario = (typeof STRESS_SCENARIOS)[number]['id'];

/**
 * The scalar parameters a stress result was computed with, as "name value" pairs (strings and numbers:
 * stale_quote's delay_s and order budget are integers). Nested parameters (per-instrument gaps, betas,
 * fee models) are shown by the scenario's own panel.
 */
export function scenarioParams(body: Rec): string {
  if (!isRec(body.parameters)) return '';
  return Object.entries(body.parameters)
    .filter(([, v]) => typeof v === 'string' || (typeof v === 'number' && Number.isFinite(v)))
    .map(([k, v]) => `${k} ${String(v)}`)
    .join(', ');
}

const SIGNED_DECIMAL = /^-?\d+(\.\d+)?$/;
const PLAIN_DECIMAL = /^\d+(\.\d+)?$/;

/** Validates a scenario parameter as the backend parses it; returns an error message or null. */
export function stressParamError(scenario: StressScenario, value: string): string | null {
  const v = value.trim();
  if (!v) return null; // the backend default applies
  switch (scenario) {
    case 'overnight_gap':
    case 'correlated_selloff': {
      // analytics/stress.py refuses a gap (or beta 1 x market shock) at or below -100%; a gap up of any size is valid.
      if (!SIGNED_DECIMAL.test(v)) return 'Enter a decimal fraction such as -0.10.';
      if (v.startsWith('-') && compareDecimal(v.slice(1), '1') >= 0) return 'A shock must be greater than -1 (a fall of less than 100%).';
      return null;
    }
    case 'stale_quote': {
      if (!/^\d+$/.test(v)) return 'Enter whole seconds.';
      const n = Number(v);
      return n >= 1 && n <= 604_800 ? null : 'Between 1 and 604,800 seconds (7 days).';
    }
    case 'fee_revision':
      if (!PLAIN_DECIMAL.test(v) || compareDecimal(v, '0') <= 0) return 'Enter a positive decimal factor such as 2.';
      return null;
  }
}

// ------------------------------------------------------------------ what-if

export interface WhatIfInput {
  session: string;
  instrument: string;
  side: 'BUY' | 'SELL';
  quantity: string;
  limit: string;
}

export function whatIfErrors(i: WhatIfInput): Partial<Record<keyof WhatIfInput, string>> {
  const e: Partial<Record<keyof WhatIfInput, string>> = {};
  if (!i.session) e.session = 'Choose a session.';
  if (!i.instrument) e.instrument = 'Choose an instrument.';
  if (!PLAIN_DECIMAL.test(i.quantity.trim()) || compareDecimal(i.quantity.trim(), '0') <= 0) e.quantity = 'Enter a positive decimal quantity.';
  if (!PLAIN_DECIMAL.test(i.limit.trim()) || compareDecimal(i.limit.trim(), '0') <= 0) e.limit = 'Enter a positive decimal limit price.';
  return e;
}

/** A what-if is computed on a core event sequence; any later core event makes it stale (whatif.is_stale). */
export function whatIfStale(snapshotSeq: number | null, currentSeq: number | null): boolean | null {
  if (snapshotSeq === null || currentSeq === null) return null;
  return currentSeq > snapshotSeq;
}

// ------------------------------------------------------------------ allocation

export const ALLOCATION_METHODS = [
  { id: 'equal_weight', label: 'Equal weight' },
  { id: 'inverse_volatility', label: 'Inverse volatility' },
  { id: 'mean_variance', label: 'Mean-variance (eq. 6.2)' },
] as const;

export interface WeightRow {
  instrument_id: string;
  current: number | null;
  equal_weight: number | null;
  inverse_volatility: number | null;
  mean_variance: number | null;
}

/** One row per instrument across the current weights and each method's target weights. */
export function weightRows(alloc: Rec): WeightRow[] {
  const baselines = isRec(alloc.baselines) ? alloc.baselines : {};
  const mv = isRec(alloc.mean_variance) ? alloc.mean_variance : {};
  const w = (src: unknown): Rec => (isRec(src) && isRec(src.weights) ? src.weights : {});
  const eq = w(baselines.equal_weight);
  const iv = w(baselines.inverse_volatility);
  const mvw = w(mv);
  const cur = isRec(mv.current_weights) ? mv.current_weights : {};
  const ids = new Set<string>([...arr(alloc.universe).filter((x): x is string => typeof x === 'string'), ...Object.keys(eq), ...Object.keys(iv), ...Object.keys(mvw), ...Object.keys(cur)]);
  return [...ids].sort().map((id) => ({ instrument_id: id, current: num(cur[id]), equal_weight: num(eq[id]), inverse_volatility: num(iv[id]), mean_variance: num(mvw[id]) }));
}

/** Allocation input checks mirroring the route (decimal strings for money-like fractions). */
export function allocationParamErrors(p: { cash_reserve: string; floor: string; cap: string; eta: string; tau: string }): Partial<Record<string, string>> {
  const e: Partial<Record<string, string>> = {};
  const frac = (k: 'cash_reserve' | 'floor' | 'cap') => {
    const v = p[k].trim();
    if (!v) return;
    if (!PLAIN_DECIMAL.test(v) || compareDecimal(v, '1') > 0) e[k] = 'A decimal fraction between 0 and 1.';
  };
  frac('cash_reserve');
  frac('floor');
  frac('cap');
  for (const k of ['eta', 'tau'] as const) {
    const v = p[k].trim();
    if (v && !PLAIN_DECIMAL.test(v)) e[k] = 'A non-negative number.';
  }
  return e;
}
