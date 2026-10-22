/**
 * Policy document fields (backend/src/unknown/policy/model.py, schema_version 1) with their units,
 * labels and one-line meanings. Used to render policy bodies, limits and field-by-field diffs. The
 * server parses policies strictly; this table only describes them. Fields it does not know are still
 * shown, as raw JSON.
 */
import { formatDecimal, formatMoney, isDecimal } from '../../format/decimal';
import { bpsToPercent, ratioBps, subDecimal } from '../../format/exact';

export type FieldUnit = 'money' | 'moneyPerShare' | 'bps' | 'ms' | 's' | 'sOrNever' | 'perMinute' | 'mb' | 'count' | 'bool' | 'text' | 'tz' | 'list' | 'url';

export interface FieldSpec {
  /** Dotted path in the policy body, e.g. "account_limits.max_order_notional". */
  path: string;
  label: string;
  unit: FieldUnit;
  /** Words after a bps value, e.g. "of session equity". */
  of?: string;
  help: string;
}

export interface SectionSpec {
  id: string;
  title: string;
  help: string;
}

export const SECTIONS: readonly SectionSpec[] = [
  { id: '', title: 'Identity and mode', help: 'Which account mode and currency the policy is for.' },
  { id: 'account_limits', title: 'Account limits', help: 'Read from the active policy whenever the core evaluates an order or a valuation.' },
  { id: 'session_template', title: 'Session template', help: 'Copied into each new session’s mandate when the session is created.' },
  { id: 'eligibility', title: 'Eligibility', help: 'What may be traded at all. Short, margin, derivatives, FX and transfers are not released.' },
  { id: 'successor', title: 'Successor rule', help: 'What happens after a session reaches its target.' },
  { id: 'wind_down', title: 'Wind-down', help: 'How a session’s inventory is sold after its target is observed.' },
  { id: 'fees', title: 'Fee estimate', help: 'Conservative estimate used for reservations; the broker’s report is final.' },
];

