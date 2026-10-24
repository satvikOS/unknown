/**
 * Wire types for the unknown-api terminal API (backend/src/unknown/api/server.py + queries.py).
 *
 * Conventions (ADR-0004, ADR-0013):
 * - Every monetary amount, price and quantity is a decimal STRING (`Decimal`). The terminal never
 *   converts these to JavaScript numbers; it formats and compares them as strings (src/format/decimal.ts).
 * - Timestamps are integer nanoseconds since the Unix epoch (`EpochNs`). They exceed 2^53, so JSON.parse
 *   yields a float accurate to roughly 0.25 microseconds. They are used only for display and for ages at
 *   millisecond resolution, never as identifiers or for exact comparison.
 * - Exact timestamps: every `*_ns` field holding an integer or null also arrives as `*_ns_str`, the same
 *   value as a decimal string (or null), in every API response (server.py, queries.with_ns_strings).
 *   Read the `_str` sibling wherever the exact value matters (`api/nsStrings.ts` exactNs/exactJson,
 *   `format/time.ts` formatTimestampNsExact, `<Timestamp nsStr>`); only a few types below declare it.
 * - Errors are `{"error", "code", ...}` with a stable `code` (`ApiErrorBody`).
 * - Rows the server stores as JSON (`row` columns) are typed with the fields the core writes today; an
 *   index signature keeps unknown future fields visible to generic renderers.
 */

/** Exact decimal string, e.g. "300.000000" or "-0.005". Never parse into a float. */
export type Decimal = string;
/** ISO 4217 currency code, e.g. "USD". */
export type CurrencyCode = string;
/** Nanoseconds since the Unix epoch (approximate in JS; see module note). */
export type EpochNs = number;
/** The exact `EpochNs` as a decimal string (the `*_ns_str` sibling of a `*_ns` field). */
export type EpochNsString = string;

export type AccountMode = 'SIMULATED' | 'PAPER' | 'LIVE';

// ------------------------------------------------------------------ auth

/** Step-up window for commands on the API's account: the active policy's reauth_max_age_s, as the core checks it. */
export interface StepUpWindow {
  account_key: string;
  max_age_s: number;
  /** "policy", "fallback: no active policy" (15 minutes), or "override (test harness)" (shorter only). */
  basis: string;
  policy_hash: string | null;
  expires_at_ns: EpochNs;
  expires_at_ns_str: EpochNsString;
}

/** GET /api/auth/session */
export interface AuthSession {
  authenticated: boolean;
  authenticated_at_ns: EpochNs | null;
  authenticated_at_ns_str?: EpochNsString | null;
  csrf_token: string | null;
  now_ns: EpochNs;
  now_ns_str?: EpochNsString;
  /** Present when authenticated; null when the core database is unreadable. */
  step_up?: StepUpWindow | null;
}

/** POST /api/auth/login (200) */
export interface LoginResponse {
  csrf_token: string;
  authenticated_at_ns: EpochNs;
}

/** POST /api/auth/reauth (200) */
export interface ReauthResponse {
  authenticated_at_ns: EpochNs;
}

/** POST /api/auth/logout (200) */
export interface LogoutResponse {
  ok: true;
}

/** Any non-2xx JSON body from the API. */
export interface ApiErrorBody {
  error: string;
  /** Stable code, e.g. COMMAND_ID_CONFLICT, COMMAND_OUTCOME_UNKNOWN, REAUTH_REQUIRED (server.py docstring). */
  code?: string;
  /** POST /api/commands errors: the command id, and whether it ran (UNKNOWN: GET /api/commands/<id>). */
  command_id?: string;
  outcome?: 'NOT_EXECUTED' | 'UNKNOWN';
}

// ------------------------------------------------------------------ accounts & control

/** accounts.row (core/handlers/admin.py register_account) */
export interface Account {
  account_key: string;
  provider: string;
  mode: AccountMode;
  base_currency: CurrencyCode;
  registered_at_ns: EpochNs;
  registered_by: string;
}

export interface LastStart {
  by: string;
  at_ns: EpochNs;
  command_id: string;
  evidence: PreflightEvidence;
}

