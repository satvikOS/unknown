/**
 * Up/down colour convention (a local preference): green-up/red-down by default, red-up/green-down as the option
 * used in several Asian markets. Applied as `data-updown` on <html>; the --up/--down tokens swap in styles.css.
 */
import { useSyncExternalStore } from 'react';

export type UpDownConvention = 'green-up' | 'red-up';

const KEY = 'unknown.terminal.updown';
const listeners = new Set<() => void>();

function read(): UpDownConvention {
  try {
    return window.localStorage.getItem(KEY) === 'red-up' ? 'red-up' : 'green-up';
  } catch {
    return 'green-up';
  }
}

let current: UpDownConvention = typeof window === 'undefined' ? 'green-up' : read();

export function applyUpDown(choice: UpDownConvention = current): void {
  if (typeof document === 'undefined') return;
  if (choice === 'red-up') document.documentElement.dataset.updown = 'red-up';
  else delete document.documentElement.dataset.updown;
}

export function setUpDown(choice: UpDownConvention): void {
  current = choice;
  try {
    if (choice === 'green-up') window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, choice);
  } catch {
    // storage unavailable: the choice applies for this page only
  }
  applyUpDown(choice);
  for (const l of [...listeners]) l();
}

export function useUpDown(): UpDownConvention {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
    () => current,
  );
}
