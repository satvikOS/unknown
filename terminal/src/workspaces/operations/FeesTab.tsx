/**
 * Operations > Fee estimates: executions whose commission is still the core's estimate, with the
 * owner command `accept_fee_estimate`. A reconciling session completes only when each fee is final
 * or an accepted estimate (FEES_NOT_FINAL); a later broker report replaces the estimate in the ledger.
 */
import { useRef, useState, type ReactNode } from 'react';
import { defaultCommandId } from '../../api/client';
import type { Execution } from '../../api/types';
import type { QueryResult } from '../../state/react';
import { useSendCommand, useTrackedCommand } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Modal } from '../../ui/Modal';
import { Money, Qty, ShortId, StatusPill, Timestamp, type Tone } from '../../ui/primitives';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { estimatedFeeExecutions } from './model';
import { InlineCommandStatus, Segmented, isBusy, TabPanel } from './parts';

export const EXECUTION_LIMIT = 500;

function feeTone(status: string): Tone {
  switch (status) {
    case 'FINAL':
      return 'ok';
    case 'ESTIMATED':
      return 'warn';
    case 'ACCEPTED_ESTIMATE':
      return 'info';
    default:
      return 'neutral';
  }
}

function AcceptCell({ e, commandId, onAsk }: { e: Execution; commandId: string | undefined; onAsk: (e: Execution, el: HTMLElement) => void }) {
  const tracked = useTrackedCommand(commandId);
  const busy = isBusy(tracked);
  return (
    <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
      {e.fee_status === 'ESTIMATED' ? (
        <button type="button" className="btn small" disabled={busy} onClick={(ev) => onAsk(e, ev.currentTarget)}>
          {busy ? 'Sending…' : 'Accept estimate…'}
        </button>
      ) : null}
      <InlineCommandStatus commandId={commandId} />
      {e.fee_status !== 'ESTIMATED' && !commandId ? <span className="muted">no action</span> : null}
    </span>
  );
}

