/**
 * Peers (CAP-CO-10): GET /api/company/<cik>/peers — the peer rule, members, exclusions and caveats, the comps
 * table (target and peers, each value with its status) and peer statistics (peers only, NM / INCOMPLETE /
 * missing excluded and counted). Filings: GET /api/company/<cik>/filings with a form filter.
 */
import { useEffect, useState, type FormEvent } from 'react';
import { apiPath, useApi } from '../research-common/api';
import { humanize, shownOf } from '../research-common/format';
import { ErrorNotice, ExtLink, Flags, IsoTime, KV, Loading, Section, StatusTag, Unavailable, UnitValue } from '../research-common/parts';
import { IssuerFrame, TextField, type IssuerContext } from './CompanyWorkspace';
import { PriceLine } from './Metrics';
import { isUnavailable, issuerHref, parsePeerList, priceParams, type CompanyOptions, type Comps, type FilingsContent, type FilingsResponse, type PeerGroup, type PeersResponse, type UnavailableBody } from './model';

export function PeersView({ ctx }: { ctx: IssuerContext }) {
  const q = useApi<PeersResponse | UnavailableBody>(apiPath('company', ctx.cik, 'peers'), { as_of: ctx.opts.as_of, peers: ctx.opts.peers, ...priceParams(ctx.opts) });
  const [peers, setPeers] = useState(ctx.opts.peers ?? '');
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setPeers(ctx.opts.peers ?? ''), [ctx.opts.peers]);
  const apply = (e: FormEvent) => {
    e.preventDefault();
    if (!peers.trim()) {
      setErr(null);
      return ctx.go({ opts: { ...ctx.opts, peers: '' } });
    }
    const p = parsePeerList(peers);
    if (!p.ok) return setErr(p.error);
    setErr(null);
    ctx.go({ opts: { ...ctx.opts, peers: p.value } });
  };
  const data = q.data && !isUnavailable(q.data) ? q.data : null;
  return (
    <IssuerFrame ctx={ctx} queries={[q]}>
      <form className="rx-toolbar" onSubmit={apply} aria-label="Peer selection">
        <TextField id="peers-list" label="Explicit peers (CIKs, comma-separated; empty = rule)" value={peers} onChange={setPeers} placeholder="9000002, 9000003" width={340} />
        <button type="submit" className="btn">
          Apply
        </button>
        {err ? (
          <span className="form-error" role="alert">
            {err}
          </span>
        ) : null}
      </form>
      <ErrorNotice what="Peers" error={q.error} />
      {q.data && isUnavailable(q.data) ? <Unavailable what="Peers" reason={q.data.reason} /> : null}
      {!q.data && !q.error ? <Loading what="peers" /> : null}
      {data ? (
        <>
          <PeerGroupPanel g={data.group} opts={ctx.opts} />
          <CompsPanel comps={data.comps} target={data.group.target.entity_id} names={new Map([data.group.target, ...data.group.members].map((m) => [m.entity_id, m.name]))} />
        </>
      ) : null}
    </IssuerFrame>
  );
}

