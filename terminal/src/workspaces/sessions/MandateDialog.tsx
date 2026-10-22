/**
 * Create a session request (create_session) or amend an unfunded one (amend_session_request).
 * Optional seed and target overrides apply to this mandate only; editing the defaults is a policy
 * change. The implied required net return is computed exactly with the core's rule, and the dialog
 * states that the target is not a forecast. The core repeats every check and decides funding.
 */
import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react';
import type { Session } from '../../api/types';
import { useAccountKey } from '../../controls/ControlProvider';
import { CommandStatus } from '../../controls/Controls';
import { useQuery, useRuntime, useTrackedCommand } from '../../state/react';
import { Modal } from '../../ui/Modal';
import { Money, ShortId, StatusPill, YesNo } from '../../ui/primitives';
import { createdSessionId, isInFlight, sessionCommandLog } from './commandLog';
import { useSessionSend } from './hooks';
import { bpsToPercent, moneyText } from './format';
import { checkMandateInput, type MandateDefaults } from './mandateInput';
import { NoteList, Pct } from './parts';
import { formatDurationS, readSessionPolicy, type SessionPolicyView } from './policy';

/** Ledger scale of every currency the core registers today (domain/money.py CURRENCIES). */
const LEDGER_SCALE = 6;

export interface MandateDialogProps {
  mode: 'create' | 'amend';
  open: boolean;
  onClose: () => void;
  /** Required for amend. */
  session?: Session;
}

