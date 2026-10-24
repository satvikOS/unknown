/**
 * Valuation snapshots (DAT-05): the end-of-day runs that performance, P&L, VaR history and the
 * backtest read, as visible now (point in time). GET /api/portfolio/snapshots. Snapshots are recorded
 * by the owner's end-of-day job (`python -m unknown.analytics.snapshots record --account <key>`).
 */
import { useState, type ReactNode } from 'react';
import { exactNs } from '../../api/nsStrings';
import { DataTable, type Column } from '../../ui/DataTable';
import { FixtureRefused, ShortId, Timestamp } from '../../ui/primitives';
import { QueryError } from '../../ui/WorkspaceFrame';
import { Segmented } from '../operations/parts';
import { usePluginQuery } from '../plugin-api/hooks';
import { SNAPSHOT_STORE_QUERY, num, ofKind, recs, str, unavailableReason, type Rec } from './model';
import { PortfolioFrame, Unavailable } from './parts';

export function SnapshotsTab({ bar }: { bar: ReactNode }) {
  const [limit, setLimit] = useState<'30' | '250' | '1000'>('30');
  const q = usePluginQuery<unknown>('/api/portfolio/snapshots', { limit }, SNAPSHOT_STORE_QUERY);
  const body = ofKind(q.data, 'snapshots');
  const why = unavailableReason(body);
  const runs = body ? recs(body.runs) : [];
  const cols: Column<Rec>[] = [
    { id: 'date', header: 'Valuation date', cell: (r) => str(r.valuation_date) ?? '?', sortValue: (r) => str(r.valuation_date) ?? '' },
    { id: 'asof', header: 'As of', cell: (r) => <Timestamp ns={num(r.as_of_ns)} nsStr={exactNs(r, 'as_of_ns')} />, sortValue: (r) => num(r.as_of_ns) ?? 0, sortKind: 'number' },
    { id: 'seq', header: 'Core seq', numeric: true, cell: (r) => num(r.core_seq) ?? '—' },
    { id: 'basis', header: 'Mark basis', cell: (r) => str(r.mark_basis) ?? '—' },
    { id: 'age', header: 'Mark budget', numeric: true, cell: (r) => (num(r.max_age_ms) !== null ? `${(num(r.max_age_ms) as number).toLocaleString('en-US')} ms` : '—') },
    { id: 'fx', header: 'Data', cell: (r) => (r.fixture === 1 || r.fixture === true ? <FixtureRefused detail="This run valued FIXTURE quotes." /> : <span className="muted">market</span>) },
    { id: 'sup', header: 'Supersedes', cell: (r) => (str(r.supersedes) ? <ShortId value={str(r.supersedes)} head={24} /> : <span className="muted">—</span>) },
    { id: 'run', header: 'Run id', cell: (r) => <ShortId value={str(r.run_id)} head={28} /> },
  ];
  return (
    <PortfolioFrame tab="snapshots" bar={bar} queries={[q]}>
      <div className="pf-toolbar">
        <Segmented
          label="Number of runs"
          options={[
            { value: '30', label: 'Last 30' },
            { value: '250', label: 'Last 250' },
            { value: '1000', label: 'Last 1000' },
          ]}
          value={limit}
          onChange={setLimit}
        />
        <span className="pf-hint">Runs visible now (point in time). A later run on the same date is a revision that supersedes the earlier one.</span>
      </div>
      <QueryError what="Snapshots" error={q.error} />
      {why ? <Unavailable what="Valuation snapshots" reason={why} /> : null}
      {body && !why ? (
        <DataTable caption="Valuation snapshot runs" columns={cols} rows={runs} rowKey={(r) => str(r.run_id) ?? '?'} initialSort={{ columnId: 'date', direction: 'descending' }} emptyText="No snapshot runs are visible yet." />
      ) : null}
      {!body && q.status === 'loading' ? <p className="muted">Loading snapshots…</p> : null}
    </PortfolioFrame>
  );
}
