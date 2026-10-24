/**
 * Market risk from real data only (CAP-PORT-03, CAP-PORT-05, CAP-PORT-11): GET /api/risk/analytics.
 *
 * Historical volatility, beta and correlation versus a benchmark ETF are computed on the server from stored daily
 * bars (the research bar store that the IBKR history adapter and other bar sources write); every figure carries its
 * formula id and the as-of of its inputs, and without bars it is an em dash with the reason. Concentration by
 * issuer and owner bucket, sector and currency come from the same holdings; scenario P&L is exact on real positions
 * and never assumes a beta of 1. Statistics are display floats; amounts are the API's exact decimal strings.
 */
import { useState, type FormEvent, type ReactNode } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { ExportButton } from '../../ui/ExportButton';
import { OWNER, nsToIso, type ExportSource, type ExportTable } from '../../ui/export';
import { Money } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { QueryError } from '../../ui/WorkspaceFrame';
import { usePluginQuery } from '../plugin-api/hooks';
import { dec, fmtNum, fmtPct, isRec, num, recs, str, weightPct, type Rec, type ScopeOption } from './model';
import { AsOf, FixtureNote, Lineage, PortfolioFrame, ScopeSelect, Toolbar, type FormulaDef } from './parts';

interface Props {
  bar: ReactNode;
  scope: string;
  setScope: (s: string) => void;
  options: readonly ScopeOption[];
  formulas: Map<string, FormulaDef>;
}

const LOOKBACKS = [63, 126, 252, 504] as const;

/** One published figure: the value, or the em dash with the server's reason; the formula and inputs' as-of on hover. */
export function Figure({ f, as = 'num' }: { f: unknown; as?: 'num' | 'pct' }) {
  if (!isRec(f)) return <NoValue reason="NOT_REPORTED" />;
  const v = num(f.value);
  const asOf = isRec(f.inputs_as_of) ? Object.entries(f.inputs_as_of).map(([k, d]) => `${k} ${String(d ?? 'none')}`).join(', ') : null;
  const title = [str(f.formula), f.n !== undefined ? `n = ${String(f.n)}` : null, asOf ? `inputs as of ${asOf}` : null].filter(Boolean).join(' · ');
  if (v === null) return <NoValue reason={str(f.reason) ?? 'NO_DATA'} detail={str(f.detail) ?? title} />;
  return <span title={title}>{as === 'pct' ? fmtPct(v, 2) : fmtNum(v, 3)}</span>;
}

function barsSource(r: Rec): ExportSource {
  const p = isRec(r.bars) ? str(r.bars.provider) : null;
  return p ? { kind: 'licensed', source: `marketdata:${p.toUpperCase()}` } : OWNER;
}

function instrumentsExport(body: Rec, rows: readonly Rec[]): ExportTable<Rec> {
  const fig = (k: string) => (r: Rec) => (isRec(r[k]) ? num((r[k] as Rec).value) : null);
  const why = (k: string) => (r: Rec) => (isRec(r[k]) ? str((r[k] as Rec).reason) : null);
  return {
    title: 'Market risk by instrument',
    rows,
    columns: [
      { id: 'inst', header: 'Instrument', value: (r) => str(r.instrument_id) },
      { id: 'ccy', header: 'Currency', value: (r) => str(r.currency) },
      { id: 'qty', header: 'Quantity', kind: 'decimal', value: (r) => dec(r.quantity) },
      { id: 'vol', header: 'Volatility (annual)', kind: 'number', value: fig('hist_vol'), reason: why('hist_vol'), source: barsSource },
      { id: 'beta', header: 'Beta', kind: 'number', value: fig('beta'), reason: why('beta'), source: barsSource },
      { id: 'corr', header: 'Correlation', kind: 'number', value: fig('correlation'), reason: why('correlation'), source: barsSource },
      { id: 'last', header: 'Last bar date', value: (r) => (isRec(r.bars) ? str(r.bars.last_date) : null), reason: (r) => (isRec(r.bars) ? str(r.bars.reason) : null) },
    ],
    provenance: {
      source: 'Market risk: GET /api/risk/analytics (stored daily bars; core positions)',
      asOf: nsToIso(body.as_of_ns),
      filters: isRec(body.parameters) ? Object.fromEntries(Object.entries(body.parameters).map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : String(v)])) : {},
      notes: ['statistics are IEEE doubles; formulas mrisk.hist_vol@1, mrisk.beta@1, mrisk.correlation@1'],
    },
  };
}

