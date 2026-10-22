/**
 * Sessions list: state, seed, target terminal equity, implied required net return, outcome,
 * protected value, funding status and reason, and predecessor/successor links. Waiting sessions
 * (successors included) state exactly why they lack funding.
 */
import { useMemo, useState } from 'react';
import type { Session } from '../../api/types';
import { useAccountKey } from '../../controls/ControlProvider';
import { useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, StatusPill, Timestamp } from '../../ui/primitives';
import { sessionStateTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import type { WorkspaceProps } from '../types';
import { requiredReturnPct } from './decimalMath';
import { MandateDialog } from './MandateDialog';
import { matchesFilter, STATE_FILTERS, stateMeaning, successorIndex, type StateFilter } from './model';
import { FundingCell, FundingExplanationBlock, Pct, SessionLink } from './parts';
import './sessions.css';

interface Row extends Session {
  requiredPct: string | null;
  successorId: string | null;
}

function columns(): Column<Row>[] {
  return [
    {
      id: 'id',
      header: 'Session',
      sortValue: (s) => s.created_at_ns,
      sortKind: 'number',
      description: 'Session id; sorts by creation time',
      cell: (s) => <SessionLink id={s.session_id} short />,
    },
    {
      id: 'state',
      header: 'State',
      sortValue: (s) => s.state,
      wrap: true,
      cell: (s) => <StatusPill label={s.state} tone={sessionStateTone(s.state)} detail={stateMeaning(s.state)} compact />,
    },
    {
      id: 'seed',
      header: 'Seed',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (s) => s.seed,
      cell: (s) => <Money amount={s.seed} currency={s.currency} />,
    },
    {
      id: 'target',
      header: 'Target terminal equity',
      description: 'The equity the mandate ends with on success. A configured mandate, not a forecast.',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (s) => s.target,
      cell: (s) => <Money amount={s.target} currency={s.currency} />,
    },
    {
      id: 'req',
      header: 'Required net return',
      description: '(target − seed) / seed, rounded half-even to 4 places as the core computes it. Not a forecast.',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (s) => s.requiredPct,
      cell: (s) => <Pct value={s.requiredPct} />,
    },
    {
      id: 'outcome',
      header: 'Outcome',
      sortValue: (s) => s.outcome,
      cell: (s) => (s.outcome ? <code className="mono">{s.outcome}</code> : <span className="muted">none yet</span>),
    },
    {
      id: 'protected',
      header: 'Protected value',
      description: 'Terminal equity protected as completed proceeds (target reached). Never used to fund new sessions.',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (s) => s.protected_value,
      cell: (s) => <Money amount={s.protected_value} currency={s.currency} kind="protected" unavailable="nothing protected" />,
    },
    {
      id: 'funding',
      header: 'Funding',
      wrap: true,
      sortValue: (s) => s.funding?.reason ?? s.funding?.status ?? null,
      cell: (s) => <FundingCell session={s} />,
    },
    {
      id: 'pred',
      header: 'Predecessor',
      sortValue: (s) => s.predecessor_id,
      cell: (s) => (s.predecessor_id ? <SessionLink id={s.predecessor_id} short /> : <span className="muted">none</span>),
    },
    {
      id: 'succ',
      header: 'Successor',
      sortValue: (s) => s.successorId,
      cell: (s) => (s.successorId ? <SessionLink id={s.successorId} short /> : <span className="muted">none</span>),
    },
    {
      id: 'created',
      header: 'Created',
      sortKind: 'number',
      sortValue: (s) => s.created_at_ns,
      cell: (s) => <Timestamp ns={s.created_at_ns} />,
    },
  ];
}

export function SessionList({ navigate }: Pick<WorkspaceProps, 'navigate'>) {
  const sessions = useQuery('/api/sessions');
  const accountKey = useAccountKey();
  const [filter, setFilter] = useState<StateFilter>('open');
  const [createOpen, setCreateOpen] = useState(false);

  const rows: Row[] = useMemo(() => {
    const list = sessions.data ?? [];
    const succ = successorIndex(list);
    return list.map((s) => ({ ...s, requiredPct: requiredReturnPct(s.seed, s.target), successorId: succ.get(s.session_id) ?? null }));
  }, [sessions.data]);

  const visible = rows.filter((r) => matchesFilter(r.state, filter));
  const waiting = rows.filter((r) => r.state === 'WAITING_FUNDS' || r.state === 'REQUESTED');
  const cols = useMemo(columns, []);

  return (
    <WorkspaceFrame
      title="Sessions"
      description="Session mandates, funding, lifecycle and results. Targets are configured mandates, not forecasts."
      queries={[sessions]}
      actions={
        <button type="button" className="btn primary small" onClick={() => setCreateOpen(true)} disabled={!accountKey} title={accountKey ? undefined : 'Account not known yet'}>
          New session request
        </button>
      }
    >
      <QueryError what="Sessions" error={sessions.error} />

      {waiting.length ? (
        <section className="panel" aria-labelledby="ses-waiting-h" style={{ marginBottom: 12 }}>
          <header>
            <h2 id="ses-waiting-h">Waiting for funds</h2>
            <span className="num muted">{waiting.length}</span>
          </header>
          <div className="panel-body ses-reasons">
            {waiting.map((s) => (
              <div key={s.session_id} className="grid" style={{ gap: 4 }}>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'baseline' }}>
                  <SessionLink id={s.session_id} />
                  {s.predecessor_id ? (
                    <span className="muted">
                      successor of <SessionLink id={s.predecessor_id} />
                    </span>
                  ) : null}
                  <span className="muted">
                    seed <Money amount={s.seed} currency={s.currency} />
                  </span>
                </div>
                <FundingExplanationBlock
                  session={s}
                  extra={
                    s.predecessor_id ? (
                      <span className="muted">
                        A successor is funded only from unallocated settled cash; its predecessor’s protected proceeds are never used.
                      </span>
                    ) : undefined
                  }
                />
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <div className="ses-toolbar">
        <div className="ses-filters" role="group" aria-label="Filter sessions by state">
          {STATE_FILTERS.map((f) => {
            const count = rows.filter((r) => matchesFilter(r.state, f.id)).length;
            return (
              <button
                key={f.id}
                type="button"
                className="btn small"
                aria-pressed={filter === f.id}
                onClick={() => setFilter(f.id)}
                title={f.states ? f.states.join(', ') : 'Every session'}
              >
                {f.label} <span className="count">{count}</span>
              </button>
            );
          })}
        </div>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          Enter or double-click opens a session.
        </span>
      </div>

      <section className="panel" aria-labelledby="ses-list-h">
        <header>
          <h2 id="ses-list-h">{STATE_FILTERS.find((f) => f.id === filter)?.label ?? 'All'} sessions</h2>
          <span className="num muted">
            {visible.length} of {rows.length}
          </span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption="Sessions for this account"
            captionHidden
            columns={cols}
            rows={visible}
            rowKey={(s) => s.session_id}
            initialSort={{ columnId: 'created', direction: 'descending' }}
            onRowActivate={(s) => navigate('sessions', s.session_id)}
            activateLabel="open"
            emptyText={
              sessions.status === 'loading'
                ? 'Loading…'
                : rows.length
                  ? `No ${(STATE_FILTERS.find((f) => f.id === filter)?.label ?? '').toLowerCase()} sessions. ${filter === 'all' ? '' : 'Choose All to see every session.'}`
                  : 'No sessions yet. Record a session request to create one.'
            }
          />
        </div>
      </section>

      <MandateDialog mode="create" open={createOpen} onClose={() => setCreateOpen(false)} />
    </WorkspaceFrame>
  );
}
