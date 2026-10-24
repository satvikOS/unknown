/**
 * Company research (CAP-CO-01/02/05/07/10/11/12, CAP-INS-01): issuer search, then per issuer a tearsheet,
 * point-in-time statements with coverage, metrics with their MISSING / INCOMPLETE reasons verbatim, a DCF
 * with its recipe, peers and filings. Everything is read from GET /api/company (read-only research store);
 * an unavailable source shows the API's reason and the command that would populate it.
 *
 * Routes: `#/company` (search), `#/company/search/q=…/as_of=…`, `#/company/<cik>/<tab>/as_of=…/price=…`,
 * `#/company/eu/<LEI>/<tab>/as_of=…/sec=<CIK>` (EU issuer from ESEF filings, see EuIssuer.tsx).
 */
import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import type { QueryResult } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge } from '../../ui/primitives';
import { WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { RouteTabs, TabPanel } from '../operations/parts';
import { apiPath, useApi } from '../research-common/api';
import { POPULATE, checkInstant, shownOf } from '../research-common/format';
import { ErrorNotice, IsoTime, Loading, Section, StatusTag, Unavailable } from '../research-common/parts';
import type { WorkspaceProps } from '../types';
import { DcfView } from './Dcf';
import { euIssuerHref, parseEuRoute } from './eu';
import { EuIssuerView } from './EuIssuer';
import { MetricsView } from './Metrics';
import {
  COMPANY_TABS,
  TAB_LABELS,
  carriedOptions,
  companyRoute,
  isUnavailable,
  issuerHref,
  parseCompanyRoute,
  priceProblems,
  searchedIssuers,
  type CompanyOptions,
  type CompanyRoute,
  type CompanyTab,
  type FilingsResponse,
  type PriceForm,
  type SearchCandidate,
  type SearchResponse,
  type UnavailableBody,
} from './model';
import { FilingsView, PeersView } from './Peers';
import { StatementsView } from './Statements';
import { TearsheetView } from './Tearsheet';
import { NoValue } from '../../ui/unavailable';

const DESCRIPTION =
  'Issuers from SEC filings and EU ESEF annual reports in the research store: point-in-time statements, metrics, DCF, peers and tearsheet.';

export function CompanyWorkspace({ route, navigate }: WorkspaceProps) {
  const eu = parseEuRoute(route.rest);
  if (eu) return <EuIssuerView key={eu.lei} r={eu} navigate={navigate} />;
  const r = parseCompanyRoute(route.rest);
  const go = (next: { cik?: number | null; tab?: CompanyTab; opts?: CompanyOptions }) =>
    navigate('company', ...companyRoute({ cik: next.cik === undefined ? r.cik : next.cik, tab: next.tab ?? r.tab, opts: next.opts ?? r.opts }));
  if (r.cik === null) return <SearchView r={r} go={go} navigate={navigate} />;
  return <IssuerView key={r.cik} r={r} go={go} />;
}

type Go = (next: { cik?: number | null; tab?: CompanyTab; opts?: CompanyOptions }) => void;

// ------------------------------------------------------------------ search

function SearchView({ r, go, navigate }: { r: CompanyRoute; go: Go; navigate: WorkspaceProps['navigate'] }) {
  const [q, setQ] = useState(r.opts.q ?? '');
  const [asOf, setAsOf] = useState(r.opts.as_of ?? '');
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setQ(r.opts.q ?? '');
    setAsOf(r.opts.as_of ?? '');
  }, [r.opts.q, r.opts.as_of]);
  const query = r.opts.q ?? '';
  // The search also matches the core's instrument master (search.py core_conn), so a new core seq refetches it.
  const search = useApi<SearchResponse>(query ? '/api/company/search' : null, { q: query, as_of: r.opts.as_of, limit: 50 }, { seqDriven: true });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const t = q.trim();
    if (!t) return setError('Enter a name, ticker, CIK, ISIN or FIGI.');
    if (t.length > 100) return setError('The query must be 1-100 characters.');
    const a = checkInstant(asOf);
    if (!a.ok) return setError(a.error);
    setError(null);
    go({ cik: null, opts: { q: t, as_of: a.value } });
  };

  const open = (c: SearchCandidate) => {
    if (c.kind === 'issuer') go({ cik: c.cik, tab: 'tearsheet', opts: carriedOptions(r.opts) });
    else if (c.kind === 'eu_issuer') navigate('company', 'eu', c.lei, 'statements', ...(r.opts.as_of ? [`as_of=${r.opts.as_of}`] : []));
    else if (c.kind === 'eu_instrument' && c.issuer_lei) navigate('company', 'eu', c.issuer_lei, 'filings');
    else if (c.kind === 'instrument') navigate('instruments', c.instrument_id);
  };

  return (
    <WorkspaceFrame title="Company" description={DESCRIPTION} queries={search.key ? [search] : []}>
      <form className="rx-toolbar" onSubmit={submit} role="search" aria-label="Issuer search">
        <div className="field" style={{ minWidth: 260, flex: '1 1 260px' }}>
          <label htmlFor="company-q">Name, ticker, CIK, ISIN or FIGI</label>
          <input id="company-q" className="input" value={q} onChange={(e) => setQ(e.target.value)} autoComplete="off" placeholder="e.g. Apple, AAPL, 320193" />
        </div>
        <AsOfField id="company-search-asof" value={asOf} onChange={setAsOf} />
        <button type="submit" className="btn primary">
          Search
        </button>
        {error ? (
          <span className="form-error" role="alert">
            {error}
          </span>
        ) : null}
      </form>
      {!query ? (
        <p className="muted">
          Search the issuers ingested into the research store, the SEC ticker snapshot, stored OpenFIGI mappings and the core instrument master. Candidates are ranked,
          never selected for you.
        </p>
      ) : null}
      <ErrorNotice what="Search" error={search.error} />
      {query && search.status === 'loading' ? <Loading what="search results" /> : null}
      {search.data ? <SearchResults data={search.data} open={open} opts={r.opts} /> : null}
    </WorkspaceFrame>
  );
}

