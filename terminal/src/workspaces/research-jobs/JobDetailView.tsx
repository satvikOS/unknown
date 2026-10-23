/**
 * One research job: state and reason, request and evidence, caps and spend, the advisory proposal
 * (model output, fenced off and never presented as advice), the job-store events and the hash-chained
 * audit trail. Cancel posts to /api/research/jobs/<id>/cancel.
 */
import { useEffect, useState, type FormEvent } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import { describeError, encodePathSegment } from '../../api/client';
import { formatMoney } from '../../format/decimal';
import { bpsToPercent } from '../../format/exact';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge, Money, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { usePluginPost, usePluginQuery } from '../plugin-api/hooks';
import { ACTIVE_POLL_MS } from './JobListView';
import {
  auditSummary,
  cancelAvailability,
  deliveryTone,
  horizonText,
  needsPolling,
  parseCancelResponse,
  parseDetail,
  proposalHeadline,
  proposalLabelExplanation,
  reasonSource,
  spendUse,
  stateExplanation,
  stateTone,
  type AuditRecord,
  type JobDetail,
  type JobEvent,
  type Proposal,
  type SpendReservation,
} from './model';
import './jobs.css';

export function ModelOutput({ p, context }: { p: Proposal; context: string }) {
  return (
    <div className="rj-model" aria-label={`Model output: ${context}`} role="group">
      <div className="rj-model-head">
        <Badge tone="warn" title="Text written by a language model; checked against the job schema and cited spans only">
          model output
        </Badge>
        <span>Not advice. A research statement for the owner to verify; it cannot reserve, order or change policy.</span>
      </div>
      <p style={{ margin: '0 0 6px' }}>
        <strong>{proposalHeadline(p)}</strong>
      </p>
      {p.abstain ? null : (
        <>
          <h3>Hypothesis (model text)</h3>
          <blockquote>{p.hypothesis || '(empty)'}</blockquote>
        </>
      )}
      <h3>{p.abstain ? 'Why it abstained (model text)' : 'Reasoning (model text)'}</h3>
      <blockquote>{p.reason || '(empty)'}</blockquote>
      {p.schema_version === 2 ? (
        <>
          <h3>Verified claims ({p.claims.length})</h3>
          {p.claims.length ? (
            <ol>
              {p.claims.map((c, i) => (
                <li key={i}>
                  {c.text}
                  {c.quote ? (
                    <div className="rj-hint">
                      Quote checked verbatim against the span: <q>{c.quote}</q>
                    </div>
                  ) : null}
                  <div className="rj-hint">Cites {citedSpans(p, c.evidence_indexes)}.</div>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted" style={{ margin: '2px 0 8px' }}>
              None carried as evidence (a claim counts only when it is verified and cites a span with store provenance).
            </p>
          )}
        </>
      ) : (
        <>
          <h3>Cited evidence ({p.evidence.length})</h3>
          {p.evidence.length ? (
            <ol>
              {p.evidence.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ol>
          ) : (
            <p className="muted" style={{ margin: '2px 0 8px' }}>
              None cited.
            </p>
          )}
        </>
      )}
      <h3>Assumptions ({p.assumptions.length})</h3>
      {p.assumptions.length ? (
        <ul>
          {p.assumptions.map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      ) : (
        <p className="muted" style={{ margin: '2px 0 8px' }}>
          None stated.
        </p>
      )}
      {p.horizon_seconds !== null ? <p className="rj-hint" style={{ margin: 0 }}>Horizon: {horizonText(p.horizon_seconds)}.</p> : null}
    </div>
  );
}

/** The span ids behind a claim's evidence indexes (an index with no reference is shown as such, never dropped). */
function citedSpans(p: Proposal, indexes: readonly number[]): string {
  if (!indexes.length) return 'no evidence reference';
  return indexes.map((i) => `[${i}] ${p.evidence_refs[i]?.span_id ?? 'missing reference'}`).join(', ');
}

/**
 * The typed fields of a version-2 proposal. They come from the job and from the research-store rows behind the
 * cited spans, never from the model, so they sit outside the model-output fence.
 */
export function ProposalTypedFields({ p }: { p: Proposal }) {
  if (p.schema_version !== 2) {
    return (
      <p className="rj-hint" style={{ margin: '8px 0 0' }}>
        Proposal schema version {p.schema_version ?? 'unknown'}: free-text evidence, with no typed evidence references, instrument ids or timestamps.
      </p>
    );
  }
  const w = p.observation_window;
  return (
    <div role="group" aria-label="Typed fields from the job and the research store" style={{ marginTop: 10 }}>
      <h3>Typed fields (from the job and the research store, not model text)</h3>
      <dl className="kv">
        <dt>As of</dt>
        <dd>
          <Timestamp ns={p.as_of_ns} never="—" />
        </dd>
        <dt>Instruments</dt>
        <dd>{p.instrument_ids.length ? p.instrument_ids.join(', ') : <span className="muted">none named</span>}</dd>
        <dt>Entities</dt>
        <dd>{p.entity_ids.length ? p.entity_ids.join(', ') : <span className="muted">none</span>}</dd>
        <dt>Observation window</dt>
        <dd>
          {w ? (
            <>
              <Timestamp ns={w.from_ns} never="—" /> to <Timestamp ns={w.to_ns} never="—" />
            </>
          ) : (
            <span className="muted">none</span>
          )}
        </dd>
        <dt>Forecast</dt>
        <dd>{p.has_forecast ? 'present' : <span className="muted">none (the agent layer never emits one)</span>}</dd>
      </dl>
      <h3>Evidence references ({p.evidence_refs.length})</h3>
      {p.evidence_refs.length ? (
        <ul className="rj-spans">
          {p.evidence_refs.map((r, i) => (
            <li key={`${i}-${r.span_id}`}>
              <span className="muted">[{i}]</span> <strong className="cmd-id">{r.span_id}</strong> <span className="muted">{r.dataset_id}</span> {r.ref_type}{' '}
              <span className="cmd-id">{r.ref_id}</span>
              <div className="rj-hint">
                observed <Timestamp ns={r.observed_at_ns} never="unknown" />, available <Timestamp ns={r.available_at_ns} never="unknown" />, span hash{' '}
                <ShortId value={r.content_hash} head={12} />
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted" style={{ margin: '2px 0 8px' }}>
          None.
        </p>
      )}
    </div>
  );
}

export function ProposalPanel({ d }: { d: JobDetail }) {
  const rec = d.recorded_proposal;
  const same = rec ? rec.matches_job_output : null;
  let empty: string;
  switch (d.job.state) {
    case 'QUEUED':
    case 'RUNNING':
      empty = 'No proposal yet: the job has not finished.';
      break;
    case 'SUCCEEDED':
      empty = 'The job succeeded but no proposal is stored.';
      break;
    default:
      empty = `No proposal: the job ended ${d.job.state.toLowerCase()}${d.job.discarded_output_hash ? '; its output was discarded (hash kept in the job record)' : ''}.`;
  }
  return (
    <section className="panel" aria-labelledby="rj-prop-h">
      <header>
        <h2 id="rj-prop-h">Advisory proposal</h2>
        {rec ? (
          <Badge tone="info" title="The core recorded it as advisory only: it cannot reserve, order or change policy">
            recorded by the core
          </Badge>
        ) : d.proposal ? (
          <StatusPill label={`delivery ${d.job.delivery}`} tone={deliveryTone(d.job.delivery)} compact />
        ) : null}
      </header>
      <div className="panel-body">
        {d.proposal ? (
          <>
            <ModelOutput p={d.proposal} context="the job's proposal" />
            <ProposalTypedFields p={d.proposal} />
          </>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            {empty}
          </p>
        )}
        {rec ? (
          <dl className="kv" style={{ marginTop: 10 }}>
            <dt>Core proposal</dt>
            <dd>
              <span className="cmd-id">{rec.proposal_id}</span>
              {rec.missing_from_core_db ? <Badge tone="bad">missing from the core database</Badge> : null}
              {rec.error ? <span className="muted"> ({rec.error})</span> : null}
            </dd>
            <dt>Author</dt>
            <dd>{rec.author ?? <span className="muted">unknown</span>}</dd>
            <dt>Core label</dt>
            <dd>
              {rec.label ? (
                <>
                  <StatusPill label={rec.label} tone={rec.label === 'FORECAST_QUANTIFIED' ? 'info' : 'neutral'} compact />{' '}
                  <span className="rj-hint">{proposalLabelExplanation(rec.label)}</span>
                </>
              ) : (
                <span className="muted">none (the core labels only schema version 2)</span>
              )}
            </dd>
            <dt>Recorded</dt>
            <dd>
              <Timestamp ns={rec.recorded_at_ns} nsStr={nsStrOf(rec, 'recorded_at_ns')} never="unknown" />
            </dd>
            <dt>Matches the job's output</dt>
            <dd>
              {same === null ? (
                <span className="muted">not comparable</span>
              ) : same ? (
                <StatusPill label="identical" tone="ok" compact />
              ) : (
                <StatusPill label="differs" tone="bad" compact detail="The core holds a different body than the job store" />
              )}
            </dd>
            <dt>Effect</dt>
            <dd>Advisory only: it cannot reserve cash, place an order or change policy.</dd>
          </dl>
        ) : null}
        {d.job.delivery_error ? (
          <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--warn)', marginTop: 8 }}>
            Delivery to the core: {d.job.delivery_error} ({d.job.delivery_attempts} attempt(s)).
          </div>
        ) : null}
      </div>
    </section>
  );
}

function CancelForm({ d, onDone }: { d: JobDetail; onDone: () => void }) {
  const post = usePluginPost();
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const can = cancelAvailability(d.job);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !can.allowed) return;
    setBusy(true);
    setMsg(null);
    try {
      const body = reason.trim() ? { reason: reason.trim().slice(0, 500) } : {};
      const res = parseCancelResponse(await post<unknown>(`/api/research/jobs/${encodePathSegment(d.job.job_id)}/cancel`, body));
      setMsg(
        res
          ? { tone: 'ok', text: res.state === 'CANCELLED' ? 'Cancelled.' : res.cancel_requested ? 'Cancel requested: the worker stops at the next turn boundary.' : `The job is ${res.state}.` }
          : { tone: 'bad', text: 'The API answered without a job state; refresh to see the job.' },
      );
      onDone();
    } catch (err) {
      setMsg({ tone: 'bad', text: describeError(err) });
    } finally {
      setBusy(false);
    }
  };
  const message = msg ? (
    <div className="callout" role={msg.tone === 'bad' ? 'alert' : 'status'} style={{ ['--tone' as string]: msg.tone === 'ok' ? 'var(--ok)' : 'var(--bad)', marginTop: 6 }}>
      {msg.text}
    </div>
  ) : null;
  // A finished job offers no cancel control; a result from this page stays visible after the state changes.
  if (!can.allowed && d.job.state !== 'RUNNING') return message;
  return (
    <form onSubmit={(e) => void submit(e)} className="rj-gap" style={{ marginTop: 10 }}>
      <label htmlFor="rj-cancel-reason" className="rj-hint">
        Cancel reason (optional)
      </label>
      <div className="rj-inline" style={{ marginTop: 4 }}>
        <input id="rj-cancel-reason" className="input" value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} disabled={!can.allowed} />
        <button type="submit" className="btn" disabled={!can.allowed || busy} aria-describedby="rj-cancel-why">
          {busy ? 'Cancelling…' : 'Cancel job'}
        </button>
      </div>
      <div id="rj-cancel-why" className="rj-hint" style={{ marginTop: 4 }}>
        {can.why}
      </div>
      {message}
    </form>
  );
}

function SpendPanel({ d }: { d: JobDetail }) {
  const env = d.envelope;
  const cap = env?.spend_cap ?? null;
  const use = spendUse(d.spend, cap);
  const pct = use?.bps ?? null;
  const width = pct === null ? 0 : Math.min(100, Math.max(0, Number(pct) / 100));
  const cols: Column<SpendReservation>[] = [
    { id: 'purpose', header: 'Purpose', cell: (r) => r.purpose },
    { id: 'model', header: 'Provider / model', cell: (r) => `${r.provider}/${r.model_id}`, wrap: true },
    { id: 'reserved', header: 'Reserved', numeric: true, cell: (r) => <Money amount={r.reserved} currency={r.currency} kind="held" /> },
    { id: 'actual', header: 'Actual', numeric: true, cell: (r) => <Money amount={r.actual} currency={r.currency} unavailable="not settled" /> },
    { id: 'state', header: 'State', cell: (r) => <StatusPill label={r.state} tone={r.state === 'SETTLED' ? 'ok' : r.state === 'UNCONFIRMED' ? 'warn' : 'neutral'} compact /> },
    { id: 'note', header: 'Note', cell: (r) => r.note || <span className="muted">—</span>, wrap: true },
  ];
  return (
    <section className="panel" aria-labelledby="rj-spend-h">
      <header>
        <h2 id="rj-spend-h">Caps and spend</h2>
        {use?.over ? <StatusPill label="over cap" tone="bad" compact /> : null}
      </header>
      <div className="panel-body">
        <dl className="kv">
          <dt>Spend cap</dt>
          <dd>{cap ? <strong className="num">{formatMoney(cap.amount, cap.currency)}</strong> : <span className="muted">not in the envelope</span>}</dd>
          <dt>Consumed</dt>
          <dd>
            {d.spend.ledger === 'absent' ? (
              <span className="muted">nothing: the spend ledger does not exist yet (no job has reserved money on this machine)</span>
            ) : use ? (
              <span className="num">
                {formatMoney(use.consumed, use.currency)} ({pct === null ? 'no ratio' : bpsToPercent(pct)} of the cap)
              </span>
            ) : d.spend.consumed !== null ? (
              <span className="num">
                {d.spend.consumed} {d.spend.currency ?? '(currency not reported)'}
              </span>
            ) : (
              <span className="muted">not reported</span>
            )}
          </dd>
          <dt>Token cap</dt>
          <dd className="num">{env?.token_cap?.toLocaleString('en-US') ?? '—'}</dd>
          <dt>Turns / retries / tools</dt>
          <dd className="num">
            {env?.max_turns ?? '—'} / {env?.retry_budget ?? '—'} / {env?.max_tool_calls ?? '—'}
          </dd>
          <dt>Deadline</dt>
          <dd>
            <Timestamp
              ns={env?.deadline_ns ?? d.job.deadline_ns}
              nsStr={env?.deadline_ns !== null && env?.deadline_ns !== undefined ? nsStrOf(env, 'deadline_ns') : nsStrOf(d.job, 'deadline_ns')}
              never="—"
            />
          </dd>
          <dt>Output schema</dt>
          <dd>
            {env?.output_schema ?? '—'} v{env?.schema_version ?? '?'}
          </dd>
        </dl>
        {use ? (
          <div
            className="rj-meter"
            role="meter"
            aria-label="Spend consumed of the job's cap"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={width}
            aria-valuetext={`${formatMoney(use.consumed, use.currency)} of ${formatMoney(use.cap, use.currency)}`}
            style={{ marginTop: 8, height: 8, border: '1px solid var(--border-strong)', borderRadius: 3, background: 'var(--surface-3)', overflow: 'hidden' }}
          >
            <div style={{ width: `${width}%`, height: '100%', background: use.over ? 'var(--bad)' : 'var(--accent)' }} />
          </div>
        ) : null}
        <p className="rj-hint" style={{ margin: '8px 0 0' }}>
          Consumed follows the spend ledger: open and unconfirmed reservations count their reserved amount, settled ones their actual amount, released
          ones nothing. The owner's daily cap, and any monthly, lifetime, per-role or per-provider caps, also apply in the worker.
        </p>
      </div>
      {d.spend.reservations.length ? (
        <div className="panel-body flush">
          <DataTable caption="Spend ledger reservations for this job" captionHidden columns={cols} rows={d.spend.reservations} rowKey={(r) => r.reservation_id} maxHeight="240px" />
        </div>
      ) : null}
    </section>
  );
}

export function JobDetailView({ jobId }: { jobId: string }) {
  const [active, setActive] = useState(true);
  const q = usePluginQuery<unknown>(`/api/research/jobs/${encodePathSegment(jobId)}`, undefined, { pollMs: active ? ACTIVE_POLL_MS : undefined });
  const d = q.data === undefined ? null : parseDetail(q.data);
  const nowActive = d ? needsPolling([d.job]) : q.status !== 'error';
  useEffect(() => setActive(nowActive), [nowActive]);

  const eventCols: Column<JobEvent>[] = [
    { id: 'seq', header: 'Seq', numeric: true, cell: (e) => e.seq },
    { id: 'at', header: 'At', cell: (e) => <Timestamp ns={e.at_ns} nsStr={nsStrOf(e, 'at_ns')} withMs /> },
    { id: 'kind', header: 'Event', cell: (e) => e.kind },
    { id: 'detail', header: 'Detail', cell: (e) => <span className="cmd-id">{e.detail}</span>, wrap: true },
  ];
  const auditCols: Column<AuditRecord>[] = [
    { id: 'seq', header: 'Seq', numeric: true, cell: (a) => a.seq },
    { id: 'at', header: 'At', cell: (a) => <Timestamp ns={a.at_ns} nsStr={nsStrOf(a, 'at_ns')} withMs /> },
    { id: 'actor', header: 'Actor', cell: (a) => a.actor },
    { id: 'kind', header: 'Record', cell: (a) => a.kind },
    { id: 'summary', header: 'Summary', cell: (a) => <span className="rj-hint">{auditSummary(a.record)}</span>, wrap: true },
    { id: 'hash', header: 'Chain hash', cell: (a) => <ShortId value={a.chain_hash} head={12} /> },
  ];

  return (
    <WorkspaceFrame
      title="Research job"
      description={jobId}
      queries={[q]}
      actions={
        <a className="btn small" href={routeHref('research-jobs')}>
          All jobs
        </a>
      }
    >
      <QueryError what="The research job" error={q.error} />
      {q.data !== undefined && !d ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          The job response did not have the expected shape; nothing is shown rather than a guess.
        </div>
      ) : null}
      {!d ? (
        q.status === 'loading' ? <p className="muted">Loading the job…</p> : null
      ) : (
        <>
          <div className="rj-split">
            <div className="rj-stack">
              <section className="panel" aria-labelledby="rj-state-h">
                <header>
                  <h2 id="rj-state-h">State</h2>
                  <span className="rj-inline">
                    <StatusPill label={d.job.state} tone={stateTone(d.job.state)} />
                    {d.job.cancel_requested && d.job.state === 'RUNNING' ? <Badge tone="warn">cancel requested</Badge> : null}
                  </span>
                </header>
                <div className="panel-body">
                  <p style={{ marginTop: 0 }}>{stateExplanation(d.job)}</p>
                  <dl className="kv">
                    <dt>Outcome</dt>
                    <dd>{d.job.outcome || <span className="muted">—</span>}</dd>
                    <dt>Reason</dt>
                    <dd>
                      {d.job.reason ? <span className="rj-text">{d.job.reason}</span> : <span className="muted">—</span>}
                      {d.job.reason ? <span className="rj-hint"> ({reasonSource(d.job, d.events)})</span> : null}
                    </dd>
                    <dt>Submitted</dt>
                    <dd>
                      <Timestamp ns={d.job.submitted_ns} nsStr={nsStrOf(d.job, 'submitted_ns')} /> by {d.job.submitted_by || 'unknown'}
                    </dd>
                    <dt>Started</dt>
                    <dd>
                      <Timestamp ns={d.job.started_ns} nsStr={nsStrOf(d.job, 'started_ns')} never="not started" />
                      {d.job.start_attempts ? <span className="muted"> (attempt {d.job.start_attempts}{d.job.worker_id ? `, worker ${d.job.worker_id}` : ''})</span> : null}
                    </dd>
                    <dt>Finished</dt>
                    <dd>
                      <Timestamp ns={d.job.finished_ns} nsStr={nsStrOf(d.job, 'finished_ns')} never="not finished" />
                    </dd>
                    <dt>Deadline</dt>
                    <dd>
                      <Timestamp ns={d.job.deadline_ns} nsStr={nsStrOf(d.job, 'deadline_ns')} never="—" />
                    </dd>
                    <dt>Delivery</dt>
                    <dd>
                      <StatusPill label={d.job.delivery} tone={deliveryTone(d.job.delivery)} compact /> <span className="muted">{d.job.delivery_attempts} attempt(s)</span>
                    </dd>
                    <dt>Output hash</dt>
                    <dd>
                      <ShortId value={d.job.output_hash} head={16} />
                      {d.job.discarded_output_hash ? (
                        <>
                          {' '}
                          <span className="muted">discarded:</span> <ShortId value={d.job.discarded_output_hash} head={16} />
                        </>
                      ) : null}
                    </dd>
                  </dl>
                  <CancelForm d={d} onDone={q.refetch} />
                </div>
              </section>
              <ProposalPanel d={d} />
            </div>
            <div className="rj-stack">
              <section className="panel" aria-labelledby="rj-req-h">
                <header>
                  <h2 id="rj-req-h">Request</h2>
                  <span className="muted">role {d.request?.role ?? d.job.role}</span>
                </header>
                <div className="panel-body">
                  <p className="rj-text" style={{ marginBottom: 8 }}>
                    {d.request?.question ?? d.job.question ?? '(no question recorded)'}
                  </p>
                  <dl className="kv">
                    <dt>Issuers</dt>
                    <dd>{d.request?.issuer_ids.length ? d.request.issuer_ids.join(', ') : <span className="muted">none (no evidence)</span>}</dd>
                    <dt>Instruments</dt>
                    <dd>{d.request?.instrument_ids.length ? d.request.instrument_ids.join(', ') : <span className="muted">none named</span>}</dd>
                    <dt>Concepts</dt>
                    <dd>{d.request?.fields?.length ? d.request.fields.join(', ') : <span className="muted">{d.request?.issuer_ids.length ? 'server default set' : '—'}</span>}</dd>
                    <dt>Evidence as of</dt>
                    <dd>
                      <Timestamp ns={d.evidence?.selection?.as_of_ns} nsStr={nsStrOf(d.evidence?.selection, 'as_of_ns')} never="—" />
                    </dd>
                    <dt>Entities</dt>
                    <dd>{d.evidence?.selection?.entity_ids.join(', ') || <span className="muted">—</span>}</dd>
                  </dl>
                  {d.evidence && d.evidence.spans.length ? (
                    <details className="rj-details" style={{ marginTop: 8 }}>
                      <summary>Evidence spans ({d.evidence.spans.length}), point-in-time SEC facts</summary>
                      <ul className="rj-spans">
                        {d.evidence.spans.map((s) => (
                          <li key={s.span_id}>
                            <strong className="cmd-id">{s.span_id}</strong> <span className="muted">{s.dataset_id}</span>
                            <div>{s.text}</div>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </div>
              </section>
              <SpendPanel d={d} />
            </div>
          </div>
          <section className="panel section-gap" aria-labelledby="rj-audit-h">
            <header>
              <h2 id="rj-audit-h">Audit trail</h2>
              {d.audit_chain.ok ? (
                <StatusPill label="hash chain verified" tone="ok" compact detail="Every audit row's hash links to the previous one" />
              ) : (
                <StatusPill label="hash chain problems" tone="bad" compact detail={d.audit_chain.problems.join('; ')} />
              )}
            </header>
            {!d.audit_chain.ok && d.audit_chain.problems.length ? (
              <div className="panel-body">
                <ul style={{ margin: 0 }}>
                  {d.audit_chain.problems.map((p, i) => (
                    <li key={i}>{p}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            <div className="panel-body flush">
              <DataTable
                caption="Audit trail records for this job"
                captionHidden
                columns={auditCols}
                rows={d.audit}
                rowKey={(a) => String(a.seq)}
                emptyText="No audit records: no worker has acted on this job."
                maxHeight="360px"
              />
            </div>
          </section>
          <section className="panel section-gap" aria-labelledby="rj-events-h">
            <header>
              <h2 id="rj-events-h">Job store events</h2>
            </header>
            <div className="panel-body flush">
              <DataTable caption="Job store events" captionHidden columns={eventCols} rows={d.events} rowKey={(e) => String(e.seq)} emptyText="No events." maxHeight="280px" />
            </div>
          </section>
        </>
      )}
    </WorkspaceFrame>
  );
}
