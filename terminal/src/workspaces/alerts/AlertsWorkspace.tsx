/**
 * Alerts (CAP-MKT-09, CAP-NEWS-13): the server-side alert engine's inbox, rules and audit trail.
 *
 * Rules are stored and evaluated on the server (api/routes/alerts.py, alerts/engine.py) against the live market-data
 * bus, the clock and the news store, so they fire with no terminal open; a fired alert also raises a macOS
 * notification on the server's machine. This view reads GET /api/alerts/{inbox,rules,audit,status}, follows
 * GET /api/alerts/stream (SSE) for new alerts, and changes rules only through POST /api/alerts/*. An alert never
 * acts on an order.
 */
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { ExportButton } from '../../ui/ExportButton';
import { OWNER, nsToIso, type ExportSource, type ExportTable } from '../../ui/export';
import { useLinkedSecurity } from '../../ui/link';
import { Badge, StatusPill, Timestamp } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { describeError } from '../../api/client';
import { RouteTabs } from '../operations/parts';
import { usePluginPost, usePluginQuery } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import {
  RULE_KINDS,
  SNOOZE_CHOICES,
  buildRule,
  describeRule,
  emptyForm,
  ruleState,
  triggerSummary,
  type AlertEvent,
  type AuditEntry,
  type EngineStatus,
  type FormErrors,
  type RuleForm,
  type RuleRow,
} from './model';
import './alerts.css';

const TABS = [
  { id: 'inbox', label: 'Inbox' },
  { id: 'rules', label: 'Rules' },
  { id: 'audit', label: 'Audit' },
] as const;
type Tab = (typeof TABS)[number]['id'];
type StreamState = 'connecting' | 'live' | 'reconnecting' | 'unsupported';

const NO_SEQ = { seqDriven: false } as const;

/** GET /api/alerts/stream: calls back on a new alert and on a rules change; reports its connection state. */
function useAlertStream(onAlert: (e: AlertEvent) => void, onRules: () => void): StreamState {
  const [state, setState] = useState<StreamState>(typeof EventSource === 'undefined' ? 'unsupported' : 'connecting');
  useEffect(() => {
    if (typeof EventSource === 'undefined') return;
    const es = new EventSource('/api/alerts/stream', { withCredentials: true });
    es.onopen = () => setState('live');
    es.onerror = () => setState('reconnecting');
    es.addEventListener('alert', (m) => {
      try {
        onAlert(JSON.parse((m as MessageEvent<string>).data) as AlertEvent);
      } catch {
        /* a malformed frame is ignored; the inbox refetch shows the stored alert */
      }
    });
    es.addEventListener('rules', () => onRules());
    return () => es.close();
  }, [onAlert, onRules]);
  return state;
}

export function AlertsWorkspace({ route, navigate }: WorkspaceProps) {
  const tab: Tab = route.rest[0] === 'rules' ? 'rules' : route.rest[0] === 'audit' ? 'audit' : 'inbox';
  const post = usePluginPost();
  const inbox = usePluginQuery<{ events: AlertEvent[]; unread: number }>('/api/alerts/inbox', { limit: 500 }, NO_SEQ);
  const rules = usePluginQuery<{ rules: RuleRow[]; version: number }>('/api/alerts/rules', undefined, NO_SEQ);
  const audit = usePluginQuery<{ entries: AuditEntry[] }>(tab === 'audit' ? '/api/alerts/audit' : null, { limit: 1000 }, NO_SEQ);
  const status = usePluginQuery<EngineStatus>('/api/alerts/status', undefined, { ...NO_SEQ, pollMs: 15000 });
  const [latest, setLatest] = useState<AlertEvent | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const refetchInbox = inbox.refetch;
  const refetchRules = rules.refetch;
  const onAlert = useMemo(
    () => (e: AlertEvent) => {
      setLatest(e);
      refetchInbox();
      refetchRules();
    },
    [refetchInbox, refetchRules],
  );
  const stream = useAlertStream(onAlert, refetchRules);

  const act = async (path: string, body: unknown) => {
    setActionError(null);
    try {
      await post(path, body);
      refetchRules();
      refetchInbox();
      if (tab === 'audit') audit.refetch();
    } catch (err) {
      setActionError(describeError(err));
    }
  };

  const bar = <RouteTabs<Tab> idBase="alr" label="Alerts sections" tabs={TABS} current={tab} onSelect={(t) => navigate('alerts', t)} />;
  return (
    <WorkspaceFrame
      title="Alerts"
      description="Server-side alerts on prices, % moves, volume, time, chart studies, news and filings. They notify; they never place, change or cancel an order."
      queries={[inbox, rules, status, ...(tab === 'audit' ? [audit] : [])]}
    >
      <EngineLine status={status.data} stream={stream} unread={inbox.data?.unread ?? null} />
      {latest ? (
        <p className="alr-latest" role="status">
          New alert: <strong>{latest.title}</strong> · {triggerSummary(latest)}
          {latest.delayed ? <Badge tone="warn">delayed data</Badge> : null}
        </p>
      ) : null}
      {bar}
      {actionError ? (
        <p className="alr-error" role="alert">
          {actionError}
        </p>
      ) : null}
      <QueryError what="Alerts" error={inbox.error ?? rules.error ?? status.error} />
      {tab === 'inbox' ? <Inbox events={inbox.data?.events ?? []} loading={inbox.status === 'loading'} onAck={(ids) => act('/api/alerts/inbox/ack', ids ? { event_ids: ids } : { all: true })} /> : null}
      {tab === 'rules' ? <Rules rows={rules.data?.rules ?? []} nowNs={status.data?.now_ns ?? Date.now() * 1e6} act={act} /> : null}
      {tab === 'audit' ? <Audit entries={audit.data?.entries ?? []} loading={audit.status === 'loading'} /> : null}
    </WorkspaceFrame>
  );
}

