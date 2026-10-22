/**
 * Controlled cancel (book Ch. 22 "Orders: controlled cancel"). Sends `request_cancel` through the
 * command tracker and shows the durable command id and its progress. Nothing changes on screen until
 * the core records the request and the state stream reports it (no optimistic update).
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import type { Order, OutboxRow } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { useCommands, useSendCommand } from '../../state/react';
import type { TrackedCommand } from '../../state/commands';
import { Modal } from '../../ui/Modal';
import { cancelAvailability } from './model';

export const RESERVATION_NOTE =
  'A cancel request does not release the reservation. The cash or quantity held for this order stays held until the broker confirms the cancel (or reconciliation proves the order is gone). Fills can still arrive before the broker cancels.';

/** Latest `request_cancel` the terminal sent for an order in this browser session, if any. */
export function useLatestCancel(orderId: string | null | undefined): TrackedCommand | undefined {
  const commands = useCommands();
  return useMemo(
    () =>
      orderId
        ? commands.find((c) => c.kind === 'request_cancel' && (c.payload as { order_id?: unknown } | null)?.order_id === orderId)
        : undefined,
    [commands, orderId],
  );
}

export interface CancelDialogProps {
  order: Pick<Order, 'order_id' | 'state' | 'cancel_state' | 'side' | 'leaves_qty' | 'instrument_id'> | null;
  /** The order's outbox when known (detail view): makes the eligibility check exact. */
  outbox?: readonly OutboxRow[] | undefined;
  onClose: () => void;
}

export function CancelDialog({ order, outbox, onClose }: CancelDialogProps) {
  const send = useSendCommand();
  const [reason, setReason] = useState('');
  const [commandId, setCommandId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const reasonRef = useRef<HTMLInputElement>(null);
  const open = order !== null;

  useEffect(() => {
    if (open) {
      setReason('');
      setCommandId(null);
      setBusy(false);
    }
  }, [open, order?.order_id]);

  if (!order) return null;
  const availability = cancelAvailability(order, outbox);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || commandId || !availability.allowed) return;
    setBusy(true);
    const payload = reason.trim() ? { order_id: order.order_id, reason: reason.trim() } : { order_id: order.order_id };
    // The label is shown upper-cased in places, so it names the order by content; the id is in the payload and timeline.
    const t = await send('request_cancel', payload, { label: `Cancel ${order.side} ${order.instrument_id} order` });
    setCommandId(t.commandId);
    setBusy(false);
  };

  return (
    <Modal open={open} onClose={onClose} labelledBy="ord-cancel-title" describedBy="ord-cancel-desc" initialFocus={reasonRef}>
      <form onSubmit={(e) => void submit(e)}>
        <header>
          <h2 id="ord-cancel-title">Request cancel</h2>
        </header>
        <div className="modal-body">
          <p style={{ marginTop: 0 }}>
            Order <span className="cmd-id">{order.order_id}</span> · {order.side} {order.instrument_id} · leaves <span className="num">{order.leaves_qty}</span> · state{' '}
            <strong>{order.state}</strong>
          </p>
          <div id="ord-cancel-desc" className="callout" style={{ ['--tone' as string]: 'var(--warn)' }}>
            <strong>The reservation is not released yet.</strong> {RESERVATION_NOTE}
          </div>
          <p className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
            {availability.reason}
          </p>
          {commandId ? (
            <CommandStatus commandId={commandId} />
          ) : (
            <div className="field">
              <label htmlFor="ord-cancel-reason">Reason (optional, recorded with the request)</label>
              <input
                ref={reasonRef}
                id="ord-cancel-reason"
                className="input"
                value={reason}
                maxLength={200}
                onChange={(e) => setReason(e.target.value)}
                disabled={!availability.allowed}
                autoComplete="off"
              />
            </div>
          )}
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            {commandId ? 'Close' : 'Keep order'}
          </button>
          {commandId ? null : (
            <button type="submit" className="btn primary" disabled={!availability.allowed || busy}>
              {busy ? 'Sending…' : 'Request cancel'}
            </button>
          )}
        </div>
      </form>
    </Modal>
  );
}