export const FIELDS: readonly FieldSpec[] = [
  { path: 'schema_version', label: 'Schema version', unit: 'count', help: 'Policy document schema; only 1 is accepted.' },
  { path: 'name', label: 'Name', unit: 'text', help: 'Owner-chosen name; the hash identifies the version.' },
  { path: 'mode', label: 'Mode', unit: 'text', help: 'Must equal the account mode to activate. LIVE cannot be activated in this release.' },
  { path: 'currency', label: 'Currency', unit: 'text', help: 'Must equal the account base currency. Every amount in the policy is in it.' },
  {
    path: 'on_stop_liquidation',
    label: 'Liquidation on stop',
    unit: 'text',
    help: 'Only NONE is released: a stop blocks new entries and never sells holdings.',
  },
  {
    path: 'auto_resume_after_clean_reconciliation',
    label: 'Auto-resume after clean reconciliation',
    unit: 'bool',
    help: 'Whether a restart hold clears without the owner after a clean reconciliation.',
  },
  { path: 'settlement_calendar', label: 'Settlement calendar', unit: 'text', help: 'Calendar used for expected settlement dates.' },

  { path: 'account_limits.authorized_capital', label: 'Authorized capital', unit: 'money', help: 'Most capital the owner may authorize into the system.' },
  { path: 'account_limits.operational_buffer', label: 'Operational buffer', unit: 'money', help: 'Unallocated cash that session funding never uses.' },
  { path: 'account_limits.max_order_notional', label: 'Max order notional', unit: 'money', help: 'Largest single buy (quantity × limit price).' },
  {
    path: 'account_limits.max_position_bps_of_session_equity',
    label: 'Max position concentration',
    unit: 'bps',
    of: 'of session equity',
    help: 'Position after a buy, at its limit price, against the session’s equity.',
  },
  {
    path: 'account_limits.max_gross_exposure',
    label: 'Max gross exposure',
    unit: 'money',
    help: 'System positions at bid (else cost) plus open buy commitments.',
  },
  { path: 'account_limits.max_orders_per_minute', label: 'Order rate', unit: 'perMinute', help: 'Orders accepted for the account in any 60 seconds.' },
  { path: 'account_limits.price_collar_bps', label: 'Price collar', unit: 'bps', of: 'from mid', help: 'How far a limit may be from the quote mid.' },
  { path: 'account_limits.quote_max_age_ms', label: 'Quote max age', unit: 'ms', help: 'Older quotes cannot validate an order or mark a position.' },
  {
    path: 'account_limits.daily_loss_trigger',
    label: 'Daily loss trigger',
    unit: 'money',
    help: 'Loss since the first valuation of the risk day that latches the stop.',
  },
  {
    path: 'account_limits.cumulative_loss_allowance',
    label: 'Cumulative loss allowance',
    unit: 'money',
    help: 'Loss from the P&L high-water mark that latches the stop; gains never refill it.',
  },
  { path: 'account_limits.risk_day_timezone', label: 'Risk-day time zone', unit: 'tz', help: 'Where the risk day starts and ends.' },
  { path: 'account_limits.max_reconciliation_age_s', label: 'Max reconciliation age', unit: 's', help: 'Start needs a clean reconciliation at most this old.' },
  {
    path: 'account_limits.reauth_max_age_s',
    label: 'Re-authentication max age',
    unit: 's',
    help: 'The core rejects start when the owner’s passphrase entry is older.',
  },
  { path: 'account_limits.max_clock_skew_ms', label: 'Max clock skew', unit: 'ms', help: 'Allowed difference from the broker’s clock.' },
  { path: 'account_limits.min_free_disk_mb', label: 'Min free disk', unit: 'mb', help: 'Start is blocked below this free space.' },
  {
    path: 'account_limits.max_participation_bps_of_adv',
    label: 'Max participation',
    unit: 'bps',
    of: 'of average daily volume',
    help: 'Largest order quantity relative to average daily volume.',
  },
  { path: 'account_limits.require_liquidity_data', label: 'Require liquidity data', unit: 'bool', help: 'Refuse orders when average daily volume is unknown.' },

  { path: 'session_template.seed', label: 'Seed', unit: 'money', help: 'Capital a new session is funded with.' },
  {
    path: 'session_template.target_terminal_equity',
    label: 'Target terminal equity',
    unit: 'money',
    help: 'Equity at which the session winds down. A configured goal, not a forecast.',
  },
  { path: 'session_template.strategy_eligibility', label: 'Eligible strategies', unit: 'list', help: 'Strategies allowed to trade in the session.' },
  {
    path: 'session_template.session_drawdown_trigger_bps',
    label: 'Session drawdown trigger',
    unit: 'bps',
    of: 'below the session high-water',
    help: 'Suspends the session when its equity falls this far.',
  },
  { path: 'session_template.session_max_loss', label: 'Session max loss', unit: 'money', help: 'Suspends the session when seed minus equity reaches it.' },
  { path: 'session_template.auto_start', label: 'Auto-start', unit: 'bool', help: 'Start the session as soon as it is funded.' },
  { path: 'session_template.expires_after_s', label: 'Expires after', unit: 'sOrNever', help: 'Session lifetime; null means no expiry.' },
  {
    path: 'session_template.below_target_after_target_observed',
    label: 'Below target after target observed',
    unit: 'text',
    help: 'CLOSE ends the session; RESUME trades again (needs max resumes ≥ 1).',
  },
  { path: 'session_template.max_resumes', label: 'Max resumes', unit: 'count', help: 'How often a session may resume after falling below target.' },

  { path: 'eligibility.asset_classes', label: 'Asset classes', unit: 'list', help: 'Only EQUITY and ETF are released.' },
  { path: 'eligibility.venues', label: 'Venues', unit: 'list', help: 'Market identifier codes an instrument may list on.' },
  { path: 'eligibility.order_types', label: 'Order types', unit: 'list', help: 'Only LIMIT is released.' },
  { path: 'eligibility.time_in_force', label: 'Time in force', unit: 'list', help: 'Only DAY is released.' },
  { path: 'eligibility.outside_regular_hours', label: 'Outside regular hours', unit: 'bool', help: 'Not released.' },
  { path: 'eligibility.allow_short', label: 'Short selling', unit: 'bool', help: 'Not released.' },
  { path: 'eligibility.allow_margin', label: 'Margin', unit: 'bool', help: 'Not released.' },
  { path: 'eligibility.allow_derivatives', label: 'Derivatives', unit: 'bool', help: 'Not released.' },
  { path: 'eligibility.allow_leveraged_or_inverse', label: 'Leveraged or inverse products', unit: 'bool', help: 'Leveraged and inverse ETFs.' },
  { path: 'eligibility.allow_fractional', label: 'Fractional quantities', unit: 'bool', help: 'Fractional share orders.' },
  { path: 'eligibility.allow_fx_conversion', label: 'FX conversion', unit: 'bool', help: 'Not released.' },
  { path: 'eligibility.allow_transfers', label: 'Transfers', unit: 'bool', help: 'Not released.' },

  { path: 'successor.create_on_success', label: 'Create successor on success', unit: 'bool', help: 'Open a new session after a successful one.' },
  {
    path: 'successor.mandate_source',
    label: 'Successor mandate source',
    unit: 'text',
    help: 'ACTIVE_TEMPLATE_AT_CREATION: the successor copies the template active when it is created.',
  },
  {
    path: 'successor.reinvest_protected_proceeds',
    label: 'Reinvest protected proceeds',
    unit: 'bool',
    help: 'Not offered: completed proceeds stay protected.',
  },

  { path: 'wind_down.order_style', label: 'Order style', unit: 'text', help: 'How wind-down sell limits are priced.' },
  { path: 'wind_down.collar_bps', label: 'Wind-down collar', unit: 'bps', of: 'below the bid', help: 'Sell limit distance below the bid.' },
  { path: 'wind_down.reprice_after_s', label: 'Reprice after', unit: 's', help: 'An unfilled wind-down order is cancelled and repriced after this.' },
  { path: 'wind_down.max_attempts', label: 'Max attempts', unit: 'count', help: 'Wind-down orders per instrument before review.' },

  { path: 'fees.model', label: 'Fee model', unit: 'text', help: 'Shape of the fee estimate.' },
  { path: 'fees.per_share', label: 'Per share', unit: 'moneyPerShare', help: 'Estimated commission per share.' },
  { path: 'fees.minimum', label: 'Minimum per order', unit: 'money', help: 'Estimated minimum commission.' },
  { path: 'fees.maximum_bps_of_notional', label: 'Maximum', unit: 'bps', of: 'of notional', help: 'Estimated commission cap.' },
  { path: 'fees.estimate_margin_bps', label: 'Estimate margin', unit: 'bps', of: 'added to the estimate', help: 'Safety margin on reserved fees.' },
  { path: 'fees.source', label: 'Source', unit: 'url', help: 'Where the fee shape comes from.' },
  { path: 'fees.verified_on', label: 'Verified', unit: 'text', help: 'When and how the fee shape was checked.' },
];

