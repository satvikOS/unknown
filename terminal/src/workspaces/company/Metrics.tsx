/**
 * Metrics (CAP-CO-07): GET /api/company/<cik>/metrics, every formula with its status. NM, MISSING, INCOMPLETE,
 * SUPPRESSED and NO_PRICE come with the API's reason, shown verbatim. "Show calculation" reads
 * /api/company/<cik>/metrics/<metric>: expression, inputs down to the source filings, and the EV bridge.
 */
import { useEffect, useRef, useState } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Money } from '../../ui/primitives';
import { apiPath, useApi } from '../research-common/api';
import { humanize } from '../research-common/format';
import { ErrorNotice, ExtLink, Flags, IsoTime, KV, Loading, Section, StatusTag, Unavailable, UnitValue } from '../research-common/parts';
import { IssuerFrame, type IssuerContext } from './CompanyWorkspace';
import { isUnavailable, priceParams, type BridgeItem, type ExplainInput, type MetricExplain, type MetricRow, type MetricsResponse, type PriceInputJson, type UnavailableBody } from './model';
import { CellDetail } from './Statements';

export function MetricsView({ ctx }: { ctx: IssuerContext }) {
  const params = { as_of: ctx.opts.as_of, ...priceParams(ctx.opts) };
  const q = useApi<MetricsResponse | UnavailableBody>(apiPath('company', ctx.cik, 'metrics'), params);
  const [metric, setMetric] = useState<string | null>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (metric && detailRef.current) {
      detailRef.current.scrollIntoView({ block: 'start' });
      detailRef.current.focus({ preventScroll: true });
    }
  }, [metric]);
  const explain = useApi<MetricExplain>(metric ? apiPath('company', ctx.cik, 'metrics', metric) : null, params);
  const data = q.data && !isUnavailable(q.data) ? q.data : null;

  return (
    <IssuerFrame ctx={ctx} queries={metric ? [q, explain] : [q]}>
      <ErrorNotice what="Metrics" error={q.error} />
      {q.data && isUnavailable(q.data) ? <Unavailable what="Metrics" reason={q.data.reason} /> : null}
      {!q.data && !q.error ? <Loading what="metrics" /> : null}
      {data ? (
        <>
          <p className="rx-hint">
            As of <IsoTime iso={data.as_of} /> · reporting currency <span className="mono">{data.currency}</span> · mapping <span className="mono">{data.mapping}</span> · latest quarter{' '}
            {data.latest_quarter ? <span className="mono">{`${data.latest_quarter.label} (${data.latest_quarter.start} → ${data.latest_quarter.end})`}</span> : 'none reported at as of'} · price input{' '}
            <PriceLine p={data.price_input} />
          </p>
          <MetricsTable data={data} selected={metric} onSelect={setMetric} />
          {metric ? (
            <div ref={detailRef} tabIndex={-1} aria-label={`Calculation of ${metric}`} className="rx-gap">
              <Section
                title={`Show calculation · ${metric}`}
                actions={
                  <button type="button" className="btn small" onClick={() => setMetric(null)}>
                    Close
                  </button>
                }
              >
                <ErrorNotice what={`Calculation of ${metric}`} error={explain.error} />
                {explain.data ? <MetricDetail m={explain.data} /> : !explain.error ? <Loading what="the calculation" /> : null}
              </Section>
            </div>
          ) : (
            <p className="rx-hint">Select a metric (Enter on a row, or double-click) to show its calculation: formula, inputs down to the source filings, and the bridge.</p>
          )}
        </>
      ) : null}
    </IssuerFrame>
  );
}

export function PriceLine({ p }: { p: PriceInputJson | null | undefined }) {
  if (!p) return <span className="muted">none: price-based metrics are NO_PRICE (no price is fetched implicitly)</span>;
  return (
    <span>
      <Money amount={p.value} currency={p.currency} /> <span className="muted">from {p.source}, observed</span> <IsoTime iso={p.observed_at} />
      {p.persisted ? null : <span className="muted"> · not persisted</span>}
    </span>
  );
}

function MetricsTable({ data, selected, onSelect }: { data: MetricsResponse; selected: string | null; onSelect: (m: string) => void }) {
  const columns: Column<MetricRow>[] = [
    {
      id: 'metric',
      header: 'Metric',
      sortValue: (m) => m.metric,
      cell: (m) => (
        <span title={m.expression}>
          <strong>{humanize(m.metric)}</strong> <span className="muted mono">{m.formula}</span>
          {selected === m.metric ? <span className="sr-only"> (selected)</span> : null}
        </span>
      ),
    },
    { id: 'value', header: 'Value', numeric: true, cell: (m) => <UnitValue value={m.value} unit={m.unit} missing={m.status} compact /> },
    { id: 'status', header: 'Status', sortValue: (m) => m.status, cell: (m) => <StatusTag status={m.status} /> },
    {
      id: 'reason',
      header: 'Reason (verbatim)',
      wrap: true,
      width: '34%',
      cell: (m) => (m.reason ? <span className="rx-reason">{m.reason}</span> : <span className="muted">—</span>),
    },
    { id: 'period', header: 'Period · basis', sortValue: (m) => m.period, cell: (m) => <span title={m.basis}><span className="mono">{m.period}</span></span> },
    { id: 'flags', header: 'Flags', wrap: true, width: '18%', cell: (m) => <Flags flags={m.flags} /> },
  ];
  const counts = new Map<string, number>();
  for (const m of data.metrics) counts.set(m.status, (counts.get(m.status) ?? 0) + 1);
  return (
    <Section
      title={`Metrics · ${data.metrics.length}`}
      flush
      actions={
        <span className="rx-flags">
          {[...counts.entries()].map(([s, n]) => (
            <StatusTag key={s} status={s} detail={`${n} metric(s)`} />
          ))}
        </span>
      }
      note="Ratios (unit ratio) are shown as exact percentages: the decimal point is moved and nothing is rounded beyond the API's HALF_EVEN at the formula's scale. The ratio itself is in the tooltip and in the calculation. Reasons are the API's text."
    >
      <DataTable
        caption={`Metrics of ${data.entity_id} as of ${data.as_of}`}
        captionHidden
        columns={columns}
        rows={data.metrics}
        rowKey={(m) => m.metric}
        onRowActivate={(m) => onSelect(m.metric)}
        activateLabel="show the calculation"
        maxHeight="min(72vh, 900px)"
      />
    </Section>
  );
}

