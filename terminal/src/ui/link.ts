/**
 * Linked security context (CAP-UX-03): panels on the same channel follow one security. A security key is
 * `<SYMBOL>.<MIC>` (e.g. `AAPL.XNAS`, `RELIANCE.XNSE`, `SAP.XETR`; MICs from config/markets/venues.json) or a core
 * instrument id. Workspaces read the key with useLinkedSecurity() and publish with setLinkedSecurity(); the
 * workstation shell decides which channel each panel listens to. Channel 'default' always exists.
 */
import { useSyncExternalStore } from 'react';

export type LinkChannel = string;

const current = new Map<LinkChannel, string | null>([['default', null]]);
const listeners = new Set<() => void>();

export function linkedSecurity(channel: LinkChannel = 'default'): string | null {
  return current.get(channel) ?? null;
}

export function setLinkedSecurity(key: string | null, channel: LinkChannel = 'default'): void {
  if ((current.get(channel) ?? null) === key) return;
  current.set(channel, key);
  for (const l of [...listeners]) l();
}

export function useLinkedSecurity(channel: LinkChannel = 'default'): string | null {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => linkedSecurity(channel),
  );
}

/** Parse a security key into symbol and market (MIC); null market for a bare symbol or a core instrument id. */
export function parseSecurityKey(key: string): { symbol: string; mic: string | null } {
  const dot = key.lastIndexOf('.');
  if (dot <= 0 || dot === key.length - 1) return { symbol: key, mic: null };
  const mic = key.slice(dot + 1);
  return /^X[A-Z0-9]{3}$/.test(mic) ? { symbol: key.slice(0, dot), mic } : { symbol: key, mic: null };
}
