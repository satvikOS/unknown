/** Root: session check, then the login screen or the authenticated shell. */
import { useEffect } from 'react';
import { useAuth, useRuntime } from './state/react';
import { AppShell } from './ui/AppShell';
import { BrandMark, LoginScreen } from './ui/AuthScreens';
import { Notices } from './ui/Notices';

export function App() {
  const { auth } = useRuntime();
  const snap = useAuth();

  useEffect(() => {
    void auth.check();
  }, [auth]);

  if (snap.status === 'authenticated') return <AppShell />;
  if (snap.status === 'unauthenticated') {
    return (
      <>
        <LoginScreen />
        <Notices />
      </>
    );
  }
  return (
    <main className="login-wrap">
      <div className="login" role="status" aria-live="polite">
        <div className="brand">
          <BrandMark />
          <span>Unknown</span>
        </div>
        {snap.status === 'checking' ? (
          <p>Checking session…</p>
        ) : (
          <>
            <h1>API unreachable</h1>
            <p className="muted">{snap.error}</p>
            <p className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
              The terminal shows nothing it cannot read from the API. The owner CLI stop path does not depend on this page:
              <br />
              <code className="mono">python -m unknown.cli stop --account &lt;key&gt;</code>
            </p>
            <button type="button" className="btn primary" onClick={() => void auth.check()}>
              Retry
            </button>
          </>
        )}
      </div>
    </main>
  );
}
