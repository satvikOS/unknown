/**
 * Chart annotations (horizontal lines and notes) saved in this browser per security. Storage can be missing or
 * throw (private windows, blocked site data): every access is guarded, and a failure only loses persistence.
 */
export interface Annotation {
  id: string;
  kind: 'hline' | 'note';
  /** Exact decimal price for a horizontal line; the bar's close for a note. */
  price: string;
  /** UTC seconds of the bar a note is attached to (null for a horizontal line). */
  time: number | null;
  text: string;
  createdMs: number;
}

export interface KeyValueStore {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

const PREFIX = 'unknown.chart.annotations.v1.';
const MAX = 200;

export function browserStore(): KeyValueStore | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function valid(a: unknown): a is Annotation {
  if (!a || typeof a !== 'object') return false;
  const r = a as Record<string, unknown>;
  return (
    typeof r.id === 'string' &&
    (r.kind === 'hline' || r.kind === 'note') &&
    typeof r.price === 'string' &&
    /^-?\d+(\.\d+)?$/.test(r.price) &&
    (r.time === null || typeof r.time === 'number') &&
    typeof r.text === 'string' &&
    typeof r.createdMs === 'number'
  );
}

export function loadAnnotations(key: string, store: KeyValueStore | null = browserStore()): Annotation[] {
  if (!store || !key) return [];
  try {
    const parsed: unknown = JSON.parse(store.getItem(PREFIX + key) ?? '[]');
    return Array.isArray(parsed) ? parsed.filter(valid).slice(0, MAX) : [];
  } catch {
    return [];
  }
}

/** Returns false when the browser refused to store (the annotations stay for this session only). */
export function saveAnnotations(key: string, list: readonly Annotation[], store: KeyValueStore | null = browserStore()): boolean {
  if (!store || !key) return false;
  try {
    store.setItem(PREFIX + key, JSON.stringify(list.slice(0, MAX)));
    return true;
  } catch {
    return false;
  }
}

let counter = 0;
export function newAnnotation(kind: Annotation['kind'], price: string, time: number | null, text: string, now = Date.now()): Annotation {
  counter += 1;
  return { id: `${now.toString(36)}-${counter}`, kind, price, time, text: text.slice(0, 200), createdMs: now };
}
