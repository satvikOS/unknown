/**
 * Risk (book Ch. 22: cash, positions, limits, aggregate loss; primary action "tighten or stop").
 *
 * Sources: GET /api/risk (recorded risk state + active policy), /api/positions and /api/quotes
 * (polled: marks move without events), /api/buckets, /api/orders?state=open, /api/instruments.
 * Recorded figures are shown as recorded. Figures the terminal derives (P&L at current marks, gross
 * exposure, concentration) are exact, follow the core's definitions and say so.
 */
import { nsStrOf } from '../../api/nsStrings';
import type { Bucket, Order, Position, Quote, RiskState } from '../../api/types';
import { isDecimal } from '../../format/decimal';
import { useNow, useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, Badge, FixtureBadge, Money, Qty, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { hasWorkspace } from '../registry';
import type { WorkspaceProps } from '../types';
import { LimitCards } from '../policy/PolicyParts';
import { bpsToPercent } from '../../format/exact';
import { UsageMeter } from './Meter';
import {
  breachLabel,
  budgetUse,
  capUse,
  concentrations,
  dailyLoss,
  estimateMarkedPnl,
  grossExposure,
  isSystemOwner,
  lossFromHighWater,
  riskDayAt,
  sessionOfOwner,
  type Concentration,
  type MarkedEstimate,
  type Multipliers,
  type OpenBuyLine,
} from './riskModel';
import './risk.css';

/** The limits the risk view leads with, in this order; every other account limit follows. */
const PRIMARY_LIMITS = [
  'authorized_capital',
  'operational_buffer',
  'max_order_notional',
  'max_position_bps_of_session_equity',
  'max_gross_exposure',
  'max_orders_per_minute',
  'price_collar_bps',
  'quote_max_age_ms',
  'daily_loss_trigger',
  'cumulative_loss_allowance',
  'risk_day_timezone',
];

function decimalOrNull(v: unknown): string | null {
  return typeof v === 'string' && isDecimal(v) ? v : null;
}

function OwnerCell({ owner, systemOwned }: { owner: string; systemOwned?: boolean }) {
  const session = sessionOfOwner(owner);
  const system = systemOwned ?? isSystemOwner(owner);
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      {session ? (
        <WorkspaceLink to="sessions" rest={[session]}>
          <span className="cmd-id">{owner}</span>
        </WorkspaceLink>
      ) : (
        <span className="cmd-id">{owner}</span>
      )}
      {system ? (
        <Badge tone="info" title="Owned by the system: counts toward authorized capital and the loss budget">
          system
        </Badge>
      ) : (
        <Badge title="Outside the system: not authorized for trading, not in the loss budget">outside</Badge>
      )}
    </span>
  );
}

// ------------------------------------------------------------------ loss budget

interface LossPanelProps {
  risk: RiskState;
  limits: Record<string, unknown>;
  timeZone: string | null;
  buckets: readonly Bucket[] | undefined;
  positions: readonly Position[] | undefined;
  quotes: Record<string, Quote> | undefined;
  quotesFetchedAtMs: number | null;
  multipliers: Multipliers | null;
}

