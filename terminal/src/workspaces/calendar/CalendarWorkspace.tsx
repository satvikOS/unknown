/**
 * Events calendar (CAP-NEWS-10, partial; function EVTS): results releases (8-K Item 2.02) and periodic reports
 * (10-Q/10-K) by week for the monitor watchlist, every ingested issuer or typed tickers. Each entry says whether it
 * occurred (filed on EDGAR), was announced by the issuer in an 8-K, or is projected (estimated internally from the
 * prior year, never presented as a company date). Reads GET /api/calendar.
 */
import { useMemo, useState, type FormEvent } from 'react';
import { Badge } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { STORAGE_KEY as MONITOR_STORAGE_KEY } from '../monitor/model';
import { useApi } from '../research-common/api';
import { ErrorNotice, ExtLink, Loading } from '../research-common/parts';
import type { WorkspaceProps } from '../types';
import {
  DEFAULTS,
  STATUSES,
  STATUS_LABEL,
  TYPES,
  WEEK_CHOICES,
  calendarParams,
  dayLabel,
  parseCalendarRoute,
  readWatchlist,
  statusTone,
  type CalEvent,
  type CalendarOptions,
  type CalendarResponse,
  type EventStatus,
  type EventType,
  type Scope,
} from './model';
import './calendar.css';

const TYPE_LABEL: Record<EventType, string> = {
  results_release: 'Results release',
  periodic_report: 'Periodic report',
  earnings_call: 'Results call',
};

function readWatchlistSafe(): string[] {
  try {
    return readWatchlist(window.localStorage.getItem(MONITOR_STORAGE_KEY));
  } catch {
    return [];
  }
}

function EventRow({ e }: { e: CalEvent }) {
  return (
    <tr className={`cal-${e.status}`}>
      <td>{dayLabel(e.date)}</td>
      <td>
        <strong>{e.ticker ?? e.cik}</strong> <span className="muted">{e.name}</span>
      </td>
      <td>{TYPE_LABEL[e.event_type] ?? e.event_label}</td>
      <td>
        <Badge tone={statusTone(e.status)}>{STATUS_LABEL[e.status]}</Badge>
        {e.overdue ? <Badge tone="warn">no filing yet</Badge> : null}
      </td>
      <td>
        {e.time ? <span className="mono">{e.time.slice(11, 16)} UTC</span> : e.time_text ? e.time_text : <NoValue reason="NOT_REPORTED" detail="no time stated" />}
      </td>
      <td className="cal-source">
        {e.link ? <ExtLink href={e.link}>{e.form ?? 'filing'}</ExtLink> : null} <span className="muted">{e.label}</span>
        {e.evidence ? <div className="cal-evidence">“{e.evidence}”</div> : null}
        {e.confirmed_by ? <div className="micro">confirmed by {e.confirmed_by}</div> : null}
      </td>
    </tr>
  );
}

