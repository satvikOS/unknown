/**
 * Markets and macro (CAP-MAC-05/07/12): GET /api/macro (catalog of series held, as of a time), the US Treasury
 * daily par yield curve by date (/api/macro/yield-curve) and the ECB euro reference rates (/api/macro/fx), each
 * value with its availability time, publication and observation id. A past "as of" never reveals data that
 * became available later. Missing tenors stay missing (a gap in the chart, never interpolated).
 *
 * Route: `#/macro/as_of=…/date=…/compare=…/fx_date=…/base=…`.
 */
import { useEffect, useState, type FormEvent } from 'react';
import { formatDecimal } from '../../format/decimal';
import { Badge } from '../../ui/primitives';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { useApi } from '../research-common/api';
import { LineChart, type ChartSeries } from '../research-common/charts';
import { POPULATE, checkInstant } from '../research-common/format';
import { Commands, ErrorNotice, Flags, IsoTime, KV, Loading, Section, StatusTag, Unavailable } from '../research-common/parts';
import { AsOfField, TextField } from '../company/CompanyWorkspace';
import type { WorkspaceProps } from '../types';
import { isUnavailable, type UnavailableBody } from '../company/model';
import {
  crossRows,
  curveSeries,
  fxRows,
  isDate,
  macroRoute,
  parseMacroRoute,
  populateFor,
  tenorTicks,
  type CurveResponse,
  type FxResponse,
  type MacroCatalog,
  type MacroOptions,
} from './model';

export function MacroWorkspace({ route, navigate }: WorkspaceProps) {
  const o = parseMacroRoute(route.rest);
  const go = (next: MacroOptions) => navigate('macro', ...macroRoute(next));
  const catalog = useApi<MacroCatalog | UnavailableBody>('/api/macro', { as_of: o.as_of });
  const curve = useApi<CurveResponse | UnavailableBody>('/api/macro/yield-curve', { as_of: o.as_of, date: o.date });
  const compare = useApi<CurveResponse | UnavailableBody>(o.compare ? '/api/macro/yield-curve' : null, { as_of: o.as_of, date: o.compare });
  const fx = useApi<FxResponse | UnavailableBody>('/api/macro/fx', { as_of: o.as_of, date: o.fx_date, base: o.base });
  const queries = [catalog, curve, fx, ...(o.compare ? [compare] : [])];

  return (
    <WorkspaceFrame
      title="Markets & macro"
      description="US Treasury par yield curve and ECB euro reference rates from the point-in-time research store, with availability times."
      queries={queries}
    >
      <AsOfBar o={o} go={go} />
      <ErrorNotice what="Macro catalog" error={catalog.error} />
      {catalog.data && isUnavailable(catalog.data) ? <Unavailable what="Macro series" reason={catalog.data.reason} commands={[POPULATE.treasury, POPULATE.ecb]} /> : null}
      {catalog.data && !isUnavailable(catalog.data) ? <CatalogPanel c={catalog.data} /> : null}
      {!catalog.data && !catalog.error ? <Loading what="the series catalog" /> : null}
      {catalog.data && isUnavailable(catalog.data) ? null : (
        <>
          <CurvePanel o={o} go={go} curve={curve.data} compare={o.compare ? compare.data : undefined} error={curve.error ?? compare.error} />
          <FxPanel o={o} go={go} fx={fx.data} error={fx.error} />
        </>
      )}
    </WorkspaceFrame>
  );
}

function AsOfBar({ o, go }: { o: MacroOptions; go: (n: MacroOptions) => void }) {
  const [asOf, setAsOf] = useState(o.as_of ?? '');
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setAsOf(o.as_of ?? ''), [o.as_of]);
  const apply = (e: FormEvent) => {
    e.preventDefault();
    const a = checkInstant(asOf);
    if (!a.ok) return setErr(a.error);
    setErr(null);
    go({ ...o, as_of: a.value });
  };
  return (
    <form className="rx-toolbar" onSubmit={apply} aria-label="Point in time">
      <AsOfField id="macro-asof" value={asOf} onChange={setAsOf} label="Knowledge as of" />
      <button type="submit" className="btn primary">
        Apply
      </button>
      {o.as_of ? (
        <button type="button" className="btn" onClick={() => go({ ...o, as_of: '' })}>
          Now
        </button>
      ) : null}
      <span className="rx-hint">Only observations available at this time are shown: {o.as_of ? <span className="mono">{o.as_of}</span> : 'now'}.</span>
      {err ? (
        <span className="form-error" role="alert">
          {err}
        </span>
      ) : null}
    </form>
  );
}

