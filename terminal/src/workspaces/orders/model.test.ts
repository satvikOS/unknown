import { describe, expect, it } from 'vitest';
import type { EventRecord, Rejection } from '../../api/types';
import { decisionFromRejection, decisionFromTimeline, explainRejectionCode, parseDecision, parseQuote, summarizeDecision } from './decision';
import {
  cancelAvailability,
  cancelStateLabel,
  isOpenOrder,
  isTerminalOrder,
  liveCancelRow,
  matchesOrderText,
  ordersRest,
  parseOrdersRoute,
  summarizeEventInfo,
  summarizeHolds,
  type OrdersRoute,
} from './model';

describe('orders routes', () => {
  it('parses blotter, views and order ids', () => {
    expect(parseOrdersRoute([])).toEqual({ view: 'blotter' });
    expect(parseOrdersRoute(['~executions'])).toEqual({ view: 'executions' });
    expect(parseOrdersRoute(['~new'])).toEqual({ view: 'ticket', instrumentId: null });
    expect(parseOrdersRoute(['~new', 'FIX-EQ-1'])).toEqual({ view: 'ticket', instrumentId: 'FIX-EQ-1' });
    expect(parseOrdersRoute(['own-1a2b'])).toEqual({ view: 'detail', orderId: 'own-1a2b' });
    expect(parseOrdersRoute(['mac-dev:1:x@y+z'])).toEqual({ view: 'detail', orderId: 'mac-dev:1:x@y+z' });
  });

  it('round-trips every route', () => {
    const routes: OrdersRoute[] = [
      { view: 'blotter' },
      { view: 'executions' },
      { view: 'ticket', instrumentId: null },
      { view: 'ticket', instrumentId: 'FIX-EQ-2' },
      { view: 'detail', orderId: 'own-1' },
    ];
    for (const r of routes) expect(parseOrdersRoute(ordersRest(r))).toEqual(r);
  });
});

describe('order states and cancel', () => {
  it('classifies open and terminal states like the core', () => {
    for (const s of ['RESERVED', 'SUBMIT_PENDING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN']) {
      expect(isOpenOrder(s), s).toBe(true);
      expect(isTerminalOrder(s), s).toBe(false);
    }
    for (const s of ['FILLED', 'CANCELED', 'REJECTED', 'EXPIRED']) {
      expect(isOpenOrder(s), s).toBe(false);
      expect(isTerminalOrder(s), s).toBe(true);
    }
  });

  it('offers cancel only where the core would act', () => {
    expect(cancelAvailability({ state: 'FILLED', cancel_state: 'NONE' }).allowed).toBe(false);
    expect(cancelAvailability({ state: 'CANCEL_PENDING', cancel_state: 'REQUESTED' }).allowed).toBe(false);
    expect(cancelAvailability({ state: 'ACKNOWLEDGED', cancel_state: 'ACKED' }).allowed).toBe(false);
    expect(cancelAvailability({ state: 'ACKNOWLEDGED', cancel_state: 'ACKED' }, []).allowed).toBe(false);
    const ack = cancelAvailability({ state: 'ACKNOWLEDGED', cancel_state: 'NONE' });
    expect(ack.allowed).toBe(true);
    expect(ack.reason).toMatch(/reservation stays in place until the broker confirms/);
    // A rejected or unknown cancel may be requested again.
    expect(cancelAvailability({ state: 'PARTIALLY_FILLED', cancel_state: 'REJECTED' }).allowed).toBe(true);
    expect(cancelAvailability({ state: 'UNKNOWN', cancel_state: 'UNKNOWN' }).reason).toMatch(/outcome is unknown/);
    expect(cancelAvailability({ state: 'RESERVED', cancel_state: 'NONE' }).reason).toMatch(/not been sent yet/);
    expect(cancelStateLabel('NONE')).toBe('none');
  });

  it('uses the outbox, when known, to tell a live cancel from a lost one', () => {
    const live = [
      { action: 'SUBMIT' as const, state: 'DONE', outbox_id: 'o:submit' },
      { action: 'CANCEL' as const, state: 'SENDING', outbox_id: 'o:cancel:1' },
    ];
    const r = cancelAvailability({ state: 'CANCEL_PENDING', cancel_state: 'SENT' }, live);
    expect(r.allowed).toBe(false);
    expect(r.reason).toMatch(/being sent \(outbox o:cancel:1\)/);
    expect(liveCancelRow(live)?.outbox_id).toBe('o:cancel:1');
    const lost = [
      { action: 'SUBMIT' as const, state: 'DONE', outbox_id: 'o:submit' },
      { action: 'CANCEL' as const, state: 'UNKNOWN', outbox_id: 'o:cancel:1' },
    ];
    expect(liveCancelRow(lost)).toBeNull();
    const again = cancelAvailability({ state: 'CANCEL_PENDING', cancel_state: 'REQUESTED' }, lost);
    expect(again.allowed).toBe(true);
    expect(again.reason).toMatch(/re-issues it/);
    expect(cancelStateLabel('ACKED')).toBe('ACKED');
  });

  it('splits holds into active and released', () => {
    const s = summarizeHolds([
      { kind: 'CASH', unit: 'USD', amount: '11.160000', status: 'ACTIVE' },
      { kind: 'QUANTITY', unit: 'FIX-EQ-1', amount: '0', status: 'RELEASED' },
    ]);
    expect(s.active.map((h) => h.kind)).toEqual(['CASH']);
    expect(s.released.map((h) => h.kind)).toEqual(['QUANTITY']);
    expect(summarizeHolds(undefined)).toEqual({ active: [], released: [] });
  });

  it('filters by every typed token across ids and symbol', () => {
    const o = { order_id: 'own-1', broker_order_id: 'SIMORD-7', instrument_id: 'FIX-EQ-1', session_id: 'ses-9' };
    expect(matchesOrderText(o, 'FIXA', '')).toBe(true);
    expect(matchesOrderText(o, 'FIXA', 'simord-7')).toBe(true);
    expect(matchesOrderText(o, 'FIXA', 'fixa ses-9')).toBe(true);
    expect(matchesOrderText(o, 'FIXA', 'fixb')).toBe(false);
  });
});

