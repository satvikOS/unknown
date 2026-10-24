/**
 * News & filings (CAP-NEWS-02/05/06/08/13): the live tape of SEC EDGAR filings and official regulator and
 * central-bank releases from the news store (GET /api/news), new items pushed by GET /api/news/stream and flashed
 * on arrival, filters and full-text search, top stories (/api/news/top), company news for the linked security
 * (CN), alerts for the monitor workspace's watchlist issuers, and a reader pane (/api/news/item) with the feed's
 * summary, typed 8-K items, tagged issuers with their identifier as-of time, the story's other copies and the link
 * to the filing index or the original release. Every row names its source and the time it was first seen.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Badge, Timestamp } from '../../ui/primitives';
import { useLinkedSecurity } from '../../ui/link';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { ExportButton } from '../../ui/ExportButton';
import { newsExport } from './exportTables';
import { useApi } from '../research-common/api';
import { Commands, ErrorNotice, ExtLink, Hash, KV, Loading, Section, Unavailable } from '../research-common/parts';
import { STORAGE_KEY as MONITOR_STORAGE_KEY } from '../monitor/model';
import type { WorkspaceProps } from '../types';
import {
  SINCE_CHOICES,
  filtered,
  formLabel,
  isNewsUnavailable,
  issuerLabel,
  listParams,
  mergeLive,
  newsRoute,
  parseNewsRoute,
  publishedLabel,
  shortTime,
  sourceName,
  splitSnippet,
  verdictTone,
  watchlistIssuers,
  type ItemDetail,
  type NewsItem,
  type NewsListResponse,
  type NewsOptions,
  type NewsSourceRow,
  type NewsUnavailable,
  type TopResponse,
} from './model';
import './news.css';
import { NoValue } from '../../ui/unavailable';

type StreamState = 'connecting' | 'live' | 'reconnecting' | 'no-store';
interface Alert {
  item: NewsItem;
  issuers: number[];
}

function readWatchlists(): string[] {
  try {
    return watchlistIssuers(window.localStorage.getItem(MONITOR_STORAGE_KEY));
  } catch {
    return [];
  }
}

function useNewsStream(enabled: boolean, issuers: readonly string[], lastId: number | null, onNews: (i: NewsItem) => void, onAlert: (a: Alert) => void) {
  const [state, setState] = useState<StreamState>('connecting');
  const [unresolved, setUnresolved] = useState<string[]>([]);
  const [watching, setWatching] = useState(0);
  const news = useRef(onNews);
  const alert = useRef(onAlert);
  news.current = onNews;
  alert.current = onAlert;
  const key = issuers.join(',');
  useEffect(() => {
    if (!enabled || typeof EventSource === 'undefined') return;
    const qs = new URLSearchParams();
    if (key) qs.set('issuers', key);
    if (lastId !== null) qs.set('last_event_id', String(lastId));
    const es = new EventSource(`/api/news/stream${qs.toString() ? `?${qs}` : ''}`, { withCredentials: true });
    setState('connecting');
    es.addEventListener('status', (e) => {
      const s = JSON.parse((e as MessageEvent<string>).data) as { store: string; unresolved: string[]; watching: unknown[] };
      setState(s.store === 'AVAILABLE' ? 'live' : 'no-store');
      setUnresolved(s.unresolved);
      setWatching(s.watching.length);
    });
    es.addEventListener('news', (e) => news.current(JSON.parse((e as MessageEvent<string>).data) as NewsItem));
    es.addEventListener('alert', (e) => alert.current(JSON.parse((e as MessageEvent<string>).data) as Alert));
    es.onerror = () => setState('reconnecting');
    return () => es.close();
  }, [enabled, key, lastId]);
  return { state, unresolved, watching };
}

export function NewsWorkspace({ route, navigate }: WorkspaceProps) {
  const o = parseNewsRoute(route.rest);
  const linked = useLinkedSecurity();
  const go = (next: NewsOptions) => navigate('news', ...newsRoute(next));
  const params = o.view === 'top' ? null : listParams(o, linked);
  const list = useApi<NewsListResponse | NewsUnavailable>(params ? '/api/news' : null, params ?? undefined);
  const top = useApi<TopResponse | NewsUnavailable>(o.view === 'top' ? '/api/news/top' : null, { hours: 72 });
  const meta = useApi<NewsListResponse | NewsUnavailable>(o.view === 'top' ? '/api/news' : null, { limit: 1 });
  const [selected, setSelected] = useState<number | null>(null);
  const detail = useApi<{ as_of: string; item: ItemDetail } | NewsUnavailable>(selected ? '/api/news/item' : null, { id: selected });
  const [live, setLive] = useState<NewsItem[]>([]);
  const [flash, setFlash] = useState<ReadonlySet<number>>(new Set());
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [issuers] = useState(readWatchlists);
  const listData = list.data && !isNewsUnavailable(list.data) ? list.data : null;
  const [startId, setStartId] = useState<number | null>(null);
  useEffect(() => {
    if (listData && startId === null) setStartId(listData.latest_id);
  }, [listData, startId]);
  const refetchTimer = useRef<number | null>(null);
  const isFiltered = filtered(o);
  const refetch = list.refetch;

  const onNews = useCallback(
    (item: NewsItem) => {
      setFlash((s) => new Set([...s, item.id]));
      window.setTimeout(() => setFlash((s) => new Set([...s].filter((x) => x !== item.id))), 1600);
      if (isFiltered) {
        if (refetchTimer.current === null) {
          refetchTimer.current = window.setTimeout(() => {
            refetchTimer.current = null;
            refetch();
          }, 800);
        }
      } else {
        setLive((cur) => mergeLive(cur, [item], 300));
      }
    },
    [isFiltered, refetch],
  );
  const onAlert = useCallback((a: Alert) => setAlerts((cur) => [a, ...cur.filter((x) => x.item.id !== a.item.id)].slice(0, 20)), []);
  const stream = useNewsStream(Boolean(list.data || top.data), issuers, startId, onNews, onAlert);
  useEffect(() => () => {
    if (refetchTimer.current !== null) window.clearTimeout(refetchTimer.current);
  }, []);
  useEffect(() => setLive([]), [route.rest.join('/')]);

  const rows = useMemo(() => (listData ? (isFiltered ? listData.items : mergeLive(listData.items, live)) : []), [listData, live, isFiltered]);
  const sources = listData?.sources ?? (meta.data && !isNewsUnavailable(meta.data) ? meta.data.sources : undefined) ?? (list.data as NewsUnavailable | undefined)?.sources;
  const unavailable = [list.data, top.data].find(isNewsUnavailable);

  return (
    <WorkspaceFrame
      title="News & filings"
      description="SEC EDGAR filings and official regulator and central-bank releases, live from the news poller; each row shows its source and when it was first seen."
      queries={[list, top, ...(selected ? [detail] : [])].filter((q) => q.key !== null)}
    >
      <Toolbar o={o} go={go} sources={sources} linked={linked} stream={stream} />
      <ErrorNotice what="News" error={list.error ?? top.error} />
      {unavailable ? (
        <Unavailable what="News store" reason={unavailable.reason} commands={unavailable.populate ?? []} />
      ) : null}
      {o.view === 'cn' && !linked ? (
        <p className="nw-hint">Company news follows the panel&apos;s linked security. Type one on the command line, for example AAPL.XNAS CN.</p>
      ) : null}
      {listData?.issuers?.unresolved.length ? (
        <p className="nw-hint">
          No SEC issuer found for {listData.issuers.unresolved.join(', ')} in the ticker snapshot
          {listData.issuers.identifiers?.as_of ? ` of ${listData.issuers.identifiers.as_of.slice(0, 10)}` : ''}.
        </p>
      ) : null}
      <div className="nw-layout">
        <div className="nw-main">
          {o.view === 'top' ? (
            <TopStories data={top.data && !isNewsUnavailable(top.data) ? top.data : null} sources={sources} onSelect={setSelected} selected={selected} />
          ) : (
            <>
              <ExportButton
                disabled={rows.length === 0}
                table={() =>
                  newsExport(rows, listData?.as_of ?? null, {
                    view: o.view,
                    source: o.source ?? 'all',
                    form: o.form ?? 'all',
                    issuer: o.issuer ?? (o.view === 'cn' ? (linked ?? 'none') : 'all'),
                    search: o.q ?? '(none)',
                  })
                }
              />
              <Tape rows={rows} sources={sources} flash={flash} selected={selected} onSelect={setSelected} loading={!list.data && !list.error && params !== null} />
            </>
          )}
        </div>
        <aside className="nw-side">
          <Alerts alerts={alerts} issuers={issuers.length} watching={stream.watching} unresolved={stream.unresolved.length} onSelect={setSelected} sources={sources} />
          <Reader detail={detail.data} error={detail.error} selected={selected} sources={sources} />
          <SourcesPanel sources={sources} />
        </aside>
      </div>
    </WorkspaceFrame>
  );
}

function Toolbar({ o, go, sources, linked, stream }: { o: NewsOptions; go: (n: NewsOptions) => void; sources?: NewsSourceRow[]; linked: string | null; stream: { state: StreamState } }) {
  const [form, setForm] = useState(o.form ?? '');
  const [issuer, setIssuer] = useState(o.issuer ?? '');
  const [q, setQ] = useState(o.q ?? '');
  useEffect(() => {
    setForm(o.form ?? '');
    setIssuer(o.issuer ?? '');
    setQ(o.q ?? '');
  }, [o.form, o.issuer, o.q]);
  const apply = (e: FormEvent) => {
    e.preventDefault();
    go({ ...o, form: form.trim() || undefined, issuer: issuer.trim() || undefined, q: q.trim() || undefined });
  };
  const tone = stream.state === 'live' ? 'ok' : stream.state === 'no-store' ? 'bad' : 'warn';
  const label = { live: 'Live', connecting: 'Connecting', reconnecting: 'Reconnecting', 'no-store': 'No news store' }[stream.state];
  return (
    <form className="nw-toolbar" onSubmit={apply} aria-label="News filters">
      <div className="nw-tabs" role="tablist" aria-label="View">
        {(['tape', 'top', 'cn'] as const).map((v) => (
          <button key={v} type="button" role="tab" aria-selected={o.view === v} className={`nw-tab${o.view === v ? ' nw-tab-on' : ''}`} onClick={() => go({ ...o, view: v })}>
            {v === 'tape' ? 'Tape' : v === 'top' ? 'Top stories' : `Company${linked ? ` · ${linked}` : ''}`}
          </button>
        ))}
      </div>
      <label>
        <span>Source</span>
        <select value={o.source ?? ''} onChange={(e) => go({ ...o, source: e.target.value || undefined })}>
          <option value="">All sources</option>
          {(sources ?? []).filter((s) => s.pollable).map((s) => (
            <option key={s.source_id} value={s.source_id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Form</span>
        <input value={form} onChange={(e) => setForm(e.target.value)} placeholder="8-K,10-Q" size={8} />
      </label>
      {o.view !== 'cn' ? (
        <label>
          <span>Issuer</span>
          <input value={issuer} onChange={(e) => setIssuer(e.target.value)} placeholder="Ticker or CIK" size={10} />
        </label>
      ) : null}
      <label>
        <span>Search</span>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder='"phrase", prefix*, AND OR NOT' size={22} />
      </label>
      <label>
        <span>Since</span>
        <select value={o.since ?? ''} onChange={(e) => go({ ...o, since: e.target.value || undefined })}>
          {SINCE_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" className="btn primary">
        Apply
      </button>
      <span className="nw-stream" title="GET /api/news/stream">
        <Badge tone={tone}>{label}</Badge>
      </span>
    </form>
  );
}

export function Tape({ rows, sources, flash, selected, onSelect, loading }: { rows: NewsItem[]; sources?: NewsSourceRow[]; flash: ReadonlySet<number>; selected: number | null; onSelect: (id: number) => void; loading: boolean }) {
  if (loading) return <Loading what="the news tape" />;
  if (!rows.length) return <p className="nw-hint">No items match. New items appear here as the poller stores them.</p>;
  return (
    <div className="nw-scroll">
      <table className="nw-table">
        <thead>
          <tr>
            <th scope="col">First seen (UTC)</th>
            <th scope="col">Source</th>
            <th scope="col">Issuer</th>
            <th scope="col">Form / items</th>
            <th scope="col">Headline</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.id}
              tabIndex={0}
              aria-selected={selected === r.id}
              className={`${flash.has(r.id) ? 'nw-flash' : ''}${selected === r.id ? ' nw-selected' : ''}${r.duplicate_of ? ' nw-dup' : ''}`}
              onClick={() => onSelect(r.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onSelect(r.id);
              }}
            >
              <td className="nw-time" title={`Published: ${publishedLabel(r)}`}>
                {shortTime(r.first_seen)}
              </td>
              <td>{sourceName(sources, r.source_id)}</td>
              <td className="nw-issuer">{issuerLabel(r)}</td>
              <td className="nw-form">{formLabel(r)}</td>
              <td className="nw-title">
                {r.title}
                {r.cluster && r.cluster.sources.length > 1 ? <span className="nw-copies"> · {r.cluster.sources.length} sources</span> : null}
                {r.duplicate_of ? <span className="nw-copies"> · copy</span> : null}
                {r.snippet ? (
                  <div className="nw-snippet">
                    {splitSnippet(r.snippet).map((p, i) => (p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
                  </div>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TopStories({ data, sources, onSelect, selected }: { data: TopResponse | null; sources?: NewsSourceRow[]; onSelect: (id: number) => void; selected: number | null }) {
  if (!data) return <Loading what="top stories" />;
  if (!data.stories.length) return <p className="nw-hint">No stories in the last {data.hours} hours.</p>;
  return (
    <Section title={`Top stories, last ${data.hours} h`}>
      <p className="nw-hint">Ranked by {data.ranking}. As of {data.as_of.slice(0, 19).replace('T', ' ')} UTC.</p>
      <div className="nw-scroll">
        <table className="nw-table">
          <thead>
            <tr>
              <th scope="col">Sources</th>
              <th scope="col">Copies</th>
              <th scope="col">First seen (UTC)</th>
              <th scope="col">Story</th>
            </tr>
          </thead>
          <tbody>
            {data.stories.map((s) => (
              <tr key={s.cluster_id} tabIndex={0} className={s.lead && selected === s.lead.id ? 'nw-selected' : ''} onClick={() => s.lead && onSelect(s.lead.id)}>
                <td className="nw-num">{s.sources.length}</td>
                <td className="nw-num">{s.copies}</td>
                <td className="nw-time">{s.first_seen.slice(0, 19).replace('T', ' ')}</td>
                <td className="nw-title">
                  {s.title}
                  <div className="nw-snippet">{s.sources.map((x) => sourceName(sources, x)).join(', ')}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Alerts({ alerts, issuers, watching, unresolved, onSelect, sources }: { alerts: Alert[]; issuers: number; watching: number; unresolved: number; onSelect: (id: number) => void; sources?: NewsSourceRow[] }) {
  return (
    <Section title="Watchlist alerts">
      <p className="nw-hint">
        {issuers
          ? `Watching ${watching} SEC issuer${watching === 1 ? '' : 's'} from the monitor watchlists${unresolved ? ` (${unresolved} keys have no SEC issuer)` : ''}. One alert per story; copies never alert.`
          : 'No monitor watchlists in this browser: add securities in the Monitor workspace to get alerts here.'}
      </p>
      {alerts.length ? (
        <ul className="nw-alerts">
          {alerts.map((a) => (
            <li key={a.item.id}>
              <button type="button" className="nw-linkbtn" onClick={() => onSelect(a.item.id)}>
                <span className="nw-time">{shortTime(a.item.first_seen)}</span> {issuerLabel(a.item)} · {formLabel(a.item)} · {a.item.title}
              </button>
              <span className="nw-src"> {sourceName(sources, a.item.source_id)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </Section>
  );
}

function Reader({ detail, error, selected, sources }: { detail: { as_of: string; item: ItemDetail } | NewsUnavailable | undefined; error: unknown; selected: number | null; sources?: NewsSourceRow[] }) {
  if (!selected) return <Section title="Reader"><p className="nw-hint">Select a row to read it.</p></Section>;
  if (error) return <ErrorNotice what="News item" error={error} />;
  if (!detail) return <Loading what="the item" />;
  if (isNewsUnavailable(detail)) return <Unavailable what="News item" reason={detail.reason} />;
  const it = detail.item;
  const src = sources?.find((s) => s.source_id === it.source_id);
  return (
    <Section title="Reader">
      <h3 className="nw-read-title">{it.title}</h3>
      <KV
        rows={[
          ['Source', <span key="s">{sourceName(sources, it.source_id)} {src ? <Badge tone={verdictTone(src.verdict)}>{src.verdict.replace(/_/g, ' ')}</Badge> : null}</span>],
          ['Published', publishedLabel(it)],
          ['First seen', <Timestamp key="f" nsStr={it.first_seen_ns} />],
          it.form ? ['Form', it.form] : null,
          it.accession ? ['Accession', <span key="a" className="mono">{it.accession}</span>] : null,
          it.filed_date ? ['Filed', it.filed_date] : null,
          it.flags.length ? ['Flags', it.flags.join(', ')] : null,
        ]}
      />
      {it.items.length ? (
        <ul className="nw-items">
          {it.items.map((i) => (
            <li key={i.code}>
              <span className="mono">Item {i.code}</span> {i.title} <span className="nw-src">({i.type.replace(/_/g, ' ')})</span>
            </li>
          ))}
        </ul>
      ) : null}
      {it.summary ? <p className="nw-summary">{it.summary}{it.summary_truncated ? ' (summary cut; read the original)' : ''}</p> : it.kind === 'release' ? <p className="nw-hint">The feed gives no summary.</p> : null}
      <p>
        <ExtLink href={it.link}>{it.kind === 'filing' ? 'Filing index on sec.gov' : 'Original release'}</ExtLink>
      </p>
      {it.entities.length ? (
        <div className="nw-scroll">
          <table className="nw-table">
            <thead>
              <tr>
                <th scope="col">Issuer</th>
                <th scope="col">Role</th>
                <th scope="col">Tickers</th>
                <th scope="col">How</th>
              </tr>
            </thead>
            <tbody>
              {it.entities.map((e) => (
                <tr key={`${e.value}-${e.role}`} title={e.identifier_source ? `Identifiers: ${e.identifier_source}, as of ${e.identifiers_as_of ?? 'not reported'}` : 'No ticker snapshot was available at first sight'}>
                  <td>
                    {e.name ?? <NoValue reason="NOT_REPORTED" />} <span className="nw-src mono">CIK {e.value}</span>
                  </td>
                  <td>{e.role}</td>
                  <td className="mono">{e.tickers.map((t) => t.ticker).join(', ') || <NoValue reason="NOT_REPORTED" />}{e.figi ? ` · ${e.figi}` : ''}</td>
                  <td>
                    {e.method === 'edgar_feed' ? 'EDGAR feed' : `name match “${e.evidence ?? ''}”`} ({Math.round(e.confidence * 100)}%)
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {it.copies.length ? (
        <>
          <h4 className="nw-sub">Same story elsewhere</h4>
          <ul className="nw-items">
            {it.copies.map((c) => (
              <li key={c.id}>
                {sourceName(sources, c.source_id)} · {shortTime(c.first_seen)} · <ExtLink href={c.link}>{c.title}</ExtLink>
                {c.duplicate_of ? ' (copy)' : ''}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {it.fetch ? (
        <p className="nw-src">
          Fetched {it.fetch.received?.slice(0, 19).replace('T', ' ') ?? <NoValue reason="NOT_REPORTED" />} UTC, HTTP {it.fetch.http_status ?? <NoValue reason="NOT_REPORTED" />}, <Hash value={it.fetch.sha256} /> · as of {detail.as_of.slice(0, 19).replace('T', ' ')} UTC
        </p>
      ) : null}
    </Section>
  );
}

export function SourcesPanel({ sources }: { sources?: NewsSourceRow[] }) {
  if (!sources) return null;
  return (
    <Section title="Sources">
      <ul className="nw-sources">
        {sources.map((s) => (
          <li key={s.source_id}>
            <span className="nw-src-name">{s.name}</span> <Badge tone={verdictTone(s.verdict)}>{s.verdict.replace(/_/g, ' ')}</Badge>{' '}
            {s.pollable ? (
              <span className="nw-src">
                {s.last_ok ? `last success ${s.last_ok.slice(0, 19).replace('T', ' ')} UTC` : 'not polled yet'}
                {s.last_status === 'FAILED' && s.last_error ? ` · last poll failed: ${s.last_error}` : ''}
              </span>
            ) : (
              <span className="nw-src">not fetched: {s.refusal}</span>
            )}
          </li>
        ))}
      </ul>
      <Commands commands={['python -m unknown.news.poller run']} />
    </Section>
  );
}