/** control.row (ADR-0010) */
export interface ControlState {
  account_key: string;
  stop_latched: boolean;
  latch_reason: string | null;
  latched_by: string | null;
  latched_at_ns: EpochNs | null;
  latch_command_id: string | null;
  resume_requires_owner: boolean;
  resume_reason: string | null;
  needs_reconciliation: boolean;
  reconciled_epoch: number;
  last_clean_reconciliation_ns: EpochNs | null;
  broker_connected: boolean;
  broker_authenticated: boolean;
  last_start: LastStart | null;
  imported: boolean;
}

/** risk_state.row (core/handlers/valuation.py) */
export interface RiskState {
  account_key: string;
  currency: CurrencyCode;
  /** Signed net capital the owner has authorized into the system. */
  authorized_total: Decimal;
  /** System value marked to bid minus authorized_total: realized and unrealized combined. */
  cumulative_pnl: Decimal;
  pnl_high_water: Decimal;
  risk_day: string | null;
  day_start_pnl: Decimal;
  breach: string | null;
  breach_at_ns: EpochNs | null;
  last_valuation_ns: EpochNs | null;
  valuation_ok?: boolean;
  stale_instruments?: string[];
}

/** execution_ownership.row (ADR-0011) */
export interface ExecutionOwnership {
  account_key: string;
  deployment_id: string;
  epoch: number;
  status: 'HELD' | 'RELEASED' | string;
  granted_at_ns: EpochNs;
  granted_by: string;
  released_at_ns?: EpochNs;
  release_certificate?: { reconciliation_run: string; deployment_id: string; epoch: number };
}

// ------------------------------------------------------------------ balances & positions

/** queries.buckets(): per-owner liquid balances (ADR-0006). Buckets partition balances; they never add. */
export interface Bucket {
  owner: string;
  currency: CurrencyCode;
  settled_cash: Decimal;
  receivable_unsettled: Decimal;
  payable_unsettled: Decimal;
  fees_accrued: Decimal;
  liquid_value: Decimal;
  available_cash: Decimal;
  holds: Decimal;
  inventory_cost: Decimal;
  system_owned: boolean;
}

/** queries.positions(): open lots aggregated per (owner, instrument), marked to bid when a quote exists. */
export interface Position {
  owner: string;
  instrument_id: string;
  symbol: string | null;
  currency: CurrencyCode;
  quantity: Decimal;
  cost_basis: Decimal;
  lots: number;
  mark: Decimal | null;
  market_value: Decimal | null;
  unrealized: Decimal | null;
  /** Human-readable basis, e.g. "bid from IBKR age 252 ms" or "unavailable: no quote". */
  mark_basis: string;
  /** The mark came from a FIXTURE (test) quote: the terminal refuses it and shows an error, never the value. */
  mark_fixture?: boolean;
}

// ------------------------------------------------------------------ sessions

export interface SessionFunding {
  status: 'WAITING' | 'FUNDED' | string;
  reason?: string | null;
  shortfall?: Decimal | null;
  amount?: Decimal;
  note?: string;
}

/** mandates.body (policy/model.py Mandate.to_json) */
export interface Mandate {
  session_id: string;
  account_key: string;
  currency: CurrencyCode;
  seed: Decimal;
  target_terminal_equity: Decimal;
  strategy_eligibility: string[];
  session_drawdown_trigger_bps: number;
  session_max_loss: Decimal;
  auto_start: boolean;
  expires_at_ns: EpochNs | null;
  below_target_after_target_observed: string;
  max_resumes: number;
  policy_hash: string;
  created_at_ns: EpochNs;
  origin: string;
}

export type SessionState =
  | 'REQUESTED'
  | 'WAITING_FUNDS'
  | 'READY'
  | 'RUNNING'
  | 'SUSPENDED'
  | 'WINDING_DOWN'
  | 'RECONCILING'
  | 'COMPLETED_UNSETTLED'
  | 'COMPLETED_SETTLED'
  | 'CLOSED'
  | 'CANCELLED'
  | 'FAILED_REVIEW';

