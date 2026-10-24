/**
 * News & filings model (CAP-NEWS-02/05/06/08/13): wire types of /api/news*, the route, live merging and the small
 * formatting rules. Pure functions only; the component lives in NewsWorkspace.tsx.
 *
 * Route: `#/news[/top|/cn][/source=…][/form=…][/issuer=…][/q=…][/since=…]`. `cn` is company news for the panel's
 * linked security (its symbol is sent as the issuer; the API resolves it to a CIK through the SEC ticker snapshot).
 */

import type { ReactElement } from 'react';
import { noValue } from '../../ui/unavailable';

export interface NewsEntity {
  scheme: string;
  value: string;
  name: string | null;
  role: string;
  method: string;
  confidence: number;
  tickers: { ticker: string; exchange: string | null }[];
  figi: string | null;
  identifiers_as_of: string | null;
  identifier_source: string | null;
  evidence: string | null;
}

export interface NewsItem {
  id: number;
  source_id: string;
  kind: 'filing' | 'release';
  guid: string;
  title: string;
  summary: string | null;
  summary_truncated: boolean;
  link: string | null;
  category: string | null;
  published: string | null;
  published_precision: 'second' | 'day' | null;
  first_seen: string;
  first_seen_ns: string;
  form: string | null;
  accession: string | null;
  items: { code: string; title: string | null; type: string }[];
  filed_date: string | null;
  size: string | null;
  cluster: { cluster_id: number; copies: number; sources: string[]; first_item_id: number } | null;
  duplicate_of: number | null;
  flags: string[];
  entities: NewsEntity[];
  snippet: string | null;
}

export interface NewsSourceRow {
  source_id: string;
  name: string;
  kind: string;
  region: string;
  provider_id: string;
  verdict: string;
  pollable: boolean;
  refusal: string | null;
  last_ok: string | null;
  last_poll: string | null;
  last_status: string | null;
  last_error: string | null;
}

export interface NewsListResponse {
  as_of: string;
  items: NewsItem[];
  next_before: number | null;
  latest_id: number;
  sources: NewsSourceRow[];
  issuers: { requested: string[]; ciks: number[]; unresolved: string[]; identifiers: { source: string | null; as_of: string | null } | null } | null;
}

export interface NewsUnavailable {
  status: 'UNAVAILABLE';
  reason: string;
  populate?: string[];
  sources?: NewsSourceRow[];
}

export interface ItemDetail extends NewsItem {
  copies: { id: number; source_id: string; title: string; link: string | null; published: string | null; first_seen: string; duplicate_of: number | null }[];
  fetch: { url: string; received: string | null; http_status: number | null; sha256: string | null; bytes: number | null } | null;
}

export interface Story {
  cluster_id: number;
  title: string;
  copies: number;
  sources: string[];
  first_seen: string;
  last_seen: string;
  earliest_published: string | null;
  lead: NewsItem | null;
}

export interface TopResponse {
  as_of: string;
  hours: number;
  ranking: string;
  stories: Story[];
}

export type NewsView = 'tape' | 'top' | 'cn';

export interface NewsOptions {
  view: NewsView;
  source?: string;
  form?: string;
  issuer?: string;
  q?: string;
  since?: string;
}

const KEYS = ['source', 'form', 'issuer', 'q', 'since'] as const;

export function isNewsUnavailable(x: unknown): x is NewsUnavailable {
  return typeof x === 'object' && x !== null && (x as { status?: unknown }).status === 'UNAVAILABLE';
}

export function parseNewsRoute(rest: readonly string[]): NewsOptions {
  const o: NewsOptions = { view: 'tape' };
  let parts = [...rest];
  if (parts[0] === 'top' || parts[0] === 'cn') {
    o.view = parts[0];
    parts = parts.slice(1);
  }
  for (const p of parts) {
    const i = p.indexOf('=');
    if (i <= 0) continue;
    const k = p.slice(0, i);
    const v = p.slice(i + 1).trim();
    if ((KEYS as readonly string[]).includes(k) && v) o[k as (typeof KEYS)[number]] = v;
  }
  return o;
}

export function newsRoute(o: NewsOptions): string[] {
  const out: string[] = o.view === 'tape' ? [] : [o.view];
  for (const k of KEYS) {
    const v = o[k]?.trim();
    if (v) out.push(`${k}=${v}`);
  }
  return out;
}

