import { describe, expect, it } from 'vitest';
import { exactJson, exactNs, isNsStringSibling, nsStrOf, withoutNsStrings } from './nsStrings';

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

  it('reads a *_ns value exactly from its sibling, and never trusts a sibling that names another value', () => {
    const r = JSON.parse('{"at_ns": 1761148672134721234, "at_ns_str": "1761148672134721234", "bad_ns": 5, "bad_ns_str": "6", "x_ns": 1, "x_ns_str": "1.5"}') as Record<string, unknown>;
    expect(r.at_ns).toBe(1761148672134721300); // what JSON.parse made of it
    expect(exactNs(r, 'at_ns')).toBe('1761148672134721234');
    expect(exactNs(r, 'bad_ns')).toBeNull();
    expect(exactNs(r, 'x_ns')).toBeNull();
    expect(exactNs(r, 'missing_ns')).toBeNull();
    expect(exactNs({ at_ns: null, at_ns_str: null }, 'at_ns')).toBeNull();
  });

  it('nsStrOf reads the sibling of a typed record that does not declare it, and tolerates an absent record', () => {
    interface Order {
      order_id: string;
      accepted_at_ns: number;
      terminal_at_ns: number | null;
    }
    const o = JSON.parse('{"order_id": "o-1", "accepted_at_ns": 1792675849903999999, "accepted_at_ns_str": "1792675849903999999", "terminal_at_ns": null, "terminal_at_ns_str": null}') as Order;
    expect(nsStrOf(o, 'accepted_at_ns')).toBe('1792675849903999999');
    expect(nsStrOf(o, 'terminal_at_ns')).toBeNull();
    expect(nsStrOf({ order_id: 'o-2', accepted_at_ns: 5, terminal_at_ns: null } as Order, 'accepted_at_ns')).toBeNull();
    expect(nsStrOf(null as Order | null, 'accepted_at_ns')).toBeNull();
    expect(nsStrOf(undefined as Order | undefined, 'accepted_at_ns')).toBeNull();
  });

  it('writes recorded JSON with every *_ns exact and without the siblings, otherwise as JSON.stringify does', () => {
    const text =
      '{"order_id": "own-1", "quote_time_ns": 1761148672134721234, "quote_time_ns_str": "1761148672134721234", "fills": [{"exec_time_ns": 1761148672999999999, "exec_time_ns_str": "1761148672999999999", "qty": "1"}], "none_ns": null, "none_ns_str": null, "empty": {}, "list": [], "flag": true, "price": "10.00"}';
    const recorded = JSON.parse(text) as Record<string, unknown>;
    const pretty = exactJson(recorded, 2);
    expect(pretty).toContain('"quote_time_ns": 1761148672134721234,');
    expect(pretty).toContain('"exec_time_ns": 1761148672999999999,');
    expect(pretty).not.toContain('_ns_str');
    expect(pretty).not.toContain('1761148672134721300');
    // Same layout as JSON.stringify(withoutNsStrings(x), null, 2), digits apart.
    const plain = JSON.stringify(withoutNsStrings(recorded), null, 2)
      .replace('1761148672134721300', '1761148672134721234')
      .replace('1761148673000000000', '1761148672999999999');
    expect(pretty).toBe(plain);
    expect(exactJson(recorded)).toBe(JSON.stringify(JSON.parse(pretty)).replace('1761148672134721300', '1761148672134721234').replace('1761148673000000000', '1761148672999999999'));
    // Without a sibling a value is written as received.
    expect(exactJson({ t_ns: 5, s: 'x', n: [1, null] })).toBe('{"t_ns":5,"s":"x","n":[1,null]}');
    expect(exactJson('x')).toBe('"x"');
    expect(exactJson(null)).toBe('null');
    expect(exactJson(undefined)).toBe('');
  });
});
