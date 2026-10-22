import { describe, expect, it } from 'vitest';
import type { Bucket, Order, Position, Quote } from '../../api/types';
import {
  breachLabel,
  budgetUse,
  capUse,
  concentrations,
  dailyLoss,
  estimateMarkedPnl,
  grossExposure,
  isSystemOwner,
  lossFromHighWater,
  recordedBlocks,
  riskDayAt,
  sessionOfOwner,
} from './riskModel';

const SES = 'session:ses-1';

function bucket(owner: string, liquid: string, system = isSystemOwner(owner)): Bucket {
  return {
    owner,
    currency: 'USD',
    settled_cash: liquid,
    receivable_unsettled: '0',
    payable_unsettled: '0',
    fees_accrued: '0',
    liquid_value: liquid,
    available_cash: liquid,
    holds: '0',
    inventory_cost: '0',
    system_owned: system,
  };
}

function position(owner: string, inst: string, mv: string | null, cost = '49.850000', qty = '5'): Position {
  return {
    owner,
    instrument_id: inst,
    symbol: inst === 'FIX-EQ-1' ? 'FIXA' : 'FIXB',
    currency: 'USD',
    quantity: qty,
    cost_basis: cost,
    lots: 1,
    mark: mv === null ? null : '9.90',
    market_value: mv,
    unrealized: null,
    mark_basis: mv === null ? 'unavailable: no quote' : 'bid from fixture-market age 200 ms FIXTURE',
    mark_fixture: mv !== null,
  };
}

function quote(inst: string, over: Partial<Quote> = {}): Quote {
  return {
    instrument_id: inst,
    bid: '9.90',
    ask: '9.92',
    last: '9.91',
    currency: 'USD',
    source: 'fixture-market',
    event_time_ns: 0,
    receipt_time_ns: 0,
    age_ms: 200,
    delayed: false,
    fixture: true,
    ...over,
  };
}

function order(id: string, inst: string, leaves: string, limit: string, over: Partial<Order> = {}): Order {
  return {
    order_id: id,
    session_id: 'ses-1',
    instrument_id: inst,
    side: 'BUY',
    currency: 'USD',
    state: 'ACKNOWLEDGED',
    leaves_qty: leaves,
    limit_price: limit,
    ...over,
  } as Order;
}

describe('owners', () => {
  it('follows the core definition of system-owned buckets', () => {
    expect(isSystemOwner('unallocated')).toBe(true);
    expect(isSystemOwner('buffer')).toBe(true);
    expect(isSystemOwner(SES)).toBe(true);
    expect(isSystemOwner('protected:ses-1')).toBe(true);
    expect(isSystemOwner('outside')).toBe(false);
    expect(isSystemOwner('external')).toBe(false);
    expect(sessionOfOwner(SES)).toBe('ses-1');
    expect(sessionOfOwner('protected:ses-1')).toBeNull();
  });
});

describe('marked P&L estimate', () => {
  const base = {
    buckets: [bucket('outside', '1000'), bucket('unallocated', '200.000000'), bucket(SES, '49.150000')],
    positions: [position(SES, 'FIX-EQ-1', '49.500000'), position('outside', 'FIX-EQ-2', '41.00')],
    quotes: { 'FIX-EQ-1': quote('FIX-EQ-1'), 'FIX-EQ-2': quote('FIX-EQ-2') },
    quotesElapsedMs: 0,
    authorizedTotal: '300.000000',
    currency: 'USD',
    quoteMaxAgeMs: 5000,
    multipliers: { 'FIX-EQ-1': '1', 'FIX-EQ-2': '1.000000' },
  };

  it('sums system buckets and fresh marks, excluding outside owners', () => {
    const e = estimateMarkedPnl(base);
    expect(e.status).toBe('complete');
    expect(e.liquid).toBe('249.150000');
    expect(e.positions).toBe('49.500000');
    expect(e.systemValue).toBe('298.650000');
    expect(e.pnl).toBe('-1.350000');
    expect(e.fixture).toBe(true);
  });

  it('fails closed on a stale, delayed or missing bid, like the core', () => {
    const stale = estimateMarkedPnl({ ...base, quotes: { 'FIX-EQ-1': quote('FIX-EQ-1', { age_ms: 4000 }) }, quotesElapsedMs: 1500 });
    expect(stale.status).toBe('incomplete');
    expect(stale.pnl).toBeNull();
    expect(stale.stale).toEqual([{ owner: SES, instrument_id: 'FIX-EQ-1', problem: 'stale' }]);
    expect(estimateMarkedPnl({ ...base, quotes: { 'FIX-EQ-1': quote('FIX-EQ-1', { delayed: true }) } }).stale[0]?.problem).toBe('delayed');
    expect(estimateMarkedPnl({ ...base, quotes: { 'FIX-EQ-1': quote('FIX-EQ-1', { bid: null }) } }).stale[0]?.problem).toBe('no bid');
    expect(estimateMarkedPnl({ ...base, quotes: {} }).stale[0]?.problem).toBe('no quote');
  });

  it('refuses positions whose multiplier is unknown or not 1 (the positions API omits it)', () => {
    expect(estimateMarkedPnl({ ...base, multipliers: {} }).stale[0]?.problem).toBe('multiplier unknown');
    expect(estimateMarkedPnl({ ...base, multipliers: { 'FIX-EQ-1': '100' } }).stale[0]?.problem).toBe('multiplier not 1');
  });

  it('values cash only when there are no positions', () => {
    const e = estimateMarkedPnl({ ...base, positions: [], buckets: [bucket('unallocated', '300.000000')] });
    expect(e.pnl).toBe('0.000000');
  });
});

