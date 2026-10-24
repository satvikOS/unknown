/**
 * Exposure and concentration (CAP-PORT-03), historical VaR and ES (CAP-PORT-10) and the VaR backtest
 * with its exceptions and Kupiec test. Sources: GET /api/portfolio/exposure, /var, /var/backtest.
 * VaR, ES and backtest figures are statistical floats in the position currency (loss positive);
 * they never size, reserve or post money, and neither bounds the worst possible loss.
 */
import { useState, type ReactNode } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { FixtureRefused, Money, StatusPill } from '../../ui/primitives';
import { QueryError } from '../../ui/WorkspaceFrame';
import { Segmented } from '../operations/parts';
import { usePluginQuery } from '../plugin-api/hooks';
import { BacktestChart, BarList } from './charts';
import { SNAPSHOT_STORE_QUERY, backtestDays, dec, fmtFloatMoney, fmtNum, fmtPct, isRec, kupiecVerdict, num, ofKind, recs, str, unavailableReason, weightPct, type BacktestDay, type Rec, type ScopeOption } from './model';
import { PortfolioFrame, AsOf, FixtureNote, Lineage, QuoteSnapshotNote, ScopeSelect, Toolbar, Unavailable, type FormulaDef } from './parts';
import { asText, NoValue } from '../../ui/unavailable';

type Group = 'by_instrument' | 'by_owner' | 'by_sector';

function ExposurePanel({ body, formulas }: { body: Rec; formulas: Map<string, FormulaDef> }) {
  const [group, setGroup] = useState<Group>('by_instrument');
  const per = isRec(body.currencies) ? Object.values(body.currencies).filter(isRec) : [];
  const keyOf = group === 'by_instrument' ? 'instrument_id' : group === 'by_owner' ? 'owner' : 'sector';
  const cls = isRec(body.classification) ? body.classification : {};
  return (
    <section className="panel pf-section" aria-labelledby="pf-expo-h">
      <header>
        <h2 id="pf-expo-h">Exposure</h2>
        <AsOf body={body} />
      </header>
      <div className="panel-body">
        <QuoteSnapshotNote body={body} />
        {per.length === 0 ? <p className="muted">No positions in this scope: no exposure.</p> : null}
        {per.map((c) => {
          const ccy = str(c.currency) ?? '';
          const conc = isRec(c.concentration) ? c.concentration : {};
          const topKey = Object.keys(conc).find((k) => /^top_\d+_share$/.test(k));
          const cov = isRec(c.coverage) ? c.coverage : {};
          const den = isRec(c.denominator) ? c.denominator : {};
          const rows = recs(c[group]);
          const cols: Column<Rec>[] = [
            { id: 'k', header: group === 'by_instrument' ? 'Instrument' : group === 'by_owner' ? 'Owner' : 'Sector (SIC division)', cell: (r) => <span className="cmd-id">{str(r[keyOf])}</span>, wrap: true },
            { id: 'n', header: 'Positions', numeric: true, cell: (r) => num(r.positions) ?? <NoValue reason="NOT_REPORTED" /> },
            { id: 'long', header: 'Long', numeric: true, cell: (r) => <Money amount={dec(r.long)} currency={ccy} /> },
            { id: 'short', header: 'Short', numeric: true, cell: (r) => <Money amount={dec(r.short)} currency={ccy} /> },
            { id: 'gross', header: 'Gross', numeric: true, cell: (r) => <Money amount={dec(r.gross)} currency={ccy} />, sortValue: (r) => dec(r.gross), sortKind: 'decimal' },
            { id: 'net', header: 'Net', numeric: true, cell: (r) => <Money amount={dec(r.net)} currency={ccy} /> },
            { id: 'w', header: 'Net of scope NAV', numeric: true, cell: (r) => weightPct(dec(r.weight_of_denominator)) },
            { id: 'g', header: 'Share of gross', numeric: true, cell: (r) => weightPct(dec(r.share_of_gross)) },
          ];
          return (
            <div key={ccy} className="pf-section">
              <div className="pf-kpis">
                <div className="pf-kpi">
                  <div className="k">Gross / net, {ccy}</div>
                  <div className="v">
                    <Money amount={dec(c.gross)} currency={ccy} />
                  </div>
                  <div className="s">
                    net <Money amount={dec(c.net)} currency={ccy} /> · long <Money amount={dec(c.long)} currency={ccy} /> · short <Money amount={dec(c.short)} currency={ccy} />
                  </div>
                </div>
                <div className="pf-kpi">
                  <div className="k">Gross of {str(den.name) ?? 'scope NAV'}</div>
                  <div className="v">{weightPct(dec(c.gross_of_denominator))}</div>
                  <div className="s">{str(den.reason) ?? (dec(den.value) ? <>NAV <Money amount={dec(den.value)} currency={ccy} /></> : null)}</div>
                </div>
                <div className="pf-kpi">
                  <div className="k">Concentration (HHI)</div>
                  <div className="v">{fmtNum(num(conc.hhi), 3)}</div>
                  <div className="s">
                    effective N {fmtNum(num(conc.effective_n), 1)}
                    {topKey ? ` · ${topKey.replace(/_/g, ' ')} ${asText(fmtPct(num(conc[topKey]), 1))}` : ''}
                  </div>
                </div>
                <div className="pf-kpi">
                  <div className="k">Coverage</div>
                  <div className="v">
                    {num(cov.priced_positions) ?? 0} priced · {num(cov.unpriced_positions) ?? 0} unpriced
                  </div>
                  <div className="s">
                    {num(cov.stale_marked_positions) ?? 0} stale-marked · sector-classified {weightPct(dec(cov.sector_classified_share_of_gross))} of gross
                  </div>
                </div>
              </div>
              <div className="pf-toolbar">
                <Segmented
                  label="Group exposure by"
                  options={[
                    { value: 'by_instrument', label: 'Instrument' },
                    { value: 'by_owner', label: 'Owner' },
                    { value: 'by_sector', label: 'Sector' },
                  ]}
                  value={group}
                  onChange={setGroup}
                />
                <span className="pf-hint">{str(conc.basis)}</span>
              </div>
              {rows.length ? <BarList items={rows.map((r) => ({ key: str(r[keyOf]) ?? '?', label: str(r[keyOf]) ?? '?', value: dec(r.gross), currency: ccy, note: `${asText(weightPct(dec(r.share_of_gross)))} of gross` }))} label={`Gross exposure ${group.replace('_', ' ')}, ${ccy}`} /> : null}
              <DataTable caption={`Exposure ${group.replace('_', ' ')}, ${ccy}`} captionHidden columns={cols} rows={rows} rowKey={(r) => str(r[keyOf]) ?? '?'} emptyText="No priced positions." maxHeight="300px" />
              {recs(c.unpriced).length ? (
                <p className="pf-hint">
                  Unpriced (excluded from exposure, never counted as zero): {recs(c.unpriced).map((u) => `${str(u.instrument_id)} (${str(u.owner)})`).join(', ')}
                </p>
              ) : null}
            </div>
          );
        })}
        <p className="pf-hint" style={{ margin: 0 }}>
          Sector scheme: {str(cls.scheme) ?? <NoValue reason="NOT_REPORTED" />}. Unclassified names fall into “{str(cls.unclassified_bucket) ?? 'unknown'}”, never a default sector. No FX conversion is applied between currencies.
        </p>
        <Lineage body={body} formulas={formulas} />
      </div>
    </section>
  );
}

