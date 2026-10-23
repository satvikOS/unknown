/**
 * One session: lifecycle and commands, mandate, result, funding and lineage, milestones (goal
 * observed, wind-down, reconciliation, completion, settlement), balances by bucket and account
 * code, positions, orders, executions, settlements and the event timeline.
 */
import { useMemo, useState } from 'react';
import { ApiError, encodePathSegment } from '../../api/client';
import { exactJson, nsStrOf } from '../../api/nsStrings';
import type { BalanceRow, Bucket, EventRecord, Execution, Order, Position, SessionDetail as Detail, Settlement } from '../../api/types';
import { useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, Badge, FixtureBadge, Money, Qty, ShortId, StatusPill, Timestamp, YesNo, type Tone } from '../../ui/primitives';
import { orderStateTone, sessionStateTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { isDecimal, isZero } from '../../format/decimal';
import { formatAge } from '../../format/time';
import { bpsToPercent } from './format';
import { accountCodeInfo, balanceSortKey, ownerRole, sessionPnl, sessionRequiredReturn, stateMeaning } from './model';
import {
  DETAIL_TIMELINE_LIMIT,
  eventPhase,
  eventSummary,
  EVENTS_PAGE_LIMIT,
  mergeTimeline,
  PHASE_LABEL,
  sessionMilestones,
  type Milestone,
  type Phase,
} from './timeline';
import { BlockerList, FundingExplanationBlock, Pct, SessionLink } from './parts';
import { readSessionPolicy } from './policy';
import { SessionActions } from './SessionActions';
import './sessions.css';

const MILESTONE_LOOK: Record<Milestone['state'], { glyph: string; tone: Tone; text: string }> = {
  done: { glyph: '✓', tone: 'ok', text: 'done' },
  current: { glyph: '…', tone: 'info', text: 'in progress' },
  pending: { glyph: '○', tone: 'neutral', text: 'not reached' },
  skipped: { glyph: '–', tone: 'neutral', text: 'not applicable' },
};

export function SessionDetailView({ id }: { id: string }) {
  const detail = useQuery(`/api/sessions/${encodePathSegment(id)}`);
  const list = useQuery('/api/sessions');
  const buckets = useQuery('/api/buckets');
  const positions = useQuery('/api/positions', undefined, { pollMs: 5000 });
  const policies = useQuery('/api/policies');
  const overview = useQuery('/api/overview');
  const s = detail.data;

  const firstPage = s?.timeline;
  const truncated = (firstPage?.length ?? 0) >= DETAIL_TIMELINE_LIMIT;
  const lastSeq = firstPage?.length ? firstPage[firstPage.length - 1]!.seq : 0;
  const more = useQuery(truncated ? '/api/events' : null, { session: id, after: lastSeq, limit: EVENTS_PAGE_LIMIT });
  const timeline = useMemo(() => mergeTimeline(firstPage, more.data), [firstPage, more.data]);
  const stillMore = truncated && (more.data?.length ?? 0) >= EVENTS_PAGE_LIMIT;

  const notFound = detail.error instanceof ApiError && detail.error.status === 404;
  const policyRec = s?.mandate ? policies.data?.find((p) => p.policy_hash === s.mandate!.policy_hash) : undefined;
  const policy = policyRec ? readSessionPolicy(policyRec) : null;
  const sessionBucket = buckets.data?.find((b) => b.owner === `session:${id}`);
  const protectedBucket = buckets.data?.find((b) => b.owner === `protected:${id}`);
  const ownPositions = (positions.data ?? []).filter((p) => p.owner === `session:${id}`);
  const positionsLoaded = positions.data !== undefined;
  const successorId = s?.successor_id ?? list.data?.find((x) => x.predecessor_id === id)?.session_id ?? null;

  return (
    <WorkspaceFrame
      title={`Session ${id}`}
      description={s ? stateMeaning(s.state) : undefined}
      queries={[detail, buckets, positions]}
      actions={
        <a className="btn small" href="#/sessions">
          ← All sessions
        </a>
      }
    >
      {notFound ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)' }}>
          The core has no session <span className="cmd-id">{id}</span>. <a href="#/sessions">Back to the session list.</a>
        </div>
      ) : (
        <QueryError what="The session" error={detail.error} />
      )}
      {!s && detail.status === 'loading' ? <p className="muted">Loading session…</p> : null}
      {s ? (
        <>
          <div className="grid grid-4">
            <LifecyclePanel session={s} overview={overview.data} policy={policy} holdings={ownPositions.length} />
            <GoalPanel session={s} timeline={timeline} />
            <ResultPanel
              session={s}
              bucket={buckets.data ? sessionBucket : undefined}
              positions={positionsLoaded ? ownPositions : null}
              positionsFetchedAtMs={positions.fetchedAtMs}
              loading={!buckets.data || !positionsLoaded}
            />
            <FundingPanel
              session={s}
              successorId={successorId}
              timeline={timeline}
              predecessorProtected={s.predecessor_id ? buckets.data?.find((b) => b.owner === `protected:${s.predecessor_id}`) : undefined}
            />
          </div>

          <section className="panel section-gap" aria-labelledby="ses-ms-h">
            <header>
              <h2 id="ses-ms-h">Lifecycle timeline</h2>
              <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
                Goal observed, wind-down, reconciliation, completion and settlement are separate steps.
              </span>
            </header>
            <div className="panel-body">
              <ol className="ses-milestones">
                {sessionMilestones(s, timeline).map((m) => {
                  const look = MILESTONE_LOOK[m.state];
                  return (
                    <li key={m.id} className="ses-milestone" data-state={m.state} style={{ ['--tone' as string]: `var(--${look.tone})` }}>
                      <div className="ms-head">
                        <span className="ms-label">
                          <span aria-hidden="true" style={{ color: `var(--${look.tone})` }}>
                            {look.glyph}
                          </span>{' '}
                          {m.label}
                        </span>
                        <span className="muted" style={{ fontSize: 'var(--fs-xs)' }}>
                          {look.text}
                        </span>
                      </div>
                      <span style={{ fontSize: 'var(--fs-sm)' }}>
                        {m.atNs !== null ? <Timestamp ns={m.atNs} nsStr={m.atNsStr} /> : <span className="muted">no timestamp</span>}
                        {m.seq !== null ? <span className="muted num"> · seq {m.seq}</span> : null}
                      </span>
                      <span className="ms-detail">{m.detail}</span>
                    </li>
                  );
                })}
              </ol>
            </div>
          </section>

          <MandatePanel session={s} policyName={policyRec?.name ?? null} />
          <BalancesPanel session={s} buckets={[sessionBucket, protectedBucket].filter((b): b is Bucket => !!b)} />

          {ownPositions.length ? <PositionsPanel positions={ownPositions} fetchedAtMs={positions.fetchedAtMs} /> : null}
          <OrdersPanel orders={s.orders} />
          <ExecutionsPanel executions={s.executions} />
          <SettlementsPanel settlements={s.settlements} />
          <TimelinePanel timeline={timeline} truncated={stillMore} />
        </>
      ) : null}
    </WorkspaceFrame>
  );
}

