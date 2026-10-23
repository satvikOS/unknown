/**
 * Executions (fills): GET /api/executions, newest first. Fee status says whether a fee is the
 * broker's final figure, an estimate, an owner-accepted estimate or recorded externally.
 */
import { useMemo, useState, type ReactNode } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import type { Execution, Instrument } from '../../api/types';
import { useQuery } from '../../state/react';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, Qty, ShortId, StatusPill, Timestamp, type Tone } from '../../ui/primitives';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { InstrumentCell, SideText, type InstrumentData } from './parts';

export const EXECUTIONS_LIMIT = 500;

const FEE_STATUS: Record<string, { tone: Tone; detail: string }> = {
  FINAL: { tone: 'ok', detail: 'The broker reported the final fee.' },
  ESTIMATED: { tone: 'warn', detail: 'Only the fee model’s estimate is known; the broker has not reported the final fee.' },
  ACCEPTED_ESTIMATE: { tone: 'info', detail: 'The owner accepted the estimate as final.' },
  EXTERNAL: { tone: 'info', detail: 'The fee was recorded from an external source.' },
};

export function feeStatusPill(status: string) {
  const s = FEE_STATUS[status] ?? { tone: 'neutral' as Tone, detail: 'Fee status reported by the core.' };
  return <StatusPill label={status} tone={s.tone} detail={s.detail} compact />;
}

export function executionColumns(instruments: ReadonlyMap<string, Instrument>, opts: { showOrder: boolean }): Column<Execution>[] {
  const cols: Column<Execution>[] = [
    { id: 'time', header: 'Executed', sortKind: 'number', sortValue: (e) => e.exec_time_ns, cell: (e) => <Timestamp ns={e.exec_time_ns} nsStr={nsStrOf(e, 'exec_time_ns')} withMs /> },
    {
      id: 'key',
      header: 'Execution',
      description: 'Provider-scoped execution key',
      sortValue: (e) => e.exec_key,
      cell: (e) => <span className="cmd-id" title={e.provider_exec_id ? `provider exec id ${e.provider_exec_id}` : undefined}>{e.exec_key}</span>,
    },
  ];
  if (opts.showOrder) {
    cols.push({
      id: 'order',
      header: 'Order',
      sortValue: (e) => e.order_id,
      cell: (e) =>
        e.order_id ? (
          <a href={routeHref('orders', e.order_id)} className="cmd-id">
            {e.order_id}
          </a>
        ) : (
          <span className="muted" title="No local order: a foreign or unmatched execution">
            no order
          </span>
        ),
    });
    cols.push({
      id: 'session',
      header: 'Session',
      sortValue: (e) => e.session_id,
      cell: (e) =>
        e.session_id ? (
          <WorkspaceLink to="sessions" rest={[e.session_id]}>
            <ShortId value={e.session_id} head={16} />
          </WorkspaceLink>
        ) : (
          <span className="muted">none</span>
        ),
    });
  }
  cols.push(
    {
      id: 'inst',
      header: 'Instrument',
      sortValue: (e) => instruments.get(e.instrument_id)?.symbol ?? e.instrument_id,
      cell: (e) => <InstrumentCell id={e.instrument_id} instrument={instruments.get(e.instrument_id)} />,
    },
    { id: 'side', header: 'Side', sortValue: (e) => e.side, cell: (e) => <SideText side={e.side} /> },
    { id: 'qty', header: 'Qty', numeric: true, sortKind: 'decimal', sortValue: (e) => e.quantity, cell: (e) => <Qty value={e.quantity} /> },
    { id: 'price', header: 'Price', numeric: true, sortKind: 'decimal', sortValue: (e) => e.price, cell: (e) => <Money amount={e.price} currency={e.currency} /> },
    {
      id: 'notional',
      header: 'Notional',
      description: 'Quantity × price × multiplier at ledger scale',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (e) => e.notional,
      cell: (e) => <Money amount={e.notional} currency={e.currency} />,
    },
    {
      id: 'fee-est',
      header: 'Fee (estimated)',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (e) => e.fee_estimate,
      cell: (e) => <Money amount={e.fee_estimate} currency={e.currency} kind="estimated" />,
    },
    {
      id: 'fee-final',
      header: 'Fee (final)',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (e) => e.fee_final,
      cell: (e) => <Money amount={e.fee_final} currency={e.currency} kind="realized" unavailable="not reported" />,
    },
    { id: 'fee-status', header: 'Fee status', sortValue: (e) => e.fee_status, cell: (e) => feeStatusPill(e.fee_status) },
    {
      id: 'status',
      header: 'Status',
      sortValue: (e) => e.status,
      cell: (e) => (
        <span className="ord-inline">
          <StatusPill label={e.status} tone={e.status === 'ACTIVE' ? 'ok' : 'neutral'} compact />
          {e.corrected_by ? <span className="muted cmd-id">by {e.corrected_by}</span> : null}
          {e.correction_of ? <span className="muted cmd-id">corrects {e.correction_of}</span> : null}
        </span>
      ),
    },
    {
      id: 'settlement',
      header: 'Settlement',
      sortValue: (e) => e.settlement_id,
      cell: (e) => <ShortId value={e.settlement_id} head={18} />,
    },
  );
  return cols;
}

