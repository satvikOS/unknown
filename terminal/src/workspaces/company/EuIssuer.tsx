/**
 * EU issuer view (CAP-CO-03): ESEF annual statements mapped with esef-ifrs-map@1, the ESEF and SEC lines side by side
 * for an owner-chosen SEC filer, and the filings index with FIRDS listings. Every figure carries its source (ESEF
 * filing or SEC accession), currency or unit and availability time; a missing figure shows its reason, never zero.
 */
import { useEffect, useState, type FormEvent } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge } from '../../ui/primitives';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { RouteTabs } from '../operations/parts';
import { apiPath, useApi } from '../research-common/api';
import { checkInstant } from '../research-common/format';
import { ErrorNotice, ExtLink, Flags, IsoTime, KV, Loading, Section, StatusTag, Unavailable, UnitValue } from '../research-common/parts';
import { AsOfField, TextField } from './CompanyWorkspace';
import {
  EU_TABS,
  EU_TAB_LABELS,
  cellText,
  checkCik,
  euRoute,
  euSections,
  leiValid,
  type CompareResponse,
  type CompareSide,
  type EuCell,
  type EuIdentity,
  type EuLine,
  type EuOptions,
  type EuRoute,
  type EuStatements,
  type EuTab,
} from './eu';
import { isUnavailable, type UnavailableBody } from './model';
import './eu.css';

type Nav = (workspace: string, ...rest: string[]) => void;
const ESEF_INGEST = (lei: string) => `python -m unknown.data.sources.esef ingest --lei ${lei}`;

export function EuIssuerView({ r, navigate }: { r: EuRoute; navigate: Nav }) {
  const go = (next: { tab?: EuTab; opts?: EuOptions }) => navigate('company', ...euRoute({ lei: r.lei, tab: next.tab ?? r.tab, opts: next.opts ?? r.opts }));
  const valid = leiValid(r.lei);
  const identity = useApi<EuIdentity | UnavailableBody>(valid ? apiPath('company', 'eu', r.lei) : null, { as_of: r.opts.as_of });
  const stmts = useApi<EuStatements | UnavailableBody>(valid && r.tab === 'statements' ? apiPath('company', 'eu', r.lei, 'statements') : null, {
    as_of: r.opts.as_of,
    periods: r.opts.periods,
  });
  const cmp = useApi<CompareResponse | UnavailableBody>(valid && r.tab === 'compare' ? apiPath('company', 'eu', r.lei, 'compare') : null, {
    as_of: r.opts.as_of,
    periods: r.opts.periods,
    sec: r.opts.sec,
  });
  const id = identity.data && !isUnavailable(identity.data) ? identity.data : null;
  return (
    <WorkspaceFrame
      title={id?.name ?? `EU issuer ${r.lei}`}
      description="EU issuer from ESEF annual reports (filings.xbrl.org) and ESMA FIRDS listings, point in time."
      queries={[identity, ...(r.tab === 'statements' ? [stmts] : r.tab === 'compare' ? [cmp] : [])]}
    >
      {!valid ? <Unavailable what="EU issuer" status="INVALID_LEI" reason={`${r.lei} is not a valid LEI (ISO 17442 check digits)`} /> : null}
      <ErrorNotice what="EU issuer" error={identity.error} />
      {identity.data && isUnavailable(identity.data) ? <Unavailable what="EU issuer" reason={identity.data.reason} commands={[ESEF_INGEST(r.lei)]} /> : null}
      {id ? <EuStrip id={id} /> : null}
      <EuControls r={r} go={go} />
      <RouteTabs idBase="company-eu" label="EU issuer views" tabs={EU_TABS.map((t) => ({ id: t, label: EU_TAB_LABELS[t] }))} current={r.tab} onSelect={(t) => go({ tab: t })} />
      {r.tab === 'statements' ? <StatementsPanel q={stmts} lei={r.lei} /> : null}
      {r.tab === 'compare' ? <ComparePanel q={cmp} hasSec={!!r.opts.sec} /> : null}
      {r.tab === 'filings' ? <FilingsPanel id={id} /> : null}
    </WorkspaceFrame>
  );
}

function EuStrip({ id }: { id: EuIdentity }) {
  return (
    <div className="rx-identity">
      <span className="rx-name">{id.name}</span>
      <span className="mono">LEI {id.lei}</span>
      <Badge>ESEF · IFRS</Badge>
      <span>{id.countries.length ? `filed in ${id.countries.join(', ')}` : 'no EU filing indexed'}</span>
      <StatusTag status={id.esef_status ?? 'UNKNOWN'} />
      <span className="muted">
        {id.source.attribution} · index checked <IsoTime iso={id.index_checked_at} />
      </span>
    </div>
  );
}

