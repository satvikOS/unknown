/**
 * Operational stress scenarios on the actual positions (CAP-PORT-11): GET /api/portfolio/stress/<name>.
 * Each result is HYPOTHETICAL: it revalues the real holdings exactly and runs the core's own reaction
 * on a private copy. A trigger requests reduction; the scenario loss is never capped at a threshold.
 * A scenario runs only when the owner presses Run (it copies the core database on the server).
 */
import { useState, type FormEvent, type ReactNode } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, StatusPill } from '../../ui/primitives';
import { QueryError } from '../../ui/WorkspaceFrame';
import { usePluginGetOnce, type OneShot } from '../plugin-api/hooks';
import { sentence, dec, isRec, num, ofKind, recs, str, STRESS_SCENARIOS, stressParamError, thresholdRows, type Rec, type ScopeOption, type StressScenario, type ThresholdRow } from './model';
import { PortfolioFrame, AsOf, FixtureNote, Lineage, QuoteSnapshotNote, ResultLabel, ScopeSelect, SnapshotStale, type FormulaDef } from './parts';

interface Run {
  scenario: StressScenario;
  scope: string;
  params: Record<string, string>;
  n: number;
}

function PnlSummary({ body }: { body: Rec }) {
  const byCcy = isRec(body.pnl_by_currency) ? Object.entries(body.pnl_by_currency) : [];
  const byOwner = recs(body.pnl_by_owner);
  if (!byCcy.length && !byOwner.length) return null;
  return (
    <div className="pf-kpis">
      {byCcy.map(([ccy, v]) => (
        <div className="pf-kpi" key={ccy}>
          <div className="k">Scenario P&amp;L, {ccy}</div>
          <div className="v">
            <Money amount={dec(v)} currency={ccy} signed />
          </div>
          <div className="s">exact revaluation of the positions in scope</div>
        </div>
      ))}
      {byOwner.map((o, i) => (
        <div className="pf-kpi" key={i}>
          <div className="k">{str(o.owner)}</div>
          <div className="v">
            <Money amount={dec(o.pnl)} currency={str(o.currency) ?? ''} signed />
          </div>
        </div>
      ))}
    </div>
  );
}