function LossBudgetPanel({ risk, limits, timeZone, buckets, positions, quotes, quotesFetchedAtMs, multipliers }: LossPanelProps) {
  const now = useNow(1000);
  const ccy = risk.currency;
  const quoteMaxAgeMs = typeof limits.quote_max_age_ms === 'number' ? limits.quote_max_age_ms : null;
  const estimate: MarkedEstimate | null =
    buckets && positions && quotes && multipliers && quoteMaxAgeMs !== null
      ? estimateMarkedPnl({
          buckets,
          positions,
          quotes,
          quotesElapsedMs: quotesFetchedAtMs === null ? 0 : now - quotesFetchedAtMs,
          authorizedTotal: risk.authorized_total,
          currency: ccy,
          quoteMaxAgeMs,
          multipliers,
        })
      : null;
  const live = estimate?.status === 'complete' && estimate.pnl !== null;
  const pnl = live ? (estimate.pnl as string) : risk.cumulative_pnl;
  const allowance = decimalOrNull(limits.cumulative_loss_allowance);
  const trigger = decimalOrNull(limits.daily_loss_trigger);
  const hwmUse = allowance ? budgetUse(lossFromHighWater(risk.pnl_high_water, pnl), allowance) : null;
  const dayUse = trigger ? budgetUse(dailyLoss(risk.day_start_pnl, pnl), trigger) : null;
  const today = timeZone ? riskDayAt(timeZone, now) : null;
  const newDay = today !== null && risk.risk_day !== null && today !== risk.risk_day;
  const basis = live ? (
    <>
      P&amp;L at current marks (terminal estimate, core definition: system buckets’ liquid value plus system positions at a fresh bid, minus authorized
      capital).
    </>
  ) : estimate?.status === 'incomplete' ? (
    <>
      Current marks are incomplete ({estimate.stale.map((s) => `${s.instrument_id}: ${s.problem}`).join('; ')}), so this uses the recorded P&amp;L. The core
      fails closed in the same case: exposure-increasing orders are refused while valuation is incomplete.
    </>
  ) : (
    <>Using the recorded P&amp;L while current marks load.</>
  );

  return (
    <section className="panel" aria-labelledby="rk-loss">
      <header>
        <h2 id="rk-loss">Loss budget</h2>
        <span className="rk-actions">
          {risk.valuation_ok === false ? <StatusPill label="valuation incomplete" tone="warn" compact /> : null}
          {risk.breach ? <StatusPill label={`breach: ${risk.breach}`} tone="bad" compact /> : <StatusPill label="no breach recorded" tone="ok" compact />}
        </span>
      </header>
      <div className="panel-body">
        {risk.breach ? (
          <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', marginBottom: 10 }}>
            <strong>Loss budget breached:</strong> {breachLabel(risk.breach)}. Recorded <Timestamp ns={risk.breach_at_ns} nsStr={nsStrOf(risk, 'breach_at_ns')} />. The core latched the stop; new
            entries stay blocked until the owner reviews and starts again.
          </div>
        ) : null}
        {hwmUse ? (
          <UsageMeter
            id="rk-hwm"
            label="Loss from P&L high-water"
            use={hwmUse}
            currency={ccy}
            limitWord="allowance"
            stateText={{ reached: 'allowance reached' }}
            basis={
              live ? (
                <>
                  At current marks
                  {estimate.fixture ? (
                    <>
                      {' '}
                      from fixture quotes <FixtureBadge />
                    </>
                  ) : null}
                  .
                </>
              ) : (
                'From the recorded P&L.'
              )
            }
          />
        ) : (
          <p className="muted">No cumulative loss allowance in the active policy.</p>
        )}
        {dayUse ? (
          <UsageMeter
            id="rk-day"
            label={`Daily loss (risk day ${risk.risk_day ?? 'not started'}${timeZone ? `, ${timeZone}` : ''})`}
            use={dayUse}
            currency={ccy}
            limitWord="trigger"
            stateText={{ reached: 'trigger reached' }}
            basis={
              newDay ? (
                `A new risk day (${today}) has begun in ${timeZone}; the core resets the day start at its next valuation.`
              ) : live ? (
                <>
                  At current marks
                  {estimate.fixture ? (
                    <>
                      {' '}
                      from fixture quotes <FixtureBadge />
                    </>
                  ) : null}
                  , from the P&amp;L at the first valuation of the risk day.
                </>
              ) : (
                'From the recorded P&L.'
              )
            }
          />
        ) : (
          <p className="muted">No daily loss trigger in the active policy.</p>
        )}
        <p className="rk-basis" style={{ margin: '0 0 10px' }}>
          {basis}
        </p>
        <dl className="kv">
          <dt>P&amp;L at current marks</dt>
          <dd>
            {live ? (
              <>
                <Money amount={estimate.pnl} currency={ccy} signed />{' '}
                {estimate.fixture ? <FixtureBadge title="Marked with fixture quotes: simulation only" /> : null}{' '}
                <span className="muted">realized + unrealized, terminal estimate</span>
              </>
            ) : (
              <span className="muted">{estimate?.status === 'incomplete' ? 'unavailable: marks incomplete' : 'loading'}</span>
            )}
          </dd>
          {live ? (
            <>
              <dt>System value</dt>
              <dd>
                <Money amount={estimate.systemValue} currency={ccy} kind="marked" />{' '}
                <span className="muted">
                  = liquid <Money amount={estimate.liquid} currency={ccy} /> + positions at bid{' '}
                  <Money amount={estimate.positions} currency={ccy} kind="marked" />
                </span>
              </dd>
            </>
          ) : null}
          <dt>Authorized capital (net)</dt>
          <dd>
            <Money amount={risk.authorized_total} currency={ccy} kind="authorized" />
            {decimalOrNull(limits.authorized_capital) ? (
              <span className="muted">
                {' '}
                of <Money amount={decimalOrNull(limits.authorized_capital)} currency={ccy} /> limit
              </span>
            ) : null}
          </dd>
          <dt>Recorded cumulative P&amp;L</dt>
          <dd>
            <Money amount={risk.cumulative_pnl} currency={ccy} signed /> <span className="muted">realized + unrealized, as last recorded</span>
          </dd>
          <dt>P&amp;L high-water</dt>
          <dd>
            <Money amount={risk.pnl_high_water} currency={ccy} signed /> <span className="muted">recorded</span>
          </dd>
          <dt>Day-start P&amp;L</dt>
          <dd>
            <Money amount={risk.day_start_pnl} currency={ccy} signed /> <span className="muted">first valuation of {risk.risk_day ?? 'no risk day yet'}</span>
          </dd>
          <dt>Breach</dt>
          <dd>
            {risk.breach ? (
              <>
                <StatusPill label={risk.breach} tone="bad" compact /> <Timestamp ns={risk.breach_at_ns} nsStr={nsStrOf(risk, 'breach_at_ns')} />
              </>
            ) : (
              <span>none recorded</span>
            )}
          </dd>
          <dt>Valuation</dt>
          <dd>
            {risk.valuation_ok === undefined ? (
              <span className="muted">not yet valued</span>
            ) : (
              <StatusPill
                label={risk.valuation_ok ? 'complete' : 'incomplete'}
                tone={risk.valuation_ok ? 'ok' : 'warn'}
                detail={
                  risk.valuation_ok ? 'Every system position had a fresh bid' : 'A system position lacked a fresh bid; exposure-increasing orders are refused'
                }
                compact
              />
            )}
            {risk.stale_instruments && risk.stale_instruments.length ? (
              <span>
                {' '}
                stale:{' '}
                {risk.stale_instruments.map((i, n) => (
                  <span key={i}>
                    {n ? ', ' : ''}
                    <WorkspaceLink to="instruments" rest={[i]}>
                      <span className="mono">{i}</span>
                    </WorkspaceLink>
                  </span>
                ))}
              </span>
            ) : (
              <span className="muted"> no stale instruments</span>
            )}
          </dd>
          <dt>Last recorded valuation</dt>
          <dd>
            <Timestamp ns={risk.last_valuation_ns} nsStr={nsStrOf(risk, 'last_valuation_ns')} never="never" />
            {risk.last_valuation_ns ? (
              <span className="muted">
                {' '}
                (<Age sinceNs={risk.last_valuation_ns} /> ago)
              </span>
            ) : null}
          </dd>
        </dl>
        <p className="rk-basis" style={{ margin: '8px 0 0' }}>
          The core values the account every few seconds but writes this record only when a decision input changes (completeness, breach, risk day, high-water,
          stale list). Recorded P&amp;L can therefore be older than the marks; breaches are decided on every valuation.
        </p>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ exposure

function ExposurePanel({
  ccy,
  limits,
  positions,
  openOrders,
  multipliers,
  conc,
}: {
  ccy: string;
  limits: Record<string, unknown>;
  positions: readonly Position[] | undefined;
  openOrders: readonly Order[] | undefined;
  multipliers: Multipliers | null;
  conc: Concentration[];
}) {
  const gross = positions && openOrders && multipliers ? grossExposure(positions, openOrders, ccy, multipliers) : null;
  const cap = decimalOrNull(limits.max_gross_exposure);
  const use = gross?.total && cap ? capUse(gross.total, cap) : null;
  const maxBps = typeof limits.max_position_bps_of_session_equity === 'number' ? BigInt(limits.max_position_bps_of_session_equity) : null;
  const largest = conc.reduce<Concentration | null>((best, c) => (c.bps !== null && (best === null || (best.bps ?? -1n) < c.bps) ? c : best), null);
  return (
    <section className="panel" aria-labelledby="rk-exposure">
      <header>
        <h2 id="rk-exposure">Exposure</h2>
        {gross ? (
          <span className="num muted">
            {gross.lines.length} positions, {gross.buys.length} open buys
          </span>
        ) : null}
      </header>
      <div className="panel-body">
        {!gross ? (
          <p className="muted">Loading positions and open orders…</p>
        ) : use ? (
          <UsageMeter
            id="rk-gross"
            label="Gross exposure"
            use={use}
            currency={ccy}
            limitWord="cap"
            stateText={{ approaching: 'over half the cap', reached: 'above cap' }}
            basis="Terminal estimate by the core’s definition; the core evaluates it with every new buy."
          />
        ) : gross.total === null ? (
          <div className="callout" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 10 }}>
            Gross exposure cannot be totalled here: {gross.unresolved.map((u) => `${u.instrument_id} (${u.reason})`).join('; ')}.
          </div>
        ) : (
          <p className="muted">No gross exposure cap in the active policy.</p>
        )}
        {gross ? (
          <dl className="kv">
            <dt>Positions at bid</dt>
            <dd>
              <Money amount={gross.positionsAtBid} currency={ccy} kind="marked" />
              {gross.lines.some((l) => l.fixture) ? (
                <>
                  {' '}
                  <FixtureBadge title="Marked with fixture quotes: simulation only" />
                </>
              ) : null}
            </dd>
            <dt>Positions at cost</dt>
            <dd>
              <Money amount={gross.positionsAtCost} currency={ccy} kind="cost" /> <span className="muted">positions without a bid</span>
            </dd>
            <dt>Open buy commitments</dt>
            <dd>
              <Money amount={gross.openBuys} currency={ccy} /> <span className="muted">leaves × limit × multiplier, {gross.buys.length} orders</span>
            </dd>
            <dt>Gross exposure</dt>
            <dd>
              {gross.total ? <Money amount={gross.total} currency={ccy} /> : <span className="muted">unavailable</span>}
              {gross.total && gross.lines.some((l) => l.fixture) ? (
                <>
                  {' '}
                  <FixtureBadge title="Includes positions marked with fixture quotes" />
                </>
              ) : null}
            </dd>
            <dt>Largest position</dt>
            <dd>
              {largest && largest.bps !== null ? (
                <>
                  <span className="num">{bpsToPercent(largest.bps)}</span> of session equity{' '}
                  {maxBps !== null ? <span className="muted">(limit {bpsToPercent(maxBps * 1n)})</span> : null}{' '}
                  <span className="mono">{largest.instrument_id}</span> in{' '}
                  <WorkspaceLink to="sessions" rest={[largest.session_id]}>
                    <ShortId value={largest.session_id} head={14} />
                  </WorkspaceLink>
                  {maxBps !== null && largest.bps > maxBps ? (
                    <>
                      {' '}
                      <StatusPill label="above limit" tone="bad" compact />
                    </>
                  ) : null}
                </>
              ) : (
                <span className="muted">no session positions</span>
              )}
            </dd>
          </dl>
        ) : null}
        <p className="rk-basis" style={{ margin: '8px 0 0' }}>
          Core definition (pre-trade check): every system-owned lot at bid, or at cost without a quote, plus the remaining commitment of every open buy.
          Concentration is position value over the session’s equity estimate; the core checks the position after a new buy at its limit price.
        </p>
      </div>
      {gross && gross.buys.length ? (
        <div className="panel-body flush" style={{ borderTop: '1px solid var(--border)' }}>
          <DataTable
            caption="Open buy commitments counted in gross exposure"
            columns={openBuyColumns(ccy)}
            rows={gross.buys}
            rowKey={(b) => b.order_id}
            maxHeight="240px"
          />
        </div>
      ) : null}
    </section>
  );
}

