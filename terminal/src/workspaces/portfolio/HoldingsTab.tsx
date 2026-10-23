/**
 * Holdings by owner (CAP-PORT-01): NAV per currency, each owner (policy bucket or session) with its
 * cash and marked value, positions with their marks, and the cash ladder. Read from
 * GET /api/portfolio/holdings; every amount is the API's exact decimal string with its currency.
 */
import { useState, type ReactNode } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, Badge, FixtureBadge, Money, Qty, StatusPill } from '../../ui/primitives';
import { QueryError, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { Segmented } from '../operations/parts';
import type { QueryResult } from '../../state/react';
import { bpsToPercent } from '../../format/exact';
import { BarList } from './charts';
import { dec, exactShares, isRec, num, ofKind, ownerLabel, recs, shortOwnerLabel, str, valuationTone, weightPct, type Rec, type ScopeOption } from './model';
import { PortfolioFrame, FixtureNote, Lineage, OwnerName, QuoteSnapshotNote, ScopeSelect, Toolbar, type FormulaDef } from './parts';

type Basis = 'bid' | 'mid' | 'last';
const BASES: readonly { value: Basis; label: string }[] = [
  { value: 'bid', label: 'Bid' },
  { value: 'mid', label: 'Mid' },
  { value: 'last', label: 'Last' },
];

const statusTone = valuationTone;

/**
 * The cash ladder (analytics/holdings.py `account_cash_ladder`). The API builds it over every owner in
 * the account whatever the scope selected, so it is labelled as account-wide.
 */
export function CashLadder({ ladder, scope }: { ladder: readonly Rec[]; scope: string | null }) {
  const scoped = scope !== null && scope !== 'account';
  return (
    <section className="panel" aria-labelledby="pf-ladder-h">
      <header>
        <h2 id="pf-ladder-h">Cash ladder, whole account</h2>
        <span className="pf-hint">{scoped ? `every owner, not only scope ${scope}` : 'every owner'}</span>
      </header>
      <div className="panel-body">
        {ladder.map((c) => {
          const ccy = str(c.currency) ?? '';
          return (
            <dl className="kv" key={ccy}>
              <dt>Ledger settled cash</dt>
              <dd>
                <Money amount={dec(c.ledger_settled_cash)} currency={ccy} kind="settled" />
              </dd>
              <dt>Receivable (unsettled)</dt>
              <dd>
                <Money amount={dec(c.receivable_unsettled)} currency={ccy} kind="unsettled" />
              </dd>
              <dt>Payable (unsettled)</dt>
              <dd>
                <Money amount={dec(c.payable_unsettled)} currency={ccy} kind="unsettled" />
              </dd>
              <dt>Fees accrued</dt>
              <dd>
                <Money amount={dec(c.fees_accrued)} currency={ccy} kind="estimated" />
              </dd>
              <dt>Reserved (holds)</dt>
              <dd>
                <Money amount={dec(c.reserved_cash_holds)} currency={ccy} kind="held" />
              </dd>
              <dt>Unallocated available</dt>
              <dd>
                <Money amount={dec(c.unallocated_available)} currency={ccy} kind="available" />
              </dd>
              <dt>Broker settled cash</dt>
              <dd>{dec(c.broker_settled_cash) ? <Money amount={dec(c.broker_settled_cash)} currency={ccy} /> : <span className="pf-hint">{str(c.broker_settled_cash_reason) ?? '—'}</span>}</dd>
            </dl>
          );
        })}
        {!ladder.length ? <p className="muted">No cash in the account.</p> : null}
      </div>
    </section>
  );
}

export interface HoldingsProps {
  bar: ReactNode;
  q: QueryResult<unknown>;
  scope: string;
  setScope: (s: string) => void;
  basis: Basis;
  setBasis: (b: Basis) => void;
  options: readonly ScopeOption[];
  formulas: Map<string, FormulaDef>;
}

export function HoldingsTab({ bar, q, scope, setScope, basis, setBasis, options, formulas }: HoldingsProps) {
  const [weightBasis, setWeightBasis] = useState<'scope_nav' | 'owner_nav'>('scope_nav');
  const body = ofKind(q.data, 'holdings');
  const totals = body && isRec(body.totals) ? Object.values(body.totals).filter(isRec) : [];
  const owners = body ? recs(body.owners) : [];
  const positions = body ? recs(body.positions) : [];
  const ladder = body ? recs(body.cash_ladder) : [];
  const mp = body && isRec(body.mark_policy) ? body.mark_policy : null;

  const ownerCols: Column<Rec>[] = [
    {
      id: 'owner',
      header: 'Owner',
      cell: (o) => (
        <span className="pf-label">
          <OwnerName owner={str(o.owner) ?? '?'} />
          {o.system_owned === true ? <Badge tone="info">system</Badge> : <Badge>outside</Badge>}
        </span>
      ),
      sortValue: (o) => str(o.owner) ?? '',
    },
    { id: 'kind', header: 'Kind', cell: (o) => str(o.kind) ?? '—', sortValue: (o) => str(o.kind) ?? '' },
    { id: 'settled', header: 'Settled cash', numeric: true, cell: (o) => <Money amount={dec(isRec(o.cash) ? o.cash.settled_cash : null)} currency={str(o.currency) ?? ''} kind="settled" /> },
    { id: 'available', header: 'Available', numeric: true, description: 'Settled cash minus active holds; unsettled sale proceeds add nothing', cell: (o) => <Money amount={dec(isRec(o.cash) ? o.cash.available_cash : null)} currency={str(o.currency) ?? ''} kind="available" /> },
    { id: 'mv', header: 'Market value (fresh)', numeric: true, cell: (o) => <Money amount={dec(o.market_value_fresh)} currency={str(o.currency) ?? ''} kind="marked" /> },
    { id: 'navm', header: 'NAV (marked)', numeric: true, description: 'Uses last marks, stale ones flagged; null when any position is unpriced', cell: (o) => <Money amount={dec(o.nav_marked)} currency={str(o.currency) ?? ''} unavailable="unpriced position" />, sortValue: (o) => dec(o.nav_marked), sortKind: 'decimal' },
    { id: 'navf', header: 'NAV (fresh)', numeric: true, description: 'Published only when every position has a fresh mark', cell: (o) => <Money amount={dec(o.nav_fresh)} currency={str(o.currency) ?? ''} unavailable="not fresh" /> },
    { id: 'status', header: 'Valuation', cell: (o) => <StatusPill label={str(o.valuation_status) ?? '?'} tone={statusTone(str(o.valuation_status))} compact /> },
  ];

  const posCols: Column<Rec>[] = [
    { id: 'owner', header: 'Owner', cell: (p) => <OwnerName owner={str(p.owner) ?? '?'} />, sortValue: (p) => str(p.owner) ?? '' },
    {
      id: 'instrument',
      header: 'Instrument',
      cell: (p) => (
        <span className="pf-label">
          <WorkspaceLink to="instruments" rest={[str(p.instrument_id) ?? '']}>
            <span className="cmd-id">{str(p.instrument_id)}</span>
          </WorkspaceLink>
          <span className="muted">{str(p.symbol)}</span>
          {p.fixture === true || p.instrument_fixture === true ? <FixtureBadge /> : null}
        </span>
      ),
      sortValue: (p) => str(p.instrument_id) ?? '',
    },
    { id: 'qty', header: 'Quantity', numeric: true, cell: (p) => <Qty value={dec(p.quantity)} />, sortValue: (p) => dec(p.quantity), sortKind: 'decimal' },
    { id: 'cost', header: 'Cost basis', numeric: true, cell: (p) => <Money amount={dec(p.cost_basis)} currency={str(p.currency) ?? ''} kind="cost" /> },
    {
      id: 'mark',
      header: 'Mark',
      numeric: true,
      description: 'Price at the chosen basis from the core quote book, its status, source and age at valuation',
      cell: (p) => {
        const m = isRec(p.mark) ? p.mark : {};
        return (
          <span className="pf-label" title={str(m.reason) ?? str(m.source) ?? undefined}>
            <Money amount={dec(m.price)} currency={str(p.currency) ?? ''} unavailable={str(m.reason) ?? 'no mark'} />
            <StatusPill label={str(m.status) ?? '?'} tone={statusTone(str(m.status))} compact detail={str(m.reason) ?? undefined} />
            {num(m.age_ms) !== null ? <Age ageMs={num(m.age_ms)} /> : null}
          </span>
        );
      },
    },
    { id: 'mv', header: 'Market value', numeric: true, cell: (p) => <Money amount={dec(p.market_value)} currency={str(p.currency) ?? ''} kind="marked" unavailable="unpriced: not valued at zero" />, sortValue: (p) => dec(p.market_value), sortKind: 'decimal' },
    { id: 'unrl', header: 'Unrealized', numeric: true, cell: (p) => <Money amount={dec(p.unrealized)} currency={str(p.currency) ?? ''} kind="unrealized" signed unavailable="unpriced" /> },
    {
      id: 'weight',
      header: weightBasis === 'scope_nav' ? 'Weight of scope NAV' : 'Weight of owner NAV',
      numeric: true,
      cell: (p) => weightPct(isRec(p.weights) ? dec(p.weights[weightBasis]) : null),
    },
    { id: 'settle', header: 'Unsettled qty', numeric: true, cell: (p) => <Qty value={dec(p.unsettled_quantity)} /> },
  ];

  const shareItems = totals.flatMap((t) => {
    const ccy = str(t.currency) ?? '';
    const parts = exactShares(owners.filter((o) => str(o.currency) === ccy).map((o) => ({ key: str(o.owner) ?? '?', value: dec(o.nav_marked) })));
    return parts.parts.map((p) => ({ key: `${ccy}:${p.key}`, label: shortOwnerLabel(p.key), title: ownerLabel(p.key), value: p.value, currency: ccy, note: `${bpsToPercent(p.bps)} of positive NAV` }));
  });
  // Owners the share bars cannot show: an unavailable NAV (unpriced position) or a negative one. Named, never dropped silently.
  const notCharted = owners.flatMap((o) => {
    const nav = dec(o.nav_marked);
    const who = `${ownerLabel(str(o.owner) ?? '?')} (${str(o.currency) ?? '?'})`;
    if (nav === null) return [`${who}: NAV unavailable, an unpriced position`];
    return nav.startsWith('-') && !/^-0(\.0*)?$/.test(nav) ? [`${who}: negative NAV`] : [];
  });

  return (
    <PortfolioFrame tab="holdings" bar={bar} queries={[q]}>
      <Toolbar>
        <ScopeSelect id="pf-h-scope" value={scope} options={options} onChange={setScope} />
        <div className="field">
          <span className="pf-hint" id="pf-basis-l">
            Mark basis
          </span>
          <Segmented label="Mark basis" options={BASES} value={basis} onChange={setBasis} />
        </div>
        <span className="spacer" />
        {mp ? (
          <span className="pf-hint">
            Marks: {str(mp.basis)} from {str(mp.source)}; fresh within {num(mp.max_age_ms)?.toLocaleString('en-US') ?? '?'} ms (policy budget)
          </span>
        ) : null}
      </Toolbar>
      <QueryError what="Holdings" error={q.error} />
      <QuoteSnapshotNote body={body} />
      <FixtureNote body={body} />
      {body ? (
        <>
          <div className="pf-kpis">
            {totals.map((t) => {
              const ccy = str(t.currency) ?? '';
              return (
                <div className="pf-kpi" key={ccy}>
                  <div className="k">NAV marked, {ccy}</div>
                  <div className="v">
                    <Money amount={dec(t.nav_marked)} currency={ccy} unavailable="unpriced position" />
                  </div>
                  <div className="s">
                    fresh: <Money amount={dec(t.nav_fresh)} currency={ccy} unavailable="not all marks fresh" /> ·{' '}
                    <StatusPill label={str(t.valuation_status) ?? '?'} tone={statusTone(str(t.valuation_status))} compact />
                  </div>
                </div>
              );
            })}
            {totals.map((t) => {
              const ccy = str(t.currency) ?? '';
              return (
                <div className="pf-kpi" key={`cash-${ccy}`}>
                  <div className="k">Available cash, {ccy}</div>
                  <div className="v">
                    <Money amount={dec(t.available_cash)} currency={ccy} />
                  </div>
                  <div className="s">
                    liquid <Money amount={dec(t.liquid_value)} currency={ccy} /> · {num(t.owners) ?? '?'} owner(s)
                  </div>
                </div>
              );
            })}
            {dec(body.authorized_total) ? (
              <div className="pf-kpi">
                <div className="k">Authorized capital</div>
                <div className="v">
                  <Money amount={dec(body.authorized_total)} currency={str(body.base_currency) ?? ''} kind="authorized" />
                </div>
                <div className="s">the account's authorized total, whatever the scope</div>
              </div>
            ) : null}
          </div>

          <div className="pf-split">
            <section className="panel" aria-labelledby="pf-nav-h">
              <header>
                <h2 id="pf-nav-h">NAV by owner</h2>
                <span className="pf-hint">buckets partition NAV; they never add to it</span>
              </header>
              <div className="panel-body">
                {shareItems.length ? <BarList items={shareItems} label="Marked NAV by owner" /> : <p className="muted" style={{ margin: 0 }}>No owner has a positive marked NAV in this scope.</p>}
                {shareItems.length ? (
                  <p className="pf-hint" style={{ margin: '6px 0 0' }}>
                    Shares are rounded to 0.01% by largest remainder, so in each currency they add up to exactly 100.00%; each is within 0.01% of its exact value.
                  </p>
                ) : null}
                {notCharted.length ? <p className="pf-hint" style={{ margin: '6px 0 0' }}>Not in the chart: {notCharted.join('; ')}.</p> : null}
              </div>
            </section>
            <CashLadder ladder={ladder} scope={str(body.scope)} />
          </div>

          <section className="panel pf-section" aria-labelledby="pf-owners-h">
            <header>
              <h2 id="pf-owners-h">Owners</h2>
              <span className="pf-hint">{owners.length} owner row(s)</span>
            </header>
            <div className="panel-body flush">
              <DataTable caption="Holdings by owner" captionHidden columns={ownerCols} rows={owners} rowKey={(o) => `${str(o.owner)}|${str(o.currency)}`} emptyText="No owners in this scope." maxHeight="360px" />
            </div>
          </section>

          <section className="panel pf-section" aria-labelledby="pf-pos-h">
            <header>
              <h2 id="pf-pos-h">Positions</h2>
              <Segmented
                label="Weight denominator"
                options={[
                  { value: 'scope_nav', label: 'of scope NAV' },
                  { value: 'owner_nav', label: 'of owner NAV' },
                ]}
                value={weightBasis}
                onChange={setWeightBasis}
              />
            </header>
            <div className="panel-body flush">
              <DataTable
                caption="Positions with marks"
                captionHidden
                columns={posCols}
                rows={positions}
                rowKey={(p) => `${str(p.owner)}|${str(p.instrument_id)}`}
                emptyText="No positions in this scope."
                maxHeight="420px"
              />
            </div>
          </section>
          <Lineage body={body} formulas={formulas} />
        </>
      ) : q.status === 'loading' ? (
        <p className="muted">Loading holdings…</p>
      ) : null}
    </PortfolioFrame>
  );
}