export function ExecutionsView({ tabs, instruments: inst }: { tabs: ReactNode; instruments: InstrumentData }) {
  const instruments = inst.map;
  const executions = useQuery('/api/executions', { limit: EXECUTIONS_LIMIT });
  const sessions = useQuery('/api/sessions');
  const [sessionId, setSessionId] = useState('');
  const [feeStatus, setFeeStatus] = useState('');
  const all = executions.data ?? [];
  const feeStatuses = useMemo(() => [...new Set(all.map((e) => e.fee_status))].sort(), [all]);
  const rows = useMemo(
    () => all.filter((e) => (!sessionId || e.session_id === sessionId) && (!feeStatus || e.fee_status === feeStatus)),
    [all, sessionId, feeStatus],
  );
  const cols = useMemo(() => executionColumns(instruments, { showOrder: true }), [instruments]);
  const truncated = all.length >= EXECUTIONS_LIMIT;
  const estimated = all.filter((e) => e.status === 'ACTIVE' && e.fee_status === 'ESTIMATED').length;

  return (
    <WorkspaceFrame
      title="Executions"
      description="Fills recorded by the core, with notional, fees and fee status."
      queries={[executions, sessions, inst.query]}
    >
      {tabs}
      <div className="ord-filters" role="group" aria-label="Execution filters">
        <div className="field">
          <label htmlFor="exec-session">Session</label>
          <select id="exec-session" className="input" value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
            <option value="">All sessions</option>
            {(sessions.data ?? []).map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id} ({s.state})
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="exec-fee">Fee status</label>
          <select id="exec-fee" className="input" value={feeStatus} onChange={(e) => setFeeStatus(e.target.value)}>
            <option value="">All</option>
            {feeStatuses.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      </div>
      <QueryError what="Executions" error={executions.error} />
      {estimated ? (
        <div className="callout ord-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
          {estimated} active execution{estimated === 1 ? ' has' : 's have'} only an estimated fee. A session cannot finish reconciling (blocker{' '}
          <code className="mono">FEES_NOT_FINAL</code>) until each fee is final or its estimate is accepted.
        </div>
      ) : null}
      <section className="panel" aria-labelledby="ord-exec-h">
        <header>
          <h2 id="ord-exec-h">Executions</h2>
          <span className="muted ord-small">
            {rows.length === all.length ? `${all.length}` : `${rows.length} of ${all.length}`} shown · newest first
            {truncated ? ` · the API returns at most ${EXECUTIONS_LIMIT}; older fills are not listed` : ''}
          </span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption={`Executions${sessionId ? ` in session ${sessionId}` : ''}`}
            captionHidden
            columns={cols}
            rows={rows}
            rowKey={(e) => e.exec_key}
            emptyText={executions.status === 'loading' ? 'Loading…' : all.length === 0 ? 'No executions recorded.' : 'No execution matches the filters.'}
          />
        </div>
      </section>
    </WorkspaceFrame>
  );
}
