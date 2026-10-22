/**
 * Research workspace model: wire types of /api/research (backend api/routes/research.py over
 * research/registry.py and promotion.py), the route, and the pure helpers the views use.
 *
 * Every response is `{available, data, provenance}`; when the registry does not exist `available` is false and
 * `unavailable_reason` names the command that creates it. Statistics in trial metrics are floats by design (the
 * research engine says so); money inside them (P&L, fees, NAV) is a decimal string in the manifest's currency.
 */

export interface Provenance {
  source: string;
  path?: string;
  schema_version?: string | null;
  read_at_ns?: number;
  fixture?: boolean | null;
  /** "FIXTURE" for anything derived from a fixture experiment; "UNKNOWN_ORIGIN" when it cannot be told. */
  label?: string | null;
  note?: string;
  formulas?: string[];
  units?: Record<string, string>;
  fixture_data_allowed?: boolean;
}

export interface Envelope<T> {
  available: boolean;
  data: T | null;
  unavailable_reason?: string;
  provenance: Provenance;
}

export interface ResearchIndex {
  counts: Record<string, number>;
  endpoints: string[];
}

export interface Formula {
  formula_id: string;
  name: string;
  book: string;
  expression: string;
  units: string;
}

export interface StrategyRow {
  spec_hash: string;
  strategy_id: string;
  version: number;
  family: string;
  registered_at_ns: number;
  registered_by: string;
  registered_role: string;
}

export interface StrategyDetail {
  spec_hash: string;
  body: Record<string, unknown>;
  hash_verified: boolean;
}

export interface ExperimentRow {
  experiment_id: string;
  spec_hash: string;
  prereg_hash: string;
  created_at_ns: number;
  created_by: string;
  created_role: string;
  budget: number;
  holdout_start: string;
  holdout_end: string;
  fixture: boolean;
  trial_counts: Record<string, number>;
  budget_current: number;
  holdout_consumed: boolean;
}

export interface HoldoutAccess {
  access_id: number;
  experiment_id: string;
  artifact_hash: string | null;
  outcome: string;
  trial_id: string | null;
  principal: string;
  role: string;
  at_ns: number;
  reason: string;
}

export interface ExperimentDetail {
  experiment_id: string;
  prereg: Record<string, unknown>;
  prereg_hash: string;
  prereg_hash_verified: boolean;
  amendments: Record<string, unknown>[];
  search_space: Record<string, unknown[]>;
  budget: number;
  trial_counts: Record<string, number>;
  /** Fraction of the DEVELOPMENT budget used (a float). */
  budget_consumption: number;
  holdout_log: HoldoutAccess[];
}

export interface FloatWithConvention {
  value: number | null;
  convention?: string;
  K?: number;
}

export interface TrialCosts {
  all_in_costs?: string;
  all_in_note?: string;
  all_in_pnl?: string;
  commission?: string;
  gross_pnl?: string;
  net_pnl?: string;
  price_cost?: string;
  third_party?: string;
  definitions?: Record<string, string>;
}

export type TrialMetrics = Record<string, unknown> & {
  sharpe_per_period?: number | null;
  total_return?: number | null;
  max_drawdown?: number | null;
  n?: number;
  mean?: number | null;
  sd?: number | null;
  final_nav?: string;
  costs?: TrialCosts;
  annualized_return?: FloatWithConvention;
  sharpe_annualized?: FloatWithConvention;
  formulas?: string[];
};

export interface Trial {
  trial_id: string;
  experiment_id: string;
  seq: number;
  segment: string;
  strategy_id: string;
  spec_hash: string;
  params: Record<string, string>;
  params_hash: string;
  manifest_hash: string | null;
  label: string | null;
  author: string;
  author_role: string;
  started_at_ns: number;
  status: string;
  finished_at_ns: number | null;
  results_hash: string | null;
  metrics: TrialMetrics | null;
  returns_hash: string | null;
  error: string | null;
}

export interface TrialDetail extends Trial {
  /** [session date, simple return] pairs (float statistics). */
  returns: [string, number][] | null;
  manifest: Record<string, unknown> | null;
}

export interface ReportRow {
  report_id: string;
  experiment_id: string | null;
  kind: string;
  body_hash: string;
  created_at_ns: number;
  fixture: number | boolean;
}

export interface ReportDetail extends ReportRow {
  body: Record<string, unknown>;
}