function candidateName(c: SearchCandidate): string {
  return c.name ?? (c.kind === 'figi_listing' ? (c.ticker ?? c.figi ?? 'unnamed') : c.kind === 'eu_instrument' ? c.isin : 'unnamed');
}

function candidateId(c: SearchCandidate): string {
  if (c.kind === 'eu_issuer') return `LEI ${c.lei}`;
  if (c.kind === 'eu_instrument') return `${c.isin} · ${c.mic}`;
  return c.kind === 'issuer' ? `CIK ${c.cik}` : c.kind === 'instrument' ? c.instrument_id : (c.figi ?? c.composite_figi ?? 'no FIGI');
}

function SearchResults({ data, open, opts }: { data: SearchResponse; open: (c: SearchCandidate) => void; opts: CompanyOptions }) {
  const columns: Column<SearchCandidate>[] = [
    {
      id: 'name',
      header: 'Name',
      wrap: true,
      sortValue: candidateName,
      cell: (c) =>
        c.kind === 'issuer' ? (
          <a href={issuerHref(c.cik, opts)} tabIndex={-1}>
            <strong>{c.name}</strong>
          </a>
        ) : c.kind === 'instrument' ? (
          <WorkspaceLink to="instruments" rest={[c.instrument_id]}>
            <strong>{candidateName(c)}</strong>
          </WorkspaceLink>
        ) : c.kind === 'eu_issuer' ? (
          <a href={euIssuerHref(c.lei, opts.as_of)} tabIndex={-1}>
            <strong>{c.name}</strong>
          </a>
        ) : (
          <strong>{candidateName(c)}</strong>
        ),
    },
    {
      id: 'source',
      header: 'Source · currency',
      description: 'Where the issuer comes from (SEC EDGAR or ESEF annual reports) and its reporting currency when known',
      sortValue: (c) => sourceLabel(c),
      cell: (c) => <span>{sourceLabel(c)}</span>,
    },
    { id: 'kind', header: 'Kind', sortValue: (c) => c.kind, cell: (c) => <Badge>{c.kind.replace('_', ' ')}</Badge> },
    { id: 'id', header: 'Identifier', sortValue: candidateId, cell: (c) => <span className="mono">{candidateId(c)}</span> },
    { id: 'match', header: 'Match', description: 'EXACT_ID < EXACT_TICKER < NAME_PREFIX < NAME_CONTAINS < FORMER_NAME', sortValue: (c) => c.match, cell: (c) => <span className="mono">{c.match}</span> },
    {
      id: 'detail',
      header: 'Listings / detail',
      wrap: true,
      cell: (c) =>
        c.kind === 'issuer' ? (
          <span>
            {c.listings.length ? c.listings.map((l) => `${l.ticker} (${l.exchange}, ${l.source}; ${l.validity})`).join('; ') : <span className="muted">no listings recorded</span>}
            {c.former_name_matches?.length ? <span className="muted"> · former name: {c.former_name_matches.join(', ')}</span> : null}
          </span>
        ) : c.kind === 'eu_issuer' ? (
          <span>
            {c.listings.length ? c.listings.map((l) => `${l.isin} (${l.mic}, ${l.currency ?? 'no currency'}, ${l.source})`).join('; ') : <span className="muted">no FIRDS listing held</span>}
            {c.reason ? <span className="muted"> · {c.reason}</span> : null}
          </span>
        ) : c.kind === 'eu_instrument' ? (
          <span>
            {c.mic} · CFI {c.cfi ?? <NoValue reason="NOT_REPORTED" />} · {c.currency ?? 'no currency'} · issuer {c.issuer_lei ?? 'unknown'}
          </span>
        ) : c.kind === 'instrument' ? (
          <span>
            {c.symbol} · {c.asset_class ?? 'class not set'} · {c.venue_mic ?? 'no venue'} · {c.currency ?? 'no currency'} · {c.status ?? 'status not set'}
          </span>
        ) : (
          <span>
            {c.ticker ?? <NoValue reason="NOT_REPORTED" />} {c.exch_code ?? ''} · {c.security_type ?? <NoValue reason="NOT_REPORTED" />} · {c.mapping_status} · <span className="muted">{c.status_note}</span>
          </span>
        ),
    },
    {
      id: 'store',
      header: 'Research store',
      cell: (c) =>
        c.kind === 'issuer' ? (
          c.ingested ? (
            <StatusTag status="INGESTED" detail={`SIC ${c.sic ?? 'unknown'}`} />
          ) : (
            <span className="muted" title={`not ingested: python -m unknown.data.sec ingest --cik ${c.cik}`}>
              not ingested
            </span>
          )
        ) : c.kind === 'eu_issuer' ? (
          <StatusTag status={c.esef_status ?? 'UNKNOWN'} detail={`${c.filings} ESEF filing(s)`} />
        ) : (
          <span className="muted">{c.source}</span>
        ),
    },
  ];
  return (
    <>
      {searchedIssuers(data) ? null : (
        <Unavailable
          what="SEC issuers"
          status="NOT_SEARCHED"
          reason="The API searched no SEC issuer source (it lists only the sources below), which is what it does when the research store has not been initialised. An issuer that is not listed here may still exist."
          commands={[POPULATE.sec]}
        />
      )}
      <Section title={`Candidates · ${shownOf(data.candidates.length, data.total_matches)}`} flush note={data.note}>
        <DataTable
          caption={`Search results for ${data.query} (${data.query_type}) as of ${data.as_of}`}
          captionHidden
          columns={columns}
          rows={data.candidates}
          rowKey={(c) => `${c.kind}:${candidateId(c)}`}
          onRowActivate={open}
          activateLabel="open the issuer"
          emptyText={`No candidates for "${data.query}" at ${data.as_of}.`}
          maxHeight="min(55vh, 520px)"
        />
      </Section>
      <Section title="Sources searched">
        <ul className="ops-checks">
          {data.sources.map((s) => (
            <li key={s.source}>
              <span className="label mono">{s.source}</span>
              <span>
                <StatusTag status={s.status} />
                {s.matched !== undefined ? <span className="muted"> {s.matched} matched</span> : null}
                {s.rows_matched !== undefined ? <span className="muted"> {s.rows_matched} rows matched</span> : null}
                {s.retrieved_at ? (
                  <span className="muted">
                    {' '}
                    · retrieved <IsoTime iso={s.retrieved_at} />
                  </span>
                ) : null}
              </span>
              {s.reason ? <span className="detail">{s.reason}</span> : null}
            </li>
          ))}
        </ul>
        <p className="rx-hint">
          Query type <span className="mono">{data.query_type}</span>, as of <IsoTime iso={data.as_of} />. Issuers that are not ingested can be added with{' '}
          <code className="mono rx-command">python -m unknown.data.sec ingest --cik &lt;CIK&gt;</code>.
        </p>
      </Section>
    </>
  );
}

