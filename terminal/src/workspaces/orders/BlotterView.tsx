/**
 * Order blotter: GET /api/orders (open or all, optionally one session), newest first as the API
 * returns them. Every column is a field of the core's order row; nothing is derived in floating point.
 */
import { useMemo, useState, type ReactNode } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import type { Instrument, Order } from '../../api/types';
import { useQuery } from '../../state/react';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, Qty, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { orderStateTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { CancelDialog } from './CancelDialog';
import { cancelAvailability, cancelStateLabel, matchesOrderText } from './model';
import { HoldsCell, InstrumentCell, SideText, type InstrumentData } from './parts';
import { ReplaceDialog, replaceable } from './ReplaceDialog';

export const BLOTTER_LIMIT = 500;

function columns(instruments: ReadonlyMap<string, Instrument>, onCancel: (o: Order) => void, onReplace: (o: Order) => void): Column<Order>[] {
  return [
    {
      id: 'order',
      header: 'Order',
      sortValue: (o) => o.order_id,
      cell: (o) => (
        <a href={routeHref('orders', o.order_id)} className="cmd-id">
          {o.order_id}
        </a>
      ),
    },
    { id: 'accepted', header: 'Accepted', sortKind: 'number', sortValue: (o) => o.accepted_at_ns, cell: (o) => <Timestamp ns={o.accepted_at_ns} nsStr={nsStrOf(o, 'accepted_at_ns')} /> },
    {
      id: 'session',
      header: 'Session',
      sortValue: (o) => o.session_id,
      cell: (o) => (
        <WorkspaceLink to="sessions" rest={[o.session_id]}>
          <ShortId value={o.session_id} head={16} />
        </WorkspaceLink>
      ),
    },
    {
      id: 'inst',
      header: 'Instrument',
      sortValue: (o) => instruments.get(o.instrument_id)?.symbol ?? o.instrument_id,
      cell: (o) => <InstrumentCell id={o.instrument_id} instrument={instruments.get(o.instrument_id)} />,
    },
    { id: 'side', header: 'Side', sortValue: (o) => o.side, cell: (o) => <SideText side={o.side} /> },
    { id: 'purpose', header: 'Purpose', sortValue: (o) => o.purpose, cell: (o) => o.purpose },
    {
      id: 'state',
      header: 'State',
      sortValue: (o) => o.state,
      cell: (o) => <StatusPill label={o.state} tone={orderStateTone(o.state)} detail={o.reason ?? undefined} compact />,
    },
    { id: 'qty', header: 'Qty', numeric: true, sortKind: 'decimal', sortValue: (o) => o.quantity, cell: (o) => <Qty value={o.quantity} /> },
    { id: 'cum', header: 'Cum', description: 'Cumulative filled quantity', numeric: true, sortKind: 'decimal', sortValue: (o) => o.cum_qty, cell: (o) => <Qty value={o.cum_qty} /> },
    { id: 'leaves', header: 'Leaves', description: 'Quantity still working', numeric: true, sortKind: 'decimal', sortValue: (o) => o.leaves_qty, cell: (o) => <Qty value={o.leaves_qty} /> },
    {
      id: 'limit',
      header: 'Limit',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (o) => o.limit_price,
      cell: (o) => <Money amount={o.limit_price} currency={o.currency} />,
    },
    {
      id: 'avg',
      header: 'Avg price',
      description: 'Average fill price (none until a fill)',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (o) => o.avg_price,
      cell: (o) => <Money amount={o.avg_price} currency={o.currency} unavailable="no fills" />,
    },
    {
      id: 'fee-est',
      header: 'Fees (estimated)',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (o) => o.fees_estimated,
      cell: (o) => <Money amount={o.fees_estimated} currency={o.currency} kind="estimated" />,
    },
    {
      id: 'fee-final',
      header: 'Fees (final)',
      description: 'Fees the broker reported as final',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (o) => o.fees_final,
      cell: (o) => <Money amount={o.fees_final} currency={o.currency} kind="realized" />,
    },
    {
      id: 'cancel',
      header: 'Cancel state',
      sortValue: (o) => o.cancel_state,
      cell: (o) =>
        o.cancel_state === 'NONE' ? (
          <span className="muted">{cancelStateLabel(o.cancel_state)}</span>
        ) : (
          <StatusPill label={o.cancel_state} tone={o.cancel_state === 'CONFIRMED' ? 'neutral' : o.cancel_state === 'REJECTED' || o.cancel_state === 'UNKNOWN' ? 'bad' : 'warn'} compact />
        ),
    },
    {
      id: 'broker',
      header: 'Broker id',
      sortValue: (o) => o.broker_order_id,
      cell: (o) => (o.broker_order_id ? <span className="cmd-id">{o.broker_order_id}</span> : <span className="muted">not assigned</span>),
    },
    {
      id: 'holds',
      header: 'Holds',
      description: 'Reservations for this order (cash for buys, quantity for sells)',
      cell: (o) => <HoldsCell holds={o.holds} />,
    },
    {
      id: 'action',
      header: 'Action',
      cell: (o) => {
        const a = cancelAvailability(o);
        return a.allowed ? (
          <span className="ord-inline">
            <button type="button" className="btn small" onClick={() => onCancel(o)} title={a.reason}>
              Cancel…
            </button>
            {replaceable(o) && o.state !== 'UNKNOWN' ? (
              <button type="button" className="btn small" onClick={() => onReplace(o)} title="Cancel this order and, once the broker confirms, send a new one">
                Replace…
              </button>
            ) : null}
          </span>
        ) : (
          <span className="muted" title={a.reason}>
            —<span className="sr-only">{a.reason}</span>
          </span>
        );
      },
    },
  ];
}

export function BlotterView({ tabs, instruments: inst, onOpen }: { tabs: ReactNode; instruments: InstrumentData; onOpen: (orderId: string) => void }) {
  const instruments = inst.map;
  const [scope, setScope] = useState<'open' | 'all'>('open');
  const [sessionId, setSessionId] = useState('');
  const [text, setText] = useState('');
  const [cancelling, setCancelling] = useState<Order | null>(null);
  const sessions = useQuery('/api/sessions');
  const orders = useQuery('/api/orders', {
    ...(scope === 'open' ? { state: 'open' as const } : {}),
    ...(sessionId ? { session: sessionId } : {}),
    limit: BLOTTER_LIMIT,
  });
  const all = orders.data ?? [];
  const rows = useMemo(
    () => all.filter((o) => matchesOrderText(o, instruments.get(o.instrument_id)?.symbol ?? null, text)),
    [all, instruments, text],
  );
  const [replacing, setReplacing] = useState<Order | null>(null);
  const cols = useMemo(() => columns(instruments, setCancelling, setReplacing), [instruments]);
  const truncated = all.length >= BLOTTER_LIMIT;
  const unknown = all.filter((o) => o.state === 'UNKNOWN').length;

  return (
    <WorkspaceFrame
      title="Orders"
      description="Intent, broker status, fills and cancel state, read from the core’s order records."
      queries={[orders, sessions, inst.query]}
    >
      {tabs}
      <div className="ord-filters" role="group" aria-label="Blotter filters">
        <fieldset className="ord-segmented">
          <legend className="sr-only">Orders to show</legend>
          <label>
            <input type="radio" name="ord-scope" value="open" checked={scope === 'open'} onChange={() => setScope('open')} />
            <span>Open</span>
          </label>
          <label>
            <input type="radio" name="ord-scope" value="all" checked={scope === 'all'} onChange={() => setScope('all')} />
            <span>All</span>
          </label>
        </fieldset>
        <div className="field">
          <label htmlFor="ord-session">Session</label>
          <select id="ord-session" className="input" value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
            <option value="">All sessions</option>
            {(sessions.data ?? []).map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id} ({s.state})
              </option>
            ))}
          </select>
        </div>
        <div className="field ord-grow">
          <label htmlFor="ord-text">Find</label>
          <input
            id="ord-text"
            className="input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="order id, broker id, symbol…"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
      </div>
      <QueryError what="Orders" error={orders.error} />
      <QueryError what="Sessions" error={sessions.error} />
      {unknown ? (
        <div className="callout ord-gap" role="status" style={{ ['--tone' as string]: 'var(--bad)' }}>
          <strong>
            {unknown} order{unknown === 1 ? '' : 's'} with an unknown broker outcome.
          </strong>{' '}
          Their reservations stay in place and no order is retried until reconciliation resolves them.
        </div>
      ) : null}
      <section className="panel" aria-labelledby="ord-blotter-h">
        <header>
          <h2 id="ord-blotter-h">{scope === 'open' ? 'Open orders' : 'All orders'}</h2>
          <span className="muted ord-small">
            {rows.length === all.length ? `${all.length}` : `${rows.length} of ${all.length}`} shown · newest first
            {truncated ? ` · the API returns at most ${BLOTTER_LIMIT}; older orders are not listed` : ''}
          </span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption={`${scope === 'open' ? 'Open orders' : 'All orders'}${sessionId ? ` in session ${sessionId}` : ''}`}
            captionHidden
            columns={cols}
            rows={rows}
            rowKey={(o) => o.order_id}
            onRowActivate={(o) => onOpen(o.order_id)}
            activateLabel="open the order"
            emptyText={
              orders.status === 'loading'
                ? 'Loading…'
                : all.length === 0
                  ? scope === 'open'
                    ? 'No open orders.'
                    : 'No orders recorded.'
                  : 'No order matches the search.'
            }
          />
        </div>
        <div className="panel-note">
          Enter or double-click a row to open the order. Cancel sends <code className="mono">request_cancel</code>; it never releases a reservation
          before the broker confirms.
        </div>
      </section>
      <CancelDialog order={cancelling} onClose={() => setCancelling(null)} />
      <ReplaceDialog order={replacing} onClose={() => setReplacing(null)} />
    </WorkspaceFrame>
  );
}
