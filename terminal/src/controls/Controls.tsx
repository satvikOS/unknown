/**
 * Red STOP (always visible, session only) and green START (preflight panel, step-up), plus the
 * non-modal control drawer that shows stop progress and start blockers from /api/preflight.
 */
import { useEffect, useRef } from 'react';
import type { TrackedCommand } from '../state/commands';
import { useAuth, useQuery, useRuntime, useTrackedCommand } from '../state/react';
import { Icon } from '../ui/Icon';
import { Age, Badge, Qty, ShortId, StatusPill, Timestamp, type Tone } from '../ui/primitives';
import { useControl } from './ControlProvider';
import { describeBlocker, entryState, rejectionBlockers, startAvailability, stopSteps, type StepState } from './model';

const STEP_UP_WINDOW_MS = 15 * 60 * 1000; // server.py STEP_UP_MAX_AGE_NS

export function RedStop() {
  const { stop, lastStopId } = useControl();
  const tracked = useTrackedCommand(lastStopId);
  const sending = tracked?.status === 'sending';
  return (
    <>
      <button
        type="button"
        className="btn-stop"
        onClick={() => {
          if (!sending) void stop();
        }}
        aria-describedby="stop-help"
        title="Blocks new entries and cancels open buy orders. Positions are not sold."
      >
        <span className="square" aria-hidden="true">
          ■
        </span>
        {sending ? 'STOPPING…' : 'STOP'}
      </button>
      <span id="stop-help" className="sr-only">
        Sends the stop command immediately, without a passphrase. It blocks new entries and cancels open buy orders; it does not sell positions.
      </span>
    </>
  );
}

export function GreenStart() {
  const { openDrawer } = useControl();
  return (
    <button
      type="button"
      className="btn-start"
      onClick={(e) => openDrawer('start', e.currentTarget)}
      title="Open preflight and request start"
      aria-haspopup="dialog"
    >
      <span className="tri" aria-hidden="true">
        ▶
      </span>
      START
    </button>
  );
}

function statusTone(status: TrackedCommand['status']): Tone {
  switch (status) {
    case 'applied':
      return 'ok';
    case 'pending':
    case 'sending':
      return 'info';
    case 'cancelled':
      return 'warn';
    default:
      return 'bad';
  }
}

function statusText(t: TrackedCommand): string {
  switch (t.status) {
    case 'sending':
      return 'sending';
    case 'pending':
      return `recorded; waiting for state seq ${t.awaitSeq ?? '?'}`;
    case 'applied':
      return t.awaitSeq !== null ? `applied (state seq ${t.awaitSeq} observed)` : 'applied (duplicate id; stored result)';
    case 'rejected':
      return `rejected: ${t.rejection?.code ?? 'unknown code'}`;
    case 'failed':
      return t.outcomeUnknown ? 'outcome unknown' : 'refused by the API';
    case 'cancelled':
      return 'cancelled (not sent)';
  }
}

/** Durable command id + live status, shared by both sections. */
export function CommandStatus({ commandId }: { commandId: string | null }) {
  const { commands } = useRuntime();
  const t = useTrackedCommand(commandId);
  if (!commandId || !t) return null;
  return (
    <div className="callout" style={{ ['--tone' as string]: `var(--${statusTone(t.status)})` }} aria-live="polite">
      <div className="progress-row">
        <span className="micro">{t.label} command</span>
        <StatusPill label={statusText(t)} tone={statusTone(t.status)} compact />
      </div>
      <div className="cmd-id">{t.commandId}</div>
      {t.error ? <div className="muted" style={{ marginTop: 4 }}>{t.error}</div> : null}
      {t.status === 'failed' && t.outcomeUnknown ? (
        <button type="button" className="btn small" style={{ marginTop: 6 }} onClick={() => void commands.retry(t.commandId)}>
          Retry with the same id
        </button>
      ) : null}
    </div>
  );
}

const STEP_GLYPH: Record<StepState, { glyph: string; tone: Tone; text: string }> = {
  done: { glyph: '✓', tone: 'ok', text: 'done' },
  pending: { glyph: '…', tone: 'info', text: 'in progress' },
  attention: { glyph: '!', tone: 'warn', text: 'needs attention' },
};