function openBuyColumns(ccy: string): Column<OpenBuyLine>[] {
  return [
    {
      id: 'order',
      header: 'Order',
      sortValue: (b) => b.order_id,
      cell: (b) => (
        <WorkspaceLink to="orders" rest={[b.order_id]}>
          <span className="cmd-id">{b.order_id}</span>
        </WorkspaceLink>
      ),
    },
    {
      id: 'session',
      header: 'Session',
      sortValue: (b) => b.session_id,
      cell: (b) => (
        <WorkspaceLink to="sessions" rest={[b.session_id]}>
          <ShortId value={b.session_id} head={14} />
        </WorkspaceLink>
      ),
    },
    { id: 'inst', header: 'Instrument', sortValue: (b) => b.instrument_id, cell: (b) => <span className="mono">{b.instrument_id}</span> },
    {
      id: 'commit',
      header: 'Commitment',
      description: 'Remaining quantity × limit price × multiplier, exact',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (b) => b.commitment,
      cell: (b) => <Money amount={b.commitment} currency={ccy} />,
    },
  ];
}

// ------------------------------------------------------------------ tables

function positionColumns(
  quotes: Record<string, Quote> | undefined,
  quotesAt: number | null,
  quoteMaxAgeMs: number | undefined,
  conc: Map<string, Concentration>,
  maxBps: bigint | null,
): Column<Position>[] {
  return [
    { id: 'owner', header: 'Owner', sortValue: (p) => p.owner, cell: (p) => <OwnerCell owner={p.owner} /> },
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
      cell: (p) => (
        <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          {p.mark_fixture ? <FixtureBadge /> : null}
          <Money amount={p.mark} currency={p.currency} kind="marked" unavailable="no quote" />
        </span>
      ),
    },
    {
      id: 'mv',
      header: 'Market value',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.market_value,
      cell: (p) => <Money amount={p.market_value} currency={p.currency} kind="marked" unavailable="no quote" />,
    },
    {
      id: 'unreal',
      header: 'Unrealized P&L',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (p) => p.unrealized,
      cell: (p) => <Money amount={p.unrealized} currency={p.currency} kind="unrealized" signed unavailable="no quote" />,
    },
    {
      id: 'conc',
      header: 'Of session equity',
      description: 'Position value (at bid, else cost) over the session’s equity estimate; limit from the active policy',
      numeric: true,
      sortKind: 'number',
      sortValue: (p) => {
        const c = conc.get(`${p.owner}/${p.instrument_id}`);
        return c?.bps === null || c?.bps === undefined ? null : Number(c.bps);
      },
      cell: (p) => {
        const c = conc.get(`${p.owner}/${p.instrument_id}`);
        if (!c || c.bps === null) return <span className="muted">not a session</span>;
        const over = maxBps !== null && c.bps > maxBps;
        return (
          <span className="num" title={`${c.value} of session equity ${c.sessionEquity} ${p.currency} (${c.basis})`}>
            {bpsToPercent(c.bps)}
            {over ? (
              <>
                {' '}
                <StatusPill label="above limit" tone="bad" compact />
              </>
            ) : null}
          </span>
        );
      },
    },
    {
      id: 'basis',
      header: 'Mark source and age',
      wrap: true,
      cell: (p) => {
        const q = quotes?.[p.instrument_id];
        if (!q) return <span title={p.mark_basis}>{p.mark_basis}</span>;
        return (
          <span title={`Server basis: ${p.mark_basis}`}>
            {q.bid === null ? 'no bid' : 'bid'} from <span className="mono">{q.source}</span>,{' '}
            <Age ageMs={q.age_ms} observedAtMs={quotesAt} staleAfterMs={quoteMaxAgeMs} /> old
            {q.delayed ? (
              <>
                {' '}
                <Badge tone="warn">delayed</Badge>
              </>
            ) : null}
            {q.fixture ? (
              <>
                {' '}
                <FixtureBadge />
              </>
            ) : null}
          </span>
        );
      },
    },
  ];
}

