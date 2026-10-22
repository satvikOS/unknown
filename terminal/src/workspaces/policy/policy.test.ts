import { describe, expect, it } from 'vitest';
import { differences, diffBodies, flatten, jsonEqual, summarize } from './diff';
import { fieldOrder, FIELDS, formatField, formatPolicyValue, humanizeKey, humanSeconds, impliedRequiredReturn, sectionOf } from './fields';
import { checkPolicyJson, lineColumn, prettyJson } from './jsonCheck';

const BASE = {
  schema_version: 1,
  name: 'simulated-default-v1',
  mode: 'SIMULATED',
  currency: 'USD',
  eligibility: { venues: ['XNAS', 'XNYS', 'BATS'], allow_short: false },
  account_limits: { max_order_notional: '100.00', price_collar_bps: 100, risk_day_timezone: 'America/New_York' },
  session_template: { seed: '100.00', target_terminal_equity: '105.00', expires_after_s: null },
  on_stop_liquidation: 'NONE',
};

describe('policy field formatting (units)', () => {
  it('renders money with currency and never as a float', () => {
    expect(formatPolicyValue('300.00', 'money', 'USD')).toEqual({ text: '300.00 USD' });
    expect(formatPolicyValue('1234567.891', 'money', 'USD').text).toBe('1,234,567.891 USD');
    expect(formatPolicyValue(300.1, 'money', 'USD')).toEqual({ text: '300.1', warning: 'expected a decimal string' });
    expect(formatPolicyValue('0.005', 'moneyPerShare', 'USD').text).toBe('0.005 USD per share');
  });

  it('renders basis points with an exact percentage', () => {
    expect(formatPolicyValue(100, 'bps', 'USD', 'from mid')).toEqual({ text: '100 bps from mid', detail: '1.00%' });
    expect(formatPolicyValue(10_000, 'bps', 'USD', 'of session equity')).toEqual({ text: '10,000 bps of session equity', detail: '100.00%' });
    expect(formatPolicyValue('100', 'bps', 'USD').warning).toBe('expected an integer');
  });

  it('renders durations, rates, sizes and flags with units', () => {
    expect(formatPolicyValue(5000, 'ms', 'USD')).toEqual({ text: '5,000 ms', detail: '5 s' });
    expect(formatPolicyValue(250, 'ms', 'USD')).toEqual({ text: '250 ms' });
    expect(formatPolicyValue(900, 's', 'USD')).toEqual({ text: '900 s', detail: '15 min' });
    expect(formatPolicyValue(30, 's', 'USD')).toEqual({ text: '30 s' });
    expect(formatPolicyValue(null, 'sOrNever', 'USD')).toEqual({ text: 'never', detail: 'no expiry' });
    expect(formatPolicyValue(10, 'perMinute', 'USD').text).toBe('10 orders per minute');
    expect(formatPolicyValue(1024, 'mb', 'USD').text).toBe('1,024 MB');
    expect(formatPolicyValue(false, 'bool', 'USD').text).toBe('no');
    expect(formatPolicyValue(['EQUITY', 'ETF'], 'list', 'USD').text).toBe('EQUITY, ETF');
    expect(formatPolicyValue([], 'list', 'USD').text).toBe('none');
  });

  it('formats exact human durations', () => {
    expect(humanSeconds(5400)).toBe('1 h 30 min');
    expect(humanSeconds(86_400)).toBe('1 d');
    expect(humanSeconds(90_061)).toBe('1 d 1 h 1 min 1 s');
    expect(humanSeconds(0)).toBe('0 s');
  });

  it('covers every limit the risk workspace shows, and formats unknown fields raw', () => {
    const limits = [
      'authorized_capital',
      'operational_buffer',
      'max_order_notional',
      'max_position_bps_of_session_equity',
      'max_gross_exposure',
      'max_orders_per_minute',
      'price_collar_bps',
      'quote_max_age_ms',
      'daily_loss_trigger',
      'cumulative_loss_allowance',
      'risk_day_timezone',
    ];
    for (const k of limits) expect(FIELDS.some((f) => f.path === `account_limits.${k}`)).toBe(true);
    expect(formatField('account_limits.new_thing', { x: 1 }, 'USD')).toEqual({ text: '{"x":1}' });
    expect(formatField('account_limits.daily_loss_trigger', '3.00', 'USD').text).toBe('3.00 USD');
    expect(humanizeKey('account_limits.new_thing')).toBe('New thing');
    expect(sectionOf('name')).toBe('');
    expect(sectionOf('fees.per_share')).toBe('fees');
  });

  it('computes the implied required return exactly', () => {
    expect(impliedRequiredReturn('100.00', '105.00')).toBe('5.00%');
    expect(impliedRequiredReturn('1', '10000')).toBe('999,900.00%');
    expect(impliedRequiredReturn('3', '4')).toBe('33.33%');
    expect(impliedRequiredReturn('0', '1')).toBeNull();
    expect(impliedRequiredReturn(100, '105')).toBeNull();
  });
});