describe('loss budgets', () => {
  it('measures loss from the high-water mark, never refilled by gains', () => {
    expect(lossFromHighWater('0.000000', '-1.350000')).toBe('1.350000');
    expect(lossFromHighWater('5.00', '2.00')).toBe('3.00');
    // a new high: no loss
    expect(lossFromHighWater('5.00', '7.25')).toBe('0.00');
  });

  it('measures daily loss from the day start and reports gains as zero loss', () => {
    expect(dailyLoss('0.000000', '-1.35')).toBe('1.350000');
    expect(dailyLoss('-2.00', '1.00')).toBe('0');
  });

  it('classifies usage with the core comparison at the trigger', () => {
    expect(budgetUse('1.350000', '15.00')).toMatchObject({ bps: 900n, state: 'within', remaining: '13.650000' });
    expect(budgetUse('1.50', '3.00')).toMatchObject({ bps: 5000n, state: 'approaching' });
    // the core breaches at loss >= trigger
    expect(budgetUse('3.00', '3.00')).toMatchObject({ state: 'reached', remaining: '0.00' });
    expect(budgetUse('4', '3')).toMatchObject({ bps: 13334n, state: 'reached' });
  });

  it('classifies exposure caps as reached only when strictly above (after <= max passes)', () => {
    expect(capUse('300.00', '300.00').state).toBe('approaching');
    expect(capUse('300.01', '300.00').state).toBe('reached');
    expect(capUse('10', '0').bps).toBeNull();
  });

  it('describes breaches and the blocks they cause', () => {
    expect(breachLabel(null)).toBe('no breach');
    expect(breachLabel('DAILY_LOSS')).toMatch(/daily loss/);
    expect(recordedBlocks({ breach: 'DAILY_LOSS', valuation_ok: false } as never)).toEqual(['LOSS_BUDGET:DAILY_LOSS', 'VALUATION_STALE']);
    expect(recordedBlocks(null)).toEqual([]);
  });
});

describe('risk day', () => {
  it('uses the policy time zone, not UTC', () => {
    const ms = Date.UTC(2026, 9, 22, 2, 30); // 02:30 UTC = 22:30 previous day in New York (EDT)
    expect(riskDayAt('America/New_York', ms)).toBe('2026-10-21');
    expect(riskDayAt('UTC', ms)).toBe('2026-10-22');
    expect(riskDayAt('Not/AZone', ms)).toBeNull();
  });
});

describe('gross exposure', () => {
  it('adds system positions at bid (or cost) and open buy commitments', () => {
    const g = grossExposure(
      [position(SES, 'FIX-EQ-1', '49.500000'), position(SES, 'FIX-EQ-2', null, '41.24', '1'), position('outside', 'FIX-EQ-1', '99')],
      [order('o1', 'FIX-EQ-2', '1', '40.96'), order('o2', 'FIX-EQ-1', '2', '9.97', { side: 'SELL' }), order('o3', 'FIX-EQ-1', '3', '9.9', { state: 'FILLED' })],
      'USD',
      { 'FIX-EQ-1': '1', 'FIX-EQ-2': '1' },
    );
    expect(g.positionsAtBid).toBe('49.500000');
    expect(g.positionsAtCost).toBe('41.24');
    expect(g.openBuys).toBe('40.96');
    expect(g.total).toBe('131.700000');
    expect(g.buys.map((b) => b.order_id)).toEqual(['o1']);
    expect(g.lines.map((l) => [l.basis, l.fixture])).toEqual([
      ['bid', true],
      ['cost', false],
    ]);
  });

  it('withholds the total when a multiplier is unknown', () => {
    const g = grossExposure([position(SES, 'FIX-EQ-1', '49.5')], [order('o1', 'FIX-EQ-9', '1', '10')], 'USD', { 'FIX-EQ-1': '1' });
    expect(g.total).toBeNull();
    expect(g.unresolved).toEqual([{ instrument_id: 'FIX-EQ-9', reason: 'order o1: multiplier unknown' }]);
  });

  it('applies the multiplier to open orders exactly', () => {
    const g = grossExposure([], [order('o1', 'OPT', '2', '1.25')], 'USD', { OPT: '100' });
    expect(g.openBuys).toBe('250.00');
  });
});

describe('concentration', () => {
  it('divides each session position by the session equity estimate', () => {
    const c = concentrations(
      [position(SES, 'FIX-EQ-1', '49.500000'), position(SES, 'FIX-EQ-2', null, '41.24', '1'), position('unallocated', 'FIX-EQ-1', '10')],
      [bucket(SES, '9.26')],
      'USD',
    );
    expect(c).toHaveLength(2);
    expect(c[0]).toMatchObject({ session_id: 'ses-1', sessionEquity: '100.000000', basis: 'bid', bps: 4950n });
    expect(c[1]).toMatchObject({ basis: 'cost', value: '41.24', bps: 4124n });
  });
});
