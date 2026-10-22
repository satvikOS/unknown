/**
 * Lifecycle commands for one session. Only the commands the core accepts in the current state are
 * offered. Consequential ones ask for confirmation with their exact effect and a preview of the
 * blockers the core is expected to report. Every command is tracked by its durable id; the page
 * changes only when the core's recorded state arrives on the stream.
 */
import { useRef, useState } from 'react';
import type { Overview, SessionDetail } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { Money } from '../../ui/primitives';
import { isInFlight } from './commandLog';
import { useLatestSessionCommand, useSessionSend } from './hooks';
import { bpsToPercent } from './format';
import { MandateDialog } from './MandateDialog';
import {
  actionInfo,
  blockersFromDetails,
  expectedEntryBlockers,
  expectedStartBlockers,
  sessionActions,
  sessionRequiredReturn,
  type SessionAction,
  type SessionCommandKind,
} from './model';
import { BlockerList, ConfirmDialog, Pct } from './parts';
import type { SessionPolicyView } from './policy';
import { formatDurationS } from './policy';

export interface SessionActionsProps {
  session: SessionDetail;
  overview: Overview | undefined;
  policy: SessionPolicyView | null;
  /** Number of open positions the session holds (from /api/positions). */
  holdings: number;
}

export function SessionActions({ session, overview, policy, holdings }: SessionActionsProps) {
  const sessionSend = useSessionSend();
  const { ref, tracked } = useLatestSessionCommand(session.session_id);
  const [confirming, setConfirming] = useState<SessionCommandKind | null>(null);
  const [amendOpen, setAmendOpen] = useState(false);
  const statusRef = useRef<HTMLDivElement>(null);
  const actions = sessionActions(session.state);
  const busy = isInFlight(tracked);

  const send = async (kind: SessionCommandKind) => {
    const info = actionInfo(kind);
    const payload: Record<string, unknown> = { session_id: session.session_id };
    if (kind === 'suspend_session') payload.reason = 'OWNER';
    setConfirming(null);
    await sessionSend(kind, payload, `${info.label}: ${session.session_id}`, session.session_id);
    // The button that sent the command may disappear when the new state arrives: keep focus on
    // the command's status, which also announces its progress.
    statusRef.current?.focus();
  };

  const onAction = (a: SessionAction) => {
    if (a.kind === 'amend_session_request') setAmendOpen(true);
    else if (a.confirm) setConfirming(a.kind);
    else void send(a.kind);
  };

  // Blockers the core reported with a refusal; those already listed as the session's current
  // blockers are not repeated.
  const rejectionBlockers = (tracked?.status === 'rejected' ? blockersFromDetails(tracked.rejection?.details) : []).filter(
    (b) => !session.blockers.includes(b),
  );

  return (
    <div className="grid" style={{ gap: 8 }}>
      {actions.length ? (
        <div className="ses-actions" role="group" aria-label="Session commands">
          {actions.map((a) => (
            <button
              key={a.kind}
              type="button"
              className={`btn small${a.tone === 'primary' ? ' primary' : a.tone === 'danger' ? ' ses-danger' : ''}`}
              onClick={() => onAction(a)}
              disabled={busy}
              title={a.effect}
              aria-describedby={busy ? 'ses-busy' : undefined}
            >
              {a.label}
              {a.confirm || a.kind === 'amend_session_request' ? '…' : ''}
            </button>
          ))}
        </div>
      ) : (
        <p className="muted" style={{ margin: 0, fontSize: 'var(--fs-sm)' }}>
          No owner command applies in {session.state}. {session.state === 'WINDING_DOWN' || session.state === 'RECONCILING' ? 'The core advances this session on its own ticks.' : ''}
        </p>
      )}
      {busy ? (
        <span id="ses-busy" className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          Commands are paused until the last one is confirmed on the state stream.
        </span>
      ) : null}
      <div ref={statusRef} tabIndex={-1} role="group" aria-label="Last session command" style={{ outlineOffset: 2 }}>
        {ref ? <CommandStatus commandId={ref.commandId} /> : null}
      </div>
      {rejectionBlockers.length ? <BlockerList codes={rejectionBlockers} label="Blockers the core reported" /> : null}

      {confirming ? (
        <ConfirmDialog
          open
          title={`${actionInfo(confirming).label}: ${session.session_id}`}
          effect={actionInfo(confirming).effect}
          confirmLabel={actionInfo(confirming).label}
          danger={actionInfo(confirming).tone === 'danger'}
          busy={busy}
          onClose={() => setConfirming(null)}
          onConfirm={() => void send(confirming)}
        >
          <ConfirmContext kind={confirming} session={session} overview={overview} policy={policy} holdings={holdings} />
        </ConfirmDialog>
      ) : null}
      <MandateDialog mode="amend" open={amendOpen} onClose={() => setAmendOpen(false)} session={session} />
    </div>
  );
}

