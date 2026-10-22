/**
 * Performance (CAP-PORT-04/05) and daily P&L (CAP-PORT-02) from point-in-time valuation snapshots:
 * TWR with its annualization rule, Modified Dietz, the money-weighted return with a uniqueness flag,
 * drawdown on the unit-value series, risk statistics, and the journal P&L decomposition with its
 * exact identity check. Sources: GET /api/portfolio/performance and /api/portfolio/pnl.
 */
import { useState, type ReactNode } from 'react';
import { formatMoney } from '../../format/decimal';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money, StatusPill } from '../../ui/primitives';
import { QueryError } from '../../ui/WorkspaceFrame';
import { Segmented } from '../operations/parts';
import { usePluginQuery } from '../plugin-api/hooks';
import { LineChart, SignedBarChart } from './charts';
import { sentences, arr, dec, fmtNum, fmtPct, irrStatus, isRec, num, ofKind, recs, str, twrStatus, unavailableReason, type Rec, type ScopeOption } from './model';
import { PortfolioFrame, AsOf, FixtureNote, Lineage, ScopeSelect, Toolbar, Unavailable, type FormulaDef } from './parts';

type Convention = 'start_of_day' | 'end_of_day';

function Kpi({ k, v, s }: { k: string; v: string; s?: string | null }) {
  return (
    <div className="pf-kpi">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
      {s ? <div className="s">{s}</div> : null}
    </div>
  );
}

