/** Command palette item model and matching (pure; unit tested). */

export interface PaletteItem {
  id: string;
  group: string;
  label: string;
  /** Right-aligned hint, e.g. a shortcut or an entity state. */
  hint?: string;
  /** Extra matched text. */
  keywords?: string;
  danger?: boolean;
  run: () => void;
}

/**
 * Every whitespace-separated query token must occur in the label or keywords (case-insensitive).
 * Ranking: label prefix match, then word-start match, then substring; original order breaks ties.
 */
export function filterPalette(items: readonly PaletteItem[], query: string, limit = 60): PaletteItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return items.slice(0, limit);
  const tokens = q.split(/\s+/);
  const scored: { item: PaletteItem; score: number; index: number }[] = [];
  items.forEach((item, index) => {
    const label = item.label.toLowerCase();
    const hay = `${label} ${(item.keywords ?? '').toLowerCase()} ${item.group.toLowerCase()}`;
    if (!tokens.every((t) => hay.includes(t))) return;
    let score = 3;
    if (label.startsWith(q)) score = 0;
    else if (new RegExp(`(^|[\\s:/._-])${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`).test(label)) score = 1;
    else if (label.includes(q)) score = 2;
    scored.push({ item, score, index });
  });
  scored.sort((a, b) => a.score - b.score || a.index - b.index);
  return scored.slice(0, limit).map((s) => s.item);
}
