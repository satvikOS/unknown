/**
 * Pure logic for the Operations workspace (book Ch. 22: reconciliation, alerts, service health).
 * Nothing here converts money to a JavaScript number: amounts stay decimal strings end to end.
 */
import type {
  CoreHealth,
  ExceptionCase,
  Execution,
  HealthResponse,
  Reconciliation,
} from '../../api/types';
import { formatMoney, isDecimal, parseDecimal, signOf } from '../../format/decimal';
import type { Tone } from '../../ui/primitives';

// ------------------------------------------------------------------ routes

export type OpsTab = 'status' | 'exceptions' | 'reconciliation' | 'fees';

export const OPS_TABS: readonly { id: OpsTab; label: string }[] = [
  { id: 'status', label: 'Health and control' },
  { id: 'exceptions', label: 'Exceptions' },
  { id: 'reconciliation', label: 'Reconciliation' },
  { id: 'fees', label: 'Fee estimates' },
];

/** `#/operations/<tab>/<id>`: unknown or missing tabs fall back to the status tab. */
export function parseOpsRoute(rest: readonly string[]): { tab: OpsTab; id: string | null } {
  const tab = OPS_TABS.find((t) => t.id === rest[0])?.id;
  if (!tab) return { tab: 'status', id: null };
  return { tab, id: rest[1] ?? null };
}

// ------------------------------------------------------------------ health

export interface HealthCheck {
  id: 'core' | 'broker_connection' | 'broker_auth' | 'clock_skew' | 'api_clock' | 'disk' | 'core_loop';
  label: string;
  tone: Tone;
  /** Short status in words (never colour alone). */
  status: string;
  detail: string;
}

/** Limits from the active policy's account_limits (numbers or numeric strings). */
export interface HealthLimits {
  maxClockSkewMs: number | null;
  minFreeDiskMb: number | null;
}

function asNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && /^\d+(\.\d+)?$/.test(v.trim())) return Number(v);
  return null;
}

export function healthLimits(accountLimits: Record<string, unknown> | null | undefined): HealthLimits {
  return {
    maxClockSkewMs: asNumber(accountLimits?.max_clock_skew_ms),
    minFreeDiskMb: asNumber(accountLimits?.min_free_disk_mb),
  };
}

export function isCoreHealth(core: HealthResponse['core'] | undefined | null): core is CoreHealth {
  return !!core && typeof core === 'object' && !('error' in core);
}

/** Consecutive failed core cycles at which the core latches a stop (service/core.py). */
export const CYCLE_FAILURE_STOP = 3;

/** Nanosecond difference in milliseconds. Timestamps are ~1.8e18 floats (256 ns granularity): fine for ms. */
export function nsDiffMs(a: number, b: number): number {
  return Math.round((a - b) / 1_000_000);
}

function signedMs(ms: number): string {
  return `${ms > 0 ? '+' : ms < 0 ? '−' : ''}${Math.abs(ms).toLocaleString('en-US')} ms`;
}

/**
 * Service health checks from GET /api/health, judged against the active policy's limits (the same
 * limits preflight applies). Each check carries a tone and a word status.
 */