const bucketColumns: Column<Bucket>[] = [
  { id: 'owner', header: 'Owner bucket', sortValue: (b) => b.owner, cell: (b) => <OwnerCell owner={b.owner} systemOwned={b.system_owned} /> },
  {
    id: 'avail',
    header: 'Available cash',
    description: 'Settled cash plus payables and accrued fees, less active holds: what new orders in this bucket may reserve',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.available_cash,
    cell: (b) => <Money amount={b.available_cash} currency={b.currency} kind="available" />,
  },
  {
    id: 'settled',
    header: 'Settled cash',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.settled_cash,
    cell: (b) => <Money amount={b.settled_cash} currency={b.currency} kind="settled" />,
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
    id: 'inv',
    header: 'Inventory at cost',
    numeric: true,
    sortKind: 'decimal',
    sortValue: (b) => b.inventory_cost,
    cell: (b) => <Money amount={b.inventory_cost} currency={b.currency} kind="cost" />,
  },
];

// ------------------------------------------------------------------ workspace

export function RiskWorkspace({ navigate }: WorkspaceProps) {
  const risk = useQuery('/api/risk');
  const positions = useQuery('/api/positions', undefined, { pollMs: 3000 });
  const quotes = useQuery('/api/quotes', undefined, { pollMs: 3000 });
  const buckets = useQuery('/api/buckets');
  const openOrders = useQuery('/api/orders', { state: 'open', limit: 500 });
  const instruments = useQuery('/api/instruments');

  const r = risk.data?.risk ?? null;
  const policy = risk.data?.policy ?? null;
  const limits: Record<string, unknown> = policy?.body.account_limits ?? {};
  const ccy = r?.currency ?? policy?.body.currency ?? '';
  const timeZone = typeof limits.risk_day_timezone === 'string' ? limits.risk_day_timezone : null;
  const multipliers: Multipliers | null = instruments.data ? Object.fromEntries(instruments.data.map((i) => [i.instrument_id, i.multiplier])) : null;
  const quoteMaxAgeMs = typeof limits.quote_max_age_ms === 'number' ? limits.quote_max_age_ms : undefined;
  const maxBps = typeof limits.max_position_bps_of_session_equity === 'number' ? BigInt(limits.max_position_bps_of_session_equity) : null;
  const conc = positions.data && buckets.data ? concentrations(positions.data, buckets.data, ccy) : [];
  const concByKey = new Map(conc.map((c) => [`${c.owner}/${c.instrument_id}`, c]));
  const limitEntries: [string, unknown][] = [
    ...PRIMARY_LIMITS.filter((k) => k in limits).map((k): [string, unknown] => [`account_limits.${k}`, limits[k]]),
    ...Object.keys(limits)
      .filter((k) => !PRIMARY_LIMITS.includes(k))
      .map((k): [string, unknown] => [`account_limits.${k}`, limits[k]]),
  ];
  const systemLiquid = buckets.data?.filter((b) => b.system_owned && b.currency === ccy) ?? [];
  const policyLinked = hasWorkspace('policy');

  return (
    <WorkspaceFrame
      title="Risk"
      description="Loss budgets, exposure, limits, positions and cash. Recorded figures are shown as recorded; derived figures are exact and say how they were derived."
      queries={[risk, positions, quotes, buckets, openOrders, instruments]}
      actions={
        policyLinked ? (
          <button type="button" className="btn small" onClick={() => navigate('policy', 'draft')} title="Draft a new policy version from the active one">
            Tighten limits…
          </button>
        ) : null
      }
    >
      <div className="rk-ws">
        <QueryError what="The risk state" error={risk.error} />
        <QueryError what="Positions" error={positions.error} />
        <QueryError what="Quotes" error={quotes.error} />
        <QueryError what="Cash buckets" error={buckets.error} />
        <QueryError what="Open orders" error={openOrders.error} />
        <QueryError what="Instruments" error={instruments.error} />

        {risk.data && !r ? (
          <div className="callout" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 12 }}>
            No risk record for this account yet. It is created when the account is registered.
          </div>
        ) : null}
        {risk.data && !policy ? (
          <div className="callout" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 12 }}>
            No policy is active for this account, so there are no limits and the core will not start.
            {policyLinked ? (
              <>
                {' '}
                Publish and activate one under <a href="#/policy">Policy</a>.
              </>
            ) : null}
          </div>
        ) : null}

        <div className="rk-split">
          {r ? (
            <LossBudgetPanel
              risk={r}
              limits={limits}
              timeZone={timeZone}
              buckets={buckets.data}
              positions={positions.data}
              quotes={quotes.data}
              quotesFetchedAtMs={quotes.fetchedAtMs}
              multipliers={multipliers}
            />
          ) : (
            <section className="panel">
              <div className="panel-body muted">{risk.status === 'loading' ? 'Loading risk state…' : 'No risk state.'}</div>
            </section>
          )}
          <ExposurePanel ccy={ccy} limits={limits} positions={positions.data} openOrders={openOrders.data} multipliers={multipliers} conc={conc} />
        </div>

        <section className="panel section-gap" aria-labelledby="rk-limits">
          <header>
            <h2 id="rk-limits">Active limits</h2>
            {policy ? (
              <span className="rk-actions" style={{ fontSize: 'var(--fs-sm)' }}>
                <span>{policy.name}</span>
                <WorkspaceLink to="policy" rest={[policy.policy_hash]}>
                  <ShortId value={policy.policy_hash} head={12} />
                </WorkspaceLink>
                <span className="muted">
                  activated <Timestamp ns={policy.activation.activated_at_ns} nsStr={nsStrOf(policy.activation, 'activated_at_ns')} /> by {policy.activation.activated_by}
                </span>
              </span>
            ) : null}
          </header>
          {policy ? (
            <>
              <LimitCards entries={limitEntries} currency={policy.body.currency} ariaLabel="Account limits of the active policy" />
              <div className="panel-note">
                Liquidation on stop: <strong className="mono">{String(policy.body.on_stop_liquidation)}</strong>. A stop blocks new entries and cancels open
                buys; it never sells holdings, and the terminal does not offer liquidation. Account limits are read from the active policy whenever the core
                evaluates, so a newly activated policy governs the account at once.
              </div>
            </>
          ) : (
            <div className="panel-body muted">{risk.status === 'loading' ? 'Loading…' : 'No active policy.'}</div>
          )}
        </section>

        <section className="panel section-gap" aria-labelledby="rk-positions">
          <header>
            <h2 id="rk-positions">Positions</h2>
            <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
              Marked to bid; unrealized P&amp;L is market value minus cost basis. Refreshed every 3 s.
            </span>
          </header>
          <div className="panel-body flush">
            <DataTable
              caption="Open positions by owner and instrument, with marks"
              captionHidden
              columns={positionColumns(quotes.data, quotes.fetchedAtMs, quoteMaxAgeMs, concByKey, maxBps)}
              rows={positions.data ?? []}
              rowKey={(p) => `${p.owner}/${p.instrument_id}`}
              emptyText={positions.status === 'loading' ? 'Loading…' : 'No open positions.'}
            />
          </div>
        </section>

        <section className="panel section-gap" aria-labelledby="rk-buckets">
          <header>
            <h2 id="rk-buckets">Cash by owner bucket</h2>
            <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
              Buckets partition real balances; they never add up to more than the account holds.
            </span>
          </header>
          <div className="panel-body flush">
            <DataTable
              caption="Cash by owner bucket, with available cash"
              captionHidden
              columns={bucketColumns}
              rows={buckets.data ?? []}
              rowKey={(b) => `${b.owner}/${b.currency}`}
              emptyText={buckets.status === 'loading' ? 'Loading…' : 'No balances recorded.'}
            />
          </div>
          {systemLiquid.length ? (
            <div className="panel-note">
              {systemLiquid.length} system-owned {systemLiquid.length === 1 ? 'bucket' : 'buckets'} in {ccy}. Available cash is per bucket: settled cash plus
              payables and accrued fees, less active holds. Receivables count only once they settle.
            </div>
          ) : null}
        </section>
      </div>
    </WorkspaceFrame>
  );
}
