/**
 * Cross-country macro comparison and series chart (CAP-MAC-05/06/09) for US, IN and the EU venue countries. Each cell
 * shows the exact value, its period, source column and availability time; a column a source does not cover, a
 * dataset not ingested and a value not yet available at the as-of time each say so in words.
 */
import { useState } from 'react';
import { Badge } from '../../ui/primitives';
import { useApi } from '../research-common/api';
import { LineChart } from '../research-common/charts';
import { Commands, ErrorNotice, IsoTime, Loading, Section, StatusTag, Unavailable } from '../research-common/parts';
import { isUnavailable, type UnavailableBody } from '../company/model';
import { cellText, chartSeries, parseCountries, periodLabel, yearTicks, type GlobalCell, type GlobalComparison, type GlobalSeries } from './global';
import type { MacroOptions } from './model';
import './global.css';

export function GlobalMacroPanels({ o, go }: { o: MacroOptions; go: (n: MacroOptions) => void }) {
  const cmp = useApi<GlobalComparison | (UnavailableBody & { commands?: string[] })>('/api/macro/global', { as_of: o.as_of });
  if (cmp.error) return <ErrorNotice what="Cross-country comparison" error={cmp.error} />;
  if (!cmp.data) return <Loading what="the cross-country comparison" />;
  if (isUnavailable(cmp.data)) return <Unavailable what="Global macro" reason={cmp.data.reason} commands={(cmp.data as { commands?: string[] }).commands} />;
  const c = cmp.data;
  return (
    <>
      <ComparisonTable c={c} />
      <SeriesPanel c={c} o={o} go={go} />
      <SourcesPanel c={c} />
    </>
  );
}

function CellView({ cell }: { cell: GlobalCell | undefined }) {
  if (!cell || cell.status !== 'OK') {
    return (
      <span className="gm-missing" title={cell?.reason ?? 'not ingested'}>
        {cellText(cell)}
      </span>
    );
  }
  const tip = [
    `${cell.dataset} · ${periodLabel(cell)}`,
    `available ${cell.available_time}`,
    `obs ${cell.obs_id}`,
    ...(cell.note ? [cell.note] : []),
    ...(cell.flags ?? []),
  ].join('\n');
  return (
    <span title={tip}>
      <span className="gm-value">{cell.value}</span>
      <span className="gm-period">{periodLabel(cell)}</span>
      {cell.note ? <span className="gm-area">{cell.area}</span> : null}
    </span>
  );
}