export function healthChecks(h: HealthResponse | null | undefined, limits: HealthLimits): HealthCheck[] {
  if (!h) return [];
  const core = h.core;
  if (!isCoreHealth(core)) {
    const error = core && typeof core === 'object' && 'error' in core ? String(core.error) : 'no health record';
    return [
      {
        id: 'core',
        label: 'Core service',
        tone: 'bad',
        status: 'unreachable',
        detail: `${error}. Commands cannot be executed; the API still serves its last database state.`,
      },
    ];
  }
  const checks: HealthCheck[] = [
    {
      id: 'core',
      label: 'Core service',
      tone: 'ok',
      status: 'reachable',
      detail: `Provider ${core.provider}, account mode ${core.account_mode}.`,
    },
    {
      id: 'broker_connection',
      label: 'Broker connection',
      tone: core.broker_connected ? 'ok' : 'bad',
      status: core.broker_connected ? 'connected' : 'disconnected',
      detail: core.detail || 'No detail reported.',
    },
    {
      id: 'broker_auth',
      label: 'Broker authentication',
      tone: core.broker_authenticated ? 'ok' : 'bad',
      status: core.broker_authenticated ? 'authenticated' : 'authentication required',
      detail: core.broker_authenticated
        ? 'The broker session is authenticated.'
        : 'Re-authenticate the broker session on the host running the core; entries stay blocked.',
    },
  ];
  if (core.broker_server_time_ns === null || core.broker_server_time_ns === undefined) {
    checks.push({
      id: 'clock_skew',
      label: 'Broker clock skew',
      tone: 'neutral',
      status: 'not reported',
      detail: 'The broker did not report its server time.',
    });
  } else {
    const skew = nsDiffMs(core.broker_server_time_ns, core.now_ns);
    const limit = limits.maxClockSkewMs;
    const over = limit !== null && Math.abs(skew) > limit;
    checks.push({
      id: 'clock_skew',
      label: 'Broker clock skew',
      tone: over ? 'bad' : 'ok',
      status: signedMs(skew),
      detail:
        limit === null
          ? 'Broker server time minus core time. No policy limit loaded.'
          : `Broker server time minus core time; the policy allows ±${limit.toLocaleString('en-US')} ms.${over ? ' Preflight reports CLOCK_SKEW.' : ''}`,
    });
  }
  const apiSkew = nsDiffMs(h.api_time_ns, core.now_ns);
  const apiOver = limits.maxClockSkewMs !== null && Math.abs(apiSkew) > limits.maxClockSkewMs;
  checks.push({
    id: 'api_clock',
    label: 'API − core time',
    tone: apiOver ? 'warn' : 'ok',
    status: signedMs(apiSkew),
    detail: 'API time when it answered minus the core time it was given; includes the socket round trip.',
  });
  const minDisk = limits.minFreeDiskMb;
  const disk = core.disk_free_mb;
  checks.push({
    id: 'disk',
    label: 'Free disk',
    tone: minDisk === null ? 'neutral' : disk < minDisk ? 'bad' : disk < minDisk * 2 ? 'warn' : 'ok',
    status: `${disk.toLocaleString('en-US')} MB`,
    detail:
      minDisk === null
        ? 'No policy minimum loaded.'
        : disk < minDisk
          ? `Below the policy minimum of ${minDisk.toLocaleString('en-US')} MB: preflight reports DISK_SPACE.`
          : `Policy minimum ${minDisk.toLocaleString('en-US')} MB.`,
  });
  const errs = core.cycle_errors;
  checks.push({
    id: 'core_loop',
    label: 'Core loop',
    tone: errs === 0 ? 'ok' : errs < CYCLE_FAILURE_STOP ? 'warn' : 'bad',
    status: errs === 0 ? 'no failures' : `${errs} consecutive failure${errs === 1 ? '' : 's'}`,
    detail:
      errs === 0
        ? 'The service loop is completing its cycles.'
        : `The core latches a stop after ${CYCLE_FAILURE_STOP} consecutive failed cycles (and again at 10 and 100). See the core log.`,
  });
  return checks;
}

export function worstTone(tones: readonly Tone[]): Tone {
  const order: Tone[] = ['bad', 'warn', 'info', 'ok', 'neutral'];
  for (const t of order) if (tones.includes(t)) return t;
  return 'neutral';
}

// ------------------------------------------------------------------ record detail

export type DetailField =
  | { key: string; label: string; kind: 'money'; value: string; currency: string }
  | { key: string; label: string; kind: 'quantity'; value: string }
  | { key: string; label: string; kind: 'decimal'; value: string }
  | { key: string; label: string; kind: 'id'; value: string; workspace: string | null }
  | { key: string; label: string; kind: 'time'; value: number }
  | { key: string; label: string; kind: 'text'; value: string }
  | { key: string; label: string; kind: 'number'; value: number }
  | { key: string; label: string; kind: 'bool'; value: boolean }
  | { key: string; label: string; kind: 'none' }
  | { key: string; label: string; kind: 'json'; value: unknown };

