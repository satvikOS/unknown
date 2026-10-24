/**
 * Events calendar (CAP-NEWS-10): wire types of GET /api/calendar and pure helpers. Entries are occurred (filed on
 * EDGAR), announced (a date the issuer stated in an 8-K) or projected (estimated internally, labelled so).
 */
import { watchlistIssuers } from '../news/model';

export type EventStatus = 'occurred' | 'announced' | 'projected';
export type EventType = 'results_release' | 'periodic_report' | 'earnings_call';

export interface CalEvent {
  event_type: EventType;
  status: EventStatus;
  basis: 'filing' | 'announced' | 'derived';
  date: string;
  time: string | null;
  time_text?: string | null;
  form: string | null;
  accession: string | null;
  items?: string[];
  link: string | null;
  label: string;
  evidence?: string;
  derived_from?: { accession: string; form: string; date: string };
  overdue?: boolean;
  confirmed_by?: string | null;
  entity_id: string;
  cik: number;
  name: string | null;
  ticker: string | null;
  event_label: string;
  week: string;
}

export interface CalendarIssuer {
  entity_id: string;
  name: string | null;
  status: string;
  reason?: string;
  detail?: string | null;
  events?: number;
}

export interface CalendarResponse {
  as_of: string;
  from: string;
  to: string;
  count: number;
  weeks: { week_start: string; events: CalEvent[] }[];
  issuers: CalendarIssuer[];
  legend: Record<string, string>;
  event_types: Record<string, string>;
  unresolved: string[];
  status?: string;
  reason?: string;
  detail?: string;
}

export type Scope = 'watchlist' | 'all' | 'tickers';

export interface CalendarOptions {
  scope: Scope;
  tickers: string;
  from: string | null;
  weeks: number;
  statuses: EventStatus[];
  types: EventType[];
}

export const STATUSES: readonly EventStatus[] = ['occurred', 'announced', 'projected'];
export const TYPES: readonly EventType[] = ['results_release', 'periodic_report', 'earnings_call'];
export const WEEK_CHOICES = [4, 8, 13, 26] as const;
export const DEFAULTS: CalendarOptions = { scope: 'watchlist', tickers: '', from: null, weeks: 13, statuses: [...STATUSES], types: [...TYPES] };

/** `#/calendar/<scope>[/<tickers>]`. */
export function parseCalendarRoute(rest: readonly string[]): Pick<CalendarOptions, 'scope' | 'tickers'> {
  const scope = (['watchlist', 'all', 'tickers'] as const).find((s) => s === rest[0]) ?? 'watchlist';
  return { scope, tickers: scope === 'tickers' ? (rest[1] ?? '').toUpperCase() : '' };
}

/** Monday of the week that holds `iso` (YYYY-MM-DD), as YYYY-MM-DD. */
export function mondayOf(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  const shift = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - shift);
  return d.toISOString().slice(0, 10);
}

export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Query parameters, or a reason the request cannot be made (an empty watchlist is said, not shown as no events). */
export function calendarParams(o: CalendarOptions, watchlist: readonly string[], todayIso: string): { params: Record<string, string> } | { blocked: string } {
  const from = mondayOf(o.from ?? todayIso);
  const params: Record<string, string> = { from, to: addDays(from, o.weeks * 7 - 1) };
  if (o.scope === 'watchlist') {
    if (!watchlist.length) return { blocked: 'Your monitor watchlist is empty: add securities in the Monitor (MON), or choose all ingested issuers.' };
    params.issuers = watchlist.join(',');
  } else if (o.scope === 'tickers') {
    const t = o.tickers.split(/[\s,]+/).filter(Boolean).join(',');
    if (!t) return { blocked: 'Type one or more tickers or CIKs.' };
    params.issuers = t.toUpperCase();
  }
  if (o.statuses.length && o.statuses.length < STATUSES.length) params.status = o.statuses.join(',');
  if (o.types.length && o.types.length < TYPES.length) params.types = o.types.join(',');
  return { params };
}

export function readWatchlist(raw: string | null): string[] {
  return watchlistIssuers(raw);
}

export function statusTone(s: EventStatus): 'ok' | 'info' | 'warn' {
  return s === 'occurred' ? 'ok' : s === 'announced' ? 'info' : 'warn';
}

export const STATUS_LABEL: Record<EventStatus, string> = {
  occurred: 'Occurred',
  announced: 'Announced',
  projected: 'Projected (estimated)',
};

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
export function dayLabel(iso: string): string {
  return `${DAYS[new Date(`${iso}T00:00:00Z`).getUTCDay()]} ${iso}`;
}
