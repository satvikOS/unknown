/** Keyboard helpers (pure parts unit tested). */

/** True when a key event should be left to a text field rather than treated as a shortcut. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (target as HTMLInputElement).type;
    return !['button', 'checkbox', 'radio', 'submit', 'reset'].includes(type);
  }
  return false;
}

/**
 * Two-key sequences such as "g o": feed keys in order; returns the completed sequence or null.
 * A prefix expires after `timeoutMs`.
 */
export class SequenceMatcher {
  private pending: string | null = null;
  private pendingAt = 0;
  private readonly prefixes: Set<string>;

  constructor(
    private readonly sequences: readonly string[],
    private readonly timeoutMs = 1500,
  ) {
    this.prefixes = new Set(sequences.map((s) => s.split(' ')[0] ?? ''));
  }

  /** Returns the matched sequence, or null (the key may have started a prefix). */
  feed(key: string, nowMs: number): string | null {
    const k = key.toLowerCase();
    if (this.pending !== null && nowMs - this.pendingAt <= this.timeoutMs) {
      const candidate = `${this.pending} ${k}`;
      this.pending = null;
      if (this.sequences.includes(candidate)) return candidate;
    }
    this.pending = null;
    if (this.prefixes.has(k)) {
      this.pending = k;
      this.pendingAt = nowMs;
    }
    return null;
  }

  get awaiting(): string | null {
    return this.pending;
  }

  reset(): void {
    this.pending = null;
  }
}

export const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const MOD_LABEL = IS_MAC ? '⌘' : 'Ctrl';
