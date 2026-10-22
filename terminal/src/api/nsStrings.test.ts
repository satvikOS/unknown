import { describe, expect, it } from 'vitest';
import { isNsStringSibling, withoutNsStrings } from './nsStrings';

describe('*_ns_str siblings (server.py: every *_ns integer or null also comes as an exact *_ns_str)', () => {
  it('recognises a sibling only beside its *_ns partner', () => {
    expect(isNsStringSibling('at_ns_str', { at_ns: 1, at_ns_str: '1' })).toBe(true);
    expect(isNsStringSibling('at_ns_str', { at_ns_str: '1' })).toBe(false);
    expect(isNsStringSibling('at_ns', { at_ns: 1 })).toBe(false);
    expect(isNsStringSibling('label_str', { label: 'x', label_str: 'y' })).toBe(false);
  });

  it('strips siblings at every depth, as recorded, without touching the input', () => {
    const recorded = { at_ns: 5, at_ns_str: '5', nested: [{ t_ns: null, t_ns_str: null, keep_str: 'k' }], orphan_ns_str: 'o' };
    expect(withoutNsStrings(recorded)).toEqual({ at_ns: 5, nested: [{ t_ns: null, keep_str: 'k' }], orphan_ns_str: 'o' });
    expect(recorded.at_ns_str).toBe('5');
    expect(withoutNsStrings('x')).toBe('x');
    expect(withoutNsStrings(null)).toBe(null);
  });
});
