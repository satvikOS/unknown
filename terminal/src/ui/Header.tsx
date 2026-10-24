/**
 * Header: account, persistent mode badge (text + colour), entry state, core/broker health, state
 * stream and latest seq, clock, palette, theme, green START and red STOP (always visible).
 */
import { useAuth, useNow, useQuery, useRuntime, useStream } from '../state/react';
import { GreenStart, RedStop } from '../controls/Controls';
import { entryState } from '../controls/model';
import type { CoreHealth, HealthResponse } from '../api/types';
import { formatClock } from '../format/time';
import { BrandMark } from './AuthScreens';
import { Icon } from './Icon';
import { MOD_LABEL } from './keys';
import { Badge, ModeBadge, StatusPill, type Tone } from './primitives';
import { nextTheme, setTheme, useTheme } from './theme';

function isCoreHealth(core: HealthResponse['core'] | undefined): core is CoreHealth {
  return !!core && !('error' in core);
}

function HealthPills() {
  const health = useQuery('/api/health', undefined, { pollMs: 5000 });
  if (health.error && !health.data) {
    return <StatusPill label="API UNREACHABLE" tone="bad" detail="The terminal API did not answer the health check." />;
  }
  const core = health.data?.core;
  if (!core) return <StatusPill label="CHECKING" tone="neutral" />;
  if (!isCoreHealth(core)) {
    return <StatusPill label="CORE DOWN" tone="bad" detail={core.error} />;
  }
  const brokerOk = core.broker_connected && core.broker_authenticated;
  const brokerLabel = !core.broker_connected ? 'BROKER DOWN' : !core.broker_authenticated ? 'BROKER AUTH' : 'BROKER OK';
  const coreTone: Tone = core.cycle_errors > 0 ? 'warn' : 'ok';
  return (
    <>
      <StatusPill
        label={core.cycle_errors > 0 ? `CORE ${core.cycle_errors} ERR` : 'CORE OK'}
        tone={coreTone}
        detail={`Core answered; ${core.cycle_errors} loop errors; disk free ${core.disk_free_mb} MB`}
      />
      <StatusPill label={brokerLabel} tone={brokerOk ? 'ok' : 'bad'} detail={`${core.provider}: ${core.detail}`} />
      {core.provider === 'simulated' ? (
        <Badge tone="info" title={`Simulated broker (${core.detail}): fills are simulated at prices from a real configured quote source; with none it fills nothing. No order reaches a market.`}>
          SIM BROKER
        </Badge>
      ) : null}
    </>
  );
}

function StreamPill() {
  const s = useStream();
  const tone: Tone = s.status === 'live' ? 'ok' : s.status === 'reconnecting' ? 'warn' : s.status === 'connecting' ? 'info' : 'neutral';
  // Never just "LIVE": that word is reserved for the account mode badge (a LIVE account trades real money).
  const label =
    s.status === 'live' ? 'STREAMING' : s.status === 'reconnecting' ? 'RECONNECTING' : s.status === 'connecting' ? 'CONNECTING' : 'STREAM OFFLINE';
  const detail = `State stream ${s.status}; ${s.reconnects} reconnects, ${s.gaps} gaps${s.lastError ? `; ${s.lastError}` : ''}`;
  return (
    <span className="topbar-group" title={detail}>
      <StatusPill label={label} tone={tone} detail={detail} />
      <span className="num muted">
        seq <span style={{ color: 'var(--text)' }}>{s.seq ?? '—'}</span>
      </span>
    </span>
  );
}

function Clock() {
  const now = useNow(1000);
  return (
    <span className="clock num" aria-label="Clock">
      <span>
        {formatClock(now, 'utc')} <span className="muted">UTC</span>
      </span>
      <span className="local muted">{formatClock(now, 'local')} local</span>
    </span>
  );
}

export function Header({ onOpenPalette }: { onOpenPalette: () => void }) {
  const { auth } = useRuntime();
  const authSnap = useAuth();
  const overview = useQuery('/api/overview');
  const health = useQuery('/api/health', undefined, { pollMs: 5000 });
  const theme = useTheme();
  const account = overview.data?.account ?? null;
  const control = overview.data?.control ?? null;
  const state = entryState(control, overview.data);
  const coreMode = health.data && isCoreHealth(health.data.core) ? health.data.core.account_mode : null;
  const modeMismatch = account && coreMode && coreMode !== account.mode;
  return (
    <header className="topbar" aria-label="Terminal status and controls">
      <div className="topbar-status">
        <div className="brand">
          <BrandMark />
          <span className="hide-sm">Unknown</span>
        </div>
        <span className="topbar-sep hide-sm" />
        <div className="topbar-group account-mode">
          <span className="account-id">
            <span className="label">Account</span>
            <span className="mono">{account?.account_key ?? '—'}</span>
          </span>
          <ModeBadge mode={account?.mode ?? coreMode} />
          {modeMismatch ? <StatusPill label="MODE MISMATCH" tone="bad" detail={`Ledger says ${account?.mode}, core broker says ${coreMode}`} /> : null}
        </div>
        <span className="topbar-sep" />
        <StatusPill label={state.label} tone={state.tone} detail={state.detail} />
        <span className="topbar-sep hide-md" />
        <div className="topbar-group hide-md">
          <HealthPills />
        </div>
        <span className="topbar-sep hide-sm" />
        <span className="hide-sm">
          <StreamPill />
        </span>
        <span className="topbar-sep hide-md hide-lg" />
        <span className="hide-md hide-lg">
          <Clock />
        </span>
      </div>
      <div className="topbar-commands">
        <button type="button" className="btn palette-trigger hide-sm" onClick={onOpenPalette} aria-keyshortcuts="Meta+K Control+K">
          <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
            <Icon name="search" size={14} /> <span className="hide-md hide-lg">Jump or run…</span>
          </span>
          <kbd>{MOD_LABEL} K</kbd>
        </button>
        <button
          type="button"
          className="btn ghost icon-btn"
          onClick={() => setTheme(nextTheme(theme))}
          aria-label={`Theme: ${theme}. Switch to ${nextTheme(theme)}.`}
          title={`Theme: ${theme}`}
        >
          <Icon name={theme === 'dark' ? 'moon' : theme === 'light' ? 'sun' : 'auto'} />
        </button>
        <button
          type="button"
          className="btn ghost icon-btn"
          onClick={() => void auth.logout()}
          aria-label="Sign out"
          title={authSnap.status === 'authenticated' ? 'Sign out' : 'Signed out'}
        >
          <Icon name="logout" />
        </button>
        <GreenStart />
        <RedStop />
      </div>
    </header>
  );
}
