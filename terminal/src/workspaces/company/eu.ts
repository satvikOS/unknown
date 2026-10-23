/**
 * EU issuers (CAP-CO-03): types and pure helpers for `GET /api/company/eu/<LEI>[/statements|/compare]`
 * (backend `api/routes/company.py`, `data/fundamentals/ifrs.py`, `data/sources/esef.py`).
 *
 * Route: `#/company/eu/<LEI>/<tab>/as_of=…/periods=…/sec=<CIK>`. The pairing with an SEC filer (`sec`) is the
 * owner's explicit choice; it is never inferred from a name.
 */
import { routeHref } from '../../state/router';

export const EU_TABS = ['statements', 'compare', 'filings'] as const;
export type EuTab = (typeof EU_TABS)[number];
export const EU_TAB_LABELS: Record<EuTab, string> = { statements: 'ESEF statements', compare: 'ESEF vs SEC', filings: 'Filings & listings' };
export const EU_OPTION_KEYS = ['as_of', 'periods', 'sec'] as const;
export type EuOptionKey = (typeof EU_OPTION_KEYS)[number];
export type EuOptions = Partial<Record<EuOptionKey, string>>;

export interface EuRoute {
  lei: string;
  tab: EuTab;
  opts: EuOptions;
}

const LEI_RE = /^[A-Z0-9]{18}[0-9]{2}$/;

/** ISO 17442 check digits (ISO 7064 MOD 97-10), as the backend checks them. */
export function leiValid(text: string): boolean {
  const t = text.trim().toUpperCase();
  if (!LEI_RE.test(t)) return false;
  let rem = 0;
  for (const ch of t) {
    const digits = String(parseInt(ch, 36));
    for (const d of digits) rem = (rem * 10 + Number(d)) % 97;
  }
  return rem === 1;
}

/** `rest` after the workspace id; null when it is not an EU issuer route (`eu/<LEI>/...`). */
export function parseEuRoute(rest: readonly string[]): EuRoute | null {
  if (rest[0] !== 'eu' || rest[1] === undefined) return null;
  const lei = rest[1].toUpperCase();
  let i = 2;
  let tab: EuTab = 'statements';
  if (rest[2] !== undefined && (EU_TABS as readonly string[]).includes(rest[2])) {
    tab = rest[2] as EuTab;
    i = 3;
  }
  const opts: EuOptions = {};
  for (const seg of rest.slice(i)) {
    const eq = seg.indexOf('=');
    if (eq <= 0) continue;
    const k = seg.slice(0, eq);
    const v = seg.slice(eq + 1).trim();
    if (v && (EU_OPTION_KEYS as readonly string[]).includes(k)) opts[k as EuOptionKey] = v;
  }
  return { lei, tab, opts };
}

export function euRoute(r: { lei: string; tab?: EuTab; opts?: EuOptions }): string[] {
  const segs = ['eu', r.lei, r.tab ?? 'statements'];
  for (const k of EU_OPTION_KEYS) {
    const v = r.opts?.[k]?.trim();
    if (v) segs.push(`${k}=${v}`);
  }
  return segs;
}

export function euIssuerHref(lei: string, asOf?: string): string {
  return routeHref('company', ...euRoute({ lei, opts: asOf ? { as_of: asOf } : {} }));
}

export interface EuSource {
  obs_id: number;
  concept: string;
  value: string;
  unit: string;
  period_start: string | null;
  period_end: string;
  filing: string;
  filing_period_end: string | null;
  filing_country: string | null;
  available_time: string;
  quality: string[];
  viewer_url: string | null;
}

export interface EuCell {
  item: string;
  period: { kind: string; start: string; end: string; label: string };
  value: string | null;
  unit: string | null;
  method: 'REPORTED' | 'FALLBACK_CONCEPT' | 'MISSING';
  rule: string;
  concept?: string | null;
  missing_reason: string | null;
  detail: string | null;
  flags: string[];
  obs_ids: number[];
  latest_input_available_time?: string;
  sources: EuSource[];
  history?: { obs_id: number; value: string; filing: string; available_time: string }[];
  restated?: boolean;
}