export function MarketRiskTab({ bar, scope, setScope, options, formulas }: Props) {
  const [bench, setBench] = useState('SPY.ARCX');
  const [draft, setDraft] = useState('SPY.ARCX');
  const [lookback, setLookback] = useState<number>(252);
  const q = usePluginQuery<unknown>('/api/risk/analytics', { scope, benchmark: bench, lookback, min_obs: Math.min(60, lookback) });
  const body = isRec(q.data) && q.data.kind === 'risk_analytics' ? q.data : null;
  const mr = body && isRec(body.market_risk) ? body.market_risk : null;
  const expo = body && isRec(body.exposure) ? body.exposure : null;
  const apply = (e: FormEvent) => {
    e.preventDefault();
    setBench(draft.trim().toUpperCase() || 'SPY.ARCX');
  };
  return (
    <PortfolioFrame tab="market" bar={bar} queries={[q]}>
      <form onSubmit={apply}>
        <Toolbar>
          <ScopeSelect id="pf-mr-scope" value={scope} options={options} onChange={setScope} />
          <div className="field">
            <label htmlFor="pf-mr-bench">Benchmark ETF</label>
            <input id="pf-mr-bench" className="input" value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} size={12} />
          </div>
          <div className="field">
            <label htmlFor="pf-mr-lb">Lookback (sessions)</label>
            <select id="pf-mr-lb" className="input" value={lookback} onChange={(e) => setLookback(Number(e.target.value))}>
              {LOOKBACKS.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </div>
          <button type="submit" className="btn">
            Apply
          </button>
        </Toolbar>
      </form>
      <QueryError what="Market risk" error={q.error} />
      {mr ? <MarketRiskBody mr={mr} expo={expo} formulas={formulas} /> : q.status === 'loading' ? <p className="muted">Loading market risk…</p> : null}
    </PortfolioFrame>
  );
}

