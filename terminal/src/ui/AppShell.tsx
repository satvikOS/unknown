/** Authenticated application shell: header, navigation, workspace, overlays and global shortcuts. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ControlDrawer } from '../controls/Controls';
import { ControlProvider, useControl } from '../controls/ControlProvider';
import { useQuery, useRoute, useRuntime } from '../state/react';
import { DEFAULT_WORKSPACE, findWorkspace, hasWorkspace, WORKSPACES } from '../workspaces/registry';
import { StepUpDialog } from './AuthScreens';
import { charKeysEnabled, setCharKeys, useCharKeys } from './charKeys';
import { CommandPalette } from './CommandPalette';
import { Header } from './Header';
import { Icon } from './Icon';
import { isTypingTarget, MOD_LABEL, SequenceMatcher } from './keys';
import { Modal } from './Modal';
import { Notices } from './Notices';
import type { PaletteItem } from './palette';
import { Kbd, modeColor } from './primitives';
import { nextTheme, setTheme, useTheme } from './theme';

function Nav({ current, onHelp }: { current: string; onHelp: () => void }) {
  return (
    <nav className="nav" aria-label="Workspaces">
      <ul>
        {WORKSPACES.map((w) => (
          <li key={w.id}>
            <a
              className="nav-item"
              href={`#/${w.id}`}
              aria-current={w.id === current ? 'page' : undefined}
              title={w.description ? `${w.description} (keys: ${w.shortcut})` : `Keys: ${w.shortcut}`}
            >
              <Icon name={w.icon ?? 'generic'} />
              <span>{w.title}</span>
              <Kbd keys={w.shortcut} />
            </a>
          </li>
        ))}
      </ul>
      <div className="nav-foot">
        <button type="button" className="btn small" onClick={onHelp} aria-keyshortcuts="Shift+?">
          Keyboard shortcuts <kbd>?</kbd>
        </button>
      </div>
    </nav>
  );
}

function ShortcutsHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  const charKeys = useCharKeys();
  const rows: [string, string][] = [
    [`${MOD_LABEL} K`, 'Command palette: jump to workspaces and records, run commands'],
    ['?', 'This list'],
    ...WORKSPACES.map((w): [string, string] => [w.shortcut, `Go to ${w.title}`]),
    ...WORKSPACES.flatMap((w) => (w.keys ?? []).map((k): [string, string] => [k.keys, `${w.title}: ${k.action}`])),
    ['↑ ↓ Home End PgUp PgDn', 'Move between table rows (when a row has focus)'],
    ['Enter', 'Open the focused row or palette result'],
    ['Esc', 'Close the palette, dialog or control panel'],
  ];
  return (
    <Modal open={open} onClose={onClose} labelledBy="help-title">
      <header>
        <h2 id="help-title">Keyboard shortcuts</h2>
      </header>
      <div className="modal-body">
        <table className="data">
          <caption className="sr-only">Keyboard shortcuts</caption>
          <thead>
            <tr>
              <th scope="col">
                <span className="th-inner">Keys</span>
              </th>
              <th scope="col">
                <span className="th-inner">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={`${k}|${v}`}>
                <td>
                  <Kbd keys={k} />
                  <span className="sr-only">{k}</span>
                </td>
                <td className="wrap">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <label className="check-row section-gap">
          <input type="checkbox" checked={charKeys} onChange={(e) => setCharKeys(e.target.checked)} />
          <span>
            Single-character shortcuts (<kbd>g</kbd> sequences, <kbd>?</kbd> and workspace keys). Turn off if they fire by accident, for
            example with speech input; {MOD_LABEL} K and the table keys keep working.
          </span>
        </label>
        <p className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          STOP has no single-key shortcut, to avoid accidental activation: use the red button, or {MOD_LABEL} K then Enter (STOP is the first
          palette entry). STOP needs no passphrase.
        </p>
      </div>
      <div className="modal-actions">
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}

function usePaletteItems(open: boolean, openHelp: () => void): { items: PaletteItem[]; status: string | undefined } {
  const { router, stream, auth } = useRuntime();
  const control = useControl();
  const theme = useTheme();
  const charKeys = useCharKeys();
  const sessions = useQuery(open && hasWorkspace('sessions') ? '/api/sessions' : null);
  const orders = useQuery(open && hasWorkspace('orders') ? '/api/orders' : null, { limit: 200 });
  const instruments = useQuery(open && hasWorkspace('instruments') ? '/api/instruments' : null);

  const items = useMemo(() => {
    const out: PaletteItem[] = [
      {
        id: 'cmd:stop',
        group: 'Control',
        label: 'STOP: block new entries and cancel open buys',
        hint: 'no passphrase',
        keywords: 'red halt emergency kill',
        danger: true,
        run: () => void control.stop(),
      },
      {
        id: 'cmd:start',
        group: 'Control',
        label: 'Start: open preflight and request start',
        hint: 'passphrase',
        keywords: 'green resume preflight blockers',
        run: () => control.openDrawer('start'),
      },
      {
        id: 'cmd:stop-progress',
        group: 'Control',
        label: 'Show stop progress',
        keywords: 'flat residual cancels unknown',
        run: () => control.openDrawer('stop'),
      },
    ];
    for (const w of WORKSPACES) {
      out.push({
        id: `ws:${w.id}`,
        group: 'Workspaces',
        label: w.title,
        hint: w.shortcut,
        keywords: [w.id, w.description ?? '', ...(w.keywords ?? [])].join(' '),
        run: () => router.navigate(w.id),
      });
    }
    for (const w of WORKSPACES) {
      for (const c of w.commands ?? []) {
        out.push({
          id: `ws:${w.id}:${c.id}`,
          group: 'Go to',
          label: `${w.title}: ${c.label}`,
          hint: c.hint,
          keywords: `${w.id} ${c.keywords ?? ''}`,
          run: () => router.navigate(w.id, ...c.path),
        });
      }
    }
    for (const s of sessions.data ?? []) {
      out.push({
        id: `ses:${s.session_id}`,
        group: 'Sessions',
        label: s.session_id,
        hint: s.state,
        keywords: `session ${s.state} ${s.outcome ?? ''}`,
        run: () => router.navigate('sessions', s.session_id),
      });
    }
    for (const o of orders.data ?? []) {
      out.push({
        id: `ord:${o.order_id}`,
        group: 'Orders',
        label: `${o.order_id} ${o.side} ${o.instrument_id}`,
        hint: o.state,
        keywords: `order ${o.session_id} ${o.broker_order_id ?? ''}`,
        run: () => router.navigate('orders', o.order_id),
      });
    }
    for (const i of instruments.data ?? []) {
      out.push({
        id: `ins:${i.instrument_id}`,
        group: 'Instruments',
        label: `${i.symbol} ${i.name}`,
        hint: i.instrument_id,
        keywords: `instrument ${i.instrument_id} ${i.venue_mic} ${i.attributes.fixture ? 'fixture' : ''}`,
        run: () => router.navigate('instruments', i.instrument_id),
      });
    }
    out.push(
      {
        id: 'term:refresh',
        group: 'Terminal',
        label: 'Refresh all visible data',
        keywords: 'reload refetch',
        run: () => stream.refreshAll(),
      },
      {
        id: 'term:theme',
        group: 'Terminal',
        label: `Theme: switch to ${nextTheme(theme)}`,
        keywords: 'dark light system appearance',
        run: () => setTheme(nextTheme(theme)),
      },
      { id: 'term:help', group: 'Terminal', label: 'Keyboard shortcuts', hint: '?', keywords: 'keys help', run: openHelp },
      {
        id: 'term:charkeys',
        group: 'Terminal',
        label: charKeys ? 'Turn off single-character shortcuts' : 'Turn on single-character shortcuts',
        keywords: 'keys speech accessibility g sequences',
        run: () => setCharKeys(!charKeys),
      },
      { id: 'term:logout', group: 'Terminal', label: 'Sign out', keywords: 'logout session', run: () => void auth.logout() },
    );
    return out;
  }, [control, router, stream, auth, theme, charKeys, sessions.data, orders.data, instruments.data, openHelp]);

  const failing = [sessions, orders, instruments].filter((q) => q.error).length;
  const loading = [sessions, orders, instruments].some((q) => q.status === 'loading');
  const status = failing ? `${failing} record source${failing === 1 ? '' : 's'} failed to load` : loading ? 'Loading records…' : undefined;
  return { items, status };
}

function Shell() {
  const { router } = useRuntime();
  const { drawer, closeDrawer } = useControl();
  const route = useRoute();
  const overview = useQuery('/api/overview');
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const openHelp = useCallback(() => setHelpOpen(true), []);
  const { items, status } = usePaletteItems(paletteOpen, openHelp);

  const workspace = findWorkspace(route.workspace);
  useEffect(() => {
    if (!workspace) router.navigate(DEFAULT_WORKSPACE);
  }, [workspace, router]);

  // Move focus to the workspace title on navigation (screen readers announce the new page).
  const lastWs = useRef<string | null>(null);
  useEffect(() => {
    if (!workspace) return;
    if (lastWs.current !== null && lastWs.current !== workspace.id) {
      document.getElementById('ws-title')?.focus();
    }
    lastWs.current = workspace.id;
    document.title = `${workspace.title} · Unknown`;
  }, [workspace]);

  const matcher = useMemo(() => new SequenceMatcher(WORKSPACES.map((w) => w.shortcut)), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      if (document.querySelector('.overlay')) return; // a dialog owns the keyboard
      if (e.key === 'Escape' && drawer) {
        closeDrawer();
        return;
      }
      if (isTypingTarget(e.target) || !charKeysEnabled()) return;
      if (e.key === '?') {
        e.preventDefault();
        setHelpOpen(true);
        return;
      }
      const seq = matcher.feed(e.key, Date.now());
      if (seq) {
        const ws = WORKSPACES.find((w) => w.shortcut === seq);
        if (ws) {
          e.preventDefault();
          router.navigate(ws.id);
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [matcher, router, drawer, closeDrawer]);

  const mode = overview.data?.account?.mode ?? null;
  const Component = workspace?.component;
  return (
    <div className="app" style={{ ['--mode-color' as string]: modeColor(mode) }}>
      <a className="skip-link" href="#main" onClick={(e) => { e.preventDefault(); mainRef.current?.focus(); }}>
        Skip to workspace
      </a>
      <div style={{ position: 'sticky', top: 0, zIndex: 30 }}>
        <div className="mode-band" data-mode={mode ?? 'UNKNOWN'} aria-hidden="true" />
        <Header onOpenPalette={() => setPaletteOpen(true)} />
      </div>
      <div className="shell-body">
        <Nav current={workspace?.id ?? ''} onHelp={openHelp} />
        <main id="main" className="main" ref={mainRef} tabIndex={-1} aria-labelledby="ws-title">
          {Component && workspace ? <Component route={route} navigate={(id, ...rest) => router.navigate(id, ...rest)} /> : null}
        </main>
      </div>
      <ControlDrawer />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} items={items} status={status} />
      <ShortcutsHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
      <StepUpDialog />
      <Notices />
    </div>
  );
}

export function AppShell() {
  return (
    <ControlProvider>
      <Shell />
    </ControlProvider>
  );
}
