/**
 * Theme preference (a harmless local preference): the dark workstation theme by default; light, or following the
 * system, are options.
 */
import { useSyncExternalStore } from 'react';

export type ThemeChoice = 'system' | 'light' | 'dark';

const KEY = 'unknown.terminal.theme';
const listeners = new Set<() => void>();

function read(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === 'light' || v === 'system' ? v : 'dark';
  } catch {
    return 'dark';
  }
}

let current: ThemeChoice = typeof window === 'undefined' ? 'dark' : read();

export function applyTheme(choice: ThemeChoice = current): void {
  const root = document.documentElement;
  if (choice === 'system') delete root.dataset.theme;
  else root.dataset.theme = choice;
}

export function setTheme(choice: ThemeChoice): void {
  current = choice;
  try {
    if (choice === 'dark') window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, choice);
  } catch {
    // storage unavailable (private mode): the choice still applies for this page
  }
  applyTheme(choice);
  for (const l of [...listeners]) l();
}

export function nextTheme(choice: ThemeChoice): ThemeChoice {
  return choice === 'dark' ? 'light' : choice === 'light' ? 'system' : 'dark';
}

export function useTheme(): ThemeChoice {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}
