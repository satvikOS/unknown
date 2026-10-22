/**
 * Company workspace model: wire types of /api/company (backend api/routes/company.py and data/fundamentals/*),
 * route parsing, and the pure helpers the views use. Decimal values stay strings throughout.
 */
import { compareDecimal, isDecimal } from '../../format/decimal';

// ------------------------------------------------------------------ wire types

/** `_unavailable(...)`: the research store or a source is missing; `reason` names the command that fills it. */
export interface UnavailableBody {
  status: 'UNAVAILABLE';
  reason: string;
  entity_id?: string;
}

export function isUnavailable(x: unknown): x is UnavailableBody {
  return typeof x === 'object' && x !== null && (x as { status?: unknown }).status === 'UNAVAILABLE' && typeof (x as { reason?: unknown }).reason === 'string';
}

export interface SearchSource {
  source: string;
  status: string;
  reason?: string;
  matched?: number;
  rows_matched?: number;
  retrieved_at?: string;
  snapshot_id?: number | string;
}

export interface IssuerListing {
  ticker: string;
  exchange: string;
  source: string;
  retrieved_at?: string;
  valid_from?: string;
  validity: string;
}

export interface SearchIssuer {
  kind: 'issuer';
  cik: number;
  entity_id: string;
  name: string;
  listings: IssuerListing[];
  match: string;
  ingested: boolean;
  sic?: string | null;
  former_name_matches?: string[];
}

export interface SearchInstrument {
  kind: 'instrument';
  instrument_id: string;
  symbol: string;
  name: string | null;
  asset_class: string | null;
  venue_mic: string | null;
  currency: string | null;
  status: string | null;
  match: string;
  source: string;
  mappings: { provider: string; external_id: string; version: number }[];
}

export interface SearchFigi {
  kind: 'figi_listing';
  figi: string | null;
  composite_figi: string | null;
  share_class_figi: string | null;
  name: string | null;
  ticker: string | null;
  exch_code: string | null;
  security_type: string | null;
  match: string;
  mapping_status: string;
  source: string;
  retrieved_at: string;
  obs_id: number;
  status_note: string;
}

export type SearchCandidate = SearchIssuer | SearchInstrument | SearchFigi;

export interface SearchResponse {
  query: string;
  query_type: string;
  as_of: string;
  candidates: SearchCandidate[];
  total_matches: number;
  selected: null;
  sources: SearchSource[];
  note: string;
}

export interface Period {
  label: string;
  kind: string;
  start: string | null;
  end: string;
  days: number | null;
  fiscal_year: number | null;
  fiscal_quarter: number | null;
  fiscal_start: string | null;
  flags: string[];
}

export interface CellSource {
  obs_id: number;
  concept: string;
  value: string;
  unit: string;
  period_start: string | null;
  period_end: string;
  accession: string;
  form: string;
  available_time: string;
  quality: string[];
  sec_filing_index: string | null;
}

export interface CellHistory {
  obs_id: number;
  value: string;
  accession: string;
  available_time: string;
}

/** A computed cell's component in summary form (statement views). */
export interface CellComponent {
  item: string;
  period: string;
  value: string | null;
  method: string;
  obs_ids: number[];
}

export interface Cell {
  item: string;
  period: Period;
  value: string | null;
  unit: string;
  method: string;
  rule: string;
  concept: string | null;
  formula: string | null;
  missing_reason: string | null;
  detail: string | null;
  not_reported: string[];
  flags: string[];
  obs_ids: number[];
  latest_input_available_time: string | null;
  sources?: CellSource[];
  history?: CellHistory[];
  restated?: boolean;
  /** Summary components in statements; full cells in a metric's show-calculation detail. */
  components?: (CellComponent | Cell)[];
}

export interface StatementRow {
  item: string;
  label: string;
  unit: string;
  rule: string;
  note: string;
  cells: Cell[];
}

