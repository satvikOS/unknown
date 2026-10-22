/**
 * Ask a question: POST /api/research/jobs. The form says plainly that a job may spend money, shows the
 * caps the API has reported (the cap of the most recent job, and the queued job's own cap once the
 * API answers), and refuses to send until the owner acknowledges the spend. Nothing here calls a model:
 * the API only writes the job store, and the owner's model worker decides whether the job runs.
 */
import { useState, type FormEvent, type ReactNode } from 'react';
import { describeError, encodePathSegment } from '../../api/client';
import { formatMoney } from '../../format/decimal';
import { useQuery } from '../../state/react';
import { routeHref } from '../../state/router';
import { Badge, StatusPill, Timestamp } from '../../ui/primitives';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { usePluginPost, usePluginQuery } from '../plugin-api/hooks';
import {
  DEFAULTS,
  EMPTY_FORM,
  LIMITS,
  MAX_FIELDS,
  MAX_IDENTIFIERS,
  addIssuer,
  buildSubmit,
  issuerForInstrument,
  parseDetail,
  parseJobList,
  parseSubmitResponse,
  spendUse,
  stateTone,
  type FormField,
  type SubmitForm,
  type SubmitResponse,
} from './model';
import './jobs.css';

const ID = (f: FormField) => `rj-f-${f}`;

function FieldError({ field, error }: { field: FormField; error: string | undefined }) {
  return error ? (
    <div id={`${ID(field)}-err`} className="form-error" role="alert">
      {error}
    </div>
  ) : null;
}

function described(field: FormField, error: string | undefined, hint: boolean): string | undefined {
  const ids = [error ? `${ID(field)}-err` : null, hint ? `${ID(field)}-hint` : null].filter(Boolean);
  return ids.length ? ids.join(' ') : undefined;
}

/** The cap and spend of the most recent job, as the API recorded them (the only caps visible before submitting). */
function RecentCap() {
  const list = usePluginQuery<unknown>('/api/research/jobs', { limit: 1 });
  const latest = list.data === undefined ? null : (parseJobList(list.data)?.[0] ?? null);
  const detailQ = usePluginQuery<unknown>(latest ? `/api/research/jobs/${encodePathSegment(latest.job_id)}` : null);
  const detail = detailQ.data === undefined ? null : parseDetail(detailQ.data);
  if (list.status === 'success' && !latest) return <li>No job has been queued on this machine yet, so the API has not reported a cap.</li>;
  if (!detail?.envelope?.spend_cap) return null;
  const cap = detail.envelope.spend_cap;
  const use = spendUse(detail.spend, cap);
  return (
    <li>
      The most recent job (
      <a href={routeHref('research-jobs', detail.job.job_id)} className="cmd-id">
        {detail.job.job_id}
      </a>
      , {detail.job.state.toLowerCase()}) was capped at <strong className="num">{formatMoney(cap.amount, cap.currency)}</strong>
      {use ? (
        <>
          {' '}
          and has consumed <span className="num">{formatMoney(use.consumed, use.currency)}</span>
        </>
      ) : detail.spend.ledger === 'absent' ? (
        <> and has spent nothing (no spend ledger exists yet)</>
      ) : null}
      .
    </li>
  );
}