function PerformancePanel({ body, formulas }: { body: Rec; formulas: Map<string, FormulaDef> }) {
  const t = twrStatus(body);
  const irr = irrStatus(body.money_weighted);
  const stats = isRec(body.statistics) ? body.statistics : {};
  const dd = isRec(body.drawdown) ? body.drawdown : {};
  const period = isRec(body.period) ? body.period : {};
  const uv = recs(body.unit_values);
  const subs = recs(body.subperiods);
  const ccy = str(body.currency) ?? '';
  const se = isRec(stats.sharpe_standard_error) ? stats.sharpe_standard_error : null;
  const neff = isRec(stats.n_eff) ? num(stats.n_eff.n_eff) : null;
  const subCols: Column<Rec>[] = [
    { id: 'start', header: 'Start', cell: (s) => str(s.start) ?? '?', sortValue: (s) => str(s.start) ?? '' },
    { id: 'end', header: 'End', cell: (s) => str(s.end) ?? '?' },
    { id: 'bmv', header: 'BMV', numeric: true, cell: (s) => <Money amount={dec(s.bmv)} currency={ccy} /> },
    { id: 'emv', header: 'EMV', numeric: true, cell: (s) => <Money amount={dec(s.emv)} currency={ccy} /> },
    { id: 'flow', header: 'External flow', numeric: true, cell: (s) => <Money amount={dec(s.external_flow)} currency={ccy} signed /> },
    { id: 'r', header: 'Return', numeric: true, cell: (s) => fmtPct(num(s.return), 3, true), sortValue: (s) => num(s.return), sortKind: 'number' },
    { id: 'note', header: 'Note', wrap: true, cell: (s) => <span className="pf-hint">{[str(s.reason), arr(s.gap_skipped_incomplete_dates).length ? 'spans skipped incomplete dates' : null].filter(Boolean).join('; ') || '—'}</span> },
  ];
  return (
    <section className="panel pf-section" aria-labelledby="pf-perf-h">
      <header>
        <h2 id="pf-perf-h">Returns</h2>
        <AsOf body={body} />
      </header>
      <div className="panel-body">
        <FixtureNote body={body} />
        {t.reason ? (
          <div className="callout pf-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
            <strong>No time-weighted return.</strong> {t.reason}
            {t.problems.length ? (
              <ul className="pf-list">
                {t.problems.map((p, i) => (
                  <li key={i}>
                    {p.start} to {p.end}: {p.reason}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        <div className="pf-kpis">
          <Kpi k="TWR (period)" v={fmtPct(t.twr, 3, true)} s={str(period.start) ? `${str(period.start)} to ${str(period.end)} (${num(period.calendar_days) ?? '?'} days)` : null} />
          <Kpi k="TWR annualized" v={t.annualized !== null ? fmtPct(t.annualized, 2, true) : 'not annualized'} s={t.annualizedReason} />
          <Kpi k="Modified Dietz" v={fmtPct(num(body.modified_dietz), 3, true)} s="approximate money-weighted, day-weighted flows" />
          <div className="pf-kpi">
            <div className="k">Money-weighted (IRR)</div>
            <div className="v">{irr.annualized !== null ? `${fmtPct(irr.annualized, 2, true)} a year` : irr.periodMwr !== null ? `${fmtPct(irr.periodMwr, 3, true)} period` : '—'}</div>
            <div className="s">
              <StatusPill label={irr.label} tone={irr.tone} compact detail={irr.reason ?? undefined} /> {irr.flag === 'unique' ? null : irr.reason}
            </div>
          </div>
          <Kpi k="Max drawdown" v={fmtPct(num(dd.max_drawdown), 2)} s={str(dd.peak_date) ? `peak ${str(dd.peak_date)}, trough ${str(dd.trough_date)}, recovery ${str(dd.recovery_date) ?? '—'}` : str(dd.reason)} />
          <Kpi k="Current drawdown" v={fmtPct(num(dd.current_drawdown), 2)} s="on the flow-adjusted unit value" />
        </div>
        {uv.length > 1 ? (
          <LineChart
            values={uv.map((u) => num(u.unit_value) ?? NaN)}
            labels={uv.map((u) => str(u.date) ?? '')}
            title="Unit value (flow-adjusted, starts at 1000)"
            format={(v) => fmtNum(v, 2)}
          />
        ) : null}
        <h3 className="pf-hint" style={{ margin: '10px 0 4px' }}>
          Risk statistics on {num(stats.n) ?? 0} sub-period returns ({num(stats.periods_per_year) ?? '?'} periods a year, std n-1)
        </h3>
        {str(stats.insufficient_data) ? <p className="pf-hint">{str(stats.insufficient_data)}</p> : null}
        <dl className="kv">
          <dt>Volatility (annualized)</dt>
          <dd className="num">{fmtPct(num(stats.volatility_annualized), 2)}</dd>
          <dt>Sharpe</dt>
          <dd>
            <span className="num">{fmtNum(num(stats.sharpe), 2)}</span>
            {se ? <span className="pf-hint"> ± {fmtNum(num(se.n_eff), 2)} (1 s.e., n_eff {neff !== null ? fmtNum(neff, 1) : '?'}). {str(se.note)}.</span> : null}
          </dd>
          <dt>Sortino</dt>
          <dd className="num">{fmtNum(num(stats.sortino), 2)}</dd>
          <dt>Risk-free rate</dt>
          <dd>{isRec(stats.risk_free) ? `${fmtPct(num(stats.risk_free.annual), 2)} (${str(stats.risk_free.series_id) ?? 'no series'})` : '—'}</dd>
          <dt>Best / worst period</dt>
          <dd className="num">
            {fmtPct(num(stats.best_period), 3, true)} / {fmtPct(num(stats.worst_period), 3, true)}
          </dd>
          {str(stats.gap_note) ? (
            <>
              <dt>Gaps</dt>
              <dd className="pf-hint">{str(stats.gap_note)}</dd>
            </>
          ) : null}
        </dl>
        <p className="pf-hint" style={{ marginBottom: 0 }}>
          Statistics are floats computed from exact NAVs. Periods under one year are never annualized (GIPS 2.A.12). Only external flows are flows.
        </p>
      </div>
      <div className="panel-body flush">
        <DataTable caption="Sub-period returns" columns={subCols} rows={subs} rowKey={(s) => `${str(s.start)}-${str(s.end)}`} emptyText="No sub-periods." maxHeight="300px" />
      </div>
      <div className="panel-body">
        <Lineage body={body} formulas={formulas} />
      </div>
    </section>
  );
}

function PnlPanel({ body, formulas }: { body: Rec; formulas: Map<string, FormulaDef> }) {
  const ccy = str(body.currency) ?? '';
  const days = recs(body.days);
  const totals = isRec(body.totals) ? body.totals : {};
  const identity = body.identity_holds === true;
  const unexplained = recs(body.unexplained_days);
  const comp = (d: Rec, k: string) => (isRec(d.components) ? dec(d.components[k]) : null);
  const money = (v: unknown) => {
    const x = dec(v);
    return x === null ? '—' : formatMoney(x, ccy, { signDisplay: 'always' });
  };
  const cols: Column<Rec>[] = [
    { id: 'end', header: 'Day', cell: (d) => str(d.end) ?? '?', sortValue: (d) => str(d.end) ?? '' },
    { id: 'pnl', header: 'P&L', numeric: true, cell: (d) => <Money amount={dec(d.pnl)} currency={ccy} signed />, sortValue: (d) => dec(d.pnl), sortKind: 'decimal' },
    { id: 'unrl', header: 'Unrealized change', numeric: true, cell: (d) => <Money amount={dec(d.unrealized_change)} currency={ccy} signed kind="unrealized" unavailable={str(d.reason) ?? 'unpriced'} /> },
    { id: 'real', header: 'Realized', numeric: true, cell: (d) => <Money amount={comp(d, 'realized')} currency={ccy} signed kind="realized" /> },
    { id: 'fees', header: 'Fees (final / est.)', numeric: true, cell: (d) => (
      <span>
        <Money amount={comp(d, 'fees_final')} currency={ccy} signed /> / <Money amount={comp(d, 'fees_estimated')} currency={ccy} signed kind="estimated" />
      </span>
    ) },
    { id: 'flows', header: 'External flows', numeric: true, cell: (d) => <Money amount={dec(d.external_flows)} currency={ccy} signed /> },
    { id: 'nav', header: 'NAV end', numeric: true, cell: (d) => <Money amount={dec(d.nav_end)} currency={ccy} /> },
    {
      id: 'resid',
      header: 'Residual',
      numeric: true,
      description: 'P&L minus its explained components; must be exactly zero',
      cell: (d) => (dec(d.residual) === null ? <span className="pf-hint">{str(d.reason) ?? '—'}</span> : <Money amount={dec(d.residual)} currency={ccy} signed />),
    },
  ];
  return (
    <section className="panel pf-section" aria-labelledby="pf-pnl-h">
      <header>
        <h2 id="pf-pnl-h">Daily P&amp;L</h2>
        {days.length ? (
          identity ? (
            <StatusPill label="identity holds" tone="ok" compact detail="Every day's P&L equals its journal components plus the unrealized change, exactly" />
          ) : (
            <StatusPill label="identity does not hold" tone="bad" compact detail={`${unexplained.length} day(s) with a non-zero residual or an unpriced snapshot`} />
          )
        ) : null}
      </header>
      <div className="panel-body">
        <FixtureNote body={body} />
        <div className="pf-kpis">
          <Kpi k="Total P&L" v={money(totals.pnl)} s="NAV change minus external flows" />
          <Kpi k="Unrealized change" v={money(totals.unrealized_change)} />
          <Kpi k="Realized" v={money(totals.realized)} />
          <Kpi k="Fees final / estimated" v={`${money(totals.fees_final)} / ${money(totals.fees_estimated)}`} />
        </div>
        {days.length ? (
          <SignedBarChart
            items={days.filter((d) => dec(d.pnl) !== null).map((d) => ({ key: `${str(d.start)}-${str(d.end)}`, label: str(d.end) ?? '?', value: dec(d.pnl) as string }))}
            currency={ccy}
            title={`Daily P&L, ${ccy}`}
          />
        ) : (
          <p className="muted">Fewer than two complete snapshots: no day to decompose.</p>
        )}
        {isRec(body.conventions) ? (
          <p className="pf-hint" style={{ margin: 0 }}>
            {sentences([str(body.conventions.sign), str(body.conventions.day_boundary), str(body.conventions.fees)])}
          </p>
        ) : null}
      </div>
      <div className="panel-body flush">
        <DataTable caption="Daily P&L decomposition" columns={cols} rows={days} rowKey={(d) => `${str(d.start)}-${str(d.end)}`} initialSort={{ columnId: 'end', direction: 'descending' }} emptyText="No days." maxHeight="320px" />
      </div>
      <div className="panel-body">
        <Lineage body={body} formulas={formulas} />
      </div>
    </section>
  );
}

export function PerformanceTab({ bar, scope, setScope, options, formulas }: { bar: ReactNode; scope: string; setScope: (s: string) => void; options: readonly ScopeOption[]; formulas: Map<string, FormulaDef> }) {
  const [convention, setConvention] = useState<Convention>('start_of_day');
  const [ppy, setPpy] = useState<'252' | '360'>('252');
  const perfQ = usePluginQuery<unknown>('/api/portfolio/performance', { scope, convention, periods_per_year: ppy });
  const pnlQ = usePluginQuery<unknown>('/api/portfolio/pnl', { scope });
  const perf = ofKind(perfQ.data, 'performance');
  const pnl = ofKind(pnlQ.data, 'pnl');
  const perfWhy = unavailableReason(perf);
  const pnlWhy = unavailableReason(pnl);
  return (
    <PortfolioFrame tab="performance" bar={bar} queries={[perfQ, pnlQ]}>
      <Toolbar>
        <ScopeSelect id="pf-p-scope" value={scope} options={options} onChange={setScope} />
        <div className="field">
          <span className="pf-hint">Flow timing</span>
          <Segmented
            label="Flow timing convention"
            options={[
              { value: 'start_of_day', label: 'Start of day' },
              { value: 'end_of_day', label: 'End of day' },
            ]}
            value={convention}
            onChange={setConvention}
          />
        </div>
        <div className="field">
          <span className="pf-hint">Periods a year</span>
          <Segmented
            label="Periods per year for annualized statistics"
            options={[
              { value: '252', label: '252' },
              { value: '360', label: '360 (IBKR)' },
            ]}
            value={ppy}
            onChange={setPpy}
          />
        </div>
      </Toolbar>
      <QueryError what="Performance" error={perfQ.error} />
      <QueryError what="P&L" error={pnlQ.error} />
      {perfWhy ? <Unavailable what="Performance" reason={perfWhy} /> : perf ? <PerformancePanel body={perf} formulas={formulas} /> : perfQ.status === 'loading' ? <p className="muted">Loading performance…</p> : null}
      {pnlWhy ? (perfWhy ? null : <Unavailable what="P&L" reason={pnlWhy} />) : pnl ? <PnlPanel body={pnl} formulas={formulas} /> : null}
    </PortfolioFrame>
  );
}