// ------------------------------------------------------------------ summary panels

function LifecyclePanel({
  session,
  overview,
  policy,
  holdings,
}: {
  session: Detail;
  overview: Parameters<typeof SessionActions>[0]['overview'];
  policy: Parameters<typeof SessionActions>[0]['policy'];
  holdings: number;
}) {
  return (
    <section className="panel" aria-labelledby="ses-life-h">
      <header>
        <h2 id="ses-life-h">Lifecycle</h2>
        <StatusPill label={session.state} tone={sessionStateTone(session.state)} />
      </header>
      <div className="panel-body grid" style={{ gap: 10 }}>
        <dl className="kv">
          <dt>Outcome</dt>
          <dd>{session.outcome ? <code className="mono">{session.outcome}</code> : <span className="muted">none yet</span>}</dd>
          {session.suspend_reason ? (
            <>
              <dt>Suspended for</dt>
              <dd>
                <code className="mono">{session.suspend_reason}</code>
              </dd>
            </>
          ) : null}
          {session.wind_down_reason ? (
            <>
              <dt>Wind-down reason</dt>
              <dd>
                <code className="mono">{session.wind_down_reason}</code>
              </dd>
            </>
          ) : null}
          {session.review_return_state ? (
            <>
              <dt>Returns to</dt>
              <dd>{session.review_return_state} after review</dd>
            </>
          ) : null}
          <dt>Below-target resumes</dt>
          <dd className="num">
            {session.resumes_used}
            {session.mandate ? <span className="muted"> of {session.mandate.max_resumes}</span> : null}
          </dd>
        </dl>
        {session.blockers.length ? <BlockerList codes={session.blockers} label="Current session blockers" /> : null}
        <SessionActions session={session} overview={overview} policy={policy} holdings={holdings} />
      </div>
    </section>
  );
}

function GoalPanel({ session, timeline }: { session: Detail; timeline: readonly EventRecord[] }) {
  const ccy = session.currency;
  const req = sessionRequiredReturn(session, timeline);
  const mismatch = req.recordedPct !== null && req.pct !== null && req.recordedPct !== req.pct;
  return (
    <section className="panel" aria-labelledby="ses-goal-h">
      <header>
        <h2 id="ses-goal-h">Mandate goal</h2>
        <Badge title="The target is a configured mandate, not a forecast or funding authorization">not a forecast</Badge>
      </header>
      <div className="panel-body">
        <dl className="ses-figures">
          <div>
            <dt>Seed</dt>
            <dd>
              <Money amount={session.seed} currency={ccy} />
            </dd>
          </div>
          <div>
            <dt>Target terminal equity</dt>
            <dd>
              <Money amount={session.target} currency={ccy} />
            </dd>
          </div>
          <div>
            <dt>Required net return</dt>
            <dd>
              <Pct value={req.pct} />
              <span className="sub">
                {req.recordedPct !== null
                  ? mismatch
                    ? `core recorded ${req.recordedPct}% (differs: check)`
                    : 'matches the core’s recorded figure'
                  : 'core rule: (target − seed) / seed'}
              </span>
            </dd>
          </div>
          <div>
            <dt>High-water</dt>
            <dd>
              <Money amount={session.high_water} currency={ccy} kind="marked" />
              <span className="sub">highest marked equity recorded</span>
            </dd>
          </div>
        </dl>
        <p className="muted" style={{ margin: '10px 0 0', fontSize: 'var(--fs-sm)' }}>
          The target is a configured mandate, not a forecast or funding authorization.
        </p>
      </div>
    </section>
  );
}