/** The peer rule and members; a member opens at the same point in time (`opts.as_of`), never with this issuer's price. */
export function PeerGroupPanel({ g, opts }: { g: PeerGroup; opts: CompanyOptions }) {
  return (
    <Section title={`Peer group · ${g.rule.id}`}>
      <KV
        rows={[
          ['Rule', g.rule.description],
          ['SIC', <span className="mono">{g.rule.sic ?? 'none'}</span>],
          ['Size band', g.rule.size_band],
          ['Target', `${g.target.name} (CIK ${g.target.cik})`],
          [
            'Members',
            g.members.length ? (
              <ul className="rx-tree-list">
                {g.members.map((m) => (
                  <li key={m.entity_id}>
                    <a href={issuerHref(m.cik, opts)}>{m.name}</a> <span className="muted mono">CIK {m.cik} · SIC {m.sic ?? '—'}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="muted">no members</span>
            ),
          ],
          g.excluded.length
            ? ['Excluded', <ul className="rx-tree-list">{g.excluded.map((x) => <li key={x.entity_id}><span className="mono">{x.entity_id}</span>: {x.reason}</li>)}</ul>]
            : null,
          ['As of', <IsoTime iso={g.as_of} />],
        ]}
      />
      {g.caveats.length ? (
        <>
          <h3 className="rx-h3">Caveats</h3>
          <ul>
            {g.caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </>
      ) : null}
    </Section>
  );
}

function CompsPanel({ comps, target, names }: { comps: Comps; target: string; names: Map<string, string> }) {
  const methods = [...new Set(Object.values(comps.peer_statistics).map((s) => s.method).filter((m): m is string => !!m))];
  const uniformMethod = methods.length === 1 ? methods[0] : undefined;
  return (
    <Section title="Comparables" flush note={comps.note}>
      <div className="rx-table-wrap">
        <table className="rx-table">
          <caption>Comparable metrics as of {comps.as_of}; each value carries its status and unit</caption>
          <thead>
            <tr>
              <th scope="col">Issuer</th>
              <th scope="col">Role</th>
              {comps.metrics.map((m) => (
                <th key={m} scope="col" className="num">
                  {humanize(m)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {comps.rows.map((r) => (
              <tr key={r.entity_id} style={r.entity_id === target ? { background: 'var(--surface-2)' } : undefined}>
                <th scope="row">
                  {names.get(r.entity_id) ?? r.entity_id}
                  {r.fundamentals.status !== 'OK' ? (
                    <span className="detail">
                      {' '}
                      <StatusTag status={r.fundamentals.status} detail={r.fundamentals.reason} />
                    </span>
                  ) : null}
                  {r.price ? (
                    <span className="detail">
                      {' '}
                      <PriceLine p={r.price} />
                    </span>
                  ) : null}
                </th>
                <td>{r.role}</td>
                {comps.metrics.map((m) => {
                  const v = r.values[m];
                  if (!v) return <td key={m} className="num muted">—</td>;
                  return (
                    <td key={m} className="num" title={v.reason ?? `${v.formula} · ${v.period}`}>
                      {v.value !== null ? <UnitValue value={v.value} unit={v.unit} compact /> : null} {v.status !== 'OK' ? <StatusTag status={v.status} detail={v.reason ?? undefined} /> : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="rx-table-wrap">
        <table className="rx-table">
          <caption>Peer statistics (peers only; the target is excluded){uniformMethod ? `; ${uniformMethod}` : ''}</caption>
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col" className="num">Median</th>
              <th scope="col" className="num">P25</th>
              <th scope="col" className="num">P75</th>
              <th scope="col" className="num">Mean</th>
              <th scope="col" className="num">Used</th>
              <th scope="col" className="num">NM</th>
              <th scope="col" className="num">Missing</th>
              <th scope="col">Method</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(comps.peer_statistics).map(([m, s]) => {
              const unit = comps.rows.find((r) => r.values[m])?.values[m]?.unit ?? '';
              return (
                <tr key={m}>
                  <th scope="row">{humanize(m)}</th>
                  {s.suppressed ? (
                    <td colSpan={4} className="wrap">
                      <StatusTag status="SUPPRESSED" /> <span className="rx-reason">{s.reason}</span>
                    </td>
                  ) : (
                    <>
                      <td className="num">
                        <UnitValue value={s.median} unit={unit} compact />
                      </td>
                      <td className="num">
                        <UnitValue value={s.p25} unit={unit} compact />
                      </td>
                      <td className="num">
                        <UnitValue value={s.p75} unit={unit} compact />
                      </td>
                      <td className="num">
                        <UnitValue value={s.mean} unit={unit} compact />
                      </td>
                    </>
                  )}
                  <td className="num">{s.n_used}</td>
                  <td className="num">{s.n_nm}</td>
                  <td className="num">{s.n_missing}</td>
                  <td className="muted">{s.suppressed ? '' : s.method === uniformMethod ? '' : (s.method ?? '')}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ------------------------------------------------------------------ filings

export function FilingsView({ ctx }: { ctx: IssuerContext }) {
  const q = useApi<FilingsResponse | UnavailableBody>(apiPath('company', ctx.cik, 'filings'), { as_of: ctx.opts.as_of, forms: ctx.opts.forms, limit: 200 });
  const [forms, setForms] = useState(ctx.opts.forms ?? '');
  useEffect(() => setForms(ctx.opts.forms ?? ''), [ctx.opts.forms]);
  const data = q.data && !isUnavailable(q.data) ? q.data : null;
  return (
    <IssuerFrame ctx={ctx} queries={[q]}>
      <form
        className="rx-toolbar"
        aria-label="Filing filter"
        onSubmit={(e) => {
          e.preventDefault();
          ctx.go({ opts: { ...ctx.opts, forms: forms.split(',').map((f) => f.trim()).filter(Boolean).join(',') } });
        }}
      >
        <TextField id="filings-forms" label="Forms (comma-separated; empty = all)" value={forms} onChange={setForms} placeholder="10-K, 10-K/A, 10-Q" width={300} />
        <button type="submit" className="btn">
          Apply
        </button>
      </form>
      <ErrorNotice what="Filings" error={q.error} />
      {q.data && isUnavailable(q.data) ? <Unavailable what="Filings" reason={q.data.reason} /> : null}
      {!q.data && !q.error ? <Loading what="filings" /> : null}
      {data ? <FilingsTable f={data} /> : null}
    </IssuerFrame>
  );
}

export function FilingsTable({ f, title = 'Filings' }: { f: FilingsContent; title?: string }) {
  return (
    <Section
      title={`${title} · ${shownOf(f.filings.length, f.count)} accepted by as of`}
      flush
      note={
        <>
          {f.not_yet_accepted_at_as_of ? `${f.not_yet_accepted_at_as_of} later filing(s) not yet accepted at as of are hidden. ` : ''}
          {f.coverage_note} {f.acceptance_note}
        </>
      }
    >
      <div className="rx-table-wrap" style={{ maxHeight: 'min(60vh, 560px)' }}>
        <table className="rx-table">
          <caption>SEC filings accepted at or before {f.as_of}</caption>
          <thead>
            <tr>
              <th scope="col">Form</th>
              <th scope="col">Filed</th>
              <th scope="col">Accepted (as published)</th>
              <th scope="col">Accession</th>
              <th scope="col">Items / flags</th>
              <th scope="col">Document</th>
            </tr>
          </thead>
          <tbody>
            {f.filings.map((x) => (
              <tr key={x.accession}>
                <th scope="row" className="mono">
                  {x.form}
                </th>
                <td className="mono">{x.filing_date}</td>
                <td className="mono" title="acceptanceDateTime exactly as the SEC submissions file publishes it">
                  {x.acceptance_time_raw}
                </td>
                <td>
                  <ExtLink href={x.links.filing_index}>{x.accession}</ExtLink>
                </td>
                <td className="wrap">
                  {x.items.length ? <span className="mono">{x.items.join(', ')}</span> : null} <Flags flags={x.flags} />
                </td>
                <td>
                  <ExtLink href={x.links.primary_document}>{x.primary_document_description || 'primary document'}</ExtLink>
                  {x.is_xbrl ? <span className="muted"> · XBRL</span> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {f.filings.length === 0 ? <p className="empty">No filings accepted by the as-of time.</p> : null}
    </Section>
  );
}
