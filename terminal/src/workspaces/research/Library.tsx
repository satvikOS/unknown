/**
 * Strategy specifications (RES-01; spec hash re-verified on read), study reports, and the formula registry
 * (formula id@version) that every derived research number refers to.
 */
import { useMemo, useState } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge, FixtureBadge, StatusPill } from '../../ui/primitives';
import { apiPath, useApi } from '../research-common/api';
import { formatStat } from '../research-common/format';
import { Hash, JsonTree, KV, Section, StatusTag } from '../research-common/parts';
import { truthy, type Envelope, type Formula, type ReportDetail, type ReportRow, type StrategyDetail, type StrategyRow } from './model';
import { EnvelopeView, NsTime, ProvenanceLabel, ProvenanceNote } from './parts';
import { ResearchFrame, type ResearchCtx } from './ResearchWorkspace';

// ------------------------------------------------------------------ strategies

export function StrategyList({ ctx }: { ctx: ResearchCtx }) {
  const q = useApi<Envelope<StrategyRow[]>>('/api/research/strategies');
  const columns: Column<StrategyRow>[] = [
    {
      id: 'hash',
      header: 'Spec hash',
      sortValue: (s) => s.spec_hash,
      cell: (s) => (
        <a href={`#/research/strategies/${s.spec_hash}`} tabIndex={-1} className="mono" title={s.spec_hash}>
          {s.spec_hash.slice(0, 16)}…
        </a>
      ),
    },
    { id: 'strategy', header: 'Strategy', sortValue: (s) => s.strategy_id, cell: (s) => <span className="mono">{`${s.strategy_id} v${s.version}`}</span> },
    { id: 'family', header: 'Family', sortValue: (s) => s.family, cell: (s) => s.family },
    { id: 'registered', header: 'Registered', sortValue: (s) => s.registered_at_ns, sortKind: 'number', cell: (s) => <NsTime ns={s.registered_at_ns} /> },
    { id: 'by', header: 'By', cell: (s) => `${s.registered_by} (${s.registered_role})` },
  ];
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what="Strategy specifications">
        {(rows, prov) => (
          <Section title={`Strategy specifications · ${rows.length}`} flush note={<ProvenanceNote p={prov} />}>
            <DataTable
              caption="Registered strategy specifications"
              captionHidden
              columns={columns}
              rows={rows}
              rowKey={(s) => s.spec_hash}
              initialSort={{ columnId: 'registered', direction: 'ascending' }}
              onRowActivate={(s) => ctx.navigate('research', 'strategies', s.spec_hash)}
              activateLabel="open the specification"
              emptyText="No strategy specification registered."
            />
          </Section>
        )}
      </EnvelopeView>
    </ResearchFrame>
  );
}

function text(v: unknown): string {
  return typeof v === 'string' ? v : v === undefined || v === null ? '' : JSON.stringify(v);
}

