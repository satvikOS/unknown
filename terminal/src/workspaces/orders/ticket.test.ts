import { describe, expect, it } from 'vitest';
import type { Instrument, Session } from '../../api/types';
import {
  buildIntentPayload,
  deadlineNsFrom,
  defaultSessionId,
  derivedPurpose,
  EMPTY_TICKET,
  hasErrors,
  ID_RE,
  isMultipleOf,
  isPositiveDecimal,
  newOrderId,
  parseDeadlineSeconds,
  tickFor,
  validateTicket,
  type TicketInput,
} from './ticket';

const instrument = {
  instrument_id: 'FIX-EQ-1',
  asset_class: 'EQUITY',
  symbol: 'FIXA',
  name: 'Fixture Equity A (simulation only)',
  currency: 'USD',
  settlement_currency: 'USD',
  venue_mic: 'XNAS',
  quantity_increment: '1',
  min_quantity: '1',
  multiplier: '1',
  settlement_days: 1,
  tick_rules: [
    { from_price: '0', tick: '0.0001' },
    { from_price: '1', tick: '0.01' },
  ],
  leveraged: false,
  inverse: false,
  status: 'ACTIVE',
  metadata_version: 1,
  attributes: { fixture: true },
  mappings: [],
} satisfies Instrument;

function session(over: Partial<Session> = {}): Session {
  return {
    session_id: 'ses-1',
    account_key: 'SIM-1',
    state: 'RUNNING',
    mandate_hash: 'm',
    predecessor_id: null,
    currency: 'USD',
    seed: '100.00',
    target: '105.00',
    outcome: null,
    created_at_ns: 0,
    funded_at_ns: null,
    started_at_ns: null,
    ended_at_ns: null,
    completed_at_ns: null,
    settled_at_ns: null,
    wind_down_reason: null,
    wind_down_evidence: null,
    wind_down_attempts: {},
    funding: null,
    high_water: '100.00',
    protected_value: null,
    terminal_equity: null,
    resumes_used: 0,
    review_return_state: null,
    blockers: [],
    suspend_reason: null,
    mandate: {
      session_id: 'ses-1',
      account_key: 'SIM-1',
      currency: 'USD',
      seed: '100.00',
      target_terminal_equity: '105.00',
      strategy_eligibility: ['owner-manual'],
      session_drawdown_trigger_bps: 200,
      session_max_loss: '5.00',
      auto_start: true,
      expires_at_ns: null,
      below_target_after_target_observed: 'CLOSE',
      max_resumes: 0,
      policy_hash: 'pol-hash',
      created_at_ns: 0,
      origin: 'OWNER',
    },
    ...over,
  };
}

const good: TicketInput = { sessionId: 'ses-1', instrumentId: 'FIX-EQ-1', side: 'BUY', quantity: '2', limitPrice: '9.96', deadlineSeconds: '60' };

describe('decimal helpers', () => {
  it('accepts only plain positive decimals', () => {
    expect(isPositiveDecimal('1')).toBe(true);
    expect(isPositiveDecimal('9.96')).toBe(true);
    expect(isPositiveDecimal(' 0.0001 ')).toBe(true);
    for (const bad of ['0', '0.000', '-1', '+1', '1,000', '1e3', '.5', '5.', '', 'abc', '1 2']) {
      expect(isPositiveDecimal(bad), bad).toBe(false);
    }
  });

  it('tests multiples exactly, including values a float gets wrong', () => {
    expect(isMultipleOf('9.96', '0.01')).toBe(true);
    expect(isMultipleOf('9.955', '0.01')).toBe(false);
    expect(isMultipleOf('0.3', '0.1')).toBe(true); // 0.3 % 0.1 is not 0 in floating point
    expect(isMultipleOf('100', '1')).toBe(true);
    expect(isMultipleOf('1.5', '1')).toBe(false);
    expect(isMultipleOf('12345678901234567890.05', '0.05')).toBe(true);
    expect(isMultipleOf('1', '0')).toBeNull();
    expect(isMultipleOf('x', '1')).toBeNull();
  });

  it('picks the tick like domain/instruments.py tick_for', () => {
    expect(tickFor('0.5', instrument.tick_rules)).toBe('0.0001');
    expect(tickFor('1', instrument.tick_rules)).toBe('0.01');
    expect(tickFor('250.00', instrument.tick_rules)).toBe('0.01');
    expect(tickFor('5', [])).toBeNull();
  });
});