function EngineLine({ status, stream, unread }: { status: EngineStatus | undefined; stream: StreamState; unread: number | null }) {
  const eng = status?.engine ?? null;
  const ageS = status?.heartbeat_ns && status.now_ns ? Math.max(0, Math.round((status.now_ns - status.heartbeat_ns) / 1e9)) : null;
  const engineTone = !eng ? 'bad' : ageS !== null && ageS > 30 ? 'bad' : 'ok';
  const n = status?.notifications;
  return (
    <div className="alr-engine" aria-label="Alert engine">
      <StatusPill
        label={!eng ? 'Engine not running' : ageS !== null && ageS > 30 ? `Engine silent ${ageS}s` : 'Engine running'}
        tone={engineTone}
        detail={status?.engine_detail ?? eng?.bus_error ?? undefined}
      />
      <span>
        Stream: <Badge tone={stream === 'live' ? 'ok' : 'warn'}>{stream}</Badge>
      </span>
      <span>
        Notifications: <Badge tone={n?.state === 'available' ? 'ok' : 'warn'} title={n?.detail ?? undefined}>{n?.state ?? 'unknown'}</Badge>
      </span>
      <span>Subscribed keys: {eng ? eng.keys.length : <NoValue reason={status?.engine_reason ?? 'NO_DATA'} detail={status?.engine_detail} />}</span>
      <span>Unread: {unread ?? <NoValue reason="PENDING" />}</span>
      {eng?.bus_error ? <span className="alr-warn">{eng.bus_error}</span> : null}
    </div>
  );
}

const eventSource = (e: AlertEvent): ExportSource =>
  e.kind === 'news' || e.kind === 'filing'
    ? { kind: 'licensed', source: `news:${String(e.trigger.source ?? '')}` }
    : e.kind === 'time'
      ? OWNER
      : { kind: 'licensed', source: `marketdata:${String(e.trigger.source ?? 'UNKNOWN').toUpperCase()}`, realtime: e.trigger.data_type === 'REALTIME' };

function inboxExport(events: readonly AlertEvent[]): ExportTable<AlertEvent> {
  return {
    title: 'Alert inbox',
    rows: events,
    columns: [
      { id: 'id', header: 'Alert', kind: 'decimal', value: (e) => String(e.event_id) },
      { id: 'fired', header: 'Fired', kind: 'datetime', value: (e) => e.fired_ns },
      { id: 'rule', header: 'Rule', value: (e) => e.title },
      { id: 'kind', header: 'Kind', value: (e) => e.kind },
      { id: 'condition', header: 'Condition', value: (e) => describeRule(e.rule) },
      { id: 'observed', header: 'Observed', value: (e) => (e.trigger.observed ?? null) as string | null, source: eventSource },
      { id: 'threshold', header: 'Threshold', value: (e) => (e.trigger.threshold ?? null) as string | null, reason: () => 'NOT_APPLICABLE' },
      { id: 'source', header: 'Data source', value: (e) => (e.trigger.source ?? null) as string | null },
      { id: 'type', header: 'Data type', value: (e) => (e.trigger.data_type ?? null) as string | null },
      { id: 'delivery', header: 'Notification', value: (e) => e.notify_status },
      { id: 'ack', header: 'Acknowledged', kind: 'datetime', value: (e) => e.ack_ns, reason: () => 'PENDING' },
    ],
    provenance: { source: 'Alert inbox: GET /api/alerts/inbox (local alert store)', asOf: nsToIso(String(BigInt(Date.now()) * 1_000_000n)), notes: ['observed values carry their data source and are gated by its rights'] },
  };
}