/** sessions.row (core/handlers/sessions.py) plus the joined mandate. */
export interface Session {
  session_id: string;
  account_key: string;
  state: SessionState;
  mandate_hash: string;
  predecessor_id: string | null;
  currency: CurrencyCode;
  seed: Decimal;
  target: Decimal;
  outcome: string | null;
  created_at_ns: EpochNs;
  funded_at_ns: EpochNs | null;
  started_at_ns: EpochNs | null;
  ended_at_ns: EpochNs | null;
  completed_at_ns: EpochNs | null;
  settled_at_ns: EpochNs | null;
  wind_down_reason: string | null;
  wind_down_evidence: unknown;
  wind_down_attempts: Record<string, number>;
  funding: SessionFunding | null;
  high_water: Decimal;
  protected_value: Decimal | null;
  terminal_equity: Decimal | null;
  resumes_used: number;
  review_return_state: string | null;
  blockers: string[];
  suspend_reason: string | null;
  mandate: Mandate | null;
  [extra: string]: unknown;
}

/** overview.sessions[]: the subset of session fields the overview carries. */
export interface SessionSummary {
  session_id: string;
  state: SessionState;
  outcome: string | null;
  seed: Decimal;
  target: Decimal;
  currency: CurrencyCode;
  funding: SessionFunding | null;
  blockers: string[] | null;
  predecessor_id: string | null;
  protected_value: Decimal | null;
}

export interface BalanceRow {
  account_code: string;
  owner: string;
  currency: CurrencyCode;
  instrument_id: string;
  amount: Decimal;
}

export interface Settlement {
  settlement_id: string;
  account_key: string;
  owner: string;
  currency: CurrencyCode;
  direction: 'RECEIVE' | 'PAY' | string;
  amount: Decimal;
  trade_date: string;
  expected_date: string;
  calendar: string;
  status: 'EXPECTED' | 'SETTLED' | 'CANCELLED' | string;
  exec_key: string;
  settled_at_ns: EpochNs | null;
  [extra: string]: unknown;
}

/** GET /api/sessions/{id} */
export interface SessionDetail extends Session {
  balances: BalanceRow[];
  orders: Order[];
  executions: Execution[];
  settlements: Settlement[];
  timeline: EventRecord[];
  successor_id: string | null;
}

// ------------------------------------------------------------------ orders & executions

export type OrderState =
  | 'RESERVED'
  | 'SUBMIT_PENDING'
  | 'ACKNOWLEDGED'
  | 'PARTIALLY_FILLED'
  | 'CANCEL_PENDING'
  | 'UNKNOWN'
  | 'FILLED'
  | 'CANCELED'
  | 'REJECTED'
  | 'EXPIRED'
  | string;

export interface OrderHold {
  kind: 'CASH' | 'QUANTITY';
  unit: string;
  amount: Decimal;
  status: 'ACTIVE' | 'RELEASED';
}

export interface OrderHoldFull extends OrderHold {
  hold_id: string;
  order_id: string;
  account_key: string;
  owner: string;
}

/** orders.row (core/handlers/orders.py create_order) with its holds. */
export interface Order {
  order_id: string;
  account_key: string;
  session_id: string;
  instrument_id: string;
  side: 'BUY' | 'SELL';
  order_type: string;
  time_in_force: string;
  quantity: Decimal;
  limit_price: Decimal;
  currency: CurrencyCode;
  state: OrderState;
  prev_state: OrderState | null;
  cancel_state: 'NONE' | 'REQUESTED' | 'SENT' | 'ACKED' | 'UNKNOWN' | string;
  cum_qty: Decimal;
  leaves_qty: Decimal;
  notional_filled: Decimal;
  avg_price: Decimal | null;
  fees_estimated: Decimal;
  fees_final: Decimal;
  fee_envelope: Decimal;
  broker_order_id: string | null;
  order_ref: string;
  accepted_at_ns: EpochNs;
  submitted_at_ns: EpochNs | null;
  terminal_at_ns: EpochNs | null;
  deadline_ns: EpochNs;
  policy_hash: string | null;
  principal_id: string;
  principal_role: string;
  strategy_id: string;
  purpose: 'ENTRY' | 'EXIT' | string;
  decision_ref: unknown;
  reason: string | null;
  broker_terminal_pending: unknown;
  command_id: string;
  holds: OrderHold[];
  [extra: string]: unknown;
}

export interface OutboxRow {
  outbox_id: string;
  order_id: string;
  account_key: string;
  action: 'SUBMIT' | 'CANCEL';
  state: string;
  attempts: number;
  last_attempt_ns: EpochNs | null;
  result: unknown;
  [extra: string]: unknown;
}

