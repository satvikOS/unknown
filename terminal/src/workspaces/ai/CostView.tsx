/** AI cost and quality (CAP-AGT-13): ledger spend by day, role and process against the caps; runs, failures, latency,
 * tokens and the share of unverified lines. Amounts are the ledger's exact decimal strings. */
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge, StatusPill } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { usePluginQuery } from '../plugin-api/hooks';
import { PROCESS_LABEL, capShare, parseDashboard, trimAmount, type ProcessCost } from './model';

function Amount({ value, currency }: { value: string | null; currency: string | null }) {
  if (value === null) return <NoValue reason="NOT_APPLICABLE" />;
  return <span className="num">{`${trimAmount(value)}${currency ? ` ${currency}` : ''}`}</span>;
}

function Meter({ spent, cap }: { spent: string; cap: string | null }) {
  const share = capShare(spent, cap);
  if (share === null) return <NoValue reason="NOT_APPLICABLE" detail="no cap" />;
  return (
    <span className="ai-meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={share} aria-label={`${share}% of the cap`}>
      <span style={{ width: `${share}%` }} />
      <em>{share}%</em>
    </span>
  );
}

export function CostView() {
  const q = usePluginQuery<unknown>('/api/ai/dashboard', { days: 14 }, { pollMs: 30_000 });
  const d = q.data === undefined ? null : parseDashboard(q.data);
  const ccy = d?.currency ?? null;
  const columns: Column<ProcessCost>[] = [
    { id: 'process', header: 'Process', cell: (p) => PROCESS_LABEL[p.process] },
    {
      id: 'on',
      header: 'Switch',
      cell: (p) => <StatusPill label={p.enabled ? 'on' : 'off'} tone={p.enabled ? 'ok' : 'neutral'} compact />,
    },
    {
      id: 'today',
      header: 'Spent today',
      numeric: true,
      cell: (p) => <Amount value={p.spentToday} currency={ccy} />,
    },
    {
      id: 'cap',
      header: 'Day cap',
      numeric: true,
      cell: (p) => <Amount value={p.perDay} currency={ccy} />,
    },
    {
      id: 'share',
      header: 'Of day cap',
      cell: (p) => <Meter spent={p.spentToday} cap={p.perDay} />,
    },
    { id: 'runs', header: 'Runs', numeric: true, cell: (p) => p.runs },
    {
      id: 'failed',
      header: 'Failed',
      numeric: true,
      description: 'Runs that ended FAILED (AI unavailable, disabled, worker error)',
      cell: (p) => (p.failed ? <Badge tone="warn">{String(p.failed)}</Badge> : 0),
    },
    {
      id: 'lat',
      header: 'Latency p50 / p95',
      numeric: true,
      cell: (p) =>
        p.p50 === null ? (
          <NoValue reason="NOT_APPLICABLE" detail="no finished run" />
        ) : (
          `${(p.p50 / 1000).toFixed(1)} s / ${((p.p95 ?? p.p50) / 1000).toFixed(1)} s`
        ),
    },
    {
      id: 'tokens',
      header: 'Tokens in / out',
      numeric: true,
      cell: (p) => `${p.promptTokens} / ${p.outputTokens}`,
    },
    {
      id: 'cost',
      header: 'Cost (all runs)',
      numeric: true,
      cell: (p) => <Amount value={p.cost} currency={ccy} />,
    },
    {
      id: 'quality',
      header: 'Unverified lines',
      numeric: true,
      description: 'Model lines not verified against a cited source',
      cell: (p) => (p.lines ? `${p.unsupportedRate ?? '0.0'}% of ${p.lines}` : <NoValue reason="NOT_APPLICABLE" detail="no model lines" />),
    },
  ];
  const days = d?.byDay ?? [];
  const roles = [...new Set(days.flatMap((x) => Object.keys(x.byRole)))].sort();
  return (
    <WorkspaceFrame
      title="AI cost and quality"
      description="Spend from the model ledger against the owner's caps; runs, latency, tokens and verification from the process store."
      queries={[q]}
    >
      <QueryError what="The AI dashboard" error={q.error} />
      {q.data !== undefined && d === null ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          The dashboard response did not have the expected shape; nothing is shown rather than a guess.
        </div>
      ) : null}
      {d ? (
        <>
          {d.problems.map((p) => (
            <div key={p} className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)' }}>
              {p}
            </div>
          ))}
          <dl className="ai-kpis">
            <div>
              <dt>Today ({d.today} UTC)</dt>
              <dd>
                <Amount value={d.spendToday} currency={ccy} /> {d.ownerCaps ? <Meter spent={d.spendToday} cap={d.ownerCaps.perDay} /> : null}
              </dd>
            </div>
            <div>
              <dt>This month</dt>
              <dd>
                <Amount value={d.spendMonth} currency={ccy} />{' '}
                {d.ownerCaps?.perMonth ? <Meter spent={d.spendMonth} cap={d.ownerCaps.perMonth} /> : null}
              </dd>
            </div>
            <div>
              <dt>Lifetime</dt>
              <dd>
                <Amount value={d.spendLifetime} currency={ccy} />{' '}
                {d.ownerCaps?.lifetime ? <Meter spent={d.spendLifetime} cap={d.ownerCaps.lifetime} /> : null}
              </dd>
            </div>
            <div>
              <dt>Unconfirmed (kept reserved)</dt>
              <dd>
                <Amount value={d.unconfirmed} currency={ccy} />
              </dd>
            </div>
            <div>
              <dt>Audit trail</dt>
              <dd>{d.auditOk ? <StatusPill label="chain intact" tone="ok" compact /> : <StatusPill label="chain broken" tone="bad" compact />}</dd>
            </div>
            <div>
              <dt>Approvals</dt>
              <dd>
                {Object.keys(d.approvals).length
                  ? Object.entries(d.approvals)
                      .map(([k, v]) => `${v} ${k.toLowerCase()}`)
                      .join(', ')
                  : 'none'}
              </dd>
            </div>
          </dl>
          {d.ownerCaps ? null : <p className="muted">No owner policy: paid model calls are disabled.</p>}
          <DataTable caption="AI processes" columns={columns} rows={d.processes} rowKey={(p) => p.process} emptyText="No processes." />
          <h3>Spend by day and role (UTC)</h3>
          <div className="ai-scroll">
            <table className="ai-table">
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  {roles.map((r) => (
                    <th key={r} scope="col" className="num">
                      {r}
                    </th>
                  ))}
                  <th scope="col" className="num">
                    Total
                  </th>
                </tr>
              </thead>
              <tbody>
                {days.map((x) => (
                  <tr key={x.day}>
                    <th scope="row">{x.day}</th>
                    {roles.map((r) => (
                      <td key={r} className="num">
                        {x.byRole[r] ? trimAmount(x.byRole[r]!) : '0'}
                      </td>
                    ))}
                    <td className="num">{trimAmount(x.total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : q.status === 'loading' ? (
        <p className="muted">Loading the AI dashboard…</p>
      ) : null}
    </WorkspaceFrame>
  );
}
