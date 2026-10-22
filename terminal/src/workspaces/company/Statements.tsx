/**
 * Point-in-time standardized statements (CAP-CO-02/05): GET /api/company/<cik>/statements with as_of, an
 * optional knowledge cutoff (what was known at an earlier time), basis FY / Q / TTM and the number of periods.
 * Every cell shows its value with its unit, or its missing reason verbatim; selecting a cell shows how it was
 * obtained (method, rule, concept or formula, components, source filings, restatement history).
 */
import { useEffect, useState, type FormEvent } from 'react';
import { Badge } from '../../ui/primitives';
import { Segmented } from '../operations/parts';
import { apiPath, useApi } from '../research-common/api';
import { checkInstant } from '../research-common/format';
import { ErrorNotice, ExtLink, Flags, IsoTime, KV, Loading, Section, StatusTag, Unavailable, UnitValue } from '../research-common/parts';
import { AsOfField, IssuerFrame, type IssuerContext } from './CompanyWorkspace';
import {
  STATEMENT_TITLES,
  cellCensus,
  isFullCell,
  isUnavailable,
  statementOrder,
  unitLabel,
  type Cell,
  type CellComponent,
  type StatementResponse,
  type UnavailableBody,
} from './model';

const BASES = ['FY', 'Q', 'TTM'] as const;
type Basis = (typeof BASES)[number];

export function StatementsView({ ctx }: { ctx: IssuerContext }) {
  const basis: Basis = (BASES as readonly string[]).includes(ctx.opts.basis ?? '') ? (ctx.opts.basis as Basis) : 'FY';
  const periods = ctx.opts.periods && /^\d+$/.test(ctx.opts.periods) ? ctx.opts.periods : basis === 'Q' ? '4' : basis === 'TTM' ? '2' : '3';
  const q = useApi<StatementResponse | UnavailableBody>(apiPath('company', ctx.cik, 'statements'), {
    as_of: ctx.opts.as_of,
    known_at: ctx.opts.known_at,
    basis,
    periods,
    history: 1,
  });
  const [selected, setSelected] = useState<{ item: string; col: number } | null>(null);
  const [known, setKnown] = useState(ctx.opts.known_at ?? '');
  const [n, setN] = useState(periods);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setKnown(ctx.opts.known_at ?? '');
    setN(periods);
  }, [ctx.opts.known_at, periods]);

  const apply = (e: FormEvent) => {
    e.preventDefault();
    const k = checkInstant(known, 'known at');
    if (!k.ok) return setErr(k.error);
    if (!/^\d+$/.test(n.trim()) || Number(n) < 1 || Number(n) > 20) return setErr('periods must be a whole number from 1 to 20');
    setErr(null);
    ctx.go({ opts: { ...ctx.opts, known_at: k.value, periods: n.trim() } });
  };

  const data = q.data && !isUnavailable(q.data) ? q.data : null;
  const cell = data && selected ? findCell(data, selected.item, selected.col) : null;

  return (
    <IssuerFrame ctx={ctx} queries={[q]}>
      <form className="rx-toolbar" onSubmit={apply} aria-label="Statement options">
        <div className="field">
          <span className="kv-label">Basis</span>
          <Segmented
            label="Basis"
            options={BASES.map((b) => ({ value: b, label: b === 'FY' ? 'Fiscal years' : b === 'Q' ? 'Quarters' : 'Trailing 12 months' }))}
            value={basis}
            onChange={(b) => {
              setSelected(null);
              ctx.go({ opts: { ...ctx.opts, basis: b, periods: '' } });
            }}
          />
        </div>
        <div className="field" style={{ width: 90 }}>
          <label htmlFor="stmt-periods">Periods</label>
          <input id="stmt-periods" className="input mono" value={n} onChange={(e) => setN(e.target.value)} inputMode="numeric" />
        </div>
        <AsOfField id="stmt-known" label="Knowledge cutoff (optional)" value={known} onChange={setKnown} />
        <button type="submit" className="btn">
          Apply
        </button>
        {err ? (
          <span className="form-error" role="alert">
            {err}
          </span>
        ) : null}
      </form>
      <ErrorNotice what="Statements" error={q.error} />
      {q.data && isUnavailable(q.data) ? <Unavailable what="Statements" reason={q.data.reason} /> : null}
      {!q.data && !q.error ? <Loading what="statements" /> : null}
      {data ? (
        <div className="rx-split">
          <div>
            <StatementTable data={data} selected={selected} onSelect={setSelected} />
          </div>
          <div>
            {cell ? (
              <Section title={`${cell.row} · ${cell.cell.period.label}`} actions={<button type="button" className="btn small" onClick={() => setSelected(null)}>Close</button>}>
                <CellDetail cell={cell.cell} />
              </Section>
            ) : (
              <Section title="Cell detail">
                <p className="muted">Select a value (or a missing reason) to see its method, rule, source filings and restatement history.</p>
              </Section>
            )}
            <CoveragePanel data={data} />
          </div>
        </div>
      ) : null}
    </IssuerFrame>
  );
}

