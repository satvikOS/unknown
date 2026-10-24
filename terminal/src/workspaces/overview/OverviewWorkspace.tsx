/**
 * Overview (book Ch. 22): mode, account health, capital, exposure and stop state. Every figure comes
 * from GET /api/overview (and the active policy for limits); nothing is computed in floating point.
 */
import { exactJson, nsStrOf } from '../../api/nsStrings';
import type { Bucket, ExceptionCase, Position, SessionSummary } from '../../api/types';
import { useControl } from '../../controls/ControlProvider';
import { entryState } from '../../controls/model';
import { useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, Badge, FixtureRefused, Money, Qty, ShortId, StatusPill, Timestamp, YesNo } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { reconciliationTone, sessionStateTone, severityTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import type { WorkspaceProps } from '../types';

const ACTIVE_SESSION_STATES = new Set(['RUNNING', 'SUSPENDED', 'WINDING_DOWN', 'RECONCILING', 'FAILED_REVIEW']);

function ownerLabel(b: { owner: string; system_owned?: boolean }) {
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <span className="cmd-id">{b.owner}</span>
      {b.system_owned === false ? (
        <Badge title="Cash outside the system: not authorized for trading">outside</Badge>
      ) : b.system_owned ? (
        <Badge tone="info" title="Owned by the system (unallocated, a session or protected proceeds)">system</Badge>
      ) : null}
    </span>
  );
}

const bucketColumns: Column<Bucket>[] = [
  { id: 'owner', header: 'Owner bucket', cell: ownerLabel, sortValue: (b) => b.owner },
  {
    id: 'settled',
    header: 'Settled cash',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.settled_cash,
    cell: (b) => <Money amount={b.settled_cash} currency={b.currency} kind="settled" />,
  },
  {
    id: 'recv',
    header: 'Receivable (unsettled)',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.receivable_unsettled,
    cell: (b) => <Money amount={b.receivable_unsettled} currency={b.currency} kind="unsettled" />,
  },
  {
    id: 'pay',
    header: 'Payable (unsettled)',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.payable_unsettled,
    cell: (b) => <Money amount={b.payable_unsettled} currency={b.currency} kind="unsettled" />,
  },
  {
    id: 'fees',
    header: 'Fees accrued',
    description: 'Accrued fees not yet paid (negative reduces liquid value)',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.fees_accrued,
    cell: (b) => <Money amount={b.fees_accrued} currency={b.currency} kind="estimated" />,
  },
  {
    id: 'liquid',
    header: 'Liquid value',
    description: 'Settled cash + receivable + payable + fees accrued',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.liquid_value,
    cell: (b) => <Money amount={b.liquid_value} currency={b.currency} />,
  },
  {
    id: 'holds',
    header: 'Held for orders',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.holds,
    cell: (b) => <Money amount={b.holds} currency={b.currency} kind="held" />,
  },
  {
    id: 'avail',
    header: 'Available cash',
    description: 'Settled cash less active holds: what new orders in this bucket may reserve',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.available_cash,
    cell: (b) => <Money amount={b.available_cash} currency={b.currency} kind="available" />,
  },
  {
    id: 'inv',
    header: 'Inventory at cost',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.inventory_cost,
    cell: (b) => <Money amount={b.inventory_cost} currency={b.currency} kind="cost" />,
  },
];

