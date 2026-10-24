/**
 * Order workstation logic: exact arithmetic, the live pretrade preview, the price ladder, baskets, cancel/replace,
 * routes and the BUY / SELL / LADDER / BLOTTER functions. Every number is a TEST FIXTURE; nothing is sent.
 */
import { describe, expect, it } from 'vitest';
import type { Instrument, Order, Session } from '../../api/types';
import type { TrackedCommand } from '../../state/commands';
import type { DepthEvent, Quote as MdQuote } from '../../state/marketdata';
import { listFunctions } from '../../ui/functions';
import { outcomeOf, parseBasket } from './basket';
import { ORDER_FUNCTIONS } from './index';
import { ceilToScale, floorToTick, mulExact, stepPrice, subExact } from './exact';
import { buildLadder, instrumentForSecurity, securityKeyOf } from './ladder';
import { ordersRest, parseOrdersRoute, type OrdersRoute } from './model';
import { parsePreview, previewMatches, previewParams, splitRequiredCash } from './preview';
import { replaceState } from './replace';
import { buildIntentPayload, EMPTY_TICKET, eligibilityOf, tifChoices, validateTicket, type TicketInput } from './ticket';

const inst = {
  instrument_id: 'FIX-EQ-1',
  asset_class: 'EQUITY',
  symbol: 'FIXA',
  name: 'Fixture Equity A (test only)',
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

const session = { session_id: 'ses-1', account_key: 'SIM-1', state: 'RUNNING', currency: 'USD', mandate: { policy_hash: 'pol-hash' } } as unknown as Session;

const ticket: TicketInput = { ...EMPTY_TICKET, sessionId: 'ses-1', instrumentId: 'FIX-EQ-1', side: 'BUY', quantity: '300', limitPrice: '1.00' };

function cmd(over: Partial<TrackedCommand>): TrackedCommand {
  return {
    commandId: 'cmd-1', kind: 'submit_intent', label: 'x', status: 'applied', startedAtMs: 0, settledAtMs: 1, response: null, rejection: null,
    awaitSeq: null, error: null, outcomeUnknown: false, lookup: null, payload: null, ...over,
  };
}

function order(over: Partial<Order>): Order {
  return { order_id: 'own-1', instrument_id: 'FIX-EQ-1', side: 'BUY', state: 'ACKNOWLEDGED', cancel_state: 'NONE', limit_price: '9.95', leaves_qty: '10', cum_qty: '0', purpose: 'ENTRY', principal_role: 'OWNER', time_in_force: 'DAY', ...over } as Order;
}

describe('exact arithmetic', () => {
  it('multiplies, subtracts, rounds up and steps prices without floating point', () => {
    expect(mulExact('300', '1.00')).toBe('300.00');
    expect(subExact('302.011672', '300.000000')).toBe('2.011672');
    expect(ceilToScale('0.1234561', 6)).toBe('0.123457');
    expect(ceilToScale('0.1234560', 6)).toBe('0.123456');
    expect(stepPrice('9.99', '0.01', 3)).toBe('10.02');
    expect(stepPrice('0.1', '0.2', -1)).toBe('-0.1'); // 0.1 + 0.2 style sums stay exact
    expect(floorToTick('9.995', '0.01')).toBe('9.990');
  });
});

describe('pretrade preview', () => {
  const response = {
    kind: 'whatif', label: 'HYPOTHETICAL what-if', snapshot_seq: 41, approved: true, code: 'APPROVED', message: null, required_cash: '302.011672',
    intent: { session_id: 'ses-1', instrument_id: 'FIX-EQ-1', side: 'BUY', quantity: '300', limit_price: '1.00', time_in_force: 'DAY' },
    decision: { approved: true, code: 'APPROVED', checks: [
      { check: 'FEE_SCHEDULE', passed: true, detail: 'fee envelope 2.011672 USD from fee.ibkr.pro.tiered.us@2026-10-23' },
      { check: 'CASH', passed: true, detail: 'required 302.011672 available 400.000000' },
    ] },
    reservation_preview: [{ kind: 'cash', unit: 'USD', amount: '302.011672', status: 'ACTIVE' }],
  };

  it('asks only once the ticket is complete, with the time in force', () => {
    expect(previewParams({ ...ticket, side: '' })).toBeNull();
    expect(previewParams({ ...ticket, quantity: '-1' })).toBeNull();
    expect(previewParams(ticket)).toEqual({ session: 'ses-1', instrument: 'FIX-EQ-1', side: 'BUY', quantity: '300', limit: '1.00', tif: 'DAY' });
  });

  it('shows the fee envelope, the reservation and every check from the core, split exactly', () => {
    const p = parsePreview(response, previewParams(ticket), inst);
    expect(p?.approved).toBe(true);
    expect(p?.notional).toBe('300.000000');
    expect(p?.feeEnvelope).toBe('2.011672');
    expect(p?.feeSource?.detail).toContain('fee.ibkr.pro.tiered.us');
    expect(p?.decision?.checks.map((c) => c.check)).toEqual(['FEE_SCHEDULE', 'CASH']);
    expect(p?.reservation[0]?.amount).toBe('302.011672');
    expect(previewMatches(response, previewParams(ticket))).toBe(true);
    expect(previewMatches(response, previewParams({ ...ticket, limitPrice: '1.01' }))).toBe(false);
  });

  it('a missing fee schedule is a failing check with its reason, and nothing is reserved', () => {
    const refused = { ...response, approved: false, code: 'FEE_SCHEDULE', required_cash: null, reservation_preview: [],
      decision: { approved: false, code: 'FEE_SCHEDULE', checks: [{ check: 'FEE_SCHEDULE', passed: false, detail: 'FEE_SCHEDULE_MISSING: no fee schedule in config/fees covers broker ibkr' }] } };
    const p = parsePreview(refused, previewParams(ticket), inst);
    expect(p?.approved).toBe(false);
    expect(p?.code).toBe('FEE_SCHEDULE');
    expect(p?.feeEnvelope).toBeNull();
    expect(p?.feeSource?.detail).toContain('FEE_SCHEDULE_MISSING');
  });

  it('a core fault is reported, never shown as a decision', () => {
    const p = parsePreview({ kind: 'whatif', label: 'HYPOTHETICAL', approved: false, fault: 'boom', reason: 'no decision' }, null, inst);
    expect(p?.fault).toBe('boom');
    expect(p?.decision).toBeNull();
    expect(splitRequiredCash('10.220000', '1', '9.02', '1')).toEqual({ notional: '9.020000', feeEnvelope: '1.200000' });
  });
});

describe('ticket: time in force and trading session', () => {
  const elig = eligibilityOf({ eligibility: { time_in_force: ['DAY'], order_types: ['LIMIT'], outside_regular_hours: false } });
  it('offers the policy list and flags anything else for the core to refuse', () => {
    expect(tifChoices(elig)).toEqual(['DAY']);
    expect(tifChoices({})).toEqual(['DAY']);
    const r = validateTicket({ ...ticket, timeInForce: 'GTC' }, { session, instrument: inst, eligibility: elig });
    expect(r.hints.timeInForce).toContain('TIME_IN_FORCE');
  });
  it('outside regular hours cannot be sent: it is an error and the payload builder refuses it', () => {
    const r = validateTicket({ ...ticket, tradingSession: 'EXTENDED' }, { session, instrument: inst, eligibility: elig });
    expect(r.errors.tradingSession).toContain('not eligible');
    expect(() => buildIntentPayload({ ...ticket, tradingSession: 'EXTENDED' }, session, 'own-1', 1_000_000_000)).toThrow();
    expect(buildIntentPayload(ticket, session, 'own-1', 1_000_000_000).time_in_force).toBe('DAY');
  });
});

describe('price ladder', () => {
  const level = (side: 'bid' | 'ask', levels: [string, string | null][]): DepthEvent =>
    ({ key: 'FIXA.XNAS', seq: 1, side, levels: levels.map(([price, size]) => ({ price, size, mm: null })), source: 'TEST', data_type: 'REALTIME', ts_recv_ns: 0 }) as DepthEvent;
  const quote = { key: 'FIXA.XNAS', bid: '9.99', ask: '10.01', bid_size: '300', ask_size: '200', last: null, source: 'TEST', data_type: 'DELAYED' } as unknown as MdQuote;

  it('uses depth when the source provides it, with our working orders at their prices', () => {
    const l = buildLadder({
      depth: { bid: level('bid', [['9.99', '100'], ['9.98', null]]), ask: level('ask', [['10.01', '50']]) },
      quote,
      orders: [order({ limit_price: '9.980', leaves_qty: '5' }), order({ order_id: 'own-2', state: 'FILLED' })],
      instrument: inst,
      halfRows: 3,
    });
    expect(l.basis).toBe('DEPTH');
    expect(l.tick).toBe('0.01');
    const at = (p: string) => l.rows.find((r) => r.price === p);
    expect(at('9.99')).toMatchObject({ bidSize: '100', bestBid: true });
    expect(at('9.98')).toMatchObject({ hasBid: true, bidSize: null }); // unreported size stays unknown
    expect(at('9.98')?.working.map((w) => w.orderId)).toEqual(['own-1']); // a FILLED order is not working
    expect(at('10.01')).toMatchObject({ askSize: '50', bestAsk: true });
    expect(l.rows.map((r) => r.price)).toEqual([...l.rows.map((r) => r.price)].sort((a, b) => Number(b) - Number(a)));
  });

  it('falls back to level 1, and says why when there is neither', () => {
    const l1 = buildLadder({ depth: { bid: null, ask: null }, quote, orders: [], instrument: inst, halfRows: 2 });
    expect(l1.basis).toBe('LEVEL1');
    expect(l1.dataType).toBe('DELAYED');
    expect(l1.rows.find((r) => r.price === '10.01')?.askSize).toBe('200');
    const none = buildLadder({ depth: { bid: null, ask: null }, quote: undefined, orders: [], instrument: inst });
    expect(none.basis).toBe('NONE');
    expect(none.rows).toEqual([]);
    expect(none.reason).toContain('No depth or level-1 quote');
  });

  it('maps a linked security to one instrument', () => {
    expect(securityKeyOf(inst)).toBe('FIXA.XNAS');
    expect(instrumentForSecurity('FIXA.XNAS', [inst])?.instrument_id).toBe('FIX-EQ-1');
    expect(instrumentForSecurity('FIXA.XNYS', [inst])).toBeUndefined();
    expect(instrumentForSecurity(null, [inst])).toBeUndefined();
  });
});

describe('basket', () => {
  it('parses one intent per line, validates each like the ticket, and names bad lines', () => {
    const items = parseBasket('# comment\nBUY 10 FIXA.XNAS 9.95\nsell 5 FIX-EQ-1 @ 10.05\nBUY ten FIXA.XNAS 9\nHOLD 1 X 1\nBUY 1 NOPE.XNAS 1', {
      session, instruments: [inst], deadlineSeconds: '60', timeInForce: 'DAY',
    });
    expect(items.map((i) => [i.lineNo, i.error === null])).toEqual([[2, true], [3, true], [4, false], [5, false], [6, false]]);
    expect(items[1]?.input).toMatchObject({ side: 'SELL', quantity: '5', limitPrice: '10.05', instrumentId: 'FIX-EQ-1' });
    expect(items[4]?.error).toContain('not one instrument');
  });

  it('each command keeps its own outcome; an unknown outcome stays UNKNOWN with its lookup', () => {
    expect(outcomeOf(undefined).kind).toBe('NOT_SENT');
    expect(outcomeOf(cmd({ status: 'applied' })).kind).toBe('ACCEPTED');
    expect(outcomeOf(cmd({ status: 'rejected', rejection: { code: 'CASH' } as TrackedCommand['rejection'] })).label).toBe('rejected CASH');
    const unknown = outcomeOf(cmd({ status: 'failed', outcomeUnknown: true, lookup: { state: 'failed', message: 'API down', atMs: 1 } }));
    expect(unknown.kind).toBe('UNKNOWN');
    expect(unknown.detail).toContain('Outcome still unknown');
    expect(outcomeOf(cmd({ status: 'failed', outcomeUnknown: true, lookup: { state: 'not_recorded', definitive: true, message: '', atMs: 1 } })).kind).toBe('NOT_EXECUTED');
    expect(outcomeOf(cmd({ status: 'cancelled' })).kind).toBe('CANCELLED');
  });
});

describe('cancel/replace', () => {
  const req = { orderId: 'own-1', cumAtRequest: '0', quantity: '10', limitPrice: '9.90' };
  it('sends the replacement only after the cancel is confirmed with nothing filled', () => {
    expect(replaceState(req, order({}), undefined, undefined).phase).toBe('CANCEL_SENDING');
    expect(replaceState(req, order({ state: 'CANCEL_PENDING', cancel_state: 'SENT' }), cmd({}), undefined).phase).toBe('AWAITING_CANCEL');
    expect(replaceState(req, order({ state: 'CANCELED' }), cmd({}), undefined).phase).toBe('READY');
    expect(replaceState(req, order({ state: 'CANCELED' }), cmd({}), cmd({ commandId: 'cmd-2' })).phase).toBe('SENT');
  });
  it('a fill, a refused cancel or an unknown outcome stops it', () => {
    expect(replaceState(req, order({ state: 'CANCELED', cum_qty: '3' }), cmd({}), undefined).phase).toBe('ABORTED');
    expect(replaceState(req, order({ state: 'FILLED', cum_qty: '10' }), cmd({}), undefined).phase).toBe('ABORTED');
    expect(replaceState(req, order({}), cmd({ status: 'rejected' }), undefined).phase).toBe('ABORTED');
    const u = replaceState(req, order({}), cmd({ status: 'failed', outcomeUnknown: true }), undefined);
    expect(u.phase).toBe('ABORTED');
    expect(u.reason).toContain('UNKNOWN');
  });
});

describe('routes and functions', () => {
  it('round-trips the new views', () => {
    const routes: OrdersRoute[] = [
      { view: 'ticket', instrumentId: null, side: 'BUY' },
      { view: 'ticket', instrumentId: 'FIX-EQ-1', side: 'SELL' },
      { view: 'ladder', instrumentId: null },
      { view: 'ladder', instrumentId: 'FIX-EQ-1' },
      { view: 'basket' },
    ];
    for (const r of routes) expect(parseOrdersRoute(ordersRest(r))).toEqual(r);
    expect(parseOrdersRoute(['~buy'])).toEqual({ view: 'ticket', instrumentId: null, side: 'BUY' });
  });
  it('registers BUY, SELL, LADDER and BLOTTER, which only open views', () => {
    const fns = listFunctions([]);
    for (const m of ['BUY', 'SELL', 'LADDER', 'BLOTTER']) expect(fns.find((f) => f.mnemonic === m)?.workspace).toBe('orders');
    expect(ORDER_FUNCTIONS.find((f) => f.mnemonic === 'BUY')?.rest).toEqual(['~buy']);
    expect(ORDER_FUNCTIONS.find((f) => f.mnemonic === 'BLOTTER')?.needsSecurity).toBe(false);
  });
});
