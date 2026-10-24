/**
 * Experiments (CAP-RES-10/-11): pre-registered experiments with trial counts (every status counts toward M),
 * the development budget, the holdout window and its access log; every trial of an experiment; one trial with
 * its metrics, return series (chart) and reproducibility manifest.
 */
import { useMemo } from 'react';
import { exactJson, nsStrOf } from '../../api/nsStrings';
import type { QueryResult } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge, FixtureRefused, Money, StatusPill } from '../../ui/primitives';
import { asText, NoValue } from '../../ui/unavailable';
import { apiPath, useApi } from '../research-common/api';
import { LineChart } from '../research-common/charts';
import { cumulativeReturns, labelIndices, maxDrawdown } from '../research-common/chartMath';
import { formatFractionPct, formatStat } from '../research-common/format';
import { Hash, JsonTree, KV, Section, StatusTag } from '../research-common/parts';
import {
  MONEY_FIELDS,
  SEGMENTS,
  STAT_FIELDS,
  manifestCurrency,
  moneyField,
  numberField,
  segmentCounts,
  statBlocks,
  totalTrials,
  type Envelope,
  type ExperimentDetail,
  type ExperimentRow,
  type Trial,
  type TrialDetail,
  type TrialMetrics,
} from './model';
import { EnvelopeView, NsTime, ProvenanceLabel, ProvenanceNote } from './parts';
import { ResearchFrame, type ResearchCtx } from './ResearchWorkspace';

// ------------------------------------------------------------------ list

export function ExperimentList({ ctx }: { ctx: ResearchCtx }) {
  const q = useApi<Envelope<ExperimentRow[]>>('/api/research/experiments');
  const columns: Column<ExperimentRow>[] = [
    {
      id: 'id',
      header: 'Experiment',
      sortValue: (e) => e.experiment_id,
      cell: (e) => (
        <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          <a href={`#/research/experiments/${encodeURIComponent(e.experiment_id)}`} tabIndex={-1}>
            <strong>{e.experiment_id}</strong>
          </a>
          {e.fixture ? <FixtureRefused detail="A FIXTURE (test) experiment: its results are not shown." /> : null}
        </span>
      ),
    },
    { id: 'created', header: 'Pre-registered', sortValue: (e) => e.created_at_ns, sortKind: 'number', cell: (e) => <NsTime ns={e.created_at_ns} nsStr={nsStrOf(e, 'created_at_ns')} /> },
    { id: 'by', header: 'By', cell: (e) => `${e.created_by} (${e.created_role})` },
    {
      id: 'trials',
      header: 'Trials (all statuses)',
      numeric: true,
      sortValue: (e) => totalTrials(e.trial_counts),
      sortKind: 'number',
      cell: (e) => (
        <span title={segmentCounts(e.trial_counts).map((s) => `${s.segment} ${s.n}`).join(', ')}>
          {totalTrials(e.trial_counts)} <span className="muted">· dev {e.trial_counts.DEVELOPMENT ?? 0}</span>
        </span>
      ),
    },
    {
      id: 'budget',
      header: 'Development budget',
      numeric: true,
      cell: (e) => (
        <span>
          {e.trial_counts.DEVELOPMENT ?? 0} of {e.budget_current}
          {e.budget_current !== e.budget ? <span className="muted"> (pre-registered {e.budget})</span> : null}
        </span>
      ),
    },
    { id: 'holdout', header: 'Holdout window', cell: (e) => <span className="mono">{`${e.holdout_start} → ${e.holdout_end}`}</span> },
    {
      id: 'consumed',
      header: 'Holdout',
      sortValue: (e) => (e.holdout_consumed ? 1 : 0),
      sortKind: 'number',
      cell: (e) => <StatusPill label={e.holdout_consumed ? 'CONSUMED' : 'UNTOUCHED'} tone={e.holdout_consumed ? 'info' : 'ok'} compact />,
    },
    { id: 'spec', header: 'Spec', cell: (e) => <Hash value={e.spec_hash} /> },
  ];
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what="Experiments">
        {(rows, prov) => (
          <Section title={`Experiments · ${rows.length}`} flush note={<ProvenanceNote p={prov} />}>
            <DataTable
              caption="Pre-registered experiments"
              captionHidden
              columns={columns}
              rows={rows}
              rowKey={(e) => e.experiment_id}
              initialSort={{ columnId: 'created', direction: 'descending' }}
              onRowActivate={(e) => ctx.navigate('research', 'experiments', e.experiment_id)}
              activateLabel="open the experiment"
              emptyText="No experiment has been pre-registered."
            />
          </Section>
        )}
      </EnvelopeView>
    </ResearchFrame>
  );
}