export interface ArtifactRow {
  artifact_hash: string;
  spec_hash: string;
  strategy_id: string;
  author: string;
  author_role: string;
  registered_at_ns: number;
  status: string;
}

export interface Transition {
  seq: number;
  artifact_hash: string;
  from_status: string | null;
  to_status: string;
  at_ns: number;
  principal: string;
  role: string;
  evidence_ids: string[];
  approval: unknown;
  slot: string | null;
  incumbent_hash: string | null;
  reason: string | null;
  prev_record_hash: string;
  record_hash: string;
}

export interface Evidence {
  evidence_id: string;
  artifact_hash: string;
  kind: string;
  payload_hash: string;
  refs: { id: string; type: string }[];
  protocol_hash: string | null;
  evaluation_started_at_ns: number | null;
  passed: boolean;
  created_at_ns: number;
  created_by: string;
  created_role: string;
  payload: Record<string, unknown>;
  gate_reasons: string[];
}

export interface ArtifactDetail extends ArtifactRow {
  body: Record<string, unknown>;
  history: Transition[];
  evidence: Evidence[];
  chain_problems: unknown[];
}

// ------------------------------------------------------------------ vocabularies

/** research/registry.py SEGMENTS, in protocol order. */
export const SEGMENTS = ['DEVELOPMENT', 'WALK_FORWARD', 'SENSITIVITY', 'STRESS', 'BASELINE', 'REPLAY', 'HOLDOUT'] as const;

/** research/promotion.py STATUSES, in lifecycle order. */
export const ARTIFACT_STATUSES = ['proposed', 'reproducible', 'validated', 'paper', 'eligible', 'active', 'quarantined', 'retired'] as const;

export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral';

export function artifactTone(status: string): Tone {
  switch (status) {
    case 'active':
    case 'eligible':
      return 'ok';
    case 'validated':
    case 'paper':
      return 'info';
    case 'quarantined':
      return 'bad';
    case 'retired':
      return 'neutral';
    default:
      return 'warn';
  }
}

// ------------------------------------------------------------------ route

export const RESEARCH_TABS = ['experiments', 'artifacts', 'strategies', 'reports', 'formulas'] as const;
export type ResearchTab = (typeof RESEARCH_TABS)[number];

export const RESEARCH_TAB_LABELS: Record<ResearchTab, string> = {
  experiments: 'Experiments',
  artifacts: 'Artifacts',
  strategies: 'Strategies',
  reports: 'Reports',
  formulas: 'Formulas',
};

export type ResearchView =
  | { tab: 'experiments'; id: null }
  | { tab: 'experiments'; id: string; segment: string | null }
  | { tab: 'experiments'; trial: string }
  | { tab: Exclude<ResearchTab, 'experiments'>; id: string | null };

/**
 * `#/research` (experiments), `#/research/experiments/<id>[/segment=…]`, `#/research/trials/<trial_id>`,
 * `#/research/<artifacts|strategies|reports>[/<id>]`, `#/research/formulas`.
 */
export function parseResearchRoute(rest: readonly string[]): ResearchView {
  const [head, id, ...more] = rest;
  if (head === 'trials' && id) return { tab: 'experiments', trial: id };
  if (head === 'artifacts' || head === 'strategies' || head === 'reports' || head === 'formulas') {
    return { tab: head, id: head === 'formulas' ? null : (id ?? null) };
  }
  if (head === 'experiments' && id) {
    const seg = more.find((s) => s.startsWith('segment='))?.slice('segment='.length) ?? null;
    return { tab: 'experiments', id, segment: seg && (SEGMENTS as readonly string[]).includes(seg) ? seg : null };
  }
  return { tab: 'experiments', id: null };
}

export function isTrialView(v: ResearchView): v is { tab: 'experiments'; trial: string } {
  return 'trial' in v;
}

// ------------------------------------------------------------------ helpers

/** The label to show for provenance: FIXTURE, UNKNOWN ORIGIN, or nothing (real data). */
export function provenanceLabel(p: Provenance | null | undefined): 'FIXTURE' | 'UNKNOWN ORIGIN' | null {
  if (!p) return null;
  if (p.label === 'FIXTURE' || p.fixture === true) return 'FIXTURE';
  if (p.label === 'UNKNOWN_ORIGIN') return 'UNKNOWN ORIGIN';
  return null;
}