function Result({ res }: { res: SubmitResponse }) {
  const duplicate = !res.created;
  return (
    <div className="callout rj-gap" role="status" style={{ ['--tone' as string]: duplicate ? 'var(--warn)' : 'var(--ok)' }}>
      <div className="rj-inline">
        <strong>{duplicate ? 'No new job: an equivalent job already exists.' : 'Job queued.'}</strong>
        <StatusPill label={res.state || 'unknown'} tone={stateTone(res.state)} compact />
      </div>
      <dl className="kv" style={{ marginTop: 6 }}>
        <dt>Job</dt>
        <dd>
          <a href={routeHref('research-jobs', res.job_id)} className="cmd-id">
            {res.job_id}
          </a>
          {res.duplicate_of ? <span className="muted"> (the existing job; a resubmission within 24 h returns it instead of spending again)</span> : null}
        </dd>
        <dt>May spend up to</dt>
        <dd>
          {res.spend_cap ? <strong className="num">{formatMoney(res.spend_cap.amount, res.spend_cap.currency)}</strong> : <span className="muted">not reported</span>}{' '}
          <span className="muted">(this job's spend cap)</span>
        </dd>
        <dt>Deadline</dt>
        <dd>
          <Timestamp ns={res.deadline_ns} never="not reported" />
        </dd>
        <dt>Evidence spans</dt>
        <dd className="num">{res.evidence_spans ?? 'not reported'}</dd>
      </dl>
      <p className="rj-hint" style={{ margin: '6px 0 0' }}>
        Nothing has been sent to a model yet. The owner's model worker runs queued jobs; without a running worker the job stays QUEUED until its deadline.
      </p>
    </div>
  );
}

export function SubmitView() {
  const post = usePluginPost();
  const instruments = useQuery('/api/instruments');
  const [form, setForm] = useState<SubmitForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<Partial<Record<FormField, string>>>({});
  const [sending, setSending] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [result, setResult] = useState<SubmitResponse | null>(null);
  const [pick, setPick] = useState('');

  const set = <K extends FormField>(k: K, v: SubmitForm[K]) => {
    setForm((f) => ({ ...f, [k]: v }));
    setErrors((e) => {
      if (!e[k]) return e;
      const next = { ...e };
      delete next[k];
      return next;
    });
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (sending) return;
    const built = buildSubmit(form);
    setErrors(built.errors);
    setRefusal(null);
    if (!built.body) return;
    setSending(true);
    try {
      const raw = await post<unknown>('/api/research/jobs', built.body);
      const parsed = parseSubmitResponse(raw);
      if (!parsed) setRefusal('The API answered, but not with a job record; check the job list before submitting again.');
      else {
        setResult(parsed);
        setForm({ ...EMPTY_FORM });
      }
    } catch (err) {
      setRefusal(describeError(err));
    } finally {
      setSending(false);
    }
  };

  const instrumentList = instruments.data ?? [];
  const picked = instrumentList.find((i) => i.instrument_id === pick);
  const mapped = picked ? issuerForInstrument(picked) : null;
  const err = (f: FormField) => errors[f];
  let hintNote: ReactNode = null;
  if (mapped && mapped.id === null) hintNote = <span className="rj-hint">{mapped.reason}.</span>;
  else if (mapped && mapped.id !== null)
    hintNote = (
      <span className="rj-hint">
        Adds <code>{mapped.id}</code> ({mapped.basis === 'cik' ? 'SEC CIK from the instrument master' : 'the instrument symbol as a ticker'}).
      </span>
    );

  return (
    <WorkspaceFrame
      title="Ask a research question"
      description="Queues a job in the research job store. The owner's model worker runs it later; this page never calls a model."
      actions={
        <a className="btn small" href={routeHref('research-jobs')}>
          All jobs
        </a>
      }
    >
      {result ? <Result res={result} /> : null}
      <div className="rj-split">
        <section className="panel" aria-labelledby="rj-form-h">
          <header>
            <h2 id="rj-form-h">New job</h2>
            <Badge tone="warn" title="A job may spend money on a paid model provider">
              may spend money
            </Badge>
          </header>
          <form className="panel-body" onSubmit={(e) => void submit(e)} noValidate aria-describedby="rj-spend-note">
            <div className="field">
              <label htmlFor={ID('question')}>Question</label>
              <textarea
                id={ID('question')}
                className="input rj-textarea"
                value={form.question}
                onChange={(e) => set('question', e.target.value)}
                aria-invalid={err('question') ? true : undefined}
                aria-describedby={described('question', err('question'), true)}
              />
              <span id={`${ID('question')}-hint`} className="rj-hint">
                {form.question.length} / {LIMITS.question} characters. The answer comes back as a hypothesis with cited evidence, or an abstention.
              </span>
              <FieldError field="question" error={err('question')} />
            </div>

            <div className="rj-two">
              <div className="field">
                <label htmlFor={ID('role')}>Role</label>
                <input
                  id={ID('role')}
                  className="input"
                  value={form.role}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(e) => set('role', e.target.value)}
                  aria-invalid={err('role') ? true : undefined}
                  aria-describedby={described('role', err('role'), true)}
                />
                <span id={`${ID('role')}-hint`} className="rj-hint">
                  Default {DEFAULTS.role}. A role the owner policy gives no concurrency never runs.
                </span>
                <FieldError field="role" error={err('role')} />
              </div>
              <div className="field">
                <label htmlFor={ID('spendCap')}>Spend cap (optional)</label>
                <input
                  id={ID('spendCap')}
                  className="input num"
                  inputMode="decimal"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="owner per-job cap"
                  value={form.spendCap}
                  onChange={(e) => set('spendCap', e.target.value)}
                  aria-invalid={err('spendCap') ? true : undefined}
                  aria-describedby={described('spendCap', err('spendCap'), true)}
                />
                <span id={`${ID('spendCap')}-hint`} className="rj-hint">
                  In the owner's currency. It can only lower the owner's per-job cap; blank uses that cap.
                </span>
                <FieldError field="spendCap" error={err('spendCap')} />
              </div>
            </div>

            <div className="field">
              <label htmlFor={ID('issuers')}>Issuers (optional)</label>
              <input
                id={ID('issuers')}
                className="input"
                autoComplete="off"
                spellCheck={false}
                placeholder="e.g. SEC-CIK-0000320193, MSFT"
                value={form.issuers}
                onChange={(e) => set('issuers', e.target.value)}
                aria-invalid={err('issuers') ? true : undefined}
                aria-describedby={described('issuers', err('issuers'), true)}
              />
              <span id={`${ID('issuers')}-hint`} className="rj-hint">
                Up to {MAX_IDENTIFIERS} point-in-time entity ids or tickers known to the research store. Their SEC facts become the job's evidence.
              </span>
              <FieldError field="issuers" error={err('issuers')} />
            </div>
            <div className="field">
              <label htmlFor="rj-instrument">Add an issuer from an instrument</label>
              <div className="rj-inline">
                <select id="rj-instrument" className="input" value={pick} onChange={(e) => setPick(e.target.value)} aria-describedby="rj-instrument-hint">
                  <option value="">Choose an instrument…</option>
                  {instrumentList.map((i) => (
                    <option key={i.instrument_id} value={i.instrument_id}>
                      {i.instrument_id} · {i.symbol}
                      {i.attributes?.fixture === true ? ' (FIXTURE)' : ''}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn"
                  disabled={!mapped || mapped.id === null}
                  onClick={() => {
                    if (mapped && mapped.id !== null) set('issuers', addIssuer(form.issuers, mapped.id));
                  }}
                >
                  Add issuer
                </button>
              </div>
              <span id="rj-instrument-hint">{hintNote ?? <span className="rj-hint">Maps an instrument to its issuer: its SEC CIK when recorded, else its symbol.</span>}</span>
            </div>
            <div className="field">
              <label htmlFor={ID('fields')}>XBRL concepts (optional, with issuers)</label>
              <input
                id={ID('fields')}
                className="input"
                autoComplete="off"
                spellCheck={false}
                placeholder="server default set"
                value={form.fields}
                onChange={(e) => set('fields', e.target.value)}
                aria-invalid={err('fields') ? true : undefined}
                aria-describedby={described('fields', err('fields'), true)}
              />
              <span id={`${ID('fields')}-hint`} className="rj-hint">
                Up to {MAX_FIELDS} taxonomy:Concept names, e.g. us-gaap:Revenues. Blank uses the server's default concepts.
              </span>
              <FieldError field="fields" error={err('fields')} />
            </div>

            <details className="rj-details">
              <summary>Limits (optional)</summary>
              <div className="rj-two">
                {(
                  [
                    ['maxOutputTokens', 'Maximum output tokens', LIMITS.maxOutputTokens, DEFAULTS.maxOutputTokens],
                    ['deadlineSeconds', 'Deadline (seconds)', LIMITS.deadlineSeconds, DEFAULTS.deadlineSeconds],
                    ['horizonSeconds', 'Horizon (seconds)', LIMITS.horizonSeconds, DEFAULTS.horizonSeconds],
                  ] as const
                ).map(([key, label, [lo, hi], def]) => (
                  <div className="field" key={key}>
                    <label htmlFor={ID(key)}>{label}</label>
                    <input
                      id={ID(key)}
                      className="input num"
                      inputMode="numeric"
                      autoComplete="off"
                      placeholder={String(def)}
                      value={form[key]}
                      onChange={(e) => set(key, e.target.value)}
                      aria-invalid={err(key) ? true : undefined}
                      aria-describedby={described(key, err(key), true)}
                    />
                    <span id={`${ID(key)}-hint`} className="rj-hint">
                      {lo.toLocaleString('en-US')} to {hi.toLocaleString('en-US')}; default {def.toLocaleString('en-US')}.
                    </span>
                    <FieldError field={key} error={err(key)} />
                  </div>
                ))}
              </div>
            </details>

            <div className="field">
              <label className="check-row" htmlFor={ID('acknowledgedSpend')}>
                <input
                  id={ID('acknowledgedSpend')}
                  type="checkbox"
                  checked={form.acknowledgedSpend}
                  onChange={(e) => set('acknowledgedSpend', e.target.checked)}
                  aria-invalid={err('acknowledgedSpend') ? true : undefined}
                  aria-describedby={described('acknowledgedSpend', err('acknowledgedSpend'), false)}
                />
                <span>
                  I understand that this job may spend money on a paid model provider, up to its spend cap ({form.spendCap.trim() ? `${form.spendCap.trim()} in the owner's currency` : "the owner's per-job cap"}).
                </span>
              </label>
              <FieldError field="acknowledgedSpend" error={err('acknowledgedSpend')} />
            </div>
            {refusal ? (
              <div className="callout rj-gap" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
                <strong>The API did not queue the job.</strong> {refusal}
              </div>
            ) : null}
            <div className="rj-actions">
              <button type="submit" className="btn primary" disabled={sending}>
                {sending ? 'Queueing…' : 'Queue research job'}
              </button>
              <span className="rj-hint">Queueing calls no model. The worker runs the job under every owner cap.</span>
            </div>
          </form>
        </section>

        <div className="rj-stack">
          <section className="panel" aria-labelledby="rj-spend-h">
            <header>
              <h2 id="rj-spend-h">Spend</h2>
              <StatusPill label="paid calls" tone="warn" compact detail="A job may spend money up to its cap" />
            </header>
            <div className="panel-body" id="rj-spend-note">
              <p style={{ marginTop: 0 }}>
                <strong>A research job may spend money.</strong> When the owner's model worker runs it, the question and its evidence go to a paid model
                provider. One job can spend up to its <em>spend cap</em>; the owner's daily, monthly and lifetime caps in <code>agents.json</code> also
                apply.
              </p>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                <li>The default cap is the owner's per-job cap. This terminal cannot read it before submitting; the API reports each job's cap once queued.</li>
                <RecentCap />
                <li>A duplicate of a queued, running or succeeded job (same question and evidence within 24 hours) returns that job instead of spending again.</li>
                <li>Cancel stops a queued job at once and a running job at its next turn boundary.</li>
              </ul>
            </div>
          </section>
          <section className="panel" aria-labelledby="rj-advice-h">
            <header>
              <h2 id="rj-advice-h">What comes back</h2>
            </header>
            <div className="panel-body">
              <p style={{ margin: 0 }}>
                The answer is <strong>model output</strong>: a research hypothesis with quoted evidence, or an abstention. It is checked against the job's
                schema and its cited evidence spans, then recorded by the core as an <strong>advisory proposal</strong>. It is not advice. It cannot
                reserve cash, place an order or change policy, and nothing trades on it.
              </p>
            </div>
          </section>
        </div>
      </div>
    </WorkspaceFrame>
  );
}
