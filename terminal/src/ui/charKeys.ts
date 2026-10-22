/**
 * Single-character shortcuts ("g" sequences, "?", and workspace keys such as the Orders "n") can be
 * turned off (WCAG 2.2 SC 2.1.4: speech-input users trigger them by accident). Cmd/Ctrl+K and the
 * table keys (arrows, Enter) are not character shortcuts and always work. A harmless local preference.
 */
import { useSyncExternalStore } from 'react';

const KEY = 'unknown.terminal.charKeys';
const listeners = new Set<() => void>();

function read(): boolean {
  try {
    return window.localStorage.getItem(KEY) !== 'off';
  } catch {
    return true;
  }
}

let enabled = typeof window === 'undefined' ? true : read();

export function charKeysEnabled(): boolean {
  return enabled;
}

export function setCharKeys(on: boolean): void {
  enabled = on;
  try {
    if (on) window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, 'off');
  } catch {
    // storage unavailable: applies to this page only
  }
  for (const l of [...listeners]) l();
}

export function useCharKeys(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => enabled,
  );
}
