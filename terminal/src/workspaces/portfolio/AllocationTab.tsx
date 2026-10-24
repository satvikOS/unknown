/**
 * Allocation proposals (CAP-PORT-13): GET /api/portfolio/allocation. Baselines (equal weight, inverse
 * volatility) are always computed; the eq. 6.2 mean-variance optimiser runs when there is enough
 * return history. For a single-session scope each method has a whole-lot trade list with every
 * constraint re-verified after rounding. Everything here is a PROPOSAL: it is never an order, and
 * this page offers no way to send it. A trade becomes real only as a new intent that passes the
 * what-if service and the independent risk gate.
 */
import { useState, type FormEvent, type ReactNode } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, Qty, StatusPill } from '../../ui/primitives';
import { QueryError } from '../../ui/WorkspaceFrame';
import { usePluginQuery } from '../plugin-api/hooks';
import { WeightBar } from './charts';
import { arr, sentence, ALLOCATION_METHODS, allocationParamErrors, fmtNum, fmtPct, isRec, num, ofKind, recs, str, weightRows, dec, type Rec, type ScopeOption, type WeightRow } from './model';
import { PortfolioFrame, AsOf, FixtureNote, Lineage, QuoteSnapshotNote, ResultLabel, ScopeSelect, Unavailable, type FormulaDef } from './parts';

interface Params {
  cash_reserve: string;
  floor: string;
  cap: string;
  eta: string;
  tau: string;
}
const DEFAULT_PARAMS: Params = { cash_reserve: '', floor: '', cap: '', eta: '', tau: '' };
const PARAM_SPECS: readonly { key: keyof Params; label: string; placeholder: string }[] = [
  { key: 'cash_reserve', label: 'Cash reserve', placeholder: '0.05' },
  { key: 'floor', label: 'Weight floor', placeholder: '0' },
  { key: 'cap', label: 'Weight cap', placeholder: '1' },
  { key: 'eta', label: 'Turnover penalty η', placeholder: '0' },
  { key: 'tau', label: 'Turnover limit τ', placeholder: 'none' },
];

export function TradeList({ name, label, tl }: { name: string; label: string; tl: Rec }) {
  const ccy = str(tl.currency) ?? '';
  if (!Array.isArray(tl.trades)) {
    return (
      <p className="pf-hint">
        {label}: {str(tl.reason) ?? 'no trade list'} {str(tl.status) ? `(${str(tl.status)})` : ''}
      </p>
    );
  }
  const checks = recs(tl.checks);
  // allocation.propose: instruments whose sizing price is a stale last mark (analytics/marks.py).
  const stale = arr(tl.stale_sizing_marks).filter((x): x is string => typeof x === 'string');
  const totals = isRec(tl.totals) ? tl.totals : {};
  const cols: Column<Rec>[] = [
    { id: 'i', header: 'Instrument', cell: (t) => <span className="cmd-id">{str(t.instrument_id)}</span> },
    { id: 's', header: 'Side', cell: (t) => str(t.side) ?? '—' },
    { id: 'q', header: 'Quantity', numeric: true, cell: (t) => <Qty value={dec(t.quantity)} /> },
    { id: 'tq', header: 'Target quantity', numeric: true, cell: (t) => <Qty value={dec(t.target_quantity)} /> },
    { id: 'p', header: 'Sizing price', numeric: true, cell: (t) => <Money amount={dec(t.sizing_price)} currency={ccy} /> },
    { id: 'n', header: 'Est. notional', numeric: true, cell: (t) => <Money amount={dec(t.estimated_notional)} currency={ccy} kind="estimated" /> },
    { id: 'f', header: 'Est. fee', numeric: true, cell: (t) => <Money amount={dec(t.estimated_fee)} currency={ccy} kind="estimated" /> },
    { id: 'r', header: 'Reservation est.', numeric: true, cell: (t) => <Money amount={dec(t.reservation_estimate)} currency={ccy} kind="estimated" /> },
  ];
  return (
    <section className="panel pf-section" aria-labelledby={`pf-tl-${name}`}>
      <header>
        <h2 id={`pf-tl-${name}`}>Trade list: {label}</h2>
        <span className="pf-label">
          {tl.constraints_hold === true ? <StatusPill label="constraints hold" tone="ok" compact /> : <StatusPill label="constraints fail" tone="bad" compact />}
          {tl.trade_list_usable === false ? <StatusPill label="not usable" tone="warn" compact /> : null}
          {stale.length ? <StatusPill label="sized on stale marks" tone="warn" compact detail={stale.join(', ')} /> : null}
        </span>
      </header>
      <div className="panel-body">
        <p className="pf-hint" style={{ marginTop: 0 }}>
          {str(tl.label) ?? 'PROPOSAL ONLY'}
        </p>
        <dl className="kv">
          <dt>Buy / sell notional</dt>
          <dd>
            <Money amount={dec(totals.buy_notional)} currency={ccy} kind="estimated" /> / <Money amount={dec(totals.sell_notional)} currency={ccy} kind="estimated" />
          </dd>
          <dt>Fees (estimated)</dt>
          <dd>
            <Money amount={dec(totals.fees)} currency={ccy} kind="estimated" />
          </dd>
          <dt>Liquid after / NAV after fees</dt>
          <dd>
            <Money amount={dec(totals.liquid_after)} currency={ccy} kind="estimated" /> / <Money amount={dec(totals.nav_after_fees)} currency={ccy} kind="estimated" />
          </dd>
        </dl>
      </div>
      <div className="panel-body flush">
        <DataTable caption={`Proposed trades, ${label}`} captionHidden columns={cols} rows={recs(tl.trades)} rowKey={(t) => `${str(t.instrument_id)}|${str(t.side)}`} emptyText="No trade: the targets equal the holdings after lot rounding." />
      </div>
      <div className="panel-body">
        <ul className="pf-list pf-checks">
          {checks.map((c, i) => (
            <li key={i}>
              <StatusPill label={c.passed === true ? 'pass' : 'fail'} tone={c.passed === true ? 'ok' : 'bad'} compact /> <code className="pf-code">{str(c.check)}</code>{' '}
              <span className="pf-hint">{str(c.detail)}</span>
            </li>
          ))}
        </ul>
        {recs(tl.left_unchanged).length ? (
          <p className="pf-hint">Left unchanged: {recs(tl.left_unchanged).map((l) => `${str(l.instrument_id)} (${str(l.reason)})`).join('; ')}</p>
        ) : null}
        {stale.length ? (
          <p className="pf-hint">
            Sized on stale marks (older than the freshness budget, or delayed): {stale.join(', ')}. The quantities may not match current prices.
          </p>
        ) : null}
        {str(tl.sequencing_note) ? <p className="pf-hint">{str(tl.sequencing_note)}</p> : null}
      </div>
    </section>
  );
}