describe('validateTicket', () => {
  it('passes a complete ticket with no hints', () => {
    const r = validateTicket(good, { session: session(), instrument });
    expect(r.errors).toEqual({});
    expect(r.hints).toEqual({});
  });

  it('reports every missing field as an error', () => {
    const r = validateTicket(EMPTY_TICKET, {});
    expect(Object.keys(r.errors).sort()).toEqual(['instrumentId', 'limitPrice', 'quantity', 'sessionId', 'side']);
    expect(hasErrors(r)).toBe(true);
  });

  it('rejects malformed numbers and deadlines', () => {
    const r = validateTicket({ ...good, quantity: '-1', limitPrice: '1,000.00', deadlineSeconds: '2' }, { session: session(), instrument });
    expect(r.errors.quantity).toMatch(/positive decimal/);
    expect(r.errors.limitPrice).toMatch(/positive decimal/);
    expect(r.errors.deadlineSeconds).toMatch(/whole seconds/);
  });

  it('refuses a session without a mandate policy hash', () => {
    const r = validateTicket(good, { session: session({ mandate: null }), instrument });
    expect(r.errors.sessionId).toMatch(/policy hash/);
  });

  it('gives advisory hints for tick, increment, session state and currency but leaves the decision to the core', () => {
    const r = validateTicket(
      { ...good, quantity: '1.5', limitPrice: '9.955', side: 'BUY' },
      { session: session({ state: 'SUSPENDED' }), instrument: { ...instrument, currency: 'EUR' } },
    );
    expect(r.errors).toEqual({});
    expect(r.hints.quantity).toMatch(/increment 1/);
    expect(r.hints.limitPrice).toMatch(/tick 0.01/);
    expect(r.hints.sessionId).toMatch(/SUSPENDED.*RUNNING/);
    expect(r.hints.instrumentId).toMatch(/EUR/);
  });

  it('allows a SELL from a winding-down session without a hint', () => {
    const r = validateTicket({ ...good, side: 'SELL' }, { session: session({ state: 'WINDING_DOWN' }), instrument });
    expect(r.hints.sessionId).toBeUndefined();
  });

  it('parses deadline seconds within bounds only', () => {
    expect(parseDeadlineSeconds('60')).toBe(60);
    expect(parseDeadlineSeconds('5')).toBe(5);
    expect(parseDeadlineSeconds('3600')).toBe(3600);
    expect(parseDeadlineSeconds('3601')).toBeNull();
    expect(parseDeadlineSeconds('4')).toBeNull();
    expect(parseDeadlineSeconds('60.5')).toBeNull();
    expect(parseDeadlineSeconds('')).toBeNull();
  });
});

describe('payload', () => {
  it('computes an exact whole-second deadline in nanoseconds', () => {
    const now = 1_792_679_077_825; // server ms
    const ns = deadlineNsFrom(now, 60);
    // ceil((now + 60 s) / 1 s) = 1792679138 s
    expect(String(ns)).toBe('1792679138000000000');
    expect(JSON.stringify({ d: ns })).toBe('{"d":1792679138000000000}');
    expect(Number.isInteger(ns)).toBe(true);
    // Exactly on a second boundary it does not round up an extra second.
    expect(String(deadlineNsFrom(1_792_679_000_000, 60))).toBe('1792679060000000000');
  });

  it('builds the exact submit_intent payload from the ticket and the session mandate', () => {
    const p = buildIntentPayload({ ...good, quantity: ' 2 ', limitPrice: '9.96 ' }, session(), 'own-abc', 1792679138000000000);
    expect(p).toEqual({
      account_key: 'SIM-1',
      session_id: 'ses-1',
      order_id: 'own-abc',
      instrument_id: 'FIX-EQ-1',
      side: 'BUY',
      quantity: '2',
      limit_price: '9.96',
      order_type: 'LIMIT',
      time_in_force: 'DAY',
      deadline_ns: 1792679138000000000,
      policy_hash: 'pol-hash',
    });
    // No client-side money estimate is ever sent.
    expect(Object.keys(p)).not.toContain('required_cash');
  });

  it('refuses to build from an invalid ticket or id', () => {
    expect(() => buildIntentPayload({ ...good, side: '' }, session(), 'own-abc', 1)).toThrow(/side/);
    expect(() => buildIntentPayload(good, session(), 'bad id', 1)).toThrow(/identifier/);
    expect(() => buildIntentPayload(good, session({ mandate: null }), 'own-abc', 1)).toThrow(/policy hash/);
    expect(() => buildIntentPayload({ ...good, quantity: '0' }, session(), 'own-abc', 1)).toThrow(/positive/);
  });

  it('generates order ids the core accepts and the API can address', () => {
    const id = newOrderId((n) => 'ab'.repeat(n));
    expect(id).toBe('own-abababababababababab');
    const random = newOrderId();
    expect(random).toMatch(/^own-[0-9a-f]{20}$/);
    expect(ID_RE.test(random)).toBe(true);
    expect(random).not.toContain('/');
  });

  it('derives purpose and default session like the core and the ticket', () => {
    expect(derivedPurpose('BUY')).toBe('ENTRY');
    expect(derivedPurpose('SELL')).toBe('EXIT');
    expect(defaultSessionId([session({ session_id: 'a', state: 'CLOSED' }), session({ session_id: 'b' })])).toBe('b');
    expect(defaultSessionId([session({ session_id: 'a', state: 'CLOSED' })])).toBe('a');
    expect(defaultSessionId([])).toBe('');
  });
});