export function truthy(v: number | boolean | null | undefined): boolean {
  return v === true || (typeof v === 'number' && v !== 0);
}

/** Trial counts in protocol order (every segment, zeros included) and their total (M counts every status). */
export function segmentCounts(counts: Record<string, number>): { segment: string; n: number }[] {
  const known = SEGMENTS.map((s) => ({ segment: s as string, n: counts[s] ?? 0 }));
  const extra = Object.keys(counts)
    .filter((s) => !(SEGMENTS as readonly string[]).includes(s))
    .map((s) => ({ segment: s, n: counts[s] ?? 0 }));
  return [...known, ...extra];
}

export function totalTrials(counts: Record<string, number>): number {
  return Object.values(counts).reduce((a, b) => a + b, 0);
}

/** The currency of money in a trial's metrics: the manifest's config currency, if it states one. */
export function manifestCurrency(manifest: Record<string, unknown> | null | undefined): string | null {
  const cfg = manifest?.config;
  if (cfg && typeof cfg === 'object') {
    const c = (cfg as Record<string, unknown>).currency;
    if (typeof c === 'string' && /^[A-Z]{3}$/.test(c)) return c;
  }
  return null;
}

/** Statistic fields of trial metrics shown in tables, with their units (fractions are per session unless said). */
export const STAT_FIELDS: readonly { key: string; label: string; kind: 'fraction' | 'number' | 'count'; unit: string }[] = [
  { key: 'sharpe_per_period', label: 'Sharpe per session', kind: 'number', unit: 'per session' },
  { key: 'total_return', label: 'Total return', kind: 'fraction', unit: 'of initial cash' },
  { key: 'max_drawdown', label: 'Max drawdown', kind: 'fraction', unit: 'of peak' },
  { key: 'mean', label: 'Mean return', kind: 'number', unit: 'fraction per session' },
  { key: 'sd', label: 'Std. dev.', kind: 'number', unit: 'fraction per session' },
  { key: 'expected_shortfall_5pct', label: 'ES 5 %', kind: 'number', unit: 'fraction per session' },
  { key: 'hit_rate', label: 'Hit rate', kind: 'fraction', unit: 'of sessions' },
  { key: 'time_in_market', label: 'Time in market', kind: 'fraction', unit: 'of sessions' },
  { key: 'exposure_mean', label: 'Mean exposure', kind: 'fraction', unit: 'of NAV' },
  { key: 'turnover_mean', label: 'Mean turnover', kind: 'number', unit: 'fraction per session' },
  { key: 'skewness', label: 'Skewness', kind: 'number', unit: '' },
  { key: 'kurtosis_non_excess', label: 'Kurtosis (non-excess)', kind: 'number', unit: '' },
  { key: 'n', label: 'Sessions', kind: 'count', unit: 'returns' },
  { key: 'orders', label: 'Orders', kind: 'count', unit: '' },
  { key: 'fills', label: 'Fills', kind: 'count', unit: '' },
  { key: 'rejected_orders', label: 'Rejected orders', kind: 'count', unit: '' },
];