function EuControls({ r, go }: { r: EuRoute; go: (n: { tab?: EuTab; opts?: EuOptions }) => void }) {
  const [asOf, setAsOf] = useState(r.opts.as_of ?? '');
  const [periods, setPeriods] = useState(r.opts.periods ?? '');
  const [sec, setSec] = useState(r.opts.sec ?? '');
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setAsOf(r.opts.as_of ?? '');
    setPeriods(r.opts.periods ?? '');
    setSec(r.opts.sec ?? '');
  }, [r.opts.as_of, r.opts.periods, r.opts.sec]);
  const apply = (e: FormEvent) => {
    e.preventDefault();
    const a = asOf.trim() ? checkInstant(asOf) : ({ ok: true, value: '' } as const);
    if (!a.ok) return setErr(a.error);
    if (periods.trim() && !/^\d{1,2}$/.test(periods.trim())) return setErr('periods must be a number (1-20)');
    const c = checkCik(sec);
    if (!c.ok) return setErr(c.error);
    setErr(null);
    go({ opts: { as_of: a.value, periods: periods.trim(), sec: c.value } });
  };
  return (
    <form className="rx-toolbar" onSubmit={apply} aria-label="EU issuer options">
      <AsOfField id="eu-asof" value={asOf} onChange={setAsOf} />
      <TextField id="eu-periods" label="Fiscal years" value={periods} onChange={setPeriods} placeholder="4" width={90} />
      <TextField id="eu-sec" label="Compare with SEC CIK" value={sec} onChange={setSec} placeholder="optional" width={160} />
      <button type="submit" className="btn primary">
        Apply
      </button>
      {err ? (
        <span className="form-error" role="alert">
          {err}
        </span>
      ) : null}
    </form>
  );
}

function StatementsPanel({ q, lei }: { q: ReturnType<typeof useApi<EuStatements | UnavailableBody>>; lei: string }) {
  const [sel, setSel] = useState<EuCell | null>(null);
  if (q.error) return <ErrorNotice what="ESEF statements" error={q.error} />;
  if (!q.data) return <Loading what="the ESEF statements" />;
  if (isUnavailable(q.data)) return <Unavailable what="ESEF statements" reason={q.data.reason} commands={[ESEF_INGEST(lei)]} />;
  const s = q.data;
  if (s.status !== 'OK') return <Unavailable what="ESEF statements" status={s.status} reason={s.reason} commands={s.status === 'NOT_AVAILABLE' ? [ESEF_INGEST(lei)] : []} />;
  return (
    <>
      <p className="rx-hint">
        Mapping <span className="mono">{s.mapping}</span> · basis FY · reporting currency <strong>{s.currency ?? 'unknown'}</strong>
        {s.currency_flags.length ? <Flags flags={s.currency_flags} /> : null} · as of <IsoTime iso={s.as_of} /> · {s.source.attribution}
      </p>
      {euSections(s).map(([key, lines]) => (
        <StatementTable key={key} section={key} lines={lines} columns={s.columns} onSelect={setSel} />
      ))}
      {sel ? <CellDetail c={sel} /> : <p className="rx-hint">Select a cell to see its concept, filing, availability time and restatement history.</p>}
    </>
  );
}

function StatementTable({ section, lines, columns, onSelect }: { section: string; lines: EuLine[]; columns: EuStatements['columns']; onSelect: (c: EuCell) => void }) {
  const cols: Column<EuLine>[] = [
    { id: 'label', header: 'Line item', wrap: true, cell: (l) => <span title={`${l.rule}: ${[...l.concepts, ...l.fallback].join(', ')}`}>{l.label}</span> },
    { id: 'unit', header: 'Unit', cell: (l) => <span className="mono">{l.unit ?? 'unknown'}</span> },
    ...columns.map((p, i) => ({
      id: p.label,
      header: p.label,
      numeric: true,
      cell: (l: EuLine) => {
        const c = l.cells[i];
        if (!c) return <span className="muted">no cell</span>;
        return (
          <button type="button" className="eu-cell" onClick={() => onSelect(c)} title={c.flags.join(' · ') || c.method}>
            {c.value !== null ? <UnitValue value={c.value} unit={c.unit} compact /> : <span className="muted">{cellText(c)}</span>}
            {c.method === 'FALLBACK_CONCEPT' || c.restated || c.flags.some((f) => f.startsWith('from_later_filing')) ? <sup className="eu-mark">*</sup> : null}
          </button>
        );
      },
    })),
  ];
  return (
    <Section title={section.replace('_', ' ')} flush>
      <DataTable caption={`ESEF ${section}`} captionHidden columns={cols} rows={lines} rowKey={(l) => l.item} emptyText="no lines" />
    </Section>
  );
}

