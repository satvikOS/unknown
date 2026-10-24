/**
 * Ownership (CAP-CO-09; function OWN): institutional holders from the SEC Form 13F data sets with quarter-over-quarter
 * changes and concentration, insider transactions from Forms 3/4/5 with the open-market flow, and 5 % holders from
 * Schedules 13D/13G. Point in time (as of a date); every row names its filing, and a missing value is an em dash with
 * its reason, never a zero. Reads GET /api/ownership/<cik|ticker>.
 */
import { useEffect, useState, type FormEvent } from 'react';
import { formatQuantity } from '../../format/decimal';
import { useLinkedSecurity } from '../../ui/link';
import { Badge } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { apiPath, useApi } from '../research-common/api';
import { Commands, ErrorNotice, ExtLink, Loading, Section } from '../research-common/parts';
import type { WorkspaceProps } from '../types';
import {
  CHANGE_LABEL,
  TABS,
  amount2,
  changeTone,
  filterInsiders,
  fractionPct,
  holderMatches,
  parseOwnershipRoute,
  type HoldersSection,
  type InsidersSection,
  type InstitutionalSection,
  type OwnershipResponse,
  type SectionStatus,
} from './model';
import './ownership.css';

function Qty({ value, reason, detail }: { value: string | null | undefined; reason?: string | null; detail?: string | null }) {
  if (value === null || value === undefined) return <NoValue reason={reason || 'NOT_REPORTED'} detail={detail} />;
  return (
    <span className="num" title={value}>
      {formatQuantity(value)}
    </span>
  );
}

function Pct({ value, reason }: { value: string | null | undefined; reason?: string | null }) {
  const p = fractionPct(value);
  if (p === null) return <NoValue reason={reason || 'NOT_REPORTED'} />;
  return (
    <span className="num" title={`${value} (fraction of shares outstanding)`}>
      {p}
    </span>
  );
}

function SectionUnavailable({ s, what, commands }: { s: SectionStatus; what: string; commands: readonly string[] }) {
  return (
    <div className="own-empty" role="status">
      <NoValue reason={s.reason || 'NO_DATA'} detail={s.detail} focusable label={what} /> <span>{s.detail}</span>
      <Commands commands={commands} />
    </div>
  );
}