// ------------------------------------------------------------------ one experiment

export function ExperimentView({ ctx, id, segment }: { ctx: ResearchCtx; id: string; segment: string | null }) {
  const q = useApi<Envelope<ExperimentDetail>>(apiPath('research', 'experiments', id));
  const trials = useApi<Envelope<Trial[]>>(apiPath('research', 'experiments', id, 'trials'), { segment: segment ?? undefined });
  return (
    <ResearchFrame ctx={ctx} queries={[q, trials]}>
      <p className="rx-identity">
        <a href="#/research/experiments">All experiments</a>
        <span className="rx-name">{id}</span>
        {q.data?.available ? <ProvenanceLabel p={q.data.provenance} /> : null}
      </p>
      <EnvelopeView q={q} what={`Experiment ${id}`}>
        {(e, prov) => <ExperimentBody e={e} prov={prov} />}
      </EnvelopeView>
      <TrialsPanel ctx={ctx} id={id} segment={segment} q={trials} />
    </ResearchFrame>
  );
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v === undefined || v === null ? '' : exactJson(v);
}

function ExperimentBody({ e, prov }: { e: ExperimentDetail; prov: Envelope<unknown>['provenance'] }) {
  const pre = e.prereg;
  const hyp = (pre.hypothesis ?? {}) as Record<string, unknown>;
  const holdout = (pre.holdout_plan ?? {}) as Record<string, unknown>;
  return (
    <div className="rx-split">
      <div>
        <Section title="Pre-registration" note={<ProvenanceNote p={prov} />}>
          <KV
            rows={[
              ['Hash', <span><Hash value={e.prereg_hash} head={16} /> {e.prereg_hash_verified ? <StatusTag status="OK" detail="re-verified by the API on read" /> : <StatusTag status="FAILED" detail="hash did not verify" />}</span>],
              ['Strategy', <span className="mono">{`${str(pre.strategy_id)} · spec `}<a href={`#/research/strategies/${str(pre.spec_hash)}`}>{str(pre.spec_hash).slice(0, 12)}…</a></span>],
              ['Registered', <span><NsTime ns={typeof pre.registered_at_ns === 'number' ? pre.registered_at_ns : null} nsStr={nsStrOf(pre, 'registered_at_ns')} /> by {str(pre.registered_by)}</span>],
              ['Hypothesis', str(hyp.statement)],
              ['Selection metric', <span className="mono">{str(pre.selection_metric)}</span>],
              ['Information clock', str(pre.information_clock)],
              ['Benchmark', str(pre.benchmark)],
              [
                'Rejection criteria',
                Array.isArray(pre.rejection_criteria) ? (
                  <ul className="rx-tree-list">
                    {pre.rejection_criteria.map((c) => (
                      <li key={str(c)}>{str(c)}</li>
                    ))}
                  </ul>
                ) : (
                  str(pre.rejection_criteria)
                ),
              ],
              ['Holdout plan', <span className="mono">{`${str(holdout.start)} → ${str(holdout.end)} · consumptions allowed ${str(holdout.consumptions_allowed)}`}</span>],
              pre.notes ? ['Notes', <span className="rx-reason">{str(pre.notes)}</span>] : null,
            ]}
          />
          <details className="rx-details rx-gap">
            <summary>Full pre-registration (as hashed)</summary>
            <JsonTree value={pre} label="Pre-registration" />
          </details>
        </Section>
      </div>
      <div>
        <Section title="Trials and budget">
          <KV
            rows={[
              ['Trials, all statuses (M)', <strong className="num">{totalTrials(e.trial_counts)}</strong>],
              ...segmentCounts(e.trial_counts).map((s) => [s.segment.toLowerCase().replace('_', ' '), <span className="num">{s.n}</span>] as const),
              ['Development budget', <span className="num">{`${e.trial_counts.DEVELOPMENT ?? 0} of ${e.budget} (${asText(formatFractionPct(e.budget_consumption, 1))} used)`}</span>],
              [
                'Search space',
                <span className="mono">
                  {Object.entries(e.search_space)
                    .map(([k, v]) => `${k} ∈ {${v.map(str).join(', ')}}`)
                    .join('; ')}
                </span>,
              ],
              ['Amendments', e.amendments.length ? <JsonTree value={e.amendments} label="Amendments" /> : <span className="muted">none</span>],
            ]}
          />
        </Section>
        <Section title={`Holdout access log · ${e.holdout_log.length}`} note="The holdout is consumed at most once per experiment, by the owner or the system evaluator; every attempt is logged, granted or refused.">
          {e.holdout_log.length ? (
            <ul className="ops-checks">
              {e.holdout_log.map((h) => (
                <li key={h.access_id}>
                  <span className="label">
                    #{h.access_id} <StatusTag status={h.outcome} />
                  </span>
                  <span>
                    {h.principal} ({h.role}) at <NsTime ns={h.at_ns} nsStr={nsStrOf(h, 'at_ns')} />
                  </span>
                  <span className="detail">
                    {h.reason}
                    {h.trial_id ? (
                      <>
                        {' · trial '}
                        <a href={`#/research/trials/${encodeURIComponent(h.trial_id)}`}>{h.trial_id}</a>
                      </>
                    ) : null}
                    {h.artifact_hash ? (
                      <>
                        {' · artifact '}
                        <a href={`#/research/artifacts/${h.artifact_hash}`}>{h.artifact_hash.slice(0, 12)}…</a>
                      </>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No access attempt: the holdout is untouched.</p>
          )}
        </Section>
      </div>
    </div>
  );
}

/** The statistic block a trial row summarises (the holdout or out-of-sample block for nested metrics). */
function rowStats(t: Trial): TrialMetrics | null {
  return statBlocks(t.metrics)[0]?.m ?? null;
}

function statCell(m: TrialMetrics | null, key: string, kind: 'fraction' | 'number' | 'count') {
  const v = numberField(m, key);
  if (v === null) return <NoValue reason="NOT_REPORTED" />;
  return <span title={String(v)}>{kind === 'fraction' ? formatFractionPct(v) : kind === 'count' ? String(v) : formatStat(v)}</span>;
}

function TrialsPanel({ ctx, id, segment, q }: { ctx: ResearchCtx; id: string; segment: string | null; q: QueryResult<Envelope<Trial[]>> }) {
  const columns: Column<Trial>[] = [
    { id: 'seq', header: '#', numeric: true, sortValue: (t) => t.seq, sortKind: 'number', cell: (t) => t.seq },
    {
      id: 'trial',
      header: 'Trial',
      sortValue: (t) => t.trial_id,
      cell: (t) => (
        <a href={`#/research/trials/${encodeURIComponent(t.trial_id)}`} tabIndex={-1} className="mono">
          {t.trial_id}
        </a>
      ),
    },
    { id: 'segment', header: 'Segment', sortValue: (t) => t.segment, cell: (t) => <span className="mono">{t.segment}</span> },
    { id: 'status', header: 'Status', sortValue: (t) => t.status, cell: (t) => <StatusTag status={t.status} detail={t.error ?? undefined} /> },
    {
      id: 'label',
      header: 'Label',
      wrap: true,
      cell: (t) => {
        const key = statBlocks(t.metrics)[0]?.key;
        return (
          <span className="mono">
            {t.label ?? <NoValue reason="NOT_REPORTED" />}
            {key ? <span className="muted" title="Statistics in this row come from this block of the trial's metrics"> [{key}]</span> : null}
          </span>
        );
      },
    },
    { id: 'sharpe', header: 'Sharpe / session', description: 'sharpe.per_period@1 (float statistic)', numeric: true, sortValue: (t) => numberField(rowStats(t), 'sharpe_per_period'), sortKind: 'number', cell: (t) => statCell(rowStats(t), 'sharpe_per_period', 'number') },
    { id: 'ret', header: 'Total return', description: 'fraction of initial cash, shown in %', numeric: true, sortValue: (t) => numberField(rowStats(t), 'total_return'), sortKind: 'number', cell: (t) => statCell(rowStats(t), 'total_return', 'fraction') },
    { id: 'dd', header: 'Max drawdown', description: 'maxdd@1, % of peak', numeric: true, sortValue: (t) => numberField(rowStats(t), 'max_drawdown'), sortKind: 'number', cell: (t) => statCell(rowStats(t), 'max_drawdown', 'fraction') },
    { id: 'n', header: 'Sessions', numeric: true, sortValue: (t) => numberField(rowStats(t), 'n'), sortKind: 'number', cell: (t) => statCell(rowStats(t), 'n', 'count') },
    { id: 'author', header: 'Author', cell: (t) => `${t.author} (${t.author_role})` },
    { id: 'started', header: 'Started', sortValue: (t) => t.started_at_ns, sortKind: 'number', cell: (t) => <NsTime ns={t.started_at_ns} nsStr={nsStrOf(t, 'started_at_ns')} /> },
    { id: 'manifest', header: 'Manifest', cell: (t) => <Hash value={t.manifest_hash} /> },
  ];
  return (
    <EnvelopeView q={q} what={`Trials of ${id}`}>
      {(rows, prov) => (
        <Section
          title={`Trials · ${rows.length}${segment ? ` in ${segment}` : ''}`}
          flush
          actions={
            <div className="field" style={{ margin: 0 }}>
              <label htmlFor="trial-segment" className="sr-only">
                Segment
              </label>
              <select
                id="trial-segment"
                className="input"
                value={segment ?? ''}
                onChange={(e) => ctx.navigate('research', 'experiments', id, ...(e.target.value ? [`segment=${e.target.value}`] : []))}
              >
                <option value="">All segments</option>
                {SEGMENTS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          }
          note={<ProvenanceNote p={prov} />}
        >
          <DataTable
            caption={`Trials of ${id}`}
            captionHidden
            columns={columns}
            rows={rows}
            rowKey={(t) => t.trial_id}
            initialSort={{ columnId: 'seq', direction: 'ascending' }}
            onRowActivate={(t) => ctx.navigate('research', 'trials', t.trial_id)}
            activateLabel="open the trial"
            emptyText="No trial in this segment."
          />
        </Section>
      )}
    </EnvelopeView>
  );
}

// ------------------------------------------------------------------ one trial

export function TrialView({ ctx, id }: { ctx: ResearchCtx; id: string }) {
  const q = useApi<Envelope<TrialDetail>>(apiPath('research', 'trials', id));
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what={`Trial ${id}`}>
        {(t, prov) => <TrialBody t={t} prov={prov} />}
      </EnvelopeView>
    </ResearchFrame>
  );
}

function TrialBody({ t, prov }: { t: TrialDetail; prov: Envelope<unknown>['provenance'] }) {
  const ccy = manifestCurrency(t.manifest);
  const path = useMemo(() => cumulativeReturns(t.returns ?? []), [t.returns]);
  const m = t.metrics;
  const blocks = statBlocks(m);
  return (
    <>
      <p className="rx-identity">
        <a href={`#/research/experiments/${encodeURIComponent(t.experiment_id)}`}>{t.experiment_id}</a>
        <span className="rx-name mono">{t.trial_id}</span>
        <ProvenanceLabel p={prov} />
        <StatusTag status={t.status} />
        <Badge>{t.segment}</Badge>
      </p>
      <div className="rx-split">
        <div>
          <Section title="Return series" note={<ProvenanceNote p={prov} />}>
            {path.length ? (
              <ReturnsChart path={path} label={t.label ?? t.trial_id} />
            ) : (
              <p className="muted">{t.returns ? 'The trial recorded no returns.' : 'No return series stored for this trial.'}</p>
            )}
          </Section>
          <Section title="Statistics" note="Floats from the research engine (each formula states its convention); money is exact in the manifest's currency.">
            {blocks.length ? (
              blocks.map((b) => <StatBlock key={b.key ?? 'run'} name={b.key} m={b.m} ccy={ccy} />)
            ) : (
              <p className="muted">{m ? 'No statistic block in these metrics; see the full metrics below.' : `No metrics recorded${t.error ? `: ${t.error}` : ''}.`}</p>
            )}
            {m ? (
              <details className="rx-details">
                <summary>All metrics (as received)</summary>
                <JsonTree value={m} label="Trial metrics" />
              </details>
            ) : null}
          </Section>
        </div>
        <div>
          <Section title="Trial">
            <KV
              rows={[
                ['Strategy', <span className="mono">{t.strategy_id} · spec <a href={`#/research/strategies/${t.spec_hash}`}>{t.spec_hash.slice(0, 12)}…</a></span>],
                ['Parameters', <span className="mono">{Object.entries(t.params).map(([k, v]) => `${k}=${v}`).join(', ')}</span>],
                ['Parameters hash', <Hash value={t.params_hash} />],
                ['Author', `${t.author} (${t.author_role})`],
                ['Started', <NsTime ns={t.started_at_ns} nsStr={nsStrOf(t, 'started_at_ns')} />],
                ['Finished', <NsTime ns={t.finished_at_ns} nsStr={nsStrOf(t, 'finished_at_ns')} />],
                ['Results hash', <Hash value={t.results_hash} />],
                ['Returns hash', <Hash value={t.returns_hash} />],
                ['Manifest hash', <Hash value={t.manifest_hash} />],
                t.error ? ['Error', <span className="rx-reason">{t.error}</span>] : null,
              ]}
            />
          </Section>
          <Section title="Reproducibility manifest">
            {t.manifest ? <JsonTree value={t.manifest} label="Manifest" /> : <p className="muted">No manifest stored for this trial.</p>}
          </Section>
        </div>
      </div>
    </>
  );
}

const BLOCK_TITLES: Record<string, string> = { holdout: 'Holdout window', oos: 'Walk-forward out of sample', full_run: 'Full run (development and holdout)' };

function StatBlock({ name, m, ccy }: { name: string | null; m: TrialMetrics; ccy: string | null }) {
  const money = MONEY_FIELDS.map((f) => [f, moneyField(m, f.key)] as const).filter(([, v]) => v !== null);
  return (
    <div className="rx-gap">
      {name ? <h3 className="rx-h3">{BLOCK_TITLES[name] ?? name}</h3> : null}
      <div className="rx-tiles">
        {STAT_FIELDS.filter((f) => numberField(m, f.key) !== null).map((f) => (
          <div key={f.key} className="rx-tile">
            <span className="micro">{f.label}</span>
            <span className="rx-tile-value">{statCell(m, f.key, f.kind)}</span>
            {f.unit ? <span className="muted"> {f.unit}</span> : null}
          </div>
        ))}
        {m.annualized_return?.value !== undefined && m.annualized_return.value !== null ? (
          <div className="rx-tile" title={m.annualized_return.convention}>
            <span className="micro">Annualized return</span>
            <span className="rx-tile-value">{formatFractionPct(m.annualized_return.value)}</span> <span className="muted">{m.annualized_return.convention}</span>
          </div>
        ) : null}
        {m.sharpe_annualized?.value !== undefined && m.sharpe_annualized.value !== null ? (
          <div className="rx-tile" title={m.sharpe_annualized.convention}>
            <span className="micro">Sharpe, annualized (K = {m.sharpe_annualized.K})</span>
            <span className="rx-tile-value">{formatStat(m.sharpe_annualized.value)}</span> <span className="muted">{m.sharpe_annualized.convention}</span>
          </div>
        ) : null}
      </div>
      {money.length ? (
        <>
          <h3 className="rx-h3">Money{name ? ` · ${BLOCK_TITLES[name] ?? name}` : ''} ({ccy ?? 'currency not stated in the manifest'})</h3>
          <KV
            rows={money.map(([f, v]) => [f.label, ccy ? <Money amount={v} currency={ccy} /> : <span className="num">{v} <span className="muted">(currency not stated)</span></span>] as const)}
          />
          {m.costs?.all_in_note ? <p className="rx-hint">{m.costs.all_in_note}</p> : null}
          {m.costs?.definitions ? (
            <ul className="rx-hint">
              {Object.entries(m.costs.definitions).map(([k, v]) => (
                <li key={k}>
                  <span className="mono">{k}</span>: {v}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function ReturnsChart({ path, label }: { path: ReturnType<typeof cumulativeReturns>; label: string }) {
  const last = path[path.length - 1]!;
  const dd = maxDrawdown(path);
  const ticks = labelIndices(path.length, 8).map((i) => ({ x: i, label: path[i]!.date }));
  return (
    <LineChart
      title={`Cumulative return of ${label}, ${path[0]!.date} to ${last.date}`}
      summary={`${path.length} sessions; cumulative ${asText(formatFractionPct(last.value))} (compounded from simple returns, return.simple@1); max drawdown of this path ${asText(formatFractionPct(dd))}. Float statistics.`}
      series={[{ id: 'cum', label: 'cumulative return', points: path.map((p, i) => ({ x: i, y: p.value * 100, label: `${p.date}: ${asText(formatFractionPct(p.value))}` })) }]}
      xTicks={ticks}
      yFormat={(v) => `${Number(v.toFixed(2))}`}
      yUnit="cumulative return, %"
      zeroLine
      height={260}
    />
  );
}