/**
 * GET /api/orders/{id}. Extends Order directly: `Omit<Order, 'holds'>` would keep only Order's index
 * signature and type every named field as unknown. OrderHoldFull extends OrderHold, so the narrower
 * `holds` is a valid override.
 */
export interface OrderDetail extends Order {
  holds: OrderHoldFull[];
  outbox: OutboxRow[];
  executions: Execution[];
  timeline: EventRecord[];
}

/** executions.row (core/handlers/broker.py) */
export interface Execution {
  exec_key: string;
  account_key: string;
  order_id: string | null;
  session_id: string | null;
  owner: string;
  instrument_id: string;
  side: 'BUY' | 'SELL';
  quantity: Decimal;
  price: Decimal;
  currency: CurrencyCode;
  notional: Decimal;
  raw_notional: Decimal;
  exec_time_ns: EpochNs;
  provider_exec_id: string | null;
  correction_of: string | null;
  corrected_by: string | null;
  status: 'ACTIVE' | 'CORRECTED' | string;
  fee_estimate: Decimal;
  fee_final: Decimal | null;
  fee_status: 'ESTIMATED' | 'FINAL' | 'ACCEPTED_ESTIMATE' | string;
  settlement_id: string;
  [extra: string]: unknown;
}

// ------------------------------------------------------------------ exceptions & reconciliation

export interface ExceptionCase {
  case_id: string;
  account_key: string;
  kind: string;
  blocking: boolean;
  status: 'OPEN' | 'RESOLVED' | string;
  severity: 'low' | 'medium' | 'high' | 'critical' | string;
  opened_at_ns: EpochNs;
  session_id: string | null;
  order_id: string | null;
  owner_hint: string | null;
  detail: Record<string, unknown>;
  key: string;
  resolution: { by: string; at_ns: EpochNs; action: string; note: string } | null;
}

export interface ReconciliationDifference {
  kind: string;
  blocking: boolean;
  detail: Record<string, unknown>;
}

export interface Reconciliation {
  run_id: string;
  account_key: string;
  status: 'CLEAN' | 'DIFFERENCES' | 'INCOMPLETE' | string;
  epoch: number;
  completed_at_ns: EpochNs;
  snapshot_as_of_ns: EpochNs | null;
  watermark: string | null;
  differences: ReconciliationDifference[];
  settlements_confirmed: string[];
  event_seq_at_start: number;
}

// ------------------------------------------------------------------ ledger & events

export interface JournalLine {
  line: number;
  account_code: string;
  owner: string;
  currency: CurrencyCode;
  instrument_id: string;
  amount: Decimal;
}

/** GET /api/ledger/entries */
export interface JournalEntry {
  entry_id: string;
  event_seq: number;
  account_key: string;
  kind: string;
  memo: string;
  reverses: string | null;
  lines: JournalLine[];
}

/** GET /api/events (payload replaced by `info` + `changed_sections`) */
export interface EventRecord {
  seq: number;
  event_id: string;
  type: string;
  schema_version: number;
  command_id: string;
  correlation_id: string;
  principal_id: string;
  account_key: string | null;
  session_id: string | null;
  order_id: string | null;
  instrument_id: string | null;
  event_time_ns: EpochNs | null;
  receipt_time_ns: EpochNs;
  recorded_time_ns: EpochNs;
  /** The exact values of the three times above (server.py adds a `*_ns_str` beside every `*_ns`). */
  event_time_ns_str?: EpochNsString | null;
  receipt_time_ns_str?: EpochNsString;
  recorded_time_ns_str?: EpochNsString;
  writer_epoch: number;
  payload_hash: string;
  chain_hash: string;
  info: Record<string, unknown>;
  changed_sections: string[];
}

/** GET /api/audit/verify */
export interface AuditVerify {
  chain_problems: string[];
  ledger_problems: string[];
  latest_seq: number;
}

// ------------------------------------------------------------------ policy & instruments

export interface PolicyBody {
  schema_version: number;
  name: string;
  mode: AccountMode;
  currency: CurrencyCode;
  eligibility: Record<string, unknown>;
  account_limits: Record<string, string | number | boolean>;
  session_template: Record<string, unknown>;
  successor: Record<string, unknown>;
  wind_down: Record<string, unknown>;
  /** Emergency liquidation behaviour on stop; only "NONE" is accepted in this release. */
  on_stop_liquidation: 'NONE' | string;
  auto_resume_after_clean_reconciliation: boolean;
  fees: Record<string, unknown>;
  settlement_calendar: string;
  [extra: string]: unknown;
}