/** Source and currency label of a candidate: SEC and ESEF issuers sit side by side in one list. */
export function sourceLabel(c: SearchCandidate): string {
  if (c.kind === 'eu_issuer') return `${c.source_label} · ${c.reporting_currency ?? 'currency not yet known'}`;
  if (c.kind === 'issuer') return c.source_label ?? 'SEC EDGAR (US filer)';
  if (c.kind === 'eu_instrument') return `ESMA FIRDS · ${c.currency ?? 'no currency'}`;
  if (c.kind === 'instrument') return `core instrument master · ${c.currency ?? 'no currency'}`;
  return c.source;
}

// ------------------------------------------------------------------ issuer frame

export function AsOfField({ id, value, onChange, label = 'As of' }: { id: string; value: string; onChange: (v: string) => void; label?: string }) {
  return (
    <div className="field" style={{ width: 250 }}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className="input mono"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="now, 2026-08-15 or …T20:00:00Z"
        autoComplete="off"
        aria-describedby={`${id}-hint`}
      />
      <span id={`${id}-hint`} className="sr-only">
        Empty means now. A date means the end of that UTC day; an instant needs a time zone.
      </span>
    </div>
  );
}

export interface IssuerContext {
  cik: number;
  tab: CompanyTab;
  opts: CompanyOptions;
  go: Go;
  /** Identity strip, as-of and price inputs, and the tab bar: rendered by each tab inside its frame. */
  head: ReactNode;
  identity: QueryResult<FilingsResponse | UnavailableBody>;
}