export function AllocationTab({ bar, scope, setScope, options, formulas }: { bar: ReactNode; scope: string; setScope: (s: string) => void; options: readonly ScopeOption[]; formulas: Map<string, FormulaDef> }) {
  const [draft, setDraft] = useState<Params>(DEFAULT_PARAMS);
  const [params, setParams] = useState<Params>(DEFAULT_PARAMS);
  const errs = allocationParamErrors(draft);
  const q = usePluginQuery<unknown>('/api/portfolio/allocation', { scope, ...params });
  const body = ofKind(q.data, 'allocation');
  const rows = body ? weightRows(body) : [];
  const mv = body && isRec(body.mean_variance) ? body.mean_variance : null;
  const cov = mv && isRec(mv.covariance) ? mv.covariance : null;
  const lists = body && isRec(body.trade_lists) ? body.trade_lists : null;
  const apply = (e: FormEvent) => {
    e.preventDefault();
    if (Object.keys(errs).length) return;
    setParams({ ...draft });
  };
  const cols: Column<WeightRow>[] = [
    { id: 'i', header: 'Instrument', cell: (r) => <span className="cmd-id">{r.instrument_id}</span> },
    {
      id: 'cur',
      header: 'Current',
      numeric: true,
      cell: (r) => (
        <span>
          {fmtPct(r.current, 2)}
          <WeightBar w={r.current} />
        </span>
      ),
    },
    ...ALLOCATION_METHODS.map(
      (m): Column<WeightRow> => ({
        id: m.id,
        header: m.label,
        numeric: true,
        cell: (r) => (
          <span>
            {fmtPct(r[m.id], 2)}
            <WeightBar w={r[m.id]} />
          </span>
        ),
      }),
    ),
  ];
  return (
    <PortfolioFrame tab="allocation" bar={bar} queries={[q]}>
      <form className="pf-toolbar" onSubmit={apply} noValidate>
        <ScopeSelect id="pf-a-scope" value={scope} options={options} onChange={setScope} />
        {PARAM_SPECS.map((p) => (
          <div className="field" key={p.key} style={{ minWidth: 110 }}>
            <label htmlFor={`pf-a-${p.key}`}>{p.label}</label>
            <input
              id={`pf-a-${p.key}`}
              className="input num"
              inputMode="decimal"
              autoComplete="off"
              placeholder={p.placeholder}
              value={draft[p.key]}
              onChange={(e) => setDraft((d) => ({ ...d, [p.key]: e.target.value }))}
              aria-invalid={errs[p.key] ? true : undefined}
              aria-describedby={errs[p.key] ? `pf-a-${p.key}-err` : undefined}
            />
            {errs[p.key] ? (
              <span id={`pf-a-${p.key}-err`} className="form-error">
                {errs[p.key]}
              </span>
            ) : null}
          </div>
        ))}
        <div className="field">
          <button type="submit" className="btn" disabled={Object.keys(errs).length > 0}>
            Recompute
          </button>
        </div>
      </form>
      <QueryError what="Allocation" error={q.error} />
      <FixtureNote data={q.data} what="Allocation" />
      {body ? (
        <>
          <ResultLabel kind="proposal" text={str(body.label)} />
          <div className="pf-note">
            <AsOf body={body} />
            <span>scope {str(body.scope)}</span>
          </div>
          <QuoteSnapshotNote body={body} />
          {str(body.history_unavailable_reason) ? <Unavailable what="Return history" reason={str(body.history_unavailable_reason) as string} /> : null}
          <section className="panel pf-section" aria-labelledby="pf-w-h">
            <header>
              <h2 id="pf-w-h">Target weights of scope NAV</h2>
              <span className="pf-hint">statistical weights (floats); trade quantities below are exact</span>
            </header>
            <div className="panel-body flush">
              <DataTable caption="Current and proposed weights" captionHidden columns={cols} rows={rows} rowKey={(r) => r.instrument_id} emptyText="No instrument in the universe (no priced position in the base currency)." />
            </div>
            <div className="panel-body">
              <ul className="pf-list pf-hint">
                {ALLOCATION_METHODS.map((m) => {
                  const res = m.id === 'mean_variance' ? mv : isRec(body.baselines) && isRec(body.baselines[m.id]) ? body.baselines[m.id] : null;
                  if (!isRec(res)) return <li key={m.id}>{m.label}: not computed.</li>;
                  return (
                    <li key={m.id}>
                      {m.label}: {str(res.status) ?? '—'}
                      {num(res.cash_weight) !== null ? `, cash ${fmtPct(num(res.cash_weight), 2)}` : ''}
                      {str(res.reason) ? ` (${str(res.reason)})` : ''}
                    </li>
                  );
                })}
                {recs(body.excluded_from_risk_estimates).map((x, i) => (
                  <li key={`x${i}`}>
                    Excluded from risk estimates: {str(x.instrument_id)}, {str(x.reason)}
                  </li>
                ))}
                {recs(body.excluded_from_universe).map((x, i) => (
                  <li key={`u${i}`}>
                    Not in the universe: {str(x.instrument_id)}, {str(x.reason)}
                  </li>
                ))}
              </ul>
            </div>
          </section>
          {mv && str(mv.status) !== 'unavailable' ? (
            <section className="panel pf-section" aria-labelledby="pf-mv-h">
              <header>
                <h2 id="pf-mv-h">Mean-variance diagnostics</h2>
                <StatusPill label={str(mv.status) ?? '?'} tone={str(mv.status)?.startsWith('optimal') ? 'ok' : 'warn'} compact />
              </header>
              <div className="panel-body">
                <dl className="kv">
                  <dt>Expected returns</dt>
                  <dd>{isRec(mv.expected_returns) ? str(mv.expected_returns.source) : '—'}</dd>
                  <dt>Portfolio volatility</dt>
                  <dd className="num">{fmtPct(num(mv.portfolio_volatility), 3)} per period</dd>
                  <dt>Turnover (L1 / half)</dt>
                  <dd className="num">
                    {fmtPct(num(mv.turnover_l1), 2)} / {fmtPct(num(mv.turnover_half_l1), 2)}
                    {num(mv.turnover_limit) !== null ? ` · limit ${fmtPct(num(mv.turnover_limit), 2)}` : ''}
                  </dd>
                  <dt>KKT residual</dt>
                  <dd className="num">{fmtNum(num(mv.kkt_residual), 12)}</dd>
                  {cov ? (
                    <>
                      <dt>Covariance</dt>
                      <dd>
                        Ledoit-Wolf shrinkage toward {str(cov.target)}, intensity {fmtNum(num(cov.intensity), 3)}, {num(cov.observations) ?? '?'} observations; condition number{' '}
                        {fmtNum(num(cov.condition_number_before), 1)} → {fmtNum(num(cov.condition_number_after), 1)}; {cov.psd === true ? 'positive semidefinite' : 'not PSD'}
                      </dd>
                    </>
                  ) : null}
                </dl>
              </div>
            </section>
          ) : mv ? (
            <p className="pf-hint">Mean-variance: {str(mv.reason) ?? 'unavailable'}.</p>
          ) : null}
          {lists && str(lists.reason) ? (
            <p className="pf-hint">{sentence(str(lists.reason))} Choose a session scope for whole-lot trade lists.</p>
          ) : lists ? (
            ALLOCATION_METHODS.filter((m) => isRec(lists[m.id])).map((m) => <TradeList key={m.id} name={m.id} label={m.label} tl={lists[m.id] as Rec} />)
          ) : null}
          <Lineage body={body} formulas={formulas} />
        </>
      ) : q.status === 'loading' ? (
        <p className="muted">Loading allocation proposals…</p>
      ) : null}
    </PortfolioFrame>
  );
}
