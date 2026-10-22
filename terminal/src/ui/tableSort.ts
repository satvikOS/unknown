/** Sorting for DataTable: exact for decimal strings, stable, empty values always last. */
import { compareDecimal, isDecimal } from '../format/decimal';

export type SortKind = 'text' | 'number' | 'decimal';
export type SortDirection = 'ascending' | 'descending';
export type SortValue = string | number | boolean | null | undefined;

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

function isEmpty(v: SortValue): boolean {
  return v === null || v === undefined || v === '';
}

/** Compare two non-empty values ascending. Malformed decimals sort after valid ones. */
export function compareValues(a: SortValue, b: SortValue, kind: SortKind): number {
  if (kind === 'decimal') {
    const as = String(a);
    const bs = String(b);
    const av = isDecimal(as);
    const bv = isDecimal(bs);
    if (av && bv) return compareDecimal(as, bs);
    if (av !== bv) return av ? -1 : 1;
    return collator.compare(as, bs);
  }
  if (kind === 'number') {
    const an = typeof a === 'number' ? a : Number(a);
    const bn = typeof b === 'number' ? b : Number(b);
    return an === bn ? 0 : an < bn ? -1 : 1;
  }
  if (typeof a === 'boolean' || typeof b === 'boolean') return Number(Boolean(a)) - Number(Boolean(b));
  return collator.compare(String(a), String(b));
}

/** Stable sort; empty values stay at the end in both directions. */
export function sortRows<T>(
  rows: readonly T[],
  getValue: (row: T) => SortValue,
  kind: SortKind,
  direction: SortDirection,
): T[] {
  const dir = direction === 'ascending' ? 1 : -1;
  return rows
    .map((row, index) => ({ row, index, value: getValue(row) }))
    .sort((x, y) => {
      const xe = isEmpty(x.value);
      const ye = isEmpty(y.value);
      if (xe || ye) return xe === ye ? x.index - y.index : xe ? 1 : -1;
      const c = compareValues(x.value, y.value, kind) * dir;
      return c !== 0 ? c : x.index - y.index;
    })
    .map((x) => x.row);
}

/** Visible window for virtualization (inclusive start, exclusive end). */
export function visibleRange(
  total: number,
  scrollTop: number,
  viewport: number,
  rowHeight: number,
  overscan = 8,
): { start: number; end: number } {
  if (total <= 0 || rowHeight <= 0) return { start: 0, end: 0 };
  const first = Math.min(Math.floor(Math.max(0, scrollTop) / rowHeight), total - 1);
  const count = Math.ceil(Math.max(viewport, rowHeight) / rowHeight);
  const start = Math.max(0, first - overscan);
  const end = Math.min(total, first + count + overscan);
  return { start, end };
}
