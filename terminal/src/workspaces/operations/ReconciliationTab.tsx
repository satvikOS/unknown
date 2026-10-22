/** Operations > Reconciliation: run history, the differences of one run, settlements it confirmed. */
import { useMemo, type ReactNode } from 'react';
import type { Reconciliation, ReconciliationDifference } from '../../api/types';
import { formatAge } from '../../format/time';
import { useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { reconciliationTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { DIFFERENCE_HELP, describeDetail, nsDiffMs, runSummary } from './model';
import { InlineFields, TabPanel } from './parts';

const RUN_LIMIT = 50;

const runColumns: Column<Reconciliation>[] = [
  {
    id: 'completed',
    header: 'Completed',
    sortKind: 'number',
    sortValue: (r) => r.completed_at_ns,
    cell: (r) => <Timestamp ns={r.completed_at_ns} />,
  },
  { id: 'status', header: 'Status', sortValue: (r) => r.status, cell: (r) => <StatusPill label={r.status} tone={reconciliationTone(r.status)} compact /> },
  { id: 'epoch', header: 'Process epoch', numeric: true, sortKind: 'number', sortValue: (r) => r.epoch, cell: (r) => r.epoch },
  {
    id: 'blocking',
    header: 'Blocking differences',
    numeric: true,
    sortKind: 'number',
    sortValue: (r) => runSummary(r).blocking,
    cell: (r) => {
      const n = runSummary(r).blocking;
      return n ? <StatusPill label={String(n)} tone="bad" compact /> : <span>0</span>;
    },
  },
  { id: 'other', header: 'Other differences', numeric: true, sortKind: 'number', sortValue: (r) => runSummary(r).nonBlocking, cell: (r) => runSummary(r).nonBlocking },
  {
    id: 'settled',
    header: 'Settlements confirmed',
    numeric: true,
    sortKind: 'number',
    sortValue: (r) => r.settlements_confirmed.length,
    cell: (r) => r.settlements_confirmed.length,
  },
  {
    id: 'snapshot',
    header: 'Broker snapshot age',
    description: 'How old the broker snapshot was when the run completed',
    numeric: true,
    sortKind: 'number',
    sortValue: (r) => (r.snapshot_as_of_ns ? nsDiffMs(r.completed_at_ns, r.snapshot_as_of_ns) : null),
    cell: (r) => (r.snapshot_as_of_ns ? formatAge(nsDiffMs(r.completed_at_ns, r.snapshot_as_of_ns)) : <span className="muted">not stated</span>),
  },
  {
    id: 'watermark',
    header: 'Execution watermark',
    sortValue: (r) => r.watermark,
    cell: (r) => (r.watermark !== null && r.watermark !== undefined ? <span className="cmd-id">{r.watermark}</span> : <span className="muted">none</span>),
  },
  { id: 'seq', header: 'Log seq at start', numeric: true, sortKind: 'number', sortValue: (r) => r.event_seq_at_start, cell: (r) => r.event_seq_at_start },
  { id: 'run', header: 'Run', sortValue: (r) => r.run_id, cell: (r) => <ShortId value={r.run_id} head={22} /> },
];

const diffColumns: Column<ReconciliationDifference & { i: number }>[] = [
  {
    id: 'kind',
    header: 'Difference',
    sortValue: (d) => d.kind,
    cell: (d) => (
      <span title={DIFFERENCE_HELP[d.kind]}>
        <code className="mono">{d.kind}</code>
      </span>
    ),
  },
  {
    id: 'blocking',
    header: 'Blocks entries',
    sortValue: (d) => d.blocking,
    cell: (d) => <StatusPill label={d.blocking ? 'blocking' : 'informational'} tone={d.blocking ? 'bad' : 'neutral'} compact />,
  },
  { id: 'detail', header: 'Detail', wrap: true, cell: (d) => <InlineFields fields={describeDetail(d.detail, { kind: d.kind })} max={8} /> },
  { id: 'meaning', header: 'Meaning', wrap: true, cell: (d) => <span className="muted">{DIFFERENCE_HELP[d.kind] ?? 'Reported by the core.'}</span> },
];

export function ReconciliationTab({
  bar,
  runId,
  navigate,
}: {
  bar: ReactNode;
  runId: string | null;
  navigate: (workspace: string, ...rest: string[]) => void;
}) {
  const runs = useQuery('/api/reconciliations', { limit: RUN_LIMIT });
  const policy = useQuery('/api/policy/active');
  const rows = runs.data ?? [];
  const latest = rows[0];
  const selected = runId ? rows.find((r) => r.run_id === runId) : latest;
  const maxAgeS = policy.data?.body.account_limits.max_reconciliation_age_s;
  const staleMs = maxAgeS !== undefined ? Number(maxAgeS) * 1000 : undefined;
  const diffs = useMemo(() => (selected?.differences ?? []).map((d, i) => ({ ...d, i })), [selected]);
  const summary = selected ? runSummary(selected) : null;

  return (
    <WorkspaceFrame
      title="Operations"
      description="Each run compares the broker’s orders, positions and cash with the ledger in one consistent cut; only a clean run advances the reconciled epoch."
      queries={[runs, policy]}
    >
      {bar}
      <TabPanel idBase="ops" current="reconciliation">
      <QueryError what="Reconciliation runs" error={runs.error} />
      {runId && runs.data && !selected ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 10 }}>
          Run <code className="mono">{runId}</code> is not among the latest {RUN_LIMIT} runs. <a href="#/operations/reconciliation">Show the latest run</a>
        </div>
      ) : null}

      {selected && summary ? (
        <section className="panel" aria-labelledby="rec-run" style={{ marginBottom: 12 }}>
          <header>
            <h2 id="rec-run">
              {selected === latest ? 'Latest run' : 'Selected run'} <span className="cmd-id" style={{ textTransform: 'none', letterSpacing: 0 }}>{selected.run_id}</span>
            </h2>
            <StatusPill label={selected.status} tone={reconciliationTone(selected.status)} />
          </header>
          <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
            <dl className="kv">
              <dt>Completed</dt>
              <dd>
                <Timestamp ns={selected.completed_at_ns} withMs />{' '}
                <span className="muted">
                  (<Age sinceNs={selected.completed_at_ns} staleAfterMs={selected === latest ? staleMs : undefined} /> ago
                  {selected === latest && maxAgeS !== undefined ? `; policy maximum age ${maxAgeS} s` : ''})
                </span>
              </dd>
              <dt>Process epoch</dt>
              <dd className="num">{selected.epoch}</dd>
              <dt>Broker snapshot as of</dt>
              <dd>
                {selected.snapshot_as_of_ns ? <Timestamp ns={selected.snapshot_as_of_ns} withMs /> : <span className="muted">not stated (incomplete run)</span>}
              </dd>
              <dt>Execution watermark</dt>
              <dd className="cmd-id">{selected.watermark ?? 'none'}</dd>
              <dt>Log seq at start</dt>
              <dd>
                <span className="num">{selected.event_seq_at_start}</span>{' '}
                <WorkspaceLink to="audit" rest={['events', `after=${Math.max(0, selected.event_seq_at_start - 1)}`]}>
                  <span className="ops-hint">events from here</span>
                </WorkspaceLink>
              </dd>
              <dt>Differences</dt>
              <dd>
                {summary.blocking} blocking, {summary.nonBlocking} informational
                {summary.kinds.length ? <span className="muted"> ({summary.kinds.join(', ')})</span> : null}
              </dd>
              <dt>Settlements confirmed</dt>
              <dd>
                {selected.settlements_confirmed.length ? (
                  <ul style={{ margin: 0, paddingLeft: 16 }}>
                    {selected.settlements_confirmed.map((s) => (
                      <li key={s} className="cmd-id">
                        {s}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <span className="muted">none in this run</span>
                )}
              </dd>
            </dl>
            {selected.status === 'INCOMPLETE' ? (
              <div className="callout" style={{ ['--tone' as string]: 'var(--warn)' }}>
                Partial broker data proves nothing: entries stay blocked until a complete clean run.
              </div>
            ) : null}
            {selected.status === 'CLEAN' ? (
              <p className="muted" style={{ margin: 0 }}>
                Clean: broker orders, positions and settled cash agree with the ledger at this cut.
              </p>
            ) : null}
            {diffs.length ? (
              <DataTable
                caption={`Differences found by run ${selected.run_id}`}
                columns={diffColumns}
                rows={diffs}
                rowKey={(d) => `${d.i}`}
                maxHeight="min(50vh, 420px)"
              />
            ) : null}
          </div>
        </section>
      ) : runs.data ? (
        <div className="callout" style={{ ['--tone' as string]: 'var(--info)', marginBottom: 12 }}>
          No reconciliation has run for this account yet. The core reconciles once a policy is active.
        </div>
      ) : null}

      <section className="panel" aria-labelledby="rec-runs">
        <header>
          <h2 id="rec-runs">Run history</h2>
          <span className="ops-hint">latest {RUN_LIMIT} runs, newest first</span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption={`Reconciliation runs (latest ${RUN_LIMIT})`}
            captionHidden
            columns={runColumns}
            rows={rows}
            rowKey={(r) => r.run_id}
            initialSort={{ columnId: 'completed', direction: 'descending' }}
            onRowActivate={(r) => navigate('operations', 'reconciliation', r.run_id)}
            activateLabel="show the run"
            emptyText={runs.status === 'loading' ? 'Loading…' : 'No runs yet.'}
          />
        </div>
      </section>
      </TabPanel>
    </WorkspaceFrame>
  );
}