export function MetricDetail({ m }: { m: MetricExplain }) {
  return (
    <div>
      <KV
        rows={[
          ['Formula', <span className="mono">{m.formula}</span>],
          ['Expression', <span className="mono">{m.expression}</span>],
          m.nm_rule ? ['NM rule', <span className="mono">{m.nm_rule}</span>] : null,
          ['Basis', m.basis],
          ['Period', <span className="mono">{m.period}</span>],
          ['Value', <UnitValue value={m.value} unit={m.unit} missing={m.status} />],
          ['Status', <StatusTag status={m.status} />],
          m.reason ? ['Reason', <span className="rx-reason">{m.reason}</span>] : null,
          m.flags.length ? ['Flags', <Flags flags={m.flags} />] : null,
          ['Source dates', m.source_dates.length ? <span>{m.source_dates.map((d, i) => <span key={d}>{i ? ', ' : ''}<IsoTime iso={d} /></span>)}</span> : <span className="muted">none</span>],
          ['Observation ids', <span className="mono">{m.input_obs_ids.join(', ') || 'none'}</span>],
        ]}
      />
      {m.bridge?.length ? <Bridge items={m.bridge} currency={m.currency ?? m.unit} /> : null}
      {m.inputs.length ? (
        <>
          <h3 className="rx-h3">Inputs</h3>
          <ul className="ops-checks">
            {m.inputs.map((i, k) => (
              <li key={k}>
                <InputLine i={i} />
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

function InputLine({ i }: { i: ExplainInput }) {
  return (
    <>
      <span className="label mono">{i.role}</span>
      <span>
        {i.cell ? (
          <UnitValue value={i.cell.value} unit={i.cell.unit} missing={i.cell.missing_reason ?? 'missing'} />
        ) : i.price ? (
          <PriceLine p={i.price} />
        ) : i.metric ? (
          <span>
            <UnitValue value={i.metric.value} unit={i.metric.unit} missing={i.metric.status} /> <StatusTag status={i.metric.status} />
          </span>
        ) : i.value !== undefined ? (
          <span className="num">{i.value}</span>
        ) : (
          <span className="muted">no value</span>
        )}
      </span>
      {i.cell ? (
        <details className="rx-details detail">
          <summary>How {i.cell.item} ({i.cell.period.label}) was obtained</summary>
          <CellDetail cell={i.cell} />
        </details>
      ) : i.metric ? (
        <details className="rx-details detail">
          <summary>Calculation of {i.metric.metric}</summary>
          <MetricDetail m={i.metric} />
        </details>
      ) : null}
    </>
  );
}

export function Bridge({ items, currency }: { items: readonly BridgeItem[]; currency: string }) {
  return (
    <>
      <h3 className="rx-h3">Bridge</h3>
      <div className="rx-table-wrap">
        <table className="rx-table">
          <caption>Components and their contribution; a missing component is excluded, never treated as zero</caption>
          <thead>
            <tr>
              <th scope="col">Component</th>
              <th scope="col">Sign</th>
              <th scope="col" className="num">
                Value
              </th>
              <th scope="col">Status</th>
              <th scope="col">Lineage or reason</th>
            </tr>
          </thead>
          <tbody>
            {items.map((b) => (
              <tr key={b.component}>
                <th scope="row">{humanize(b.component)}</th>
                <td className="mono">{b.sign === 1 || b.sign === '+' ? '+' : '−'}</td>
                <td className="num">{b.value === null ? <span className="rx-missing">{b.missing_reason ?? 'MISSING'}</span> : <Money amount={b.value} currency={currency} />}</td>
                <td>
                  <StatusTag status={b.status} />
                </td>
                <td className="wrap">
                  {b.value === null ? (
                    <span className="rx-reason">{`${b.missing_reason ?? ''}${b.detail ? `: ${b.detail}` : ''}`}</span>
                  ) : (
                    <span className="muted">
                      {b.source ? `${b.source} · ` : ''}
                      {b.method ? `${b.method} · ` : ''}
                      {b.period_end ? `at ${b.period_end} · ` : ''}
                      obs {(b.obs_ids ?? []).join(', ') || 'none'}
                      {b.sec_filing_index && b.accessions?.length ? (
                        <>
                          {' · '}
                          <ExtLink href={b.sec_filing_index}>{b.accessions.join(', ')}</ExtLink>
                        </>
                      ) : null}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