function positionColumns(fetchedAtMs: number | null): Column<Position>[] {
  return [
    { id: 'owner', header: 'Owner', cell: (p) => <span className="cmd-id">{p.owner}</span>, sortValue: (p) => p.owner },
    {
      id: 'inst',
      header: 'Instrument',
      sortValue: (p) => p.symbol ?? p.instrument_id,
      cell: (p) => (
        <WorkspaceLink to="instruments" rest={[p.instrument_id]}>
          <strong>{p.symbol ?? p.instrument_id}</strong> <span className="muted">{p.instrument_id}</span>
        </WorkspaceLink>
      ),
    },
    { id: 'qty', header: 'Quantity', numeric: true, sortKind: 'decimal', sortValue: (p) => p.quantity, cell: (p) => <Qty value={p.quantity} /> },
    { id: 'lots', header: 'Lots', numeric: true, sortKind: 'number', sortValue: (p) => p.lots, cell: (p) => p.lots },
    {
      id: 'cost',
      header: 'Cost basis',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.cost_basis,
      cell: (p) => <Money amount={p.cost_basis} currency={p.currency} kind="cost" />,
    },
    {
      id: 'mark',
      header: 'Mark (bid)',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.mark,
      cell: (p) => (p.mark_fixture ? <FixtureRefused detail={p.mark_basis} /> : <Money amount={p.mark} currency={p.currency} kind="marked" unavailable="No quote for this instrument." />),
    },
    {
      id: 'mv',
      header: 'Market value',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.market_value,
      cell: (p) => (p.mark_fixture ? <FixtureRefused /> : <Money amount={p.market_value} currency={p.currency} kind="marked" unavailable="No quote for this instrument." />),
    },
    {
      id: 'unreal',
      header: 'Unrealized P&L',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.unrealized,
      cell: (p) => (p.mark_fixture ? <FixtureRefused /> : <Money amount={p.unrealized} currency={p.currency} kind="unrealized" signed unavailable="No quote for this instrument." />),
    },
    {
      id: 'basis',
      header: 'Mark source and age',
      wrap: true,
      cell: (p) => (
        <span>
          {p.mark_basis}
          {fetchedAtMs !== null ? (
            <span className="muted">
              {' '}
              · read <Age ageMs={0} observedAtMs={fetchedAtMs} /> ago
            </span>
          ) : null}
        </span>
      ),
    },
  ];
}

function sessionColumns(): Column<SessionSummary>[] {
  return [
    {
      id: 'id',
      header: 'Session',
      sortValue: (s) => s.session_id,
      cell: (s) => (
        <WorkspaceLink to="sessions" rest={[s.session_id]}>
          <span className="cmd-id">{s.session_id}</span>
        </WorkspaceLink>
      ),
    },
    {
      id: 'state',
      header: 'State',
      sortValue: (s) => s.state,
      cell: (s) => <StatusPill label={s.state} tone={sessionStateTone(s.state)} compact />,
    },
    { id: 'outcome', header: 'Outcome', sortValue: (s) => s.outcome, cell: (s) => s.outcome ?? <span className="muted">none yet</span> },
    { id: 'seed', header: 'Seed', numeric: true, sortKind: 'decimal', sortValue: (s) => s.seed, cell: (s) => <Money amount={s.seed} currency={s.currency} /> },
    {
      id: 'target',
      header: 'Target terminal equity',
      description: 'A configured mandate, not a forecast',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (s) => s.target,
      cell: (s) => <Money amount={s.target} currency={s.currency} />,
    },
    {
      id: 'funding',
      header: 'Funding',
      wrap: true,
      cell: (s) =>
        s.funding ? (
          <span>
            <StatusPill label={s.funding.status} tone={s.funding.status === 'FUNDED' ? 'ok' : 'warn'} compact />
            {s.funding.reason ? <span className="muted"> {s.funding.reason}</span> : null}
            {s.funding.shortfall ? (
              <>
                {' '}
                shortfall <Money amount={s.funding.shortfall} currency={s.currency} />
              </>
            ) : null}
          </span>
        ) : (
          <span className="muted">not evaluated</span>
        ),
    },
    {
      id: 'protected',
      header: 'Protected value',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (s) => s.protected_value,
      cell: (s) => <Money amount={s.protected_value} currency={s.currency} kind="protected" unavailable="none protected" />,
    },
    {
      id: 'blockers',
      header: 'Blockers',
      wrap: true,
      cell: (s) => (s.blockers && s.blockers.length ? <code className="mono">{s.blockers.join(', ')}</code> : <span className="muted">none</span>),
    },
    {
      id: 'pred',
      header: 'Predecessor',
      cell: (s) =>
        s.predecessor_id ? (
          <WorkspaceLink to="sessions" rest={[s.predecessor_id]}>
            <ShortId value={s.predecessor_id} head={14} />
          </WorkspaceLink>
        ) : (
          <span className="muted">none</span>
        ),
    },
  ];
}