export function StrategyView({ ctx, hash }: { ctx: ResearchCtx; hash: string }) {
  const q = useApi<Envelope<StrategyDetail>>(apiPath('research', 'strategies', hash));
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what={`Strategy ${hash.slice(0, 12)}`}>
        {(s, prov) => {
          const b = s.body;
          const hyp = (b.hypothesis ?? {}) as Record<string, unknown>;
          return (
            <>
              <p className="rx-identity">
                <a href="#/research/strategies">All specifications</a>
                <span className="rx-name mono">{`${text(b.strategy_id)} v${text(b.version)}`}</span>
                {prov.fixture_data_allowed ? (
                  <Badge tone="warn" title="The specification's data requirements allow FIXTURE (synthetic) data">
                    FIXTURE DATA ALLOWED
                  </Badge>
                ) : null}
                {s.hash_verified ? <StatusTag status="OK" detail="spec hash re-verified by the API" /> : <StatusTag status="FAILED" />}
              </p>
              <Section title={text(b.title) || 'Specification'} note={<ProvenanceNote p={prov} />}>
                <KV
                  rows={[
                    ['Spec hash', <Hash value={s.spec_hash} head={24} />],
                    ['Family', text(b.family)],
                    ['Author', text(b.author)],
                    ['Hypothesis', text(hyp.statement)],
                    ['Mechanism', text(hyp.mechanism)],
                    ['Expected effect', text(hyp.expected_effect)],
                    [
                      'Equations',
                      Array.isArray(b.equations) ? (
                        <ul className="rx-tree-list">
                          {(b.equations as Record<string, unknown>[]).map((e) => (
                            <li key={text(e.id)}>
                              <span className="mono">{text(e.formula_id)}</span>: <span className="mono">{text(e.expression)}</span> <span className="muted">({text(e.book_ref)})</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        '—'
                      ),
                    ],
                    ['Parameters', <span className="mono">{Object.entries((b.parameters ?? {}) as Record<string, unknown>).map(([k, v]) => `${k}=${text(v)}`).join(', ')}</span>],
                    ['Promotion evidence', <span className="mono">{Array.isArray(b.promotion_evidence) ? (b.promotion_evidence as unknown[]).map(text).join(', ') : '—'}</span>],
                  ]}
                />
                <h3 className="rx-h3">Full specification</h3>
                <JsonTree value={b} label="Strategy specification" />
              </Section>
            </>
          );
        }}
      </EnvelopeView>
    </ResearchFrame>
  );
}

// ------------------------------------------------------------------ reports

export function ReportList({ ctx }: { ctx: ResearchCtx }) {
  const q = useApi<Envelope<ReportRow[]>>('/api/research/reports');
  const columns: Column<ReportRow>[] = [
    {
      id: 'id',
      header: 'Report',
      sortValue: (r) => r.report_id,
      cell: (r) => (
        <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          <a href={`#/research/reports/${encodeURIComponent(r.report_id)}`} tabIndex={-1}>
            <strong>{r.report_id}</strong>
          </a>
          {truthy(r.fixture) ? <FixtureBadge /> : null}
        </span>
      ),
    },
    { id: 'kind', header: 'Kind', sortValue: (r) => r.kind, cell: (r) => <span className="mono">{r.kind}</span> },
    { id: 'experiment', header: 'Experiment', cell: (r) => r.experiment_id ?? <span className="muted">study (several)</span> },
    { id: 'created', header: 'Created', sortValue: (r) => r.created_at_ns, sortKind: 'number', cell: (r) => <NsTime ns={r.created_at_ns} /> },
    { id: 'hash', header: 'Body hash', cell: (r) => <Hash value={r.body_hash} /> },
  ];
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what="Reports">
        {(rows, prov) => (
          <Section title={`Reports · ${rows.length}`} flush note={<ProvenanceNote p={prov} />}>
            <DataTable
              caption="Research reports"
              captionHidden
              columns={columns}
              rows={rows}
              rowKey={(r) => r.report_id}
              initialSort={{ columnId: 'created', direction: 'descending' }}
              onRowActivate={(r) => ctx.navigate('research', 'reports', r.report_id)}
              activateLabel="open the report"
              emptyText="No report recorded."
            />
          </Section>
        )}
      </EnvelopeView>
    </ResearchFrame>
  );
}

interface StudyResult {
  kind?: string;
  experiment_id?: string;
  status?: string;
  failing_gates?: string[];
  conclusion?: string;
  evidence?: { evidence_id: string; kind: string; passed: boolean; reasons: string[] }[];
  dsr?: { dsr?: number; M?: number; T?: number };
  pbo?: { pbo?: number; S?: number; N?: number };
  n_eff?: { gate?: number; n?: number };
  paired_bootstrap_oos?: { conservative_lower?: number; point?: number; hurdle?: number; passes?: boolean; confidence?: number };
  selected?: { artifact_hash?: string };
}

export function ReportView({ ctx, id }: { ctx: ResearchCtx; id: string }) {
  const q = useApi<Envelope<ReportDetail>>(apiPath('research', 'reports', id));
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what={`Report ${id}`}>
        {(r, prov) => {
          const b = r.body;
          const results = (Array.isArray(b.results) ? b.results : []) as StudyResult[];
          return (
            <>
              <p className="rx-identity">
                <a href="#/research/reports">All reports</a>
                <span className="rx-name">{text(b.title) || r.report_id}</span>
                <ProvenanceLabel p={prov} />
                <span className="mono muted">{r.kind}</span>
              </p>
              {b.warning ? (
                <div className="callout" role="note" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 10 }}>
                  {text(b.warning)}
                </div>
              ) : null}
              <Section title="Report" note={<ProvenanceNote p={prov} />}>
                <KV
                  rows={[
                    ['Report', <span className="mono">{r.report_id}</span>],
                    ['Created', <NsTime ns={r.created_at_ns} />],
                    ['Body hash', <Hash value={r.body_hash} head={24} />],
                    b.candidate ? ['Candidate', text(b.candidate)] : null,
                    Array.isArray(b.baselines) ? ['Baselines', (b.baselines as unknown[]).map(text).join(', ')] : null,
                    b.study_version ? ['Study version', <span className="mono">{text(b.study_version)}</span>] : null,
                  ]}
                />
              </Section>
              {results.map((res, i) => (
                <Section
                  key={res.experiment_id ?? i}
                  title={
                    <>
                      {res.kind ?? `result ${i + 1}`} {res.status ? <StatusPill label={res.status} tone="info" compact /> : null}
                    </>
                  }
                >
                  {res.conclusion ? <p>{res.conclusion}</p> : null}
                  <KV
                    rows={[
                      res.experiment_id ? ['Experiment', <a href={`#/research/experiments/${encodeURIComponent(res.experiment_id)}`}>{res.experiment_id}</a>] : null,
                      ['Failing gates', res.failing_gates?.length ? <span className="mono">{res.failing_gates.join(', ')}</span> : <span className="muted">none</span>],
                      res.selected?.artifact_hash ? ['Selected artifact', <a href={`#/research/artifacts/${res.selected.artifact_hash}`}>{res.selected.artifact_hash.slice(0, 16)}…</a>] : null,
                      res.dsr ? ['DSR (dsr@1)', <span className="num">{`${formatStat(res.dsr.dsr ?? null)} (M = ${res.dsr.M ?? '?'}, T = ${res.dsr.T ?? '?'})`}</span>] : null,
                      res.pbo ? ['PBO (pbo.cscv@1)', <span className="num">{`${formatStat(res.pbo.pbo ?? null)} (S = ${res.pbo.S ?? '?'}, N = ${res.pbo.N ?? '?'})`}</span>] : null,
                      res.n_eff ? ['n_eff (eq4.2@1, gate form)', <span className="num">{`${formatStat(res.n_eff.gate ?? null)} of n = ${res.n_eff.n ?? '?'}`}</span>] : null,
                      res.paired_bootstrap_oos
                        ? [
                            'Paired bootstrap (OOS)',
                            <span className="num">
                              {`point ${formatStat(res.paired_bootstrap_oos.point ?? null)}, conservative lower ${formatStat(res.paired_bootstrap_oos.conservative_lower ?? null)} vs hurdle ${formatStat(res.paired_bootstrap_oos.hurdle ?? null)} at ${formatStat(res.paired_bootstrap_oos.confidence ?? null)} confidence`}{' '}
                              <StatusPill label={res.paired_bootstrap_oos.passes ? 'PASS' : 'FAIL'} tone={res.paired_bootstrap_oos.passes ? 'ok' : 'bad'} compact />
                            </span>,
                          ]
                        : null,
                    ]}
                  />
                  {res.evidence?.length ? (
                    <>
                      <h3 className="rx-h3">Gate verdicts</h3>
                      <ul className="ops-checks">
                        {res.evidence.map((e) => (
                          <li key={e.evidence_id}>
                            <span className="label">
                              <StatusPill label={e.passed ? 'PASS' : 'FAIL'} tone={e.passed ? 'ok' : 'bad'} compact /> <span className="mono">{e.kind}</span>
                            </span>
                            <span>{e.reasons.length ? <span className="rx-reason">{e.reasons.join('; ')}</span> : <span className="muted">—</span>}</span>
                          </li>
                        ))}
                      </ul>
                    </>
                  ) : null}
                  <details className="rx-details rx-gap">
                    <summary>Full result (as recorded)</summary>
                    <JsonTree value={res} label={`Result ${res.kind ?? i + 1}`} />
                  </details>
                </Section>
              ))}
              <details className="rx-details rx-gap">
                <summary>Protocol and provenance (as recorded)</summary>
                <JsonTree value={{ protocol: b.protocol, provenance: b.provenance }} label="Protocol and provenance" />
              </details>
            </>
          );
        }}
      </EnvelopeView>
    </ResearchFrame>
  );
}

