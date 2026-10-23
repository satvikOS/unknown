/**
 * Orders workspace model: routes, order-state classification, cancel eligibility, hold summaries and
 * timeline summaries. Pure functions (unit tested); the core remains the authority for every state.
 */
import { exactNs, isNsStringSibling } from '../../api/nsStrings';
import type { EventRecord, Order, OrderDetail, OrderHold, OutboxRow } from '../../api/types';

/** GET /api/orders/{id} (api/queries.py order_detail); the shared type now carries every named field. */
export type OrderDetailRecord = OrderDetail;

// ------------------------------------------------------------------ routes

/** `#/orders`, `#/orders/~executions`, `#/orders/~new[/<instrument_id>]`, `#/orders/<order_id>`. */
export type OrdersRoute =
  | { view: 'blotter' }
  | { view: 'executions' }
  | { view: 'ticket'; instrumentId: string | null }
  | { view: 'detail'; orderId: string };

/**
 * View segments start with "~", a character the core never allows in an identifier
 * (domain/ids.py), so they can never collide with an order id.
 */
export const EXECUTIONS_SEGMENT = '~executions';
export const TICKET_SEGMENT = '~new';

export function parseOrdersRoute(rest: readonly string[]): OrdersRoute {
  const first = rest[0];
  if (!first) return { view: 'blotter' };
  if (first === EXECUTIONS_SEGMENT) return { view: 'executions' };
  if (first === TICKET_SEGMENT) return { view: 'ticket', instrumentId: rest[1] || null };
  return { view: 'detail', orderId: first };
}

export function ordersRest(route: OrdersRoute): string[] {
  switch (route.view) {
    case 'blotter':
      return [];
    case 'executions':
      return [EXECUTIONS_SEGMENT];
    case 'ticket':
      return route.instrumentId ? [TICKET_SEGMENT, route.instrumentId] : [TICKET_SEGMENT];
    case 'detail':
      return [route.orderId];
  }
}

// ------------------------------------------------------------------ order states

/** core/state.py TERMINAL_ORDER_STATES */
export const TERMINAL_ORDER_STATES: readonly string[] = ['FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'];
/** api/queries.py orders(open_only=True) */
export const OPEN_ORDER_STATES: readonly string[] = ['RESERVED', 'SUBMIT_PENDING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN'];
/** Cancel states that mean a cancel was requested and may still be travelling to the broker. */
export const CANCEL_IN_FLIGHT: readonly string[] = ['REQUESTED', 'SENT'];
/** Outbox states of a cancel row the core counts as queued or in flight (orders.py _live_cancel_row). */
export const LIVE_OUTBOX_STATES: readonly string[] = ['PENDING', 'SENDING'];

export function isTerminalOrder(state: string): boolean {
  return TERMINAL_ORDER_STATES.includes(state);
}

export function isOpenOrder(state: string): boolean {
  return OPEN_ORDER_STATES.includes(state);
}

export interface CancelAvailability {
  allowed: boolean;
  /** Why not, or what the request will do. */
  reason: string;
}

/** A queued or in-flight cancel row in the order's outbox, if any. */
export function liveCancelRow(outbox: readonly Pick<OutboxRow, 'action' | 'state' | 'outbox_id'>[]): Pick<OutboxRow, 'action' | 'state' | 'outbox_id'> | null {
  return outbox.find((b) => b.action === 'CANCEL' && LIVE_OUTBOX_STATES.includes(b.state)) ?? null;
}

/**
 * Whether the owner can usefully send `request_cancel`, mirroring core/handlers/orders.py
 * cancel_order: a terminal order, an order whose cancel the broker acknowledged, and an order with a
 * cancel row queued or being sent are no-ops in the core, so the control is not offered for them.
 *
 * With the order's `outbox` (detail view) the check is exact. Without it (blotter rows) a REQUESTED
 * or SENT cancel state is treated as in flight; the detail view can re-issue a cancel that was lost.
 */
