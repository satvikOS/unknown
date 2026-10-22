/**
 * Activate a published policy for the account (step-up: the API answers 428 when the passphrase entry
 * is older than its window; the client prompts and retries with the same command id). Shows what
 * changes, what does not, the sessions whose mandates stay as they are, and the core's own checks
 * evaluated on visible state. Nothing is applied locally: the dialog shows the command id and waits
 * for the state stream.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { defaultCommandId } from '../../api/client';
import type { ActivePolicy, PolicyRecord } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { useQuery, useSendCommand, useTrackedCommand } from '../../state/react';
import { Modal } from '../../ui/Modal';
import { ShortId, StatusPill } from '../../ui/primitives';
import { WorkspaceLink } from '../../ui/WorkspaceFrame';
import { differences, diffBodies } from './diff';
import { ACTIVATION_EFFECTS, activationChecks, definiteFailure, sessionsKeepingMandates } from './model';
import { DiffTable } from './PolicyParts';
import { sessionStateTone } from '../../ui/tones';

export interface ActivateDialogProps {
  policy: PolicyRecord;
  active: ActivePolicy | null;
  accountKey: string;
  onClose: () => void;
}

export function ActivateDialog({ policy, active: liveActive, accountKey, onClose }: ActivateDialogProps) {
  // The comparison is against the policy active when the dialog opened, so it stays meaningful after activation.
  const [active] = useState(liveActive);
  const titleId = useId();
  const descId = useId();
  const send = useSendCommand();
  const [commandId, setCommandId] = useState<string | null>(null);
  const tracked = useTrackedCommand(commandId);
  const accounts = useQuery('/api/accounts');
  const risk = useQuery('/api/risk');
  const sessions = useQuery('/api/sessions');

  const account = accounts.data?.find((a) => a.account_key === accountKey) ?? null;
  const checks = activationChecks(policy.body as Record<string, unknown>, account, risk.data?.risk ?? null);
  const blocked = definiteFailure(checks);
  const changes = active ? differences(diffBodies(active.body, policy.body)) : [];
  const kept = sessionsKeepingMandates(sessions.data ?? []);
  const isActive = liveActive?.policy_hash === policy.policy_hash && !commandId;
  const busy = tracked?.status === 'sending';
  const unknownOutcome = tracked?.status === 'failed' && tracked.outcomeUnknown;
  const done = tracked?.status === 'applied' || tracked?.status === 'pending';
  const settled = done || tracked?.status === 'rejected' || tracked?.status === 'failed';
  const closeRef = useRef<HTMLButtonElement>(null);
  // The Activate button disables once the command is decided; keep focus inside the dialog on Close.
  useEffect(() => {
    if (settled) closeRef.current?.focus();
  }, [settled]);

  const activate = async () => {
    // The durable id is shown from the first moment (and while a passphrase prompt is open).
    const id = defaultCommandId('activate_policy');
    setCommandId(id);
    await send('activate_policy', { account_key: accountKey, policy_hash: policy.policy_hash }, { label: `Activate policy ${policy.name}`, commandId: id });
  };

  return (
    <Modal open onClose={onClose} labelledBy={titleId} describedBy={descId} className="rk-dialog">
      <header>
        <h2 id={titleId}>
          Activate “{policy.name}” for {accountKey}
        </h2>
        <p id={descId} className="muted" style={{ margin: '4px 0 0' }}>
          <ShortId value={policy.policy_hash} head={16} /> replaces{' '}
          {active ? (
            <>
              “{active.name}” <ShortId value={active.policy_hash} head={16} />
            </>
          ) : (
            'no active policy'
          )}
          . You may be asked for your passphrase.
        </p>
      </header>
      <div className="modal-body">
        <h3 style={{ fontSize: 'var(--fs-md)', margin: '8px 0 6px' }}>What changes</h3>
        <ul style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 4 }}>
          {ACTIVATION_EFFECTS.slice(1).map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>

        <h3 style={{ fontSize: 'var(--fs-md)', margin: '14px 0 6px' }}>Field changes {active ? <span className="muted">({changes.length})</span> : null}</h3>
        {active ? (
          <DiffTable
            rows={changes}
            currencyA={active.body.currency}
            currencyB={policy.body.currency}
            baseLabel="Active now"
            compareLabel="After activation"
            caption="Fields that change on activation"
            compact
            emptyText="Identical bodies."
            maxHeight="260px"
          />
        ) : (
          <p className="muted">No policy is active; every field takes effect.</p>
        )}

        <h3 style={{ fontSize: 'var(--fs-md)', margin: '14px 0 6px' }}>Sessions that keep their mandates ({kept.length})</h3>
        {kept.length ? (
          <ul className="rk-checks">
            {kept.map((s) => (
              <li key={s.session_id}>
                <StatusPill label={s.state} tone={sessionStateTone(s.state)} compact />
                <span>
                  <WorkspaceLink to="sessions" rest={[s.session_id]}>
                    <span className="cmd-id">{s.session_id}</span>
                  </WorkspaceLink>{' '}
                  <span className="muted">
                    mandate from policy <ShortId value={s.mandate?.policy_hash ?? null} head={12} />, unchanged
                  </span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">{sessions.status === 'loading' ? 'Loading sessions…' : 'No unfinished sessions.'}</p>
        )}

        <h3 style={{ fontSize: 'var(--fs-md)', margin: '14px 0 6px' }}>Checks the core will make</h3>
        <ul className="rk-checks" aria-label="Activation checks">
          {checks.map((c) => (
            <li key={c.code}>
              <StatusPill
                label={c.ok === null ? 'not evaluated' : c.ok ? 'passes' : c.definite ? 'fails' : 'expected to fail'}
                tone={c.ok === null ? 'neutral' : c.ok ? 'ok' : 'bad'}
                compact
              />
              <span>
                {c.label} <span className="muted">({c.detail})</span>
                {c.ok === false ? (
                  <span className="muted">
                    {' '}
                    The core would reject with <code className="mono">{c.code}</code>.
                  </span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
        <p className="rk-basis">Evaluated here on the state the terminal can see; the core repeats every check when it receives the command.</p>

        {commandId ? <CommandStatus commandId={commandId} /> : null}
        {tracked?.status === 'rejected' && tracked.rejection ? (
          <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', marginTop: 8 }}>
            <strong>The core rejected the activation</strong> (<code className="mono">{tracked.rejection.code}</code>): {tracked.rejection.message}
          </div>
        ) : null}
        {tracked?.status === 'applied' ? (
          <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--ok)', marginTop: 8 }}>
            “{policy.name}” is now the active policy for {accountKey}. It applies to future sessions; existing mandates are unchanged.
          </div>
        ) : null}
      </div>
      <div className="modal-actions">
        <button type="button" className="btn" onClick={onClose} ref={closeRef}>
          {done ? 'Close' : 'Cancel'}
        </button>
        <button
          type="button"
          className="btn primary"
          onClick={() => void activate()}
          disabled={isActive || Boolean(blocked) || busy || done || unknownOutcome}
          title={
            blocked
              ? `The core would reject this: ${blocked.code}`
              : isActive
                ? 'Already the active policy'
                : unknownOutcome
                  ? 'Outcome unknown: retry with the same command id above'
                  : undefined
          }
        >
          {isActive ? 'Already active' : busy ? 'Sending…' : done ? 'Sent' : 'Activate policy'}
        </button>
      </div>
    </Modal>
  );
}