export function numberField(m: Record<string, unknown> | null | undefined, key: string): number | null {
  const v = m?.[key];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Money fields in trial metrics (decimal strings). */
export const MONEY_FIELDS: readonly { key: string; label: string }[] = [
  { key: 'final_nav', label: 'Final NAV' },
  { key: 'net_pnl', label: 'Net P&L' },
  { key: 'gross_pnl', label: 'Gross P&L' },
  { key: 'commission', label: 'Commissions' },
  { key: 'third_party', label: 'Third-party fees' },
  { key: 'price_cost', label: 'Price costs (spread, slippage, impact)' },
  { key: 'all_in_pnl', label: 'All-in P&L' },
  { key: 'all_in_costs', label: 'All-in costs' },
];

export function moneyField(m: TrialMetrics | null | undefined, key: string): string | null {
  if (!m) return null;
  if (key === 'final_nav') return typeof m.final_nav === 'string' ? m.final_nav : null;
  const v = m.costs?.[key as keyof TrialCosts];
  return typeof v === 'string' ? v : null;
}

function isStatDict(v: unknown): v is TrialMetrics {
  return typeof v === 'object' && v !== null && !Array.isArray(v) && ('sharpe_per_period' in v || 'total_return' in v || 'n' in v);
}

const BLOCK_ORDER = ['holdout', 'oos', 'full_run'];

/**
 * The statistic blocks inside a trial's metrics. Most trials store one flat block; a HOLDOUT trial stores
 * `holdout` and `full_run`, a WALK_FORWARD trial `oos` (plus `folds`), a holdout baseline `holdout`.
 * The first block is the one a summary row shows; its key says which it is (null for a flat block).
 */
export function statBlocks(m: TrialMetrics | null | undefined): { key: string | null; m: TrialMetrics }[] {
  if (!m) return [];
  if (isStatDict(m)) return [{ key: null, m }];
  const keys = Object.keys(m).filter((k) => isStatDict(m[k]));
  keys.sort((a, b) => {
    const ia = BLOCK_ORDER.indexOf(a);
    const ib = BLOCK_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  return keys.map((k) => ({ key: k, m: m[k] as TrialMetrics }));
}

/** Evidence verdicts: passed count, failed kinds (sorted), for a one-line gate summary. */
export function gateSummary(evidence: readonly Pick<Evidence, 'kind' | 'passed'>[]): { passed: number; failed: string[] } {
  const failed = evidence.filter((e) => !e.passed).map((e) => e.kind);
  return { passed: evidence.length - failed.length, failed: [...new Set(failed)].sort() };
}

// ------------------------------------------------------------------ study reports

/** The statistics a fixture-study result records (research/study.py), each with the reason when it is not computable. */
export interface StudyStats {
  dsr?: { dsr?: number | null; M?: number; T?: number; reason?: string } | null;
  pbo?: { pbo?: number | null; S?: number; N?: number; reason?: string } | null;
  n_eff?: { gate?: number | null; n?: number; reason?: string } | null;
  paired_bootstrap_oos?: {
    computable?: boolean;
    reason?: string;
    n?: number;
    conservative_lower?: number;
    point?: number;
    hurdle?: number;
    passes?: boolean;
    confidence?: number;
  } | null;
}

export interface StatLine {
  label: string;
  text: string;
  /** PASS / FAIL for the gate statistic; NOT COMPUTABLE when the engine could not compute it. */
  verdict?: 'PASS' | 'FAIL' | 'NOT COMPUTABLE';
}

/**
 * One line per recorded statistic. A statistic the engine could not compute (too few observations, zero variance)
 * shows the engine's reason instead of a blank value, and the bootstrap is NOT COMPUTABLE rather than FAIL.
 */
export function studyStatLines(res: StudyStats, fmt: (x: number | null | undefined) => string): StatLine[] {
  const out: StatLine[] = [];
  const d = res.dsr;
  if (d) {
    const v = typeof d.dsr === 'number' ? fmt(d.dsr) : (d.reason ?? 'not computed (no reason recorded)');
    out.push({ label: 'DSR (dsr@1)', text: `${v} (M = ${d.M ?? '?'}, T = ${d.T ?? '?'})` });
  }
  const p = res.pbo;
  if (p) {
    const v = typeof p.pbo === 'number' ? fmt(p.pbo) : (p.reason ?? 'not computed (no reason recorded)');
    out.push({ label: 'PBO (pbo.cscv@1)', text: `${v} (S = ${p.S ?? '?'}, N = ${p.N ?? '?'})` });
  }
  const n = res.n_eff;
  if (n) {
    const v = typeof n.gate === 'number' ? fmt(n.gate) : (n.reason ?? 'not computed (no reason recorded)');
    out.push({ label: 'n_eff (eq4.2@1, gate form)', text: `${v} of n = ${n.n ?? '?'}` });
  }
  const b = res.paired_bootstrap_oos;
  if (b) {
    if (b.computable === false) {
      out.push({ label: 'Paired bootstrap (OOS)', text: `${b.reason ?? 'not computable'} (n = ${b.n ?? '?'})`, verdict: 'NOT COMPUTABLE' });
    } else {
      out.push({
        label: 'Paired bootstrap (OOS)',
        text: `point ${fmt(b.point)}, conservative lower ${fmt(b.conservative_lower)} vs hurdle ${fmt(b.hurdle)} at ${fmt(b.confidence)} confidence`,
        verdict: b.passes ? 'PASS' : 'FAIL',
      });
    }
  }
  return out;
}