function findCell(data: StatementResponse, item: string, col: number): { row: string; cell: Cell } | null {
  for (const rows of Object.values(data.statements)) {
    const r = rows.find((x) => x.item === item);
    const c = r?.cells[col];
    if (r && c) return { row: r.label, cell: c };
  }
  return null;
}

function StatementTable({ data, selected, onSelect }: { data: StatementResponse; selected: { item: string; col: number } | null; onSelect: (s: { item: string; col: number }) => void }) {
  const cols = data.columns;
  return (
    <Section
      title={`Statements · ${data.basis} · ${data.currency}`}
      flush
      note={
        <>
          As of <IsoTime iso={data.as_of} />
          {data.knowledge_cutoff ? (
            <>
              {' '}
              · knowledge cutoff <IsoTime iso={data.knowledge_cutoff} />
            </>
          ) : null}{' '}
          · mapping <span className="mono">{data.mapping}</span> · {data.facts_used} facts used · {data.source.attribution}
        </>
      }
    >
      {cols.length === 0 ? <p className="empty">No {data.basis} period was reported at the as-of time.</p> : null}
      <div className="rx-table-wrap" style={{ maxHeight: 'min(72vh, 900px)' }}>
        <table className="rx-table">
          <caption>
            Standardized {data.basis} statements of {data.entity_id}, values in {data.currency} unless the row says otherwise
          </caption>
          <thead>
            <tr>
              <th scope="col">Line item</th>
              {cols.map((c) => (
                <th key={c.label} scope="col" className="num">
                  <span className="rx-col-head">
                    <span>{c.label}</span>
                    <span className="muted mono">
                      {c.start ? `${c.start} → ` : ''}
                      {c.end}
                      {c.days ? ` (${c.days} d)` : ''}
                    </span>
                    {c.flags.length ? <Flags flags={c.flags} /> : null}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          {statementOrder(data.statements).map((key) => (
            <tbody key={key}>
              <tr className="rx-group">
                <th scope="colgroup" colSpan={cols.length + 1}>
                  {STATEMENT_TITLES[key] ?? key}
                </th>
              </tr>
              {(data.statements[key] ?? []).map((row) => (
                <tr key={row.item}>
                  <th scope="row" title={`${row.rule}${row.note ? `: ${row.note}` : ''}`}>
                    {row.label} <span className="muted">({unitLabel(row.unit)})</span>
                  </th>
                  {cols.map((col, i) => {
                    const c = row.cells[i];
                    if (!c) return <td key={col.label} className="num muted">—</td>;
                    const isSel = selected?.item === row.item && selected.col === i;
                    return (
                      <td key={col.label} className="num">
                        <button
                          type="button"
                          className="rx-cell-btn"
                          aria-pressed={isSel}
                          aria-label={`${row.label}, ${col.label}: ${c.value ?? `missing, ${c.missing_reason ?? 'no reason'}`}${c.restated ? ', restated' : ''}`}
                          title={c.value === null ? (c.detail ?? c.missing_reason ?? '') : `${c.method}${c.formula ? `: ${c.formula}` : ''}`}
                          onClick={() => onSelect({ item: row.item, col: i })}
                        >
                          {c.value === null ? (
                            <span className="rx-missing">{c.missing_reason ?? 'MISSING'}</span>
                          ) : (
                            <>
                              {c.restated ? <Badge tone="warn" title="Restated: an earlier filing reported a different value">R</Badge> : null}
                              <CellValue cell={c} />
                            </>
                          )}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
    </Section>
  );
}

export function CellValue({ cell }: { cell: { value: string | null; unit: string } }) {
  return <UnitValue value={cell.value} unit={unitLabel(cell.unit)} />;
}

function CoveragePanel({ data }: { data: StatementResponse }) {
  const census = cellCensus(data);
  const cov = data.coverage;
  return (
    <Section title="Coverage" note={cov.note}>
      <KV
        rows={[
          ['Mapping', <span className="mono">{cov.mapping}</span>],
          ['Concepts reported', <span className="num">{cov.reported_concepts}</span>],
          ['Mapped concepts used', <span className="num">{cov.mapped_concepts_used}</span>],
          [
            'Unmapped concepts',
            <span>
              <span className="num">{cov.unmapped_concepts}</span>
              {cov.unmapped_sample.length ? <span className="muted mono"> e.g. {cov.unmapped_sample.join(', ')}</span> : null}
            </span>,
          ],
          ['Cells', <span className="num">{census.total}</span>],
          ['By method', <span className="mono">{census.methods.map(([m, k]) => `${m} ${k}`).join(' · ') || 'none'}</span>],
          ['Missing, by reason', <span className="mono">{census.missing.map(([m, k]) => `${m} ${k}`).join(' · ') || 'none'}</span>],
          data.currency_flags.length ? ['Currency flags', <Flags flags={data.currency_flags} />] : null,
          data.calendar_flags.length ? ['Calendar flags', <Flags flags={data.calendar_flags} />] : null,
          ['Source', <span>{data.source.dataset} ({data.source.provider})</span>],
        ]}
      />
    </Section>
  );
}

/** Everything the API says about how one cell was obtained (also used by a metric's show-calculation). */
export function CellDetail({ cell, depth = 0 }: { cell: Cell; depth?: number }) {
  return (
    <div className="rx-cell-detail">
      <KV
        rows={[
          ['Value', cell.value === null ? <StatusTag status={cell.missing_reason ?? 'MISSING'} /> : <CellValue cell={cell} />],
          cell.value === null ? ['Missing reason', <span className="rx-reason">{cell.missing_reason}{cell.detail ? `: ${cell.detail}` : ''}</span>] : null,
          ['Period', <span className="mono">{`${cell.period.label} (${cell.period.start ?? '…'} → ${cell.period.end})`}</span>],
          ['Method', <span className="mono">{cell.method}</span>],
          ['Rule', <span className="mono">{cell.rule}</span>],
          cell.concept ? ['Concept', <span className="mono">{cell.concept}</span>] : null,
          cell.formula ? ['Formula', <span className="mono">{cell.formula}</span>] : null,
          cell.flags.length ? ['Flags', <Flags flags={cell.flags} />] : null,
          cell.not_reported.length ? ['Not reported', <span className="mono">{cell.not_reported.join(', ')}</span>] : null,
          cell.restated !== undefined ? ['Restated', cell.restated ? <StatusTag status="RESTATED" /> : 'no'] : null,
          ['Latest input available', <IsoTime iso={cell.latest_input_available_time} />],
          ['Observation ids', <span className="mono">{cell.obs_ids.join(', ') || 'none'}</span>],
        ]}
      />
      {cell.sources?.length ? (
        <>
          <h3 className="rx-h3">Source filings</h3>
          <ul className="ops-checks">
            {cell.sources.map((s) => (
              <li key={s.obs_id}>
                <span className="label mono">{s.form}</span>
                <span>
                  <UnitValue value={s.value} unit={unitLabel(s.unit)} /> <span className="muted mono">{s.concept}</span>
                </span>
                <span className="detail">
                  {s.sec_filing_index ? <ExtLink href={s.sec_filing_index}>{s.accession}</ExtLink> : s.accession} · available <IsoTime iso={s.available_time} /> · obs {s.obs_id}
                  {s.quality.length ? ` · ${s.quality.join(', ')}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {cell.history?.length ? (
        <>
          <h3 className="rx-h3">Earlier values (restatement history)</h3>
          <ul className="ops-checks">
            {cell.history.map((h) => (
              <li key={h.obs_id}>
                <span className="label mono">{h.accession}</span>
                <span>
                  <UnitValue value={h.value} unit={unitLabel(cell.unit)} />
                </span>
                <span className="detail">
                  available <IsoTime iso={h.available_time} /> · obs {h.obs_id}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {cell.components?.length ? (
        <>
          <h3 className="rx-h3">Components</h3>
          <ul className="ops-checks">
            {cell.components.map((c, i) => (
              <li key={i}>
                <ComponentLine c={c} depth={depth} />
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

function ComponentLine({ c, depth }: { c: CellComponent | Cell; depth: number }) {
  if (!isFullCell(c)) {
    return (
      <>
        <span className="label mono">{`${c.item} · ${c.period}`}</span>
        <span>
          {c.value === null ? <span className="rx-missing">missing</span> : <span className="num">{c.value}</span>} <span className="rx-method">{c.method}</span>
        </span>
        <span className="detail mono">obs {c.obs_ids.join(', ') || 'none'}</span>
      </>
    );
  }
  return (
    <>
      <span className="label mono">{`${c.item} · ${c.period.label}`}</span>
      <span>
        <CellValue cell={c} /> <span className="rx-method">{c.method}</span>
      </span>
      {depth < 2 ? (
        <details className="rx-details detail">
          <summary>How {c.item} was obtained</summary>
          <CellDetail cell={c} depth={depth + 1} />
        </details>
      ) : (
        <span className="detail mono">obs {c.obs_ids.join(', ') || 'none'}</span>
      )}
    </>
  );
}