describe('timeline summaries', () => {
  it('summarises the decision and scalar fields and counts nested ones', () => {
    const text = summarizeEventInfo({
      info: {
        decision: { approved: true, code: 'APPROVED', checks: [{}, {}, {}] },
        quote: { bid: '1' },
        required_cash: '11.160000',
        purpose: 'ENTRY',
        evidence: { a: 1 },
      },
    });
    expect(text).toBe('pretrade APPROVED (3 checks) · required cash 11.160000 · purpose=ENTRY · quote recorded · 1 structured field');
    expect(summarizeEventInfo({ info: { note: 'reservation retained until broker confirmation', reason: 'requested' } })).toBe(
      'note=reservation retained until broker confirmation · reason=requested',
    );
    expect(summarizeEventInfo({ info: {} })).toBe('');
  });
});

const checks = [
  { check: 'SIDE', passed: true, detail: 'BUY' },
  { check: 'PRICE_COLLAR', passed: false, detail: 'buy limit 20.00 vs cap 10.049500 (mid 9.950, 100 bps)' },
  { check: 'CASH', passed: false, detail: 'required 21.200000 available 88.840000' },
];
const quote = {
  instrument_id: 'FIX-EQ-1',
  bid: '9.94',
  ask: '9.96',
  last: '9.95',
  currency: 'USD',
  source: 'fixture-market',
  event_time_ns: 1792679099000000000,
  receipt_time_ns: 1792679099000000000,
  age_ms: 252,
  delayed: false,
  fixture: true,
};

describe('pretrade decisions', () => {
  it('reads the decision and quote from a rejected submit_intent', () => {
    const rejection: Rejection = {
      code: 'PRICE_COLLAR',
      message: 'pretrade check failed',
      details: { decision: { approved: false, code: 'PRICE_COLLAR', checks }, quote },
    };
    const r = decisionFromRejection(rejection);
    expect(r.decision?.code).toBe('PRICE_COLLAR');
    expect(r.decision?.checks).toHaveLength(3);
    expect(r.quote?.fixture).toBe(true);
    expect(r.quote?.source).toBe('fixture-market');
    const s = summarizeDecision(r.decision!);
    expect(s).toEqual({ total: 3, passed: 1, failed: 2, decisive: checks[1] });
  });

  it('reports no decision for rejections raised before pretrade', () => {
    const r = decisionFromRejection({ code: 'UNKNOWN_SESSION', message: 'nope', details: {} });
    expect(r.decision).toBeNull();
    expect(r.quote).toBeNull();
    expect(explainRejectionCode('UNKNOWN_SESSION')).toMatch(/session/);
    expect(explainRejectionCode('PRICE_COLLAR')).toBeNull();
  });

  it('reads the accepted decision from the OrderAccepted event', () => {
    const ev = (type: string, seq: number, info: Record<string, unknown>) => ({ type, seq, info }) as unknown as EventRecord;
    const timeline = [
      ev('OrderAccepted', 18, { decision: { approved: true, code: 'APPROVED', checks: [checks[0]] }, quote, required_cash: '11.160000', purpose: 'ENTRY' }),
      ev('OrderSubmitStarted', 19, { attempt: 1 }),
    ];
    const r = decisionFromTimeline(timeline);
    expect(r?.decision?.approved).toBe(true);
    expect(r?.requiredCash).toBe('11.160000');
    expect(r?.seq).toBe(18);
    expect(decisionFromTimeline([timeline[1]!])).toBeNull();
    expect(decisionFromTimeline(undefined)).toBeNull();
  });

  it('refuses malformed records instead of guessing', () => {
    expect(parseDecision({ approved: true, code: 'APPROVED', checks: [{ check: 'X' }] })).toBeNull();
    expect(parseDecision({ approved: 'yes', code: 'APPROVED', checks: [] })).toBeNull();
    expect(parseDecision(null)).toBeNull();
    expect(parseQuote({ ...quote, bid: 9.94 })).toBeNull();
    expect(parseQuote({ ...quote, age_ms: '252' })).toBeNull();
    expect(parseQuote({ ...quote, bid: null })?.bid).toBeNull();
  });
});