/** Query parameters for GET /api/news. Company news uses the linked security; without one there is no query. */
export function listParams(o: NewsOptions, linked: string | null): Record<string, string> | null {
  const issuer = o.view === 'cn' ? linked : o.issuer;
  if (o.view === 'cn' && !issuer) return null;
  const p: Record<string, string> = { limit: '200' };
  if (o.source) p.source = o.source;
  if (o.form) p.form = o.form;
  if (issuer) p.issuer = issuer;
  if (o.q) p.q = o.q;
  if (o.since) p.since = o.since;
  return p;
}

/** True when the server filters the list, so a live item must be checked by a refetch rather than merged. */
export function filtered(o: NewsOptions): boolean {
  return o.view === 'cn' || KEYS.some((k) => Boolean(o[k]));
}

/** Newest first, one row per id, capped. */
export function mergeLive(current: readonly NewsItem[], incoming: readonly NewsItem[], cap = 500): NewsItem[] {
  const byId = new Map<number, NewsItem>();
  for (const it of current) byId.set(it.id, it);
  for (const it of incoming) byId.set(it.id, it);
  return [...byId.values()].sort((a, b) => b.id - a.id).slice(0, cap);
}

/** Issuer tokens for alerts from the monitor workspace's browser-side watchlists (all lists, de-duplicated). */
export function watchlistIssuers(raw: string | null, cap = 500): string[] {
  if (!raw) return [];
  try {
    const state = JSON.parse(raw) as { lists?: { keys?: unknown }[] };
    const out = new Set<string>();
    for (const l of state.lists ?? []) {
      for (const k of Array.isArray(l.keys) ? l.keys : []) {
        if (typeof k === 'string' && /^[A-Z0-9][A-Z0-9.\-/]{0,30}$/.test(k)) out.add(k);
      }
    }
    return [...out].slice(0, cap);
  } catch {
    return [];
  }
}

/** The feed's own time: a date-only feed shows the date and says so; a missing time is said, never invented. */
export function publishedLabel(item: Pick<NewsItem, 'published' | 'published_precision'>): string {
  if (!item.published) return 'not given by the feed';
  if (item.published_precision === 'day') return `${item.published.slice(0, 10)} (date only)`;
  return `${item.published.slice(0, 19).replace('T', ' ')} UTC`;
}

export function shortTime(iso: string | null): string | ReactElement {
  return iso ? iso.slice(11, 19) : noValue('NOT_REPORTED');
}

export interface SnippetPart {
  text: string;
  hit: boolean;
}

/** FTS5 snippet markers (U+0002 start, U+0003 end) to parts. */
export function splitSnippet(s: string | null): SnippetPart[] {
  if (!s) return [];
  const out: SnippetPart[] = [];
  for (const seg of s.split('\u0002')) {
    const end = seg.indexOf('\u0003');
    if (end < 0) {
      if (seg) out.push({ text: seg, hit: false });
      continue;
    }
    if (end > 0) out.push({ text: seg.slice(0, end), hit: true });
    if (seg.slice(end + 1)) out.push({ text: seg.slice(end + 1), hit: false });
  }
  return out;
}

/** The issuer column: the first tagged issuer's ticker as of the snapshot, else its name, else nothing. */
export function issuerLabel(item: Pick<NewsItem, 'entities'>): string {
  const e = item.entities.find((x) => x.role !== 'reporting') ?? item.entities[0];
  if (!e) return '';
  return e.tickers[0]?.ticker ?? e.name ?? `CIK ${e.value}`;
}

export function formLabel(item: Pick<NewsItem, 'form' | 'items' | 'category'>): string {
  if (!item.form) return item.category ?? '';
  return item.items.length ? `${item.form} ${item.items.map((i) => i.code).join(',')}` : item.form;
}

export function sourceName(sources: readonly NewsSourceRow[] | undefined, id: string): string {
  return sources?.find((s) => s.source_id === id)?.name ?? id;
}

export function verdictTone(verdict: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  return verdict === 'allowed' ? 'ok' : verdict === 'allowed_with_conditions' ? 'warn' : verdict === 'blocked' ? 'bad' : 'neutral';
}

export const SINCE_CHOICES: readonly { value: string; label: string }[] = [
  { value: '', label: 'Any time' },
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
];
