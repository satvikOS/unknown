/**
 * Instruments: the instrument master (GET /api/instruments) with tick rules, multiplier, settlement
 * cycle, provider mappings and attributes; fixture instruments are flagged FIXTURE. The latest quote
 * (GET /api/quotes, live process state, polled) always shows its source and age.
 * Route: `#/instruments` (list) and `#/instruments/<instrument_id>` (detail).
 */
import { useEffect, useMemo, useState } from 'react';
import { exactJson, nsStrOf } from '../../api/nsStrings';
import type { Instrument, Position, Quote } from '../../api/types';
import { useQuery } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, Badge, FixtureBadge, Money, Qty, StatusPill, Timestamp } from '../../ui/primitives';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import type { WorkspaceProps } from '../types';
import '../operations/ops.css';
import { attributeEntries, instrumentFlags, isFixture, mappingRows, matchInstrument, settlementLabel, tickAt, tickBands, tickSummary, type MappingRow, type TickBand } from './model';

const QUOTE_POLL_MS = 2000;

function QuoteView({ q, fetchedAtMs, staleAfterMs, compact }: { q: Quote | undefined; fetchedAtMs: number | null; staleAfterMs?: number; compact?: boolean }) {
  if (!q) return <span className="muted">no quote</span>;
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: compact ? 'nowrap' : 'wrap' }}>
      <Money amount={q.bid} currency={q.currency} kind="marked" unavailable="no bid" />
      <span className="muted">×</span>
      <Money amount={q.ask} currency={q.currency} unavailable="no ask" />
      <span className="muted">
        {q.source} · <Age ageMs={q.age_ms} observedAtMs={fetchedAtMs} staleAfterMs={staleAfterMs} />
      </span>
      {q.fixture ? <FixtureBadge title="Quote from the fixture market: generated for simulation, not market data" /> : null}
      {q.delayed ? <Badge tone="warn">delayed</Badge> : null}
    </span>
  );
}

export function InstrumentsWorkspace({ route, navigate }: WorkspaceProps) {
  const instruments = useQuery('/api/instruments');
  const quotes = useQuery('/api/quotes', undefined, { pollMs: QUOTE_POLL_MS });
  const policy = useQuery('/api/policy/active');
  const quoteMaxAge = policy.data?.body.account_limits.quote_max_age_ms;
  const staleAfterMs = quoteMaxAge !== undefined ? Number(quoteMaxAge) : undefined;
  const selectedId = route.rest[0] ?? null;
  const rows = instruments.data ?? [];
  const selected = selectedId ? rows.find((i) => i.instrument_id === selectedId) : undefined;

  return (
    <WorkspaceFrame
      title="Instruments"
      description="Instrument master: identifiers, trading rules, settlement cycle and provider mappings. A display symbol is never a key."
      queries={[instruments, quotes]}
    >
      <QueryError what="Instruments" error={instruments.error} />
      <QueryError what="Quotes" error={quotes.error} />
      {selectedId && instruments.data && !selected ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 10 }}>
          No instrument <code className="mono">{selectedId}</code> in the master. <a href="#/instruments">Show all instruments</a>
        </div>
      ) : null}
      {selected ? (
        <InstrumentDetail
          i={selected}
          quote={quotes.data?.[selected.instrument_id]}
          quotesAtMs={quotes.fetchedAtMs}
          staleAfterMs={staleAfterMs}
          onBack={() => navigate('instruments')}
        />
      ) : (
        <InstrumentList rows={rows} loading={instruments.status === 'loading'} quotes={quotes.data ?? {}} quotesAtMs={quotes.fetchedAtMs} staleAfterMs={staleAfterMs} navigate={navigate} />
      )}
    </WorkspaceFrame>
  );
}