export function MandateDialog({ mode, open, onClose, session }: MandateDialogProps) {
  const { router } = useRuntime();
  const accountKey = useAccountKey();
  const sessionSend = useSessionSend();
  const uid = useId();
  const seedRef = useRef<HTMLInputElement>(null);
  const [seed, setSeed] = useState('');
  const [target, setTarget] = useState('');
  const [touched, setTouched] = useState(false);
  const [commandId, setCommandId] = useState<string | null>(null);
  const tracked = useTrackedCommand(commandId);

  const active = useQuery(open && mode === 'create' ? '/api/policy/active' : null);
  const policies = useQuery(open && mode === 'amend' ? '/api/policies' : null);
  const buckets = useQuery(open ? '/api/buckets' : null);
  const overview = useQuery(open ? '/api/overview' : null);

  useEffect(() => {
    if (open) {
      setSeed('');
      setTarget('');
      setTouched(false);
      setCommandId(null);
    }
  }, [open]);

  const policy: SessionPolicyView | null = useMemo(() => {
    if (mode === 'create') return active.data ? readSessionPolicy(active.data) : null;
    const hash = session?.mandate?.policy_hash;
    const rec = hash ? policies.data?.find((p) => p.policy_hash === hash) : undefined;
    return rec ? readSessionPolicy(rec) : null;
  }, [mode, active.data, policies.data, session?.mandate?.policy_hash]);

  const currency = mode === 'amend' ? (session?.currency ?? policy?.currency ?? '') : (policy?.currency ?? '');
  const defaults: MandateDefaults | null =
    mode === 'amend'
      ? session
        ? { seed: session.seed, target: session.target }
        : null
      : policy?.template.seed && policy.template.target
        ? { seed: policy.template.seed, target: policy.template.target }
        : null;

  const unallocated = buckets.data?.find((b) => b.owner === 'unallocated' && b.currency === currency);
  const control = overview.data?.control;
  const check = defaults
    ? checkMandateInput(
        { seed, target },
        defaults,
        {
          currency,
          ledgerScale: LEDGER_SCALE,
          ...(policy?.limits.authorizedCapital !== undefined ? { authorizedCapitalLimit: policy.limits.authorizedCapital } : {}),
          ...(policy?.fees.minimum !== undefined ? { feeMinimum: policy.fees.minimum } : {}),
          ...(policy?.fees.estimateMarginBps !== undefined ? { feeEstimateMarginBps: policy.fees.estimateMarginBps } : {}),
        },
        {
          ...(unallocated ? { unallocatedAvailable: unallocated.available_cash } : {}),
          ...(policy?.limits.operationalBuffer !== undefined ? { operationalBuffer: policy.limits.operationalBuffer } : {}),
          stopLatched: control?.stop_latched ?? false,
          needsReconciliation: control?.needs_reconciliation ?? false,
          blockingExceptions: overview.data?.exceptions_open.filter((e) => e.blocking).length ?? 0,
        },
      )
    : null;

  const noChange = mode === 'amend' && check !== null && !check.overrides.seed && !check.overrides.target_terminal_equity;
  const busy = isInFlight(tracked);
  const loading = mode === 'create' ? active.status === 'loading' : policies.status === 'loading';
  const noPolicy = mode === 'create' && active.status === 'success' && !active.data;
  const canSubmit = !!check && check.ok && !noChange && !busy && !!accountKey && !noPolicy && (mode === 'create' || !!session);

  const seedErr = touched ? check?.errors.find((e) => e.field === 'seed') : undefined;
  const targetErr = touched ? check?.errors.find((e) => e.field === 'target') : undefined;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!canSubmit || !check || !accountKey) return;
    const t =
      mode === 'create'
        ? await sessionSend('create_session', { account_key: accountKey, ...check.overrides }, 'Create session request', null)
        : await sessionSend('amend_session_request', { session_id: session!.session_id, ...check.overrides }, `Amend ${session!.session_id}`, session!.session_id);
    setCommandId(t.commandId);
    if (t.status === 'pending' || t.status === 'applied') {
      if (mode === 'create') {
        const sid = createdSessionId(t);
        if (sid) {
          sessionCommandLog.bind(t.commandId, sid);
          onClose();
          router.navigate('sessions', sid);
        }
      } else {
        onClose();
      }
    }
  };

  const title = mode === 'create' ? 'New session request' : `Amend request ${session?.session_id ?? ''}`;
  return (
    <Modal open={open} onClose={onClose} labelledBy={`${uid}-title`} describedBy={`${uid}-desc`} initialFocus={seedRef} className="ses-dialog">
      <form onSubmit={(e) => void submit(e)} noValidate>
        <header>
          <h2 id={`${uid}-title`}>{title}</h2>
        </header>
        <div className="modal-body">
          <p id={`${uid}-desc`} style={{ marginTop: 0 }}>
            {mode === 'create'
              ? 'Records a session mandate from the active policy’s template. Leave a field empty to use the template value. Overrides apply to this mandate only; changing the defaults is a new policy version.'
              : 'Replaces the seed or target of this unfunded request with a new mandate (a new mandate hash) and re-evaluates funding. Empty fields keep their current value.'}
          </p>
          {noPolicy ? (
            <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
              No policy is active for this account, so no mandate can be created.
            </div>
          ) : null}
          {loading ? <p className="muted">Loading the governing policy…</p> : null}

          <div className="ses-form-grid">
            <div className="field">
              <label htmlFor={`${uid}-seed`}>Seed ({currency || 'currency'})</label>
              <input
                ref={seedRef}
                id={`${uid}-seed`}
                className="input"
                inputMode="decimal"
                autoComplete="off"
                spellCheck={false}
                placeholder={defaults?.seed ?? ''}
                value={seed}
                onChange={(e) => setSeed(e.target.value)}
                onBlur={() => setTouched(true)}
                aria-invalid={seedErr ? true : undefined}
                aria-describedby={`${uid}-seed-hint ${uid}-seed-err`}
              />
              <span id={`${uid}-seed-hint`} className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
                {defaults ? `Empty keeps the ${mode === 'create' ? 'template' : 'current'} value, ${moneyText(defaults.seed, currency)}.` : ' '}
              </span>
              <div id={`${uid}-seed-err`} className="form-error" role="alert">
                {seedErr?.message ?? ''}
              </div>
            </div>
            <div className="field">
              <label htmlFor={`${uid}-target`}>Target terminal equity ({currency || 'currency'})</label>
              <input
                id={`${uid}-target`}
                className="input"
                inputMode="decimal"
                autoComplete="off"
                spellCheck={false}
                placeholder={defaults?.target ?? ''}
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                onBlur={() => setTouched(true)}
                aria-invalid={targetErr ? true : undefined}
                aria-describedby={`${uid}-target-hint ${uid}-target-err`}
              />
              <span id={`${uid}-target-hint`} className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
                {defaults ? `Empty keeps the ${mode === 'create' ? 'template' : 'current'} value, ${moneyText(defaults.target, currency)}.` : ' '}
              </span>
              <div id={`${uid}-target-err`} className="form-error" role="alert">
                {targetErr?.message ?? ''}
              </div>
            </div>
          </div>

          <div className="ses-implied" aria-live="polite">
            <span className="micro">Implied required net return</span>
            <span className="pct">{check?.requiredPct ? <Pct value={check.requiredPct} /> : <span className="muted">—</span>}</span>
            {check?.requiredPct ? (
              <span className="muted">
                from <Money amount={check.seed} currency={currency} /> to <Money amount={check.target} currency={currency} />, net of all costs
              </span>
            ) : null}
          </div>
          <p style={{ margin: '6px 0 0', fontSize: 'var(--fs-sm)' }}>
            <strong>The target is a configured mandate, not a forecast or a funding authorization.</strong>{' '}
            <span className="muted">Nothing here estimates whether or when it is reached.</span>
          </p>
          {check ? <NoteList notes={check.notes} label="Feasibility and funding" /> : null}
          {policy ? <PolicySummary policy={policy} mode={mode} session={session} /> : null}
          {mode === 'amend' && !policy && policies.status === 'success' ? (
            <p className="muted">The policy that governs this mandate is not in the policy list; the core still validates the amendment against it.</p>
          ) : null}
          {noChange ? <p className="muted">Enter a new seed or target to amend the request.</p> : null}
          {commandId && tracked && tracked.status !== 'pending' && tracked.status !== 'applied' ? (
            <div className="section-gap">
              <CommandStatus commandId={commandId} />
            </div>
          ) : null}
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
          <button type="submit" className="btn primary" disabled={!canSubmit}>
            {busy ? 'Sending…' : mode === 'create' ? 'Record request' : 'Amend request'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function PolicySummary({ policy, mode, session }: { policy: SessionPolicyView; mode: 'create' | 'amend'; session?: Session }) {
  const t = policy.template;
  const m = session?.mandate;
  const ccy = policy.currency;
  return (
    <details className="ses-info section-gap">
      <summary>
        {mode === 'create' ? 'Template and rules from the active policy' : 'Rules from the mandate’s policy'}: {policy.name} <ShortId value={policy.policyHash} head={10} />
      </summary>
      <dl className="kv" style={{ marginTop: 8 }}>
        <dt>Currency</dt>
        <dd>
          {ccy} {policy.mode ? <StatusPill label={policy.mode} tone="info" compact /> : null}
        </dd>
        {mode === 'create' ? (
          <>
            <dt>Template seed</dt>
            <dd>{t.seed ? <Money amount={t.seed} currency={ccy} /> : <span className="muted">not set</span>}</dd>
            <dt>Template target</dt>
            <dd>{t.target ? <Money amount={t.target} currency={ccy} /> : <span className="muted">not set</span>}</dd>
          </>
        ) : null}
        <dt>Loss triggers</dt>
        <dd>
          {mode === 'amend' && m ? (
            <>
              suspend at {bpsToPercent(m.session_drawdown_trigger_bps)} below high-water, or a loss of <Money amount={m.session_max_loss} currency={ccy} /> from the seed
            </>
          ) : t.drawdownTriggerBps !== undefined && t.maxLoss ? (
            <>
              suspend at {bpsToPercent(t.drawdownTriggerBps)} below high-water, or a loss of <Money amount={t.maxLoss} currency={ccy} /> from the seed
            </>
          ) : (
            <span className="muted">not set</span>
          )}
        </dd>
        <dt>Auto start</dt>
        <dd>{t.autoStart === undefined ? <span className="muted">not set</span> : <YesNo value={t.autoStart} />}</dd>
        <dt>Expiry</dt>
        <dd>{t.expiresAfterS === null ? 'none' : t.expiresAfterS !== undefined ? `after ${formatDurationS(t.expiresAfterS)}` : <span className="muted">not set</span>}</dd>
        <dt>Below target after observation</dt>
        <dd>
          {t.belowTarget ?? <span className="muted">not set</span>}
          {t.maxResumes !== undefined ? <span className="muted"> (max resumes {t.maxResumes})</span> : null}
        </dd>
        <dt>Strategies</dt>
        <dd>{t.strategies?.length ? t.strategies.join(', ') : <span className="muted">none</span>}</dd>
        <dt>Authorized capital limit</dt>
        <dd>{policy.limits.authorizedCapital ? <Money amount={policy.limits.authorizedCapital} currency={ccy} kind="authorized" /> : <span className="muted">not set</span>}</dd>
        <dt>Protected proceeds</dt>
        <dd>
          Completed proceeds stay protected; successor {policy.successor.createOnSuccess ? 'created on success' : 'not created'}
          {policy.successor.reinvestProtected === false ? ' and funded only from unallocated cash, never from protected proceeds' : ''}.
        </dd>
      </dl>
    </details>
  );
}