function Inbox({ events, loading, onAck }: { events: AlertEvent[]; loading: boolean; onAck: (ids: number[] | null) => void }) {
  const cols: Column<AlertEvent>[] = [
    { id: 'fired', header: 'Fired (UTC)', cell: (e) => <Timestamp ns={e.fired_ns} />, sortValue: (e) => e.fired_ns, sortKind: 'number' },
    {
      id: 'rule',
      header: 'Alert',
      wrap: true,
      cell: (e) => (
        <span className="alr-cell">
          <strong>{e.title}</strong>
          <span className="muted">{describeRule(e.rule)}</span>
        </span>
      ),
    },
    {
      id: 'what',
      header: 'Observed',
      wrap: true,
      cell: (e) => (
        <span className="alr-cell">
          <span>{triggerSummary(e)}</span>
          {e.trigger.label ? <Badge tone="warn">{e.trigger.label}</Badge> : null}
          {e.trigger.link ? (
            <a href={String(e.trigger.link)} target="_blank" rel="noreferrer noopener">
              source document
            </a>
          ) : null}
        </span>
      ),
    },
    {
      id: 'delivery',
      header: 'Notification',
      cell: (e) => <StatusPill label={e.notify_status} tone={e.notify_status === 'delivered' ? 'ok' : e.notify_status === 'failed' ? 'bad' : 'warn'} detail={e.notify_detail ?? undefined} compact />,
    },
    {
      id: 'ack',
      header: 'Read',
      cell: (e) =>
        e.ack_ns ? (
          <span className="muted">read</span>
        ) : (
          <button type="button" className="btn small" onClick={() => onAck([e.event_id])}>
            Mark read
          </button>
        ),
    },
  ];
  const unread = events.filter((e) => !e.ack_ns).length;
  return (
    <section className="panel" aria-labelledby="alr-inbox-h">
      <header>
        <h2 id="alr-inbox-h">Inbox</h2>
        <span className="muted">
          {events.length} alert(s), {unread} unread
        </span>
        <button type="button" className="btn small" disabled={!unread} onClick={() => onAck(null)}>
          Mark all read
        </button>
        <ExportButton disabled={!events.length} table={() => inboxExport(events)} />
      </header>
      <div className="panel-body flush">
        <DataTable caption="Fired alerts" captionHidden columns={cols} rows={events} rowKey={(e) => String(e.event_id)} emptyText={loading ? 'Loading…' : 'No alert has fired yet.'} />
      </div>
    </section>
  );
}

