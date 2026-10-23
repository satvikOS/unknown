/**
 * Linked security context (CAP-UX-03): panels on the same channel follow one security. A security key is
 * `<SYMBOL>.<MIC>` (e.g. `AAPL.XNAS`, `RELIANCE.XNSE`, `SAP.XETR`; MICs from config/markets/venues.json) or a core
 * instrument id. Workspaces read the key with useLinkedSecurity() and publish with useSetLinkedSecurity() (or
 * setLinkedSecurity(key, channel)); the workstation shell decides which channel each panel listens to by wrapping
 * the panel in <LinkChannelProvider>. Outside a panel (a single workspace opened by its hash route) the channel is
 * 'default'. Channel 'default' always exists. Nothing here assumes a broker or a data source.
 */
import { createContext, createElement, useCallback, useContext, useSyncExternalStore, type ReactNode } from 'react';

export type LinkChannel = string;

/** A user-visible link channel: number, name and colour token (colour is never the only indicator). */
export interface LinkChannelInfo {
  id: LinkChannel;
  num: number;
  name: string;
  /** CSS colour for the chip, a token defined in styles.css. */
  color: string;
}

/** Eight user channels in the FDC3 recommended order; channel 1 is 'default'. */
export const LINK_CHANNELS: readonly LinkChannelInfo[] = [
  { id: 'default', num: 1, name: 'Red', color: 'var(--link-1)' },
  { id: 'ch2', num: 2, name: 'Orange', color: 'var(--link-2)' },
  { id: 'ch3', num: 3, name: 'Yellow', color: 'var(--link-3)' },
  { id: 'ch4', num: 4, name: 'Green', color: 'var(--link-4)' },
  { id: 'ch5', num: 5, name: 'Cyan', color: 'var(--link-5)' },
  { id: 'ch6', num: 6, name: 'Blue', color: 'var(--link-6)' },
  { id: 'ch7', num: 7, name: 'Magenta', color: 'var(--link-7)' },
  { id: 'ch8', num: 8, name: 'Purple', color: 'var(--link-8)' },
];

/** Prefix of a panel's private channel: an unlinked panel follows nobody and nobody follows it. */
export const UNLINKED_PREFIX = 'solo:';

export function unlinkedChannel(panelId: string): LinkChannel {
  return `${UNLINKED_PREFIX}${panelId}`;
}

export function isUnlinked(channel: LinkChannel): boolean {
  return channel.startsWith(UNLINKED_PREFIX);
}

export function channelInfo(channel: LinkChannel): LinkChannelInfo | null {
  return LINK_CHANNELS.find((c) => c.id === channel) ?? null;
}

const current = new Map<LinkChannel, string | null>([['default', null]]);
const listeners = new Map<LinkChannel, Set<() => void>>();

export function linkedSecurity(channel: LinkChannel = 'default'): string | null {
  return current.get(channel) ?? null;
}

export function setLinkedSecurity(key: string | null, channel: LinkChannel = 'default'): void {
  if ((current.get(channel) ?? null) === key) return;
  current.set(channel, key);
  for (const l of [...(listeners.get(channel) ?? [])]) l();
}

/** Subscribe to one channel; returns the unsubscribe function. */
export function subscribeLinkedSecurity(channel: LinkChannel, listener: () => void): () => void {
  let set = listeners.get(channel);
  if (!set) {
    set = new Set();
    listeners.set(channel, set);
  }
  set.add(listener);
  return () => {
    set.delete(listener);
  };
}

const ChannelContext = createContext<LinkChannel>('default');

/** Sets the channel that useLinkedSecurity() and useSetLinkedSecurity() use for everything inside it. */
export function LinkChannelProvider({ channel, children }: { channel: LinkChannel; children?: ReactNode }) {
  return createElement(ChannelContext.Provider, { value: channel }, children);
}

/** The channel of the panel this component is in ('default' outside the workstation). */
export function useLinkChannel(): LinkChannel {
  return useContext(ChannelContext);
}

/** The security on `channel`, or on the enclosing panel's channel when no channel is given. */
export function useLinkedSecurity(channel?: LinkChannel): string | null {
  const fromContext = useContext(ChannelContext);
  const ch = channel ?? fromContext;
  const subscribe = useCallback((l: () => void) => subscribeLinkedSecurity(ch, l), [ch]);
  return useSyncExternalStore(
    subscribe,
    () => linkedSecurity(ch),
    () => linkedSecurity(ch),
  );
}

/** A setter bound to the enclosing panel's channel: selecting a row publishes it to every linked panel. */
export function useSetLinkedSecurity(): (key: string | null) => void {
  const ch = useContext(ChannelContext);
  return useCallback((key: string | null) => setLinkedSecurity(key, ch), [ch]);
}

/** Parse a security key into symbol and market (MIC); null market for a bare symbol or a core instrument id. */
export function parseSecurityKey(key: string): { symbol: string; mic: string | null } {
  const dot = key.lastIndexOf('.');
  if (dot <= 0 || dot === key.length - 1) return { symbol: key, mic: null };
  const mic = key.slice(dot + 1);
  return /^[A-Z][A-Z0-9]{3}$/.test(mic) ? { symbol: key.slice(0, dot), mic } : { symbol: key, mic: null };
}
