/**
 * What-if sandbox (CAP-PORT-12): GET /api/portfolio/whatif runs a hypothetical intent through the
 * core's own command path on a private copy of the database. The decision, every pretrade check and
 * the reservation preview come from the same code the live gate runs, but nothing is reserved,
 * placed or authorized: a real order is a new intent in Orders and passes the gate again.
 */
import { useState, type FormEvent, type ReactNode } from 'react';
import { formatMoney, formatQuantity } from '../../format/decimal';
import { useQuery } from '../../state/react';
import { Money, Qty, StatusPill } from '../../ui/primitives';
import { QueryError, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { parseDecision } from '../orders/decision';
import { DecisionTable } from '../orders/parts';
import { usePluginGetOnce } from '../plugin-api/hooks';
import { sentence, sentences, dec, fmtNum, isRec, num, ofKind, str, weightPct, whatIfErrors, type Rec, type WhatIfInput } from './model';
import { PortfolioFrame, AsOf, FixtureNote, Lineage, QuoteSnapshotNote, ResultLabel, SnapshotStale, type FormulaDef } from './parts';

const EMPTY: WhatIfInput = { session: '', instrument: '', side: 'BUY', quantity: '', limit: '' };

/**
 * The intent a what-if result was computed for (whatif.dry_run echoes the payload it ran), so a result is
 * never read against the form after the owner has edited it.
 */
export function intentText(intent: unknown): string {
  if (!isRec(intent)) return 'not reported by the API';
  return `${str(intent.side) ?? '?'} ${str(intent.quantity) ?? '?'} ${str(intent.instrument_id) ?? '?'} at limit ${str(intent.limit_price) ?? '?'} for session ${str(intent.session_id) ?? '?'}`;
}

export function Impact({ body }: { body: Rec }) {
  const imp = isRec(body.impact) ? body.impact : {};
  if (str(imp.reason)) return <p className="pf-hint">Impact not computed: {str(imp.reason)}</p>;
  const ccy = str(imp.currency) ?? '';
  const cash = isRec(imp.cash) ? imp.cash : {};
  const qty = isRec(imp.quantity) ? imp.quantity : {};
  const fill = isRec(imp.if_filled) ? imp.if_filled : {};
  const head = isRec(imp.headroom) ? imp.headroom : {};
  return (
    <div className="pf-split">
      <section className="panel" aria-labelledby="pf-wi-cash-h">
        <header>
          <h2 id="pf-wi-cash-h">Cash and quantity</h2>
        </header>
        <div className="panel-body">
          <dl className="kv">
            <dt>Order notional at limit</dt>
            <dd>
              <Money amount={dec(imp.order_notional_at_limit)} currency={ccy} />
            </dd>
            <dt>Available before</dt>
            <dd>
              <Money amount={dec(cash.available_before)} currency={ccy} kind="available" />
            </dd>
            <dt>Required reservation</dt>
            <dd>
              <Money amount={dec(cash.required_reservation)} currency={ccy} kind="held" /> <span className="pf-hint">notional plus the fee envelope</span>
            </dd>
            <dt>Available after reservation</dt>
            <dd>
              <Money amount={dec(cash.available_after_reservation)} currency={ccy} kind="available" />
            </dd>
            {dec(cash.shortfall) ? (
              <>
                <dt>Shortfall</dt>
                <dd>
                  <Money amount={dec(cash.shortfall)} currency={ccy} />
                </dd>
              </>
            ) : null}
            <dt>Owned / reserved before</dt>
            <dd>
              <Qty value={dec(qty.owned_before)} /> / <Qty value={dec(qty.reserved_before)} />
            </dd>
            {dec(qty.excess_over_unreserved) ? (
              <>
                <dt>Excess over unreserved</dt>
                <dd>
                  <Qty value={dec(qty.excess_over_unreserved)} />
                </dd>
              </>
            ) : null}
            <dt>Headroom after</dt>
            <dd className="num">
              {[
                dec(head.cash) ? `cash ${formatMoney(dec(head.cash) as string, ccy, { signDisplay: 'auto' })}` : null,
                dec(head.max_order_notional) ? `max order notional ${formatMoney(dec(head.max_order_notional) as string, ccy)}` : null,
                dec(head.owned_quantity) ? `unreserved quantity ${formatQuantity(dec(head.owned_quantity) as string)}` : null,
              ]
                .filter(Boolean)
                .join(' · ') || '—'}
            </dd>
          </dl>
          <p className="pf-hint" style={{ marginBottom: 0 }}>
            {sentence(str(cash.note))}
          </p>
        </div>
      </section>
      <section className="panel" aria-labelledby="pf-wi-fill-h">
        <header>
          <h2 id="pf-wi-fill-h">If completely filled</h2>
        </header>
        <div className="panel-body">
          <p className="pf-hint" style={{ marginTop: 0 }}>
            {sentences([str(fill.assumption), str(fill.note)])}
          </p>
          {str(fill.reason) ? (
            <p className="pf-hint">{str(fill.reason)}</p>
          ) : (
            <dl className="kv">
              <dt>Position value</dt>
              <dd>
                <Money amount={dec(fill.position_value_before)} currency={ccy} /> → <Money amount={dec(fill.position_value_after)} currency={ccy} />
              </dd>
              <dt>Owner NAV</dt>
              <dd>
                <Money amount={dec(fill.owner_nav_before)} currency={ccy} /> → <Money amount={dec(fill.owner_nav_after)} currency={ccy} />
              </dd>
              <dt>Weight of owner NAV</dt>
              <dd className="num">
                {weightPct(dec(fill.weight_before))} → {weightPct(dec(fill.weight_after))}
              </dd>
              <dt>Concentration (HHI)</dt>
              <dd className="num">
                {fmtNum(num(fill.hhi_before), 3)} → {fmtNum(num(fill.hhi_after), 3)}
              </dd>
              <dt>Estimated fee</dt>
              <dd>
                <Money amount={dec(fill.estimated_fee)} currency={ccy} kind="estimated" />
              </dd>
            </dl>
          )}
        </div>
      </section>
    </div>
  );
}

export function WhatIfTab({ bar, formulas }: { bar: ReactNode; formulas: Map<string, FormulaDef> }) {
  const sessions = useQuery('/api/sessions');
  const instruments = useQuery('/api/instruments');
  const [input, setInput] = useState<WhatIfInput>(EMPTY);
  const [errors, setErrors] = useState<Partial<Record<keyof WhatIfInput, string>>>({});
  const [asked, setAsked] = useState<WhatIfInput | null>(null);
  const once = usePluginGetOnce<unknown>();
  const body = ofKind(once.data, 'whatif');
  const decision = body ? parseDecision(body.decision) : null;

  const set = <K extends keyof WhatIfInput>(k: K, v: WhatIfInput[K]) => {
    setInput((i) => ({ ...i, [k]: v }));
    setErrors((e) => ({ ...e, [k]: undefined }));
  };
  const runWith = (i: WhatIfInput) => {
    setAsked(i);
    void once.run('/api/portfolio/whatif', { session: i.session, instrument: i.instrument, side: i.side, quantity: i.quantity.trim(), limit: i.limit.trim() });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const errs = whatIfErrors(input);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    runWith(input);
  };
  const err = (k: keyof WhatIfInput) => errors[k];
  const quote = body && isRec(body.quote_used) ? body.quote_used : null;

  return (
    <PortfolioFrame tab="whatif" bar={bar} queries={[]}>
      <form className="pf-toolbar" onSubmit={submit} noValidate aria-label="Hypothetical intent">
        <div className="field">
          <label htmlFor="pf-wi-session">Session</label>
          <select id="pf-wi-session" className="input" value={input.session} onChange={(e) => set('session', e.target.value)} aria-invalid={err('session') ? true : undefined} aria-describedby={err('session') ? 'pf-wi-session-err' : undefined}>
            <option value="">Choose a session…</option>
            {(sessions.data ?? []).map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id} · {s.state}
              </option>
            ))}
          </select>
          {err('session') ? (
            <span id="pf-wi-session-err" className="form-error">
              {err('session')}
            </span>
          ) : null}
        </div>
        <div className="field">
          <label htmlFor="pf-wi-inst">Instrument</label>
          <select id="pf-wi-inst" className="input" value={input.instrument} onChange={(e) => set('instrument', e.target.value)} aria-invalid={err('instrument') ? true : undefined} aria-describedby={err('instrument') ? 'pf-wi-inst-err' : undefined}>
            <option value="">Choose an instrument…</option>
            {(instruments.data ?? []).map((i) => (
              <option key={i.instrument_id} value={i.instrument_id}>
                {i.instrument_id} · {i.symbol}
              </option>
            ))}
          </select>
          {err('instrument') ? (
            <span id="pf-wi-inst-err" className="form-error">
              {err('instrument')}
            </span>
          ) : null}
        </div>
        <div className="field">
          <label htmlFor="pf-wi-side">Side</label>
          <select id="pf-wi-side" className="input" value={input.side} onChange={(e) => set('side', e.target.value === 'SELL' ? 'SELL' : 'BUY')}>
            <option value="BUY">BUY</option>
            <option value="SELL">SELL</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="pf-wi-qty">Quantity</label>
          <input id="pf-wi-qty" className="input num" inputMode="decimal" autoComplete="off" value={input.quantity} onChange={(e) => set('quantity', e.target.value)} aria-invalid={err('quantity') ? true : undefined} aria-describedby={err('quantity') ? 'pf-wi-qty-err' : undefined} />
          {err('quantity') ? (
            <span id="pf-wi-qty-err" className="form-error">
              {err('quantity')}
            </span>
          ) : null}
        </div>
        <div className="field">
          <label htmlFor="pf-wi-limit">Limit price</label>
          <input id="pf-wi-limit" className="input num" inputMode="decimal" autoComplete="off" value={input.limit} onChange={(e) => set('limit', e.target.value)} aria-invalid={err('limit') ? true : undefined} aria-describedby={err('limit') ? 'pf-wi-limit-err' : undefined} />
          {err('limit') ? (
            <span id="pf-wi-limit-err" className="form-error">
              {err('limit')}
            </span>
          ) : null}
        </div>
        <div className="field">
          <button type="submit" className="btn primary" disabled={once.status === 'loading'}>
            {once.status === 'loading' ? 'Evaluating…' : 'Evaluate (read-only)'}
          </button>
        </div>
      </form>
      <p className="pf-hint">
        Read-only sandbox: the intent runs through the core's own pretrade gate and reservation code on a private copy of the database. It reserves nothing,
        places nothing and authorizes nothing. A real order is a new intent in <WorkspaceLink to="orders" rest={['~new']}>Orders</WorkspaceLink> and passes the gate
        again.
      </p>
      <QueryError what="What-if" error={once.error} />
      {isRec(once.data) && once.data.kind === 'whatif' && str(once.data.fault) ? (
        <div className="callout pf-gap" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          <strong>No decision.</strong> {str(once.data.reason)} ({str(once.data.fault)})
        </div>
      ) : null}
      {body && !str(body.fault) ? (
        <>
          <ResultLabel kind="hypothetical" text={str(body.label)} />
          <SnapshotStale seq={num(body.snapshot_seq)} onRerun={() => asked && runWith(asked)} busy={once.status === 'loading'} />
          <p className="pf-hint" style={{ margin: '4px 0' }}>
            Evaluated intent: <strong>{intentText(body.intent)}</strong>
          </p>
          <div className="pf-note">
            <StatusPill label={body.approved === true ? 'would be approved' : `would be refused: ${str(body.code) ?? '?'}`} tone={body.approved === true ? 'ok' : 'bad'} />
            {str(body.message) ? <span>{str(body.message)}</span> : null}
            <span>evaluated as deployment {str(body.evaluated_as_deployment) ?? '?'}</span>
            <AsOf body={body} />
          </div>
          <QuoteSnapshotNote body={body} />
          <FixtureNote body={body} />
          {quote ? (
            <p className="pf-hint">
              Quote evaluated: bid {str(quote.bid) ?? '—'} / ask {str(quote.ask) ?? '—'} {str(quote.currency)}, source {str(quote.source)}, age {num(quote.age_ms) ?? '?'} ms,
              market-data type {str(quote.market_data_type) ?? '?'}, consolidation {str(quote.consolidation) ?? '?'}
              {quote.fixture === true ? ' (FIXTURE)' : ''}.
            </p>
          ) : null}
          {Array.isArray(body.reservation_preview) && body.reservation_preview.length ? (
            <p className="pf-hint">
              Reservation the core would make:{' '}
              {body.reservation_preview
                .filter(isRec)
                .map((h) => `${str(h.kind)} ${str(h.amount)} ${str(h.unit)}`)
                .join('; ')}{' '}
              (preview only; nothing is held).
            </p>
          ) : null}
          <Impact body={body} />
          {decision ? (
            <section className="panel pf-section" aria-labelledby="pf-wi-dec-h">
              <header>
                <h2 id="pf-wi-dec-h">Pretrade decision (hypothetical)</h2>
              </header>
              <div className="panel-body">
                <DecisionTable decision={decision} caption="Pretrade checks for the hypothetical intent" />
              </div>
            </section>
          ) : (
            <p className="pf-hint">No pretrade decision record: the intent was refused before the risk gate ({str(body.code) ?? 'no code'}).</p>
          )}
          <Lineage body={body} formulas={formulas} />
        </>
      ) : null}
      {!body && once.status === 'idle' ? <p className="muted">Enter an intent and evaluate it.</p> : null}
    </PortfolioFrame>
  );
}
