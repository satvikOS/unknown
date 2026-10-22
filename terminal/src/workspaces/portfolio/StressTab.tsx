/**
 * Operational stress scenarios on the actual positions (CAP-PORT-11): GET /api/portfolio/stress/<name>.
 * Each result is HYPOTHETICAL: it revalues the real holdings exactly and runs the core's own reaction
 * on a private copy. A trigger requests reduction; the scenario loss is never capped at a threshold.
 * A scenario runs only when the owner presses Run (it copies the core database on the server).
 */
import { Fragment, useState, type FormEvent, type ReactNode } from 'react';
import { formatDecimal } from '../../format/decimal';
import { useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, StatusPill } from '../../ui/primitives';
import { QueryError } from '../../ui/WorkspaceFrame';
import { usePluginGetOnce, type OneShot } from '../plugin-api/hooks';
import { arr, sentence, dec, isRec, num, ofKind, recs, scenarioParams, str, STRESS_SCENARIOS, stressParamError, thresholdRows, type Rec, type ScopeOption, type StressScenario, type ThresholdRow } from './model';
import { PortfolioFrame, AsOf, FixtureNote, Lineage, OwnerName, QuoteSnapshotNote, ResultLabel, ScopeSelect, SnapshotStale, type FormulaDef } from './parts';

interface Run {
  scenario: StressScenario;
  scope: string;
  params: Record<string, string>;
  n: number;
}

export function PnlSummary({ body }: { body: Rec }) {
  const byCcy = isRec(body.pnl_by_currency) ? Object.entries(body.pnl_by_currency) : [];
  const byOwner = recs(body.pnl_by_owner);
  // analytics/stress.py `_revalue`: an unpriced position, or one with no shock mapped, is not revalued and is
  // in no scenario P&L figure; the totals are only over the positions that were revalued.
  const unmapped = recs(body.unmapped);
  if (!byCcy.length && !byOwner.length && !unmapped.length) return null;
  return (
    <>
      <div className="pf-kpis">
        {byCcy.map(([ccy, v]) => (
          <div className="pf-kpi" key={ccy}>
            <div className="k">Scenario P&amp;L, {ccy}</div>
            <div className="v">
              <Money amount={dec(v)} currency={ccy} signed />
            </div>
            <div className="s">exact revaluation of the revalued positions, plus resting orders that fill through the gap</div>
          </div>
        ))}
        {byOwner.map((o, i) => (
          <div className="pf-kpi" key={i}>
            <div className="k">
              <OwnerName owner={str(o.owner) ?? '?'} />
            </div>
            <div className="v">
              <Money amount={dec(o.pnl)} currency={str(o.currency) ?? ''} signed />
            </div>
          </div>
        ))}
      </div>
      {unmapped.length ? (
        <div className="callout pf-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
          <strong>{unmapped.length} position(s) not revalued and left out of the scenario P&amp;L:</strong>{' '}
          {unmapped.map((u) => `${str(u.instrument_id) ?? '?'} (${str(u.owner) ?? '?'}): ${str(u.reason) ?? 'no reason given'}`).join('; ')}.
        </div>
      ) : null}
    </>
  );
}

/** An amount the response gives without a currency: shown exactly, and marked as unlabelled. */
function Unlabelled({ v }: { v: string | null }) {
  if (v === null) return <span className="muted">—</span>;
  return (
    <span className="num" title="The response does not say which currency this amount is in">
      {formatDecimal(v, { minFraction: 2 })} <span className="muted">(currency not reported)</span>
    </span>
  );
}

/**
 * The fee-revision reaction (stress.fee_revision): the revised final fees booked as broker commissions on a
 * private copy, with each affected owner's available cash and the open exceptions afterwards. It is not a
 * valuation run, so it reports no stop latch or breach.
 */