export interface Coverage {
  mapping: string;
  reported_concepts: number;
  mapped_concepts_used: number;
  unmapped_concepts: number;
  unmapped_sample: string[];
  note: string;
}

export interface StatementResponse {
  entity_id: string;
  as_of: string;
  knowledge_cutoff: string | null;
  mapping: string;
  basis: string;
  currency: string;
  currency_flags: string[];
  columns: Period[];
  statements: Record<string, StatementRow[]>;
  calendar_flags: string[];
  facts_used: number;
  coverage: Coverage;
  source: { dataset: string; provider: string; attribution: string };
}

export interface PriceInputJson {
  value: string;
  currency: string;
  source: string;
  listing: string;
  observed_at: string;
  obs_id: number | null;
  persisted: boolean;
}

export interface BridgeItem {
  component: string;
  sign: '+' | '-' | 1 | -1;
  value: string | null;
  contribution?: string;
  status: string;
  source?: string;
  obs_ids?: number[];
  period_end?: string;
  method?: string;
  accessions?: string[];
  sec_filing_index?: string;
  missing_reason?: string | null;
  detail?: string | null;
}

export interface MetricInputSummary {
  role: string;
  value: string | null;
  period: string | null;
  method: string | null;
}

export interface MetricRow {
  metric: string;
  formula: string;
  expression: string;
  nm_rule: string | null;
  basis: string;
  period: string;
  value: string | null;
  unit: string;
  /** OK | NM | MISSING | INCOMPLETE | SUPPRESSED | NO_PRICE */
  status: string;
  reason: string | null;
  flags: string[];
  input_obs_ids: number[];
  source_dates: string[];
  bridge?: BridgeItem[];
  inputs: MetricInputSummary[];
}

export interface MetricsResponse {
  entity_id: string;
  as_of: string;
  currency: string;
  mapping: string;
  latest_quarter: Period | null;
  price_input: PriceInputJson | null;
  metrics: MetricRow[];
}

export interface ExplainInput {
  role: string;
  cell?: Cell;
  price?: PriceInputJson;
  metric?: MetricExplain;
  value?: string;
}

export interface MetricExplain extends Omit<MetricRow, 'inputs'> {
  inputs: ExplainInput[];
  entity_id?: string;
  as_of?: string;
  mapping?: string;
  currency?: string;
}

export interface Identity {
  cik: number;
  entity_id: string;
  name: string;
  name_as_of: string;
  former_names: { name: string; from: string | null; to: string | null }[];
  sic: string | null;
  sic_description: string | null;
  fiscal_year_end: string | null;
  state_of_incorporation: string | null;
  entity_type: string | null;
  filer_category: string | null;
  lei: string | null;
  listings: { ticker: string; exchange: string; validity: string }[];
  source: { dataset: string; raw_sha256: string; retrieved_at: string; url: string; attribution: string };
  links: Record<string, string>;
  caveats: string[];
}

export interface Filing {
  accession: string;
  form: string;
  filing_date: string;
  report_date: string | null;
  acceptance_time_raw: string;
  items: string[];
  flags: string[];
  primary_document_description: string | null;
  is_xbrl: boolean;
  file_number: string | null;
  /** primary_document is null when the submissions file names no primary document. */
  links: { filing_index: string; primary_document: string | null };
}

export interface FilingsContent {
  cik: number;
  as_of: string;
  count: number;
  filings: Filing[];
  not_yet_accepted_at_as_of: number;
  coverage_note: string;
  acceptance_note: string;
}

export interface FilingsResponse extends FilingsContent {
  identity: Identity;
}

export interface Block<T> {
  status: string;
  source: string;
  as_of: string;
  content?: T;
  reason?: string;
  newest_input?: string;
  age_seconds?: number;
}

export interface PeerMember {
  entity_id: string;
  name: string;
  cik: number;
  sic: string | null;
}