const exceptionColumns: Column<ExceptionCase>[] = [
  { id: 'case', header: 'Case', sortValue: (e) => e.case_id, cell: (e) => <ShortId value={e.case_id} head={16} /> },
  { id: 'kind', header: 'Kind', sortValue: (e) => e.kind, cell: (e) => <code className="mono">{e.kind}</code> },
  {
    id: 'sev',
    header: 'Severity',
    sortValue: (e) => ['low', 'medium', 'high', 'critical'].indexOf(e.severity),
    sortKind: 'number',
    cell: (e) => <StatusPill label={e.severity} tone={severityTone(e.severity)} compact />,
  },
  {
    id: 'blocking',
    header: 'Blocks entries',
    sortValue: (e) => e.blocking,
    cell: (e) => <YesNo value={e.blocking} goodWhen={false} />,
  },
  { id: 'opened', header: 'Opened', sortKind: 'number', sortValue: (e) => e.opened_at_ns, cell: (e) => <Timestamp ns={e.opened_at_ns} nsStr={nsStrOf(e, 'opened_at_ns')} /> },
  {
    id: 'ref',
    header: 'Session / order',
    cell: (e) =>
      e.session_id || e.order_id ? (
        <span className="cmd-id">{[e.session_id, e.order_id].filter(Boolean).join(' / ')}</span>
      ) : (
        <span className="muted">account</span>
      ),
  },
  {
    id: 'detail',
    header: 'Detail',
    wrap: true,
    cell: (e) => <span className="cmd-id">{exactJson(e.detail).slice(0, 220)}</span>,
  },
];