function InstrumentList({
  rows,
  loading,
  quotes,
  quotesAtMs,
  staleAfterMs,
  navigate,
}: {
  rows: readonly Instrument[];
  loading: boolean;
  quotes: Record<string, Quote>;
  quotesAtMs: number | null;
  staleAfterMs: number | undefined;
  navigate: WorkspaceProps['navigate'];
}) {
  const [text, setText] = useState('');
  const [assetClass, setAssetClass] = useState('');
  const classes = useMemo(() => [...new Set(rows.map((i) => i.asset_class))].sort(), [rows]);
  const shown = useMemo(() => rows.filter((i) => (!assetClass || i.asset_class === assetClass) && matchInstrument(i, text)), [rows, text, assetClass]);
  const fixtures = rows.filter(isFixture).length;

  const columns: Column<Instrument>[] = [
    {
      id: 'symbol',
      header: 'Symbol',
      sortValue: (i) => i.symbol,
      cell: (i) => (
        <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          <a href={`#/instruments/${encodeURIComponent(i.instrument_id)}`} tabIndex={-1}>
            <strong>{i.symbol}</strong>
          </a>
          {instrumentFlags(i).map((f) => (
            <Badge key={f.label} tone={f.tone} title={f.detail}>
              {f.label}
            </Badge>
          ))}
        </span>
      ),
    },
    { id: 'name', header: 'Name', wrap: true, sortValue: (i) => i.name, cell: (i) => i.name },
    { id: 'id', header: 'Instrument id', sortValue: (i) => i.instrument_id, cell: (i) => <span className="cmd-id">{i.instrument_id}</span> },
    { id: 'class', header: 'Asset class', sortValue: (i) => i.asset_class, cell: (i) => i.asset_class },
    { id: 'venue', header: 'Venue (MIC)', sortValue: (i) => i.venue_mic, cell: (i) => <span className="mono">{i.venue_mic}</span> },
    {
      id: 'ccy',
      header: 'Trade / settle ccy',
      sortValue: (i) => i.currency,
      cell: (i) => (
        <span className="mono">
          {i.currency}
          {i.settlement_currency !== i.currency ? ` / ${i.settlement_currency}` : ''}
        </span>
      ),
    },
    { id: 'mult', header: 'Multiplier', numeric: true, sortKind: 'decimal', sortValue: (i) => i.multiplier, cell: (i) => <Qty value={i.multiplier} /> },
    {
      id: 'qty',
      header: 'Qty increment / min',
      numeric: true,
      sortKind: 'decimal',
      sortValue: (i) => i.quantity_increment,
      cell: (i) => (
        <span>
          <Qty value={i.quantity_increment} /> <span className="muted">/</span> <Qty value={i.min_quantity} />
        </span>
      ),
    },
    {
      id: 'settle',
      header: 'Settlement',
      sortKind: 'number',
      sortValue: (i) => i.settlement_days,
      cell: (i) => <span title={`${i.settlement_days} business day(s) after trade date on the venue calendar`}>{settlementLabel(i.settlement_days)}</span>,
    },
    { id: 'ticks', header: 'Tick rules', wrap: true, cell: (i) => <span className="mono">{tickSummary(i.tick_rules)}</span> },
    {
      id: 'status',
      header: 'Status',
      sortValue: (i) => i.status,
      cell: (i) => <StatusPill label={i.status} tone={i.status === 'ACTIVE' ? 'ok' : 'bad'} compact />,
    },
    {
      id: 'maps',
      header: 'Provider mappings',
      sortKind: 'number',
      sortValue: (i) => i.mappings.length,
      cell: (i) =>
        i.mappings.length ? (
          <span className="cmd-id">
            {mappingRows(i.mappings)
              .filter((m) => m.current)
              .map((m) => `${m.provider}:${m.external_id}`)
              .join(', ')}
          </span>
        ) : (
          <span className="muted">none</span>
        ),
    },
    {
      id: 'quote',
      header: 'Latest quote (bid × ask, source, age)',
      cell: (i) => <QuoteView q={quotes[i.instrument_id]} fetchedAtMs={quotesAtMs} staleAfterMs={staleAfterMs} compact />,
    },
  ];

  return (
    <>
      <div className="ops-toolbar">
        <div className="field" style={{ minWidth: 260 }}>
          <label htmlFor="inst-search">Search</label>
          <input
            id="inst-search"
            className="input"
            value={text}
            placeholder="symbol, name, id, venue, provider id…"
            autoComplete="off"
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="inst-class">Asset class</label>
          <select id="inst-class" className="input" value={assetClass} onChange={(e) => setAssetClass(e.target.value)}>
            <option value="">All classes</option>
            {classes.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>
        <span className="spacer" />
        <span className="ops-hint">
          {rows.length} instrument{rows.length === 1 ? '' : 's'}
          {fixtures ? ` · ${fixtures} FIXTURE (simulation only)` : ''} · quotes polled every {QUOTE_POLL_MS / 1000} s
        </span>
      </div>
      <section className="panel">
        <div className="panel-body flush">
          <DataTable
            caption={`Instrument master: ${shown.length} of ${rows.length}`}
            captionHidden
            columns={columns}
            rows={shown}
            rowKey={(i) => i.instrument_id}
            initialSort={{ columnId: 'symbol', direction: 'ascending' }}
            onRowActivate={(i) => navigate('instruments', i.instrument_id)}
            activateLabel="open the instrument"
            emptyText={loading ? 'Loading…' : rows.length ? 'No instrument matches.' : 'No instruments registered yet.'}
          />
        </div>
      </section>
      <p className="ops-hint">Press Enter on a focused row (or double-click it) for tick bands, every mapping version, attributes and holdings.</p>
    </>
  );
}

const bandColumns = (currency: string, current: string | null): Column<TickBand>[] => [
  { id: 'from', header: 'From price (inclusive)', numeric: true, cell: (b) => <Money amount={b.from} currency={currency} /> },
  {
    id: 'to',
    header: 'Up to (exclusive)',
    numeric: true,
    cell: (b) => (b.to !== null ? <Money amount={b.to} currency={currency} /> : <span className="muted">no upper bound</span>),
  },
  {
    id: 'tick',
    header: 'Tick size',
    numeric: true,
    cell: (b) => (
      <span>
        <Money amount={b.tick} currency={currency} />
        {current !== null && b.tick === current ? (
          <>
            {' '}
            <Badge tone="info" title="The band containing the current bid">
              current
            </Badge>
          </>
        ) : null}
      </span>
    ),
  },
];

const mappingColumns: Column<MappingRow>[] = [
  { id: 'provider', header: 'Provider', sortValue: (m) => m.provider, cell: (m) => <span className="mono">{m.provider}</span> },
  { id: 'ext', header: 'External id', sortValue: (m) => m.external_id, cell: (m) => <span className="cmd-id">{m.external_id}</span> },
  { id: 'version', header: 'Version', numeric: true, sortKind: 'number', sortValue: (m) => m.version, cell: (m) => m.version },
  {
    id: 'current',
    header: 'Status',
    sortValue: (m) => m.current,
    cell: (m) => <StatusPill label={m.current ? 'current' : 'superseded'} tone={m.current ? 'ok' : 'neutral'} compact />,
  },
  {
    id: 'verified',
    header: 'Verified',
    sortKind: 'number',
    sortValue: (m) => m.verified_at_ns,
    cell: (m) => <Timestamp ns={m.verified_at_ns} nsStr={nsStrOf(m, 'verified_at_ns')} never="not verified" />,
  },
  {
    id: 'details',
    header: 'Details',
    wrap: true,
    cell: (m) => (Object.keys(m.details ?? {}).length ? <span className="cmd-id">{exactJson(m.details)}</span> : <span className="muted">none</span>),
  },
];

function InstrumentDetail({
  i,
  quote,
  quotesAtMs,
  staleAfterMs,
  onBack,
}: {
  i: Instrument;
  quote: Quote | undefined;
  quotesAtMs: number | null;
  staleAfterMs: number | undefined;
  onBack: () => void;
}) {
  const positions = useQuery('/api/positions');
  const held: Position[] = (positions.data ?? []).filter((p) => p.instrument_id === i.instrument_id);
  const bands = tickBands(i.tick_rules);
  const currentTick = tickAt(i.tick_rules, quote?.bid ?? quote?.last ?? null);
  const flags = instrumentFlags(i);
  const attrs = attributeEntries(i.attributes);
  // The list row that opened this detail is gone: move focus to the instrument heading.
  useEffect(() => {
    document.getElementById('inst-title')?.focus();
  }, [i.instrument_id]);

  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <section className="panel" aria-labelledby="inst-title">
        <header>
          <h2 id="inst-title" tabIndex={-1} style={{ textTransform: 'none', letterSpacing: 0, fontSize: 'var(--fs-lg)' }}>
            {i.symbol} <span className="muted" style={{ fontWeight: 400 }}>{i.name}</span>
          </h2>
          <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
            {flags.map((f) => (
              <Badge key={f.label} tone={f.tone} title={f.detail}>
                {f.label}
              </Badge>
            ))}
            <button type="button" className="btn small" onClick={onBack}>
              All instruments
            </button>
          </span>
        </header>
        {isFixture(i) ? (
          <div className="panel-note" style={{ color: 'var(--warn)' }}>
            FIXTURE: registered for simulation only. Its reference data and quotes are generated, not market data.
          </div>
        ) : null}
        <div className="panel-body">
          <div className="grid grid-2">
            <dl className="kv">
              <dt>Instrument id</dt>
              <dd className="cmd-id">{i.instrument_id}</dd>
              <dt>Asset class</dt>
              <dd>{i.asset_class}</dd>
              <dt>Venue (MIC)</dt>
              <dd className="mono">{i.venue_mic}</dd>
              <dt>Trading currency</dt>
              <dd className="mono">{i.currency}</dd>
              <dt>Settlement currency</dt>
              <dd className="mono">{i.settlement_currency}</dd>
              <dt>Settlement cycle</dt>
              <dd>
                {settlementLabel(i.settlement_days)} <span className="muted">({i.settlement_days} business day{i.settlement_days === 1 ? '' : 's'} after trade date)</span>
              </dd>
              <dt>Status</dt>
              <dd>
                <StatusPill label={i.status} tone={i.status === 'ACTIVE' ? 'ok' : 'bad'} compact />
              </dd>
              <dt>Metadata version</dt>
              <dd className="num">{i.metadata_version}</dd>
            </dl>
            <dl className="kv">
              <dt>Multiplier</dt>
              <dd>
                <Qty value={i.multiplier} /> <span className="muted">(notional = quantity × price × multiplier)</span>
              </dd>
              <dt>Quantity increment</dt>
              <dd>
                <Qty value={i.quantity_increment} />
              </dd>
              <dt>Minimum quantity</dt>
              <dd>
                <Qty value={i.min_quantity} />
              </dd>
              <dt>Leveraged / inverse</dt>
              <dd>
                {i.leveraged ? 'leveraged' : 'not leveraged'}, {i.inverse ? 'inverse' : 'not inverse'}
              </dd>
              <dt>Latest quote</dt>
              <dd>
                <QuoteView q={quote} fetchedAtMs={quotesAtMs} staleAfterMs={staleAfterMs} />
              </dd>
              {quote ? (
                <>
                  <dt>Last</dt>
                  <dd>
                    <Money amount={quote.last} currency={quote.currency} unavailable="no last" />{' '}
                    {quote.event_time_ns ? (
                      <span className="muted">
                        event time <Timestamp ns={quote.event_time_ns} nsStr={nsStrOf(quote, 'event_time_ns')} withMs />
                      </span>
                    ) : null}
                  </dd>
                  <dt>Tick at the bid</dt>
                  <dd>{currentTick ? <Money amount={currentTick} currency={i.currency} /> : <span className="muted">no band covers this price</span>}</dd>
                </>
              ) : null}
            </dl>
          </div>
        </div>
      </section>

      <div className="grid grid-2">
        <section className="panel" aria-labelledby="inst-ticks">
          <header>
            <h2 id="inst-ticks">Tick rules</h2>
            <span className="ops-hint">a rule applies from its price until the next rule’s price</span>
          </header>
          <div className="panel-body flush">
            <DataTable caption="Tick size bands" captionHidden columns={bandColumns(i.currency, currentTick)} rows={bands} rowKey={(b) => b.from} emptyText="No tick rules." />
          </div>
        </section>
        <section className="panel" aria-labelledby="inst-attrs">
          <header>
            <h2 id="inst-attrs">Attributes</h2>
            <span className="ops-hint">namespaced extras from the provider or the registration</span>
          </header>
          <div className="panel-body">
            {attrs.length ? (
              <dl className="kv">
                {attrs.map(([k, v]) => (
                  <div key={k} style={{ display: 'contents' }}>
                    <dt className="mono">{k}</dt>
                    <dd>
                      {k === 'fixture' && (v === true || v === 'true') ? (
                        <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                          <FixtureBadge /> true: simulation only
                        </span>
                      ) : typeof v === 'object' && v !== null ? (
                        <span className="cmd-id">{exactJson(v)}</span>
                      ) : (
                        <span className="mono">{String(v)}</span>
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                No attributes.
              </p>
            )}
          </div>
        </section>
      </div>

      <section className="panel" aria-labelledby="inst-maps">
        <header>
          <h2 id="inst-maps">Provider mappings</h2>
          <span className="ops-hint">every version; the highest per provider is current</span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption="Provider identifier mappings"
            captionHidden
            columns={mappingColumns}
            rows={mappingRows(i.mappings)}
            rowKey={(m) => `${m.provider}/${m.version}/${m.external_id}`}
            emptyText="No provider mappings: the instrument cannot be routed to or matched from any provider."
          />
        </div>
      </section>

      <section className="panel" aria-labelledby="inst-held">
        <header>
          <h2 id="inst-held">Holdings by owner</h2>
          <span className="ops-hint">open lots, marked to bid</span>
        </header>
        <div className="panel-body flush">
          <DataTable
            caption="Open positions in this instrument"
            captionHidden
            columns={[
              { id: 'owner', header: 'Owner', sortValue: (p) => p.owner, cell: (p) => <span className="cmd-id">{p.owner}</span> },
              { id: 'qty', header: 'Quantity', numeric: true, sortKind: 'decimal', sortValue: (p) => p.quantity, cell: (p) => <Qty value={p.quantity} /> },
              {
                id: 'cost',
                header: 'Cost basis',
                numeric: true,
                sortKind: 'decimal',
                sortValue: (p) => p.cost_basis,
                cell: (p) => <Money amount={p.cost_basis} currency={p.currency} kind="cost" />,
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
                id: 'basis',
                header: 'Mark source and age',
                wrap: true,
                cell: (p) => (
                  <span>
                    {p.mark_fixture ? <FixtureBadge /> : null} {p.mark_fixture ? p.mark_basis.replace(/ FIXTURE$/, '') : p.mark_basis}
                  </span>
                ),
              },
            ]}
            rows={held}
            rowKey={(p) => p.owner}
            emptyText={positions.status === 'loading' ? 'Loading…' : 'No open position in this instrument.'}
          />
        </div>
      </section>
    </div>
  );
}
