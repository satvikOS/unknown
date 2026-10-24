/** Small presentational pieces shared by the orders views. Every value shown is the server's own. */
import { useId, useMemo, useState } from 'react';
import type { Instrument, OrderHold, Quote } from '../../api/types';
import { useQuery, type QueryResult } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, Badge, FixtureRefused, Money, Qty, StatusPill, type Tone } from '../../ui/primitives';
import { WorkspaceLink } from '../../ui/WorkspaceFrame';
import { summarizeDecision, type PretradeCheck, type PretradeDecision } from './decision';
import { NoValue } from '../../ui/unavailable';

/** Instruments by id (one cached query shared by every orders view). */
export interface InstrumentData {
  map: ReadonlyMap<string, Instrument>;
  list: readonly Instrument[];
  query: QueryResult<Instrument[]>;
}

export function useInstrumentMap(): InstrumentData {
  const query = useQuery('/api/instruments');
  const list = query.data ?? EMPTY;
  const map = useMemo(() => new Map(list.map((i) => [i.instrument_id, i])), [list]);
  return { map, list, query };
}
const EMPTY: readonly Instrument[] = [];

export function isFixtureInstrument(i: Instrument | undefined): boolean {
  return Boolean(i && i.attributes && i.attributes.fixture === true);
}

/** Symbol + id, linked to the instruments workspace when it exists; a fixture (not real) instrument is an error. */
export function InstrumentCell({ id, instrument }: { id: string; instrument: Instrument | undefined }) {
  return (
    <span className="ord-inline">
      <WorkspaceLink to="instruments" rest={[id]}>
        <strong>{instrument?.symbol ?? id}</strong>
      </WorkspaceLink>
      {instrument ? <span className="muted cmd-id">{id}</span> : null}
      {isFixtureInstrument(instrument) ? <FixtureRefused detail="This instrument is not a real security." /> : null}
    </span>
  );
}

/** BUY / SELL in text with a direction glyph (never colour alone). */
export function SideText({ side }: { side: string }) {
  const buy = side === 'BUY';
  return (
    <span className="ord-side" data-side={side}>
      <span aria-hidden="true">{buy ? '▲' : side === 'SELL' ? '▼' : '·'}</span>
      {side}
    </span>
  );
}

/** One hold: cash in its currency, quantity in its instrument, with ACTIVE / RELEASED in text. */
export function HoldValue({ hold }: { hold: OrderHold }) {
  const active = hold.status === 'ACTIVE';
  return (
    <span className="ord-inline">
      {hold.kind === 'CASH' ? <Money amount={hold.amount} currency={hold.unit} kind="held" /> : <Qty value={hold.amount} unit={hold.unit} />}
      <StatusPill label={active ? 'held' : 'released'} tone={active ? 'info' : 'neutral'} compact />
    </span>
  );
}

export function HoldsCell({ holds }: { holds: readonly OrderHold[] | undefined }) {
  if (!holds || holds.length === 0) return <span className="muted">none</span>;
  return (
    <span className="ord-stack">
      {holds.map((h, i) => (
        <HoldValue key={`${h.kind}-${i}`} hold={h} />
      ))}
    </span>
  );
}

// ------------------------------------------------------------------ quotes

export interface QuoteLineProps {
  quote: Quote | null | undefined;
  /** When the quote was read (browser ms): the shown age keeps growing from the server's age_ms. */
  observedAtMs?: number | null;
  /** Policy quote_max_age_ms: beyond it the age is labelled stale. */
  staleAfterMs?: number | undefined;
  /** "live" for the current book, "at decision" for the quote recorded with a pretrade decision. */
  context: 'live' | 'at decision';
}