function CoreReaction({ body }: { body: Rec }) {
  const cr = isRec(body.core_reaction) ? body.core_reaction : null;
  if (!cr) return null;
  const events = recs(cr.events);
  return (
    <section className="panel pf-section" aria-labelledby="pf-cr-h">
      <header>
        <h2 id="pf-cr-h">Core reaction (private copy)</h2>
        <span className="pf-label">
          {cr.stop_latched_after === true ? <StatusPill label={`stop latch: ${str(cr.latch_reason_after) ?? 'set'}`} tone="bad" compact /> : <StatusPill label="no stop latch" tone="neutral" compact />}
          {cr.valuation_ok_after === false ? <StatusPill label="valuation incomplete" tone="warn" compact /> : null}
          {str(cr.breach_after) ? <StatusPill label={`breach ${str(cr.breach_after)}`} tone="bad" compact /> : null}
        </span>
      </header>
      <div className="panel-body">
        <p className="pf-hint" style={{ marginTop: 0 }}>
          The core's valuation command ran on a copy of core seq {num(cr.computed_on_core_seq) ?? '?'}. These events were recorded only in that copy.
        </p>
        {events.length ? (
          <ul className="pf-list">
            {events.map((e, i) => {
              const info = isRec(e.info) ? e.info : {};
              return (
                <li key={i}>
                  <strong>{str(e.type)}</strong>
                  {str(e.session_id) ? ` session ${str(e.session_id)}` : ''}
                  {str(info.reason) ? `: ${str(info.reason)}` : ''}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            No core events.
          </p>
        )}
      </div>
    </section>
  );
}

function ScenarioDetails({ body, scenario }: { body: Rec; scenario: StressScenario }) {
  if (scenario === 'stale_quote') {
    const gate = recs(body.order_gate);
    return (
      <section className="panel pf-section" aria-labelledby="pf-sq-h">
        <header>
          <h2 id="pf-sq-h">What stale quotes would block</h2>
        </header>
        <div className="panel-body">
          {str(body.finding) ? <p style={{ marginTop: 0 }}>{str(body.finding)}.</p> : null}
          <p className="pf-hint">
            Became stale: {recs(body.became_stale).map((b) => `${str(b.instrument_id)} (${str(b.owner)})`).join(', ') || 'none'}. Valuation after:{' '}
            {isRec(body.valuation_status_after) ? Object.entries(body.valuation_status_after).map(([c, s]) => `${c} ${String(s)}`).join(', ') : '—'}.
          </p>
          <ul className="pf-list">
            {gate.map((g, i) => (
              <li key={i}>
                {str(g.side)} {str(g.quantity)} {str(g.instrument_id)} at {str(g.limit_price)} for {str(g.owner)}:{' '}
                {g.approved === true ? <StatusPill label="approved" tone="ok" compact /> : <StatusPill label={`refused ${str(g.code) ?? ''}`} tone="bad" compact />}{' '}
                <span className="pf-hint">{recs(g.failed_checks).map((c) => `${str(c.check)} (${str(c.detail)})`).join('; ')}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>
    );
  }
  if (scenario === 'fee_revision') {
    const exits = recs(body.exit_costs);
    const params = isRec(body.parameters) ? body.parameters : {};
    return (
      <section className="panel pf-section" aria-labelledby="pf-fee-h">
        <header>
          <h2 id="pf-fee-h">Fee revision</h2>
        </header>
        <div className="panel-body">
          <p className="pf-hint" style={{ marginTop: 0 }}>
            {str(body.note)}
          </p>
          <p className="pf-hint">
            Model now: {isRec(params.model_now) ? JSON.stringify(params.model_now) : '—'}. Revised: {isRec(params.model_revised) ? JSON.stringify(params.model_revised) : '—'}.
          </p>
          <ul className="pf-list">
            {exits.map((x, i) => (
              <li key={i}>
                Exit {str(x.instrument_id)} ({str(x.owner)}): fee now {str(x.exit_fee_current_model)}, revised {str(x.exit_fee_revised)}, difference {str(x.difference)}
              </li>
            ))}
            {recs(body.pending_estimates).length === 0 ? <li>No pending fee estimates to finalise.</li> : null}
            {recs(body.open_buy_envelopes).length === 0 ? <li>No open buy envelopes to recompute.</li> : null}
          </ul>
        </div>
      </section>
    );
  }
  const resting = recs(body.resting_orders);
  return resting.length ? (
    <section className="panel pf-section" aria-labelledby="pf-rest-h">
      <header>
        <h2 id="pf-rest-h">Resting orders under the scenario</h2>
      </header>
      <div className="panel-body">
        <ul className="pf-list">
          {resting.map((r, i) => (
            <li key={i}>{Object.entries(r).map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join('; ')}</li>
          ))}
        </ul>
      </div>
    </section>
  ) : null;
}

function StressResult({ run, q, rerun, formulas }: { run: Run; q: OneShot<unknown>; rerun: () => void; formulas: Map<string, FormulaDef> }) {
  const body = ofKind(q.data, 'stress');
  const thresholds = body ? thresholdRows(body) : [];
  const positions = body ? recs(body.positions) : [];
  const tCols: Column<ThresholdRow>[] = [
    { id: 't', header: 'Threshold', cell: (t) => t.threshold },
    { id: 'o', header: 'Owner', cell: (t) => t.owner ?? <span className="muted">system</span> },
    { id: 'm', header: 'Measure', description: 'What Before and After measure: a session equity or the system P&L (losses negative)', cell: (t) => t.measure },
    { id: 'l', header: 'Level', numeric: true, cell: (t) => t.level ?? '—' },
    { id: 'b', header: 'Before', numeric: true, cell: (t) => t.before ?? '—' },
    { id: 'a', header: 'After', numeric: true, cell: (t) => t.after ?? '—' },
    { id: 'c', header: 'Crossed', cell: (t) => (t.crossed ? <StatusPill label="crossed" tone="bad" compact detail={t.consequence ?? undefined} /> : <span className="muted">no</span>) },
    { id: 'x', header: 'Loss beyond', numeric: true, cell: (t) => t.beyond ?? '—' },
  ];
  const pCols: Column<Rec>[] = [
    { id: 'i', header: 'Instrument', cell: (p) => <span className="cmd-id">{str(p.instrument_id)}</span> },
    { id: 'o', header: 'Owner', cell: (p) => str(p.owner) ?? '—' },
    { id: 'q', header: 'Quantity', numeric: true, cell: (p) => str(p.quantity) ?? '—' },
    { id: 's', header: 'Shock', numeric: true, cell: (p) => str(p.shock) ?? '—' },
    { id: 'pb', header: 'Price before / after', numeric: true, cell: (p) => `${str(p.price_before) ?? '—'} → ${str(p.price_after) ?? '—'}` },
    { id: 'mv', header: 'Value after', numeric: true, cell: (p) => <Money amount={dec(p.market_value_after)} currency={str(p.currency) ?? ''} /> },
    { id: 'pnl', header: 'P&L', numeric: true, cell: (p) => <Money amount={dec(p.pnl)} currency={str(p.currency) ?? ''} signed /> },
  ];
  return (
    <>
      <QueryError what="Stress scenario" error={q.error} />
      {q.status === 'loading' && !body ? <p className="muted">Loading the scenario on a private copy of the core…</p> : null}
      {isRec(q.data) && q.data.kind === 'stress' && str(q.data.fault) ? (
        <div className="callout pf-gap" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          <strong>Scenario unavailable.</strong> {str(q.data.reason)} ({str(q.data.fault)})
        </div>
      ) : null}
      {body && !str(body.fault) ? (
        <>
          <ResultLabel kind="hypothetical" text={str(body.label)} />
          <SnapshotStale seq={num(body.core_seq)} onRerun={rerun} busy={q.status === 'loading'} />
          <div className="pf-note">
            <strong>{STRESS_SCENARIOS.find((s) => s.id === run.scenario)?.label}</strong>
            <span>
              scope {str(body.scope) ?? run.scope}
              {isRec(body.parameters) ? ` · ${Object.entries(body.parameters).filter(([, v]) => typeof v === 'string').map(([k, v]) => `${k} ${String(v)}`).join(', ')}` : ''}
            </span>
            <AsOf body={body} />
          </div>
          <QuoteSnapshotNote body={body} />
          <FixtureNote body={body} />
          {str(body.note) && run.scenario !== 'fee_revision' ? <p className="pf-hint">{str(body.note)}</p> : null}
          <PnlSummary body={body} />
          {positions.length ? (
            <section className="panel pf-section" aria-labelledby="pf-sp-h">
              <header>
                <h2 id="pf-sp-h">Positions revalued</h2>
              </header>
              <div className="panel-body flush">
                <DataTable caption="Positions under the scenario" captionHidden columns={pCols} rows={positions} rowKey={(p) => `${str(p.owner)}|${str(p.instrument_id)}`} maxHeight="280px" />
              </div>
            </section>
          ) : null}
          {thresholds.length ? (
            <section className="panel pf-section" aria-labelledby="pf-th-h">
              <header>
                <h2 id="pf-th-h">Loss thresholds</h2>
                <span className="pf-hint">a crossed trigger requests reduction; execution at the threshold is not guaranteed</span>
              </header>
              <div className="panel-body flush">
                <DataTable caption="Loss thresholds under the scenario" captionHidden columns={tCols} rows={thresholds} rowKey={(t) => `${t.threshold}|${t.owner ?? ''}`} />
              </div>
              {thresholds.some((t) => t.caveat) ? <div className="panel-note">{sentence(thresholds.find((t) => t.caveat)?.caveat)}</div> : null}
            </section>
          ) : null}
          <ScenarioDetails body={body} scenario={run.scenario} />
          <CoreReaction body={body} />
          <Lineage body={body} formulas={formulas} />
        </>
      ) : null}
    </>
  );
}

export function StressTab({ bar, scope, setScope, options, formulas }: { bar: ReactNode; scope: string; setScope: (s: string) => void; options: readonly ScopeOption[]; formulas: Map<string, FormulaDef> }) {
  const [scenario, setScenario] = useState<StressScenario>('overnight_gap');
  const [param, setParam] = useState('');
  const [run, setRun] = useState<Run | null>(null);
  const once = usePluginGetOnce<unknown>();
  const start = (r: Run) => {
    setRun(r);
    void once.run(`/api/portfolio/stress/${r.scenario}`, { scope: r.scope, ...r.params });
  };
  const spec = STRESS_SCENARIOS.find((s) => s.id === scenario) ?? STRESS_SCENARIOS[0];
  const err = stressParamError(scenario, param);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (err) return;
    const params: Record<string, string> = param.trim() ? { [spec.param]: param.trim() } : {};
    start({ scenario, scope, params, n: (run?.n ?? 0) + 1 });
  };
  return (
    <PortfolioFrame tab="stress" bar={bar} queries={[]}>
      <form className="pf-toolbar" onSubmit={submit} noValidate>
        <ScopeSelect id="pf-s-scope" value={scope} options={options} onChange={setScope} />
        <div className="field">
          <label htmlFor="pf-s-scn">Scenario</label>
          <select
            id="pf-s-scn"
            className="input"
            value={scenario}
            onChange={(e) => {
              setScenario(e.target.value as StressScenario);
              setParam('');
            }}
          >
            {STRESS_SCENARIOS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="pf-s-param">{spec.paramLabel}</label>
          <input
            id="pf-s-param"
            className="input num"
            inputMode="decimal"
            autoComplete="off"
            placeholder={spec.def}
            value={param}
            onChange={(e) => setParam(e.target.value)}
            aria-invalid={err ? true : undefined}
            aria-describedby={err ? 'pf-s-param-err' : undefined}
          />
          {err ? (
            <span id="pf-s-param-err" className="form-error" role="alert">
              {err}
            </span>
          ) : null}
        </div>
        <div className="field">
          <button type="submit" className="btn primary" disabled={Boolean(err)}>
            Run scenario
          </button>
        </div>
      </form>
      <p className="pf-hint">
        Read-only: the scenario runs the core's own code on a private copy of the database. It reserves nothing, places nothing and changes nothing. Blank
        parameters use the server default ({spec.def}).
      </p>
      {run ? (
        <StressResult run={run} q={once} rerun={() => start({ ...run, n: run.n + 1 })} formulas={formulas} />
      ) : (
        <p className="muted">Choose a scenario and run it.</p>
      )}
    </PortfolioFrame>
  );
}