function FeeReaction({ cr }: { cr: Rec }) {
  const events = recs(cr.events);
  const cash = recs(cr.available_cash_after);
  const exceptions = arr(cr.open_exceptions_after).filter((x): x is string => typeof x === 'string');
  return (
    <section className="panel pf-section" aria-labelledby="pf-cr-h">
      <header>
        <h2 id="pf-cr-h">Core reaction (private copy)</h2>
        {exceptions.length ? <StatusPill label={`${exceptions.length} open exception(s)`} tone="bad" compact /> : <StatusPill label="no open exception" tone="neutral" compact />}
      </header>
      <div className="panel-body">
        <p className="pf-hint" style={{ marginTop: 0 }}>
          The core booked the revised final fees as broker commissions on a private copy of the database. These events were recorded only in that copy.
        </p>
        {events.length ? (
          <ul className="pf-list">
            {events.map((e, i) => {
              const info = isRec(e.info) ? e.info : {};
              return (
                <li key={i}>
                  <strong>{str(e.type)}</strong>
                  {str(info.reason) ? `: ${str(info.reason)}` : ''}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="muted">No core events.</p>
        )}
        {cash.length ? (
          <dl className="kv">
            {cash.map((c, i) => (
              <Fragment key={i}>
                <dt>
                  Available cash after, <OwnerName owner={str(c.owner) ?? '?'} />
                </dt>
                <dd>
                  <Money amount={dec(c.available)} currency={str(c.currency) ?? ''} kind="available" signed />
                </dd>
              </Fragment>
            ))}
          </dl>
        ) : null}
        {exceptions.length ? <p className="pf-hint">Open exceptions after: {exceptions.join(', ')}.</p> : null}
      </div>
    </section>
  );
}

export function CoreReaction({ body }: { body: Rec }) {
  const cr = isRec(body.core_reaction) ? body.core_reaction : null;
  if (!cr) return null;
  if (num(cr.computed_on_core_seq) === null && ('available_cash_after' in cr || 'open_exceptions_after' in cr)) return <FeeReaction cr={cr} />;
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

export function ScenarioDetails({ body, scenario, instrumentCurrency = () => null }: { body: Rec; scenario: StressScenario; instrumentCurrency?: (id: string) => string | null }) {
  if (scenario === 'stale_quote') {
    const gate = recs(body.order_gate);
    const before = isRec(body.fresh_nav_before) ? body.fresh_nav_before : {};
    const after = isRec(body.fresh_nav_after) ? body.fresh_nav_after : {};
    const ccys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
    const conservative = recs(body.conservative_values);
    const haircut = isRec(body.parameters) ? str(body.parameters.haircut) : null;
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
          {ccys.length ? (
            <dl className="kv">
              {ccys.map((c) => (
                <Fragment key={c}>
                  <dt>Fresh NAV, {c}</dt>
                  <dd>
                    <Money amount={dec(before[c])} currency={c} unavailable="not every mark fresh" /> →{' '}
                    <Money amount={dec(after[c])} currency={c} unavailable="not every mark fresh" />
                  </dd>
                </Fragment>
              ))}
            </dl>
          ) : null}
          {conservative.length ? (
            <h3 className="pf-hint" style={{ margin: '8px 0 2px' }}>
              Stale positions at their last mark and after the haircut
            </h3>
          ) : null}
          {conservative.length ? (
            <ul className="pf-list">
              {conservative.map((v, i) => {
                const ccy = instrumentCurrency(str(v.instrument_id) ?? '');
                return (
                  <li key={i}>
                    {str(v.instrument_id)} ({str(v.owner)}): at its last mark {ccy ? <Money amount={dec(v.value_at_last_mark)} currency={ccy} /> : <Unlabelled v={dec(v.value_at_last_mark)} />}, after a{' '}
                    {haircut ?? '?'} haircut {ccy ? <Money amount={dec(v.value_after_haircut)} currency={ccy} /> : <Unlabelled v={dec(v.value_after_haircut)} />}
                  </li>
                );
              })}
            </ul>
          ) : null}
          {gate.length ? (
            <h3 className="pf-hint" style={{ margin: '8px 0 2px' }}>
              The pretrade gate at the later instant
            </h3>
          ) : null}
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
    const pending = recs(body.pending_estimates);
    const envelopes = recs(body.open_buy_envelopes);
    const under = envelopes.filter((e) => dec(e.under_reserved_by) !== null);
    const extra = isRec(body.additional_cost_by_currency) ? Object.entries(body.additional_cost_by_currency) : [];
    const params = isRec(body.parameters) ? body.parameters : {};
    const model = (m: unknown) =>
      isRec(m) ? `per share ${str(m.per_share) ?? '?'}, minimum ${str(m.minimum) ?? '?'}, cap ${num(m.maximum_bps_of_notional) ?? '?'} bps of notional` : '—';
    const pCols: Column<Rec>[] = [
      { id: 'x', header: 'Execution', cell: (p) => <span className="cmd-id">{str(p.exec_key)}</span>, wrap: true },
      { id: 'o', header: 'Owner', cell: (p) => (str(p.owner) ? <OwnerName owner={str(p.owner) ?? ''} /> : <span className="muted">—</span>) },
      { id: 'e', header: 'Estimate', numeric: true, cell: (p) => <Money amount={dec(p.estimate)} currency={str(p.currency) ?? ''} kind="estimated" /> },
      { id: 'r', header: 'Revised final', numeric: true, cell: (p) => <Money amount={dec(p.revised_final)} currency={str(p.currency) ?? ''} /> },
      { id: 'a', header: 'Additional cost', numeric: true, cell: (p) => <Money amount={dec(p.additional_cost)} currency={str(p.currency) ?? ''} /> },
    ];
    const eCols: Column<Rec>[] = [
      { id: 'o', header: 'Order', cell: (e) => <span className="cmd-id">{str(e.order_id)}</span>, wrap: true },
      { id: 'h', header: 'Fee envelope left', numeric: true, cell: (e) => <Unlabelled v={dec(e.fee_envelope_remaining)} /> },
      { id: 'r', header: 'Revised fee', numeric: true, cell: (e) => <Unlabelled v={dec(e.revised_fee)} /> },
      {
        id: 'u',
        header: 'Under-reserved by',
        numeric: true,
        cell: (e) => (dec(e.under_reserved_by) !== null ? <span className="pf-label"><StatusPill label="under-reserved" tone="bad" compact /> <Unlabelled v={dec(e.under_reserved_by)} /></span> : <span className="muted">covered</span>),
      },
    ];
    return (
      <section className="panel pf-section" aria-labelledby="pf-fee-h">
        <header>
          <h2 id="pf-fee-h">Fee revision</h2>
          {under.length ? <StatusPill label={`${under.length} open buy order(s) under-reserved`} tone="bad" compact /> : null}
        </header>
        <div className="panel-body">
          <p className="pf-hint" style={{ marginTop: 0 }}>
            {sentence(str(body.note))}
          </p>
          <p className="pf-hint">
            Fee model now: {model(params.model_now)}. Revised: {model(params.model_revised)}.
          </p>
          {extra.length ? (
            <div className="pf-kpis">
              {extra.map(([ccy, v]) => (
                <div className="pf-kpi" key={ccy}>
                  <div className="k">Additional fees on pending estimates, {ccy}</div>
                  <div className="v">
                    <Money amount={dec(v)} currency={ccy} />
                  </div>
                  <div className="s">revised final minus the estimate already booked</div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
        <div className="panel-body" style={{ paddingBottom: 4 }}>
          <h3 className="pf-hint" style={{ margin: 0 }}>
            Pending fee estimates finalised at the revised fee
          </h3>
        </div>
        <div className="panel-body flush">
          <DataTable caption="Pending fee estimates finalised at the revised fee" captionHidden columns={pCols} rows={pending} rowKey={(p) => str(p.exec_key) ?? '?'} emptyText="No pending fee estimates to finalise." maxHeight="260px" />
        </div>
        <div className="panel-body" style={{ paddingBottom: 4 }}>
          <h3 className="pf-hint" style={{ margin: 0 }}>
            Open buy orders: fee envelope left against the revised fee (in the order's currency, which the response does not report)
          </h3>
        </div>
        <div className="panel-body flush">
          <DataTable caption="Open buy orders: fee envelope against the revised fee" captionHidden columns={eCols} rows={envelopes} rowKey={(e) => str(e.order_id) ?? '?'} emptyText="No open buy envelopes to recompute." maxHeight="260px" />
        </div>
        <div className="panel-body">
          {exits.length ? (
            <ul className="pf-list">
              {exits.map((x, i) => {
                const ccy = instrumentCurrency(str(x.instrument_id) ?? '');
                const amt = (v: unknown) => (ccy ? <Money amount={dec(v)} currency={ccy} /> : <Unlabelled v={dec(v)} />);
                return (
                  <li key={i}>
                    Exit {str(x.instrument_id)} ({str(x.owner)}): fee now {amt(x.exit_fee_current_model)}, revised {amt(x.exit_fee_revised)}, difference {amt(x.difference)}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="muted" style={{ margin: 0 }}>
              No priced long position to exit.
            </p>
          )}
          {exits.length ? <p className="pf-hint">Exit fees are in the position's currency, taken from the instrument master (the rows carry none).</p> : null}
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

/** A threshold amount with its currency, or the exact decimal marked as unlabelled when the currency is not known. */
function ThresholdAmount({ v, currency }: { v: string | null; currency: string | null }) {
  if (v !== null && currency) return <Money amount={v} currency={currency} />;
  return <Unlabelled v={v} />;
}

function CrossedCell({ t }: { t: ThresholdRow }) {
  if (t.crossed === null) return <StatusPill label="unknown" tone="warn" compact detail={t.reason ?? 'the backend could not decide'} />;
  return t.crossed ? <StatusPill label="crossed" tone="bad" compact detail={t.consequence ?? undefined} /> : <span className="muted">no</span>;
}

/** The scenario a stress body was computed for (its own `scenario` field), never the form's current choice. */
export function bodyScenario(body: Rec | null): StressScenario | null {
  const s = body ? str(body.scenario) : null;
  return STRESS_SCENARIOS.find((x) => x.id === s)?.id ?? null;
}

function StressResult({ run, q, rerun, formulas }: { run: Run; q: OneShot<unknown>; rerun: () => void; formulas: Map<string, FormulaDef> }) {
  const body = ofKind(q.data, 'stress');
  // While a new run loads, q.data still holds the previous result: label and lay it out by its own scenario.
  const scenario = bodyScenario(body) ?? run.scenario;
  // Account thresholds are in the account's base currency; the rows do not carry it, the account record does.
  const overview = useQuery('/api/overview');
  // Exit costs and stale-quote values carry no currency; a position is in its instrument's currency.
  const instruments = useQuery('/api/instruments');
  const instrumentCurrency = (id: string) => instruments.data?.find((i) => i.instrument_id === id)?.currency ?? null;
  const thresholds = body ? thresholdRows(body, overview.data?.account?.base_currency ?? null) : [];
  const positions = body ? recs(body.positions) : [];
  const tCols: Column<ThresholdRow>[] = [
    { id: 't', header: 'Threshold', cell: (t) => t.threshold },
    { id: 'o', header: 'Owner', cell: (t) => (t.owner ? <OwnerName owner={t.owner} /> : <span className="muted">system</span>) },
    { id: 'c', header: 'Crossed', cell: (t) => <CrossedCell t={t} /> },
    {
      id: 'm',
      header: 'Crossed when',
      description: 'Session rows: equity after the scenario at or below the level. Account rows: the loss at or above the level.',
      cell: (t) => (t.measure === 'equity' ? 'equity ≤ level' : 'loss ≥ level'),
    },
    { id: 'l', header: 'Level', numeric: true, cell: (t) => <ThresholdAmount v={t.level} currency={t.currency} /> },
    { id: 'b', header: 'Before', numeric: true, description: 'Session equity, or system P&L for account rows (losses negative)', cell: (t) => <ThresholdAmount v={t.before} currency={t.currency} /> },
    { id: 'a', header: 'After', numeric: true, description: 'Session equity, or system P&L for account rows (losses negative)', cell: (t) => <ThresholdAmount v={t.after} currency={t.currency} /> },
    { id: 'loss', header: 'Loss', numeric: true, description: 'Account rows: the loss compared with the level', cell: (t) => <ThresholdAmount v={t.loss} currency={t.currency} /> },
    { id: 'x', header: 'Loss beyond', numeric: true, cell: (t) => <ThresholdAmount v={t.beyond} currency={t.currency} /> },
  ];
  const pCols: Column<Rec>[] = [
    { id: 'i', header: 'Instrument', cell: (p) => <span className="cmd-id">{str(p.instrument_id)}</span> },
    { id: 'o', header: 'Owner', cell: (p) => (str(p.owner) ? <OwnerName owner={str(p.owner) ?? ''} /> : '—') },
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
            <strong>{STRESS_SCENARIOS.find((s) => s.id === scenario)?.label}</strong>
            <span>
              scope {str(body.scope) ?? run.scope}
              {scenarioParams(body) ? ` · ${scenarioParams(body)}` : ''}
            </span>
            <AsOf body={body} />
          </div>
          <QuoteSnapshotNote body={body} />
          <FixtureNote body={body} />
          {str(body.note) && scenario !== 'fee_revision' ? <p className="pf-hint">{str(body.note)}</p> : null}
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
              {thresholds.some((t) => t.notes.length || t.reason) ? (
                <div className="panel-note">
                  <ul className="pf-list">
                    {thresholds.flatMap((t) =>
                      [...(t.reason ? [`unknown: ${t.reason}`] : []), ...t.notes].map((n, i) => (
                        <li key={`${t.threshold}|${t.owner ?? ''}|${i}`}>
                          <span className="cmd-id">{t.threshold}</span>: {sentence(n)}
                        </li>
                      )),
                    )}
                  </ul>
                </div>
              ) : null}
              {thresholds.some((t) => t.caveat) ? <div className="panel-note">{sentence(thresholds.find((t) => t.caveat)?.caveat)}</div> : null}
            </section>
          ) : null}
          <ScenarioDetails body={body} scenario={scenario} instrumentCurrency={instrumentCurrency} />
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