/** Bid / ask / last with currency, the source, the age and the DELAYED label. A fixture quote is an error, never shown. */
export function QuoteLine({ quote, observedAtMs, staleAfterMs, context }: QuoteLineProps) {
  if (!quote) {
    return (
      <p className="muted ord-quote-none">
        {context === 'live' ? 'The core holds no quote for this instrument, so the QUOTE_FRESH check would fail.' : 'No quote was recorded with this decision.'}
      </p>
    );
  }
  if (quote.fixture) {
    return (
      <p className="ord-quote-none" role="alert">
        <FixtureRefused label="Quote" /> The quote from {quote.source} is not real market data and is not shown.
      </p>
    );
  }
  return (
    <div className="ord-quote">
      <dl className="ord-quote-grid">
        <div>
          <dt>Bid</dt>
          <dd>
            <Money amount={quote.bid} currency={quote.currency} unavailable="The quote has no bid." />
          </dd>
        </div>
        <div>
          <dt>Ask</dt>
          <dd>
            <Money amount={quote.ask} currency={quote.currency} unavailable="The quote has no ask." />
          </dd>
        </div>
        <div>
          <dt>Last</dt>
          <dd>
            <Money amount={quote.last} currency={quote.currency} unavailable="The quote has no last price." />
          </dd>
        </div>
      </dl>
      <div className="ord-quote-meta">
        <span>
          source <span className="mono">{quote.source}</span>
        </span>
        <span>
          {context === 'live' ? (
            <>
              age <Age ageMs={quote.age_ms} observedAtMs={observedAtMs ?? null} staleAfterMs={staleAfterMs} />
            </>
          ) : (
            <>
              age <span className="num">{quote.age_ms} ms</span> when evaluated
            </>
          )}
        </span>
        {quote.delayed ? (
          <Badge tone="warn" title="Delayed quotes cannot validate an order (QUOTE_NOT_DELAYED)">
            DELAYED
          </Badge>
        ) : null}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ pretrade decision

function checkTone(c: PretradeCheck, decisive: PretradeCheck | null): Tone {
  if (c.passed) return 'ok';
  return c === decisive ? 'bad' : 'warn';
}

/** Every check of a pretrade decision, in the core's evaluation order, as a semantic table. */
export function DecisionTable({ decision, caption }: { decision: PretradeDecision; caption: string }) {
  const summary = summarizeDecision(decision);
  const [failedOnly, setFailedOnly] = useState(false);
  const toggleId = useId();
  const order = useMemo(() => new Map(decision.checks.map((c, i) => [c, i])), [decision]);
  const columns: Column<PretradeCheck>[] = [
    {
      id: 'n',
      header: '#',
      description: 'Evaluation order in the core',
      numeric: true,
      sortKind: 'number',
      sortValue: (c) => order.get(c) ?? 0,
      cell: (c) => (order.get(c) ?? 0) + 1,
      width: 40,
    },
    { id: 'check', header: 'Check', sortValue: (c) => c.check, cell: (c) => <code className="mono">{c.check}</code> },
    {
      id: 'result',
      header: 'Result',
      sortValue: (c) => (c.passed ? 1 : 0),
      sortKind: 'number',
      cell: (c) => (
        <span className="ord-inline">
          <StatusPill label={c.passed ? 'pass' : 'fail'} tone={checkTone(c, summary.decisive)} compact />
          {c === summary.decisive ? <Badge tone="bad">decisive</Badge> : null}
        </span>
      ),
    },
    {
      id: 'detail',
      header: 'Core detail',
      description: 'The core’s text for this rule. For a passing check it can name the condition that would have failed.',
      wrap: true,
      cell: (c) => (c.detail ? <span className={c.passed ? 'muted' : undefined}>{c.detail}</span> : <NoValue reason="NOT_REPORTED" />),
    },
  ];
  return (
    <div className="ord-decision">
      <div className="ord-decision-summary">
        <StatusPill label={decision.approved ? 'APPROVED' : `REJECTED: ${decision.code}`} tone={decision.approved ? 'ok' : 'bad'} />
        <span className="muted">
          {summary.total} checks: {summary.passed} passed, {summary.failed} failed
          {summary.decisive ? `; the first failure (${summary.decisive.check}) is the rejection code` : ''}.
        </span>
        {summary.failed ? (
          <label htmlFor={toggleId} className="ord-inline ord-small">
            <input id={toggleId} type="checkbox" checked={failedOnly} onChange={(e) => setFailedOnly(e.target.checked)} /> Failed checks only
          </label>
        ) : null}
      </div>
      <DataTable
        caption={caption}
        captionHidden
        columns={columns}
        rows={failedOnly ? decision.checks.filter((c) => !c.passed) : decision.checks}
        rowKey={(c) => `${order.get(c) ?? 0}-${c.check}`}
        maxHeight="min(60vh, 520px)"
        emptyText="The decision lists no checks."
      />
      <p className="muted ord-footnote">
        The detail column is the core’s recorded text for each rule. For a passing check it can describe the condition that would have failed, not
        what was observed.
      </p>
    </div>
  );
}