describe('policy diff', () => {
  it('flattens objects to leaves and keeps lists and nulls as leaves', () => {
    const f = flatten(BASE);
    expect(f.get('eligibility.venues')).toEqual(['XNAS', 'XNYS', 'BATS']);
    expect(f.get('session_template.expires_after_s')).toBeNull();
    expect(f.has('eligibility')).toBe(false);
    expect(flatten({ a: {} }).get('a')).toEqual({});
  });

  it('compares structurally regardless of key order', () => {
    expect(jsonEqual({ a: 1, b: [1, { c: 2 }] }, { b: [1, { c: 2 }], a: 1 })).toBe(true);
    expect(jsonEqual([1, 2], [2, 1])).toBe(false);
    expect(jsonEqual({ a: 1 }, { a: 1, b: 2 })).toBe(false);
    expect(jsonEqual(null, {})).toBe(false);
    const reordered = JSON.parse(JSON.stringify(BASE, Object.keys(BASE).reverse()));
    expect(differences(diffBodies(BASE, { ...reordered, ...BASE }))).toEqual([]);
  });

  it('reports changed, added and removed fields with list deltas, in documented order', () => {
    const next = structuredClone(BASE) as Record<string, any>;
    next.name = 'tighter-v2';
    next.account_limits.max_order_notional = '50.00';
    next.eligibility.venues = ['XNAS', 'XNYS', 'ARCX'];
    delete next.on_stop_liquidation;
    next.account_limits.extra_field = 1;
    const rows = diffBodies(BASE, next);
    const changed = differences(rows);
    expect(changed.map((r) => [r.path, r.change])).toEqual([
      ['name', 'changed'],
      ['on_stop_liquidation', 'removed'],
      ['account_limits.max_order_notional', 'changed'],
      ['eligibility.venues', 'changed'],
      ['account_limits.extra_field', 'added'],
    ]);
    const venues = changed.find((r) => r.path === 'eligibility.venues')!;
    expect(venues.listAdded).toEqual(['ARCX']);
    expect(venues.listRemoved).toEqual(['BATS']);
    const notional = changed.find((r) => r.path === 'account_limits.max_order_notional')!;
    expect(notional).toMatchObject({ a: '100.00', b: '50.00', section: 'account_limits' });
    expect(summarize(rows)).toEqual({ changed: 3, added: 1, removed: 1, same: rows.length - 5 });
    expect(fieldOrder('name')).toBeLessThan(fieldOrder('account_limits.authorized_capital'));
  });

  it('treats a string amount and a numeric amount as different', () => {
    const rows = differences(diffBodies({ x: '1.0' }, { x: 1 }));
    expect(rows).toHaveLength(1);
  });
});

describe('policy JSON check', () => {
  it('accepts a well-formed object and returns the same value as JSON.parse', () => {
    const text = prettyJson(BASE);
    const r = checkPolicyJson(text);
    expect(r.ok).toBe(true);
    expect(r.value).toEqual(JSON.parse(text));
  });

  it('reports syntax errors with line and column', () => {
    const r = checkPolicyJson('{\n  "a": 1,\n  "b": }\n');
    expect(r.ok).toBe(false);
    expect(r.issues[0]).toMatchObject({ line: 3, column: 8 });
    expect(r.issues[0]?.message).toMatch(/expected a value/);
    expect(checkPolicyJson('{"a": 1,}').issues[0]?.message).toMatch(/Trailing comma/);
    expect(checkPolicyJson('{"a": "x}').issues[0]?.message).toMatch(/Unterminated/);
    expect(checkPolicyJson('{"a": tru}').ok).toBe(false);
    expect(checkPolicyJson('{} {}').issues[0]?.message).toMatch(/after the end/);
  });

  it('requires an object at the top level', () => {
    expect(checkPolicyJson('[1, 2]').issues[0]?.message).toMatch(/must be a JSON object/);
    expect(checkPolicyJson('   ').issues[0]?.message).toMatch(/empty/);
  });

  it('refuses duplicate keys that JSON.parse would silently drop', () => {
    const r = checkPolicyJson('{"limits": {"max": "1.00", "max": "2.00"}}');
    expect(r.ok).toBe(false);
    expect(r.issues[0]?.message).toMatch(/Duplicate key “max”/);
    expect(r.issues[0]).toMatchObject({ line: 1, column: 28 });
  });

  it('refuses number literals that would not reach the server as typed', () => {
    expect(checkPolicyJson('{"n": 10.0}').issues[0]?.message).toMatch(/10\.0 would be sent as 10/);
    expect(checkPolicyJson('{"amount": 300.10}').issues[0]?.message).toMatch(/300\.1/);
    expect(checkPolicyJson('{"n": 12345678901234567890}').ok).toBe(false);
    expect(checkPolicyJson('{"n": 1e3}').ok).toBe(false);
    expect(checkPolicyJson('{"n": 10, "m": -5, "f": 0.5}').ok).toBe(true);
  });

  it('decodes escapes and handles keys that look like object internals', () => {
    const r = checkPolicyJson('{"__proto__": {"x": 1}, "s": "a\\u0041\\n"}');
    expect(r.ok).toBe(true);
    expect(Object.keys(r.value as object)).toEqual(['__proto__', 's']);
    expect((r.value as Record<string, unknown>).s).toBe('aA\n');
  });

  it('maps offsets to 1-based lines and columns', () => {
    expect(lineColumn('ab\ncd', 0)).toEqual({ line: 1, column: 1 });
    expect(lineColumn('ab\ncd', 4)).toEqual({ line: 2, column: 2 });
  });
});
