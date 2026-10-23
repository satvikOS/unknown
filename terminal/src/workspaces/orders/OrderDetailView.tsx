/**
 * One order: GET /api/orders/{id} — the order row, its holds, outbox rows, executions (with fee
 * status) and event timeline, plus the pretrade decision the core recorded when it accepted it.
 */
import { useMemo, useState, type ReactNode } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import { ApiError, encodePathSegment } from '../../api/client';
import type { EventRecord, OrderHoldFull, OutboxRow } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, Qty, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { orderStateTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { CancelDialog, RESERVATION_NOTE, useLatestCancel } from './CancelDialog';
import { decisionFromTimeline } from './decision';
import { executionColumns } from './ExecutionsView';
import { cancelAvailability, cancelStateLabel, isOpenOrder, summarizeEventInfo, summarizeHolds, type OrderDetailRecord } from './model';
import { DecisionTable, HoldValue, InstrumentCell, QuoteLine, SideText, type InstrumentData } from './parts';

const holdColumns: Column<OrderHoldFull>[] = [
  { id: 'id', header: 'Hold', sortValue: (h) => h.hold_id, cell: (h) => <span className="cmd-id">{h.hold_id}</span> },
  { id: 'owner', header: 'Owner bucket', sortValue: (h) => h.owner, cell: (h) => <span className="cmd-id">{h.owner}</span> },
  { id: 'kind', header: 'Kind', sortValue: (h) => h.kind, cell: (h) => h.kind },
  { id: 'amount', header: 'Amount and status', cell: (h) => <HoldValue hold={h} /> },
];

function outboxResult(r: unknown) {
  if (!r || typeof r !== 'object') return <span className="muted">no result yet</span>;
  const x = r as { status?: unknown; broker_order_id?: unknown; reason?: unknown; at_ns?: unknown };
  return (
    <span className="ord-inline">
      {typeof x.status === 'string' ? (
        <StatusPill label={x.status} tone={x.status === 'ACCEPTED' ? 'ok' : x.status === 'REJECTED' ? 'bad' : 'warn'} compact />
      ) : null}
      {typeof x.broker_order_id === 'string' && x.broker_order_id ? <span className="cmd-id">{x.broker_order_id}</span> : null}
      {typeof x.reason === 'string' && x.reason ? <span>{x.reason}</span> : null}
      {typeof x.at_ns === 'number' ? <Timestamp ns={x.at_ns} nsStr={nsStrOf(x, 'at_ns')} withMs /> : null}
    </span>
  );
}

const outboxColumns: Column<OutboxRow>[] = [
  { id: 'id', header: 'Outbox row', sortValue: (b) => b.outbox_id, cell: (b) => <span className="cmd-id">{b.outbox_id}</span> },
  { id: 'action', header: 'Action', sortValue: (b) => b.action, cell: (b) => b.action },
  {
    id: 'state',
    header: 'State',
    sortValue: (b) => b.state,
    cell: (b) => (
      <StatusPill
        label={b.state}
        tone={b.state === 'DONE' ? 'ok' : b.state === 'UNKNOWN' ? 'bad' : b.state === 'SKIPPED' ? 'neutral' : 'warn'}
        compact
      />
    ),
  },
  { id: 'attempts', header: 'Attempts', numeric: true, sortKind: 'number', sortValue: (b) => b.attempts, cell: (b) => b.attempts },
  {
    id: 'last',
    header: 'Last attempt',
    sortKind: 'number',
    sortValue: (b) => b.last_attempt_ns,
    cell: (b) => <Timestamp ns={b.last_attempt_ns} nsStr={nsStrOf(b, 'last_attempt_ns')} withMs never="not attempted" />,
  },
  { id: 'result', header: 'Result', wrap: true, cell: (b) => outboxResult(b.result) },
];

const timelineColumns: Column<EventRecord>[] = [
  { id: 'seq', header: 'Seq', numeric: true, sortKind: 'number', sortValue: (e) => e.seq, cell: (e) => e.seq },
  { id: 'time', header: 'Recorded', sortKind: 'number', sortValue: (e) => e.recorded_time_ns, cell: (e) => <Timestamp ns={e.recorded_time_ns} nsStr={nsStrOf(e, 'recorded_time_ns')} withMs /> },
  { id: 'type', header: 'Event', sortValue: (e) => e.type, cell: (e) => <code className="mono">{e.type}</code> },
  { id: 'principal', header: 'Principal', sortValue: (e) => e.principal_id, cell: (e) => <span className="cmd-id">{e.principal_id}</span> },
  { id: 'command', header: 'Command', sortValue: (e) => e.command_id, cell: (e) => <ShortId value={e.command_id} head={22} /> },
  { id: 'info', header: 'Recorded detail', wrap: true, cell: (e) => summarizeEventInfo(e) || <span className="muted">—</span> },
  { id: 'changed', header: 'Changed', wrap: true, cell: (e) => <span className="muted">{e.changed_sections.join(', ')}</span> },
];

export function OrderDetailView({
  tabs,
  orderId,
  instruments: inst,
  onBack,
}: {
  tabs: ReactNode;
  orderId: string;
  instruments: InstrumentData;
  onBack: () => void;
}) {
  const instruments = inst.map;
  // The API splits the path without percent-decoding, so an id containing "/" cannot be addressed.
  const addressable = !orderId.includes('/');
  const detail = useQuery(addressable ? `/api/orders/${encodePathSegment(orderId)}` : null);
  const policy = useQuery('/api/policy/active');
  const [cancelOpen, setCancelOpen] = useState(false);
  const latestCancel = useLatestCancel(orderId);
  // The wire shape is api/queries.py order_detail; see OrderDetailRecord for why it is retyped here.
  const o = detail.data as OrderDetailRecord | undefined;
  const recorded = useMemo(() => decisionFromTimeline(o?.timeline), [o?.timeline]);
  const execCols = useMemo(() => executionColumns(instruments, { showOrder: false }), [instruments]);
  const holds = summarizeHolds(o?.holds);
  const notFound = detail.error instanceof ApiError && detail.error.status === 404;
  const quoteMaxAge = policy.data?.body.account_limits.quote_max_age_ms;

  const availability = o ? cancelAvailability(o, o.outbox) : null;
  const instrument = o ? instruments.get(o.instrument_id) : undefined;

  return (
    <WorkspaceFrame
      title={`Order ${orderId}`}
      description="The core’s order record with its holds, broker messages, fills, pretrade decision and events."
      queries={[detail, inst.query]}
    >
      {tabs}
      {!addressable ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)' }}>
          <strong>This order id contains “/”.</strong> The API does not decode path segments, so the order cannot be fetched by id. It is still listed in
          the blotter.
        </div>
      ) : notFound ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          <strong>No order with id {orderId}.</strong> The core has no order row with this id.{' '}
          <button type="button" className="btn small" onClick={onBack}>
            Back to the blotter
          </button>
        </div>
      ) : (
        <QueryError what={`Order ${orderId}`} error={detail.error} />
      )}
      {!o ? (
        notFound || !addressable ? null : <p className="muted">{detail.status === 'loading' ? 'Loading the order…' : 'No data.'}</p>
      ) : (
        <>
          <section className="panel ord-detail-head" aria-labelledby="ord-detail-h">
            <header>
              <h2 id="ord-detail-h">
                Order <span className="cmd-id">{o.order_id}</span>
              </h2>
              <span className="ord-inline">
                <StatusPill label={o.state} tone={orderStateTone(o.state)} />
                {o.cancel_state !== 'NONE' ? <StatusPill label={`cancel ${o.cancel_state}`} tone={o.cancel_state === 'CONFIRMED' ? 'neutral' : 'warn'} compact /> : null}
                {availability?.allowed ? (
                  <button type="button" className="btn small" onClick={() => setCancelOpen(true)}>
                    Request cancel…
                  </button>
                ) : null}
              </span>
            </header>
            <div className="panel-body">
              <div className="ord-summary">
                <div>
                  <span className="kv-label">Side</span>
                  <SideText side={o.side} /> <span className="muted">{o.purpose}</span>
                </div>
                <div>
                  <span className="kv-label">Instrument</span>
                  <InstrumentCell id={o.instrument_id} instrument={instrument} />
                </div>
                <div>
                  <span className="kv-label">Quantity / cum / leaves</span>
                  <span className="num">
                    <Qty value={o.quantity} /> / <Qty value={o.cum_qty} /> / <Qty value={o.leaves_qty} />
                  </span>
                </div>
                <div>
                  <span className="kv-label">Limit</span>
                  <Money amount={o.limit_price} currency={o.currency} />
                </div>
                <div>
                  <span className="kv-label">Avg price</span>
                  <Money amount={o.avg_price} currency={o.currency} unavailable="no fills" />
                </div>
                <div>
                  <span className="kv-label">Notional filled</span>
                  <Money amount={o.notional_filled} currency={o.currency} />
                </div>
              </div>
            </div>
            {availability && isOpenOrder(o.state) ? <div className="panel-note">{availability.reason}</div> : null}
          </section>

          {latestCancel ? (
            <div className="ord-gap">
              <CommandStatus commandId={latestCancel.commandId} />
            </div>
          ) : null}

          <div className="grid grid-2 section-gap">
            <section className="panel" aria-labelledby="ord-facts-h">
              <header>
                <h2 id="ord-facts-h">Order record</h2>
              </header>
              <div className="panel-body">
                <dl className="kv">
                  <dt>Session</dt>
                  <dd>
                    <WorkspaceLink to="sessions" rest={[o.session_id]}>
                      <span className="cmd-id">{o.session_id}</span>
                    </WorkspaceLink>
                  </dd>
                  <dt>Type / time in force</dt>
                  <dd>
                    {o.order_type} / {o.time_in_force}
                  </dd>
                  <dt>Previous state</dt>
                  <dd>{o.prev_state ?? <span className="muted">none</span>}</dd>
                  <dt>Cancel state</dt>
                  <dd>{cancelStateLabel(o.cancel_state)}</dd>
                  <dt>Reason</dt>
                  <dd>{o.reason ?? <span className="muted">none recorded</span>}</dd>
                  <dt>Fees (estimated)</dt>
                  <dd>
                    <Money amount={o.fees_estimated} currency={o.currency} kind="estimated" />
                  </dd>
                  <dt>Fees (final)</dt>
                  <dd>
                    <Money amount={o.fees_final} currency={o.currency} kind="realized" />
                  </dd>
                  <dt title="Fee allowance the core reserved on top of the notional for a buy">Fee envelope (reserved)</dt>
                  <dd>
                    <Money amount={o.fee_envelope} currency={o.currency} kind="held" />
                  </dd>
                  <dt>Broker order id</dt>
                  <dd>{o.broker_order_id ? <span className="cmd-id">{o.broker_order_id}</span> : <span className="muted">not assigned</span>}</dd>
                  <dt>Order reference</dt>
                  <dd className="cmd-id">{o.order_ref}</dd>
                  <dt>Accepted</dt>
                  <dd>
                    <Timestamp ns={o.accepted_at_ns} nsStr={nsStrOf(o, 'accepted_at_ns')} withMs />
                  </dd>
                  <dt>Submitted</dt>
                  <dd>
                    <Timestamp ns={o.submitted_at_ns} nsStr={nsStrOf(o, 'submitted_at_ns')} withMs never="not yet sent to the broker" />
                  </dd>
                  <dt>Terminal</dt>
                  <dd>
                    <Timestamp ns={o.terminal_at_ns} nsStr={nsStrOf(o, 'terminal_at_ns')} withMs never="not terminal" />
                  </dd>
                  <dt>Intent deadline</dt>
                  <dd>
                    <Timestamp ns={o.deadline_ns} nsStr={nsStrOf(o, 'deadline_ns')} />
                  </dd>
                  <dt>Principal</dt>
                  <dd>
                    <span className="cmd-id">{o.principal_id}</span> <span className="muted">({o.principal_role})</span>
                  </dd>
                  <dt>Strategy</dt>
                  <dd className="cmd-id">{o.strategy_id}</dd>
                  <dt>Policy</dt>
                  <dd>
                    <ShortId value={o.policy_hash} head={16} />
                  </dd>
                  <dt>Command</dt>
                  <dd className="cmd-id">{o.command_id}</dd>
                </dl>
              </div>
            </section>

            <section className="panel" aria-labelledby="ord-holds-h">
              <header>
                <h2 id="ord-holds-h">Holds</h2>
                <span className="muted ord-small">
                  {holds.active.length} held, {holds.released.length} released
                </span>
              </header>
              <div className="panel-body flush">
                <DataTable caption="Holds for this order" captionHidden columns={holdColumns} rows={o.holds} rowKey={(h) => h.hold_id} emptyText="No holds recorded." />
              </div>
              <div className="panel-note">
                Holds are recomputed by the core from the order’s state. {RESERVATION_NOTE}
              </div>
            </section>
          </div>

          <section className="panel section-gap" aria-labelledby="ord-decision-h">
            <header>
              <h2 id="ord-decision-h">Pretrade decision</h2>
              {recorded?.seq ? <span className="muted ord-small num">recorded at seq {recorded.seq}</span> : null}
            </header>
            <div className="panel-body">
              {recorded?.decision ? (
                <>
                  <div className="ord-decision-context">
                    {recorded.requiredCash ? (
                      <span>
                        Cash the core required: <Money amount={recorded.requiredCash} currency={o.currency} kind="held" />{' '}
                        <span className="muted">(notional plus fee envelope)</span>
                      </span>
                    ) : null}
                    <QuoteLine quote={recorded.quote} context="at decision" staleAfterMs={typeof quoteMaxAge === 'number' ? quoteMaxAge : undefined} />
                  </div>
                  <DecisionTable decision={recorded.decision} caption={`Pretrade checks for order ${o.order_id}`} />
                </>
              ) : (
                <p className="muted">
                  {recorded ? 'The acceptance event carries no readable decision record.' : 'No OrderAccepted event is in this order’s timeline.'}
                </p>
              )}
            </div>
          </section>

          <section className="panel section-gap" aria-labelledby="ord-outbox-h">
            <header>
              <h2 id="ord-outbox-h">Outbox</h2>
              <span className="muted ord-small">Messages to the broker, committed before each send</span>
            </header>
            <div className="panel-body flush">
              <DataTable caption="Outbox rows for this order" captionHidden columns={outboxColumns} rows={o.outbox} rowKey={(b) => b.outbox_id} emptyText="No outbox rows." />
            </div>
          </section>

          <section className="panel section-gap" aria-labelledby="ord-execs-h">
            <header>
              <h2 id="ord-execs-h">Executions</h2>
              <span className="muted ord-small num">{o.executions.length}</span>
            </header>
            <div className="panel-body flush">
              <DataTable caption="Executions for this order" captionHidden columns={execCols} rows={o.executions} rowKey={(e) => e.exec_key} emptyText="No fills yet." />
            </div>
          </section>

          <section className="panel section-gap" aria-labelledby="ord-timeline-h">
            <header>
              <h2 id="ord-timeline-h">Timeline</h2>
              <span className="muted ord-small num">{o.timeline.length} events</span>
            </header>
            <div className="panel-body flush">
              <DataTable
                caption="Events recorded for this order"
                captionHidden
                columns={timelineColumns}
                rows={o.timeline}
                rowKey={(e) => String(e.seq)}
                initialSort={{ columnId: 'seq', direction: 'ascending' }}
                emptyText="No events."
              />
            </div>
          </section>
          <CancelDialog order={cancelOpen ? o : null} outbox={o.outbox} onClose={() => setCancelOpen(false)} />
        </>
      )}
    </WorkspaceFrame>
  );
}
