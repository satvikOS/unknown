/**
 * Equity screener (CAP-MKT-10): wire types of /api/screener and the pure builder logic. Criterion values are exact
 * decimal strings sent as typed; ratios are fractions (0.25 = 25 %). Nothing here computes a screen.
 */
import { formatDecimal, isDecimal } from '../../format/decimal';
import { amount2, fractionPct, roundDecimal } from '../ownership/model';

export type Op = 'gt' | 'gte' | 'lt' | 'lte' | 'between';
export type Treatment = 'exclude' | 'include';
export type Unit = 'ratio' | 'x' | 'money' | 'price';

export interface FieldInfo {
  field: string;
  label: string;
  unit: Unit;
  sources: string[];
  kind: string;
  formula: string;
  basis: string;
  nm_rule: string | null;
  needs_price: boolean;
}

export interface FieldsResponse {
  fields: FieldInfo[];
  operators: string[];
  treatments: string[];
  units: Record<string, string>;
}

export interface Criterion {
  field: string;
  op: Op;
  value: string;
  value2?: string;
  currency?: string;
  missing: Treatment;
  nm: Treatment;
}

export interface Definition {
  universe: { sources: string[]; sic_from?: number; sic_to?: number; entities?: string[] };
  criteria: Criterion[];
  sort: { field: string; direction: 'asc' | 'desc' } | null;
  limit: number;
  columns: string[];
}

export interface FieldValue {
  value: string | null;
  status: string;
  unit: string | null;
  currency: string | null;
  formula: string | null;
  period: string | null;
  input_obs_ids: number[];
  flags: string[];
  reason?: string;
  detail?: string | null;
}

export interface Check {
  field: string;
  value: string | null;
  status: string;
  result: 'PASS' | 'FAIL' | 'UNKNOWN' | 'EXCLUDED';
  reason?: string;
  detail?: string | null;
  treatment?: string;
}

export interface Identity {
  entity_id: string;
  name: string | null;
  source: 'sec' | 'esef';
  cik: number | null;
  lei: string | null;
  ticker: string | null;
  exchange: string | null;
  sic: string | null;
  country: string | null;
}

export interface ResultRow extends Identity {
  outcome: 'PASS' | 'UNKNOWN';
  has_data_at_as_of: boolean;
  checks: Check[];
  values: Record<string, FieldValue>;
  price: { value: string; currency: string; source: string; observed_at: string } | null;
}

export interface ExcludedRow extends Identity {
  has_data_at_as_of: boolean;
  reasons: { field: string; reason: string | null; detail: string | null }[];
}

export interface Manifest {
  engine: string;
  definition: Definition;
  definition_hash: string;
  as_of: string;
  knowledge_cutoff: string;
  universe: { count: number; by_source: Record<string, number> };
  price_inputs: Record<string, { value: string; currency: string; source: string; observed_at: string }>;
  data_versions: Record<string, { observations: number; max_obs_id: number | null; latest_available: string | null }>;
  formulas: string[];
  live_quotes?: string;
}

export interface RunResponse {
  as_of: string;
  knowledge_cutoff: string;
  results: ResultRow[];
  excluded: ExcludedRow[];
  counts: Record<string, number>;
  excluded_by_reason: Record<string, number>;
  truncated: boolean;
  columns: string[];
  manifest: Manifest;
  result_hash: string;
  coverage: { universe: number; with_data_at_as_of: number; without_data_at_as_of: number; note: string };
  computed_at: string;
  live_quotes?: string;
  run_id?: string;
  screen_id?: string | null;
  run_at?: string;
  status?: string;
  reason?: string;
  detail?: string;
}

export interface SavedScreen {
  screen_id: string;
  version: number;
  name: string;
  definition: Definition;
  definition_hash: string;
  saved_at: string;
  runs?: number | { run_id: string; as_of: string; knowledge_cutoff: string; result_hash: string; run_at: string }[];
}

export interface VerifyResponse {
  run_id: string;
  reproducible: boolean;
  stored_hash: string;
  recomputed_hash: string;
  as_of: string;
  knowledge_cutoff: string;
}

/** The builder's editable state: everything as typed. */
export interface Draft {
  sources: { sec: boolean; esef: boolean };
  sicFrom: string;
  sicTo: string;
  criteria: Criterion[];
  sortField: string;
  sortDir: 'asc' | 'desc';
  columns: string[];
  limit: number;
}

export const OPS: readonly { id: Op; label: string }[] = [
  { id: 'gte', label: '≥' },
  { id: 'gt', label: '>' },
  { id: 'lte', label: '≤' },
  { id: 'lt', label: '<' },
  { id: 'between', label: 'between' },
];

