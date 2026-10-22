/**
 * Normalizes GET /api/capabilities. The API returns docs/capabilities/register.json verbatim, or
 * `{"capabilities": [], "note": ...}` when it is not published.
 *
 * Register schema_version 1 (written by the parity workstream): top-level `description`,
 * `status_vocabulary`, `areas` (per-area source verification) and `capabilities`, where each entry
 * has `id`, `area`, `title`, `priority`, `reference_scope[]`, `source_verification`, and
 * `implementation: {status, evidence, gaps, requirements}` among other fields. Unknown shapes fall
 * back to common field names so nothing is hidden.
 */
import type { CapabilitiesResponse } from '../../api/types';

export interface CapabilityRow {
  key: string;
  id: string;
  title: string;
  area: string | null;
  priority: string | null;
  /** Implementation status in the ADR-0001 vocabulary (proposed, specified, implemented, tested, ...). */
  status: string | null;
  verifiedOn: string | null;
  /** Reference products named in reference_scope (deduplicated). */
  products: string[];
  gaps: string[];
  evidence: string[];
  /** The full entry, for the detail view. */
  raw: Record<string, unknown>;
}

export interface AreaVerification {
  area: string;
  verifiedOn: string | null;
  urlsChecked: number | null;
  urlsSupported: number | null;
}

export interface NormalizedRegister {
  published: boolean;
  note: string | null;
  description: string | null;
  schemaVersion: number | null;
  statusVocabulary: string[];
  areas: AreaVerification[];
  rows: CapabilityRow[];
}

const ID_KEYS = ['id', 'capability_id', 'key', 'code'];
const TITLE_KEYS = ['title', 'name', 'capability', 'summary'];
const AREA_KEYS = ['area', 'category', 'domain', 'group'];
const STATUS_KEYS = ['status', 'register_status', 'state'];
const DATE_KEYS = ['verified_on', 'as_of', 'checked_on', 'date', 'updated_at'];
const LIST_KEYS = ['capabilities', 'entries', 'items', 'register'];

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function str(v: unknown): string | null {
  if (typeof v === 'string' && v !== '') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return null;
}

function first(obj: Obj, keys: string[]): string | null {
  for (const k of keys) {
    const s = str(obj[k]);
    if (s !== null) return s;
  }
  return null;
}

function strings(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => str(x) ?? (isObj(x) ? (str(x.summary) ?? str(x.title) ?? str(x.url) ?? JSON.stringify(x)) : JSON.stringify(x)));
}

function normalizeEntry(entry: unknown, i: number): CapabilityRow {
  if (!isObj(entry)) {
    const t = str(entry) ?? JSON.stringify(entry);
    return { key: `row-${i}`, id: String(i + 1), title: t, area: null, priority: null, status: null, verifiedOn: null, products: [], gaps: [], evidence: [], raw: { value: entry } };
  }
  const impl = isObj(entry.implementation) ? entry.implementation : null;
  const verification = isObj(entry.source_verification) ? entry.source_verification : null;
  const scope = Array.isArray(entry.reference_scope) ? entry.reference_scope : [];
  const products = [...new Set(scope.map((s) => (isObj(s) ? str(s.product) : null)).filter((p): p is string => p !== null))];
  const id = first(entry, ID_KEYS) ?? String(i + 1);
  return {
    key: `${id}-${i}`,
    id,
    title: first(entry, TITLE_KEYS) ?? '',
    area: first(entry, AREA_KEYS),
    priority: str(entry.priority),
    status: (impl && str(impl.status)) ?? first(entry, STATUS_KEYS),
    verifiedOn: (verification && str(verification.verified_on)) ?? first(entry, DATE_KEYS),
    products,
    gaps: impl ? strings(impl.gaps) : [],
    evidence: impl ? strings(impl.evidence) : [],
    raw: entry,
  };
}

export function normalizeRegister(data: CapabilitiesResponse | unknown[] | null | undefined): NormalizedRegister {
  const empty: NormalizedRegister = { published: false, note: null, description: null, schemaVersion: null, statusVocabulary: [], areas: [], rows: [] };
  if (!data) return empty;
  if (Array.isArray(data)) {
    const rows = data.map(normalizeEntry);
    return { ...empty, published: rows.length > 0, rows };
  }
  const listKey = LIST_KEYS.find((k) => Array.isArray(data[k]));
  const list = listKey ? (data[listKey] as unknown[]) : [];
  const rows = list.map(normalizeEntry);
  const areas: AreaVerification[] = isObj(data.areas)
    ? Object.entries(data.areas).map(([area, v]) => {
        const ver = isObj(v) && isObj(v.verification) ? v.verification : null;
        return {
          area,
          verifiedOn: ver ? str(ver.verified_on) : null,
          urlsChecked: ver && typeof ver.urls_checked === 'number' ? ver.urls_checked : null,
          urlsSupported:
            ver && typeof ver.urls_supported_after_correction === 'number'
              ? ver.urls_supported_after_correction
              : ver && typeof ver.urls_supported === 'number'
                ? ver.urls_supported
                : null,
        };
      })
    : [];
  return {
    published: rows.length > 0,
    note: typeof data.note === 'string' ? data.note : null,
    description: typeof data.description === 'string' ? data.description : null,
    schemaVersion: typeof data.schema_version === 'number' ? data.schema_version : null,
    statusVocabulary: Array.isArray(data.status_vocabulary) ? data.status_vocabulary.filter((s): s is string => typeof s === 'string') : [],
    areas,
    rows,
  };
}

/** Count rows per value of `pick`, in descending count order (null counted as "unstated"). */
export function countBy(rows: readonly CapabilityRow[], pick: (r: CapabilityRow) => string | null): [string, number][] {
  const m = new Map<string, number>();
  for (const r of rows) {
    const k = pick(r) ?? 'unstated';
    m.set(k, (m.get(k) ?? 0) + 1);
  }
  return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