/** GET /api/policies */
export interface PolicyRecord {
  policy_hash: string;
  name: string;
  published_by: string;
  published_at_ns: EpochNs;
  body: PolicyBody;
}

export interface PolicyActivation {
  account_key: string;
  policy_hash: string;
  activated_at_ns: EpochNs;
  activated_by: string;
}

/** GET /api/policy/active (null when no policy is active) */
export interface ActivePolicy {
  policy_hash: string;
  name: string;
  body: PolicyBody;
  activation: PolicyActivation;
}

export interface ProviderMapping {
  provider: string;
  external_id: string;
  version: number;
  instrument_id: string;
  verified_at_ns: EpochNs | null;
  details: Record<string, unknown>;
}

/** GET /api/instruments (domain/instruments.py Instrument.to_json + mappings) */
export interface Instrument {
  instrument_id: string;
  asset_class: string;
  symbol: string;
  name: string;
  currency: CurrencyCode;
  settlement_currency: CurrencyCode;
  venue_mic: string;
  quantity_increment: Decimal;
  min_quantity: Decimal;
  multiplier: Decimal;
  settlement_days: number;
  tick_rules: { from_price: Decimal; tick: Decimal }[];
  leveraged: boolean;
  inverse: boolean;
  status: string;
  metadata_version: number;
  attributes: Record<string, unknown>;
  mappings: ProviderMapping[];
}

/** GET /api/risk */
export interface RiskResponse {
  risk: RiskState | null;
  policy: ActivePolicy | null;
}

// ------------------------------------------------------------------ live process state (core socket)

/** market/quotes.py Quote.to_json */
export interface Quote {
  instrument_id: string;
  bid: Decimal | null;
  ask: Decimal | null;
  last: Decimal | null;
  currency: CurrencyCode;
  source: string;
  event_time_ns: EpochNs | null;
  receipt_time_ns: EpochNs;
  /** Age when the API answered; add the time since the response arrived for a live age. */
  age_ms: number;
  delayed: boolean;
  fixture: boolean;
}

/** GET /api/quotes: instrument_id -> latest quote ({} when the core is unreachable). */
export type QuoteBook = Record<string, Quote>;

export interface CoreHealth {
  broker_connected: boolean;
  broker_authenticated: boolean;
  broker_server_time_ns: EpochNs | null;
  disk_free_mb: number;
  detail: string;
  now_ns: EpochNs;
  cycle_errors: number;
  provider: string;
  account_mode: AccountMode;
}

/** GET /api/health */
export interface HealthResponse {
  core: CoreHealth | { error: string };
  api_time_ns: EpochNs;
  latest_seq: number;
}

export interface PreflightEvidence {
  epoch?: number;
  policy_hash?: string;
  health?: {
    broker_connected: boolean;
    broker_authenticated: boolean;
    broker_server_time_ns: EpochNs | null;
    disk_free_mb: number;
    detail: string;
  };
  reconciliation?: string | null;
  [extra: string]: unknown;
}

/** core/handlers/control.py stop_progress: never "flat" from an empty order list alone. */
export interface StopProgress {
  entries_blocked: boolean;
  open_buys: number;
  cancels_pending: number;
  unknown_orders: number;
  open_sells: number;
  /** owner -> instrument_id -> open quantity */
  residual_positions: Record<string, Record<string, Decimal>>;
  reconciled_flat: boolean;
}

/** GET /api/preflight (dry run of green start with the session's authentication time) */
export interface PreflightResponse {
  blockers: string[];
  evidence: PreflightEvidence;
  stop_progress: StopProgress;
}

/** GET /api/capabilities: the parity register when published, else an empty list with a note. */
export interface CapabilitiesResponse {
  capabilities?: unknown[];
  note?: string;
  [extra: string]: unknown;
}

// ------------------------------------------------------------------ overview