function ResultPanel({
  session,
  bucket,
  positions,
  positionsFetchedAtMs,
  loading,
}: {
  session: Detail;
  bucket: Bucket | undefined;
  positions: readonly Position[] | null;
  positionsFetchedAtMs: number | null;
  loading: boolean;
}) {
  const ccy = session.currency;
  const pnl = sessionPnl(session, bucket, positions);
  const holdingCount = positions?.length ?? 0;
  return (
    <section className="panel" aria-labelledby="ses-result-h">
      <header>
        <h2 id="ses-result-h">Result</h2>
        {pnl.fixtureMarks ? <FixtureBadge title="Marks come from fixture quotes generated for simulation" /> : null}
      </header>
      <div className="panel-body">
        {pnl.basis === 'unfunded' ? (
          <p className="muted" style={{ margin: 0 }}>
            No capital is allocated to this session{session.state === 'CANCELLED' ? ': it was cancelled.' : ' yet.'}
          </p>
        ) : pnl.basis === 'unavailable' ? (
          <p className="muted" style={{ margin: 0 }}>
            {loading ? 'Loading balances and marked holdings…' : 'The session’s bucket is not in the balances yet.'}
          </p>
        ) : pnl.basis === 'inconsistent' ? (
          <p style={{ margin: 0, color: 'var(--warn)' }}>
            ▲ Balances and holdings were read at different moments (inventory cost{' '}
            <Money amount={pnl.inventoryCost} currency={ccy} kind="cost" /> does not match the holdings’ cost basis). Equity is not shown until
            they agree; both refresh on the next state change.
          </p>
        ) : pnl.basis === 'terminal' ? (
          <dl className="ses-figures">
            <div>
              <dt>Terminal equity</dt>
              <dd>
                <Money amount={session.terminal_equity} currency={ccy} kind="realized" unavailable="not recorded" />
              </dd>
            </div>
            <div>
              <dt>Change vs seed</dt>
              <dd>
                <Money amount={pnl.changeVsSeed} currency={ccy} kind="realized" signed showKind unavailable="not recorded" />
              </dd>
            </div>
            <div>
              <dt>Protected value</dt>
              <dd>
                <Money amount={session.protected_value} currency={ccy} kind="protected" showKind unavailable="nothing protected" />
                <span className="sub">{session.protected_value ? 'completed proceeds; never used to fund new sessions' : 'closed sessions return capital to unallocated'}</span>
              </dd>
            </div>
          </dl>
        ) : (
          <>
            <dl className="ses-figures">
              <div>
                <dt>Marked equity</dt>
                <dd>
                  <Money amount={pnl.markedEquity} currency={ccy} kind="marked" unavailable={pnl.unmarked.length ? 'holding without a mark' : 'unavailable'} />
                  <span className="sub">liquid value + holdings at bid</span>
                </dd>
              </div>
              <div>
                <dt>Change vs seed</dt>
                <dd>
                  <Money amount={pnl.changeVsSeed} currency={ccy} signed unavailable="needs every mark" />
                  <span className="sub">realized + unrealized</span>
                </dd>
              </div>
              <div>
                <dt>Realized, net of fees</dt>
                <dd>
                  <Money amount={pnl.realizedNet} currency={ccy} kind="realized" signed />
                  <span className="sub">liquid + inventory cost − seed; fees estimated until final</span>
                </dd>
              </div>
              <div>
                <dt>Unrealized</dt>
                <dd>
                  <Money amount={pnl.unrealized} currency={ccy} kind="unrealized" signed unavailable="needs every mark" />
                  <span className="sub">
                    {holdingCount} holding{holdingCount === 1 ? '' : 's'} at bid
                    {positionsFetchedAtMs !== null ? (
                      <>
                        , read <Age ageMs={0} observedAtMs={positionsFetchedAtMs} /> ago
                      </>
                    ) : null}
                  </span>
                </dd>
              </div>
              <div>
                <dt>Still to target</dt>
                <dd>
                  <Money amount={pnl.toTarget} currency={ccy} kind="marked" unavailable="needs every mark" />
                  <span className="sub">target − marked equity; not a forecast</span>
                </dd>
              </div>
            </dl>
            {pnl.unmarked.length ? (
              <p style={{ margin: '8px 0 0', color: 'var(--warn)', fontSize: 'var(--fs-sm)' }}>
                ▲ No usable mark for {pnl.unmarked.join(', ')}: equity is not shown rather than guessed.
              </p>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}

function FundingPanel({
  session,
  successorId,
  timeline,
  predecessorProtected,
}: {
  session: Detail;
  successorId: string | null;
  timeline: readonly EventRecord[];
  /** The predecessor's protected-proceeds bucket, shown to make clear it is not used for funding. */
  predecessorProtected: Bucket | undefined;
}) {
  const notCreated = [...timeline].reverse().find((e) => e.type === 'SuccessorNotCreated');
  const successorText = successorId
    ? null
    : session.state === 'COMPLETED_UNSETTLED' || session.state === 'COMPLETED_SETTLED'
      ? notCreated
        ? `Not created: ${typeof notCreated.info.reason === 'string' ? notCreated.info.reason : 'see the timeline'}.`
        : 'None recorded.'
      : session.state === 'CLOSED' || session.state === 'CANCELLED'
        ? 'None: only a session that reaches its target gets a successor.'
        : 'Created only if this session completes at its target.';
  return (
    <section className="panel" aria-labelledby="ses-fund-h">
      <header>
        <h2 id="ses-fund-h">Funding and lineage</h2>
      </header>
      <div className="panel-body grid" style={{ gap: 10 }}>
        <FundingExplanationBlock
          session={session}
          extra={
            session.predecessor_id && (session.state === 'WAITING_FUNDS' || session.state === 'REQUESTED') ? (
              <span className="muted">
                This successor is funded only from unallocated settled cash; its predecessor’s protected proceeds
                {predecessorProtected ? (
                  <>
                    {' '}
                    (<Money amount={predecessorProtected.liquid_value} currency={predecessorProtected.currency} kind="protected" />, of which{' '}
                    <Money amount={predecessorProtected.receivable_unsettled} currency={predecessorProtected.currency} kind="unsettled" /> unsettled)
                  </>
                ) : null}{' '}
                are never used.
              </span>
            ) : undefined
          }
        />
        <dl className="kv">
          <dt>Origin</dt>
          <dd className="cmd-id">{session.mandate?.origin ?? 'unknown'}</dd>
          <dt>Predecessor</dt>
          <dd>{session.predecessor_id ? <SessionLink id={session.predecessor_id} /> : <span className="muted">none</span>}</dd>
          <dt>Successor</dt>
          <dd>{successorId ? <SessionLink id={successorId} /> : <span className="muted">{successorText}</span>}</dd>
          <dt>Funded</dt>
          <dd>
            <Timestamp ns={session.funded_at_ns} nsStr={nsStrOf(session, 'funded_at_ns')} never="not funded" />
          </dd>
        </dl>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ mandate

function MandatePanel({ session, policyName }: { session: Detail; policyName: string | null }) {
  const m = session.mandate;
  const ccy = session.currency;
  return (
    <section className="panel section-gap" aria-labelledby="ses-mandate-h">
      <header>
        <h2 id="ses-mandate-h">Mandate</h2>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          frozen at request; amendments create a new mandate hash
        </span>
      </header>
      <div className="panel-body">
        {!m ? (
          <p className="muted">The mandate body for hash {session.mandate_hash} is not stored.</p>
        ) : (
          <div className="grid grid-2">
            <dl className="kv" aria-label="Mandate terms">
              <dt>Seed</dt>
              <dd>
                <Money amount={m.seed} currency={ccy} />
              </dd>
              <dt>Target terminal equity</dt>
              <dd>
                <Money amount={m.target_terminal_equity} currency={ccy} /> <span className="muted">(not a forecast)</span>
              </dd>
              <dt>Currency</dt>
              <dd>{m.currency}</dd>
              <dt>Strategy eligibility</dt>
              <dd>
                {m.strategy_eligibility.length ? (
                  m.strategy_eligibility.map((x) => (
                    <code key={x} className="mono" style={{ marginRight: 6 }}>
                      {x}
                    </code>
                  ))
                ) : (
                  <span className="muted">none</span>
                )}
              </dd>
              <dt>Drawdown trigger</dt>
              <dd>
                {bpsToPercent(m.session_drawdown_trigger_bps)} below high-water <span className="muted">({m.session_drawdown_trigger_bps} bps): suspends</span>
              </dd>
              <dt>Maximum loss</dt>
              <dd>
                <Money amount={m.session_max_loss} currency={ccy} /> <span className="muted">from the seed: suspends</span>
              </dd>
              <dt>Auto start</dt>
              <dd>
                <YesNo value={m.auto_start} />
              </dd>
              <dt>Expires</dt>
              <dd>
                <Timestamp ns={m.expires_at_ns} nsStr={nsStrOf(m, 'expires_at_ns')} never="no expiry" />
              </dd>
              <dt>Below target after observation</dt>
              <dd>
                <code className="mono">{m.below_target_after_target_observed}</code>
                <span className="muted"> · up to {m.max_resumes} resume{m.max_resumes === 1 ? '' : 's'}</span>
              </dd>
            </dl>
            <dl className="kv" aria-label="Mandate identity">
              <dt>Mandate hash</dt>
              <dd className="cmd-id">{session.mandate_hash}</dd>
              <dt>Policy hash</dt>
              <dd>
                <span className="cmd-id">{m.policy_hash}</span>
                {policyName ? <span className="muted"> ({policyName})</span> : null}
              </dd>
              <dt>Session id</dt>
              <dd className="cmd-id">{m.session_id}</dd>
              <dt>Account</dt>
              <dd className="cmd-id">{m.account_key}</dd>
              <dt>Origin</dt>
              <dd className="cmd-id">{m.origin}</dd>
              <dt>Mandate created</dt>
              <dd>
                <Timestamp ns={m.created_at_ns} nsStr={nsStrOf(m, 'created_at_ns')} withMs />
              </dd>
            </dl>
          </div>
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ balances

const bucketColumns: Column<Bucket>[] = [
  {
    id: 'owner',
    header: 'Bucket',
    cell: (b) => (
      <span title={b.owner}>
        <Badge tone={ownerRole(b.owner) === 'protected' ? 'ok' : 'info'}>{ownerRole(b.owner)}</Badge>
      </span>
    ),
  },
  { id: 'settled', header: 'Settled cash', numeric: true, cell: (b) => <Money amount={b.settled_cash} currency={b.currency} kind="settled" /> },
  { id: 'recv', header: 'Receivable (unsettled)', numeric: true, cell: (b) => <Money amount={b.receivable_unsettled} currency={b.currency} kind="unsettled" /> },
  { id: 'pay', header: 'Payable (unsettled)', numeric: true, cell: (b) => <Money amount={b.payable_unsettled} currency={b.currency} kind="unsettled" /> },
  { id: 'fees', header: 'Fees accrued', numeric: true, cell: (b) => <Money amount={b.fees_accrued} currency={b.currency} kind="estimated" /> },
  { id: 'liquid', header: 'Liquid value', description: 'Settled cash + receivable + payable + fees accrued', numeric: true, cell: (b) => <Money amount={b.liquid_value} currency={b.currency} /> },
  { id: 'holds', header: 'Held for orders', numeric: true, cell: (b) => <Money amount={b.holds} currency={b.currency} kind="held" /> },
  { id: 'avail', header: 'Available cash', numeric: true, cell: (b) => <Money amount={b.available_cash} currency={b.currency} kind="available" /> },
  { id: 'inv', header: 'Inventory at cost', numeric: true, cell: (b) => <Money amount={b.inventory_cost} currency={b.currency} kind="cost" /> },
];

const balanceColumns: Column<BalanceRow>[] = [
  {
    id: 'owner',
    header: 'Bucket',
    sortValue: (r) => balanceSortKey(r.owner, r.account_code),
    cell: (r) => <Badge tone={ownerRole(r.owner) === 'protected' ? 'ok' : 'info'}>{ownerRole(r.owner)}</Badge>,
  },
  {
    id: 'code',
    header: 'Account code',
    sortValue: (r) => r.account_code,
    cell: (r) => (
      <span title={accountCodeInfo(r.account_code).note}>
        {accountCodeInfo(r.account_code).label} <code className="mono muted">{r.account_code}</code>
      </span>
    ),
  },
  { id: 'inst', header: 'Instrument', sortValue: (r) => r.instrument_id, cell: (r) => (r.instrument_id ? <WorkspaceLink to="instruments" rest={[r.instrument_id]}>{r.instrument_id}</WorkspaceLink> : <span className="muted">—</span>) },
  {
    id: 'amount',
    header: 'Ledger balance',
    description: 'Signed ledger balance: debit positive, credit negative',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (r) => r.amount,
    cell: (r) => {
      const info = accountCodeInfo(r.account_code);
      return <Money amount={r.amount} currency={r.currency} {...(info.kind ? { kind: info.kind } : {})} />;
    },
  },
  { id: 'note', header: 'Meaning', wrap: true, cell: (r) => <span className="muted">{accountCodeInfo(r.account_code).note}</span> },
];

function BalancesPanel({ session, buckets }: { session: Detail; buckets: Bucket[] }) {
  const rows = useMemo(
    () => [...session.balances].sort((a, b) => balanceSortKey(a.owner, a.account_code).localeCompare(balanceSortKey(b.owner, b.account_code))),
    [session.balances],
  );
  return (
    <section className="panel section-gap ses-captioned" aria-labelledby="ses-bal-h">
      <header>
        <h2 id="ses-bal-h">Balances</h2>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          session and protected buckets
        </span>
      </header>
      <div className="panel-body flush">
        <DataTable
          caption="Session and protected bucket summary"
          columns={bucketColumns}
          rows={buckets}
          rowKey={(b) => `${b.owner}/${b.currency}`}
          emptyText="No bucket balances for this session."
          maxHeight="none"
        />
        <DataTable
          caption="Balances by bucket and account code"
          columns={balanceColumns}
          rows={rows}
          rowKey={(r) => `${r.owner}/${r.account_code}/${r.currency}/${r.instrument_id}`}
          emptyText="No ledger balances for this session."
          maxHeight="min(50vh, 420px)"
        />
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ positions

function PositionsPanel({ positions, fetchedAtMs }: { positions: Position[]; fetchedAtMs: number | null }) {
  const cols: Column<Position>[] = [
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
    { id: 'cost', header: 'Cost basis', numeric: true, sortKind: 'decimal', sortValue: (p) => p.cost_basis, cell: (p) => <Money amount={p.cost_basis} currency={p.currency} kind="cost" /> },
    {
      id: 'mark',
      header: 'Mark (bid)',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.mark,
      cell: (p) => (
        <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          {p.mark_fixture ? <FixtureBadge /> : null}
          <Money amount={p.mark} currency={p.currency} kind="marked" unavailable="no quote" />
        </span>
      ),
    },
    { id: 'mv', header: 'Market value', numeric: true, sortKind: 'decimal', sortValue: (p) => p.market_value, cell: (p) => <Money amount={p.market_value} currency={p.currency} kind="marked" unavailable="no quote" /> },
    {
      id: 'unreal',
      header: 'Unrealized P&L',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.unrealized,
      cell: (p) => <Money amount={p.unrealized} currency={p.currency} kind="unrealized" signed unavailable="no quote" />,
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
  return (
    <section className="panel section-gap" aria-labelledby="ses-pos-h">
      <header>
        <h2 id="ses-pos-h">Holdings</h2>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          Marked to bid; unrealized P&amp;L is market value minus cost basis.
        </span>
      </header>
      <div className="panel-body flush">
        <DataTable caption="Session holdings" captionHidden columns={cols} rows={positions} rowKey={(p) => p.instrument_id} emptyText="No holdings." />
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ orders, executions, settlements

const orderColumns: Column<Order>[] = [
  {
    id: 'id',
    header: 'Order',
    sortValue: (o) => o.order_id,
    cell: (o) => (
      <WorkspaceLink to="orders" rest={[o.order_id]}>
        <ShortId value={o.order_id} head={14} />
      </WorkspaceLink>
    ),
  },
  { id: 'side', header: 'Side', sortValue: (o) => o.side, cell: (o) => <strong>{o.side}</strong> },
  { id: 'inst', header: 'Instrument', sortValue: (o) => o.instrument_id, cell: (o) => o.instrument_id },
  { id: 'qty', header: 'Quantity', numeric: true, sortKind: 'decimal', sortValue: (o) => o.quantity, cell: (o) => <Qty value={o.quantity} /> },
  { id: 'limit', header: 'Limit', numeric: true, sortKind: 'decimal', sortValue: (o) => o.limit_price, cell: (o) => <Money amount={o.limit_price} currency={o.currency} /> },
  {
    id: 'state',
    header: 'State',
    sortValue: (o) => o.state,
    cell: (o) => (
      <span>
        <StatusPill label={o.state} tone={orderStateTone(o.state)} compact />
        {o.cancel_state && o.cancel_state !== 'NONE' ? <span className="muted"> cancel {o.cancel_state}</span> : null}
      </span>
    ),
  },
  { id: 'filled', header: 'Filled', numeric: true, sortKind: 'decimal', sortValue: (o) => o.cum_qty, cell: (o) => <Qty value={o.cum_qty} /> },
  { id: 'avg', header: 'Average price', numeric: true, sortKind: 'decimal', sortValue: (o) => o.avg_price, cell: (o) => <Money amount={o.avg_price} currency={o.currency} unavailable="no fills" /> },
  {
    id: 'fees',
    header: 'Fees',
    description: 'Final fees when reported, otherwise the estimate',
    numeric: true,
    cell: (o) =>
      isDecimal(o.fees_final) && !isZero(o.fees_final) ? (
        <Money amount={o.fees_final} currency={o.currency} kind="realized" showKind />
      ) : (
        <Money amount={o.fees_estimated} currency={o.currency} kind="estimated" showKind />
      ),
  },
  { id: 'purpose', header: 'Purpose', sortValue: (o) => o.purpose, cell: (o) => <code className="mono">{o.purpose}</code> },
  { id: 'accepted', header: 'Accepted', sortKind: 'number', sortValue: (o) => o.accepted_at_ns, cell: (o) => <Timestamp ns={o.accepted_at_ns} nsStr={nsStrOf(o, 'accepted_at_ns')} /> },
];

function OrdersPanel({ orders }: { orders: Order[] }) {
  return (
    <section className="panel section-gap" aria-labelledby="ses-ord-h">
      <header>
        <h2 id="ses-ord-h">Orders</h2>
        <span className="num muted">{orders.length}</span>
      </header>
      <div className="panel-body flush">
        <DataTable
          caption="Orders placed for this session"
          captionHidden
          columns={orderColumns}
          rows={orders}
          rowKey={(o) => o.order_id}
          initialSort={{ columnId: 'accepted', direction: 'descending' }}
          emptyText="No orders for this session."
        />
      </div>
    </section>
  );
}

const executionColumns: Column<Execution>[] = [
  { id: 'key', header: 'Execution', sortValue: (e) => e.exec_key, cell: (e) => <ShortId value={e.exec_key} head={16} /> },
  {
    id: 'order',
    header: 'Order',
    sortValue: (e) => e.order_id,
    cell: (e) =>
      e.order_id ? (
        <WorkspaceLink to="orders" rest={[e.order_id]}>
          <ShortId value={e.order_id} head={14} />
        </WorkspaceLink>
      ) : (
        <span className="muted">unmapped</span>
      ),
  },
  { id: 'side', header: 'Side', sortValue: (e) => e.side, cell: (e) => <strong>{e.side}</strong> },
  { id: 'inst', header: 'Instrument', sortValue: (e) => e.instrument_id, cell: (e) => e.instrument_id },
  { id: 'qty', header: 'Quantity', numeric: true, sortKind: 'decimal', sortValue: (e) => e.quantity, cell: (e) => <Qty value={e.quantity} /> },
  { id: 'price', header: 'Price', numeric: true, sortKind: 'decimal', sortValue: (e) => e.price, cell: (e) => <Money amount={e.price} currency={e.currency} /> },
  { id: 'notional', header: 'Notional', numeric: true, sortKind: 'decimal', sortValue: (e) => e.notional, cell: (e) => <Money amount={e.notional} currency={e.currency} /> },
  {
    id: 'fee',
    header: 'Fee',
    numeric: true,
    cell: (e) =>
      e.fee_final !== null ? (
        <Money amount={e.fee_final} currency={e.currency} kind="realized" showKind />
      ) : (
        <Money amount={e.fee_estimate} currency={e.currency} kind="estimated" showKind />
      ),
  },
  { id: 'feestatus', header: 'Fee status', sortValue: (e) => e.fee_status, cell: (e) => <code className="mono">{e.fee_status}</code> },
  { id: 'status', header: 'Status', sortValue: (e) => e.status, cell: (e) => <StatusPill label={e.status} tone={e.status === 'ACTIVE' ? 'ok' : 'warn'} compact /> },
  { id: 'time', header: 'Executed', sortKind: 'number', sortValue: (e) => e.exec_time_ns, cell: (e) => <Timestamp ns={e.exec_time_ns} nsStr={nsStrOf(e, 'exec_time_ns')} withMs /> },
];

function ExecutionsPanel({ executions }: { executions: Execution[] }) {
  return (
    <section className="panel section-gap" aria-labelledby="ses-exe-h">
      <header>
        <h2 id="ses-exe-h">Executions</h2>
        <span className="num muted">{executions.length}</span>
      </header>
      <div className="panel-body flush">
        <DataTable
          caption="Executions attributed to this session"
          captionHidden
          columns={executionColumns}
          rows={executions}
          rowKey={(e) => e.exec_key}
          initialSort={{ columnId: 'time', direction: 'descending' }}
          emptyText="No executions for this session."
        />
      </div>
    </section>
  );
}

const settlementColumns: Column<Settlement>[] = [
  { id: 'id', header: 'Settlement', sortValue: (s) => s.settlement_id, cell: (s) => <ShortId value={s.settlement_id} head={16} /> },
  { id: 'owner', header: 'Bucket', sortValue: (s) => s.owner, cell: (s) => <Badge tone={ownerRole(s.owner) === 'protected' ? 'ok' : 'info'}>{ownerRole(s.owner)}</Badge> },
  { id: 'dir', header: 'Direction', sortValue: (s) => s.direction, cell: (s) => s.direction },
  {
    id: 'amount',
    header: 'Amount',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (s) => s.amount,
    cell: (s) => <Money amount={s.amount} currency={s.currency} kind={s.status === 'SETTLED' ? 'settled' : 'unsettled'} showKind />,
  },
  { id: 'trade', header: 'Trade date', sortValue: (s) => s.trade_date, cell: (s) => <span className="num">{s.trade_date}</span> },
  { id: 'expected', header: 'Expected', sortValue: (s) => s.expected_date, cell: (s) => <span className="num">{s.expected_date}</span> },
  {
    id: 'status',
    header: 'Status',
    sortValue: (s) => s.status,
    cell: (s) => <StatusPill label={s.status} tone={s.status === 'SETTLED' ? 'ok' : s.status === 'EXPECTED' ? 'info' : 'warn'} compact />,
  },
  { id: 'settled', header: 'Settled at', sortKind: 'number', sortValue: (s) => s.settled_at_ns, cell: (s) => <Timestamp ns={s.settled_at_ns} nsStr={nsStrOf(s, 'settled_at_ns')} never="not yet" /> },
];

function SettlementsPanel({ settlements }: { settlements: Settlement[] }) {
  return (
    <section className="panel section-gap" aria-labelledby="ses-set-h">
      <header>
        <h2 id="ses-set-h">Settlements</h2>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          Settlement changes availability, not P&amp;L.
        </span>
      </header>
      <div className="panel-body flush">
        <DataTable
          caption="Settlements owned by this session or its protected proceeds"
          captionHidden
          columns={settlementColumns}
          rows={settlements}
          rowKey={(s) => s.settlement_id}
          initialSort={{ columnId: 'expected', direction: 'ascending' }}
          emptyText="No settlements for this session’s buckets."
        />
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ event timeline

interface TimelineRow extends EventRecord {
  phase: Phase;
}

const timelineColumns: Column<TimelineRow>[] = [
  { id: 'seq', header: 'Seq', numeric: true, sortKind: 'number', sortValue: (e) => e.seq, cell: (e) => e.seq },
  { id: 'time', header: 'Recorded', sortKind: 'number', sortValue: (e) => e.recorded_time_ns, cell: (e) => <Timestamp ns={e.recorded_time_ns} nsStr={nsStrOf(e, 'recorded_time_ns')} withMs /> },
  { id: 'phase', header: 'Phase', sortValue: (e) => e.phase, cell: (e) => <Badge tone={e.phase === 'goal' ? 'ok' : e.phase === 'review' ? 'bad' : 'neutral'}>{PHASE_LABEL[e.phase]}</Badge> },
  { id: 'type', header: 'Event', sortValue: (e) => e.type, cell: (e) => <code className="mono">{e.type}</code> },
  { id: 'by', header: 'By', sortValue: (e) => e.principal_id, cell: (e) => <span className="cmd-id">{e.principal_id}</span> },
  {
    id: 'order',
    header: 'Order',
    cell: (e) =>
      e.order_id ? (
        <WorkspaceLink to="orders" rest={[e.order_id]}>
          <ShortId value={e.order_id} head={12} />
        </WorkspaceLink>
      ) : (
        <span className="muted">—</span>
      ),
  },
  { id: 'cmd', header: 'Command', cell: (e) => <ShortId value={e.command_id} head={18} /> },
  {
    id: 'summary',
    header: 'Detail',
    wrap: true,
    cell: (e) => (
      <details className="ses-info">
        <summary>{eventSummary(e) || 'show recorded detail'}</summary>
        <pre className="ses-json">{exactJson(e.info, 2)}</pre>
        <span className="muted" style={{ fontSize: 'var(--fs-xs)' }}>
          changed {e.changed_sections.join(', ') || 'nothing'} · chain {e.chain_hash.slice(0, 12)}…
        </span>
      </details>
    ),
  },
];

const PHASE_ORDER: readonly Phase[] = ['request', 'funding', 'start', 'running', 'goal', 'wind_down', 'reconciliation', 'completion', 'settlement', 'review', 'trading', 'other'];

function TimelinePanel({ timeline, truncated }: { timeline: readonly EventRecord[]; truncated: boolean }) {
  const [phase, setPhase] = useState<Phase | 'all' | 'lifecycle'>('lifecycle');
  const rows: TimelineRow[] = useMemo(() => timeline.map((e) => ({ ...e, phase: eventPhase(e) })), [timeline]);
  const counts = new Map<Phase, number>();
  for (const r of rows) counts.set(r.phase, (counts.get(r.phase) ?? 0) + 1);
  const lifecycleCount = rows.filter((r) => r.phase !== 'trading').length;
  const visible = phase === 'all' ? rows : phase === 'lifecycle' ? rows.filter((r) => r.phase !== 'trading') : rows.filter((r) => r.phase === phase);
  const first = rows[0];
  const last = rows[rows.length - 1];
  return (
    <section className="panel section-gap" aria-labelledby="ses-tl-h">
      <header>
        <h2 id="ses-tl-h">Event timeline</h2>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          {rows.length} event{rows.length === 1 ? '' : 's'}
          {first && last && last.recorded_time_ns >= first.recorded_time_ns ? ` over ${formatAge((last.recorded_time_ns - first.recorded_time_ns) / 1e6)}` : ''}
        </span>
      </header>
      <div className="panel-body" style={{ paddingBottom: 6 }}>
        <div className="ses-filters" role="group" aria-label="Filter events by phase">
          <button type="button" className="btn small" aria-pressed={phase === 'lifecycle'} onClick={() => setPhase('lifecycle')}>
            Lifecycle <span className="count">{lifecycleCount}</span>
          </button>
          <button type="button" className="btn small" aria-pressed={phase === 'all'} onClick={() => setPhase('all')}>
            All <span className="count">{rows.length}</span>
          </button>
          {PHASE_ORDER.filter((p) => counts.get(p)).map((p) => (
            <button key={p} type="button" className="btn small" aria-pressed={phase === p} onClick={() => setPhase(p)}>
              {PHASE_LABEL[p]} <span className="count">{counts.get(p)}</span>
            </button>
          ))}
        </div>
        {truncated ? (
          <p style={{ color: 'var(--warn)', fontSize: 'var(--fs-sm)', margin: '8px 0 0' }}>
            ▲ More events exist than the API returns in two pages; the earliest {rows.length} are shown. The lifecycle milestones above may miss later events.
          </p>
        ) : null}
      </div>
      <div className="panel-body flush">
        <DataTable
          caption="Events recorded for this session"
          captionHidden
          columns={timelineColumns}
          rows={visible}
          rowKey={(e) => String(e.seq)}
          initialSort={{ columnId: 'seq', direction: 'ascending' }}
          emptyText="No events in this phase."
          maxHeight="min(70vh, 720px)"
        />
      </div>
    </section>
  );
}