function VarPanel({ body, formulas }: { body: Rec; formulas: Map<string, FormulaDef> }) {
  const by = isRec(body.by_currency) ? Object.entries(body.by_currency).filter((e): e is [string, Rec] => isRec(e[1])) : [];
  const hist = isRec(body.history) ? body.history : {};
  return (
    <section className="panel pf-section" aria-labelledby="pf-var-h">
      <header>
        <h2 id="pf-var-h">Value at risk</h2>
        <span className="pf-hint">{str(body.method)}</span>
      </header>
      <div className="panel-body">
        <QuoteSnapshotNote body={body} />
        <div className="callout pf-gap" role="note" style={{ ['--tone' as string]: 'var(--info)' }}>
          {str(body.statement) ?? 'VaR does not represent the worst-case loss'}. Confidence {str(body.alpha) ?? '?'}, horizon {num(body.horizon_days) ?? '?'} day(s), up to{' '}
          {num(body.lookback_scenarios) ?? '?'} scenarios from {num(hist.valuation_dates_visible) ?? 0} visible valuation date(s). Figures are statistical estimates (floats), loss positive.
        </div>
        {str(hist.unavailable_reason) ? <Unavailable what="Return history" reason={str(hist.unavailable_reason) as string} /> : null}
        {by.map(([ccy, v]) => (
          <div key={ccy} className="pf-section">
            <div className="pf-kpis">
              <div className="pf-kpi">
                <div className="k">VaR, {ccy}</div>
                <div className="v">{fmtFloatMoney(num(v.var), ccy)}</div>
                <div className="s">{num(v.var) === null ? str(v.reason) : `${asText(fmtPct(num(v.var_pct_of_scope_nav), 2))} of scope NAV; scenario ${str(v.var_scenario_date) ?? '?'}`}</div>
              </div>
              <div className="pf-kpi">
                <div className="k">Expected shortfall, {ccy}</div>
                <div className="v">{fmtFloatMoney(num(v.es), ccy)}</div>
                <div className="s">{num(v.es_tail_mean_ibkr_form) !== null ? `IBKR tail-mean form ${asText(fmtFloatMoney(num(v.es_tail_mean_ibkr_form), ccy))}` : 'quantile-integral definition'}</div>
              </div>
              <div className="pf-kpi">
                <div className="k">Worst scenario loss</div>
                <div className="v">{fmtFloatMoney(num(v.worst_loss), ccy)}</div>
                <div className="s">
                  {num(v.scenarios) ?? 0} scenarios{isRec(v.scenario_dates) && str(v.scenario_dates.first) ? `, ${str(v.scenario_dates.first)} to ${str(v.scenario_dates.last)}` : ''}
                </div>
              </div>
              <div className="pf-kpi">
                <div className="k">Coverage</div>
                <div className="v">{v.coverage_complete === true ? 'complete' : v.coverage_complete === false ? 'partial' : <NoValue reason="NOT_REPORTED" />}</div>
                <div className="s">{str(v.partial_note) ?? (recs(v.uncovered).length ? `${recs(v.uncovered).length} position(s) without history` : 'every position covered')}</div>
              </div>
            </div>
            {recs(v.uncovered).length ? (
              <ul className="pf-list pf-hint">
                {recs(v.uncovered).map((u, i) => (
                  <li key={i}>
                    {str(u.instrument_id)}: {str(u.reason)}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ))}
        <Lineage body={body} formulas={formulas} />
      </div>
    </section>
  );
}

/**
 * Whether the valuation snapshots a backtest reads include FIXTURE data. var.backtest reports no fixture
 * flag of its own, so the terminal reads it from GET /api/portfolio/snapshots (one flag per run). Null when
 * the snapshot list could not be read.
 */
export function snapshotsIncludeFixture(snapshots: unknown): boolean | null {
  const b = ofKind(snapshots, 'snapshots');
  if (!b || b.available !== true) return null;
  return recs(b.runs).some((r) => r.fixture === true || r.fixture === 1);
}

function BacktestFixtureNote({ fixtureRuns, hasDays }: { fixtureRuns: boolean | null | undefined; hasDays: boolean }) {
  if (!hasDays || fixtureRuns === false || fixtureRuns === undefined) return null;
  if (fixtureRuns === null) {
    return <p className="pf-hint">The backtest response does not say whether its valuation snapshots used FIXTURE data, and the snapshot list could not be read.</p>;
  }
  return (
    <div className="callout pf-gap" role="alert" style={{ ['--tone' as string]: 'var(--alert)' }}>
      <FixtureRefused label="VaR backtest" detail="Its valuation snapshots include FIXTURE quotes." />{' '}
      <span>
        <strong>VaR backtest not shown.</strong> Its valuation snapshots include FIXTURE (test) quotes, which are not real market data.
      </span>
    </div>
  );
}

export function BacktestPanel({ body, formulas, fixtureRuns }: { body: Rec; formulas: Map<string, FormulaDef>; fixtureRuns?: boolean | null }) {
  // Read from snapshots that include FIXTURE quotes: refused, only the error is shown.
  if (fixtureRuns === true) return <BacktestFixtureNote fixtureRuns hasDays />;
  const ccy = str(body.currency) ?? '';
  const days = backtestDays(body);
  const verdict = kupiecVerdict(body.kupiec);
  const exceptions = days.filter((d) => d.exception);
  const cols: Column<BacktestDay>[] = [
    { id: 'date', header: 'Day', cell: (d) => d.date, sortValue: (d) => d.date },
    { id: 'var', header: 'VaR forecast', numeric: true, cell: (d) => fmtFloatMoney(d.forecast, ccy, 4) },
    { id: 'loss', header: 'Realized loss', numeric: true, cell: (d) => fmtFloatMoney(d.realized, ccy, 4), sortValue: (d) => d.realized, sortKind: 'number' },
    { id: 'x', header: 'Exception', cell: (d) => (d.exception ? <StatusPill label="exception" tone="bad" compact /> : <span className="muted">no</span>) },
  ];
  return (
    <section className="panel pf-section" aria-labelledby="pf-bt-h">
      <header>
        <h2 id="pf-bt-h">VaR backtest</h2>
        <span className="pf-label">
          <StatusPill label={`${exceptions.length} exception(s)`} tone={exceptions.length ? 'warn' : 'neutral'} compact />
          <span className="pf-hint">confidence {str(body.alpha) ?? '?'}, window {num(body.window) ?? '?'}</span>
        </span>
      </header>
      <div className="panel-body">
        <BacktestFixtureNote fixtureRuns={fixtureRuns} hasDays={days.length > 0} />
        {verdict ? (
          <div className="callout pf-gap" role="status" style={{ ['--tone' as string]: `var(--${verdict.tone === 'neutral' ? 'info' : verdict.tone})` }}>
            {verdict.text}
          </div>
        ) : null}
        {days.length ? <BacktestChart days={days} currency={ccy} /> : <p className="muted">No backtest day yet: each forecast needs enough return history at the prior valuation.</p>}
        {exceptions.length ? (
          <p className="pf-hint">
            Exceptions: {exceptions.map((d) => `${d.date} (loss ${asText(fmtFloatMoney(d.realized, ccy))} vs VaR ${asText(fmtFloatMoney(d.forecast, ccy))})`).join('; ')}
          </p>
        ) : null}
        <p className="pf-hint" style={{ margin: 0 }}>
          {str(body.note)}
        </p>
      </div>
      <div className="panel-body flush">
        <DataTable caption="VaR backtest days" captionHidden columns={cols} rows={days} rowKey={(d) => d.date} initialSort={{ columnId: 'date', direction: 'descending' }} emptyText="No days." maxHeight="280px" />
      </div>
      <div className="panel-body">
        {recs(body.skipped).length ? (
          <details className="pf-lineage">
            <summary>{recs(body.skipped).length} day(s) skipped</summary>
            <ul className="pf-list">
              {recs(body.skipped).map((s, i) => (
                <li key={i}>
                  {str(s.date)}: {str(s.reason)}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
        <Lineage body={body} formulas={formulas} />
      </div>
    </section>
  );
}

export function RiskTab({ bar, scope, setScope, options, formulas }: { bar: ReactNode; scope: string; setScope: (s: string) => void; options: readonly ScopeOption[]; formulas: Map<string, FormulaDef> }) {
  const [alpha, setAlpha] = useState<'0.95' | '0.99'>('0.99');
  const [horizon, setHorizon] = useState<'1' | '5' | '10'>('1');
  const expoQ = usePluginQuery<unknown>('/api/portfolio/exposure', { scope });
  const varQ = usePluginQuery<unknown>('/api/portfolio/var', { scope, alpha, horizon });
  const btQ = usePluginQuery<unknown>('/api/portfolio/var/backtest', { scope, alpha }, SNAPSHOT_STORE_QUERY);
  // The backtest carries no fixture flag; the snapshot runs it reads do (route limit: 5000 runs).
  const snapQ = usePluginQuery<unknown>('/api/portfolio/snapshots', { limit: 5000 }, SNAPSHOT_STORE_QUERY);
  const fixtureRuns = snapQ.status === 'error' ? null : snapQ.data === undefined ? undefined : snapshotsIncludeFixture(snapQ.data);
  const expo = ofKind(expoQ.data, 'exposure');
  const v = ofKind(varQ.data, 'var');
  const bt = ofKind(btQ.data, 'var_backtest');
  const btWhy = unavailableReason(bt);
  return (
    <PortfolioFrame tab="risk" bar={bar} queries={[expoQ, varQ, btQ]}>
      <Toolbar>
        <ScopeSelect id="pf-r-scope" value={scope} options={options} onChange={setScope} />
        <div className="field">
          <span className="pf-hint">VaR confidence</span>
          <Segmented
            label="VaR confidence level"
            options={[
              { value: '0.95', label: '95%' },
              { value: '0.99', label: '99%' },
            ]}
            value={alpha}
            onChange={setAlpha}
          />
        </div>
        <div className="field">
          <span className="pf-hint">Horizon (days)</span>
          <Segmented
            label="VaR horizon in days"
            options={[
              { value: '1', label: '1' },
              { value: '5', label: '5' },
              { value: '10', label: '10' },
            ]}
            value={horizon}
            onChange={setHorizon}
          />
        </div>
      </Toolbar>
      <QueryError what="Exposure" error={expoQ.error} />
      <QueryError what="VaR" error={varQ.error} />
      <QueryError what="VaR backtest" error={btQ.error} />
      <FixtureNote data={expoQ.data} what="Exposure" />
      <FixtureNote data={varQ.data} what="VaR" />
      <FixtureNote data={btQ.data} what="VaR backtest" />
      {expo ? <ExposurePanel body={expo} formulas={formulas} /> : expoQ.status === 'loading' ? <p className="muted">Loading exposure…</p> : null}
      {v ? <VarPanel body={v} formulas={formulas} /> : null}
      {btWhy ? <Unavailable what="VaR backtest" reason={btWhy} /> : bt ? <BacktestPanel body={bt} formulas={formulas} fixtureRuns={fixtureRuns} /> : null}
    </PortfolioFrame>
  );
}