function Rules({ rows, nowNs, act }: { rows: RuleRow[]; nowNs: number; act: (path: string, body: unknown) => Promise<void> }) {
  const base = (r: RuleRow) => `/api/alerts/rules/${encodeURIComponent(r.rule_id)}`;
  const cols: Column<RuleRow>[] = [
    {
      id: 'rule',
      header: 'Rule',
      wrap: true,
      cell: (r) => (
        <span className="alr-cell">
          <strong>{r.rule.name}</strong>
          <span className="muted">{describeRule(r.rule)}</span>
          <span className="muted">
            {r.rule.repeat === 'once' ? 'once' : 're-arms'} · {r.rule.allow_delayed ? 'delayed data allowed (labelled)' : 'real-time only'}
          </span>
        </span>
      ),
    },
    {
      id: 'state',
      header: 'State',
      cell: (r) => {
        const s = ruleState(r, nowNs);
        return (
          <span className="alr-cell">
            <StatusPill label={s.label} tone={s.tone} detail={s.detail ?? undefined} compact />
            {r.snooze_until_ns && r.snooze_until_ns > nowNs ? (
              <span className="muted">
                until <Timestamp ns={r.snooze_until_ns} />
              </span>
            ) : null}
            {s.detail ? <span className="muted">{s.detail}</span> : null}
          </span>
        );
      },
    },
    { id: 'n', header: 'Triggers', numeric: true, cell: (r) => r.triggers, sortValue: (r) => r.triggers, sortKind: 'number' },
    { id: 'last', header: 'Last trigger (UTC)', cell: (r) => (r.last_trigger_ns ? <Timestamp ns={r.last_trigger_ns} /> : <NoValue reason="NOT_APPLICABLE" detail="has not fired" />) },
    {
      id: 'actions',
      header: 'Actions',
      cell: (r) => (
        <span className="alr-actions">
          {r.status === 'active' ? (
            <button type="button" className="btn small" onClick={() => void act(`${base(r)}/pause`, {})}>
              Pause
            </button>
          ) : (
            <button type="button" className="btn small" onClick={() => void act(`${base(r)}/resume`, {})}>
              Resume
            </button>
          )}
          <select
            aria-label={`Snooze ${r.rule.name}`}
            className="input"
            value=""
            onChange={(e) => {
              const v = e.target.value;
              if (v === 'clear') void act(`${base(r)}/snooze`, { clear: true });
              else if (v) void act(`${base(r)}/snooze`, { minutes: Number(v) });
            }}
          >
            <option value="">Snooze…</option>
            {SNOOZE_CHOICES.map((c) => (
              <option key={c.minutes} value={c.minutes}>
                {c.label}
              </option>
            ))}
            {r.snooze_until_ns ? <option value="clear">End snooze</option> : null}
          </select>
          <button
            type="button"
            className="btn ghost small"
            onClick={() => {
              if (window.confirm(`Delete the alert rule "${r.rule.name}"? Its history stays in the audit trail.`)) void act(`${base(r)}/delete`, {});
            }}
          >
            Delete
          </button>
        </span>
      ),
    },
  ];
  return (
    <>
      <NewRule onCreate={(rule) => act('/api/alerts/rules', { rule })} />
      <section className="panel" aria-labelledby="alr-rules-h">
        <header>
          <h2 id="alr-rules-h">Rules</h2>
          <span className="muted">{rows.length} rule(s)</span>
        </header>
        <div className="panel-body flush">
          <DataTable caption="Alert rules" captionHidden columns={cols} rows={rows} rowKey={(r) => r.rule_id} emptyText="No alert rules yet. Create one above." />
        </div>
      </section>
    </>
  );
}

function Field({ id, label, error, children }: { id: string; label: string; error?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {error ? (
        <span className="alr-field-error" id={`${id}-err`}>
          {error}
        </span>
      ) : null}
    </div>
  );
}