function ConfirmContext({
  kind,
  session,
  overview,
  policy,
  holdings,
}: {
  kind: SessionCommandKind;
  session: SessionDetail;
  overview: Overview | undefined;
  policy: SessionPolicyView | null;
  holdings: number;
}) {
  const ccy = session.currency;
  const m = session.mandate;
  if (kind === 'start_session' || kind === 'resume_session') {
    const expected = kind === 'start_session' ? expectedStartBlockers(overview, session.session_id) : expectedEntryBlockers(overview);
    const req = sessionRequiredReturn(session);
    return (
      <>
        <dl className="kv">
          <dt>Seed</dt>
          <dd>
            <Money amount={session.seed} currency={ccy} />
          </dd>
          <dt>Target terminal equity</dt>
          <dd>
            <Money amount={session.target} currency={ccy} /> <span className="muted">(a mandate, not a forecast)</span>
          </dd>
          <dt>Required net return</dt>
          <dd>
            <Pct value={req.pct} />
          </dd>
          {m ? (
            <>
              <dt>Loss triggers</dt>
              <dd>
                suspend at {bpsToPercent(m.session_drawdown_trigger_bps)} below high-water, or a loss of{' '}
                <Money amount={m.session_max_loss} currency={ccy} /> from the seed
              </dd>
              <dt>Strategies</dt>
              <dd>{m.strategy_eligibility.join(', ') || 'none'}</dd>
            </>
          ) : null}
        </dl>
        <div className="section-gap">
          {expected.length ? (
            <>
              <p style={{ margin: '0 0 6px' }}>
                <strong>Expected to be refused now.</strong> The current state shows these blockers; the core makes the decision and has no bypass.
              </p>
              <BlockerList codes={expected} label="Expected blockers" />
            </>
          ) : (
            <p className="muted" style={{ margin: 0 }}>
              No blocker is visible in the current state. The core repeats every check when it runs the command.
            </p>
          )}
        </div>
      </>
    );
  }
  if (kind === 'end_session') {
    const stop = overview?.control?.stop_latched ?? false;
    const w = policy?.windDown;
    return (
      <>
        <dl className="kv">
          <dt>Holdings to sell</dt>
          <dd className="num">{holdings}</dd>
          <dt>Wind-down orders</dt>
          <dd>
            {w?.collarBps !== undefined
              ? `limit sells at the bid less ${bpsToPercent(w.collarBps)}${w.repriceAfterS !== undefined ? `, repriced after ${formatDurationS(w.repriceAfterS)}` : ''}${w.maxAttempts !== undefined ? `, at most ${w.maxAttempts} attempts per instrument` : ''}`
              : 'per the mandate’s policy (not loaded)'}
          </dd>
          <dt>Outcome</dt>
          <dd>OWNER_ENDED: remaining capital returns to unallocated; no successor is created.</dd>
        </dl>
        {stop ? (
          <div className="callout section-gap" style={{ ['--tone' as string]: 'var(--warn)' }}>
            A stop is latched. Stop liquidation is NONE, so nothing is sold until the stop is cleared with START; the session stays in wind-down until then.
          </div>
        ) : null}
      </>
    );
  }
  if (kind === 'cancel_session') {
    return session.state === 'READY' ? (
      <p style={{ margin: 0 }}>
        The funded seed of <Money amount={session.seed} currency={ccy} /> returns to unallocated cash.
      </p>
    ) : (
      <p className="muted" style={{ margin: 0 }}>
        The session is not funded, so no money moves.
      </p>
    );
  }
  return null;
}