export function OverviewWorkspace(_props: WorkspaceProps) {
  const overview = useQuery('/api/overview', undefined, { pollMs: 5000 });
  const policy = useQuery('/api/policy/active');
  const { openDrawer } = useControl();
  const o = overview.data;
  const control = o?.control ?? null;
  const risk = o?.risk ?? null;
  const state = entryState(control, o);
  const limits = policy.data?.body.account_limits;
  const ccy = risk?.currency ?? o?.account?.base_currency ?? policy.data?.body.currency ?? '';
  const sessions = o?.sessions ?? [];
  const activeSessions = sessions.filter((s) => ACTIVE_SESSION_STATES.has(s.state)).length;
  const exceptions = o?.exceptions_open ?? [];
  const blockingExceptions = exceptions.filter((e) => e.blocking).length;
  const rec = o?.last_reconciliation ?? null;

  return (
    <WorkspaceFrame
      title="Overview"
      description="Account control state, capital, exposure and sessions, read from the core’s projections."
      queries={[overview, policy]}
    >
      <QueryError what="The overview" error={overview.error} />
      {o && !o.account ? (
        <div className="callout" style={{ ['--tone' as string]: 'var(--warn)' }}>
          No account is registered in this database yet. Register one with <code className="mono">python -m unknown.cli bootstrap-sim</code> (simulation)
          or through the owner commands.
        </div>
      ) : null}

      <div className="grid grid-4">
        <section className="panel" aria-labelledby="ov-control">
          <header>
            <h2 id="ov-control">Control</h2>
            <StatusPill label={state.label} tone={state.tone} />
          </header>
          <div className="panel-body">
            <dl className="kv">
              <dt>Stop</dt>
              <dd>
                {control ? (
                  control.stop_latched ? (
                    <>
                      <StatusPill label="latched" tone="bad" compact /> {control.latch_reason ?? ''}
                    </>
                  ) : (
                    <StatusPill label="not latched" tone="ok" compact />
                  )
                ) : (
                  <span className="muted">unknown</span>
                )}
              </dd>
              {control?.stop_latched ? (
                <>
                  <dt>Latch command</dt>
                  <dd className="cmd-id">{control.latch_command_id ?? 'not recorded'}</dd>
                  <dt>Latched</dt>
                  <dd>
                    <Timestamp ns={control.latched_at_ns} nsStr={nsStrOf(control, 'latched_at_ns')} /> <span className="muted">by {control.latched_by ?? 'unknown'}</span>
                  </dd>
                </>
              ) : null}
              <dt>Resume hold</dt>
              <dd>{control ? (control.resume_requires_owner ? `owner required (${control.resume_reason ?? 'no reason'})` : 'none') : <NoValue reason="NOT_REPORTED" detail="The core has not reported its control state." />}</dd>
              <dt>Reconciliation</dt>
              <dd>{control ? (control.needs_reconciliation ? 'required' : `current (epoch ${control.reconciled_epoch})`) : <NoValue reason="NOT_REPORTED" detail="The core has not reported its control state." />}</dd>
              <dt>Broker (recorded)</dt>
              <dd>
                {control ? (
                  <StatusPill
                    label={control.broker_connected ? (control.broker_authenticated ? 'connected' : 'not authenticated') : 'disconnected'}
                    tone={control.broker_connected && control.broker_authenticated ? 'ok' : 'bad'}
                    compact
                  />
                ) : (
                  <NoValue reason="NOT_REPORTED" detail="The core has not reported its control state." />
                )}
              </dd>
              <dt>Last start</dt>
              <dd>
                {control?.last_start ? (
                  <>
                    <Timestamp ns={control.last_start.at_ns} nsStr={nsStrOf(control.last_start, 'at_ns')} /> <span className="cmd-id">{control.last_start.command_id}</span>
                  </>
                ) : (
                  <span className="muted">never</span>
                )}
              </dd>
            </dl>
          </div>
          <div className="panel-note">
            <button type="button" className="btn small" onClick={(e) => openDrawer(state.startApplies ? 'start' : 'stop', e.currentTarget)}>
              {state.startApplies ? 'Inspect blockers' : 'Stop progress'}
            </button>
          </div>
        </section>

        <section className="panel" aria-labelledby="ov-risk">
          <header>
            <h2 id="ov-risk">Capital and loss budget</h2>
            {risk ? (
              risk.breach ? (
                <StatusPill label={`breach: ${risk.breach}`} tone="bad" compact />
              ) : (
                <StatusPill label="within budget" tone="ok" compact />
              )
            ) : null}
          </header>
          <div className="panel-body">
            {risk ? (
              <dl className="kv">
                <dt>Authorized capital</dt>
                <dd>
                  <Money amount={risk.authorized_total} currency={risk.currency} kind="authorized" />
                  {limits?.authorized_capital !== undefined ? (
                    <span className="muted">
                      {' '}
                      of <Money amount={String(limits.authorized_capital)} currency={ccy} /> limit
                    </span>
                  ) : null}
                </dd>
                <dt title="System value marked to bid minus authorized capital, as the core last recorded it">Cumulative P&amp;L (recorded)</dt>
                <dd>
                  <Money amount={risk.cumulative_pnl} currency={risk.currency} signed />{' '}
                  <span className="muted">
                    realized + unrealized, marked to bid, as last recorded by the core. The core records it when a loss input changes, not on
                    every valuation; <WorkspaceLink to="risk">Risk</WorkspaceLink> shows an estimate at current marks.
                  </span>
                </dd>
                <dt>P&amp;L high-water</dt>
                <dd>
                  <Money amount={risk.pnl_high_water} currency={risk.currency} signed />
                </dd>
                <dt>Day start P&amp;L</dt>
                <dd>
                  <Money amount={risk.day_start_pnl} currency={risk.currency} signed /> <span className="muted">{risk.risk_day ?? 'no risk day'}</span>
                </dd>
                {limits ? (
                  <>
                    <dt>Loss triggers</dt>
                    <dd>
                      daily <Money amount={String(limits.daily_loss_trigger)} currency={ccy} />, from high-water{' '}
                      <Money amount={String(limits.cumulative_loss_allowance)} currency={ccy} />
                    </dd>
                  </>
                ) : null}
                <dt>Last recorded valuation</dt>
                <dd>
                  <StatusPill
                    label={risk.valuation_ok === false ? 'incomplete' : 'complete'}
                    tone={risk.valuation_ok === false ? 'warn' : 'ok'}
                    detail={risk.valuation_ok === false ? 'Some marks were stale or missing when it was recorded' : 'Every position had a usable mark when it was recorded'}
                    compact
                  />{' '}
                  {risk.last_valuation_ns ? (
                    <span className="muted">
                      recorded <Age sinceNs={risk.last_valuation_ns} /> ago
                    </span>
                  ) : (
                    <span className="muted">never valued</span>
                  )}
                  {risk.stale_instruments?.length ? <span className="muted"> stale: {risk.stale_instruments.join(', ')}</span> : null}
                </dd>
              </dl>
            ) : (
              <p className="muted">No risk record yet.</p>
            )}
          </div>
        </section>

        <section className="panel" aria-labelledby="ov-activity">
          <header>
            <h2 id="ov-activity">Activity</h2>
            {o ? <span className="num muted">seq {o.latest_seq}</span> : null}
          </header>
          <div className="panel-body">
            {o ? (
              <dl className="kv">
                <dt>Open orders</dt>
                <dd className="num">
                  <WorkspaceLink to="orders">{o.open_orders}</WorkspaceLink>
                </dd>
                <dt>Unknown outcome</dt>
                <dd>
                  <StatusPill label={String(o.unknown_orders)} tone={o.unknown_orders ? 'bad' : 'ok'} detail="Orders whose broker outcome is unknown stay reserved" compact />
                </dd>
                <dt>Open exceptions</dt>
                <dd>
                  <StatusPill
                    label={`${exceptions.length} (${blockingExceptions} blocking)`}
                    tone={blockingExceptions ? 'bad' : exceptions.length ? 'warn' : 'ok'}
                    compact
                  />
                </dd>
                <dt>Sessions</dt>
                <dd className="num">
                  {sessions.length} total, {activeSessions} active
                </dd>
                <dt>Positions</dt>
                <dd className="num">{o.positions.length}</dd>
              </dl>
            ) : null}
          </div>
        </section>

        <section className="panel" aria-labelledby="ov-rec">
          <header>
            <h2 id="ov-rec">Execution and reconciliation</h2>
            {rec ? <StatusPill label={rec.status} tone={reconciliationTone(rec.status)} compact /> : null}
          </header>
          <div className="panel-body">
            <dl className="kv">
              <dt>Ownership</dt>
              <dd>
                {o?.ownership ? (
                  <>
                    <StatusPill label={o.ownership.status} tone={o.ownership.status === 'HELD' ? 'ok' : 'warn'} compact />{' '}
                    <span className="mono">{o.ownership.deployment_id}</span> <span className="muted">epoch {o.ownership.epoch}</span>
                  </>
                ) : (
                  <span className="muted">not acquired</span>
                )}
              </dd>
              <dt>Active policy</dt>
              <dd>
                {policy.data ? (
                  <>
                    {policy.data.name} <ShortId value={policy.data.policy_hash} head={10} />
                  </>
                ) : (
                  <ShortId value={o?.active_policy} head={10} />
                )}
              </dd>
              <dt>Last reconciliation</dt>
              <dd>
                {rec ? (
                  <>
                    <Timestamp ns={rec.completed_at_ns} nsStr={nsStrOf(rec, 'completed_at_ns')} />{' '}
                    <span className="muted">
                      (<Age sinceNs={rec.completed_at_ns} staleAfterMs={limits?.max_reconciliation_age_s !== undefined ? Number(limits.max_reconciliation_age_s) * 1000 : undefined} /> ago)
                    </span>
                  </>
                ) : (
                  <span className="muted">none yet</span>
                )}
              </dd>
              {rec ? (
                <>
                  <dt>Run</dt>
                  <dd>
                    <ShortId value={rec.run_id} head={20} /> <span className="muted">epoch {rec.epoch}</span>
                  </dd>
                  <dt>Differences</dt>
                  <dd className="num">{rec.differences.length}</dd>
                </>
              ) : null}
            </dl>
          </div>
        </section>
      </div>

      <section className="panel section-gap" aria-labelledby="ov-buckets">
        <header>
          <h2 id="ov-buckets">Owner buckets</h2>
          <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
            Buckets partition real balances; they never add up to more than the account holds.
          </span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption="Cash by owner bucket"
            captionHidden
            columns={bucketColumns}
            rows={o?.buckets ?? []}
            rowKey={(b) => `${b.owner}/${b.currency}`}
            emptyText={overview.status === 'loading' ? 'Loading…' : 'No balances recorded.'}
          />
        </div>
      </section>

      <section className="panel section-gap" aria-labelledby="ov-positions">
        <header>
          <h2 id="ov-positions">Positions</h2>
          <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
            Marked to bid; unrealized P&amp;L is market value minus cost basis.
          </span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption="Open positions by owner and instrument"
            captionHidden
            columns={positionColumns(overview.fetchedAtMs)}
            rows={o?.positions ?? []}
            rowKey={(p) => `${p.owner}/${p.instrument_id}`}
            emptyText="No open positions."
          />
        </div>
      </section>

      <div className="grid grid-2 section-gap">
        <section className="panel" aria-labelledby="ov-sessions">
          <header>
            <h2 id="ov-sessions">Sessions</h2>
            <span className="num muted">{sessions.length}</span>
          </header>
          <div className="panel-body flush">
            <DataTable
              caption="Sessions for this account"
              captionHidden
              columns={sessionColumns()}
              rows={sessions}
              rowKey={(s) => s.session_id}
              emptyText="No sessions yet."
            />
          </div>
        </section>
        <section className="panel" aria-labelledby="ov-exceptions">
          <header>
            <h2 id="ov-exceptions">Open exceptions</h2>
            <span className="num muted">{exceptions.length}</span>
          </header>
          <div className="panel-body flush">
            <DataTable
              caption="Open exception cases"
              captionHidden
              columns={exceptionColumns}
              rows={exceptions}
              rowKey={(e) => e.case_id}
              initialSort={{ columnId: 'opened', direction: 'descending' }}
              emptyText="No open exceptions."
            />
          </div>
        </section>
      </div>
    </WorkspaceFrame>
  );
}