export interface PeerGroup {
  target: PeerMember;
  as_of: string;
  rule: { id: string; description: string; sic: string | null; size_band: string };
  members: PeerMember[];
  excluded: { entity_id: string; reason: string }[];
  caveats: string[];
}

export interface CompsValue {
  value: string | null;
  status: string;
  unit: string;
  formula: string;
  period: string;
  reason: string | null;
  flags: string[];
  input_obs_ids?: number[];
  source_dates?: string[];
}

export interface CompsRow {
  entity_id: string;
  role: string;
  values: Record<string, CompsValue>;
  fundamentals: { status: string; facts_at_as_of?: number; reason?: string };
  price: PriceInputJson | null;
}

export interface PeerStat {
  n_used: number;
  n_nm: number;
  n_missing: number;
  suppressed: boolean;
  mean?: string;
  median?: string;
  p25?: string;
  p75?: string;
  method?: string;
  reason?: string;
}

export interface Comps {
  as_of: string;
  metrics: string[];
  rows: CompsRow[];
  peer_statistics: Record<string, PeerStat>;
  note: string;
}

export interface PeersResponse {
  group: PeerGroup;
  comps: Comps;
}

export interface TearsheetResponse {
  entity_id: string;
  as_of: string;
  generated_at: string;
  blocks: {
    identity: Block<Identity>;
    filings: Block<FilingsContent>;
    statements: Block<{ annual: StatementResponse; quarterly: StatementResponse; ttm: StatementResponse } | { reason: string }>;
    metrics: Block<{ price_input: PriceInputJson | null; metrics: MetricRow[]; show_calculation: string; note: string }>;
    peers: Block<PeersResponse>;
    description: Block<{ reason: string; link: string | null; accession: string }>;
    quote: Block<never>;
    events: Block<never>;
  };
  status_vocabulary: string[];
}

export interface DcfInput {
  value?: string;
  values?: string[];
  source?: string;
  currency?: string | null;
  obs_id?: number;
  available_time?: string;
  unit?: string;
  fallback?: string | null;
  convention_note?: string;
  we?: string;
  wd?: string;
  base?: MetricRow;
}

export interface DcfResult {
  model: string;
  entity_id: string;
  as_of: string;
  currency: string;
  balance_sheet_date: string;
  inputs: Record<string, DcfInput | string>;
  wacc_build_up: {
    formula: string;
    expression: string;
    cost_of_equity: string;
    after_tax_cost_of_debt: string;
    wacc: string;
    inputs: Record<string, string>;
    sources: Record<string, string | null>;
  } | null;
  valuation: {
    formula: string;
    wacc: string;
    g: string;
    T: number;
    fcff_next: string;
    projections: { t: number; fcff: string; discount_factor: string; pv: string }[];
    pv_forecast: string;
    terminal_value: string;
    pv_terminal_value: string;
    enterprise_value: string;
    terminal_share_of_ev: string;
  };
  equity_bridge: {
    equity_value: string;
    bridge: (BridgeItem & { sign: number })[];
    status: string;
    missing_components?: string[];
    per_share?: string;
    shares: { value: string | null; source: string; obs_ids: number[]; flags: string[] };
    expression: string;
  };
  sensitivity: {
    wacc_axis: string[];
    g_axis: string[];
    grid: { wacc: string; g: string; valid: boolean; enterprise_value?: string; equity_value?: string; per_share?: string; reason?: string }[][];
    measure: string;
    range: { min: string; max: string } | null;
    invalid_cells: number;
  };
  scenarios: {
    scenarios: { name: string; wacc: string; g: string; fcff_scale: string; valid: boolean; enterprise_value?: string; equity_value?: string; per_share?: string; reason?: string }[];
    range: { min: string; max: string } | null;
    note: string;
  };
  football_field: Record<string, { min?: string; max?: string; status?: string; note?: string; reason?: string } | PriceInputJson>;
  overrides: string[];
  arithmetic: string;
  disclaimer: string;
}

