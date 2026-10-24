/**
 * Cancel/replace from the blotter (replace.ts): request_cancel first; the replacement intent (a new order id) is
 * sent once, automatically, only after the core shows the old order CANCELED with nothing filled since the request.
 * The dialog stays open on the outcome; closing it before the cancel confirms abandons the replacement, never the
 * cancel.
 */
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { encodePathSegment } from '../../api/client';
import type { Order } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { useAuth, useQuery, useSendCommand, useTrackedCommand } from '../../state/react';
import { Modal } from '../../ui/Modal';
import { StatusPill } from '../../ui/primitives';
import { cancelAvailability } from './model';
import { OutcomeLine } from './PreviewPanel';
import { replaceState, type ReplaceRequest } from './replace';
import { buildIntentPayload, deadlineNsFrom, DEFAULT_DEADLINE_SECONDS, EMPTY_TICKET, isPositiveDecimal, newOrderId } from './ticket';

/** Orders a replace can rebuild: the owner's own entries and exits (a disposal or a wind-down keeps its own flow). */
export function replaceable(o: Pick<Order, 'purpose' | 'principal_role'>): boolean {
  return (o.purpose === 'ENTRY' || o.purpose === 'EXIT') && o.principal_role === 'OWNER';
}

export function ReplaceDialog({ order, onClose }: { order: Order | null; onClose: () => void }) {
  const send = useSendCommand();
  const auth = useAuth();
  const sessions = useQuery('/api/sessions');
  const orders = useQuery('/api/orders', { state: 'open' });
  const [qty, setQty] = useState('');
  const [limit, setLimit] = useState('');
  const [req, setReq] = useState<ReplaceRequest | null>(null);
  const [cancelId, setCancelId] = useState<string | null>(null);
  const [newIds, setNewIds] = useState<{ commandId: string; orderId: string } | null>(null);
  const sentRef = useRef(false);
  const open = order !== null;

  useEffect(() => {
    if (order) {
      setQty(order.leaves_qty);
      setLimit(order.limit_price);
      setReq(null);
      setCancelId(null);
      setNewIds(null);
      sentRef.current = false;
    }
  }, [open, order?.order_id]);

  const cancel = useTrackedCommand(cancelId);
  const replacement = useTrackedCommand(newIds?.commandId);
  // The open-orders list drops the order once it is CANCELED; the detail query then reports its final state.
  const live = (orders.data ?? []).find((o) => o.order_id === order?.order_id);
  const detail = useQuery(order && req && !live ? `/api/orders/${encodePathSegment(order.order_id)}` : null);
  const current = live ?? detail.data;
  const st = req ? replaceState(req, current, cancel, replacement) : null;

  useEffect(() => {
    if (!order || !req || st?.phase !== 'READY' || sentRef.current) return;
    const session = (sessions.data ?? []).find((s) => s.session_id === order.session_id);
    if (!session) return;
    sentRef.current = true; // exactly once: a READY seen again never sends a second replacement
    const orderId = newOrderId();
    const payload = buildIntentPayload(
      { ...EMPTY_TICKET, sessionId: session.session_id, instrumentId: order.instrument_id, side: order.side, quantity: req.quantity, limitPrice: req.limitPrice, timeInForce: order.time_in_force },
      session,
      orderId,
      deadlineNsFrom(Date.now() + auth.clockOffsetMs, DEFAULT_DEADLINE_SECONDS),
    );
    void send('submit_intent', payload, { label: `Replace: ${payload.side} ${payload.quantity} ${payload.instrument_id} @ ${payload.limit_price}` }).then((t) =>
      setNewIds({ commandId: t.commandId, orderId }),
    );
  }, [st?.phase, order, req, sessions.data, auth.clockOffsetMs, send]);

  if (!order) return null;
  const availability = cancelAvailability(order);
  const valid = isPositiveDecimal(qty) && isPositiveDecimal(limit);
  const can = availability.allowed && replaceable(order) && valid;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!can || req) return;
    const r: ReplaceRequest = { orderId: order.order_id, cumAtRequest: order.cum_qty, quantity: qty.trim(), limitPrice: limit.trim() };
    setReq(r);
    const t = await send('request_cancel', { order_id: order.order_id, reason: `replace with ${r.quantity} @ ${r.limitPrice}` }, { label: `Cancel ${order.side} ${order.instrument_id} order to replace it` });
    setCancelId(t.commandId);
  };

  return (
    <Modal open={open} onClose={onClose} labelledBy="ord-replace-title" describedBy="ord-replace-desc">
      <form onSubmit={(e) => void submit(e)}>
        <header>
          <h2 id="ord-replace-title">Cancel and replace</h2>
        </header>
        <div className="modal-body">
          <p style={{ marginTop: 0 }}>
            Order <span className="cmd-id">{order.order_id}</span> · {order.side} {order.instrument_id} · leaves <span className="num">{order.leaves_qty}</span> @{' '}
            <span className="num">{order.limit_price}</span> · <strong>{order.state}</strong>
          </p>
          <p id="ord-replace-desc" className="callout" style={{ ['--tone' as string]: 'var(--warn)' }}>
            The core has no amend: this sends a cancel, waits for the broker to confirm it, then sends a new order with a new id. If anything fills meanwhile, or
            the cancel is refused or its outcome is unknown, the new order is not sent.
          </p>
          {!replaceable(order) ? <p className="form-error">Only the owner’s own ENTRY and EXIT orders can be replaced here; cancel this one instead.</p> : null}
          {!availability.allowed ? <p className="muted">{availability.reason}</p> : null}
          {req ? (
            <>
              {cancelId ? <CommandStatus commandId={cancelId} /> : null}
              {st ? (
                <p role="status">
                  <StatusPill label={st.phase.replace('_', ' ')} tone={st.phase === 'ABORTED' ? 'bad' : st.phase === 'SENT' ? 'ok' : 'info'} compact /> {st.reason}
                </p>
              ) : null}
              {newIds ? <OutcomeLine commandId={newIds.commandId} orderId={newIds.orderId} /> : null}
            </>
          ) : (
            <div className="ord-two">
              <div className="field">
                <label htmlFor="ord-replace-qty">New quantity</label>
                <input id="ord-replace-qty" className="input num" inputMode="decimal" autoComplete="off" value={qty} onChange={(e) => setQty(e.target.value)} aria-invalid={!isPositiveDecimal(qty) || undefined} />
              </div>
              <div className="field">
                <label htmlFor="ord-replace-limit">New limit</label>
                <input id="ord-replace-limit" className="input num" inputMode="decimal" autoComplete="off" value={limit} onChange={(e) => setLimit(e.target.value)} aria-invalid={!isPositiveDecimal(limit) || undefined} />
              </div>
            </div>
          )}
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
          {req ? null : (
            <button type="submit" className="btn primary" disabled={!can}>
              Cancel and replace
            </button>
          )}
        </div>
      </form>
    </Modal>
  );
}
