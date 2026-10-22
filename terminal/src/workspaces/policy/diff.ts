/**
 * Field-by-field comparison of two policy bodies. Objects are walked to their leaves; lists and
 * scalars are leaves. Lists of strings also report which items were added and removed (venues,
 * asset classes). Comparison is structural and key-order independent: the server hashes canonical
 * JSON, so key order never makes two policies different.
 */
import { fieldOrder, sectionOf } from './fields';

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type Change = 'same' | 'changed' | 'added' | 'removed';

export interface DiffRow {
  path: string;
  section: string;
  change: Change;
  /** Value in the base policy (absent for `added`). */
  a?: JsonValue;
  /** Value in the compared policy (absent for `removed`). */
  b?: JsonValue;
  listAdded?: JsonValue[];
  listRemoved?: JsonValue[];
}

function isObject(v: unknown): v is Record<string, JsonValue> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Leaves of a JSON value by dotted path. A non-object root is one leaf at path "". */
export function flatten(value: unknown, prefix = '', out: Map<string, JsonValue> = new Map()): Map<string, JsonValue> {
  if (isObject(value) && Object.keys(value).length > 0) {
    for (const [k, v] of Object.entries(value)) flatten(v, prefix ? `${prefix}.${k}` : k, out);
  } else {
    out.set(prefix, value as JsonValue);
  }
  return out;
}

/** Deep equality for JSON values; object key order does not matter, list order does. */
export function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((x, i) => jsonEqual(x, b[i]));
  }
  if (isObject(a) && isObject(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && jsonEqual(a[k], b[k]));
  }
  return false;
}

function listDelta(a: JsonValue[], b: JsonValue[]): { added: JsonValue[]; removed: JsonValue[] } {
  return {
    added: b.filter((x) => !a.some((y) => jsonEqual(x, y))),
    removed: a.filter((x) => !b.some((y) => jsonEqual(x, y))),
  };
}

/** Compare base `a` with `b`. Rows follow the documented field order, unknown fields last. */
export function diffBodies(a: unknown, b: unknown): DiffRow[] {
  const fa = flatten(a);
  const fb = flatten(b);
  const paths = [...new Set([...fa.keys(), ...fb.keys()])];
  paths.sort((x, y) => fieldOrder(x) - fieldOrder(y) || x.localeCompare(y));
  return paths.map((path): DiffRow => {
    const inA = fa.has(path);
    const inB = fb.has(path);
    const row: DiffRow = { path, section: sectionOf(path), change: 'same' };
    if (inA) row.a = fa.get(path) as JsonValue;
    if (inB) row.b = fb.get(path) as JsonValue;
    if (!inA) row.change = 'added';
    else if (!inB) row.change = 'removed';
    else if (!jsonEqual(row.a, row.b)) {
      row.change = 'changed';
      if (Array.isArray(row.a) && Array.isArray(row.b)) {
        const d = listDelta(row.a, row.b);
        row.listAdded = d.added;
        row.listRemoved = d.removed;
      }
    }
    return row;
  });
}

export interface DiffSummary {
  changed: number;
  added: number;
  removed: number;
  same: number;
}

export function summarize(rows: readonly DiffRow[]): DiffSummary {
  const s: DiffSummary = { changed: 0, added: 0, removed: 0, same: 0 };
  for (const r of rows) s[r.change] += 1;
  return s;
}

export function differences(rows: readonly DiffRow[]): DiffRow[] {
  return rows.filter((r) => r.change !== 'same');
}