function NewRule({ onCreate }: { onCreate: (rule: unknown) => Promise<void> }) {
  const linked = useLinkedSecurity();
  const [f, setF] = useState<RuleForm>(() => emptyForm(linked));
  const [errors, setErrors] = useState<FormErrors>({});
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof RuleForm>(k: K, v: RuleForm[K]) => setF((x) => ({ ...x, [k]: v }));
  const market = f.kind === 'price_cross' || f.kind === 'pct_move' || f.kind === 'volume' || f.kind === 'study';
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const { rule, errors: errs } = buildRule(f);
    setErrors(errs);
    if (!rule) return;
    setBusy(true);
    try {
      await onCreate(rule);
      setF(emptyForm(linked));
    } finally {
      setBusy(false);
    }
  };
  const input = (k: keyof RuleForm, label: string, placeholder = '') => (
    <Field id={`alr-${k}`} label={label} error={errors[k]}>
      <input
        id={`alr-${k}`}
        className="input"
        value={String(f[k])}
        placeholder={placeholder}
        aria-invalid={errors[k] ? true : undefined}
        aria-describedby={errors[k] ? `alr-${k}-err` : undefined}
        onChange={(e) => set(k, e.target.value as never)}
        spellCheck={false}
        autoComplete="off"
      />
    </Field>
  );
  const select = <K extends keyof RuleForm>(k: K, label: string, options: readonly (readonly [string, string])[]) => (
    <Field id={`alr-${k}`} label={label}>
      <select id={`alr-${k}`} className="input" value={String(f[k])} onChange={(e) => set(k, e.target.value as RuleForm[K])}>
        {options.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
    </Field>
  );
  const directions = [
    ['either', 'either way'],
    ['up', 'up'],
    ['down', 'down'],
  ] as const;
  return (
    <section className="panel" aria-labelledby="alr-new-h">
      <header>
        <h2 id="alr-new-h">New rule</h2>
      </header>
      <form className="panel-body alr-form" onSubmit={(e) => void submit(e)}>
        {select('kind', 'When', RULE_KINDS.map((k) => [k.id, k.label] as const))}
        {market ? input('key', 'Security', 'AAPL.XNAS') : null}
        {f.kind === 'price_cross' ? (
          <>
            {select('field', 'Price', [
              ['last', 'last'],
              ['bid', 'bid'],
              ['ask', 'ask'],
              ['mid', 'mid'],
            ])}
            {input('level', 'Level', '200.00')}
          </>
        ) : null}
        {f.kind === 'pct_move' ? input('threshold', 'Move (%)', '5') : null}
        {f.kind === 'volume' ? input('threshold', 'Volume', '1000000') : null}
        {f.kind === 'study' ? (
          <>
            {select('study', 'Study', [
              ['rsi', 'RSI'],
              ['sma', 'SMA'],
              ['ema', 'EMA'],
            ])}
            {input('n', 'Length', '14')}
            {select('compare', 'Compare', [
              ['value', 'study value with a level'],
              ['price', 'last price with the study'],
            ])}
            {f.compare === 'value' ? input('level', 'Level', '70') : null}
          </>
        ) : null}
        {f.kind === 'price_cross' || f.kind === 'pct_move' || f.kind === 'study' ? select('direction', 'Direction', directions) : null}
        {f.kind === 'price_cross' || f.kind === 'study' ? input('hysteresis', 'Hysteresis', '0') : null}
        {f.kind === 'time' ? (
          <Field id="alr-at" label="At (your local time)" error={errors.at}>
            <input id="alr-at" className="input" type="datetime-local" value={f.at} onChange={(e) => set('at', e.target.value)} />
          </Field>
        ) : null}
        {f.kind === 'news' || f.kind === 'filing' ? (
          <>
            {input('issuer', 'Issuer (CIK or ticker)', 'AAPL')}
            {input('forms', 'Forms (optional)', '8-K, 10-Q')}
          </>
        ) : null}
        {input('name', 'Name (optional)')}
        {f.kind !== 'time' ? select('repeat', 'Repeat', [
          ['rearm', 'every time it re-arms'],
          ['once', 'once, then retire'],
        ]) : null}
        {market ? (
          <label className="alr-check">
            <input type="checkbox" checked={!f.allowDelayed} onChange={(e) => set('allowDelayed', !e.target.checked)} /> Real-time data only
          </label>
        ) : null}
        <button type="submit" className="btn primary" disabled={busy}>
          {busy ? 'Saving…' : 'Create rule'}
        </button>
      </form>
    </section>
  );
}

function Audit({ entries, loading }: { entries: AuditEntry[]; loading: boolean }) {
  const cols: Column<AuditEntry>[] = [
    { id: 'ts', header: 'Time (UTC)', cell: (a) => <Timestamp ns={a.ts_ns} />, sortValue: (a) => a.ts_ns, sortKind: 'number' },
    { id: 'actor', header: 'By', cell: (a) => a.actor },
    { id: 'action', header: 'Action', cell: (a) => <span className="cmd-id">{a.action}</span> },
    { id: 'rule', header: 'Rule', cell: (a) => a.rule_id ?? <NoValue reason="NOT_APPLICABLE" /> },
    { id: 'detail', header: 'Detail', wrap: true, cell: (a) => <code className="alr-detail">{JSON.stringify(a.detail)}</code> },
  ];
  return (
    <section className="panel" aria-labelledby="alr-audit-h">
      <header>
        <h2 id="alr-audit-h">Audit trail</h2>
        <span className="muted">append-only: rule changes, triggers, suppressed triggers, deliveries and windows not evaluated</span>
      </header>
      <div className="panel-body flush">
        <DataTable caption="Alert audit trail" captionHidden columns={cols} rows={entries} rowKey={(a) => String(a.audit_id)} emptyText={loading ? 'Loading…' : 'No audit entries.'} />
      </div>
    </section>
  );
}
