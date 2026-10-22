/**
 * Shared control state: which drawer section is open, and the stop/start command paths.
 * The red stop needs only a session; green start needs step-up (the API answers 428 and the client
 * prompts, then retries with the same command id).
 */
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { defaultCommandId } from '../api/client';
import { useQuery, useRuntime, useSendCommand } from '../state/react';

export type DrawerSection = 'stop' | 'start';

interface ControlContextValue {
  drawer: DrawerSection | null;
  openDrawer: (section: DrawerSection, opener?: HTMLElement | null) => void;
  closeDrawer: () => void;
  /** Send red STOP immediately (no confirmation: stopping is the safe direction) and show progress. */
  stop: () => Promise<void>;
  /** Send green START (the panel's "Request start"). */
  start: () => Promise<void>;
  lastStopId: string | null;
  lastStartId: string | null;
  accountKey: string | null;
}

const ControlContext = createContext<ControlContextValue | null>(null);

export function useControl(): ControlContextValue {
  const v = useContext(ControlContext);
  if (!v) throw new Error('useControl must be used inside <ControlProvider>');
  return v;
}

/** The account this API serves: from the overview, else the first registered account. */
export function useAccountKey(): string | null {
  const overview = useQuery('/api/overview');
  const accounts = useQuery(overview.data?.account ? null : '/api/accounts');
  return overview.data?.account?.account_key ?? accounts.data?.[0]?.account_key ?? null;
}

export function ControlProvider({ children }: { children: ReactNode }) {
  const { notices } = useRuntime();
  const send = useSendCommand();
  const accountKey = useAccountKey();
  const [drawer, setDrawer] = useState<DrawerSection | null>(null);
  const [lastStopId, setLastStopId] = useState<string | null>(null);
  const [lastStartId, setLastStartId] = useState<string | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  const openDrawer = useCallback((section: DrawerSection, from?: HTMLElement | null) => {
    if (from) opener.current = from;
    else if (document.activeElement instanceof HTMLElement) opener.current = document.activeElement;
    setDrawer(section);
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawer(null);
    const el = opener.current;
    if (el && document.contains(el)) el.focus();
  }, []);

  const stop = useCallback(async () => {
    setDrawer('stop');
    if (!accountKey) {
      notices.push({
        tone: 'error',
        title: 'STOP not sent: account unknown',
        detail: 'Use the owner CLI: python -m unknown.cli stop --account <key>, or the broker’s own cancel-all.',
      });
      return;
    }
    // The durable id is chosen and shown before the request leaves, so it is visible while sending.
    const commandId = defaultCommandId('stop');
    setLastStopId(commandId);
    await send('stop', { account_key: accountKey, reason: 'OWNER_TERMINAL_STOP' }, { label: 'STOP', commandId });
  }, [accountKey, notices, send]);

  const start = useCallback(async () => {
    if (!accountKey) return;
    const commandId = defaultCommandId('start');
    setLastStartId(commandId);
    await send('start', { account_key: accountKey }, { label: 'START', commandId });
  }, [accountKey, send]);

  const value = useMemo(
    () => ({ drawer, openDrawer, closeDrawer, stop, start, lastStopId, lastStartId, accountKey }),
    [drawer, openDrawer, closeDrawer, stop, start, lastStopId, lastStartId, accountKey],
  );
  return <ControlContext.Provider value={value}>{children}</ControlContext.Provider>;
}
