/**
 * Order ticket for owner manual intents (`submit_intent`). Two steps: fill and review, then send.
 * The terminal does not estimate required cash, fees or limits: the core's pretrade check decides and
 * the decision (accepted, or the rejection code with every check's diagnostics) is shown after sending.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react';
import { encodePathSegment } from '../../api/client';
import type { Instrument, Session } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { entryState } from '../../controls/model';
import { formatTimestampMs } from '../../format/time';
import { useAuth, useNow, useQuery, useSendCommand, useTrackedCommand } from '../../state/react';
import { routeHref } from '../../state/router';
import { Money, Qty, ShortId, StatusPill } from '../../ui/primitives';
import { sessionStateTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { decisionFromRejection, decisionFromTimeline, explainRejectionCode } from './decision';
import type { OrderDetailRecord } from './model';
import { DecisionTable, isFixtureInstrument, QuoteLine, SideText, type InstrumentData } from './parts';
import {
  buildIntentPayload,
  deadlineNsFrom,
  defaultSessionId,
  derivedPurpose,
  EMPTY_TICKET,
  hasErrors,
  newOrderId,
  parseDeadlineSeconds,
  validateTicket,
  type SubmitIntentPayload,
  type TicketField,
  type TicketInput,
} from './ticket';

interface SentIntent {
  commandId: string;
  orderId: string;
  payload: SubmitIntentPayload;
  symbol: string;
}

const FIELD_IDS: Record<TicketField, string> = {
  sessionId: 'tkt-session',
  instrumentId: 'tkt-instrument',
  side: 'tkt-side-buy',
  quantity: 'tkt-qty',
  limitPrice: 'tkt-limit',
  deadlineSeconds: 'tkt-deadline',
};
const FIELD_ORDER: TicketField[] = ['sessionId', 'instrumentId', 'side', 'quantity', 'limitPrice', 'deadlineSeconds'];

function FieldMessages({ field, error, hint }: { field: TicketField; error: string | undefined; hint: string | undefined }) {
  return (
    <>
      {error ? (
        <div id={`${FIELD_IDS[field]}-err`} className="form-error" role="alert">
          {error}
        </div>
      ) : null}
      {hint ? (
        <div id={`${FIELD_IDS[field]}-hint`} className="ord-hint">
          <span aria-hidden="true">▲ </span>
          {hint}
        </div>
      ) : null}
    </>
  );
}

function describedBy(field: TicketField, error: string | undefined, hint: string | undefined): string | undefined {
  const ids = [error ? `${FIELD_IDS[field]}-err` : '', hint ? `${FIELD_IDS[field]}-hint` : ''].filter(Boolean);
  return ids.length ? ids.join(' ') : undefined;
}

function instrumentLabel(i: Instrument): string {
  return `${i.symbol} · ${i.name} · ${i.instrument_id}${isFixtureInstrument(i) ? ' · FIXTURE' : ''}`;
}

function sessionLabel(s: Session): string {
  return `${s.session_id} · ${s.state} · seed ${s.seed} ${s.currency}`;
}

export function TicketView({
  tabs,
  instruments: inst,
  presetInstrumentId,
}: {
  tabs: ReactNode;
  instruments: InstrumentData;
  presetInstrumentId: string | null;
}) {
  const instruments = inst.map;
  const instrumentList = inst.list;
  const sessionsQ = useQuery('/api/sessions');
  const quotesQ = useQuery('/api/quotes', undefined, { pollMs: 1000 });
  const overview = useQuery('/api/overview');
  const policy = useQuery('/api/policy/active');
  const buckets = useQuery('/api/buckets');
  const positions = useQuery('/api/positions');
  const auth = useAuth();
  const send = useSendCommand();
  const now = useNow(1000);

  const [input, setInput] = useState<TicketInput>(() => ({ ...EMPTY_TICKET, instrumentId: presetInstrumentId ?? '' }));
  const [stage, setStage] = useState<'edit' | 'review'>('edit');
  const [showErrors, setShowErrors] = useState(false);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<SentIntent | null>(null);
  const resultRef = useRef<HTMLHeadingElement>(null);
  const sendRef = useRef<HTMLButtonElement>(null);

  const sessions = sessionsQ.data ?? [];
  useEffect(() => {
    if (!input.sessionId && sessions.length) setInput((i) => (i.sessionId ? i : { ...i, sessionId: defaultSessionId(sessions) }));
  }, [sessions, input.sessionId]);
  useEffect(() => {
    if (presetInstrumentId) setInput((i) => ({ ...i, instrumentId: presetInstrumentId }));
  }, [presetInstrumentId]);
  useEffect(() => {
    if (stage === 'review') sendRef.current?.focus();
  }, [stage]);

  const session = sessions.find((s) => s.session_id === input.sessionId);
  const instrument = instruments.get(input.instrumentId);
  const issues = useMemo(() => validateTicket(input, { session, instrument }), [input, session, instrument]);
  const quote = input.instrumentId ? quotesQ.data?.[input.instrumentId] : undefined;
  const limits = policy.data?.body.account_limits;
  const quoteMaxAge = typeof limits?.quote_max_age_ms === 'number' ? limits.quote_max_age_ms : undefined;
  const sessionBucket = (buckets.data ?? []).find((b) => session && b.owner === `session:${session.session_id}` && b.currency === session.currency);
  const sessionPosition = (positions.data ?? []).find((p) => session && p.owner === `session:${session.session_id}` && p.instrument_id === input.instrumentId);
  const entries = entryState(overview.data?.control ?? null, overview.data ?? null);
  const deadlineSecs = parseDeadlineSeconds(input.deadlineSeconds);
  const ccy = instrument?.currency ?? session?.currency ?? '';

  const set = <K extends TicketField>(field: K, value: TicketInput[K]) => {
    setInput((i) => ({ ...i, [field]: value }));
    if (stage === 'review') setStage('edit');
  };

  const review = (e: FormEvent) => {
    e.preventDefault();
    if (hasErrors(issues)) {
      setShowErrors(true);
      const first = FIELD_ORDER.find((f) => issues.errors[f]);
      if (first) document.getElementById(FIELD_IDS[first])?.focus();
      return;
    }
    setStage('review');
  };

  const sendIntent = async () => {
    if (sending || !session || hasErrors(issues) || deadlineSecs === null) return;
    setSending(true);
    try {
      const orderId = newOrderId();
      // Deadline from the server clock (offset measured at the last session check), whole seconds.
      const payload = buildIntentPayload(input, session, orderId, deadlineNsFrom(Date.now() + auth.clockOffsetMs, deadlineSecs));
      const symbol = instrument?.symbol ?? input.instrumentId;
      const t = await send('submit_intent', payload, { label: `${payload.side} ${payload.quantity} ${symbol} @ ${payload.limit_price}` });
      setSent({ commandId: t.commandId, orderId, payload, symbol });
      setStage('edit');
      setTimeout(() => resultRef.current?.focus(), 0);
    } finally {
      setSending(false);
    }
  };

  const err = (f: TicketField) => (showErrors ? issues.errors[f] : undefined);
  const entriesBlocked = overview.data?.control ? entries.label !== 'ENTRIES PERMITTED' : false;
  const hint = (f: TicketField) =>
    f === 'side' && input.side === 'BUY' && entriesBlocked
      ? `Entries are ${entries.label}: ${entries.detail} The core will reject a BUY; a SELL that reduces a position remains possible.`
      : issues.hints[f];

  return (
    <WorkspaceFrame
      title="New order"
      description="Owner manual intent. The core’s pretrade check decides, and its decision is shown after sending."
      queries={[sessionsQ, quotesQ, inst.query]}
    >
      {tabs}
      <QueryError what="Sessions" error={sessionsQ.error} />
      <QueryError what="Instruments" error={inst.query.error} />
      {sessionsQ.status === 'success' && sessions.length === 0 ? (
        <div className="callout ord-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
          No session exists yet. Every order belongs to a session; create one in{' '}
          <WorkspaceLink to="sessions">the Sessions workspace</WorkspaceLink>.
        </div>
      ) : null}
      {inst.query.status === 'success' && instrumentList.length === 0 ? (
        <div className="callout ord-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
          The instrument master is empty, so there is nothing to order.
        </div>
      ) : null}
      <div className="ord-ticket-grid">
        <section className="panel" aria-labelledby="tkt-h">
          <header>
            <h2 id="tkt-h">Order ticket</h2>
            <span className="muted ord-small">Owner manual intent · limit order · day</span>
          </header>
          <form className="panel-body ord-ticket" onSubmit={review} noValidate>
            <div className="field">
              <label htmlFor={FIELD_IDS.sessionId}>Session</label>
              <select
                id={FIELD_IDS.sessionId}
                className="input"
                value={input.sessionId}
                onChange={(e) => set('sessionId', e.target.value)}
                aria-invalid={err('sessionId') ? true : undefined}
                aria-describedby={describedBy('sessionId', err('sessionId'), hint('sessionId'))}
              >
                <option value="">Choose a session…</option>
                {sessions.map((s) => (
                  <option key={s.session_id} value={s.session_id}>
                    {sessionLabel(s)}
                  </option>
                ))}
              </select>
              <FieldMessages field="sessionId" error={err('sessionId')} hint={hint('sessionId')} />
            </div>

            <div className="field">
              <label htmlFor={FIELD_IDS.instrumentId}>Instrument</label>
              <select
                id={FIELD_IDS.instrumentId}
                className="input"
                value={input.instrumentId}
                onChange={(e) => set('instrumentId', e.target.value)}
                aria-invalid={err('instrumentId') ? true : undefined}
                aria-describedby={describedBy('instrumentId', err('instrumentId'), hint('instrumentId'))}
              >
                <option value="">Choose an instrument…</option>
                {instrumentList.map((i) => (
                  <option key={i.instrument_id} value={i.instrument_id}>
                    {instrumentLabel(i)}
                  </option>
                ))}
              </select>
              <FieldMessages field="instrumentId" error={err('instrumentId')} hint={hint('instrumentId')} />
            </div>

            <fieldset className="field ord-fieldset" aria-describedby={describedBy('side', err('side'), hint('side'))}>
              <legend>Side</legend>
              <div className="ord-segmented ord-side-choice">
                <label>
                  <input id={FIELD_IDS.side} type="radio" name="tkt-side" value="BUY" checked={input.side === 'BUY'} onChange={() => set('side', 'BUY')} />
                  <span>
                    <span aria-hidden="true">▲ </span>BUY
                  </span>
                </label>
                <label>
                  <input type="radio" name="tkt-side" value="SELL" checked={input.side === 'SELL'} onChange={() => set('side', 'SELL')} />
                  <span>
                    <span aria-hidden="true">▼ </span>SELL
                  </span>
                </label>
              </div>
              <FieldMessages field="side" error={err('side')} hint={hint('side')} />
            </fieldset>

            <div className="ord-two">
              <div className="field">
                <label htmlFor={FIELD_IDS.quantity}>Quantity{instrument ? <span className="muted"> (min {instrument.min_quantity}, step {instrument.quantity_increment})</span> : null}</label>
                <input
                  id={FIELD_IDS.quantity}
                  className="input num"
                  inputMode="decimal"
                  autoComplete="off"
                  spellCheck={false}
                  value={input.quantity}
                  onChange={(e) => set('quantity', e.target.value)}
                  aria-invalid={err('quantity') ? true : undefined}
                  aria-describedby={describedBy('quantity', err('quantity'), hint('quantity'))}
                />
                <FieldMessages field="quantity" error={err('quantity')} hint={hint('quantity')} />
              </div>
              <div className="field">
                <label htmlFor={FIELD_IDS.limitPrice}>Limit price{ccy ? <span className="muted"> ({ccy} per unit)</span> : null}</label>
                <input
                  id={FIELD_IDS.limitPrice}
                  className="input num"
                  inputMode="decimal"
                  autoComplete="off"
                  spellCheck={false}
                  value={input.limitPrice}
                  onChange={(e) => set('limitPrice', e.target.value)}
                  aria-invalid={err('limitPrice') ? true : undefined}
                  aria-describedby={describedBy('limitPrice', err('limitPrice'), hint('limitPrice'))}
                />
                <FieldMessages field="limitPrice" error={err('limitPrice')} hint={hint('limitPrice')} />
              </div>
            </div>
            {quote && (quote.bid || quote.ask) ? (
              <div className="ord-inline ord-copy" role="group" aria-label="Copy a price from the current quote into the limit price">
                <span className="muted ord-small">Copy into limit from the {quote.fixture ? 'FIXTURE ' : ''}quote:</span>
                {quote.bid ? (
                  <button type="button" className="btn small" onClick={() => set('limitPrice', quote.bid ?? '')}>
                    bid {quote.bid}
                  </button>
                ) : null}
                {quote.ask ? (
                  <button type="button" className="btn small" onClick={() => set('limitPrice', quote.ask ?? '')}>
                    ask {quote.ask}
                  </button>
                ) : null}
              </div>
            ) : null}

            <div className="field">
              <label htmlFor={FIELD_IDS.deadlineSeconds}>Deadline: seconds after sending</label>
              <input
                id={FIELD_IDS.deadlineSeconds}
                className="input num ord-narrow"
                inputMode="numeric"
                autoComplete="off"
                value={input.deadlineSeconds}
                onChange={(e) => set('deadlineSeconds', e.target.value)}
                aria-invalid={err('deadlineSeconds') ? true : undefined}
                aria-describedby={`${FIELD_IDS.deadlineSeconds}-note${err('deadlineSeconds') ? ` ${FIELD_IDS.deadlineSeconds}-err` : ''}`}
              />
              <div id={`${FIELD_IDS.deadlineSeconds}-note`} className="muted ord-small">
                {deadlineSecs !== null ? (
                  <>
                    If sent now, the intent expires at {formatTimestampMs(deadlineNsFrom(now + auth.clockOffsetMs, deadlineSecs) / 1e6)} (server clock). An intent not yet
                    sent to the broker by then is expired by the core and its reservation released.
                  </>
                ) : (
                  'Whole seconds; the default is 60.'
                )}
              </div>
              <FieldMessages field="deadlineSeconds" error={err('deadlineSeconds')} hint={undefined} />
            </div>

            {stage === 'edit' ? (
              <div className="ord-actions">
                <button type="submit" className="btn primary">
                  Review order
                </button>
                <span className="muted ord-small">Nothing is sent until you confirm on the next step.</span>
              </div>
            ) : (
              <ReviewBlock
                input={input}
                session={session}
                instrument={instrument}
                sending={sending}
                sendRef={sendRef}
                onSend={() => void sendIntent()}
                onEdit={() => setStage('edit')}
              />
            )}
          </form>
        </section>

        <aside className="ord-ticket-side" aria-label="Market and limits for this ticket">
          <section className="panel" aria-labelledby="tkt-quote-h">
            <header>
              <h2 id="tkt-quote-h">Quote{instrument ? ` · ${instrument.symbol}` : ''}</h2>
              <span className="muted ord-small">from the core’s quote book, every second</span>
            </header>
            <div className="panel-body">
              <QueryError what="Quotes" error={quotesQ.error} />
              {input.instrumentId ? (
                <QuoteLine quote={quote} observedAtMs={quotesQ.fetchedAtMs} staleAfterMs={quoteMaxAge} context="live" />
              ) : (
                <p className="muted">Choose an instrument to see its quote.</p>
              )}
              {limits ? (
                <p className="muted ord-small" style={{ marginBottom: 0 }}>
                  Policy: a quote older than {String(limits.quote_max_age_ms)} ms fails QUOTE_FRESH; a limit more than {String(limits.price_collar_bps)} bps
                  beyond mid fails PRICE_COLLAR.
                </p>
              ) : null}
            </div>
          </section>

          <section className="panel" aria-labelledby="tkt-ctx-h">
            <header>
              <h2 id="tkt-ctx-h">Context</h2>
              <StatusPill label={entries.label} tone={entries.tone} detail={entries.detail} compact />
            </header>
            <div className="panel-body">
              <dl className="kv">
                <dt>Entries</dt>
                <dd>{entries.detail}</dd>
                <dt>Session</dt>
                <dd>
                  {session ? (
                    <>
                      <StatusPill label={session.state} tone={sessionStateTone(session.state)} compact /> <ShortId value={session.session_id} head={18} />
                    </>
                  ) : (
                    <span className="muted">none chosen</span>
                  )}
                </dd>
                <dt>Session cash available</dt>
                <dd>
                  {sessionBucket ? (
                    <>
                      <Money amount={sessionBucket.available_cash} currency={sessionBucket.currency} kind="available" />{' '}
                      <span className="muted">
                        (held <Money amount={sessionBucket.holds} currency={sessionBucket.currency} kind="held" />)
                      </span>
                    </>
                  ) : (
                    <span className="muted">{session ? 'no cash bucket for this session' : '—'}</span>
                  )}
                </dd>
                <dt>Session position</dt>
                <dd>
                  {sessionPosition ? (
                    <Qty value={sessionPosition.quantity} unit={instrument?.symbol ?? sessionPosition.instrument_id} />
                  ) : (
                    <span className="muted">{input.instrumentId && session ? 'none in this instrument' : '—'}</span>
                  )}
                </dd>
                {limits ? (
                  <>
                    <dt>Max order notional</dt>
                    <dd>
                      <Money amount={String(limits.max_order_notional)} currency={policy.data?.body.currency ?? ccy} />
                    </dd>
                    <dt>Orders per minute</dt>
                    <dd className="num">at most {String(limits.max_orders_per_minute)}</dd>
                  </>
                ) : null}
                {instrument ? (
                  <>
                    <dt>Tick sizes</dt>
                    <dd>
                      {instrument.tick_rules.map((r, i) => (
                        <span key={i} className="num ord-tick">
                          from {r.from_price}: {r.tick}
                        </span>
                      ))}
                    </dd>
                    <dt>Venue / status</dt>
                    <dd>
                      {instrument.venue_mic} / {instrument.status}
                    </dd>
                  </>
                ) : null}
              </dl>
              <p className="muted ord-small" style={{ marginBottom: 0 }}>
                These are the core’s own figures, for orientation. The core computes the cash an order requires (notional plus fee envelope) and evaluates
                every limit when it receives the intent.
              </p>
            </div>
          </section>
        </aside>
      </div>

      {sent ? <SubmitResult sent={sent} headingRef={resultRef} onDismiss={() => setSent(null)} /> : null}
    </WorkspaceFrame>
  );
}

function ReviewBlock({
  input,
  session,
  instrument,
  sending,
  sendRef,
  onSend,
  onEdit,
}: {
  input: TicketInput;
  session: Session | undefined;
  instrument: Instrument | undefined;
  sending: boolean;
  sendRef: RefObject<HTMLButtonElement | null>;
  onSend: () => void;
  onEdit: () => void;
}) {
  const ccy = instrument?.currency ?? session?.currency ?? '';
  return (
    <div className="ord-review" role="group" aria-labelledby="tkt-review-h">
      <h3 id="tkt-review-h">Review the intent</h3>
      <dl className="kv">
        <dt>Order</dt>
        <dd>
          <SideText side={input.side} /> <Qty value={input.quantity.trim()} /> <strong>{instrument?.symbol ?? input.instrumentId}</strong> limit{' '}
          <Money amount={input.limitPrice.trim()} currency={ccy} />
        </dd>
        <dt>Type / time in force</dt>
        <dd>LIMIT / DAY</dd>
        <dt>Purpose</dt>
        <dd>{derivedPurpose(input.side)} (the core derives it from the side)</dd>
        <dt>Session</dt>
        <dd className="cmd-id">{session?.session_id}</dd>
        <dt>Mandate policy</dt>
        <dd>
          <ShortId value={session?.mandate?.policy_hash} head={16} />
        </dd>
        <dt>Deadline</dt>
        <dd>{input.deadlineSeconds.trim()} s after sending</dd>
        <dt>Order id</dt>
        <dd className="muted">assigned when you send (own-…)</dd>
      </dl>
      {isFixtureInstrument(instrument) ? (
        <p className="ord-small">
          <strong>FIXTURE</strong> instrument: this is a simulation-only security priced by the fixture market.
        </p>
      ) : null}
      <div className="ord-actions">
        <button ref={sendRef} type="button" className="btn primary" onClick={onSend} disabled={sending}>
          {sending ? 'Sending…' : 'Send intent to the core'}
        </button>
        <button type="button" className="btn" onClick={onEdit} disabled={sending}>
          Edit
        </button>
      </div>
    </div>
  );
}

function SubmitResult({ sent, headingRef, onDismiss }: { sent: SentIntent; headingRef: RefObject<HTMLHeadingElement | null>; onDismiss: () => void }) {
  const t = useTrackedCommand(sent.commandId);
  const accepted = t?.status === 'pending' || t?.status === 'applied';
  const detail = useQuery(accepted ? `/api/orders/${encodePathSegment(sent.orderId)}` : null);
  const order = detail.data as OrderDetailRecord | undefined; // see OrderDetailRecord
  const recorded = useMemo(() => decisionFromTimeline(order?.timeline), [order?.timeline]);
  const rejected = t?.status === 'rejected' ? decisionFromRejection(t.rejection) : null;
  const p = sent.payload;
  let body: ReactNode = null;

  if (t?.status === 'rejected' && t.rejection) {
    const why = explainRejectionCode(t.rejection.code);
    body = rejected?.decision ? (
      <>
        <p className="muted ord-small">
          The core’s pretrade check rejected the intent ({t.rejection.message || t.rejection.code}). Nothing was reserved and no order was created; a
          corrected ticket is sent with a new order id.
        </p>
        <QuoteLine quote={rejected.quote} context="at decision" />
        <DecisionTable decision={rejected.decision} caption={`Pretrade checks for the rejected intent ${sent.orderId}`} />
      </>
    ) : (
      <>
        <p>
          <StatusPill label={`REJECTED: ${t.rejection.code}`} tone="bad" /> {t.rejection.message ? <span>{t.rejection.message}</span> : null}
          {why ? <span className="muted"> {why}</span> : null}
        </p>
        {t.rejection.code === 'COMMAND_ID_CONFLICT' ? (
          <p className="muted">
            This request was not executed: nothing was reserved for it. The earlier command with this id keeps its own result; check the blotter for
            order <span className="cmd-id">{sent.orderId}</span> before sending a new ticket, which gets a new command id.
          </p>
        ) : (
          <p className="muted">
            No per-check diagnostics: the core refused the intent before its pretrade evaluation. Nothing was reserved and no order was created.
          </p>
        )}
      </>
    );
  } else if (accepted && t) {
    const code = typeof t.response?.body.decision === 'string' ? t.response.body.decision : 'APPROVED';
    const state = typeof t.response?.body.state === 'string' ? t.response.body.state : 'RESERVED';
    body = (
      <>
        <p>
          <StatusPill label={`ACCEPTED: ${code}`} tone="ok" /> Order{' '}
          <a href={routeHref('orders', sent.orderId)} className="cmd-id">
            {sent.orderId}
          </a>{' '}
          was {state} by the core{t.response?.duplicate ? ' (duplicate command id: the core returned its stored result)' : ''}. Its reservation stays in
          place until the order reaches a terminal state (filled, or cancelled, rejected or expired as confirmed by the broker).
        </p>
        {recorded?.decision ? (
          <>
            {recorded.requiredCash ? (
              <p>
                Cash the core required: <Money amount={recorded.requiredCash} currency={order?.currency ?? ''} kind="held" />{' '}
                <span className="muted">(notional plus fee envelope, decided by the core)</span>
              </p>
            ) : null}
            <QuoteLine quote={recorded.quote} context="at decision" />
            <DecisionTable decision={recorded.decision} caption={`Pretrade checks for order ${sent.orderId}`} />
          </>
        ) : (
          <p className="muted">{detail.error ? 'The decision record could not be loaded.' : 'Loading the decision record…'}</p>
        )}
        <QueryError what="The order" error={detail.error} />
      </>
    );
  } else if (t?.status === 'failed') {
    body = (
      <p className="muted">
        Order id <span className="cmd-id">{sent.orderId}</span>. If the core recorded this intent, the order appears in the blotter. Retrying with the same
        command id returns the recorded result and never creates a second order.
      </p>
    );
  }

  return (
    <section className="panel section-gap" aria-labelledby="tkt-result-h">
      <header>
        <h2 id="tkt-result-h" ref={headingRef} tabIndex={-1}>
          Last intent: {p.side} {p.quantity} {sent.symbol} @ {p.limit_price}
        </h2>
        <button type="button" className="btn small" onClick={onDismiss}>
          Dismiss
        </button>
      </header>
      <div className="panel-body">
        <CommandStatus commandId={sent.commandId} />
        <div className="ord-result-body">{body}</div>
      </div>
    </section>
  );
}