function IssuerView({ r, go }: { r: CompanyRoute; go: Go }) {
  const cik = r.cik!;
  const identity = useApi<FilingsResponse | UnavailableBody>(apiPath('company', cik, 'filings'), { as_of: r.opts.as_of, limit: 1 });
  const head = (
    <>
      <IssuerStrip cik={cik} q={identity} />
      <IssuerControls r={r} go={go} />
      <RouteTabs
        idBase="company"
        label="Company views"
        tabs={COMPANY_TABS.map((t) => ({ id: t, label: TAB_LABELS[t] }))}
        current={r.tab}
        onSelect={(t) => go({ tab: t })}
      />
    </>
  );
  const ctx: IssuerContext = { cik, tab: r.tab, opts: r.opts, go, head, identity };
  switch (r.tab) {
    case 'statements':
      return <StatementsView ctx={ctx} />;
    case 'metrics':
      return <MetricsView ctx={ctx} />;
    case 'dcf':
      return <DcfView ctx={ctx} />;
    case 'peers':
      return <PeersView ctx={ctx} />;
    case 'filings':
      return <FilingsView ctx={ctx} />;
    default:
      return <TearsheetView ctx={ctx} />;
  }
}

/** The frame every issuer tab renders: header provenance covers the identity query and the tab's own queries. */
export function IssuerFrame({ ctx, queries, children }: { ctx: IssuerContext; queries: readonly QueryResult<unknown>[]; children: ReactNode }) {
  return (
    <WorkspaceFrame title="Company" description={DESCRIPTION} queries={[ctx.identity, ...queries]}>
      {ctx.head}
      <TabPanel idBase="company" current={ctx.tab}>
        {children}
      </TabPanel>
    </WorkspaceFrame>
  );
}

function IssuerStrip({ cik, q }: { cik: number; q: QueryResult<FilingsResponse | UnavailableBody> }) {
  if (q.error) return <ErrorNotice what={`Issuer CIK ${cik}`} error={q.error} />;
  if (!q.data) return <p className="rx-identity muted">CIK {cik}: loading identity…</p>;
  if (isUnavailable(q.data)) {
    return (
      <p className="rx-identity">
        <span className="rx-name">CIK {cik}</span>
        <StatusTag status="UNAVAILABLE" /> <span className="muted">identity not available:</span> <span className="rx-reason">{q.data.reason}</span>
        <a href="#/company">Search issuers</a>
      </p>
    );
  }
  const id = q.data.identity;
  return (
    <div className="rx-identity">
      <span className="rx-name">{id.name_as_of || id.name}</span>
      <span className="mono">CIK {id.cik}</span>
      <span className="mono muted">{id.entity_id}</span>
      {id.sic ? (
        <span className="muted">
          SIC {id.sic}
          {id.sic_description ? ` (${id.sic_description})` : ''}
        </span>
      ) : null}
      {id.listings.length ? <span className="muted">{id.listings.map((l) => `${l.ticker} · ${l.exchange}`).join(', ')}</span> : null}
      {id.fiscal_year_end ? <span className="muted">FYE {id.fiscal_year_end}</span> : null}
      <a href="#/company">Search issuers</a>
    </div>
  );
}