export function CalendarWeeks({ data }: { data: CalendarResponse }) {
  if (!data.weeks.length) {
    return (
      <p className="muted" role="status">
        No events in {data.from} to {data.to} for the selected issuers and filters.
      </p>
    );
  }
  return (
    <>
      {data.weeks.map((w) => (
        <section key={w.week_start} className="cal-week" aria-label={`Week of ${w.week_start}`}>
          <h3>Week of {w.week_start}</h3>
          <div className="rx-table-wrap">
            <table className="rx-table cal-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Issuer</th>
                  <th>Event</th>
                  <th>Status</th>
                  <th>Time</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {w.events.map((e) => (
                  <EventRow key={`${e.entity_id}-${e.status}-${e.event_type}-${e.date}-${e.accession ?? ''}`} e={e} />
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </>
  );
}

export function IssuerNotes({ data }: { data: CalendarResponse }) {
  const missing = data.issuers.filter((i) => i.status !== 'OK');
  if (!missing.length && !data.unresolved.length) return null;
  return (
    <ul className="cal-notes">
      {data.unresolved.map((u) => (
        <li key={u}>
          {u}: <NoValue reason="NOT_FOUND" detail="not an SEC issuer in the research store" /> not an SEC issuer held in the research store
        </li>
      ))}
      {missing.map((i) => (
        <li key={i.entity_id}>
          {i.name ?? i.entity_id}: <NoValue reason={i.reason ?? 'NO_DATA'} detail={i.detail} /> {i.detail}
        </li>
      ))}
    </ul>
  );
}

export function CalendarWorkspace({ route, navigate }: WorkspaceProps) {
  const fromRoute = parseCalendarRoute(route.rest);
  const [opts, setOpts] = useState<CalendarOptions>({ ...DEFAULTS, ...fromRoute });
  const [draftTickers, setDraftTickers] = useState(fromRoute.tickers);
  const watchlist = useMemo(readWatchlistSafe, []);
  const today = new Date().toISOString().slice(0, 10);
  const req = calendarParams(opts, watchlist, today);
  const q = useApi<CalendarResponse>('params' in req ? '/api/calendar' : null, 'params' in req ? req.params : undefined);
  const setScope = (scope: Scope) => {
    setOpts({ ...opts, scope });
    navigate('calendar', scope, ...(scope === 'tickers' && opts.tickers ? [opts.tickers] : []));
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setOpts({ ...opts, tickers: draftTickers.toUpperCase() });
    if (opts.scope === 'tickers') navigate('calendar', 'tickers', draftTickers.toUpperCase());
  };
  const toggle = <T extends string>(list: T[], v: T): T[] => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  return (
    <WorkspaceFrame
      title="Events calendar"
      description="Results releases and periodic reports from SEC filings: occurred, announced by the issuer, or projected (estimated internally)"
      queries={[q]}
    >
      <form className="cal-toolbar" onSubmit={submit}>
        <label>
          <span>Issuers</span>
          <select value={opts.scope} onChange={(e) => setScope(e.target.value as Scope)}>
            <option value="watchlist">Monitor watchlist ({watchlist.length})</option>
            <option value="all">All ingested SEC issuers</option>
            <option value="tickers">Tickers or CIKs…</option>
          </select>
        </label>
        {opts.scope === 'tickers' ? (
          <label>
            <span>Tickers</span>
            <input value={draftTickers} onChange={(e) => setDraftTickers(e.target.value)} placeholder="AAPL, MSFT, 19617" />
          </label>
        ) : null}
        <label>
          <span>From week of</span>
          <input type="date" value={opts.from ?? ''} onChange={(e) => setOpts({ ...opts, from: e.target.value || null })} />
        </label>
        <label>
          <span>Weeks</span>
          <select value={opts.weeks} onChange={(e) => setOpts({ ...opts, weeks: Number(e.target.value) })}>
            {WEEK_CHOICES.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>Status</legend>
          {STATUSES.map((s: EventStatus) => (
            <label key={s} className="cal-check">
              <input type="checkbox" checked={opts.statuses.includes(s)} onChange={() => setOpts({ ...opts, statuses: toggle(opts.statuses, s) })} />
              {STATUS_LABEL[s]}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Event</legend>
          {TYPES.map((t: EventType) => (
            <label key={t} className="cal-check">
              <input type="checkbox" checked={opts.types.includes(t)} onChange={() => setOpts({ ...opts, types: toggle(opts.types, t) })} />
              {TYPE_LABEL[t]}
            </label>
          ))}
        </fieldset>
        {opts.scope === 'tickers' ? (
          <button type="submit" className="btn primary">
            Apply
          </button>
        ) : null}
      </form>
      {'blocked' in req ? (
        <p className="muted" role="status">
          {req.blocked}
        </p>
      ) : null}
      <ErrorNotice what="Calendar" error={q.error} />
      {'params' in req && !q.data && !q.error ? <Loading what="calendar" /> : null}
      {q.data && q.data.status === 'UNAVAILABLE' ? (
        <p role="status">
          <NoValue reason={q.data.reason ?? 'NO_DATA'} detail={q.data.detail} focusable label="Calendar" /> {q.data.detail}
        </p>
      ) : null}
      {q.data && q.data.weeks ? (
        <>
          <dl className="cal-legend">
            {Object.entries(q.data.legend).map(([k, v]) => (
              <div key={k}>
                <dt>
                  <Badge tone={statusTone(k as EventStatus)}>{STATUS_LABEL[k as EventStatus] ?? k}</Badge>
                </dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          <IssuerNotes data={q.data} />
          <CalendarWeeks data={q.data} />
          <p className="micro muted">Source: U.S. SEC EDGAR submissions and 8-K filings. As of {q.data.as_of.slice(0, 19).replace('T', ' ')} UTC.</p>
        </>
      ) : null}
    </WorkspaceFrame>
  );
}
