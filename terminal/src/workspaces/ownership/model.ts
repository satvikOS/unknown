/**
 * Ownership (CAP-CO-09): wire types of GET /api/ownership/<cik|ticker> and pure helpers for the views.
 * Numbers stay exact decimal strings; display rounding happens here and the exact value stays in the tooltip.
 */
import { formatDecimal, isDecimal } from '../../format/decimal';
import { fromScaled, toScaled } from '../../format/exact';
import { shiftDecimal } from '../research-common/format';

export interface SectionStatus {
  status: string;
  reason?: string;
  detail?: string | null;
}

export interface InsiderTxn {
  obs_id: number;
  accession: string;
  form: string | null;
  filing_date: string | null;
  available_time: string;
  insider: string;
  role: string;
  table: 'non_derivative' | 'derivative';
  kind: 'transaction' | 'holding';
  security: string | null;
  date: string | null;
  code: string | null;
  code_label: string;
  category: string;
  shares: string | null;
  price: string | null;
  price_reason: string | null;
  acquired_disposed: string | null;
  shares_after: string | null;
  direct_indirect: string | null;
  nature: string | null;
  superseded_by_amendment: boolean;
  links: { filing_index: string | null; document: string | null };
}

export interface Flow {
  window_days: number;
  from: string;
  to: string;
  purchases: string;
  sales: string;
  net: string;
  purchase_count: number;
  sale_count: number;
  excluded_without_price: number;
  unit: string;
}

export interface InsidersSection extends SectionStatus {
  transactions: InsiderTxn[];
  count?: number;
  truncated?: boolean;
  filings?: number;
  summary: Record<string, Flow> | null;
  ten_b5_one?: string;
}

export interface Holder {
  manager_cik: number;
  manager: string | null;
  shares: string | null;
  shares_reason?: string;
  value?: string;
  value_unit?: string;
  change: string;
  prior_shares: string | null;
  change_shares: string | null;
  accession: string;
  filing_date: string | null;
  pct_held: string | null;
  pct_reason: string | null;
  amendment_type?: string | null;
}

export interface Coverage {
  window: string;
  period: string;
  holdings_reports_in_window: number;
  holders_found: number;
  filers_count: number;
}

export interface InstitutionalSection extends SectionStatus {
  period?: string;
  prior_period?: string | null;
  holders: Holder[];
  holder_count?: number;
  total_shares_reported?: string;
  shares_outstanding?: { value: string | null; date?: string; concept?: string; reason?: string; detail?: string; gap_days?: number };
  concentration?: Record<string, string | null>;
  changes?: { entries: number; exits: number; increases: number; decreases: number };
  coverage?: Coverage[];
  notes?: string[];
}

export interface SchedulePerson {
  name: string | null;
  aggregate_shares: string | null;
  percent_of_class: string | null;
  type: string | null;
}

export interface ScheduleFiling {
  accession: string;
  form: string;
  filing_date: string;
  parsed: boolean;
  persons: SchedulePerson[];
  event_date?: string | null;
  index_url: string;
  document_url?: string | null;
  reason?: string;
}

export interface HoldersSection extends SectionStatus {
  filings: ScheduleFiling[];
  count?: number;
  note?: string;
}

export interface OwnershipResponse {
  cik: number;
  entity_id: string;
  name: string | null;
  tickers: string[];
  as_of: string;
  insiders?: InsidersSection;
  institutional?: InstitutionalSection;
  major_holders?: HoldersSection;
  commands: string[];
  non_us: string;
  status?: string;
  reason?: string;
  detail?: string;
}

export type OwnershipTab = 'institutional' | 'insiders' | 'holders';
export const TABS: readonly { id: OwnershipTab; label: string }[] = [
  { id: 'institutional', label: 'Institutional (13F)' },
  { id: 'insiders', label: 'Insiders (Forms 3/4/5)' },
  { id: 'holders', label: '5 % holders (13D/13G)' },
];

/** `#/ownership/<key>/<tab>`: key is a CIK, ticker or live key (AAPL.XNAS). */
export function parseOwnershipRoute(rest: readonly string[]): { key: string | null; tab: OwnershipTab } {
  const key = rest[0] && rest[0].trim() ? rest[0].trim().toUpperCase() : null;
  const tab = TABS.find((t) => t.id === rest[1])?.id ?? 'institutional';
  return { key, tab };
}

/** Round a decimal string half away from zero to `places` (display only). */
export function roundDecimal(value: string, places: number): string | null {
  if (!isDecimal(value)) return null;
  const { units, scale } = toScaled(value);
  if (scale <= places) return fromScaled(units, scale);
  const div = 10n ** BigInt(scale - places);
  const half = div / 2n;
  const q = units >= 0n ? (units + half) / div : -((-units + half) / div);
  return fromScaled(q, places);
}

/** A fraction ("0.0796890911") as a short percentage ("7.97 %"); the exact value belongs in the tooltip. */
export function fractionPct(value: string | null | undefined, places = 2): string | null {
  if (!value || !isDecimal(value)) return null;
  const shifted = shiftDecimal(value, 2);
  const r = shifted === null ? null : roundDecimal(shifted, places);
  return r === null ? null : `${formatDecimal(r, { minFraction: places })} %`;
}

/** An amount with two decimals, grouped ("-182550" -> "−182,550.00"). */
export function amount2(value: string): string {
  return formatDecimal(value, { minFraction: 2 });
}

export function changeTone(change: string): 'ok' | 'bad' | 'info' | 'neutral' {
  if (change === 'INCREASE' || change === 'ENTRY') return 'ok';
  if (change === 'DECREASE' || change === 'EXIT') return 'bad';
  if (change === 'UNCHANGED') return 'neutral';
  return 'info';
}

export const CHANGE_LABEL: Record<string, string> = {
  INCREASE: 'Increase',
  DECREASE: 'Decrease',
  UNCHANGED: 'Unchanged',
  ENTRY: 'New position',
  EXIT: 'Exit (not in filed report)',
  NO_PRIOR_REPORT: 'No prior report held',
  NO_PRIOR_PERIOD: 'No prior period held',
};

/** Insider rows a filter keeps: all, open-market trades only, or one category. */
export function filterInsiders(rows: readonly InsiderTxn[], only: 'all' | 'open_market'): InsiderTxn[] {
  if (only === 'all') return [...rows];
  return rows.filter((r) => r.category === 'open_market_purchase' || r.category === 'open_market_sale');
}

export function holderMatches(h: Holder, q: string): boolean {
  const t = q.trim().toLowerCase();
  return !t || (h.manager ?? '').toLowerCase().includes(t) || String(h.manager_cik).includes(t);
}