function IssuerControls({ r, go }: { r: CompanyRoute; go: Go }) {
  const [asOf, setAsOf] = useState(r.opts.as_of ?? '');
  const [price, setPrice] = useState<PriceForm>({
    price: r.opts.price ?? '',
    price_currency: r.opts.price_currency ?? 'USD',
    price_source: r.opts.price_source ?? '',
    price_time: r.opts.price_time ?? '',
  });
  const [errors, setErrors] = useState<string[]>([]);
  useEffect(() => {
    setAsOf(r.opts.as_of ?? '');
    setPrice({ price: r.opts.price ?? '', price_currency: r.opts.price_currency ?? 'USD', price_source: r.opts.price_source ?? '', price_time: r.opts.price_time ?? '' });
  }, [r.opts.as_of, r.opts.price, r.opts.price_currency, r.opts.price_source, r.opts.price_time]);

  const apply = (e: FormEvent) => {
    e.preventDefault();
    const problems: string[] = [];
    const a = checkInstant(asOf);
    if (!a.ok) problems.push(a.error);
    problems.push(...priceProblems(price));
    if (price.price.trim() && price.price_time.trim()) {
      const t = checkInstant(price.price_time, 'price time');
      if (!t.ok) problems.push(t.error);
    }
    setErrors(problems);
    if (problems.length) return;
    const has = !!price.price.trim();
    go({
      opts: {
        ...r.opts,
        as_of: a.ok ? a.value : '',
        price: has ? price.price.trim() : '',
        price_currency: has ? price.price_currency.trim().toUpperCase() : '',
        price_source: has ? price.price_source.trim() : '',
        price_time: has ? price.price_time.trim() : '',
      },
    });
  };
  const set = (k: keyof PriceForm) => (v: string) => setPrice((p) => ({ ...p, [k]: v }));
  const priced = !!r.opts.price;
  return (
    <form className="rx-toolbar" onSubmit={apply} aria-label="Point in time and price input">
      <AsOfField id="company-asof" value={asOf} onChange={setAsOf} />
      <details className="rx-details" open={priced || undefined} style={{ flex: '1 1 100%' }}>
        <summary>
          Price input for multiples {priced ? <Badge tone="info">{`${r.opts.price} ${r.opts.price_currency ?? ''} · ${r.opts.price_source ?? ''}`}</Badge> : <span className="muted">(none: price-based metrics stay NO_PRICE)</span>}
        </summary>
        <div className="rx-toolbar" style={{ marginTop: 6 }}>
          <TextField id="company-price" label="Price" value={price.price} onChange={set('price')} placeholder="50.00" width={110} />
          <TextField id="company-price-ccy" label="Currency" value={price.price_currency} onChange={set('price_currency')} placeholder="USD" width={80} />
          <TextField id="company-price-source" label="Source (required)" value={price.price_source} onChange={set('price_source')} placeholder="where it was observed" width={220} />
          <TextField id="company-price-time" label="Observed at (≤ as of)" value={price.price_time} onChange={set('price_time')} placeholder="2026-10-21T20:00:00Z" width={220} />
        </div>
        <p className="rx-hint">No price is ever fetched implicitly: the API needs the value, its currency, its source and the time it was observed (not after as of).</p>
      </details>
      <div className="rx-form-actions" style={{ marginTop: 0 }}>
        <button type="submit" className="btn primary">
          Apply
        </button>
        {r.opts.as_of || priced ? (
          <button
            type="button"
            className="btn"
            onClick={() => go({ opts: { ...r.opts, as_of: '', price: '', price_currency: '', price_source: '', price_time: '' } })}
          >
            Now, no price
          </button>
        ) : null}
        <span className="rx-hint">
          Point in time: {r.opts.as_of ? <span className="mono">{r.opts.as_of}</span> : 'now'} (only data available at that time is used)
        </span>
      </div>
      {errors.length ? (
        <ul className="form-error" role="alert" style={{ flex: '1 1 100%', margin: 0 }}>
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      ) : null}
    </form>
  );
}

export function TextField({
  id,
  label,
  value,
  onChange,
  placeholder,
  width,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  width?: number;
  hint?: string;
}) {
  return (
    <div className="field" style={width ? { width } : undefined}>
      <label htmlFor={id}>{label}</label>
      <input id={id} className="input mono" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} autoComplete="off" title={hint} />
    </div>
  );
}