/** Keys whose decimal values are quantities (shares, contracts), not money. */
const QUANTITY_KEYS = new Set(['quantity', 'broker_filled', 'local', 'filled_quantity', 'open_qty', 'leaves_qty', 'cum_qty']);
/** POSITION_MISMATCH compares quantities under these keys. */
const POSITION_QTY_KEYS = new Set(['broker', 'ledger']);
const ID_WORKSPACE: Record<string, string> = {
  order_id: 'orders',
  instrument_id: 'instruments',
  session_id: 'sessions',
  case_id: 'operations',
  run_id: 'operations',
};

export function humanizeKey(key: string): string {
  const s = key.replace(/_ns$/, '').replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Classify the fields of an exception or reconciliation-difference `detail` record for display.
 * Decimal values become money only when the record states its currency; quantities are recognised
 * by key; a decimal with neither is shown exactly, marked as having no stated unit.
 */
export function describeDetail(detail: Record<string, unknown> | null | undefined, context?: { kind?: string }): DetailField[] {
  if (!detail) return [];
  const ccy = typeof detail.currency === 'string' && /^[A-Z]{3}$/.test(detail.currency) ? detail.currency : null;
  const positionLike = context?.kind === 'POSITION_MISMATCH' || 'instrument_id' in detail;
  return Object.entries(detail).map(([key, value]): DetailField => {
    const label = humanizeKey(key);
    if (value === null || value === undefined || value === '') return { key, label, kind: 'none' };
    if (typeof value === 'boolean') return { key, label, kind: 'bool', value };
    if (typeof value === 'number') {
      if (key.endsWith('_ns')) return { key, label, kind: 'time', value };
      return { key, label, kind: 'number', value };
    }
    if (typeof value === 'string') {
      if (key.endsWith('_id') || key === 'key') return { key, label, kind: 'id', value, workspace: ID_WORKSPACE[key] ?? null };
      if (isDecimal(value)) {
        if (QUANTITY_KEYS.has(key) || (positionLike && POSITION_QTY_KEYS.has(key))) return { key, label, kind: 'quantity', value };
        if (ccy) return { key, label, kind: 'money', value, currency: ccy };
        return { key, label, kind: 'decimal', value };
      }
      return { key, label, kind: 'text', value };
    }
    return { key, label, kind: 'json', value };
  });
}

// ------------------------------------------------------------------ exceptions

/** Every registered currency keeps 6 decimal places in the ledger (backend domain/money.py CURRENCIES). */
export const LEDGER_SCALE = 6;

export type ResolveAction = 'acknowledge' | 'record_external_cash_flow' | 'cover_shortfall';

/** Owners an overcommitment may be funded from (core/handlers/control.py _cover_shortfall). */
export const SHORTFALL_SOURCES = ['unallocated', 'buffer', 'outside'] as const;
export type ShortfallSource = (typeof SHORTFALL_SOURCES)[number];

export interface ResolveDraft {
  action: ResolveAction;
  note: string;
  currency: string;
  amount: string;
  fromOwner?: ShortfallSource | '';
}

/** The resolve_exception payload the core reads today (assignable to `CommandPayloads['resolve_exception']`). */
export interface ResolvePayload {
  case_id: string;
  action: ResolveAction;
  note: string;
  currency?: string;
  amount?: string;
  from_owner?: ShortfallSource;
}

export interface ResolveCheck {
  ok: boolean;
  errors: Partial<Record<'action' | 'note' | 'currency' | 'amount' | 'fromOwner', string>>;
  payload: ResolvePayload | null;
  /** Plain statement of what the core will book, shown before sending. */
  effect: string;
}

/** Actions the core accepts for this case (core/handlers/control.py resolve_exception). */
export function allowedActions(c: Pick<ExceptionCase, 'kind'>): ResolveAction[] {
  if (c.kind === 'CASH_MISMATCH') return ['acknowledge', 'record_external_cash_flow'];
  if (c.kind === 'OVERCOMMITTED') return ['cover_shortfall', 'acknowledge'];
  return ['acknowledge'];
}

/** For OVERCOMMITTED: the shortfall the core reported when it opened (or last updated) the case. */
export function reportedShortfall(c: Pick<ExceptionCase, 'kind' | 'detail'>): { currency: string; amount: string } | null {
  if (c.kind !== 'OVERCOMMITTED') return null;
  const ccy = c.detail?.currency;
  const amt = c.detail?.shortfall ?? c.detail?.need;
  if (typeof ccy !== 'string' || typeof amt !== 'string' || !isDecimal(amt)) return null;
  return { currency: ccy, amount: amt };
}

/** Canonical signed decimal for the payload ("+12.50" -> "12.50"); null when malformed. */
export function normalizeSignedAmount(raw: string): string | null {
  const s = raw.trim();
  if (!/^[+-]?\d+(\.\d+)?$/.test(s)) return null;
  return s.startsWith('+') ? s.slice(1) : s;
}

/** Magnitude of a decimal string without its sign (for wording "deposit of 12.50"). */
export function magnitude(value: string): string {
  return value.replace(/^[+-]/, '');
}

export function validateResolve(c: ExceptionCase, d: ResolveDraft): ResolveCheck {
  const errors: ResolveCheck['errors'] = {};
  if (c.status !== 'OPEN') errors.action = `This case is ${c.status}; only open cases can be resolved.`;
  if (!allowedActions(c).includes(d.action)) {
    errors.action =
      d.action === 'record_external_cash_flow'
        ? 'External cash flows resolve CASH_MISMATCH cases only.'
        : d.action === 'cover_shortfall'
          ? 'Covering a shortfall resolves OVERCOMMITTED cases only.'
          : 'This action does not apply to this case.';
  }
  const note = d.note.trim();
  if (!note) errors.note = 'A note is required: say what you checked and why this resolves the case.';
  let effect = 'Marks the case resolved with your note. Nothing is booked to the ledger.';
  let amount: string | null = null;
  const currency = d.currency.trim().toUpperCase();
  if (d.action === 'record_external_cash_flow') {
    if (!/^[A-Z]{3}$/.test(currency)) errors.currency = 'Enter a three-letter currency code, e.g. USD.';
    if (/,/.test(d.amount)) {
      errors.amount = 'Use a dot for decimals and no thousands separators, e.g. -1250.50.';
    } else {
      amount = normalizeSignedAmount(d.amount);
      const parsed = amount === null ? null : parseDecimal(amount);
      if (amount === null || parsed === null) {
        errors.amount = 'Enter a signed amount: positive for money that arrived, negative for money that left.';
        amount = null;
      } else if (signOf(amount) === 0) {
        errors.amount = 'The amount cannot be zero.';
        amount = null;
      } else if (parsed.frac.length > LEDGER_SCALE) {
        errors.amount = `The ledger keeps ${LEDGER_SCALE} decimal places; this amount has ${parsed.frac.length}.`;
        amount = null;
      }
    }
    if (amount !== null && !errors.currency) {
      const deposit = signOf(amount) > 0;
      const m = formatMoney(magnitude(amount), currency);
      effect = deposit
        ? `Records a deposit of ${m} that arrived outside the system: CASH +${m} and CONTRIBUTED −${m} for owner “outside” (not authorized for trading). The account then needs a fresh clean reconciliation.`
        : `Records a withdrawal of ${m}: CASH −${m} and CONTRIBUTED +${m} for owner “outside”. The core refuses it if the outside bucket has less available cash. The account then needs a fresh clean reconciliation.`;
    }
  }
  let source: ShortfallSource | null = null;
  if (d.action === 'cover_shortfall') {
    source = SHORTFALL_SOURCES.find((x) => x === d.fromOwner) ?? null;
    if (!source) errors.fromOwner = 'Choose where the cash comes from: unallocated, buffer or outside.';
    const sf = reportedShortfall(c);
    const what = sf
      ? `the shortfall (reported as ${formatMoney(sf.amount, sf.currency)}; the core recomputes it when the command runs)`
      : 'the shortfall the core computes';
    effect = `Transfers ${what} as CASH from “${source ?? '…'}” to the owner holding the deficit (an OWNER_TRANSFER entry), then closes the case. The core refuses it if “${source ?? '…'}” has less available cash.`;
  } else if (d.action === 'acknowledge' && c.kind === 'OVERCOMMITTED') {
    effect = 'Closes the case without moving cash. The core refuses this while a shortfall remains (SHORTFALL_UNCOVERED); cover it instead.';
  }
  const ok = Object.keys(errors).length === 0;
  let payload: ResolvePayload | null = null;
  if (ok) {
    if (d.action === 'record_external_cash_flow' && amount !== null) payload = { case_id: c.case_id, action: d.action, note, currency, amount };
    else if (d.action === 'cover_shortfall' && source) payload = { case_id: c.case_id, action: d.action, note, from_owner: source };
    else payload = { case_id: c.case_id, action: 'acknowledge', note };
  }
  return { ok, errors, payload, effect };
}

/** For CASH_MISMATCH: the unexplained difference the core reported, as a suggestion only. */
export function suggestedCashFlow(c: Pick<ExceptionCase, 'kind' | 'detail'>): { currency: string; amount: string } | null {
  if (c.kind !== 'CASH_MISMATCH') return null;
  const ccy = c.detail?.currency;
  const amt = c.detail?.unexplained;
  if (typeof ccy !== 'string' || typeof amt !== 'string' || !isDecimal(amt) || signOf(amt) === 0) return null;
  return { currency: ccy, amount: amt };
}

export function exceptionCounts(cases: readonly ExceptionCase[]): { open: number; blocking: number; critical: number } {
  let open = 0;
  let blocking = 0;
  let critical = 0;
  for (const c of cases) {
    if (c.status !== 'OPEN') continue;
    open += 1;
    if (c.blocking) blocking += 1;
    if (c.severity === 'critical') critical += 1;
  }
  return { open, blocking, critical };
}

export const SEVERITY_RANK: Record<string, number> = { low: 0, medium: 1, high: 2, critical: 3 };

// ------------------------------------------------------------------ reconciliation

export interface RunSummary {
  blocking: number;
  nonBlocking: number;
  kinds: string[];
}

export function runSummary(run: Pick<Reconciliation, 'differences'>): RunSummary {
  let blocking = 0;
  let nonBlocking = 0;
  const kinds = new Set<string>();
  for (const d of run.differences) {
    if (d.blocking) blocking += 1;
    else nonBlocking += 1;
    kinds.add(d.kind);
  }
  return { blocking, nonBlocking, kinds: [...kinds].sort() };
}

/** Plain-language meaning of each difference kind (core/handlers/recon.py). */
export const DIFFERENCE_HELP: Record<string, string> = {
  FOREIGN_ACTIVITY: 'An open order at the broker that this deployment did not place; the stop latches.',
  EXECUTIONS_MISSING: 'The broker reports more filled quantity than the ledger has recorded.',
  ORDER_UNRESOLVED: 'A local open order the broker no longer lists, with no conclusive lookup yet.',
  ORDER_STATUS_INCONSISTENT: 'The broker lookup reports a non-terminal status for an order it no longer lists as open.',
  ORDER_MISSING_AT_BROKER: 'A partly filled local order the broker no longer lists.',
  POSITION_MISMATCH: 'Broker position quantity differs from the ledger lots.',
  SUB_LEDGER_SCALE_RESIDUAL: 'Broker cash finer than the ledger scale; the residual is reported, never dropped.',
  ROUNDING_BOOKED: 'A cash gap fully explained by recorded per-trade rounding was booked to ROUNDING.',
  CASH_MISMATCH: 'Broker settled cash differs from ledger cash plus due settlements by an unexplained amount.',
  UNSETTLED_DIFFERENCE: 'Broker unsettled cash differs from the ledger’s expected settlements.',
};

// ------------------------------------------------------------------ fees

/** Executions whose commission is still the core's estimate (accept_fee_estimate applies). */
export function estimatedFeeExecutions(rows: readonly Execution[]): Execution[] {
  return rows.filter((e) => e.fee_status === 'ESTIMATED');
}