function CellDetail({ c }: { c: EuCell }) {
  const src = c.sources[0];
  return (
    <Section title={`${c.item} · ${c.period.label}`}>
      <KV
        rows={[
          ['Value', c.value !== null ? <UnitValue value={c.value} unit={c.unit} /> : <span>{c.missing_reason}</span>],
          ['Method', <span className="mono">{c.method}</span>],
          ['Rule', <span className="mono">{c.rule}</span>],
          ['Concept', <span className="mono">{c.concept ?? 'none'}</span>],
          c.detail ? ['Detail', c.detail] : null,
          ['Flags', <Flags flags={c.flags} />],
          src ? ['Filing', <span className="mono">{src.filing}</span>] : null,
          src ? ['Filed in', `${src.filing_country ?? '?'} (report for ${src.filing_period_end ?? '?'})`] : null,
          src ? ['Available', <IsoTime iso={src.available_time} />] : null,
          src?.viewer_url ? ['Report', <ExtLink href={src.viewer_url}>iXBRL viewer (filings.xbrl.org)</ExtLink>] : null,
          ['Observations', <span className="mono">{c.obs_ids.join(', ') || 'none'}</span>],
          c.history?.length
            ? ['Earlier publications', <span>{c.history.map((h) => `${h.value} (${h.filing}, ${h.available_time})`).join('; ')}</span>]
            : null,
        ]}
      />
    </Section>
  );
}

function sideText(s: CompareSide | null): string {
  if (!s) return 'not in this source';
  return s.value ?? s.missing_reason ?? 'no value';
}

function ComparePanel({ q, hasSec }: { q: ReturnType<typeof useApi<CompareResponse | UnavailableBody>>; hasSec: boolean }) {
  if (q.error) return <ErrorNotice what="ESEF vs SEC" error={q.error} />;
  if (!q.data) return <Loading what="the comparison" />;
  if (isUnavailable(q.data)) return <Unavailable what="ESEF vs SEC" reason={q.data.reason} />;
  const c = q.data;
  return (
    <>
      {!hasSec ? (
        <p className="rx-hint">Enter the SEC CIK of the same issuer (e.g. a 20-F filer) above to show its SEC lines beside the ESEF lines. The pairing is your choice; it is never inferred from names.</p>
      ) : null}
      <p className="rx-hint">
        ESEF: {c.esef.source.attribution} · currency <strong>{c.esef.currency ?? 'unknown'}</strong>
        {c.sec ? (
          <>
            {' '}
            · SEC: <span className="mono">{c.sec.entity_id}</span> ({c.sec.mapping}) · currency <strong>{c.sec.currency}</strong>
          </>
        ) : null}
      </p>
      {c.lines.map((l) => (
        <Section key={l.item} title={l.label} flush>
          <table className="rx-table eu-compare">
            <thead>
              <tr>
                <th>FY end</th>
                <th>ESEF (IFRS)</th>
                <th>SEC</th>
                <th>ESEF − SEC</th>
              </tr>
            </thead>
            <tbody>
              {l.cells.map((cell) => (
                <tr key={cell.fy_end}>
                  <td className="mono">{cell.fy_end}</td>
                  <td title={cell.esef ? `${cell.esef.concept ?? ''} · ${cell.esef.document ?? ''}` : ''}>
                    {cell.esef?.value ? <UnitValue value={cell.esef.value} unit={cell.esef.unit} compact /> : <span className="muted">{sideText(cell.esef)}</span>}
                  </td>
                  <td title={cell.sec ? `${cell.sec.concept ?? ''} · ${cell.sec.document ?? ''}` : ''}>
                    {cell.sec?.value ? <UnitValue value={cell.sec.value} unit={cell.sec.unit} compact /> : <span className="muted">{c.sec ? sideText(cell.sec) : 'no SEC filer chosen'}</span>}
                  </td>
                  <td>{cell.difference_esef_minus_sec ?? <span className="muted">{cell.note ?? '—'}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ))}
      <p className="rx-hint">{c.note}</p>
    </>
  );
}

function FilingsPanel({ id }: { id: EuIdentity | null }) {
  if (!id) return <Loading what="the filings index" />;
  return (
    <>
      <Section title={`ESEF filings available at the as-of time · ${id.filings.length}`}>
        {id.reason ? <Unavailable what="ESEF filings" status={id.esef_status ?? 'NOT_AVAILABLE'} reason={id.reason} /> : null}
        <ul className="ops-checks">
          {id.filings.map((f) => (
            <li key={f.fxo_id}>
              <span className="label mono">{f.fxo_id}</span>
              <span>
                FY ending {f.period_end} · {f.country} · available <IsoTime iso={f.available_time} />{' '}
                {f.viewer_url ? <ExtLink href={f.viewer_url}>viewer</ExtLink> : null}
              </span>
            </li>
          ))}
        </ul>
      </Section>
      <Section title={`EU listings (ESMA FIRDS) · ${id.listings.length}`} note={id.listings_note}>
        {id.listings.length ? (
          <ul className="ops-checks">
            {id.listings.map((l) => (
              <li key={`${l.isin}:${l.mic}`}>
                <span className="label mono">
                  {l.isin} · {l.mic}
                </span>
                <span>
                  {l.full_name} · CFI {l.cfi ?? '—'} · {l.currency ?? 'no currency'} · first trade {l.first_trade_date ?? '—'}
                  {l.termination_date ? ` · terminated ${l.termination_date}` : ''} · <span className="muted">{l.source}</span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No FIRDS listing held for this LEI.</p>
        )}
      </Section>
    </>
  );
}