const BY_PATH = new Map(FIELDS.map((f, i) => [f.path, { spec: f, index: i }]));

export function fieldSpec(path: string): FieldSpec | undefined {
  return BY_PATH.get(path)?.spec;
}

/** Position in the documented field order; unknown fields sort after known ones. */
export function fieldOrder(path: string): number {
  return BY_PATH.get(path)?.index ?? Number.MAX_SAFE_INTEGER;
}

export function sectionOf(path: string): string {
  const dot = path.indexOf('.');
  return dot < 0 ? '' : path.slice(0, dot);
}

export function sectionTitle(id: string): string {
  return SECTIONS.find((s) => s.id === id)?.title ?? id.replace(/_/g, ' ');
}

/** "max_order_notional" -> "Max order notional" for fields the table does not know. */
export function humanizeKey(path: string): string {
  const last = path.slice(path.lastIndexOf('.') + 1);
  return last.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

// ------------------------------------------------------------------ formatting

export interface FormattedValue {
  text: string;
  /** A unit conversion or explanation, e.g. "1.00%" or "15 min". */
  detail?: string;
  /** Set when the value does not have the expected JSON type (the server will reject it). */
  warning?: string;
}

function groupInt(n: number): string {
  return formatDecimal(String(n));
}

function isInt(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v);
}

