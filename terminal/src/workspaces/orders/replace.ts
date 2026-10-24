/**
 * Cancel/replace from the blotter. The core has no amend command, so a replace is two commands: request_cancel
 * for the working order, then, only once the core shows that order CANCELED with nothing filled since the request,
 * a new submit_intent with a new order id. The reservation of the old order is released by the core when the broker
 * confirms the cancel (ADR-0009), never by the terminal. A fill, a refused cancel or an unknown outcome stops the
 * replace: the new intent is not sent, and the owner decides.
 */
import type { Order } from '../../api/types';
import type { TrackedCommand } from '../../state/commands';
import { cmpExact } from './exact';

export interface ReplaceRequest {
  orderId: string;
  /** cum_qty when the replace was requested: any fill after it stops the replacement. */
  cumAtRequest: string;
  quantity: string;
  limitPrice: string;
}

export type ReplacePhase = 'CANCEL_SENDING' | 'AWAITING_CANCEL' | 'READY' | 'SENT' | 'ABORTED';

export interface ReplaceState {
  phase: ReplacePhase;
  reason: string;
}

export function replaceState(
  req: ReplaceRequest,
  order: Pick<Order, 'state' | 'cum_qty' | 'cancel_state'> | undefined,
  cancel: TrackedCommand | undefined,
  replacement: TrackedCommand | undefined,
): ReplaceState {
  if (replacement) return { phase: 'SENT', reason: 'The replacement intent was sent as a new order.' };
  if (!cancel || cancel.status === 'sending') return { phase: 'CANCEL_SENDING', reason: 'Sending the cancel request.' };
  if (cancel.status === 'rejected') {
    return { phase: 'ABORTED', reason: `The core refused the cancel (${cancel.rejection?.code ?? 'REJECTED'}); the replacement was not sent.` };
  }
  if (cancel.status === 'cancelled') return { phase: 'ABORTED', reason: 'The step-up prompt was dismissed; no cancel was sent and nothing was replaced.' };
  if (cancel.status === 'failed') {
    return {
      phase: 'ABORTED',
      reason: cancel.outcomeUnknown
        ? 'The cancel request’s outcome is UNKNOWN. The replacement was not sent; look the command up before acting.'
        : 'The cancel request was not executed; the replacement was not sent.',
    };
  }
  if (!order) return { phase: 'AWAITING_CANCEL', reason: 'Waiting for the order record.' };
  if (cmpExact(order.cum_qty, req.cumAtRequest) !== 0) {
    return { phase: 'ABORTED', reason: `The order filled (cumulative ${order.cum_qty}) while the cancel was pending; the replacement was not sent. Re-enter it if you still want it.` };
  }
  if (order.state === 'CANCELED') return { phase: 'READY', reason: 'The broker confirmed the cancel; sending the replacement.' };
  if (['FILLED', 'REJECTED', 'EXPIRED'].includes(order.state)) {
    return { phase: 'ABORTED', reason: `The order ended ${order.state}; the replacement was not sent.` };
  }
  return { phase: 'AWAITING_CANCEL', reason: `Waiting for the broker to confirm the cancel (order ${order.state}, cancel ${order.cancel_state}). The reservation stays until then.` };
}
