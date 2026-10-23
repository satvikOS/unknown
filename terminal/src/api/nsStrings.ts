/**
 * The API adds an exact decimal-string sibling `<key>_str` beside every `<key>` ending in `_ns` that holds an
 * integer or null, in every JSON response, including inside recorded payloads such as event `info` and
 * exception `detail` (server.py, queries.with_ns_strings). The event never recorded those siblings, so views
 * that show recorded data as recorded leave them out, and print each `*_ns` value exactly from its sibling
 * (`exactJson`, `exactNs`) instead of the float JSON.parse made of it.
 */

/** True when `key` is the API's `*_ns_str` sibling of a `*_ns` key present in the same record. */
export function isNsStringSibling(key: string, record: Record<string, unknown>): boolean {
  return key.endsWith('_ns_str') && Object.prototype.hasOwnProperty.call(record, key.slice(0, -'_str'.length));
}

const INTEGER = /^-?\d+$/;

/**
 * The exact decimal string of `record[key]` when it is a `*_ns` number whose `*_ns_str` sibling is an integer
 * string naming the same value (JSON.parse rounds integers beyond 2^53; the sibling never is). Null otherwise.
 */
export function exactNs(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  const text = record[`${key}_str`];
  if (!key.endsWith('_ns') || typeof value !== 'number' || typeof text !== 'string' || !INTEGER.test(text)) return null;
  return Number(text) === value ? text : null; // a sibling that names another value is not trusted
}

/**
 * `exactNs` for a typed API record, which need not declare the sibling: the exact string of `record[key]`, or
 * null (no record, no sibling, or a sibling naming another value). For `<Timestamp nsStr>`:
 *   <Timestamp ns={o.accepted_at_ns} nsStr={nsStrOf(o, 'accepted_at_ns')} />
 */
export function nsStrOf<T extends object>(record: T | null | undefined, key: Extract<keyof T, string>): string | null {
  return record ? exactNs(record as unknown as Record<string, unknown>, key) : null;
}

/**
 * `JSON.stringify(value, null, indent)` for recorded data, with every `*_ns` integer written exactly from its
 * `*_ns_str` sibling and the siblings themselves left out (the event never recorded them). Where no exact
 * sibling exists the number is written as JSON.parse left it.
 */
export function exactJson(value: unknown, indent = 0): string {
  const pad = (depth: number) => (indent ? `\n${' '.repeat(indent * depth)}` : '');
  const colon = indent ? ': ' : ':';
  const walk = (v: unknown, depth: number): string | undefined => {
    if (Array.isArray(v)) {
      if (!v.length) return '[]';
      return `[${v.map((x) => pad(depth + 1) + (walk(x, depth + 1) ?? 'null')).join(',')}${pad(depth)}]`;
    }
    if (v !== null && typeof v === 'object') {
      const record = v as Record<string, unknown>;
      const parts: string[] = [];
      for (const [k, x] of Object.entries(record)) {
        if (isNsStringSibling(k, record)) continue;
        const text = exactNs(record, k) ?? walk(x, depth + 1);
        if (text !== undefined) parts.push(`${pad(depth + 1)}${JSON.stringify(k)}${colon}${text}`);
      }
      return parts.length ? `{${parts.join(',')}${pad(depth)}}` : '{}';
    }
    return JSON.stringify(v) as string | undefined;
  };
  return walk(value, 0) ?? '';
}

/** A copy of `value` without the API's `*_ns_str` siblings, at every depth (for showing recorded data). */
export function withoutNsStrings<T>(value: T): T {
  if (Array.isArray(value)) return value.map((v: unknown) => withoutNsStrings(v)) as T;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(record)) {
      if (!isNsStringSibling(k, record)) out[k] = withoutNsStrings(v);
    }
    return out as T;
  }
  return value;
}