/** Exact human duration for whole seconds: 900 -> "15 min", 5400 -> "1 h 30 min", 86400 -> "1 d". */
export function humanSeconds(total: number): string {
  if (!Number.isInteger(total) || total < 0) return `${total} s`;
  if (total === 0) return '0 s';
  const parts: string[] = [];
  const d = Math.floor(total / 86_400);
  const h = Math.floor((total % 86_400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (d) parts.push(`${d} d`);
  if (h) parts.push(`${h} h`);
  if (m) parts.push(`${m} min`);
  if (s) parts.push(`${s} s`);
  return parts.join(' ');
}

function raw(value: unknown): string {
  return value === undefined ? 'absent' : JSON.stringify(value);
}

/** Render a policy value with its unit. Never converts money to a number. */
export function formatPolicyValue(value: unknown, unit: FieldUnit, currency: string, of?: string): FormattedValue {
  const wrong = (expected: string): FormattedValue => ({ text: raw(value), warning: `expected ${expected}` });
  switch (unit) {
    case 'money':
      return typeof value === 'string' && isDecimal(value) ? { text: formatMoney(value, currency) } : wrong('a decimal string');
    case 'moneyPerShare':
      return typeof value === 'string' && isDecimal(value) ? { text: `${formatDecimal(value)} ${currency} per share` } : wrong('a decimal string');
    case 'bps':
      return isInt(value) ? { text: `${groupInt(value)} bps${of ? ` ${of}` : ''}`, detail: bpsToPercent(value) } : wrong('an integer');
    case 'ms':
      if (!isInt(value)) return wrong('an integer');
      return value >= 1000 && value % 1000 === 0 ? { text: `${groupInt(value)} ms`, detail: humanSeconds(value / 1000) } : { text: `${groupInt(value)} ms` };
    case 's':
      return isInt(value) ? { text: `${groupInt(value)} s`, ...(value >= 60 ? { detail: humanSeconds(value) } : {}) } : wrong('an integer');
    case 'sOrNever':
      if (value === null) return { text: 'never', detail: 'no expiry' };
      return isInt(value) ? { text: `${groupInt(value)} s`, ...(value >= 60 ? { detail: humanSeconds(value) } : {}) } : wrong('an integer or null');
    case 'perMinute':
      return isInt(value) ? { text: `${groupInt(value)} orders per minute` } : wrong('an integer');
    case 'mb':
      return isInt(value) ? { text: `${groupInt(value)} MB` } : wrong('an integer');
    case 'count':
      return isInt(value) ? { text: groupInt(value) } : wrong('an integer');
    case 'bool':
      return typeof value === 'boolean' ? { text: value ? 'yes' : 'no' } : wrong('true or false');
    case 'list':
      if (!Array.isArray(value)) return wrong('a list');
      return { text: value.length ? value.map((v) => (typeof v === 'string' ? v : JSON.stringify(v))).join(', ') : 'none' };
    case 'tz':
    case 'text':
    case 'url':
      return typeof value === 'string' ? { text: value } : wrong('a string');
  }
}

/** Format any path: known fields with units, unknown ones as raw JSON. */
export function formatField(path: string, value: unknown, currency: string): FormattedValue {
  const spec = fieldSpec(path);
  if (!spec) return { text: raw(value) };
  return formatPolicyValue(value, spec.unit, currency, spec.of);
}

/**
 * Implied required net return of a session template: (target - seed) / seed, exact to 0.01%
 * (half-even, as the core records it with a SessionRequested event). "999,900.00%" for 1 -> 10,000.
 */
export function impliedRequiredReturn(seed: unknown, target: unknown): string | null {
  if (typeof seed !== 'string' || typeof target !== 'string' || !isDecimal(seed) || !isDecimal(target)) return null;
  const bps = ratioBps(subDecimal(target, seed), seed, 'half-even');
  return bps === null ? null : bpsToPercent(bps);
}
