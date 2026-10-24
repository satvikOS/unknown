/**
 * Live pretrade preview panel and per-command outcome line, shared by the ticket, the ladder and the basket.
 * The preview polls the core's what-if route while the ticket is complete; every figure is the core's.
 */
import { useEffect, useState } from 'react';
import type { Instrument } from '../../api/types';
import { useTrackedCommand } from '../../state/react';
import { routeHref } from '../../state/router';
import { Money, StatusPill } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { QueryError } from '../../ui/WorkspaceFrame';
import { usePluginQuery } from '../plugin-api/hooks';
import { outcomeOf } from './basket';
import { DecisionTable } from './parts';
import { parsePreview, previewMatches, PREVIEW_ENDPOINT, PREVIEW_POLL_MS, type PreviewParams, type PretradePreview } from './preview';

/** The params after `ms` without a change (typing does not fire one request per keystroke). */
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  const key = JSON.stringify(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [key, ms]);
  return v;
}

export interface PreviewState {
  preview: PretradePreview | null;
  /** The shown preview is for an older ticket (a newer one is being evaluated). */
  pending: boolean;
  error: unknown;
  fetchedAtMs: number | null;
}

export function usePretradePreview(params: PreviewParams | null, instrument: Instrument | undefined): PreviewState {
  const debounced = useDebounced(params, 300);
  const q = usePluginQuery<unknown>(debounced ? PREVIEW_ENDPOINT : null, debounced ?? undefined, { pollMs: PREVIEW_POLL_MS });
  const current = previewMatches(q.data, params);
  return {
    preview: q.data !== undefined && params ? parsePreview(q.data, params, instrument) : null,
    pending: !!params && !current,
    error: q.error,
    fetchedAtMs: q.fetchedAtMs,
  };
}

export function PreviewPanel({ state, params, currency, headingId }: { state: PreviewState; params: PreviewParams | null; currency: string; headingId: string }) {
  const p = state.preview;
  return (
    <section className="panel ord-preview" aria-labelledby={headingId} aria-busy={state.pending || undefined}>
      <header>
        <h2 id={headingId}>Pretrade preview</h2>
        {p && !state.pending ? (
          <StatusPill label={p.approved ? 'WOULD PASS' : `WOULD FAIL ${p.code}`} tone={p.approved ? 'ok' : 'bad'} detail={p.label} compact />
        ) : null}
      </header>
      <div className="panel-body">
        <QueryError what="Pretrade preview" error={state.error} />
        {!params ? (
          <p className="muted">Complete the session, instrument, side, quantity and limit to see the core’s checks before sending.</p>
        ) : !p ? (
          <p className="muted" role="status">Evaluating on a private copy of the core…</p>
        ) : p.fault ? (
          <p role="alert">
            <NoValue reason="ERROR" detail={p.fault} /> {p.message ?? 'The core faulted on the private copy; no decision is available.'}
          </p>
        ) : (
          <>
            {state.pending ? <p className="muted ord-small" role="status">Updating for the edited ticket…</p> : null}
            <dl className="kv">
              <dt>Notional</dt>
              <dd>{p.notional ? <Money amount={p.notional} currency={currency} /> : <NoValue reason="NOT_APPLICABLE" detail={params.side === 'SELL' ? 'A sell reserves quantity, not cash.' : 'Not computed: the core refused before reserving.'} />}</dd>
              <dt>Fee envelope</dt>
              <dd>
                {p.feeEnvelope ? <Money amount={p.feeEnvelope} currency={currency} kind="held" /> : <NoValue reason="NOT_APPLICABLE" detail={params.side === 'SELL' ? 'The core reserves no fee envelope for a sell; fees are paid from proceeds.' : 'No envelope: the core refused before reserving.'} />}
                {p.feeSource ? <div className="muted ord-small">{p.feeSource.detail}</div> : null}
              </dd>
              <dt>Reservation</dt>
              <dd>
                {p.reservation.length ? (
                  p.reservation.map((h, i) => (
                    <div key={i} className="num">
                      {h.kind} {h.kind === 'cash' ? <Money amount={h.amount} currency={h.unit || currency} kind="held" /> : `${h.amount} ${h.unit}`}
                    </div>
                  ))
                ) : (
                  <NoValue reason="NOT_APPLICABLE" detail="Nothing would be reserved: the core would refuse this intent." />
                )}
              </dd>
            </dl>
            {p.decision ? (
              <DecisionTable decision={p.decision} caption="Every check the core will run on this intent, with its result and reason" />
            ) : (
              <p>
                <StatusPill label={p.code} tone="bad" compact /> {p.message ?? 'Refused before the pretrade checks ran.'}
              </p>
            )}
            <p className="muted ord-small" style={{ marginBottom: 0 }}>
              {p.label}. Sending runs the same checks again on the live core, which decides.
            </p>
          </>
        )}
      </div>
    </section>
  );
}

/** One sent intent's outcome: accepted, rejected with its code, or UNKNOWN with the stored-result lookup. */
export function OutcomeLine({ commandId, orderId }: { commandId: string; orderId: string }) {
  const t = useTrackedCommand(commandId);
  const o = outcomeOf(t);
  return (
    <span className="ord-outcome">
      <StatusPill label={o.label} tone={o.tone} detail={o.detail} compact />{' '}
      <a href={routeHref('orders', orderId)} className="cmd-id">
        {orderId}
      </a>
      {o.kind === 'UNKNOWN' || o.kind === 'REJECTED' || o.kind === 'NOT_EXECUTED' ? <span className="muted ord-small"> {o.detail}</span> : null}
    </span>
  );
}