export interface EuLine {
  item: string;
  label: string;
  unit: string | null;
  rule: string;
  concepts: string[];
  fallback: string[];
  cells: EuCell[];
}

export interface EuFiling {
  fxo_id: string;
  period_end: string;
  country: string;
  published: string | null;
  date_added: string | null;
  viewer_url: string | null;
  available_time?: string;
  error_count?: number | null;
  warning_count?: number | null;
}

export interface EuStatements {
  status: 'OK' | 'NO_FILINGS_INDEXED' | 'NOT_AVAILABLE';
  reason?: string;
  entity_id: string;
  lei: string;
  name: string | null;
  as_of: string;
  mapping: string;
  basis: 'FY';
  currency: string | null;
  currency_flags: string[];
  columns: { kind: string; start: string; end: string; label: string }[];
  statements: Record<string, EuLine[]>;
  filings: EuFiling[];
  countries: string[];
  source: { dataset: string; provider: string; attribution: string };
}

export interface EuListing {
  isin: string;
  mic: string;
  cfi: string | null;
  full_name: string | null;
  short_name: string | null;
  currency: string | null;
  issuer_lei: string | null;
  first_trade_date: string | null;
  termination_date: string | null;
  file_name?: string;
  source: string;
}

export interface EuIdentity {
  entity_id: string;
  lei: string;
  name: string;
  as_of: string;
  esef_status: string | null;
  reason: string | null;
  countries: string[];
  index_checked_at: string | null;
  filings: EuFiling[];
  listings: EuListing[];
  listings_note: string;
  source: { dataset: string; provider: string; attribution: string };
}

export interface CompareSide {
  value: string | null;
  unit: string | null;
  method: string | null;
  concept: string | null;
  missing_reason: string | null;
  obs_ids: number[];
  flags: string[];
  source: 'ESEF' | 'SEC';
  document: string | null;
  available_time: string | null;
}

export interface CompareResponse {
  esef: { lei: string; name: string | null; currency: string | null; mapping: string; status: string; source: { attribution: string } };
  sec: { entity_id: string; currency: string; mapping: string; source: { attribution: string } } | null;
  fy_ends: string[];
  lines: { item: string; label: string; statement: string; cells: { fy_end: string; esef: CompareSide | null; sec: CompareSide | null; difference_esef_minus_sec: string | null; note: string | null }[] }[];
  note: string;
}

export const STATEMENT_ORDER = ['income', 'per_share', 'shares', 'balance', 'cash_flow'] as const;
export const STATEMENT_TITLES: Record<string, string> = {
  income: 'Income statement',
  per_share: 'Per share',
  shares: 'Shares',
  balance: 'Balance sheet',
  cash_flow: 'Cash flow',
};

/** Ordered statement sections present in a response. */
export function euSections(s: Pick<EuStatements, 'statements'>): [string, EuLine[]][] {
  const known = STATEMENT_ORDER.filter((k) => s.statements[k]?.length).map((k) => [k, s.statements[k]] as [string, EuLine[]]);
  const extra = Object.entries(s.statements).filter(([k, v]) => !(STATEMENT_ORDER as readonly string[]).includes(k) && v.length);
  return [...known, ...extra];
}

/** A short cell label for a table: the value, or the missing reason (never zero). */
export function cellText(c: Pick<EuCell, 'value' | 'missing_reason'>): string {
  return c.value ?? c.missing_reason ?? 'no value';
}

/** The CIK typed for the pairing, or an error. */
export function checkCik(text: string): { ok: true; value: string } | { ok: false; error: string } {
  const t = text.trim().toUpperCase().replace(/^CIK/, '');
  if (!t) return { ok: true, value: '' };
  if (!/^\d{1,10}$/.test(t) || Number(t) <= 0) return { ok: false, error: 'SEC pairing must be a CIK (digits)' };
  return { ok: true, value: String(Number(t)) };
}