/** GET /api/overview */
export interface Overview {
  account: Account | null;
  control: ControlState | null;
  risk: RiskState | null;
  ownership: ExecutionOwnership | null;
  active_policy: string | null;
  buckets: Bucket[];
  positions: Position[];
  sessions: SessionSummary[];
  open_orders: number;
  unknown_orders: number;
  exceptions_open: ExceptionCase[];
  last_reconciliation: Reconciliation | null;
  latest_seq: number;
}

// ------------------------------------------------------------------ commands

/** Command kinds the API forwards from the terminal (server.py TERMINAL_KINDS). */
export type CommandKind =
  | 'register_account'
  | 'publish_policy'
  | 'activate_policy'
  | 'acquire_execution_ownership'
  | 'release_execution_ownership'
  | 'authorize_capital'
  | 'deauthorize_capital'
  | 'create_session'
  | 'cancel_session'
  | 'suspend_session'
  | 'resume_session'
  | 'end_session'
  | 'start'
  | 'resolve_exception'
  | 'accept_fee_estimate'
  | 'register_instrument'
  | 'amend_session_request'
  | 'stop'
  | 'start_session'
  | 'request_cancel'
  | 'submit_intent'
  | 'submit_research_proposal';

/** Kinds that need a passphrase re-entry no older than 15 minutes (server.py STEP_UP_KINDS). */
export const STEP_UP_KINDS: ReadonlySet<CommandKind> = new Set<CommandKind>([
  'start',
  'publish_policy',
  'activate_policy',
  'authorize_capital',
  'deauthorize_capital',
  'acquire_execution_ownership',
  'release_execution_ownership',
  'resolve_exception',
]);

/** Payloads the core handlers read today. Kinds not listed here accept a generic object. */
export interface CommandPayloads {
  stop: { account_key: string; reason?: string };
  start: { account_key: string };
  authorize_capital: { account_key: string; amount: Decimal; currency?: CurrencyCode };
  deauthorize_capital: { account_key: string; amount: Decimal; currency?: CurrencyCode };
  activate_policy: { account_key: string; policy_hash: string };
  publish_policy: { body: PolicyBody };
  acquire_execution_ownership: { account_key: string; deployment_id: string };
  release_execution_ownership: { account_key: string };
  /** core/handlers/control.py resolve_exception (cover_shortfall resolves OVERCOMMITTED cases). */
  resolve_exception: {
    case_id: string;
    action?: 'acknowledge' | 'record_external_cash_flow' | 'cover_shortfall';
    note: string;
    currency?: CurrencyCode;
    amount?: Decimal;
    from_owner?: 'unallocated' | 'buffer' | 'outside';
  };
  accept_fee_estimate: { exec_key: string };
  request_cancel: { order_id: string; reason?: string; [extra: string]: unknown };
  /** core/handlers/sessions.py: only the fields the owner entered override the active template. */
  create_session: { account_key: string; seed?: Decimal; target_terminal_equity?: Decimal };
  amend_session_request: { session_id: string; seed?: Decimal; target_terminal_equity?: Decimal };
  cancel_session: { session_id: string; reason?: string };
  suspend_session: { session_id: string; reason?: string };
  resume_session: { session_id: string; reason?: string };
  end_session: { session_id: string; reason?: string };
  start_session: { session_id: string; reason?: string };
  /** core/handlers/orders.py submit_intent (owner manual intent). */
  submit_intent: {
    account_key: string;
    session_id: string;
    order_id: string;
    instrument_id: string;
    side: 'BUY' | 'SELL';
    quantity: Decimal;
    limit_price: Decimal;
    order_type: 'LIMIT';
    time_in_force: 'DAY';
    deadline_ns: number;
    policy_hash: string;
  };
}

export type CommandPayload<K extends CommandKind> = K extends keyof CommandPayloads
  ? CommandPayloads[K]
  : Record<string, unknown>;

