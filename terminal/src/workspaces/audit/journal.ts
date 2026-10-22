/**
 * Journal paging over GET /api/ledger/entries (newest event_seq first; `before` is exclusive).
 *
 * One event can write several journal entries with the same event_seq, so a page that ends inside such
 * a group must not continue with before=<that seq> (it would skip the rest of the group). The next
 * cursor is seq+1 and the overlap is de-duplicated by entry_id; if a page made no progress at all (a
 * single group larger than the page), the cursor falls back to the group's seq and the possible skip
 * is reported instead of looping forever.
 */
import type { JournalEntry } from '../../api/types';
import { isZeroDecimal, totalsByCurrency } from './exact';

export interface JournalPageState {
  entries: readonly JournalEntry[];
  /** `before` for the next page, or null when the oldest entry has been loaded. */
  cursor: number | null;
  /** True when a group larger than the page size forced a cursor that may skip entries. */
  possiblySkipped: boolean;
}

export const EMPTY_JOURNAL: JournalPageState = { entries: [], cursor: null, possiblySkipped: false };

/** Merge one page (requested with `requestedBefore`, `limit`) into the state. */
export function mergeJournalPage(
  state: JournalPageState,
  page: readonly JournalEntry[],
  limit: number,
  requestedBefore: number | null,
): JournalPageState {
  const seen = new Set(state.entries.map((e) => e.entry_id));
  const fresh = page.filter((e) => !seen.has(e.entry_id));
  const entries = [...state.entries, ...fresh];
  if (page.length < limit) return { entries, cursor: null, possiblySkipped: state.possiblySkipped };
  const lastSeq = page[page.length - 1]!.event_seq;
  let cursor = lastSeq + 1;
  let possiblySkipped = state.possiblySkipped;
  if (fresh.length === 0 || (requestedBefore !== null && cursor >= requestedBefore)) {
    // No progress possible with seq+1: move past the group and say so.
    cursor = lastSeq;
    possiblySkipped = true;
  }
  return { entries, cursor, possiblySkipped };
}

export interface EntryBalance {
  balanced: boolean;
  totals: [string, string][];
}

/** Per-currency totals of an entry's postings (exact); an entry must balance to zero in each currency. */
export function entryBalance(e: Pick<JournalEntry, 'lines'>): EntryBalance {
  const totals = totalsByCurrency(e.lines);
  return { balanced: totals.every(([, t]) => isZeroDecimal(t)), totals };
}

/** Owner kinds (backend core/ledger.py): who a posting belongs to. */
export function ownerKind(owner: string): { kind: 'outside' | 'unallocated' | 'buffer' | 'external' | 'session' | 'protected' | 'exception' | 'other'; ref: string | null } {
  if (owner === 'outside' || owner === 'unallocated' || owner === 'buffer' || owner === 'external') return { kind: owner, ref: null };
  const i = owner.indexOf(':');
  if (i > 0) {
    const head = owner.slice(0, i);
    if (head === 'session' || head === 'protected' || head === 'exception') return { kind: head, ref: owner.slice(i + 1) };
  }
  return { kind: 'other', ref: null };
}

/** Owners mentioned by loaded entries plus well-known ones, for the filter suggestions. */
export function ownerSuggestions(entries: readonly JournalEntry[], known: readonly string[] = []): string[] {
  const set = new Set<string>(['outside', 'unallocated', 'buffer', 'external', ...known]);
  for (const e of entries) for (const l of e.lines) set.add(l.owner);
  return [...set].sort();
}

/**
 * New entries at the head (a fresh newest-first page). Returns null when the head page is full and
 * shares nothing with what is loaded: entries may lie between, so the caller reloads from the top.
 */
export function mergeJournalHead(state: JournalPageState, page: readonly JournalEntry[], limit: number): JournalPageState | null {
  if (state.entries.length === 0) return mergeJournalPage(EMPTY_JOURNAL, page, limit, null);
  const seen = new Set(state.entries.map((e) => e.entry_id));
  const fresh = page.filter((e) => !seen.has(e.entry_id));
  if (fresh.length === 0) return state;
  if (fresh.length === page.length && page.length >= limit) return null;
  return { ...state, entries: [...fresh, ...state.entries] };
}