function StopSection() {
  const { lastStopId } = useControl();
  const overview = useQuery('/api/overview');
  const preflight = useQuery('/api/preflight', undefined, { pollMs: 2000 });
  const policy = useQuery('/api/policy/active');
  const control = overview.data?.control ?? null;
  const progress = preflight.data?.stop_progress;
  const liquidation = policy.data?.body.on_stop_liquidation;
  const residual = progress
    ? Object.entries(progress.residual_positions).flatMap(([owner, byInst]) =>
        Object.entries(byInst).map(([inst, qty]) => ({ owner, inst, qty })),
      )
    : [];
  return (
    <section aria-labelledby="stop-h">
      <h3 id="stop-h" tabIndex={-1}>
        Stop progress
      </h3>
      <CommandStatus commandId={lastStopId} />
      {control?.stop_latched ? (
        <dl className="kv" style={{ margin: '10px 0' }}>
          <dt>Latched by command</dt>
          <dd className="cmd-id">{control.latch_command_id ?? 'not recorded'}</dd>
          <dt>Reason</dt>
          <dd>{control.latch_reason ?? 'not recorded'}</dd>
          <dt>Latched at</dt>
          <dd>
            <Timestamp ns={control.latched_at_ns} /> by {control.latched_by ?? 'unknown'}
          </dd>
        </dl>
      ) : (
        <p className="muted">No stop is latched. STOP blocks new entries immediately.</p>
      )}
      {preflight.error && !progress ? (
        <p className="tone-bad" style={{ color: 'var(--bad)' }}>
          Stop progress unavailable: the core did not answer the preflight query.
        </p>
      ) : null}
      {progress ? (
        <ul className="checklist" aria-label="Stop progress steps">
          {stopSteps(progress).map((s) => {
            const g = STEP_GLYPH[s.state];
            return (
              <li key={s.id}>
                <span className={`tone-${g.tone}`} style={{ color: `var(--${g.tone})` }} aria-hidden="true">
                  {g.glyph}
                </span>
                <span>
                  {s.label}
                  <span className="sr-only">: {g.text}</span>
                </span>
                <span className="num">{s.value}</span>
              </li>
            );
          })}
        </ul>
      ) : preflight.status === 'loading' ? (
        <p className="muted">Loading stop progress…</p>
      ) : null}
      {residual.length ? (
        <table className="data" style={{ marginTop: 8 }}>
          <caption className="sr-only">Residual positions after stop</caption>
          <thead>
            <tr>
              <th scope="col">
                <span className="th-inner">Owner</span>
              </th>
              <th scope="col">
                <span className="th-inner">Instrument</span>
              </th>
              <th scope="col" className="right">
                <span className="th-inner">Open quantity</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {residual.map((r) => (
              <tr key={`${r.owner}/${r.inst}`}>
                <td className="cmd-id">{r.owner}</td>
                <td>{r.inst}</td>
                <td className="right">
                  <Qty value={r.qty} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <div className="callout section-gap" style={{ ['--tone' as string]: 'var(--warn)' }}>
        <strong>Liquidation on stop: {liquidation ?? 'policy not loaded'}.</strong>{' '}
        {liquidation === 'NONE' || liquidation === undefined
          ? 'Stop blocks new entries and cancels open buy orders. Residual positions are not sold automatically, and this terminal does not offer liquidation.'
          : 'This release accepts only NONE; the policy reports a different value.'}
      </div>
      <p className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
        If the terminal cannot reach the API: <code className="mono">python -m unknown.cli stop --account {overview.data?.account?.account_key ?? '<key>'}</code>.
        If the core is down, nothing here is sending orders, but orders already resting at the broker remain: use the broker’s own cancel-all.
      </p>
    </section>
  );
}

function StartSection() {
  const { start, lastStartId, accountKey } = useControl();
  const { stepUp, stream } = useRuntime();
  const auth = useAuth();
  const overview = useQuery('/api/overview');
  const preflight = useQuery('/api/preflight', undefined, { pollMs: 3000 });
  const control = overview.data?.control ?? null;
  const state = entryState(control, overview.data);
  const avail = startAvailability(control, preflight.data, accountKey);
  const tracked = useTrackedCommand(lastStartId);
  const busy = tracked?.status === 'sending';
  const failedBlockers = tracked?.rejection ? rejectionBlockers(tracked.rejection.details) : [];
  const authAgeMs = auth.authenticatedAtNs !== null ? Date.now() + auth.clockOffsetMs - auth.authenticatedAtNs / 1e6 : null;
  const stepUpFresh = authAgeMs !== null && authAgeMs < STEP_UP_WINDOW_MS;

  return (
    <section aria-labelledby="start-h">
      <h3 id="start-h" tabIndex={-1}>
        Preflight and start
      </h3>
      <div className="progress-row" style={{ marginBottom: 8 }}>
        <span>Current state</span>
        <StatusPill label={state.label} tone={state.tone} detail={state.detail} />
      </div>
      <p className="muted" style={{ margin: '0 0 10px' }}>
        {state.detail}
      </p>
      {preflight.error && !preflight.data ? (
        <p style={{ color: 'var(--bad)' }}>Preflight unavailable: the core did not answer. Start is not possible without a fresh preflight.</p>
      ) : null}
      {preflight.data ? (
        preflight.data.blockers.length === 0 ? (
          <StatusPill label="Preflight clear: no blockers" tone="ok" />
        ) : (
          <div className="grid" style={{ gap: 6 }} role="list" aria-label="Preflight blockers">
            {preflight.data.blockers.map((code) => {
              const b = describeBlocker(code);
              return (
                <div className="blocker" role="listitem" key={code} style={b.stepUp ? { borderLeftColor: 'var(--warn)' } : undefined}>
                  <strong>{b.title}</strong>
                  <code>{code}</code>
                  <span className="muted">{b.resolution}</span>
                  {b.stepUp ? (
                    <button
                      type="button"
                      className="btn small"
                      style={{ justifySelf: 'start', marginTop: 4 }}
                      onClick={() =>
                        void stepUp
                          .request({ kind: 'start', commandId: 'preflight', reason: 'Re-enter the passphrase to refresh owner authentication.' })
                          .then((ok) => ok && stream.refreshAll())
                      }
                    >
                      Re-enter passphrase now
                    </button>
                  ) : null}
                </div>
              );
            })}
          </div>
        )
      ) : preflight.status === 'loading' ? (
        <p className="muted">Running preflight…</p>
      ) : null}
      {preflight.data ? (
        <dl className="kv section-gap">
          <dt>Policy</dt>
          <dd>
            <ShortId value={preflight.data.evidence.policy_hash ?? null} head={16} />
          </dd>
          <dt>Reconciliation</dt>
          <dd>
            <ShortId value={preflight.data.evidence.reconciliation ?? null} head={20} />
          </dd>
          <dt>Process epoch</dt>
          <dd className="num">{preflight.data.evidence.epoch ?? 'unknown'}</dd>
          <dt>Broker</dt>
          <dd>{preflight.data.evidence.health?.detail ?? 'no health evidence'}</dd>
          <dt>Checked</dt>
          <dd>
            <Age ageMs={0} observedAtMs={preflight.fetchedAtMs} /> ago
          </dd>
        </dl>
      ) : null}
      <p className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
        Start needs owner authentication no older than 15 minutes.{' '}
        {authAgeMs === null ? 'Passphrase entry time unknown.' : stepUpFresh ? 'Your last passphrase entry is recent enough.' : 'You will be asked to re-enter your passphrase.'}
      </p>
      <button
        type="button"
        className="btn-start"
        disabled={!avail.enabled || busy}
        aria-describedby="start-reason"
        onClick={() => void start()}
      >
        <span aria-hidden="true">▶</span> {busy ? 'REQUESTING…' : 'REQUEST START'}
      </button>
      <div id="start-reason" className="muted" style={{ marginTop: 4, fontSize: 'var(--fs-sm)' }}>
        {avail.reason}
      </div>
      <div className="section-gap">
        <CommandStatus commandId={lastStartId} />
      </div>
      {failedBlockers.length ? (
        <div className="section-gap">
          <span className="micro">Blockers reported by the core at the time of the request</span>
          <ul>
            {failedBlockers.map((b) => (
              <li key={b}>
                <code className="mono">{b}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <p className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
        There is no bypass: blockers clear only when their cause is resolved. The core repeats every check when it executes start.
      </p>
    </section>
  );
}

export function ControlDrawer() {
  const { drawer, closeDrawer } = useControl();
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!drawer) return;
    const h = ref.current?.querySelector<HTMLElement>(drawer === 'stop' ? '#stop-h' : '#start-h');
    h?.focus();
  }, [drawer]);
  if (!drawer) return null;
  return (
    <aside
      ref={ref}
      className="drawer"
      role="dialog"
      aria-modal="false"
      aria-labelledby="control-drawer-title"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          closeDrawer();
        }
      }}
    >
      <header>
        <h2 id="control-drawer-title">Control</h2>
        <button type="button" className="btn ghost icon-btn" onClick={closeDrawer} aria-label="Close control panel">
          <Icon name="close" />
        </button>
      </header>
      <div className="drawer-body">
        {drawer === 'stop' ? (
          <>
            <StopSection />
            <StartSection />
          </>
        ) : (
          <>
            <StartSection />
            <StopSection />
          </>
        )}
        <Badge tone="info" title="Nothing on this panel updates optimistically">
          Values refresh from the server
        </Badge>
      </div>
    </aside>
  );
}