export function MarketRiskBody({ mr, expo, formulas }: { mr: Rec; expo: Rec | null; formulas: Map<string, FormulaDef> }) {
  const bench = isRec(mr.benchmark) ? mr.benchmark : {};
  const rows = recs(mr.instruments);
  const portfolio = isRec(mr.portfolio) ? Object.values(mr.portfolio).filter(isRec) : [];
  const cols: Column<Rec>[] = [
    { id: 'inst', header: 'Instrument', cell: (r) => <span className="cmd-id">{str(r.symbol) ?? str(r.instrument_id)}</span>, sortValue: (r) => str(r.instrument_id) ?? '' },
    { id: 'mv', header: 'Market value', numeric: true, cell: (r) => <Money amount={dec(r.market_value)} currency={str(r.currency) ?? ''} reason={str(r.market_value_reason) ?? 'NO_DATA'} />, sortValue: (r) => dec(r.market_value), sortKind: 'decimal' },
    { id: 'vol', header: 'Volatility', description: formulas.get('mrisk.hist_vol@1')?.definition, numeric: true, cell: (r) => <Figure f={r.hist_vol} as="pct" />, sortValue: (r) => (isRec(r.hist_vol) ? num(r.hist_vol.value) : null), sortKind: 'number' },
    { id: 'beta', header: 'Beta', description: formulas.get('mrisk.beta@1')?.definition, numeric: true, cell: (r) => <Figure f={r.beta} />, sortValue: (r) => (isRec(r.beta) ? num(r.beta.value) : null), sortKind: 'number' },
    { id: 'corr', header: 'Correlation', description: formulas.get('mrisk.correlation@1')?.definition, numeric: true, cell: (r) => <Figure f={r.correlation} /> },
    { id: 'n', header: 'Returns', numeric: true, cell: (r) => (isRec(r.beta) && num(r.beta.n) !== null ? String(r.beta.n) : <NoValue reason="NO_DATA" />) },
    {
      id: 'bars',
      header: 'Bars',
      wrap: true,
      cell: (r) => {
        const b = isRec(r.bars) ? r.bars : {};
        return str(b.provider) ? (
          <span className="pf-hint">
            {str(b.provider)} · {String(b.closes)} closes to {str(b.last_date)}
            {str(b.note) ? ` · ${str(b.note)}` : ''}
          </span>
        ) : (
          <NoValue reason={str(b.reason) ?? 'NO_DATA'} detail={str(b.detail)} />
        );
      },
    },
  ];
  return (
    <>
      <FixtureNote data={mr} what="Market risk" />
      <section className="panel pf-section" aria-labelledby="pf-mr-bench-h">
        <header>
          <h2 id="pf-mr-bench-h">Benchmark {str(bench.key)}</h2>
          <AsOf body={mr} />
        </header>
        <div className="panel-body pf-kpis">
          <div className="pf-kpi">
            <div className="k">Benchmark volatility (annual)</div>
            <div className="v">
              <Figure f={mr.benchmark_vol} as="pct" />
            </div>
            <div className="s">
              {str(bench.provider) ? `${str(bench.provider)} daily closes ${str(bench.first_date)} to ${str(bench.last_date)} (${String(bench.closes)})` : <NoValue reason={str(bench.reason) ?? 'NO_DATA'} detail={str(bench.detail)} />}
            </div>
          </div>
          {portfolio.map((p) => (
            <div className="pf-kpi" key={str(p.currency) ?? ''}>
              <div className="k">Portfolio, {str(p.currency)}</div>
              <div className="v">
                beta <Figure f={p.portfolio_beta} /> · vol <Figure f={p.portfolio_vol} as="pct" />
              </div>
              <div className="s">
                priced value <Money amount={dec(p.priced_market_value)} currency={str(p.currency) ?? ''} />
                {isRec(p.portfolio_beta) && str(p.portfolio_beta.coverage) ? <> · beta coverage {weightPct(str(p.portfolio_beta.coverage))}</> : null}
              </div>
            </div>
          ))}
        </div>
      </section>
      <section className="panel pf-section" aria-labelledby="pf-mr-inst-h">
        <header>
          <h2 id="pf-mr-inst-h">By instrument</h2>
          <ExportButton disabled={!rows.length} table={() => instrumentsExport(mr, rows)} />
        </header>
        <div className="panel-body flush">
          <DataTable caption="Market risk by instrument" captionHidden columns={cols} rows={rows} rowKey={(r) => `${str(r.instrument_id)}|${str(r.currency)}`} emptyText="No positions in this scope." />
        </div>
      </section>
      <Concentration mr={mr} expo={expo} />
      <Scenarios mr={mr} />
      <Correlations mr={mr} />
      <Lineage body={mr} formulas={formulas} />
    </>
  );
}

function GroupTable({ rows, keyName, label, ccy }: { rows: Rec[]; keyName: string; label: string; ccy: string }) {
  const cols: Column<Rec>[] = [
    { id: 'k', header: label, wrap: true, cell: (r) => str(r[keyName]) },
    { id: 'gross', header: 'Gross', numeric: true, cell: (r) => <Money amount={dec(r.gross)} currency={ccy} /> },
    { id: 'share', header: 'Share of gross', numeric: true, cell: (r) => weightPct(dec(r.share_of_gross)) },
    { id: 'w', header: 'Net of scope NAV', numeric: true, cell: (r) => weightPct(dec(r.weight_of_denominator)) },
  ];
  return <DataTable caption={`${label}, ${ccy}`} captionHidden columns={cols} rows={rows} rowKey={(r) => str(r[keyName]) ?? ''} emptyText="No priced positions." maxHeight="240px" />;
}

