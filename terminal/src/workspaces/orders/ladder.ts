/**
 * Price ladder (CAP-OMS-01 order entry): one row per price, with market depth when the serving source provides it,
 * else level 1 from the same market-data client, and our own working orders at each price. Pure and exact: prices
 * are decimal strings laid out on the instrument's tick grid with BigInt arithmetic. Clicking a row only stages an
 * intent; the core's pretrade check decides when it is sent.
 */
import type { Instrument, Order } from '../../api/types';
import { parseDecimal } from '../../format/decimal';
import type { DepthEvent, Quote as MdQuote } from '../../state/marketdata';
import { addExact, cmpExact, floorToTick, stepPrice } from './exact';
import { isOpenOrder } from './model';
import { tickFor } from './ticket';

export const LADDER_HALF_ROWS = 12;

export interface WorkingAt {
  orderId: string;
  side: 'BUY' | 'SELL';
  leaves: string;
  state: string;
  cancelState: string;
}

export interface LadderRow {
  price: string;
  bidSize: string | null;
  askSize: string | null;
  /** True when the book has a level at this price on that side (its size may still be unreported). */
  hasBid: boolean;
  hasAsk: boolean;
  bestBid: boolean;
  bestAsk: boolean;
  working: WorkingAt[];
}

export type LadderBasis = 'DEPTH' | 'LEVEL1' | 'NONE';

export interface Ladder {
  rows: LadderRow[];
  basis: LadderBasis;
  tick: string | null;
  source: string | null;
  dataType: string | null;
  /** Why the ladder has no market rows (shown with the unavailable marker). */
  reason: string | null;
}

/** Canonical key of a decimal price ("9.950" and "9.95" are one row). */
export function priceKey(price: string): string | null {
  const p = parseDecimal(price);
  return p ? `${p.negative ? '-' : ''}${p.int}${p.frac ? `.${p.frac}` : ''}` : null;
}

/** The security key the market-data client uses for an instrument ("SYMBOL.MIC"). */
export function securityKeyOf(i: Pick<Instrument, 'symbol' | 'venue_mic'> | undefined): string | null {
  return i && i.symbol && i.venue_mic ? `${i.symbol}.${i.venue_mic}` : null;
}

/** The instrument a linked security names: symbol and venue MIC, or a unique symbol when no MIC is given. */
export function instrumentForSecurity(key: string | null, instruments: readonly Instrument[]): Instrument | undefined {
  if (!key) return undefined;
  const dot = key.lastIndexOf('.');
  const mic = dot > 0 && /^[A-Z][A-Z0-9]{3}$/.test(key.slice(dot + 1)) ? key.slice(dot + 1) : null;
  const symbol = mic ? key.slice(0, dot) : key;
  const same = instruments.filter((i) => i.symbol === symbol && (mic === null || i.venue_mic === mic));
  return same.length === 1 ? same[0] : undefined;
}

interface Level {
  price: string;
  size: string | null;
}

function merge(levels: readonly Level[]): Map<string, Level> {
  const out = new Map<string, Level>();
  for (const l of levels) {
    const k = priceKey(l.price);
    if (!k) continue;
    const prev = out.get(k);
    if (!prev) out.set(k, { price: k, size: l.size });
    else out.set(k, { price: k, size: prev.size !== null && l.size !== null ? addExact(prev.size, l.size) : prev.size ?? l.size });
  }
  return out;
}

export interface LadderInput {
  depth: { bid: DepthEvent | null; ask: DepthEvent | null };
  quote: MdQuote | undefined;
  orders: readonly Order[];
  instrument: Pick<Instrument, 'instrument_id' | 'tick_rules'>;
  halfRows?: number;
}

export function buildLadder({ depth, quote, orders, instrument, halfRows = LADDER_HALF_ROWS }: LadderInput): Ladder {
  const depthBids = depth.bid?.levels ?? [];
  const depthAsks = depth.ask?.levels ?? [];
  let basis: LadderBasis = 'NONE';
  let bids: Level[] = [];
  let asks: Level[] = [];
  let source: string | null = null;
  let dataType: string | null = null;
  if (depthBids.length || depthAsks.length) {
    basis = 'DEPTH';
    bids = depthBids.map((l) => ({ price: l.price, size: l.size }));
    asks = depthAsks.map((l) => ({ price: l.price, size: l.size }));
    const src = depth.bid ?? depth.ask;
    source = src?.source ?? null;
    dataType = src?.data_type ?? null;
  } else if (quote && (quote.bid || quote.ask)) {
    basis = 'LEVEL1';
    if (quote.bid) bids = [{ price: quote.bid, size: quote.bid_size }];
    if (quote.ask) asks = [{ price: quote.ask, size: quote.ask_size }];
    source = quote.source;
    dataType = quote.data_type;
  }
  const bidMap = merge(bids);
  const askMap = merge(asks);
  const bestBid = [...bidMap.keys()].sort((a, b) => cmpExact(b, a) ?? 0)[0] ?? null;
  const bestAsk = [...askMap.keys()].sort((a, b) => cmpExact(a, b) ?? 0)[0] ?? null;

  const working = new Map<string, WorkingAt[]>();
  for (const o of orders) {
    if (o.instrument_id !== instrument.instrument_id || !isOpenOrder(o.state)) continue;
    const k = priceKey(o.limit_price);
    if (!k) continue;
    const list = working.get(k) ?? [];
    list.push({ orderId: o.order_id, side: o.side, leaves: o.leaves_qty, state: o.state, cancelState: o.cancel_state });
    working.set(k, list);
  }

  // The grid is anchored at the best bid (else the best ask, the last trade, or a working order) and runs one row
  // further up when both sides are present, so the spread sits in the middle.
  const center = bestBid ?? bestAsk ?? (quote?.last ? priceKey(quote.last) : null) ?? [...working.keys()][0] ?? null;

  const keys = new Set<string>([...bidMap.keys(), ...askMap.keys(), ...working.keys()]);
  const tick = center ? tickFor(center, instrument.tick_rules) : null;
  if (center && tick) {
    const anchor = floorToTick(center, tick);
    const up = bestBid && bestAsk ? halfRows + 1 : halfRows;
    for (let k = -halfRows; anchor && k <= up; k += 1) {
      const p = stepPrice(anchor, tick, k);
      const key = p ? priceKey(p) : null;
      if (key && (cmpExact(key, '0') ?? 0) > 0) keys.add(key);
    }
  }
  const rows = [...keys]
    .sort((a, b) => cmpExact(b, a) ?? 0)
    .map((price) => ({
      price,
      bidSize: bidMap.get(price)?.size ?? null,
      askSize: askMap.get(price)?.size ?? null,
      hasBid: bidMap.has(price),
      hasAsk: askMap.has(price),
      bestBid: price === bestBid,
      bestAsk: price === bestAsk,
      working: working.get(price) ?? [],
    }));
  const reason = basis === 'NONE'
    ? quote
      ? 'The quote has no bid or ask.'
      : 'No depth or level-1 quote for this security from the market-data sources (not provided, not entitled, or not subscribed).'
    : null;
  return { rows, basis, tick, source, dataType, reason };
}

/** Side a click stages: the bid column buys at that price, the ask column sells. */
export function sideForColumn(column: 'bid' | 'ask'): 'BUY' | 'SELL' {
  return column === 'bid' ? 'BUY' : 'SELL';
}