export function ComparisonTable({ c }: { c: GlobalComparison }) {
  return (
    <Section title="Cross-country comparison" note={<>Knowledge as of <IsoTime iso={c.as_of} />. {c.note}</>} flush>
      <div className="rx-table-wrap">
        <table className="rx-table gm-table">
          <caption className="sr-only">Latest value of each series per country available at the as-of time</caption>
          <thead>
            <tr>
              <th scope="col">Country</th>
              {c.columns.map((col) => (
                <th key={col.column_id} scope="col" title={`${col.definition}\n${col.attribution}`}>
                  {col.label}
                  <span className="gm-unit">{col.unit}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {c.rows.map((r) => (
              <tr key={r.country.code}>
                <th scope="row">
                  {r.country.name} <span className="mono">{r.country.code}</span> {r.country.euro_area ? <Badge>euro area</Badge> : null}
                </th>
                {c.columns.map((col) => (
                  <td key={col.column_id}>
                    <CellView cell={r.cells[col.column_id]} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="rx-hint">
        Hover a value for its dataset, period, availability time and observation id. Euro-area countries show the ECB policy rate (BIS area XM). NOT_COVERED: the
        source does not publish the series for that country; NOT_INGESTED: run the dataset's ingest command below; NOT_AVAILABLE_AT_AS_OF: nothing published by then.
      </p>
    </Section>
  );
}

function SeriesPanel({ c, o, go }: { c: GlobalComparison; o: MacroOptions; go: (n: MacroOptions) => void }) {
  const known = c.rows.map((r) => r.country.code);
  const column = o.col && c.columns.some((x) => x.column_id === o.col) ? o.col : (c.columns.find((x) => x.column_id === 'bis_policy_rate') ?? c.columns[0])?.column_id;
  const chosen = parseCountries(o.countries, known);
  const countries = chosen.length ? chosen : known.slice(0, 4);
  const [draft, setDraft] = useState<string[] | null>(null);
  const sel = draft ?? countries;
  const q = useApi<GlobalSeries>(column ? '/api/macro/global/series' : null, { as_of: o.as_of, column, countries: countries.join(',') });
  const series = q.data ? chartSeries(q.data) : [];
  const col = c.columns.find((x) => x.column_id === column);
  return (
    <Section title="Series" note={col ? `${col.definition}. ${col.attribution}` : undefined}>
      <form
        className="rx-toolbar"
        aria-label="Series selection"
        onSubmit={(e) => {
          e.preventDefault();
          setDraft(null);
          go({ ...o, countries: sel.join(',') });
        }}
      >
        <label className="field">
          <span>Series</span>
          <select className="input" value={column} onChange={(e) => go({ ...o, col: e.target.value })}>
            {c.columns.map((x) => (
              <option key={x.column_id} value={x.column_id}>
                {x.label}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="gm-countries">
          <legend>Countries</legend>
          {known.map((k) => (
            <label key={k}>
              <input type="checkbox" checked={sel.includes(k)} onChange={(e) => setDraft(e.target.checked ? [...sel, k] : sel.filter((x) => x !== k))} /> {k}
            </label>
          ))}
        </fieldset>
        <button type="submit" className="btn">
          Show
        </button>
      </form>
      <ErrorNotice what="Series" error={q.error} />
      {!q.data && !q.error ? <Loading what="the series" /> : null}
      {q.data ? (
        <>
          {series.length ? (
            <LineChart
              title={`${q.data.column.label} by country`}
              summary={`${q.data.column.label} for ${series.map((s) => s.label).join(', ')}, as of ${q.data.as_of}. Missing periods are gaps, never zero.`}
              series={series}
              xTicks={yearTicks(series)}
              yFormat={(v) => v.toFixed(2)}
              yUnit={q.data.column.unit}
              zeroLine
            />
          ) : null}
          <ul className="ops-checks">
            {q.data.series.map((s) => (
              <li key={s.country}>
                <span className="label mono">{s.country}</span>
                <span>
                  <StatusTag status={s.status} />
                  {s.points.length ? (
                    <span className="muted">
                      {' '}
                      {s.points.length} points, {periodLabel(s.points[0]!)} to {periodLabel(s.points[s.points.length - 1]!)}
                    </span>
                  ) : null}
                </span>
                {s.reason ? <span className="detail">{s.reason}</span> : null}
                {s.command ? <Commands commands={[s.command]} /> : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </Section>
  );
}

function SourcesPanel({ c }: { c: GlobalComparison }) {
  return (
    <Section title="Sources">
      <ul className="ops-checks">
        {Object.values(c.datasets).map((d) => (
          <li key={d.dataset}>
            <span className="label mono">{d.dataset}</span>
            <span>
              <StatusTag status={d.status} />
              <span className="muted">
                {' '}
                {d.observations} observations{d.first_period_end ? `, periods ${d.first_period_end} to ${d.last_period_end}` : ''} · newest available{' '}
                <IsoTime iso={d.newest_available_time} />
              </span>
            </span>
            {d.status !== 'OK' && d.command ? <Commands commands={[d.command]} /> : null}
          </li>
        ))}
        <li>
          <span className="label mono">fred.series</span>
          <span>
            <StatusTag status={c.fred.status} /> {c.fred.status === 'ACTIVE' ? <span className="muted">{c.fred.attribution}</span> : null}
          </span>
          {c.fred.reason ? <span className="detail">{c.fred.reason}</span> : null}
        </li>
      </ul>
      <h3 className="gm-subhead">Not used (rights review)</h3>
      <ul className="ops-checks">
        {c.blocked_sources.map((b) => (
          <li key={b.provider_id}>
            <span className="label">{b.name}</span>
            <span>
              <StatusTag status="BLOCKED" /> <span className="muted">reviewed {b.verified_on}</span>
            </span>
            <span className="detail">{b.reason}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}
