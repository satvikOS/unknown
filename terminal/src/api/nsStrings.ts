/**
 * The API adds an exact decimal-string sibling `<key>_str` beside every `<key>` ending in `_ns` that holds an
 * integer or null, in every JSON response, including inside recorded payloads such as event `info` and
 * exception `detail` (server.py, queries.with_ns_strings). The event never recorded those siblings, so views
 * that show recorded data as recorded leave them out; read the sibling only where the exact value matters.
 */

/** True when `key` is the API's `*_ns_str` sibling of a `*_ns` key present in the same record. */
export function isNsStringSibling(key: string, record: Record<string, unknown>): boolean {
  return key.endsWith('_ns_str') && Object.prototype.hasOwnProperty.call(record, key.slice(0, -'_str'.length));
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