/** A deterministic domain refusal stored as the command's result (domain/errors.py Rejected.to_json). */
export interface Rejection {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

/** POST /api/commands (200): the core accepted or deterministically rejected the command. */
export interface CommandResponse {
  command_id: string;
  accepted: boolean;
  /** True when this command id was already recorded; the stored result is returned again. */
  duplicate: boolean;
  /** Handler result when accepted; a `Rejection` when not. */
  body: Record<string, unknown>;
  /** Sequence numbers of the events this execution recorded (absent or empty on duplicates). */
  events?: number[];
  /**
   * Set when the core recorded the command and then failed (e.g. writing its restore marker): the result
   * above stands, and the core needs the owner's attention ("<ExceptionType>: <message>").
   */
  post_commit_error?: string;
}

/** GET /api/commands/<command_id> (200): the owner's stored result for a command id. */
export interface CommandResult {
  command_id: string;
  kind: string;
  status: 'ACCEPTED' | 'REJECTED';
  accepted: boolean;
  /** Handler result when accepted; a `Rejection` when not. */
  body: Record<string, unknown>;
  /** Sequence numbers of the events the execution recorded. */
  events: number[];
  payload_hash: string;
  principal_id: string;
  recorded_time_ns: EpochNs;
  recorded_time_ns_str: EpochNsString;
  /** "core": answered after every request the core had queued; "database": the core did not answer. */
  source: 'core' | 'database';
}

export function isRejection(body: unknown): body is Rejection {
  return (
    typeof body === 'object' &&
    body !== null &&
    typeof (body as { code?: unknown }).code === 'string' &&
    typeof (body as { message?: unknown }).message === 'string'
  );
}

// ------------------------------------------------------------------ endpoint map (typed GETs)

/** Query parameters accepted by each GET endpoint (server.py `_get`). */
export interface EndpointParams {
  '/api/overview': { account?: string };
  '/api/accounts': Record<string, never>;
  '/api/sessions': { account?: string };
  '/api/orders': { account?: string; state?: 'open'; session?: string; limit?: number };
  '/api/positions': { account?: string };
  '/api/buckets': { account?: string };
  '/api/executions': { account?: string; limit?: number };
  /** status=OPEN (default) returns open cases; any other value (use "ALL") returns every case. */
  '/api/exceptions': { account?: string; status?: 'OPEN' | 'ALL' };
  '/api/reconciliations': { account?: string; limit?: number };
  '/api/ledger/entries': { account?: string; owner?: string; limit?: number; before?: number };
  '/api/events': { after?: number; limit?: number; type?: string; session?: string; order?: string; account?: string };
  '/api/audit/verify': Record<string, never>;
  '/api/policies': Record<string, never>;
  '/api/policy/active': { account?: string };
  '/api/instruments': Record<string, never>;
  '/api/risk': { account?: string };
  '/api/quotes': Record<string, never>;
  '/api/health': Record<string, never>;
  '/api/preflight': Record<string, never>;
  '/api/capabilities': Record<string, never>;
  '/api/auth/session': Record<string, never>;
}

export interface EndpointResponse {
  '/api/overview': Overview;
  '/api/accounts': Account[];
  '/api/sessions': Session[];
  '/api/orders': Order[];
  '/api/positions': Position[];
  '/api/buckets': Bucket[];
  '/api/executions': Execution[];
  '/api/exceptions': ExceptionCase[];
  '/api/reconciliations': Reconciliation[];
  '/api/ledger/entries': JournalEntry[];
  '/api/events': EventRecord[];
  '/api/audit/verify': AuditVerify;
  '/api/policies': PolicyRecord[];
  '/api/policy/active': ActivePolicy | null;
  '/api/instruments': Instrument[];
  '/api/risk': RiskResponse;
  '/api/quotes': QuoteBook;
  '/api/health': HealthResponse;
  '/api/preflight': PreflightResponse;
  '/api/capabilities': CapabilitiesResponse;
  '/api/auth/session': AuthSession;
}

/** Detail endpoints addressed by id. */
export type SessionDetailPath = `/api/sessions/${string}`;
export type OrderDetailPath = `/api/orders/${string}`;
export type CommandResultPath = `/api/commands/${string}`;

export type ListEndpoint = keyof EndpointResponse;
export type Endpoint = ListEndpoint | SessionDetailPath | OrderDetailPath | CommandResultPath;

export type ResponseOf<E extends Endpoint> = E extends ListEndpoint
  ? EndpointResponse[E]
  : E extends SessionDetailPath
    ? SessionDetail
    : E extends OrderDetailPath
      ? OrderDetail
      : E extends CommandResultPath
        ? CommandResult
        : never;

export type ParamsOf<E extends Endpoint> = E extends ListEndpoint ? EndpointParams[E] : Record<string, never>;