export function InstitutionalView({ s, commands }: { s: InstitutionalSection; commands: readonly string[] }) {
  const [q, setQ] = useState('');
  if (s.status !== 'OK') return <SectionUnavailable s={s} what="Institutional holders" commands={commands} />;
  const rows = s.holders.filter((h) => holderMatches(h, q));
  const so = s.shares_outstanding;
  return (
    <>
      <dl className="own-facts">
        <div>
          <dt>Report period</dt>
          <dd>
            {s.period} <span className="muted">(prior {s.prior_period ?? 'none held'})</span>
          </dd>
        </div>
        <div>
          <dt>Holders reporting</dt>
          <dd className="num">{s.holder_count}</dd>
        </div>
        <div>
          <dt>Shares reported</dt>
          <dd>
            <Qty value={s.total_shares_reported} />
          </dd>
        </div>
        <div>
          <dt>Shares outstanding</dt>
          <dd>
            <Qty value={so?.value} reason={so?.reason} detail={so?.detail} />
            {so?.date ? <span className="muted"> at {so.date}</span> : null}
          </dd>
        </div>
        {(['top5', 'top10', 'top20'] as const).map((k) => (
          <div key={k}>
            <dt>Top {k.slice(3)}</dt>
            <dd>
              <Pct value={s.concentration?.[k]} reason="NOT_REPORTED" />
            </dd>
          </div>
        ))}
        <div>
          <dt>Changes</dt>
          <dd>
            {s.changes ? `${s.changes.entries} new · ${s.changes.exits} exits · ${s.changes.increases} up · ${s.changes.decreases} down` : '—'}
          </dd>
        </div>
      </dl>
      <div className="own-toolbar">
        <label>
          <span>Filter holders</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Manager name or CIK" />
        </label>
        <span className="muted">
          {rows.length} of {s.holders.length} shown
        </span>
      </div>
      <div className="rx-table-wrap">
        <table className="rx-table own-table">
          <caption>Institutional holders, report period {s.period} (Form 13F information tables)</caption>
          <thead>
            <tr>
              <th>Holder</th>
              <th className="num">Shares</th>
              <th className="num">% held</th>
              <th>Change</th>
              <th className="num">Change (shares)</th>
              <th className="num">Value (USD)</th>
              <th>Filed</th>
              <th>Filing</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((h) => (
              <tr key={`${h.manager_cik}-${h.accession}`}>
                <td>
                  {h.manager ?? <NoValue reason="NOT_REPORTED" />} <span className="muted mono">{h.manager_cik}</span>
                  {h.amendment_type ? <Badge tone="info">{h.amendment_type.toLowerCase()}</Badge> : null}
                </td>
                <td className="num">
                  <Qty value={h.shares} reason={h.shares_reason} />
                </td>
                <td className="num">{h.shares === null ? <NoValue reason="NOT_APPLICABLE" /> : <Pct value={h.pct_held} reason={h.pct_reason} />}</td>
                <td>
                  <Badge tone={changeTone(h.change)}>{CHANGE_LABEL[h.change] ?? h.change}</Badge>
                </td>
                <td className="num">
                  <Qty value={h.change_shares} reason="NOT_APPLICABLE" />
                </td>
                <td className="num">
                  {h.value ? <Qty value={h.value} /> : <NoValue reason="NOT_APPLICABLE" />}
                  {h.value_unit === 'USD_thousands' ? <span className="muted"> k</span> : null}
                </td>
                <td>{h.filing_date ?? <NoValue reason="NOT_APPLICABLE" />}</td>
                <td className="mono">{h.accession}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="own-notes">
        {(s.notes ?? []).map((n) => (
          <li key={n}>{n}</li>
        ))}
        {(s.coverage ?? []).map((c) => (
          <li key={c.window}>
            Data set {c.window}: {c.holdings_reports_in_window} holdings reports for {c.period}, {c.holders_found} list this issuer.
          </li>
        ))}
      </ul>
    </>
  );
}

export function InsidersView({ s, commands }: { s: InsidersSection; commands: readonly string[] }) {
  const [only, setOnly] = useState<'all' | 'open_market'>('all');
  if (s.status !== 'OK') return <SectionUnavailable s={s} what="Insider transactions" commands={commands} />;
  const rows = filterInsiders(s.transactions, only);
  return (
    <>
      <div className="own-flow">
        {Object.values(s.summary ?? {}).map((f) => (
          <div key={f.window_days} className="own-flow-card">
            <div className="micro">Net open-market flow, {f.window_days} days</div>
            <div className="num own-flow-net">{amount2(f.net)}</div>
            <div className="muted">
              buys {amount2(f.purchases)} ({f.purchase_count}) · sells {amount2(f.sales)} ({f.sale_count})
              {f.excluded_without_price ? ` · ${f.excluded_without_price} without a price not valued` : ''}
            </div>
            <div className="micro muted">{f.unit}</div>
          </div>
        ))}
      </div>
      <div className="own-toolbar">
        <label>
          <span>Show</span>
          <select value={only} onChange={(e) => setOnly(e.target.value as 'all' | 'open_market')}>
            <option value="all">All rows (grants, exercises, holdings)</option>
            <option value="open_market">Open-market purchases and sales (P, S)</option>
          </select>
        </label>
        <span className="muted">{s.ten_b5_one}</span>
      </div>
      <div className="rx-table-wrap">
        <table className="rx-table own-table">
          <caption>Insider transactions and holdings (newest first)</caption>
          <thead>
            <tr>
              <th>Date</th>
              <th>Insider</th>
              <th>Role</th>
              <th>Code</th>
              <th>A/D</th>
              <th className="num">Shares</th>
              <th className="num">Price</th>
              <th className="num">Owned after</th>
              <th>D/I</th>
              <th>Security</th>
              <th>Filing</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.obs_id} className={t.superseded_by_amendment ? 'own-superseded' : undefined}>
                <td>{t.date ?? <NoValue reason="NOT_REPORTED" />}</td>
                <td>{t.insider}</td>
                <td>{t.role}</td>
                <td title={t.code_label}>
                  {t.code ?? <span className="muted">holding</span>} <span className="muted">{t.code ? t.code_label : ''}</span>
                </td>
                <td>{t.acquired_disposed ?? <NoValue reason="NOT_APPLICABLE" />}</td>
                <td className="num">
                  <Qty value={t.shares} reason={t.kind === 'holding' ? 'NOT_APPLICABLE' : 'NOT_REPORTED'} />
                </td>
                <td className="num">
                  {t.price !== null ? <span className="num">{t.price}</span> : <NoValue reason={t.price_reason ?? 'NOT_REPORTED'} />}
                </td>
                <td className="num">
                  <Qty value={t.shares_after} />
                </td>
                <td title={t.nature ?? undefined}>{t.direct_indirect ?? <NoValue reason="NOT_REPORTED" />}</td>
                <td>
                  {t.security}
                  {t.table === 'derivative' ? <span className="muted"> (derivative)</span> : null}
                </td>
                <td>
                  <ExtLink href={t.links.filing_index}>{t.form}</ExtLink> <span className="muted">{t.filing_date}</span>
                  {t.superseded_by_amendment ? <Badge tone="warn">amended</Badge> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function HoldersView({ s, commands }: { s: HoldersSection; commands: readonly string[] }) {
  if (s.status !== 'OK') return <SectionUnavailable s={s} what="5 % holders" commands={commands} />;
  return (
    <>
      <div className="rx-table-wrap">
        <table className="rx-table own-table">
          <caption>Schedules 13D and 13G on this issuer, with amendments (newest first)</caption>
          <thead>
            <tr>
              <th>Filed</th>
              <th>Form</th>
              <th>Reporting person</th>
              <th className="num">Shares</th>
              <th className="num">% of class</th>
              <th>Event date</th>
              <th>Filing</th>
            </tr>
          </thead>
          <tbody>
            {s.filings.flatMap((f) =>
              (f.persons.length ? f.persons : [null]).map((p, i) => (
                <tr key={`${f.accession}-${i}`}>
                  <td>{f.filing_date}</td>
                  <td>{f.form}</td>
                  <td>{p ? p.name : <span className="muted">{f.reason}</span>}</td>
                  <td className="num">{p ? <Qty value={p.aggregate_shares} /> : <NoValue reason="NOT_REPORTED" detail={f.reason} />}</td>
                  <td className="num">
                    {p && p.percent_of_class !== null ? `${p.percent_of_class} %` : <NoValue reason="NOT_REPORTED" detail={f.reason} />}
                  </td>
                  <td>{f.event_date ?? <NoValue reason="NOT_REPORTED" />}</td>
                  <td>
                    <ExtLink href={f.index_url}>{f.accession}</ExtLink>
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
      {s.note ? <p className="muted">{s.note}</p> : null}
    </>
  );
}

export function OwnershipWorkspace({ route, navigate }: WorkspaceProps) {
  const parsed = parseOwnershipRoute(route.rest);
  const linked = useLinkedSecurity();
  const key = parsed.key ?? (linked ? linked.toUpperCase() : null);
  const [draft, setDraft] = useState(key ?? '');
  const [asOf, setAsOf] = useState('');
  useEffect(() => setDraft(key ?? ''), [key]);
  const q = useApi<OwnershipResponse>(key ? apiPath('ownership', key) : null, { as_of: asOf || undefined, top: 200 });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (draft.trim()) navigate('ownership', draft.trim().toUpperCase(), parsed.tab);
  };
  const d = q.data;
  return (
    <WorkspaceFrame
      title="Ownership"
      description="Institutional holders (13F), insider transactions (Forms 3/4/5) and 5 % holders (13D/13G) from SEC EDGAR, point in time"
      queries={[q]}
    >
      <form className="own-toolbar" onSubmit={submit}>
        <label>
          <span>Issuer</span>
          <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Ticker or CIK" size={12} />
        </label>
        <label>
          <span>As of</span>
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
        </label>
        <button type="submit" className="btn primary">
          Open
        </button>
      </form>
      {!key ? <p className="muted">Type a ticker or CIK, or link a security (OWN on the command line).</p> : null}
      <ErrorNotice what="Ownership" error={q.error} />
      {key && !d && !q.error ? <Loading what="ownership" /> : null}
      {d && d.status === 'UNAVAILABLE' ? (
        <div className="own-empty" role="status">
          <NoValue reason={d.reason || 'NO_DATA'} detail={d.detail} focusable label="Ownership" /> {d.detail}
        </div>
      ) : null}
      {d && d.status !== 'UNAVAILABLE' ? (
        <>
          <p className="own-head">
            <strong>{d.name}</strong> <span className="muted">CIK {d.cik}</span> {d.tickers.join(', ')}{' '}
            <span className="muted">as of {d.as_of.slice(0, 19).replace('T', ' ')} UTC</span>
          </p>
          <nav className="own-tabs" aria-label="Ownership views">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                className={`btn${t.id === parsed.tab ? ' primary' : ''}`}
                aria-current={t.id === parsed.tab ? 'page' : undefined}
                onClick={() => navigate('ownership', key ?? '', t.id)}
              >
                {t.label}
              </button>
            ))}
          </nav>
          {parsed.tab === 'institutional' && d.institutional ? (
            <Section title="Institutional holders" note="Source: U.S. SEC Form 13F data sets">
              <InstitutionalView s={d.institutional} commands={d.commands} />
            </Section>
          ) : null}
          {parsed.tab === 'insiders' && d.insiders ? (
            <Section title="Insider transactions" note="Source: U.S. SEC EDGAR Forms 3, 4 and 5">
              <InsidersView s={d.insiders} commands={d.commands} />
            </Section>
          ) : null}
          {parsed.tab === 'holders' && d.major_holders ? (
            <Section title="5 % holders" note="Source: U.S. SEC EDGAR Schedules 13D and 13G">
              <HoldersView s={d.major_holders} commands={d.commands} />
            </Section>
          ) : null}
          <p className="micro muted">{d.non_us}</p>
        </>
      ) : null}
    </WorkspaceFrame>
  );
}