function CatalogPanel({ c }: { c: MacroCatalog }) {
  return (
    <Section title="Series held" note={<>Not available: {c.not_available.join('; ')}.</>}>
      <div className="rx-table-wrap">
        <table className="rx-table">
          <caption>
            Macro datasets in the research store, as known at <IsoTime iso={c.as_of} />
          </caption>
          <thead>
            <tr>
              <th scope="col">Series</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">
                Observations
              </th>
              <th scope="col">Dates</th>
              <th scope="col">Newest availability</th>
            </tr>
          </thead>
          <tbody>
            {c.series.map((s) => (
              <tr key={s.dataset}>
                <th scope="row">
                  {s.title} <span className="muted mono">{s.dataset}</span>
                </th>
                <td className="wrap">
                  <StatusTag status={s.status} /> {s.reason ? <span className="rx-reason">{s.reason}</span> : null}
                  {s.status !== 'OK' ? <Commands commands={populateFor(s.dataset)} /> : null}
                </td>
                <td className="num">{s.observations}</td>
                <td className="mono">{s.first_date ? `${s.first_date} → ${s.last_date}` : '—'}</td>
                <td>
                  <IsoTime iso={s.newest_available_time} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function CurvePanel({
  o,
  go,
  curve,
  compare,
  error,
}: {
  o: MacroOptions;
  go: (n: MacroOptions) => void;
  curve: CurveResponse | UnavailableBody | undefined;
  compare: CurveResponse | UnavailableBody | undefined;
  error: unknown;
}) {
  const [date, setDate] = useState(o.date ?? '');
  const [cmp, setCmp] = useState(o.compare ?? '');
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setDate(o.date ?? '');
    setCmp(o.compare ?? '');
  }, [o.date, o.compare]);
  const apply = (e: FormEvent) => {
    e.preventDefault();
    for (const [n, v] of [
      ['curve date', date],
      ['compare date', cmp],
    ] as const) {
      if (v.trim() && !isDate(v.trim())) return setErr(`${n} must be a date (YYYY-MM-DD)`);
    }
    setErr(null);
    go({ ...o, date: date.trim(), compare: cmp.trim() });
  };
  const c = curve && !isUnavailable(curve) && curve.status !== 'UNAVAILABLE' ? curve : null;
  const k = compare && !isUnavailable(compare) && compare.status !== 'UNAVAILABLE' ? compare : null;
  const series: ChartSeries[] = [];
  if (c) series.push({ id: 'main', label: `${c.curve_date} (${c.status})`, points: curveSeries(c.points, c.unit), markers: true });
  if (k) series.push({ id: 'compare', label: `${k.curve_date} (${k.status})`, points: curveSeries(k.points, k.unit), tone: 'warn', dashed: true, markers: true });
  return (
    <Section title="US Treasury par yield curve" note={c ? <>{c.attribution}. {c.method_note}</> : undefined}>
      <form className="rx-toolbar" onSubmit={apply} aria-label="Curve dates">
        <TextField id="curve-date" label="Curve date (empty = latest)" value={date} onChange={setDate} placeholder="2026-10-21" width={180} />
        <TextField id="curve-compare" label="Compare with date" value={cmp} onChange={setCmp} placeholder="optional" width={180} />
        <button type="submit" className="btn">
          Show
        </button>
        {err ? (
          <span className="form-error" role="alert">
            {err}
          </span>
        ) : null}
      </form>
      <ErrorNotice what="Yield curve" error={error} />
      {curve && isUnavailable(curve) ? <Unavailable what="Yield curve" reason={curve.reason} commands={[POPULATE.treasury]} /> : null}
      {curve && !isUnavailable(curve) && curve.status === 'UNAVAILABLE' ? <Unavailable what="Yield curve" reason={curve.reason} commands={[POPULATE.treasury]} /> : null}
      {compare && !isUnavailable(compare) && compare.status === 'UNAVAILABLE' ? <Unavailable what={`Comparison curve ${o.compare}`} reason={compare.reason} commands={[POPULATE.treasury]} /> : null}
      {!curve && !error ? <Loading what="the curve" /> : null}
      {c ? (
        <>
          <div className="rx-identity">
            <span className="rx-name">{c.curve}</span>
            <span className="mono">curve date {c.curve_date}</span>
            <StatusTag status={c.status} detail={c.missing_tenors?.length ? `missing ${c.missing_tenors.join(', ')}` : undefined} />
            {c.slope_10y_2y ? (
              <span title={`${c.slope_10y_2y.formula}; inputs obs ${c.slope_10y_2y.inputs.join(', ')}`}>
                10Y − 2Y slope <strong className="num">{formatDecimal(c.slope_10y_2y.value_bp)}</strong> {c.slope_10y_2y.unit} <Badge>{c.slope_10y_2y.label}</Badge>
              </span>
            ) : (
              <span className="muted">10Y − 2Y slope not computed (a tenor is missing)</span>
            )}
          </div>
          {c.fallback ? (
            <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 8 }}>
              {c.fallback}
            </div>
          ) : null}
          {k?.fallback ? (
            <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 8 }}>
              Comparison: {k.fallback}
            </div>
          ) : null}
          {c.missing_tenors?.length ? (
            <p className="rx-hint">
              Missing tenors on {c.curve_date}: <span className="mono">{c.missing_tenors.join(', ')}</span> (left as gaps; never interpolated).
            </p>
          ) : null}
          <LineChart
            title={`Treasury par yield curve on ${c.curve_date}${k ? ` and ${k.curve_date}` : ''}, in ${c.unit ?? 'percent'}`}
            summary={`${c.points?.length ?? 0} tenors on ${c.curve_date}${c.points?.length ? `, from ${c.points[0]!.tenor} ${c.points[0]!.value_pct} to ${c.points[c.points.length - 1]!.tenor} ${c.points[c.points.length - 1]!.value_pct} ${c.unit ?? 'percent'}` : ''}. Tenors are evenly spaced, not to scale.`}
            series={series}
            xTicks={tenorTicks(c.points, k?.points)}
            yFormat={(v) => v.toFixed(2)}
            yUnit={c.unit ?? 'percent'}
          />
          <CurveTable c={c} k={k} />
        </>
      ) : null}
    </Section>
  );
}