export function FeesTab({ bar, executions }: { bar: ReactNode; executions: QueryResult<Execution[]> }) {
  const send = useSendCommand();
  const [scope, setScope] = useState<'estimated' | 'all'>('estimated');
  const [commands, setCommands] = useState<Record<string, string>>({});
  const [asking, setAsking] = useState<Execution | null>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const all = executions.data ?? [];
  const estimated = estimatedFeeExecutions(all);
  // Rows acted on in this view stay listed so their command can be followed to `applied` and the
  // server's new fee status is visible (the list itself is never edited locally).
  const rows = scope === 'estimated' ? all.filter((e) => e.fee_status === 'ESTIMATED' || commands[e.exec_key] !== undefined) : all;

  const accept = async (e: Execution) => {
    setAsking(null);
    const commandId = defaultCommandId('accept_fee_estimate');
    setCommands((m) => ({ ...m, [e.exec_key]: commandId }));
    await send('accept_fee_estimate', { exec_key: e.exec_key }, { label: `Accept fee estimate (${e.side} ${e.quantity} ${e.instrument_id})`, commandId });
  };

  const columns: Column<Execution>[] = [
    { id: 'time', header: 'Executed', sortKind: 'number', sortValue: (e) => e.exec_time_ns, cell: (e) => <Timestamp ns={e.exec_time_ns} /> },
    {
      id: 'trade',
      header: 'Trade',
      sortValue: (e) => e.instrument_id,
      cell: (e) => (
        <span style={{ display: 'inline-flex', gap: 5, alignItems: 'baseline' }}>
          <span>{e.side}</span>
          <Qty value={e.quantity} />
          <WorkspaceLink to="instruments" rest={[e.instrument_id]}>
            <span className="mono">{e.instrument_id}</span>
          </WorkspaceLink>
          <span className="muted">@</span>
          <Money amount={e.price} currency={e.currency} />
        </span>
      ),
    },
    {
      id: 'notional',
      header: 'Notional',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (e) => e.notional,
      cell: (e) => <Money amount={e.notional} currency={e.currency} />,
    },
    {
      id: 'est',
      header: 'Fee estimate',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (e) => e.fee_estimate,
      cell: (e) => <Money amount={e.fee_estimate} currency={e.currency} kind="estimated" />,
    },
    {
      id: 'final',
      header: 'Fee reported',
      description: 'Commission reported by the broker',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (e) => e.fee_final,
      cell: (e) => <Money amount={e.fee_final} currency={e.currency} unavailable="not reported yet" />,
    },
    { id: 'status', header: 'Fee status', sortValue: (e) => e.fee_status, cell: (e) => <StatusPill label={e.fee_status} tone={feeTone(e.fee_status)} compact /> },
    {
      id: 'refs',
      header: 'Session / order / execution',
      cell: (e) => (
        <span className="cmd-id" style={{ display: 'inline-flex', gap: 4 }}>
          {e.session_id ? (
            <WorkspaceLink to="sessions" rest={[e.session_id]}>
              <ShortId value={e.session_id} head={12} />
            </WorkspaceLink>
          ) : (
            <span className="muted">outside</span>
          )}
          <span className="muted">/</span>
          {e.order_id ? (
            <WorkspaceLink to="orders" rest={[e.order_id]}>
              <ShortId value={e.order_id} head={14} />
            </WorkspaceLink>
          ) : (
            <span className="muted">no order</span>
          )}
          <span className="muted">/</span>
          <ShortId value={e.exec_key} head={14} />
        </span>
      ),
    },
    {
      id: 'action',
      header: 'Action',
      cell: (e) => <AcceptCell e={e} commandId={commands[e.exec_key]} onAsk={(x) => setAsking(x)} />,
    },
  ];

  return (
    <WorkspaceFrame
      title="Operations"
      description="Executions whose commission is still estimated. A reconciling session completes only when every fee is final or an accepted estimate."
      queries={[executions]}
    >
      {bar}
      <TabPanel idBase="ops" current="fees">
      <QueryError what="Executions" error={executions.error} />
      <div className="ops-toolbar">
        <Segmented
          label="Which executions"
          value={scope}
          onChange={setScope}
          options={[
            { value: 'estimated', label: `Estimated fees (${executions.data ? estimated.length : '…'})` },
            { value: 'all', label: `All recent (${executions.data ? all.length : '…'})` },
          ]}
        />
        <span className="ops-hint">
          From the latest {EXECUTION_LIMIT} executions (the API returns at most that many; older estimated fees are not listed here).
        </span>
      </div>
      <section className="panel">
        <div className="panel-body flush">
          <DataTable
            caption={scope === 'estimated' ? 'Executions with an estimated fee' : 'Recent executions and their fee status'}
            captionHidden
            columns={columns}
            rows={rows}
            rowKey={(e) => e.exec_key}
            initialSort={{ columnId: 'time', direction: 'descending' }}
            emptyText={executions.status === 'loading' ? 'Loading…' : scope === 'estimated' ? 'No execution has an estimated fee.' : 'No executions yet.'}
          />
        </div>
      </section>

      <Modal open={asking !== null} onClose={() => setAsking(null)} labelledBy="fee-accept-title" describedBy="fee-accept-desc" initialFocus={confirmRef}>
        {asking ? (
          <>
            <header>
              <h2 id="fee-accept-title">Accept the fee estimate?</h2>
            </header>
            <div className="modal-body" id="fee-accept-desc">
              <p style={{ marginTop: 0 }}>
                The estimate of <Money amount={asking.fee_estimate} currency={asking.currency} kind="estimated" showKind /> for execution{' '}
                <span className="cmd-id">{asking.exec_key}</span> ({asking.side} <Qty value={asking.quantity} /> {asking.instrument_id} at{' '}
                <Money amount={asking.price} currency={asking.currency} />) becomes the fee used to complete its session.
              </p>
              <p className="muted" style={{ marginBottom: 0 }}>
                Nothing is booked now: the estimate was accrued when the execution was recorded. When the broker reports the commission, the
                core books it in place of the estimate, and opens a post-completion notice if the session’s proceeds are already protected.
              </p>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => setAsking(null)}>
                Cancel
              </button>
              <button ref={confirmRef} type="button" className="btn primary" onClick={() => void accept(asking)}>
                Accept estimate
              </button>
            </div>
          </>
        ) : null}
      </Modal>
      </TabPanel>
    </WorkspaceFrame>
  );
}
