/**
 * Basket (#/orders/~basket): several intents typed one per line, each previewed by the core's what-if route and,
 * on confirmation, sent as its own submit_intent command with its own order id. Each row shows its own outcome;
 * an UNKNOWN outcome is shown with its stored-result lookup and is never resent from here.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useAuth, useQuery, useSendCommand } from '../../state/react';
import { StatusPill } from '../../ui/primitives';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { parseBasket, type BasketItem } from './basket';
import { type InstrumentData } from './parts';
import { previewParams } from './preview';
import { OutcomeLine, usePretradePreview } from './PreviewPanel';
import { buildIntentPayload, deadlineNsFrom, DEFAULT_DEADLINE_SECONDS, defaultSessionId, DEFAULT_TIF, eligibilityOf, newOrderId } from './ticket';

interface SentLine {
  lineNo: number;
  text: string;
  orderId: string;
  commandId: string;
}

function PreviewCell({ item }: { item: BasketItem }) {
  const params = item.error || !item.input ? null : previewParams(item.input);
  const s = usePretradePreview(params, item.instrument);
  if (item.error) return <span className="form-error">{item.error}</span>;
  if (!s.preview || s.pending) return <span className="muted">evaluating…</span>;
  const p = s.preview;
  const failed = p.decision?.checks.find((c) => !c.passed);
  return (
    <span>
      <StatusPill label={p.approved ? 'WOULD PASS' : `WOULD FAIL ${p.code}`} tone={p.approved ? 'ok' : 'bad'} detail={p.label} compact />
      {p.feeEnvelope ? <span className="muted ord-small"> fee envelope {p.feeEnvelope}</span> : null}
      {failed ? <span className="muted ord-small"> {failed.detail}</span> : null}
    </span>
  );
}

export function BasketView({ tabs, instruments: inst }: { tabs: ReactNode; instruments: InstrumentData }) {
  const sessionsQ = useQuery('/api/sessions');
  const policy = useQuery('/api/policy/active');
  const sessions = sessionsQ.data ?? [];
  const [sessionId, setSessionId] = useState('');
  const [text, setText] = useState('');
  const [sent, setSent] = useState<SentLine[]>([]);
  const [busy, setBusy] = useState(false);
  const send = useSendCommand();
  const auth = useAuth();
  useEffect(() => {
    if (!sessionId && sessions.length) setSessionId(defaultSessionId(sessions));
  }, [sessions, sessionId]);
  const session = sessions.find((s) => s.session_id === sessionId);
  const eligibility = useMemo(() => eligibilityOf(policy.data?.body), [policy.data]);
  const items = useMemo(
    () => parseBasket(text, { session, instruments: inst.list, eligibility, deadlineSeconds: String(DEFAULT_DEADLINE_SECONDS), timeInForce: DEFAULT_TIF }),
    [text, session, inst.list, eligibility],
  );
  const sendable = items.filter((i) => !i.error && i.input);

  /** One command per line, in order, each with its own ids; a failure or UNKNOWN never triggers a resend. */
  const sendAll = async () => {
    if (busy || !session || !sendable.length || items.some((i) => i.error)) return;
    setBusy(true);
    try {
      for (const item of sendable) {
        if (!item.input) continue;
        const orderId = newOrderId();
        const payload = buildIntentPayload(item.input, session, orderId, deadlineNsFrom(Date.now() + auth.clockOffsetMs, DEFAULT_DEADLINE_SECONDS));
        const label = `${payload.side} ${payload.quantity} ${item.instrument?.symbol ?? payload.instrument_id} @ ${payload.limit_price}`;
        const t = await send('submit_intent', payload, { label });
        const line: SentLine = { lineNo: item.lineNo, text: label, orderId, commandId: t.commandId };
        setSent((s) => [line, ...s]);
        if (t.status === 'cancelled') break; // the owner dismissed step-up: stop, nothing further is sent
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <WorkspaceFrame title="Basket" description="Several limit intents, each sent as its own command with its own order id and outcome. The core checks every one." queries={[sessionsQ, inst.query]}>
      {tabs}
      <QueryError what="Sessions" error={sessionsQ.error} />
      <div className="ord-ticket-grid">
        <section className="panel" aria-labelledby="bsk-h">
          <header>
            <h2 id="bsk-h">Intents</h2>
            <span className="muted ord-small">limit · {DEFAULT_TIF} · regular hours</span>
          </header>
          <div className="panel-body">
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
              <span>One intent per line: SIDE QUANTITY SECURITY LIMIT</span>
              <textarea className="input ord-basket-text" rows={8} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} placeholder={'BUY 10 AAPL.XNAS 189.50\nSELL 5 MSFT.XNAS 410.00'} />
            </label>
            {items.length ? (
              <table className="ord-basket" aria-label="Basket lines and their pretrade preview">
                <thead>
                  <tr>
                    <th scope="col" className="num">Line</th>
                    <th scope="col">Intent</th>
                    <th scope="col">Preview</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={`${it.lineNo}:${it.raw}`}>
                      <td className="num">{it.lineNo}</td>
                      <td>{it.raw}</td>
                      <td>
                        <PreviewCell item={it} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
            <div className="ord-actions">
              <button type="button" className="btn primary" disabled={busy || !session || !sendable.length || items.some((i) => i.error)} onClick={() => void sendAll()}>
                Send {sendable.length} intent{sendable.length === 1 ? '' : 's'}
              </button>
              <span className="muted ord-small">Each line is a separate command; a refusal or unknown outcome does not resend anything.</span>
            </div>
          </div>
        </section>
        <aside className="ord-ticket-side" aria-label="Basket outcomes">
          <section className="panel" aria-labelledby="bsk-out-h">
            <header>
              <h2 id="bsk-out-h">Outcomes</h2>
            </header>
            <div className="panel-body">
              {sent.length ? (
                <ul className="ord-stack">
                  {sent.map((s) => (
                    <li key={s.commandId}>
                      <span className="muted">line {s.lineNo}</span> {s.text} <OutcomeLine commandId={s.commandId} orderId={s.orderId} />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">Nothing sent yet.</p>
              )}
            </div>
          </section>
        </aside>
      </div>
    </WorkspaceFrame>
  );
}
