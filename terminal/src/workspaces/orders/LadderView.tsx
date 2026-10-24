/**
 * Price ladder (#/orders/~ladder, function LADDER): market depth when the serving source provides it, else level 1,
 * from the broker-agnostic market-data client, with our working orders at each price. A click on the bid column
 * stages a BUY limit at that price, on the ask column a SELL; the staged intent shows the core's live pretrade
 * preview and is sent only on "Send". A click on one of our orders opens the controlled cancel.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import type { Order } from '../../api/types';
import { useMarketData, useMarketDataKeys } from '../../state/marketdata';
import { useAuth, useQuery, useSendCommand } from '../../state/react';
import { useLinkedSecurity } from '../../ui/link';
import { PriceCell } from '../../ui/live';
import { StatusPill } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { CancelDialog } from './CancelDialog';
import { buildLadder, instrumentForSecurity, securityKeyOf } from './ladder';
import { type InstrumentData } from './parts';
import { previewParams } from './preview';
import { OutcomeLine, PreviewPanel, usePretradePreview } from './PreviewPanel';
import {
  buildIntentPayload,
  deadlineNsFrom,
  DEFAULT_DEADLINE_SECONDS,
  defaultSessionId,
  EMPTY_TICKET,
  hasErrors,
  isPositiveDecimal,
  newOrderId,
  validateTicket,
  type TicketInput,
} from './ticket';

interface Staged {
  side: 'BUY' | 'SELL';
  price: string;
}

export function LadderView({ tabs, instruments: inst, routeInstrumentId }: { tabs: ReactNode; instruments: InstrumentData; routeInstrumentId: string | null }) {
  const linked = useLinkedSecurity();
  const [chosenId, setChosenId] = useState<string>('');
  const instrumentId = chosenId || routeInstrumentId || instrumentForSecurity(linked, inst.list)?.instrument_id || '';
  const instrument = inst.map.get(instrumentId);
  const key = securityKeyOf(instrument);
  const client = useMarketData();
  useMarketDataKeys(key ? [key] : []);
  const sessionsQ = useQuery('/api/sessions');
  const ordersQ = useQuery('/api/orders', { state: 'open' });
  const sessions = sessionsQ.data ?? [];
  const [sessionId, setSessionId] = useState('');
  const [quantity, setQuantity] = useState('');
  const [staged, setStaged] = useState<Staged | null>(null);
  const [cancelling, setCancelling] = useState<Order | null>(null);
  const [sent, setSent] = useState<{ commandId: string; orderId: string; text: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const send = useSendCommand();
  const auth = useAuth();

  useEffect(() => {
    if (!sessionId && sessions.length) setSessionId(defaultSessionId(sessions));
  }, [sessions, sessionId]);
  useEffect(() => setStaged(null), [instrumentId]);

  const depth = key ? client.depth(key) : { bid: null, ask: null };
  const quote = key ? client.quote(key) : undefined;
  const status = key ? client.status(key) : undefined;
  const ladder = useMemo(
    () => (instrument ? buildLadder({ depth, quote, orders: ordersQ.data ?? [], instrument }) : null),
    [instrument, depth, quote, ordersQ.data],
  );
  const session = sessions.find((s) => s.session_id === sessionId);
  const input: TicketInput = { ...EMPTY_TICKET, sessionId, instrumentId, side: staged?.side ?? '', quantity, limitPrice: staged?.price ?? '', deadlineSeconds: String(DEFAULT_DEADLINE_SECONDS) };
  const issues = validateTicket(input, { session, instrument });
  const pParams = staged && !hasErrors(issues) ? previewParams(input) : null;
  const preview = usePretradePreview(pParams, instrument);
  const ccy = instrument?.currency ?? session?.currency ?? '';

  const sendStaged = async () => {
    if (busy || !staged || !session || hasErrors(issues)) return;
    setBusy(true);
    try {
      const orderId = newOrderId();
      const payload = buildIntentPayload(input, session, orderId, deadlineNsFrom(Date.now() + auth.clockOffsetMs, DEFAULT_DEADLINE_SECONDS));
      const text = `${payload.side} ${payload.quantity} ${instrument?.symbol ?? instrumentId} @ ${payload.limit_price}`;
      const t = await send('submit_intent', payload, { label: text });
      setSent((s) => [{ commandId: t.commandId, orderId, text }, ...s].slice(0, 10));
      setStaged(null);
    } finally {
      setBusy(false);
    }
  };

  const stage = (side: 'BUY' | 'SELL', price: string) => setStaged({ side, price });

  return (
    <WorkspaceFrame title="Price ladder" description="Depth or level 1 from the market-data sources, with our working orders. Click a price to stage a limit order; nothing is sent until you confirm." queries={[ordersQ, sessionsQ, inst.query]}>
      {tabs}
      <QueryError what="Open orders" error={ordersQ.error} />
      <div className="ord-filters" role="group" aria-label="Ladder settings">
        <label className="field">
          <span>Instrument</span>
          <select className="input" value={instrumentId} onChange={(e) => setChosenId(e.target.value)}>
            <option value="">Choose an instrument…</option>
            {inst.list.map((i) => (
              <option key={i.instrument_id} value={i.instrument_id}>
                {i.symbol} · {i.venue_mic} · {i.instrument_id}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Session</span>
          <select className="input" value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
            <option value="">Choose a session…</option>
            {sessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id} · {s.state}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Quantity per click</span>
          <input className="input num ord-narrow" inputMode="decimal" autoComplete="off" value={quantity} onChange={(e) => setQuantity(e.target.value)} aria-invalid={quantity && !isPositiveDecimal(quantity) ? true : undefined} />
        </label>
      </div>

      {!instrument ? (
        <p className="muted">Choose an instrument, or load a security on the command line (for example AAPL.XNAS LADDER).</p>
      ) : (
        <div className="ord-ticket-grid">
          <section className="panel" aria-labelledby="lad-h">
            <header>
              <h2 id="lad-h">
                {instrument.symbol} · {instrument.venue_mic}
              </h2>
              {ladder && ladder.basis !== 'NONE' ? (
                <StatusPill
                  label={ladder.basis === 'DEPTH' ? 'DEPTH' : 'LEVEL 1'}
                  tone={ladder.dataType === 'REALTIME' ? 'ok' : 'warn'}
                  detail={`${ladder.basis === 'DEPTH' ? 'Market depth' : 'Level 1 (depth not provided)'} from ${ladder.source ?? 'unknown source'}, ${ladder.dataType ?? 'unknown type'}${ladder.tick ? `; tick ${ladder.tick}` : ''}`}
                  compact
                />
              ) : null}
            </header>
            <div className="panel-body">
              {status?.state === 'ERROR' || status?.state === 'NOT_ENTITLED' ? (
                <p role="alert">
                  <NoValue reason={status.state === 'ERROR' ? 'ERROR' : 'NOT_ENTITLED'} detail={status.reason} /> {status.reason}
                </p>
              ) : null}
              {ladder?.reason ? (
                <p className="muted">
                  <NoValue reason="NO_SOURCE" detail={ladder.reason} /> {ladder.reason} Our working orders are still listed and can be cancelled.
                </p>
              ) : null}
              {ladder && ladder.rows.length ? (
                <table className="ord-ladder" aria-label={`Price ladder ${key ?? instrumentId}`}>
                  <thead>
                    <tr>
                      <th scope="col">Our buys</th>
                      <th scope="col" className="num">Bid size</th>
                      <th scope="col" className="num">Price</th>
                      <th scope="col" className="num">Ask size</th>
                      <th scope="col">Our sells</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ladder.rows.map((row) => {
                      const mine = (side: 'BUY' | 'SELL') =>
                        row.working
                          .filter((w) => w.side === side)
                          .map((w) => {
                            const o = (ordersQ.data ?? []).find((x) => x.order_id === w.orderId);
                            return (
                              <button key={w.orderId} type="button" className="btn small ord-working" onClick={() => o && setCancelling(o)} title={`${w.orderId} ${w.state}; cancel ${w.cancelState}. Click to cancel.`}>
                                {w.leaves} {w.state === 'UNKNOWN' ? 'UNKNOWN' : ''}
                                <span className="sr-only"> {side} working at {row.price}, open the cancel dialog</span>
                              </button>
                            );
                          });
                      const isStaged = staged?.price === row.price;
                      return (
                        <tr key={row.price} data-best={row.bestBid ? 'bid' : row.bestAsk ? 'ask' : undefined} data-staged={isStaged ? staged?.side : undefined}>
                          <td>{mine('BUY')}</td>
                          <td className="num">
                            <button type="button" className="ord-ladder-cell bid" onClick={() => stage('BUY', row.price)} aria-label={`Stage BUY at ${row.price}`}>
                              {row.hasBid ? <PriceCell value={row.bidSize} reason="NOT_REPORTED" badge="none" label={`bid size at ${row.price}`} /> : ''}
                            </button>
                          </td>
                          <th scope="row" className="num ord-ladder-price">{row.price}</th>
                          <td className="num">
                            <button type="button" className="ord-ladder-cell ask" onClick={() => stage('SELL', row.price)} aria-label={`Stage SELL at ${row.price}`}>
                              {row.hasAsk ? <PriceCell value={row.askSize} reason="NOT_REPORTED" badge="none" label={`ask size at ${row.price}`} /> : ''}
                            </button>
                          </td>
                          <td>{mine('SELL')}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : null}
            </div>
          </section>

          <aside className="ord-ticket-side" aria-label="Staged order">
            <section className="panel" aria-labelledby="lad-stage-h">
              <header>
                <h2 id="lad-stage-h">Staged order</h2>
              </header>
              <div className="panel-body">
                {staged ? (
                  <>
                    <p>
                      <strong>
                        {staged.side} {quantity || '?'} {instrument.symbol} @ {staged.price}
                      </strong>{' '}
                      <span className="muted">limit · {input.timeInForce} · regular hours</span>
                    </p>
                    {hasErrors(issues) ? <p className="form-error" role="alert">{Object.values(issues.errors)[0]}</p> : null}
                    <div className="ord-actions">
                      <button type="button" className="btn primary" disabled={busy || hasErrors(issues)} onClick={() => void sendStaged()}>
                        Send {staged.side}
                      </button>
                      <button type="button" className="btn" onClick={() => setStaged(null)}>
                        Clear
                      </button>
                    </div>
                  </>
                ) : (
                  <p className="muted">Click a bid size to stage a BUY at that price, or an ask size to stage a SELL.</p>
                )}
                {sent.length ? (
                  <ul className="ord-stack" aria-label="Sent from the ladder">
                    {sent.map((s) => (
                      <li key={s.commandId}>
                        {s.text} <OutcomeLine commandId={s.commandId} orderId={s.orderId} />
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </section>
            <PreviewPanel state={preview} params={pParams} currency={ccy} headingId="lad-preview-h" />
          </aside>
        </div>
      )}
      <CancelDialog order={cancelling} onClose={() => setCancelling(null)} />
    </WorkspaceFrame>
  );
}