export interface DcfRefused {
  status: 'REFUSED';
  reason: string;
  model: string;
  entity_id: string;
  as_of: string;
}

export type DcfResponse = DcfResult | DcfRefused | UnavailableBody;

export function isRefused(x: unknown): x is DcfRefused {
  return typeof x === 'object' && x !== null && (x as { status?: unknown }).status === 'REFUSED';
}

// ------------------------------------------------------------------ route

export const COMPANY_TABS = ['tearsheet', 'statements', 'metrics', 'dcf', 'peers', 'filings'] as const;
export type CompanyTab = (typeof COMPANY_TABS)[number];

export const TAB_LABELS: Record<CompanyTab, string> = {
  tearsheet: 'Tearsheet',
  statements: 'Statements',
  metrics: 'Metrics',
  dcf: 'DCF',
  peers: 'Peers',
  filings: 'Filings',
};

/** Options carried in the route as `key=value` segments (bookmarkable). */
export const OPTION_KEYS = ['q', 'as_of', 'known_at', 'basis', 'periods', 'price', 'price_currency', 'price_source', 'price_time', 'peers', 'forms'] as const;
export type OptionKey = (typeof OPTION_KEYS)[number];
export type CompanyOptions = Partial<Record<OptionKey, string>>;

export interface CompanyRoute {
  /** null on the search view. */
  cik: number | null;
  tab: CompanyTab;
  opts: CompanyOptions;
}

/** `#/company[/<cik>[/<tab>]][/key=value...]`; a non-numeric first segment is the search view. */
export function parseCompanyRoute(rest: readonly string[]): CompanyRoute {
  let i = 0;
  let cik: number | null = null;
  let tab: CompanyTab = 'tearsheet';
  const first = rest[0];
  if (first !== undefined && /^\d{1,10}$/.test(first) && Number(first) > 0) {
    cik = Number(first);
    i = 1;
    const t = rest[1];
    if (t !== undefined && (COMPANY_TABS as readonly string[]).includes(t)) {
      tab = t as CompanyTab;
      i = 2;
    }
  } else if (first === 'search') {
    i = 1;
  }
  const opts: CompanyOptions = {};
  for (const seg of rest.slice(i)) {
    const eq = seg.indexOf('=');
    if (eq <= 0) continue;
    const k = seg.slice(0, eq);
    const v = seg.slice(eq + 1).trim();
    if (v && (OPTION_KEYS as readonly string[]).includes(k)) opts[k as OptionKey] = v;
  }
  return { cik, tab, opts };
}

/** Route segments (after the workspace id) for a view; options in a fixed order, empty ones dropped. */
export function companyRoute(r: { cik: number | null; tab?: CompanyTab; opts?: CompanyOptions }): string[] {
  const segs: string[] = r.cik === null ? ['search'] : [String(r.cik), r.tab ?? 'tearsheet'];
  for (const k of OPTION_KEYS) {
    const v = r.opts?.[k]?.trim();
    if (v) segs.push(`${k}=${v}`);
  }
  return segs.length === 1 && r.cik === null ? [] : segs;
}

/** Options that apply to every issuer view (as of and the price input); the search text and tab-local ones are dropped. */
export function carriedOptions(opts: CompanyOptions): CompanyOptions {
  const out: CompanyOptions = {};
  for (const k of ['as_of', 'price', 'price_currency', 'price_source', 'price_time'] as const) if (opts[k]) out[k] = opts[k];
  return out;
}

// ------------------------------------------------------------------ inputs

export const DECIMAL_INPUT = /^[+-]?\d+(\.\d+)?$/;

export interface PriceForm {
  price: string;
  price_currency: string;
  price_source: string;
  price_time: string;
}

/**
 * The API needs a price with its currency, source and observation time (no price is ever fetched implicitly).
 * Returns the problems to show before sending; an empty price means "no price input".
 */
