/**
 * Market data (CAP-MKT-01, CAP-MKT-02, CAP-IBKR-08, CAP-IBKR-12, CAP-DATA-10): the IBKR market-data
 * session's state, its subscriptions with the market-data type IBKR delivered, the feed's
 * consolidation label, entitlement errors, the line budget and pacing, and the session's audit log.
 *
 * Source: GET /api/marketdata/status, polled (the status file is process state outside the event log).
 * Route: `#/marketdata` and `#/marketdata/<instrument_id>` (one subscription's detail).
 */
import { nsStrOf } from '../../api/nsStrings';
import { formatDecimal } from '../../format/decimal';
import { formatAge } from '../../format/time';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge, StatusPill, Timestamp } from '../../ui/primitives';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { usePluginQuery } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import {
  connectionSummary,
  consolidationText,
  countSubscriptions,
  deliveredVsRequested,
  entitlementIssues,
  lineUsePercent,
  parseStatus,
  statusTone,
  type AuditEntry,
  type MarketDataStatus,
  type Subscription,
} from './model';
import './md.css';
import { NoValue } from '../../ui/unavailable';

export const STATUS_POLL_MS = 5000;

function sentence(text: string | null): string {
  if (!text) return '';
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

function TypeCell({ s }: { s: Subscription }) {
  const t = deliveredVsRequested(s);
  return (
    <span className="md-inline">
      <span>{t.text}</span>
      {t.differs ? (
        <Badge tone="warn" title="IBKR delivered a different market-data type than requested; the delivered type is what counts">
          differs
        </Badge>
      ) : null}
    </span>
  );
}

function ConsolidationCell({ c }: { c: string }) {
  const t = consolidationText(c);
  return <StatusPill label={t.label} tone={t.tone} detail={t.detail} compact />;
}

function SessionPanel({ st }: { st: MarketDataStatus }) {
  const conn = connectionSummary(st.connection, st.status_file_stale);
  const c = st.connection;
  return (
    <section className="panel" aria-labelledby="md-session-h">
      <header>
        <h2 id="md-session-h">Session</h2>
        <StatusPill label={conn.label} tone={conn.tone} detail={conn.detail} compact />
      </header>
      <div className="panel-body">
        <dl className="kv">
          <dt>State</dt>
          <dd>{conn.detail}</dd>
          <dt>Connected</dt>
          <dd>{c?.connected === null || c === null ? <NoValue reason="NOT_REPORTED" /> : c.connected ? 'yes' : 'no'}</dd>
          <dt>Paper identity</dt>
          <dd>{c?.verified_paper ? 'verified' : 'not verified'}</dd>
          {c?.reason ? (
            <>
              <dt>Reason</dt>
              <dd>{c.reason}</dd>
            </>
          ) : null}
          <dt>Upstream / farms</dt>
          <dd>
            {c?.upstream_down ?? 'upstream ok'}
            {c && c.data_farms_down.length ? `; data farms down: ${c.data_farms_down.join(', ')}` : '; no data farm reported down'}
          </dd>
          <dt>Host / client</dt>
          <dd className="num">
            {c?.host ?? <NoValue reason="NOT_REPORTED" />} · client {c?.client_id ?? <NoValue reason="NOT_REPORTED" />}
          </dd>
          <dt>Account</dt>
          <dd className="num">{c?.account ?? <NoValue reason="NOT_REPORTED" />}</dd>
          <dt>Requested type</dt>
          <dd>{st.requested_market_data_type ?? <NoValue reason="NOT_REPORTED" />} <span className="md-hint">(IBKR reports the delivered type per subscription)</span></dd>
          <dt>Status file</dt>
          <dd>
            written <Timestamp ns={st.written_at_ns} nsStr={nsStrOf(st, 'written_at_ns')} />{' '}
            {st.status_file_age_ms !== null ? <span className="md-hint">({formatAge(st.status_file_age_ms)} old)</span> : null}
            {st.status_file_stale ? (
              <div>
                <StatusPill label="stale" tone="warn" compact detail={st.status_file_stale_reason ?? undefined} /> <span className="md-hint">{st.status_file_stale_reason}</span>
              </div>
            ) : null}
          </dd>
          <dt>Evidence</dt>
          <dd>{st.evidence_level ?? 'unknown evidence level'}</dd>
          <dt>Storage</dt>
          <dd className="md-hint">{st.storage ?? <NoValue reason="NOT_REPORTED" />}</dd>
        </dl>
      </div>
    </section>
  );
}

function EntitlementPanel({ st }: { st: MarketDataStatus }) {
  const issues = entitlementIssues(st.subscriptions.filter((s) => s.current));
  const er = st.entitlement_error_rate;
  return (
    <section className="panel" aria-labelledby="md-ent-h">
      <header>
        <h2 id="md-ent-h">Entitlements</h2>
        {issues.some((i) => i.blocking) ? (
          <StatusPill label={`${issues.filter((i) => i.blocking).length} unavailable`} tone="bad" compact />
        ) : issues.length ? (
          <StatusPill label="partial" tone="warn" compact />
        ) : (
          <StatusPill label="no entitlement errors" tone="ok" compact />
        )}
      </header>
      <div className="panel-body">
        <dl className="kv">
          <dt>Error rate</dt>
          <dd className="num">
            {er?.value ?? <span className="muted">{er?.missing_reason ?? 'not reported'}</span>}
            {er?.value ? <span className="md-hint"> entitlement errors / requests sent on this connection</span> : null}
          </dd>
        </dl>
        {er?.formula ? <p className="md-hint" style={{ margin: '4px 0 8px' }}>{er.formula}</p> : null}
        {issues.length ? (
          <ul className="md-issues" aria-label="Entitlement problems by instrument">
            {issues.map((i) => (
              <li key={`${i.instrument_id}-${i.state}`}>
                <span className="md-inline">
                  <a href={routeHref('marketdata', i.instrument_id)} className="cmd-id">
                    {i.instrument_id}
                  </a>
                  <StatusPill label={i.state} tone={i.blocking ? 'bad' : 'warn'} compact />
                  {i.code !== null ? <span className="num md-hint">IBKR {i.code}</span> : null}
                </span>
                <div className="md-hint">{i.cause}</div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            No subscription reports an entitlement or availability error.
          </p>
        )}
      </div>
    </section>
  );
}

function CapacityPanel({ st }: { st: MarketDataStatus }) {
  const lb = st.line_budget;
  const pct = lineUsePercent(lb);
  const p = st.pacing;
  return (
    <section className="panel" aria-labelledby="md-cap-h">
      <header>
        <h2 id="md-cap-h">Lines and pacing</h2>
        {p?.violations ? <StatusPill label={`${p.violations} pacing violation(s)`} tone="bad" compact /> : null}
      </header>
      <div className="panel-body">
        <div id="md-lines-label" className="md-hint">
          Streaming lines in use
        </div>
        <div
          className="md-meter"
          role="meter"
          aria-labelledby="md-lines-label"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct ?? undefined}
          aria-valuetext={lb ? `${lb.in_use ?? '?'} of ${lb.capacity ?? '?'} lines in use` : 'not reported'}
        >
          <div style={{ width: `${pct ?? 0}%` }} />
        </div>
        <dl className="kv">
          <dt>In use / capacity</dt>
          <dd className="num">
            {lb?.in_use ?? <NoValue reason="NOT_REPORTED" />} / {lb?.capacity ?? <NoValue reason="NOT_REPORTED" />} <span className="md-hint">({lb?.lines ?? <NoValue reason="NOT_REPORTED" />} allocated, {lb?.tws_reserved_lines ?? <NoValue reason="NOT_REPORTED" />} reserved for TWS)</span>
          </dd>
          <dt>Headroom</dt>
          <dd className="num">{lb?.headroom ?? <NoValue reason="NOT_REPORTED" />}</dd>
          <dt>Refused locally</dt>
          <dd className="num">{lb?.refused_locally ?? <NoValue reason="NOT_REPORTED" />}</dd>
          <dt>Pacing</dt>
          <dd className="num">
            {p ? `${p.in_window ?? '?'} in window · max ${p.max_per_second ?? '?'}/s · ${p.reserved ?? '?'} reserved` : <NoValue reason="NOT_REPORTED" />}
          </dd>
          <dt>Freshness budgets</dt>
          <dd className="num">
            {Object.keys(st.freshness_budgets_ms).length
              ? Object.entries(st.freshness_budgets_ms)
                  .map(([use, ms]) => `${use} ${ms.toLocaleString('en-US')} ms`)
                  .join(' · ')
              : 'none set by the owner'}
          </dd>
        </dl>
      </div>
    </section>
  );
}

export function SubscriptionDetail({ s }: { s: Subscription }) {
  const a = s.analytics;
  // Prices are per share in the quote's currency (marketdata.py quote_analytics units); never shown bare.
  const quoteCcy = a?.currency ?? '(currency not reported)';
  return (
    <section className="panel md-detail" aria-labelledby="md-sub-h">
      <header>
        <h2 id="md-sub-h">Subscription {s.instrument_id}</h2>
        <a className="btn small" href={routeHref('marketdata')}>
          Close
        </a>
      </header>
      <div className="panel-body">
        <dl className="kv">
          <dt>Status</dt>
          <dd>
            <StatusPill label={s.status} tone={statusTone(s.status)} compact />
            {s.reported_status ? <span className="md-hint"> (session reported {s.reported_status})</span> : null}
            {s.live_reason ? <div className="md-hint">{s.live_reason}</div> : null}
          </dd>
          <dt>Delivered type</dt>
          <dd>
            <TypeCell s={s} />
          </dd>
          <dt>Source label</dt>
          <dd className="cmd-id">{s.source ?? <NoValue reason="NOT_REPORTED" />}</dd>
          <dt>Consolidation</dt>
          <dd>
            <ConsolidationCell c={s.consolidation} /> <span className="md-hint">{consolidationText(s.consolidation).detail}</span>
          </dd>
          <dt>Feed label</dt>
          <dd>
            {s.feed?.label ?? <NoValue reason="NOT_REPORTED" />}
            {s.feed?.evidence ? <div className="md-hint">Evidence: {s.feed.evidence}{s.feed.observed_on ? ` (observed ${s.feed.observed_on})` : ''}</div> : null}
          </dd>
          <dt>Contract</dt>
          <dd className="num">conId {s.con_id ?? <NoValue reason="NOT_REPORTED" />} · req {s.req_id ?? <NoValue reason="NOT_REPORTED" />}</dd>
          <dt>Quote state</dt>
          <dd>{s.quote_state ?? <NoValue reason="NOT_REPORTED" />}</dd>
          <dt>Last tick</dt>
          <dd>{s.age_ms !== null ? `${formatAge(s.age_ms)} before the status was written` : 'no tick'} · {s.ticks ?? <NoValue reason="NOT_REPORTED" />} ticks</dd>
          {s.unavailable ? (
            <>
              <dt>Unavailable</dt>
              <dd>
                <StatusPill label={s.unavailable.state} tone="bad" compact /> {s.unavailable.code !== null ? <span className="num">IBKR {s.unavailable.code}</span> : null}
                <div>{s.unavailable.cause}</div>
              </dd>
            </>
          ) : null}
          <dt>Fields</dt>
          <dd>
            {s.fields.length ? (
              <ul className="md-fields">
                {s.fields.map((f) => (
                  <li key={f.name}>
                    {f.name}: {f.present ? 'present' : <span className="muted">null ({f.null_reason ?? 'no reason given'})</span>}
                  </li>
                ))}
              </ul>
            ) : (
              <span className="muted">none received</span>
            )}
            <div className="md-hint">The status file holds no prices (CAP-IBKR-12); prices live only in the core's quote book.</div>
          </dd>
          <dt>Quote analytics</dt>
          <dd>
            {a ? (
              <span className="num">
                mid {a.mid !== null ? `${formatDecimal(a.mid)} ${quoteCcy}` : <NoValue reason="NOT_REPORTED" />} · spread {a.spread !== null ? `${formatDecimal(a.spread)} ${quoteCcy}` : <NoValue reason="NOT_REPORTED" />} ({a.spread_bps ?? <NoValue reason="NOT_REPORTED" />} bps) · age{' '}
                {a.age_ms !== null ? formatAge(a.age_ms) : <NoValue reason="NOT_REPORTED" />}
                {a.stale ? ` · stale for ${Object.entries(a.stale).filter(([, v]) => v).map(([k]) => k).join(', ') || 'no use'}` : ''}
                {a.missing_reason ? ` · ${a.missing_reason}` : ''}
              </span>
            ) : (
              <span className="muted">{s.analytics_missing_reason ?? 'not available'}</span>
            )}
          </dd>
          {s.warnings.length ? (
            <>
              <dt>Warnings</dt>
              <dd>
                <ul className="md-fields">
                  {s.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
          {s.errors.length ? (
            <>
              <dt>Errors</dt>
              <dd>
                <ul className="md-fields">
                  {s.errors.map((e, i) => (
                    <li key={i}>
                      <span className="num">{e.code ?? <NoValue reason="NOT_REPORTED" />}</span> {e.text}
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
          {s.sampling_mode ? (
            <>
              <dt>Sampling</dt>
              <dd className="md-hint">{s.sampling_mode}</dd>
            </>
          ) : null}
        </dl>
      </div>
    </section>
  );
}

export function MarketDataWorkspace({ route, navigate }: WorkspaceProps) {
  const q = usePluginQuery<unknown>('/api/marketdata/status', undefined, { pollMs: STATUS_POLL_MS });
  const st = q.data === undefined ? null : parseStatus(q.data);
  const selectedId = route.rest[0] ?? null;
  const selected = st?.subscriptions.find((s) => s.instrument_id === selectedId && s.current) ?? st?.subscriptions.find((s) => s.instrument_id === selectedId) ?? null;
  const counts = st ? countSubscriptions(st.subscriptions) : null;

  const columns: Column<Subscription>[] = [
    {
      id: 'instrument',
      header: 'Instrument',
      cell: (s) => (
        <span className="md-inline">
          <a href={routeHref('marketdata', s.instrument_id)} className="cmd-id">
            {s.instrument_id}
          </a>
          {s.current ? null : <Badge title="An earlier record of a re-subscribed instrument">earlier</Badge>}
        </span>
      ),
      sortValue: (s) => s.instrument_id,
    },
    { id: 'status', header: 'Status', cell: (s) => <StatusPill label={s.status} tone={statusTone(s.status)} detail={s.live_reason ?? s.unavailable?.cause} compact />, sortValue: (s) => s.status },
    { id: 'live', header: 'Live', description: 'Usable as a live quote: delivered type live, no delayed ticks, no unavailable state, fresh connection', cell: (s) => (s.live ? 'yes' : 'no'), sortValue: (s) => (s.live ? 1 : 0), sortKind: 'number' },
    { id: 'type', header: 'Delivered type', description: 'The market-data type IBKR reported for this subscription (marketDataType)', cell: (s) => <TypeCell s={s} />, sortValue: (s) => s.delivered_type_name ?? '' },
    { id: 'consolidation', header: 'Consolidation', cell: (s) => <ConsolidationCell c={s.consolidation} />, sortValue: (s) => s.consolidation },
    {
      id: 'entitlement',
      header: 'Entitlement',
      cell: (s) =>
        s.unavailable ? (
          <span className="md-inline">
            <span>{s.unavailable.state}</span>
            {s.unavailable.code !== null ? <span className="num md-hint">{s.unavailable.code}</span> : null}
          </span>
        ) : s.warnings.some((w) => w.startsWith('IBKR 10090')) ? (
          <span>PARTIALLY_SUBSCRIBED</span>
        ) : (
          <span className="muted">ok</span>
        ),
    },
    { id: 'state', header: 'Quote state', cell: (s) => s.quote_state ?? <NoValue reason="NOT_REPORTED" /> },
    { id: 'age', header: 'Tick age at write', numeric: true, description: 'Receipt-time age of the latest tick when the session wrote the status file (see the file age above)', cell: (s) => (s.age_ms !== null ? formatAge(s.age_ms) : <NoValue reason="NOT_REPORTED" />), sortValue: (s) => s.age_ms ?? -1, sortKind: 'number' },
    { id: 'ticks', header: 'Ticks', numeric: true, cell: (s) => s.ticks ?? 0, sortValue: (s) => s.ticks ?? 0, sortKind: 'number' },
  ];

  return (
    <WorkspaceFrame
      title="Market data"
      description="IBKR market-data session: state, subscriptions, the type IBKR delivered, feed consolidation and entitlement errors."
      queries={[q]}
    >
      <QueryError what="Market-data status" error={q.error} />
      {q.data !== undefined && !st ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          The status response did not have the expected shape; nothing is shown rather than a guess.
        </div>
      ) : null}
      {st && !st.available ? (
        <div className="callout md-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
          <strong>No market-data session status.</strong> {st.unavailable_reason}
          <div className="md-hint" style={{ marginTop: 4 }}>
            Evidence level: {st.evidence_level ?? 'unknown'}. Quotes for orders still come from the core's quote book; this page reports the IBKR session only.
          </div>
        </div>
      ) : null}
      {st && st.available ? (
        <>
          {st.status_file_stale ? (
            <div className="callout md-gap" role="alert" style={{ ['--tone' as string]: 'var(--warn)' }}>
              <strong>Status file is stale.</strong> {sentence(st.status_file_stale_reason)} Every subscription below is shown as not live; the status the
              session last reported is in each subscription's detail.
            </div>
          ) : null}
          <div className="md-grid">
            <SessionPanel st={st} />
            <EntitlementPanel st={st} />
            <CapacityPanel st={st} />
          </div>
          <section className="panel" aria-labelledby="md-subs-h">
            <header>
              <h2 id="md-subs-h">Subscriptions</h2>
              {counts ? (
                <span className="md-hint num">
                  {counts.total} current · {counts.live} live · {counts.delayed} delayed · {counts.unavailable} unavailable
                </span>
              ) : null}
            </header>
            <div className="panel-body">
              <p className="md-hint" style={{ margin: 0 }}>
                Delayed data never validates an order. A non-consolidated real-time feed validates SIMULATED and PAPER orders only; LIVE requires a
                consolidated feed.
              </p>
            </div>
            <div className="panel-body flush">
              <DataTable
                caption="Market-data subscriptions"
                captionHidden
                columns={columns}
                rows={st.subscriptions}
                rowKey={(s) => `${s.instrument_id}#${s.req_id ?? ''}`}
                initialSort={{ columnId: 'instrument', direction: 'ascending' }}
                onRowActivate={(s) => navigate('marketdata', s.instrument_id)}
                activateLabel="show"
                emptyText="The session has no subscriptions."
              />
            </div>
          </section>
          {selectedId && selected ? <SubscriptionDetail s={selected} /> : null}
          {selectedId && !selected ? (
            <div className="callout md-detail" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
              No subscription for {selectedId} in the current status.
            </div>
          ) : null}
          <AuditPanel title="Session audit log" id="md-audit-h" rows={st.audit} empty="No audit entries." />
          {st.anomalies.length ? <AuditPanel title="Anomalies" id="md-anom-h" rows={st.anomalies} empty="None." /> : null}
        </>
      ) : null}
      {!st && q.status === 'loading' ? <p className="muted">Loading market-data status…</p> : null}
    </WorkspaceFrame>
  );
}

function AuditPanel({ title, id, rows, empty }: { title: string; id: string; rows: AuditEntry[]; empty: string }) {
  const cols: Column<AuditEntry & { i: number }>[] = [
    { id: 'at', header: 'At', cell: (a) => <Timestamp ns={a.at_ns} nsStr={nsStrOf(a, 'at_ns')} withMs />, sortValue: (a) => a.i, sortKind: 'number' },
    { id: 'instrument', header: 'Instrument', cell: (a) => a.instrument_id ?? <span className="muted">session</span> },
    { id: 'kind', header: 'Kind', cell: (a) => a.kind },
    { id: 'detail', header: 'Detail', cell: (a) => a.detail, wrap: true },
  ];
  return (
    <section className="panel section-gap" aria-labelledby={id}>
      <header>
        <h2 id={id}>{title}</h2>
        <span className="md-hint">newest last · at most 100</span>
      </header>
      <div className="panel-body flush">
        <DataTable caption={title} captionHidden columns={cols} rows={rows.map((r, i) => ({ ...r, i }))} rowKey={(a) => String(a.i)} emptyText={empty} maxHeight="320px" />
      </div>
    </section>
  );
}
