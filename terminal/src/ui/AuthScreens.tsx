/** Login screen and step-up (passphrase re-entry) dialog. */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useAuth, useRuntime, useStepUp } from '../state/react';
import { Modal } from './Modal';

function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="6" fill="var(--surface-3)" />
      <path d="M8 9v9a8 8 0 0 0 16 0V9" fill="none" stroke="var(--accent)" strokeWidth="3" strokeLinecap="round" />
      <circle cx="16" cy="18" r="2.5" fill="var(--warn)" />
    </svg>
  );
}

export { BrandMark };

export function LoginScreen() {
  const { auth } = useRuntime();
  const snap = useAuth();
  const [pass, setPass] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const errId = useId();

  useEffect(() => inputRef.current?.focus(), []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!pass || busy) return;
    setBusy(true);
    setError(null);
    const err = await auth.login(pass);
    setBusy(false);
    if (err) {
      setError(err);
      inputRef.current?.select();
    } else {
      setPass('');
    }
  };

  return (
    <main className="login-wrap">
      <form className="login" onSubmit={(e) => void submit(e)} aria-labelledby="login-title">
        <div className="brand">
          <BrandMark />
          <span>Unknown</span>
        </div>
        <h1 id="login-title">Owner sign-in</h1>
        <p className="muted" style={{ marginTop: 0 }}>
          This terminal is a view of the core’s authoritative state and an authenticated command surface.
        </p>
        {snap.notice ? (
          <p className="callout" role="status">
            {snap.notice}
          </p>
        ) : null}
        <div className="field">
          <label htmlFor="login-pass">Passphrase</label>
          <input
            ref={inputRef}
            id="login-pass"
            className="input"
            type="password"
            autoComplete="current-password"
            value={pass}
            onChange={(e) => setPass(e.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errId : undefined}
            required
          />
        </div>
        <div id={errId} className="form-error" role="alert">
          {error ?? ''}
        </div>
        <button type="submit" className="btn primary" style={{ width: '100%', height: 34 }} disabled={busy || !pass}>
          {busy ? 'Checking…' : 'Sign in'}
        </button>
        <p className="muted" style={{ fontSize: 'var(--fs-sm)', marginBottom: 0 }}>
          No passphrase yet? On the host: <code className="mono">python -m unknown.cli set-passphrase</code>
        </p>
      </form>
    </main>
  );
}

const KIND_TEXT: Record<string, string> = {
  start: 'Starting (green)',
  publish_policy: 'Publishing a policy',
  activate_policy: 'Activating a policy',
  authorize_capital: 'Authorizing capital',
  deauthorize_capital: 'Deauthorizing capital',
  acquire_execution_ownership: 'Acquiring execution ownership',
  release_execution_ownership: 'Releasing execution ownership',
  resolve_exception: 'Resolving an exception',
};

/** Shown when the API answers 428; on success the client retries the same command id. */
export function StepUpDialog() {
  const { stepUp, auth } = useRuntime();
  const snap = useStepUp();
  const [pass, setPass] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (snap.open) {
      setPass('');
      setError(null);
    }
  }, [snap.open]);

  const req = snap.request;
  const action = req ? (KIND_TEXT[req.kind] ?? req.kind) : '';

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!pass || busy) return;
    setBusy(true);
    const err = await auth.reauth(pass);
    setBusy(false);
    if (err) {
      setError(err);
      inputRef.current?.select();
      return;
    }
    setPass('');
    stepUp.settle(true);
  };

  return (
    <Modal open={snap.open} onClose={() => stepUp.settle(false)} labelledBy="stepup-title" describedBy="stepup-desc" initialFocus={inputRef}>
      <form onSubmit={(e) => void submit(e)}>
        <header>
          <h2 id="stepup-title">Confirm with your passphrase</h2>
        </header>
        <div className="modal-body">
          <p id="stepup-desc" style={{ marginTop: 0 }}>
            {action} requires a passphrase entry from the last 15 minutes.
            {req && req.commandId !== 'preflight' ? (
              <>
                {' '}
                The command <span className="cmd-id">{req.commandId}</span> has not been executed; it is sent again with the same id after you confirm.
              </>
            ) : null}
          </p>
          <div className="field">
            <label htmlFor="stepup-pass">Passphrase</label>
            <input
              ref={inputRef}
              id="stepup-pass"
              className="input"
              type="password"
              autoComplete="current-password"
              value={pass}
              onChange={(e) => setPass(e.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? 'stepup-err' : undefined}
            />
          </div>
          <div id="stepup-err" className="form-error" role="alert">
            {error ?? ''}
          </div>
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => stepUp.settle(false)}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={!pass || busy}>
            {busy ? 'Checking…' : 'Confirm'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