// ------------------------------------------------------------------ formulas

export function FormulaList({ ctx }: { ctx: ResearchCtx }) {
  const q = useApi<Envelope<Formula[]>>('/api/research/formulas');
  const [filter, setFilter] = useState('');
  const rows = useMemo(() => {
    const all = q.data?.data ?? [];
    const f = filter.trim().toLowerCase();
    return f ? all.filter((x) => `${x.formula_id} ${x.name} ${x.expression} ${x.book}`.toLowerCase().includes(f)) : all;
  }, [q.data, filter]);
  const columns: Column<Formula>[] = [
    { id: 'id', header: 'Formula id@version', sortValue: (f) => f.formula_id, cell: (f) => <span className="mono">{f.formula_id}</span> },
    { id: 'name', header: 'Name', wrap: true, sortValue: (f) => f.name, cell: (f) => f.name },
    { id: 'expr', header: 'Expression', wrap: true, cell: (f) => <span className="mono">{f.expression}</span> },
    { id: 'units', header: 'Units', wrap: true, cell: (f) => f.units },
    { id: 'book', header: 'Reference', wrap: true, cell: (f) => <span className="muted">{f.book}</span> },
  ];
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what="Formulas">
        {(all, prov) => (
          <Section
            title={`Formula registry · ${all.length}`}
            flush
            actions={
              <div className="field" style={{ margin: 0 }}>
                <label htmlFor="formula-filter" className="sr-only">
                  Filter formulas
                </label>
                <input id="formula-filter" className="input" placeholder="filter…" value={filter} onChange={(e) => setFilter(e.target.value)} autoComplete="off" />
              </div>
            }
            note={<ProvenanceNote p={prov} />}
          >
            <DataTable caption="Research formula registry" captionHidden columns={columns} rows={rows} rowKey={(f) => f.formula_id} initialSort={{ columnId: 'id', direction: 'ascending' }} emptyText="No formula matches." />
          </Section>
        )}
      </EnvelopeView>
    </ResearchFrame>
  );
}
