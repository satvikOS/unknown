/**
 * Equity screener (CAP-MKT-10; function EQS): point-in-time criteria over SEC and ESEF fundamentals and metrics, plus
 * prices where a real one exists (owner price inputs, or live quotes for a run as of now). Missing and not-meaningful
 * values never pass or fail silently: each criterion says whether such a row is excluded (with its reason, counted) or
 * kept as unknown. A run records its as-of time, knowledge cutoff, data versions and prices, and can be re-verified.
 * Reads /api/screener (GET fields, run, screens, runs, verify; POST screens and runs).
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { describeError } from '../../api/client';
import { Badge } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { usePluginPost } from '../plugin-api/hooks';
import { apiPath, useApi } from '../research-common/api';
import { ErrorNotice, Loading, Section } from '../research-common/parts';
import type { WorkspaceProps } from '../types';
import {
  EMPTY_DRAFT,
  OPS,
  displayValue,
  fromDefinition,
  newCriterion,
  outcomeTone,
  parseScreenerRoute,
  toDefinition,
  type Criterion,
  type Draft,
  type FieldInfo,
  type FieldsResponse,
  type RunResponse,
  type SavedScreen,
  type VerifyResponse,
} from './model';
import './screener.css';

function CriterionRow({ c, fields, onChange, onRemove, index }: {
  c: Criterion; fields: readonly FieldInfo[]; onChange: (c: Criterion) => void; onRemove: () => void; index: number;
}) {
  const f = fields.find((x) => x.field === c.field);
  const money = f?.unit === 'money' || f?.unit === 'price';
  return (
    <div className="eqs-crit" role="group" aria-label={`Criterion ${index + 1}`}>
      <select aria-label="Field" value={c.field} onChange={(e) => {
        const nf = fields.find((x) => x.field === e.target.value);
        if (nf) onChange({ ...newCriterion(nf), op: c.op, value: c.value, value2: c.value2, missing: c.missing, nm: c.nm });
      }}>
        {fields.map((x) => (
          <option key={x.field} value={x.field}>
            {x.label} ({x.unit})
          </option>
        ))}
      </select>
      <select aria-label="Operator" value={c.op} onChange={(e) => onChange({ ...c, op: e.target.value as Criterion['op'] })}>
        {OPS.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label}
          </option>
        ))}
      </select>
      <input aria-label="Value" className="num" value={c.value} onChange={(e) => onChange({ ...c, value: e.target.value })}
        placeholder={f?.unit === 'ratio' ? '0.25 = 25 %' : 'value'} size={12} />
      {c.op === 'between' ? (
        <input aria-label="Upper value" className="num" value={c.value2 ?? ''} onChange={(e) => onChange({ ...c, value2: e.target.value })} size={12} />
      ) : null}
      {money ? (
        <input aria-label="Currency" value={c.currency ?? ''} onChange={(e) => onChange({ ...c, currency: e.target.value.toUpperCase() })} size={4} maxLength={3} />
      ) : null}
      <select aria-label="If missing" value={c.missing} onChange={(e) => onChange({ ...c, missing: e.target.value as Criterion['missing'] })}>
        <option value="exclude">missing: exclude</option>
        <option value="include">missing: keep as unknown</option>
      </select>
      <select aria-label="If not meaningful" value={c.nm} onChange={(e) => onChange({ ...c, nm: e.target.value as Criterion['nm'] })}>
        <option value="exclude">NM: exclude</option>
        <option value="include">NM: keep as unknown</option>
      </select>
      <button type="button" className="btn" onClick={onRemove} aria-label={`Remove criterion ${index + 1}`}>
        Remove
      </button>
      {f ? <span className="micro muted eqs-crit-note">{f.formula} · {f.basis}{f.nm_rule ? ` · ${f.nm_rule}` : ''}</span> : null}
    </div>
  );
}

export function ResultsView({ r, fields }: { r: RunResponse; fields: readonly FieldInfo[] }) {
  const units = useMemo(() => new Map(fields.map((f) => [f.field, f])), [fields]);
  return (
    <>
      <p className="eqs-summary">
        <Badge tone="ok">{r.counts.PASS ?? 0} pass</Badge> <Badge tone="warn">{r.counts.UNKNOWN ?? 0} unknown</Badge>{' '}
        <Badge tone="bad">{r.counts.FAIL ?? 0} fail</Badge> <Badge tone="neutral">{r.counts.EXCLUDED ?? 0} excluded</Badge>{' '}
        <span className="muted">
          universe {r.coverage.universe} ({r.coverage.with_data_at_as_of} with data at the as-of time) · as of {r.as_of.slice(0, 19)}Z · known at{' '}
          {r.knowledge_cutoff.slice(0, 19)}Z
        </span>
      </p>
      {r.live_quotes ? <p className="micro muted">Live quotes: {r.live_quotes}</p> : null}
      {r.results.length ? (
        <div className="rx-table-wrap">
          <table className="rx-table eqs-table">
            <caption>Results{r.truncated ? ' (truncated to the limit)' : ''}</caption>
            <thead>
              <tr>
                <th>Issuer</th>
                <th>Source</th>
                <th>Outcome</th>
                {r.columns.map((c) => (
                  <th key={c} className="num" title={units.get(c)?.formula}>
                    {units.get(c)?.label ?? c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {r.results.map((row) => (
                <tr key={row.entity_id}>
                  <td>
                    <strong>{row.ticker ?? row.lei ?? row.cik}</strong> <span className="muted">{row.name}</span>
                  </td>
                  <td>{row.source.toUpperCase()}</td>
                  <td>
                    <Badge tone={outcomeTone(row.outcome)}>{row.outcome}</Badge>
                  </td>
                  {r.columns.map((c) => {
                    const v = row.values[c];
                    const shown = v ? displayValue(v, units.get(c)?.unit) : null;
                    return (
                      <td key={c} className="num" title={v ? `${v.value ?? ''} ${v.formula ?? ''} ${v.period ?? ''} obs ${v.input_obs_ids.join(',')}`.trim() : undefined}>
                        {shown !== null ? shown : <NoValue reason={v?.reason ?? 'ERROR'} detail={v?.detail} label={`${row.name} ${c}`} />}
                        {v && v.status === 'NM' ? <span className="micro muted"> NM</span> : null}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted" role="status">No issuer passes these criteria at this as-of time.</p>
      )}
      {r.excluded.length ? (
        <details className="eqs-excluded">
          <summary>
            {r.excluded.length} excluded:{' '}
            {Object.entries(r.excluded_by_reason).map(([k, n]) => `${k} (${n})`).join(' · ')}
          </summary>
          <ul>
            {r.excluded.map((x) => (
              <li key={x.entity_id}>
                <strong>{x.ticker ?? x.lei ?? x.cik}</strong> {x.name}:{' '}
                {x.reasons.map((rr) => (
                  <span key={rr.field}>
                    {rr.field} <NoValue reason={rr.reason ?? 'ERROR'} detail={rr.detail} /> <span className="muted">{rr.detail}</span>{' '}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <details className="eqs-manifest">
        <summary>Run manifest (reproducibility)</summary>
        <dl>
          <dt>Engine</dt>
          <dd>{r.manifest.engine}</dd>
          <dt>Definition hash</dt>
          <dd className="mono">{r.manifest.definition_hash}</dd>
          <dt>Result hash</dt>
          <dd className="mono">{r.result_hash}</dd>
          <dt>Prices used</dt>
          <dd>{Object.keys(r.manifest.price_inputs).length}</dd>
          {Object.entries(r.manifest.data_versions).map(([ds, v]) => (
            <div key={ds} className="eqs-dv">
              <dt>{ds}</dt>
              <dd>
                {v.observations} observations, max id {v.max_obs_id ?? 'none'}, latest available {v.latest_available ?? 'none'}
              </dd>
            </div>
          ))}
        </dl>
        <p className="micro muted">{r.coverage.note}</p>
      </details>
    </>
  );
}

function SavedScreens({ onLoad, onRecordRun, refreshKey }: { onLoad: (s: SavedScreen) => void; onRecordRun: (s: SavedScreen) => void; refreshKey: number }) {
  const list = useApi<{ screens: SavedScreen[]; count: number }>(apiPath('screener', 'screens'));
  const [open, setOpen] = useState<string | null>(null);
  const detail = useApi<SavedScreen>(open ? apiPath('screener', 'screens', open) : null);
  const handled = useRef(refreshKey);
  const refetchers = useRef([list.refetch, detail.refetch]);
  refetchers.current = [list.refetch, detail.refetch];
  useEffect(() => {
    if (refreshKey === handled.current) return;      // refetch once per save or recorded run
    handled.current = refreshKey;
    for (const f of refetchers.current) f();
  }, [refreshKey]);
  const [verifying, setVerifying] = useState<string | null>(null);
  const verify = useApi<VerifyResponse>(verifying ? apiPath('screener', 'runs', verifying, 'verify') : null);
  if (list.error) return <ErrorNotice what="Saved screens" error={list.error} />;
  if (!list.data) return <Loading what="saved screens" />;
  if (!list.data.screens.length) return <p className="muted">No saved screens yet.</p>;
  return (
    <ul className="eqs-saved">
      {list.data.screens.map((s) => (
        <li key={s.screen_id}>
          <strong>{s.name}</strong> <span className="muted">v{s.version} · saved {s.saved_at.slice(0, 10)} · {typeof s.runs === 'number' ? s.runs : 0} runs</span>{' '}
          <button type="button" className="btn" onClick={() => onLoad(s)}>Load</button>{' '}
          <button type="button" className="btn" onClick={() => onRecordRun(s)}>Run and record</button>{' '}
          <button type="button" className="btn" onClick={() => setOpen(open === s.screen_id ? null : s.screen_id)}>{open === s.screen_id ? 'Hide runs' : 'Runs'}</button>
          {open === s.screen_id && detail.data && Array.isArray(detail.data.runs) ? (
            <ul>
              {detail.data.runs.length ? detail.data.runs.map((r) => (
                <li key={r.run_id}>
                  <a href={`#/screener/run/${r.run_id}`}>{r.run_id}</a> <span className="muted">as of {r.as_of.slice(0, 10)} · run {r.run_at.slice(0, 19)}Z</span>{' '}
                  <button type="button" className="btn" onClick={() => setVerifying(r.run_id)}>Verify</button>
                  {verifying === r.run_id && verify.data ? (
                    <Badge tone={verify.data.reproducible ? 'ok' : 'bad'}>{verify.data.reproducible ? 'reproduced' : 'differs'}</Badge>
                  ) : null}
                </li>
              )) : <li className="muted">No recorded runs.</li>}
            </ul>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function ScreenerWorkspace({ route, navigate }: WorkspaceProps) {
  const { runId } = parseScreenerRoute(route.rest);
  const fieldsQ = useApi<FieldsResponse>(apiPath('screener', 'fields'));
  const fields = fieldsQ.data?.fields ?? [];
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [asOf, setAsOf] = useState('');
  const [knownAt, setKnownAt] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState<{ definition: string; as_of?: string; known_at?: string } | null>(null);
  const [recorded, setRecorded] = useState<RunResponse | null>(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const post = usePluginPost();
  const adhoc = useApi<RunResponse>(submitted && !runId ? apiPath('screener', 'run') : null, submitted ?? undefined);
  const stored = useApi<RunResponse>(runId ? apiPath('screener', 'runs', runId) : null);

  const build = () => {
    const out = toDefinition(draft, fields);
    if ('error' in out) {
      setProblem(out.error);
      return null;
    }
    setProblem(null);
    return out.definition;
  };
  const run = (e: FormEvent) => {
    e.preventDefault();
    const def = build();
    if (!def) return;
    setRecorded(null);
    if (runId) navigate('screener');
    setSubmitted({ definition: JSON.stringify(def), as_of: asOf || undefined, known_at: knownAt || undefined });
  };
  const save = async () => {
    const def = build();
    if (!def || !name.trim()) {
      if (!name.trim()) setProblem('Name the screen to save it.');
      return;
    }
    setBusy(true);
    try {
      const s = await post<SavedScreen>('/api/screener/screens', { name: name.trim(), definition: def });
      setNotice(`Saved ${s.name} (version ${s.version}).`);
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setProblem(describeError(err));
    } finally {
      setBusy(false);
    }
  };
  const recordRun = async (s: SavedScreen) => {
    setBusy(true);
    try {
      const r = await post<RunResponse>('/api/screener/runs', { screen_id: s.screen_id, as_of: asOf || undefined, known_at: knownAt || undefined });
      setRecorded(r);
      setSubmitted(null);
      setNotice(`Recorded run ${r.run_id} of ${s.name}.`);
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setProblem(describeError(err));
    } finally {
      setBusy(false);
    }
  };
  const shown = runId ? stored.data : recorded ?? adhoc.data;
  const shownError = runId ? stored.error : adhoc.error;
  return (
    <WorkspaceFrame
      title="Equity screener"
      description="Point-in-time screens over SEC and ESEF fundamentals with reproducible as-of runs (missing values are never silent)"
      queries={[fieldsQ, ...(runId ? [stored] : submitted ? [adhoc] : [])]}
    >
      <ErrorNotice what="Screen fields" error={fieldsQ.error} />
      <Section title="Screen">
        <form className="eqs-form" onSubmit={run}>
          <fieldset>
            <legend>Universe (issuers held in the research store)</legend>
            <label className="eqs-check">
              <input type="checkbox" checked={draft.sources.sec} onChange={(e) => setDraft({ ...draft, sources: { ...draft.sources, sec: e.target.checked } })} /> SEC (US filers)
            </label>
            <label className="eqs-check">
              <input type="checkbox" checked={draft.sources.esef} onChange={(e) => setDraft({ ...draft, sources: { ...draft.sources, esef: e.target.checked } })} /> ESEF (EU annual reports)
            </label>
            <label>
              <span>SIC from</span>
              <input value={draft.sicFrom} onChange={(e) => setDraft({ ...draft, sicFrom: e.target.value })} size={5} />
            </label>
            <label>
              <span>SIC to</span>
              <input value={draft.sicTo} onChange={(e) => setDraft({ ...draft, sicTo: e.target.value })} size={5} />
            </label>
          </fieldset>
          <fieldset>
            <legend>Criteria</legend>
            {draft.criteria.map((c, i) => (
              <CriterionRow key={i} index={i} c={c} fields={fields}
                onChange={(nc) => setDraft({ ...draft, criteria: draft.criteria.map((x, j) => (j === i ? nc : x)) })}
                onRemove={() => setDraft({ ...draft, criteria: draft.criteria.filter((_, j) => j !== i) })} />
            ))}
            <button type="button" className="btn" disabled={!fields.length}
              onClick={() => fields[0] && setDraft({ ...draft, criteria: [...draft.criteria, newCriterion(fields[0])] })}>
              Add criterion
            </button>
          </fieldset>
          <fieldset>
            <legend>Sort and run</legend>
            <label>
              <span>Sort by</span>
              <select value={draft.sortField} onChange={(e) => setDraft({ ...draft, sortField: e.target.value })}>
                <option value="">Name</option>
                {fields.map((f) => (
                  <option key={f.field} value={f.field}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Direction</span>
              <select value={draft.sortDir} onChange={(e) => setDraft({ ...draft, sortDir: e.target.value as 'asc' | 'desc' })}>
                <option value="desc">High to low</option>
                <option value="asc">Low to high</option>
              </select>
            </label>
            <label>
              <span>As of (blank = now)</span>
              <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
            </label>
            <label>
              <span>Known at (blank = run time)</span>
              <input type="date" value={knownAt} onChange={(e) => setKnownAt(e.target.value)} />
            </label>
            <button type="submit" className="btn primary" disabled={!fields.length}>
              Run
            </button>
            <label>
              <span>Name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Screen name" />
            </label>
            <button type="button" className="btn" disabled={busy} onClick={save}>
              Save screen
            </button>
          </fieldset>
        </form>
        {problem ? (
          <p className="eqs-problem" role="alert">
            {problem}
          </p>
        ) : null}
        {notice ? <p className="muted" role="status">{notice}</p> : null}
      </Section>
      <Section title="Saved screens">
        <SavedScreens refreshKey={refreshKey} onLoad={(s) => { setDraft(fromDefinition(s.definition)); setName(s.name); }} onRecordRun={recordRun} />
      </Section>
      <ErrorNotice what="Screen run" error={shownError} />
      {(runId || submitted) && !shown && !shownError ? <Loading what="screen results" /> : null}
      {shown && shown.status === 'UNAVAILABLE' ? (
        <p role="status">
          <NoValue reason={shown.reason ?? 'NO_DATA'} detail={shown.detail} focusable label="Screen" /> {shown.detail}
        </p>
      ) : null}
      {shown && shown.results ? (
        <Section title={shown.run_id ? `Run ${shown.run_id}` : 'Results'}>
          <ResultsView r={shown} fields={fields} />
        </Section>
      ) : null}
    </WorkspaceFrame>
  );
}