export const EMPTY_DRAFT: Draft = {
  sources: { sec: true, esef: true },
  sicFrom: '',
  sicTo: '',
  criteria: [],
  sortField: '',
  sortDir: 'desc',
  columns: [],
  limit: 200,
};

export function newCriterion(f: FieldInfo): Criterion {
  return { field: f.field, op: 'gte', value: '', missing: 'exclude', nm: 'exclude', ...(f.unit === 'money' || f.unit === 'price' ? { currency: 'USD' } : {}) };
}

/** Draft -> definition, or the first problem in words (the server validates again and says why it refuses). */
export function toDefinition(d: Draft, fields: readonly FieldInfo[]): { definition: Definition } | { error: string } {
  const sources = (['sec', 'esef'] as const).filter((s) => d.sources[s]);
  if (!sources.length) return { error: 'Choose at least one universe source (SEC or ESEF).' };
  const universe: Definition['universe'] = { sources: [...sources] };
  for (const [k, v] of [['sic_from', d.sicFrom], ['sic_to', d.sicTo]] as const) {
    if (v.trim()) {
      if (!/^\d{1,4}$/.test(v.trim())) return { error: `SIC ${k === 'sic_from' ? 'from' : 'to'} must be a 1-4 digit code.` };
      universe[k] = Number(v.trim());
    }
  }
  const byId = new Map(fields.map((f) => [f.field, f]));
  const criteria: Criterion[] = [];
  for (const [i, c] of d.criteria.entries()) {
    const f = byId.get(c.field);
    if (!f) return { error: `Criterion ${i + 1}: unknown field ${c.field}.` };
    if (!isDecimal(c.value.trim())) return { error: `Criterion ${i + 1} (${f.label}): value must be a plain decimal number.` };
    const out: Criterion = { field: c.field, op: c.op, value: c.value.trim(), missing: c.missing, nm: c.nm };
    if (c.op === 'between') {
      if (!c.value2 || !isDecimal(c.value2.trim())) return { error: `Criterion ${i + 1} (${f.label}): "between" needs an upper value.` };
      out.value2 = c.value2.trim();
    }
    if (f.unit === 'money' || f.unit === 'price') {
      const cur = (c.currency ?? '').trim().toUpperCase();
      if (!/^[A-Z]{3}$/.test(cur)) return { error: `Criterion ${i + 1} (${f.label}): name the currency (no FX conversion is applied).` };
      out.currency = cur;
    }
    criteria.push(out);
  }
  return {
    definition: {
      universe,
      criteria,
      sort: d.sortField ? { field: d.sortField, direction: d.sortDir } : null,
      limit: d.limit,
      columns: d.columns.filter((c) => byId.has(c)),
    },
  };
}

export function fromDefinition(def: Definition): Draft {
  return {
    sources: { sec: def.universe.sources.includes('sec'), esef: def.universe.sources.includes('esef') },
    sicFrom: def.universe.sic_from !== undefined ? String(def.universe.sic_from) : '',
    sicTo: def.universe.sic_to !== undefined ? String(def.universe.sic_to) : '',
    criteria: def.criteria.map((c) => ({ ...c })),
    sortField: def.sort?.field ?? '',
    sortDir: def.sort?.direction ?? 'desc',
    columns: [...def.columns],
    limit: def.limit,
  };
}

/** A value for a grid cell: ratios as percentages, multiples with "x", money with its currency. Null means "show the reason". */
export function displayValue(v: FieldValue, unit: Unit | undefined): string | null {
  if (v.value === null) return null;
  if (unit === 'ratio') return fractionPct(v.value) ?? v.value;
  if (unit === 'x') return `${formatDecimal(roundTo(v.value, 2))}x`;
  if (unit === 'money') return `${formatDecimal(roundTo(v.value, 0))}${v.currency ? ` ${v.currency}` : ''}`;
  if (unit === 'price') return `${amount2(v.value)}${v.currency ? ` ${v.currency}` : ''}`;
  return v.value;
}

function roundTo(value: string, places: number): string {
  return roundDecimal(value, places) ?? value;
}

export function outcomeTone(o: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  return o === 'PASS' ? 'ok' : o === 'UNKNOWN' ? 'warn' : o === 'FAIL' ? 'bad' : 'neutral';
}

/** `#/screener/run/<run_id>` opens a stored run. */
export function parseScreenerRoute(rest: readonly string[]): { runId: string | null } {
  return { runId: rest[0] === 'run' && rest[1] ? rest[1] : null };
}
