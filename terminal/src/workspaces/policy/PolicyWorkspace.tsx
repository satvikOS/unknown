/**
 * Policy (ADR-0007, ADR-0013 R13): published policy versions, their bodies with units, field-by-field
 * comparison, a JSON editor that publishes a new version (the core validates the schema), and
 * activation for the account. Publishing and activation are step-up owner commands; nothing is
 * applied locally.
 *
 * Routes: #/policy, #/policy/<hash>, #/policy/compare/<a>/<b>, #/policy/draft[/<base hash>].
 */
import { useEffect, useRef, useState } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import type { EventRecord, PolicyRecord, Session } from '../../api/types';
import { useAccountKey } from '../../controls/ControlProvider';
import { useQuery } from '../../state/react';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { sessionStateTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import type { WorkspaceProps } from '../types';
import { ActivateDialog } from './ActivateDialog';
import { FIELDS, impliedRequiredReturn, SECTIONS, sectionOf } from './fields';
import { flatten } from './diff';
import { prettyJson } from './jsonCheck';
import { ACTIVATION_EFFECTS, newestFirst, parsePolicyRoute, policyByHash, type PolicyView } from './model';
import { FieldList } from './PolicyParts';
import { PolicyCompare } from './PolicyCompare';
import { PolicyDraft } from './PolicyDraft';
import { ActivateButton, PolicyName, ViewHeading, type PolicyContext } from './shared';
import '../risk/risk.css';

function viewKey(v: PolicyView): string {
  return v.kind === 'detail' ? `detail:${v.hash}` : v.kind;
}

const TABS: { kind: PolicyView['kind'][]; label: string; href: string }[] = [
  { kind: ['list', 'detail'], label: 'Published policies', href: routeHref('policy') },
  { kind: ['compare'], label: 'Compare', href: routeHref('policy', 'compare') },
  { kind: ['draft'], label: 'New version', href: routeHref('policy', 'draft') },
];

export function PolicyWorkspace({ route, navigate }: WorkspaceProps) {
  const view = parsePolicyRoute(route.rest);
  const policies = useQuery('/api/policies');
  const active = useQuery('/api/policy/active');
  const published = useQuery('/api/events', { type: 'PolicyPublished', limit: 2000 });
  const accountKey = useAccountKey();
  const [activating, setActivating] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Moving between policy views keeps focus predictable: the new view's heading.
  const key = viewKey(view);
  const firstKey = useRef(key);
  useEffect(() => {
    if (key !== firstKey.current) headingRef.current?.focus();
    firstKey.current = '';
  }, [key]);

  const publishedBy = new Map<string, EventRecord>();
  for (const e of published.data ?? []) {
    const h = e.info.policy_hash;
    if (typeof h === 'string' && !publishedBy.has(h)) publishedBy.set(h, e);
  }
  const ctx: PolicyContext = {
    policies: policies.data,
    active: active.data,
    publishedBy,
    accountKey,
    navigate,
    activate: setActivating,
    headingRef,
  };
  const activatingPolicy = policyByHash(policies.data, activating);

  return (
    <WorkspaceFrame
      title="Policy"
      description="Published policy versions, their limits with units, comparison, new versions and activation for the account."
      queries={[policies, active, published]}
    >
      <div className="rk-ws">
        <QueryError what="Published policies" error={policies.error} />
        <QueryError what="The active policy" error={active.error} />
        <QueryError what="Publication records" error={published.error} />

        <nav aria-label="Policy views">
          <ul className="rk-subnav">
            {TABS.map((t) => (
              <li key={t.label}>
                <a href={t.href} aria-current={t.kind.includes(view.kind) ? 'page' : undefined}>
                  {t.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="callout" style={{ marginBottom: 12 }}>
          <strong>How changes apply.</strong> {ACTIVATION_EFFECTS[0]} Activation applies to future sessions; existing mandates are unchanged.{' '}
          <span className="muted">{ACTIVATION_EFFECTS[3]}</span>
        </div>

        {view.kind === 'list' ? <PolicyList ctx={ctx} /> : null}
        {view.kind === 'detail' ? <PolicyDetail ctx={ctx} hash={view.hash} loading={policies.status === 'loading'} /> : null}
        {view.kind === 'compare' ? <PolicyCompare ctx={ctx} a={view.a} b={view.b} /> : null}
        {view.kind === 'draft' ? <PolicyDraft ctx={ctx} baseHash={view.base} /> : null}

        {activatingPolicy && accountKey ? (
          <ActivateDialog policy={activatingPolicy} active={active.data ?? null} accountKey={accountKey} onClose={() => setActivating(null)} />
        ) : null}
      </div>
    </WorkspaceFrame>
  );
}

// ------------------------------------------------------------------ list

function PolicyList({ ctx }: { ctx: PolicyContext }) {
  const activations = useQuery(ctx.accountKey ? '/api/events' : null, { type: 'PolicyActivated', account: ctx.accountKey ?? undefined, limit: 2000 });
  const rows = newestFirst(ctx.policies ?? []);
  const activeHash = ctx.active?.policy_hash;
  const nameOf = (hash: unknown) => (typeof hash === 'string' ? policyByHash(ctx.policies, hash)?.name : undefined);

  const columns: Column<PolicyRecord>[] = [
    {
      id: 'name',
      header: 'Name',
      sortValue: (p) => p.name,
      cell: (p) => (
        <a href={routeHref('policy', p.policy_hash)}>
          <PolicyName policy={p} activeHash={activeHash} />
        </a>
      ),
    },
    { id: 'hash', header: 'Policy hash', sortValue: (p) => p.policy_hash, cell: (p) => <ShortId value={p.policy_hash} head={16} /> },
    { id: 'mode', header: 'Mode', sortValue: (p) => p.body.mode, cell: (p) => <span className="mono">{p.body.mode}</span> },
    { id: 'ccy', header: 'Currency', sortValue: (p) => p.body.currency, cell: (p) => <span className="mono">{p.body.currency}</span> },
    { id: 'by', header: 'Published by', sortValue: (p) => p.published_by, cell: (p) => p.published_by },
    {
      id: 'at',
      header: 'Published at',
      sortKind: 'number',
      sortValue: (p) => p.published_at_ns,
      cell: (p) => <Timestamp ns={p.published_at_ns} nsStr={nsStrOf(p, 'published_at_ns')} />,
    },
    {
      id: 'cmd',
      header: 'Publish command',
      cell: (p) => {
        const e = ctx.publishedBy.get(p.policy_hash);
        return e ? <ShortId value={e.command_id} head={22} /> : <span className="muted">not found</span>;
      },
    },
  ];

  const historyColumns: Column<EventRecord>[] = [
    { id: 'at', header: 'Activated at', sortKind: 'number', sortValue: (e) => e.recorded_time_ns, cell: (e) => <Timestamp ns={e.recorded_time_ns} nsStr={nsStrOf(e, 'recorded_time_ns')} /> },
    {
      id: 'policy',
      header: 'Policy',
      sortValue: (e) => String(e.info.policy_hash ?? ''),
      cell: (e) => {
        const h = typeof e.info.policy_hash === 'string' ? e.info.policy_hash : null;
        return h ? (
          <a href={routeHref('policy', h)}>
            {nameOf(h) ?? 'unknown'} <ShortId value={h} head={12} />
          </a>
        ) : (
          <span className="muted">not recorded</span>
        );
      },
    },
    { id: 'by', header: 'By', sortValue: (e) => e.principal_id, cell: (e) => e.principal_id },
    { id: 'cmd', header: 'Command', cell: (e) => <ShortId value={e.command_id} head={24} /> },
    { id: 'seq', header: 'Event seq', numeric: true, sortKind: 'number', sortValue: (e) => e.seq, cell: (e) => e.seq },
  ];

  return (
    <>
      <section className="panel" aria-labelledby="policy-view-title">
        <ViewHeading
          ctx={ctx}
          aside={
            <span className="rk-actions">
              <a className="btn small" href={routeHref('policy', 'draft')}>
                New version from active…
              </a>
            </span>
          }
        >
          Active for {ctx.accountKey ?? 'this account'}
        </ViewHeading>
        <div className="panel-body">
          {ctx.active ? (
            <dl className="kv">
              <dt>Policy</dt>
              <dd>
                <a href={routeHref('policy', ctx.active.policy_hash)}>{ctx.active.name}</a> <ShortId value={ctx.active.policy_hash} head={16} />
              </dd>
              <dt>Mode and currency</dt>
              <dd className="mono">
                {ctx.active.body.mode} · {ctx.active.body.currency}
              </dd>
              <dt>Activated</dt>
              <dd>
                <Timestamp ns={ctx.active.activation.activated_at_ns} nsStr={nsStrOf(ctx.active.activation, 'activated_at_ns')} /> <span className="muted">by {ctx.active.activation.activated_by}</span>
              </dd>
              <dt>Liquidation on stop</dt>
              <dd>
                <span className="mono">{String(ctx.active.body.on_stop_liquidation)}</span>{' '}
                <span className="muted">a stop never sells holdings; liquidation is not offered</span>
              </dd>
            </dl>
          ) : ctx.active === null ? (
            <p className="muted">No policy is active for this account. Activate a published version below; the core will not start without one.</p>
          ) : (
            <p className="muted">Loading…</p>
          )}
        </div>
      </section>

      <section className="panel section-gap" aria-labelledby="policy-published">
        <header>
          <h2 id="policy-published">Published versions</h2>
          <span className="num muted">{rows.length}</span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption="Published policy versions, newest first"
            captionHidden
            columns={columns}
            rows={rows}
            rowKey={(p) => p.policy_hash}
            onRowActivate={(p) => ctx.navigate('policy', p.policy_hash)}
            activateLabel="Open policy"
            emptyText={ctx.policies === undefined ? 'Loading…' : 'No policy has been published.'}
          />
        </div>
      </section>

      <section className="panel section-gap" aria-labelledby="policy-history">
        <header>
          <h2 id="policy-history">Activation history for {ctx.accountKey ?? 'this account'}</h2>
          <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
            From the hash-chained event log
          </span>
        </header>
        <div className="panel-body flush">
          <QueryError what="Activation history" error={activations.error} />
          <DataTable
            caption="Policy activations for this account"
            captionHidden
            columns={historyColumns}
            rows={activations.data ?? []}
            rowKey={(e) => e.event_id}
            initialSort={{ columnId: 'at', direction: 'descending' }}
            emptyText={activations.status === 'loading' ? 'Loading…' : 'No activations recorded.'}
          />
        </div>
      </section>
    </>
  );
}

// ------------------------------------------------------------------ detail

function PolicyDetail({ ctx, hash, loading }: { ctx: PolicyContext; hash: string; loading: boolean }) {
  const sessions = useQuery('/api/sessions');
  const [copied, setCopied] = useState<string | null>(null);
  const policy = policyByHash(ctx.policies, hash);
  if (!policy) {
    return (
      <section className="panel" aria-labelledby="policy-view-title">
        <ViewHeading ctx={ctx}>Policy {hash.slice(0, 16)}</ViewHeading>
        <div className="panel-body">
          {loading || ctx.policies === undefined ? (
            <p className="muted">Loading…</p>
          ) : (
            <p>
              No published policy has the hash <code className="mono">{hash}</code>. <a href={routeHref('policy')}>Back to published policies</a>.
            </p>
          )}
        </div>
      </section>
    );
  }
  const body = policy.body as Record<string, unknown>;
  const ccy = typeof body.currency === 'string' ? body.currency : '';
  const leaves = [...flatten(body).entries()];
  const known = new Set(FIELDS.map((f) => f.path));
  const event = ctx.publishedBy.get(policy.policy_hash);
  const isActive = ctx.active?.policy_hash === policy.policy_hash;
  const usedBy: Session[] = (sessions.data ?? []).filter((s) => s.mandate?.policy_hash === policy.policy_hash);
  const template = (body.session_template ?? {}) as Record<string, unknown>;
  const required = impliedRequiredReturn(template.seed, template.target_terminal_equity);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(prettyJson(body));
      setCopied('Copied');
    } catch {
      setCopied('Copy failed: the browser refused clipboard access');
    }
  };

  return (
    <>
      <section className="panel" aria-labelledby="policy-view-title">
        <ViewHeading
          ctx={ctx}
          aside={
            <span className="rk-actions">
              {ctx.active && !isActive ? (
                <a className="btn small" href={routeHref('policy', 'compare', ctx.active.policy_hash, policy.policy_hash)}>
                  Compare with active
                </a>
              ) : null}
              <a className="btn small" href={routeHref('policy', 'draft', policy.policy_hash)}>
                New version from this…
              </a>
              <ActivateButton ctx={ctx} policy={policy} />
            </span>
          }
        >
          <span className="rk-name">{policy.name}</span> {isActive ? <StatusPill label="active" tone="ok" compact /> : <StatusPill label="not active" tone="neutral" compact />}
        </ViewHeading>
        <div className="panel-body">
          <dl className="kv">
            <dt>Policy hash</dt>
            <dd className="cmd-id">{policy.policy_hash}</dd>
            <dt>Published</dt>
            <dd>
              <Timestamp ns={policy.published_at_ns} nsStr={nsStrOf(policy, 'published_at_ns')} /> <span className="muted">by {policy.published_by}</span>
            </dd>
            <dt>Publish command</dt>
            <dd>
              {event ? (
                <>
                  <span className="cmd-id">{event.command_id}</span> <span className="muted">event seq {event.seq}</span>
                </>
              ) : (
                <span className="muted">not found in the event log</span>
              )}
            </dd>
            {isActive && ctx.active ? (
              <>
                <dt>Activated</dt>
                <dd>
                  <Timestamp ns={ctx.active.activation.activated_at_ns} nsStr={nsStrOf(ctx.active.activation, 'activated_at_ns')} /> <span className="muted">by {ctx.active.activation.activated_by}</span>
                </dd>
              </>
            ) : null}
            <dt>Sessions created under it</dt>
            <dd>
              {usedBy.length ? (
                <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 6 }}>
                  {usedBy.map((s) => (
                    <span key={s.session_id} style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
                      <WorkspaceLink to="sessions" rest={[s.session_id]}>
                        <ShortId value={s.session_id} head={14} />
                      </WorkspaceLink>
                      <StatusPill label={s.state} tone={sessionStateTone(s.state)} compact />
                    </span>
                  ))}
                </span>
              ) : (
                <span className="muted">{sessions.status === 'loading' ? 'loading' : 'none'}</span>
              )}
            </dd>
          </dl>
        </div>
      </section>

      <div className="grid grid-2 section-gap">
        {SECTIONS.map((sec) => {
          const entries = leaves.filter(([p]) => sectionOf(p) === sec.id);
          if (!entries.length) return null;
          const ordered = [...entries.filter(([p]) => known.has(p)), ...entries.filter(([p]) => !known.has(p))];
          ordered.sort(([a], [b]) => {
            const ia = FIELDS.findIndex((f) => f.path === a);
            const ib = FIELDS.findIndex((f) => f.path === b);
            return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib) || a.localeCompare(b);
          });
          return (
            <section key={sec.id || 'top'} className="panel" aria-labelledby={`policy-sec-${sec.id || 'top'}`}>
              <header>
                <h2 id={`policy-sec-${sec.id || 'top'}`}>{sec.title}</h2>
              </header>
              <div className="panel-body">
                <p className="rk-basis" style={{ margin: '0 0 6px' }}>
                  {sec.help}
                </p>
                <FieldList
                  entries={ordered}
                  currency={ccy}
                  extra={
                    sec.id === 'session_template' ? (
                      <div className="rk-field-row">
                        <dt>
                          Implied required net return
                          <span className="rk-path">derived: (target − seed) ÷ seed</span>
                        </dt>
                        <dd>
                          <span className="mono">{required ?? 'not computable'}</span>
                          <span className="rk-help">What the target asks for after costs. A configured goal, not a forecast.</span>
                        </dd>
                      </div>
                    ) : null
                  }
                />
              </div>
            </section>
          );
        })}
        {(() => {
          const other = leaves.filter(([p]) => !SECTIONS.some((s) => s.id === sectionOf(p)));
          return other.length ? (
            <section className="panel" aria-labelledby="policy-sec-other">
              <header>
                <h2 id="policy-sec-other">Other fields</h2>
              </header>
              <div className="panel-body">
                <FieldList entries={other} currency={ccy} />
              </div>
            </section>
          ) : null;
        })()}
      </div>

      <section className="panel section-gap" aria-labelledby="policy-json">
        <header>
          <h2 id="policy-json">Body as published (JSON)</h2>
          <span className="rk-actions">
            {copied ? (
              <span className="muted" role="status">
                {copied}
              </span>
            ) : null}
            <button type="button" className="btn small" onClick={() => void copy()}>
              Copy JSON
            </button>
          </span>
        </header>
        <pre className="rk-json" tabIndex={0} aria-label="Policy body JSON">
          {prettyJson(body)}
        </pre>
      </section>
    </>
  );
}