function Concentration({ mr, expo }: { mr: Rec; expo: Rec | null }) {
  const conc = isRec(mr.concentration) ? Object.values(mr.concentration).filter(isRec) : [];
  const expoCcy = expo && isRec(expo.currencies) ? expo.currencies : {};
  return (
    <section className="panel pf-section" aria-labelledby="pf-mr-conc-h">
      <header>
        <h2 id="pf-mr-conc-h">Concentration</h2>
        <span className="pf-hint">issuer (SEC CIK), owner bucket, sector (SEC SIC division) and currency; no FX conversion</span>
      </header>
      <div className="panel-body">
        {conc.length === 0 ? <p className="muted">No priced positions: no concentration.</p> : null}
        {conc.map((c) => {
          const ccy = str(c.currency) ?? '';
          const e = isRec(expoCcy[ccy]) ? (expoCcy[ccy] as Rec) : {};
          const hhi = isRec(e.concentration) ? num(e.concentration.hhi) : null;
          return (
            <div key={ccy} className="pf-section">
              <p className="pf-hint">
                {ccy}: gross <Money amount={dec(c.gross)} currency={ccy} /> · HHI {hhi === null ? <NoValue reason="NO_DATA" /> : fmtNum(hhi, 4)}
              </p>
              <div className="pf-split">
                <GroupTable rows={recs(c.by_issuer)} keyName="issuer" label="Issuer" ccy={ccy} />
                <GroupTable rows={recs(c.by_owner_bucket)} keyName="owner_bucket" label="Owner bucket" ccy={ccy} />
                <GroupTable rows={recs(e.by_sector)} keyName="sector" label="Sector" ccy={ccy} />
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Scenarios({ mr }: { mr: Rec }) {
  const rows = recs(mr.scenarios);
  const pnl = (r: Rec, k: 'uniform' | 'beta') => {
    const block = isRec(r[k]) ? (r[k] as Rec) : {};
    const by = isRec(block.pnl_by_currency) ? Object.entries(block.pnl_by_currency) : [];
    const left = recs(block.not_revalued).length;
    return (
      <span className="pf-label">
        {by.length ? by.map(([c, v]) => <Money key={c} amount={dec(v)} currency={c} signed />) : <NoValue reason="NO_DATA" detail="no position could be revalued" />}
        {left ? <span className="pf-hint">{left} not revalued</span> : null}
      </span>
    );
  };
  const cols: Column<Rec>[] = [
    { id: 's', header: 'Shock', numeric: true, cell: (r) => str(r.shock) },
    { id: 'u', header: 'Uniform move', description: 'every priced position moves by the shock', numeric: true, cell: (r) => pnl(r, 'uniform') },
    { id: 'b', header: 'Benchmark move × beta', description: 'each position moves beta × shock; positions without a beta are left out', numeric: true, cell: (r) => pnl(r, 'beta') },
  ];
  return (
    <section className="panel pf-section" aria-labelledby="pf-mr-sc-h">
      <header>
        <h2 id="pf-mr-sc-h">Scenario P&amp;L (hypothetical)</h2>
        <span className="pf-hint">exact on current positions and marks; formula {str(rows[0]?.formula) ?? 'mrisk.scenario@1'}</span>
      </header>
      <div className="panel-body flush">
        <DataTable caption="Scenario P&L" captionHidden columns={cols} rows={rows} rowKey={(r) => str(r.shock) ?? ''} emptyText="No scenarios." />
      </div>
    </section>
  );
}

function Correlations({ mr }: { mr: Rec }) {
  const m = isRec(mr.correlation_matrix) ? mr.correlation_matrix : {};
  const pairs = recs(m.pairs);
  if (!pairs.length) return null;
  const cols: Column<Rec>[] = [
    { id: 'a', header: 'Instrument A', cell: (r) => <span className="cmd-id">{str(r.a)}</span> },
    { id: 'b', header: 'Instrument B', cell: (r) => <span className="cmd-id">{str(r.b)}</span> },
    { id: 'rho', header: 'Correlation', numeric: true, cell: (r) => <Figure f={r} />, sortValue: (r) => num(r.value), sortKind: 'number' },
  ];
  return (
    <section className="panel pf-section" aria-labelledby="pf-mr-corr-h">
      <header>
        <h2 id="pf-mr-corr-h">Pairwise correlation</h2>
        <span className="pf-hint">{str(m.note)}</span>
      </header>
      <div className="panel-body flush">
        <DataTable caption="Pairwise correlation" captionHidden columns={cols} rows={pairs} rowKey={(r) => `${str(r.a)}|${str(r.b)}`} maxHeight="300px" />
      </div>
    </section>
  );
}