export function priceProblems(p: PriceForm): string[] {
  if (!p.price.trim()) return [];
  const out: string[] = [];
  if (!DECIMAL_INPUT.test(p.price.trim())) out.push('price must be a decimal number, e.g. 50.00');
  if (!/^[A-Za-z]{3}$/.test(p.price_currency.trim())) out.push('price currency must be a 3-letter code, e.g. USD');
  if (!p.price_source.trim()) out.push('price source is required (where the price was observed)');
  if (!p.price_time.trim()) out.push('price time is required (an instant at or before as of)');
  else if (!/(Z|[+-]\d{2}:\d{2})$/.test(p.price_time.trim()) && !/^\d{4}-\d{2}-\d{2}$/.test(p.price_time.trim())) {
    out.push('price time must include a time zone, e.g. 2026-10-21T20:00:00Z');
  }
  return out;
}

export function priceParams(opts: CompanyOptions): Record<string, string | undefined> {
  if (!opts.price) return {};
  return { price: opts.price, price_currency: opts.price_currency?.toUpperCase(), price_source: opts.price_source, price_time: opts.price_time };
}

/** "9000002, 9000003" -> "9000002,9000003"; null with a message when an entry is not a CIK. */
export function parsePeerList(text: string): { ok: true; value: string } | { ok: false; error: string } {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const bad = parts.filter((p) => !/^(CIK)?\d{1,10}$/i.test(p));
  if (bad.length) return { ok: false, error: `not a CIK: ${bad.join(', ')}` };
  return { ok: true, value: parts.map((p) => p.replace(/^CIK/i, '')).join(',') };
}

export interface DcfForm {
  g: string;
  flows: 'explicit' | 'growth';
  fcff: string;
  fcff_growth: string;
  years: string;
  rate: 'wacc' | 'capm';
  wacc: string;
  rf_mode: 'treasury' | 'explicit';
  rf_tenor: string;
  rf_date: string;
  rf: string;
  rate_currency: string;
  beta: string;
  erp: string;
  crp: string;
  kd: string;
  tax: string;
  we: string;
  wd: string;
  step_wacc: string;
  step_g: string;
  allow_financial: boolean;
  allow_currency_mismatch: boolean;
}

export const TREASURY_TENORS = ['1M', '1.5M', '2M', '3M', '4M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '20Y', '30Y'] as const;

export const EMPTY_DCF: DcfForm = {
  g: '',
  flows: 'explicit',
  fcff: '',
  fcff_growth: '',
  years: '5',
  rate: 'capm',
  wacc: '',
  rf_mode: 'treasury',
  rf_tenor: '10Y',
  rf_date: '',
  rf: '',
  rate_currency: '',
  beta: '',
  erp: '',
  crp: '',
  kd: '',
  tax: '',
  we: '',
  wd: '',
  step_wacc: '',
  step_g: '',
  allow_financial: false,
  allow_currency_mismatch: false,
};

/**
 * Query parameters for GET /api/company/<cik>/dcf from the form. Only the syntax is checked here; whether the
 * inputs are sufficient and consistent (WACC > g, currencies, missing weights) is the API's decision, which it
 * returns as REFUSED with its reason.
 */
export function dcfParams(f: DcfForm): { ok: true; params: Record<string, string> } | { ok: false; errors: string[] } {
  const errors: string[] = [];
  const params: Record<string, string> = {};
  const dec = (name: string, v: string, required = false) => {
    const t = v.trim();
    if (!t) {
      if (required) errors.push(`${name} is required`);
      return;
    }
    if (!DECIMAL_INPUT.test(t)) errors.push(`${name} must be a decimal number (e.g. 0.025), got ${JSON.stringify(t)}`);
    else params[name] = t;
  };
  dec('g', f.g, true);
  if (f.flows === 'explicit') {
    const flows = f.fcff.split(/[\s,]+/).filter(Boolean);
    if (!flows.length) errors.push('fcff: give at least one explicit cash flow');
    const bad = flows.filter((x) => !DECIMAL_INPUT.test(x));
    if (bad.length) errors.push(`fcff: not decimal numbers: ${bad.join(', ')}`);
    else if (flows.length) params.fcff = flows.join(',');
  } else {
    dec('fcff_growth', f.fcff_growth, true);
    const y = f.years.trim();
    if (!/^\d+$/.test(y) || Number(y) < 1 || Number(y) > 30) errors.push('years must be a whole number from 1 to 30');
    else params.years = y;
  }
  if (f.rate === 'wacc') {
    dec('wacc', f.wacc, true);
  } else {
    if (f.rf_mode === 'treasury') {
      params.rf_tenor = f.rf_tenor;
      if (f.rf_date.trim()) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(f.rf_date.trim())) errors.push('rf date must be YYYY-MM-DD');
        else params.rf_date = f.rf_date.trim();
      }
    } else {
      dec('rf', f.rf, true);
      if (f.rate_currency.trim()) {
        if (!/^[A-Za-z]{3}$/.test(f.rate_currency.trim())) errors.push('rate currency must be a 3-letter code');
        else params.rate_currency = f.rate_currency.trim().toUpperCase();
      }
    }
    dec('beta', f.beta);
    dec('erp', f.erp);
    dec('crp', f.crp);
    dec('kd', f.kd);
    dec('tax', f.tax);
    dec('we', f.we);
    dec('wd', f.wd);
  }
  dec('step_wacc', f.step_wacc);
  dec('step_g', f.step_g);
  if (f.allow_financial) params.allow_financial = '1';
  if (f.allow_currency_mismatch) params.allow_currency_mismatch = '1';
  return errors.length ? { ok: false, errors } : { ok: true, params };
}