export function cancelAvailability(
  order: Pick<Order, 'state' | 'cancel_state'>,
  outbox?: readonly Pick<OutboxRow, 'action' | 'state' | 'outbox_id'>[],
): CancelAvailability {
  if (isTerminalOrder(order.state)) return { allowed: false, reason: `The order is ${order.state}; there is nothing to cancel.` };
  if (order.cancel_state === 'ACKED') {
    return { allowed: false, reason: 'The broker acknowledged a cancel; waiting for it to confirm the order is cancelled.' };
  }
  if (outbox) {
    const live = liveCancelRow(outbox);
    if (live) return { allowed: false, reason: `A cancel is ${live.state === 'PENDING' ? 'queued' : 'being sent'} (outbox ${live.outbox_id}).` };
    if (CANCEL_IN_FLIGHT.includes(order.cancel_state) || order.cancel_state === 'UNKNOWN') {
      return {
        allowed: true,
        reason: `The earlier cancel (${order.cancel_state}) is no longer queued or in flight, so requesting again re-issues it. The reservation stays until the broker confirms.`,
      };
    }
  } else if (CANCEL_IN_FLIGHT.includes(order.cancel_state)) {
    return { allowed: false, reason: `A cancel is in flight (${order.cancel_state}). Open the order to see its outbox; a lost cancel can be re-issued there.` };
  }
  if (order.state === 'RESERVED') {
    return {
      allowed: true,
      reason: 'If the order has not been sent yet, the core cancels it locally and releases its reservation; if sending has begun, the cancel goes to the broker.',
    };
  }
  if (order.state === 'UNKNOWN') {
    return { allowed: true, reason: 'The broker outcome is unknown. The cancel is sent, and the reservation stays until the broker confirms or reconciliation resolves the order.' };
  }
  return { allowed: true, reason: 'The cancel goes to the broker. The reservation stays in place until the broker confirms the cancel.' };
}

/** Owner-facing cancel state (NONE means no cancel has been requested). */
export function cancelStateLabel(cancelState: string): string {
  return cancelState === 'NONE' ? 'none' : cancelState;
}

// ------------------------------------------------------------------ holds

export interface HoldSummary {
  active: OrderHold[];
  released: OrderHold[];
}

export function summarizeHolds(holds: readonly OrderHold[] | null | undefined): HoldSummary {
  const list = holds ?? [];
  return { active: list.filter((h) => h.status === 'ACTIVE'), released: list.filter((h) => h.status !== 'ACTIVE') };
}

// ------------------------------------------------------------------ timeline

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

function scalarText(v: unknown): string | null {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
  return null;
}

/**
 * One-line summary of an event's `info` for the order timeline. The pretrade decision is summarised
 * (the full check list has its own table); other scalar fields are listed as key=value in a stable
 * order; nested values are counted, not dropped silently.
 */
export function summarizeEventInfo(e: Pick<EventRecord, 'info'>): string {
  const info = isRecord(e.info) ? e.info : {};
  const parts: string[] = [];
  const decision = info.decision;
  if (isRecord(decision) && typeof decision.code === 'string') {
    const checks = Array.isArray(decision.checks) ? decision.checks.length : 0;
    parts.push(`pretrade ${decision.code} (${checks} checks)`);
  }
  if (typeof info.required_cash === 'string') parts.push(`required cash ${info.required_cash}`);
  const keys = Object.keys(info)
    .filter((k) => !['decision', 'quote', 'required_cash'].includes(k) && !isNsStringSibling(k, info))
    .sort();
  let nested = 0;
  for (const k of keys) {
    const text = exactNs(info, k) ?? scalarText(info[k]); // a *_ns value exactly from the API's *_ns_str
    if (text !== null) parts.push(`${k}=${text}`);
    else if (info[k] !== null && info[k] !== undefined && info[k] !== '') nested += 1;
  }
  if (isRecord(info.quote)) parts.push('quote recorded');
  if (nested) parts.push(`${nested} structured field${nested === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

// ------------------------------------------------------------------ blotter filtering

/** Client-side text filter over identifiers the owner types (order, broker id, instrument, symbol). */
export function matchesOrderText(order: Pick<Order, 'order_id' | 'broker_order_id' | 'instrument_id' | 'session_id'>, symbol: string | null, text: string): boolean {
  const q = text.trim().toLowerCase();
  if (!q) return true;
  const hay = [order.order_id, order.broker_order_id ?? '', order.instrument_id, order.session_id, symbol ?? ''].join(' ').toLowerCase();
  return q.split(/\s+/).every((t) => hay.includes(t));
}