/** Par yields of the curve date, with the comparison date's value for every tenor (also where the main curve has none). */
export function CurveTable({ c, k }: { c: CurveResponse; k: CurveResponse | null }) {
  const other = new Map((k?.points ?? []).map((p) => [p.tenor, p]));
  return (
    <div className="rx-table-wrap rx-gap">
      <table className="rx-table">
        <caption>
          Par yields on {c.curve_date} in {c.unit}, with availability and lineage{k ? `; comparison ${k.curve_date}` : ''}
        </caption>
        <thead>
          <tr>
            <th scope="col">Tenor</th>
            <th scope="col" className="num">
              Years
            </th>
            <th scope="col" className="num">
              Par yield ({c.unit})
            </th>
            {k ? (
              <th scope="col" className="num">
                {k.curve_date} ({k.unit})
              </th>
            ) : null}
            <th scope="col">Available at</th>
            <th scope="col">Publication · observation</th>
            <th scope="col">Flags</th>
          </tr>
        </thead>
        <tbody>
          {(c.points ?? []).map((p) => (
            <tr key={p.tenor}>
              <th scope="row" className="mono">
                {p.tenor}
              </th>
              <td className="num">{p.years}</td>
              <td className="num">{p.value_pct}</td>
              {k ? <td className="num">{other.get(p.tenor)?.value_pct ?? <span className="muted">missing</span>}</td> : null}
              <td>
                <IsoTime iso={p.available_time} />
              </td>
              <td className="mono">
                {p.publication_id} · obs {p.obs_id}
              </td>
              <td>
                <Flags flags={p.flags} />
              </td>
            </tr>
          ))}
          {(c.missing_tenors ?? []).map((t) => {
            const kp = other.get(t);
            return (
              <tr key={`missing-${t}`}>
                <th scope="row" className="mono">
                  {t}
                </th>
                <td className="num">{kp?.years ?? ''}</td>
                <td className="muted">missing</td>
                {k ? <td className="num">{kp?.value_pct ?? <span className="muted">missing</span>}</td> : null}
                <td colSpan={3} className="muted">
                  missing on {c.curve_date} (not published or not available at as of); not interpolated
                  {kp ? ` · ${k!.curve_date}: obs ${kp.obs_id}` : ''}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FxPanel({ o, go, fx, error }: { o: MacroOptions; go: (n: MacroOptions) => void; fx: FxResponse | UnavailableBody | undefined; error: unknown }) {
  const [date, setDate] = useState(o.fx_date ?? '');
  const [base, setBase] = useState(o.base ?? 'EUR');
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setDate(o.fx_date ?? '');
    setBase(o.base ?? 'EUR');
  }, [o.fx_date, o.base]);
  const apply = (e: FormEvent) => {
    e.preventDefault();
    if (date.trim() && !isDate(date.trim())) return setErr('reference date must be a date (YYYY-MM-DD)');
    if (!/^[A-Za-z]{3}$/.test(base.trim())) return setErr('base must be a 3-letter currency code');
    setErr(null);
    const b = base.trim().toUpperCase();
    go({ ...o, fx_date: date.trim(), base: b === 'EUR' ? '' : b });
  };
  const f = fx && !isUnavailable(fx) && fx.status !== 'UNAVAILABLE' ? fx : null;
  const crosses = f ? crossRows(f) : [];
  return (
    <Section title="ECB euro reference rates" note={f ? <>{f.attribution}</> : undefined}>
      <form className="rx-toolbar" onSubmit={apply} aria-label="Reference rate options">
        <TextField id="fx-date" label="Reference date (empty = latest)" value={date} onChange={setDate} placeholder="2026-10-21" width={200} />
        <TextField id="fx-base" label="Express in base" value={base} onChange={setBase} placeholder="EUR" width={110} />
        <button type="submit" className="btn">
          Show
        </button>
        {err ? (
          <span className="form-error" role="alert">
            {err}
          </span>
        ) : null}
      </form>
      <ErrorNotice what="Reference rates" error={error} />
      {fx && isUnavailable(fx) ? <Unavailable what="Reference rates" reason={fx.reason} commands={[POPULATE.ecb]} /> : null}
      {fx && !isUnavailable(fx) && fx.status === 'UNAVAILABLE' ? <Unavailable what="Reference rates" reason={fx.reason} commands={[POPULATE.ecb]} /> : null}
      {!fx && !error ? <Loading what="reference rates" /> : null}
      {f ? (
        <>
          <div className="callout" role="note" style={{ ['--tone' as string]: 'var(--info)', marginBottom: 8 }}>
            {f.use_note}
          </div>
          <KV
            rows={[
              ['As of', <IsoTime iso={f.as_of} />],
              ['Requested date', f.requested_date ?? 'latest per currency'],
              f.stale.length ? ['Stale', <span className="mono">{f.stale.join(', ')}</span>] : null,
              f.unavailable.length ? ['Unavailable', <span className="mono">{f.unavailable.join(', ')}</span>] : null,
            ]}
          />
          <div className="rx-table-wrap rx-gap">
            <table className="rx-table">
              <caption>Reference rates as published (units of the currency per 1 EUR)</caption>
              <thead>
                <tr>
                  <th scope="col">Currency</th>
                  <th scope="col" className="num">
                    Rate
                  </th>
                  <th scope="col">Meaning</th>
                  <th scope="col">Reference date</th>
                  <th scope="col">Available at</th>
                  <th scope="col">Observation</th>
                  <th scope="col">Flags</th>
                </tr>
              </thead>
              <tbody>
                {fxRows(f).map((r) => (
                  <tr key={r.currency}>
                    <th scope="row" className="mono">
                      {r.currency}
                    </th>
                    <td className="num">{r.rate}</td>
                    <td>{r.orientation}</td>
                    <td className="mono">{r.reference_date}</td>
                    <td>
                      <IsoTime iso={r.available_time} />
                    </td>
                    <td className="mono">obs {r.obs_id}</td>
                    <td>
                      <Flags flags={r.flags} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {f.normalized ? (
            f.normalized.rates ? (
              <div className="rx-table-wrap rx-gap">
                <table className="rx-table">
                  <caption>Derived cross rates in {f.normalized.base} (modified from ECB data)</caption>
                  <thead>
                    <tr>
                      <th scope="col">Currency</th>
                      <th scope="col" className="num">
                        Value
                      </th>
                      <th scope="col">Meaning</th>
                      <th scope="col">Formula · rounding</th>
                      <th scope="col">Reference dates</th>
                      <th scope="col">Flags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {crosses.map((x) => (
                      <tr key={x.currency}>
                        <th scope="row" className="mono">
                          {x.currency}
                        </th>
                        <td className="num">{x.value}</td>
                        <td>{x.meaning}</td>
                        <td className="mono wrap">
                          {x.formula} · {x.rounding} · inputs obs {x.inputs.join(', ')}
                        </td>
                        <td className="mono">{x.reference_dates.join(', ')}</td>
                        <td>
                          <Flags flags={x.flags} />
                          <span className="muted"> {x.label}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Unavailable what={`Cross rates in ${f.normalized.base}`} reason={f.normalized.reason} status={f.normalized.status ?? 'UNAVAILABLE'} />
            )
          ) : null}
        </>
      ) : null}
    </Section>
  );
}