// ------------------------------------------------------------------ statements

export const STATEMENT_TITLES: Record<string, string> = {
  income: 'Income statement',
  balance: 'Balance sheet',
  cash_flow: 'Cash flow',
  per_share: 'Per share',
  shares: 'Shares',
};

/** Statement sections in display order: the known ones first, then any others the API adds. */
export function statementOrder(statements: Record<string, unknown>): string[] {
  const known = Object.keys(STATEMENT_TITLES).filter((k) => k in statements);
  return [...known, ...Object.keys(statements).filter((k) => !(k in STATEMENT_TITLES))];
}

/** Count of cells by method and of missing cells by reason, for the coverage summary. */
export function cellCensus(resp: StatementResponse): { methods: [string, number][]; missing: [string, number][]; total: number } {
  const methods = new Map<string, number>();
  const missing = new Map<string, number>();
  let total = 0;
  for (const rows of Object.values(resp.statements)) {
    for (const r of rows) {
      for (const c of r.cells) {
        total++;
        methods.set(c.method, (methods.get(c.method) ?? 0) + 1);
        if (c.value === null) missing.set(c.missing_reason ?? 'UNSTATED', (missing.get(c.missing_reason ?? 'UNSTATED') ?? 0) + 1);
      }
    }
  }
  const sort = (m: Map<string, number>) => [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return { methods: sort(methods), missing: sort(missing), total };
}

/** Units of a statement row for display: "USD/shares" reads as "USD per share". */
export function unitLabel(unit: string): string {
  if (/^[A-Z]{3}\/shares$/.test(unit)) return `${unit.slice(0, 3)} per share`;
  return unit;
}

/** True when a component is a full cell (show-calculation detail) rather than a summary. */
export function isFullCell(c: CellComponent | Cell): c is Cell {
  return typeof (c as Cell).period === 'object' && (c as Cell).period !== null;
}

/** Exact equality of two decimal strings ("0.020" equals "0.02"); false for anything that is not a decimal. */
export function sameDecimal(a: string | null | undefined, b: string | null | undefined): boolean {
  return isDecimal(a) && isDecimal(b) && compareDecimal(a, b) === 0;
}
