/**
 * Layout persistence in localStorage (a per-browser preference store; CAP-UX-04). The store keeps the working
 * draft (autosaved on every change) and named layouts; each explicit save creates a new immutable version and the
 * previous versions stay restorable (last 10). Every read and write is guarded: private mode, a full quota or a
 * corrupt document never break the terminal. A corrupt store is set aside under its own key for diagnosis and
 * the workstation opens a template, saying so.
 */
import { LAYOUT_SCHEMA, validateLayout, type LayoutDoc } from './layout';

export const STORE_KEY = 'unknown.terminal.workstation.v1';
export const CORRUPT_KEY = 'unknown.terminal.workstation.corrupt';
export const STORE_SCHEMA = 1;
export const HISTORY_CAP = 10;

export interface LayoutVersion {
  rev: number;
  savedAtMs: number;
  doc: LayoutDoc;
}

export interface SavedLayout extends LayoutVersion {
  /** Older versions, newest first. */
  history: LayoutVersion[];
}

export interface LayoutStore {
  schema: typeof STORE_SCHEMA;
  draft: LayoutDoc;
  layouts: Record<string, SavedLayout>;
}

export type KeyValueStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export function emptyStore(draft: LayoutDoc): LayoutStore {
  return { schema: STORE_SCHEMA, draft, layouts: {} };
}

function validVersion(x: unknown): LayoutVersion | null {
  if (!x || typeof x !== 'object') return null;
  const v = x as Record<string, unknown>;
  const doc = validateLayout(v.doc);
  if (!doc || typeof v.rev !== 'number' || !Number.isInteger(v.rev) || v.rev < 1 || typeof v.savedAtMs !== 'number') return null;
  return { rev: v.rev, savedAtMs: v.savedAtMs, doc };
}

export interface LoadResult {
  store: LayoutStore;
  /** Plain-language reason when anything had to be recovered; null when the store loaded intact. */
  recovered: string | null;
}

/** Read the store; anything unreadable is replaced by `fallback` and reported, never thrown. */
export function loadStore(storage: KeyValueStorage | null, fallback: () => LayoutDoc): LoadResult {
  let raw: string | null = null;
  try {
    raw = storage?.getItem(STORE_KEY) ?? null;
  } catch {
    return { store: emptyStore(fallback()), recovered: 'Browser storage is unavailable: layouts last for this page only.' };
  }
  if (raw === null) return { store: emptyStore(fallback()), recovered: null };
  const setAside = (why: string): LoadResult => {
    try {
      storage?.setItem(CORRUPT_KEY, raw ?? '');
    } catch {
      // nothing more to keep
    }
    return { store: emptyStore(fallback()), recovered: `${why} It was set aside and a template opened instead.` };
  };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return setAside('The saved layout could not be read (not valid JSON).');
  }
  if (!parsed || typeof parsed !== 'object') return setAside('The saved layout is not a layout store.');
  const p = parsed as Record<string, unknown>;
  if (p.schema !== STORE_SCHEMA) return setAside(`The saved layout uses schema ${JSON.stringify(p.schema ?? null)}, this terminal reads ${STORE_SCHEMA}.`);
  const dropped: string[] = [];
  const layouts: Record<string, SavedLayout> = {};
  const src = p.layouts && typeof p.layouts === 'object' ? (p.layouts as Record<string, unknown>) : {};
  for (const [name, entry] of Object.entries(src)) {
    const v = validVersion(entry);
    if (!v || v.doc.name !== name) {
      dropped.push(name);
      continue;
    }
    const hist = Array.isArray((entry as Record<string, unknown>).history) ? ((entry as Record<string, unknown>).history as unknown[]) : [];
    layouts[name] = { ...v, history: hist.map(validVersion).filter((h): h is LayoutVersion => h !== null).slice(0, HISTORY_CAP) };
  }
  const draft = validateLayout(p.draft);
  if (!draft) return setAside('The working layout is damaged.');
  const store: LayoutStore = { schema: STORE_SCHEMA, draft, layouts };
  return {
    store,
    recovered: dropped.length ? `Damaged saved layout${dropped.length === 1 ? '' : 's'} skipped: ${dropped.map((d) => d.slice(0, 40)).join(', ')}.` : null,
  };
}

/** Write the store; returns null or the reason it could not be written. */
export function persistStore(storage: KeyValueStorage | null, store: LayoutStore): string | null {
  if (!storage) return 'Browser storage is unavailable: layouts last for this page only.';
  try {
    storage.setItem(STORE_KEY, JSON.stringify(store));
    return null;
  } catch (e) {
    return `The layout could not be saved in this browser (${e instanceof Error ? e.name : 'storage error'}).`;
  }
}

/** Save the draft as a named layout: a new version; the previous one moves into history. */
export function saveLayout(store: LayoutStore, name: string, nowMs: number): LayoutStore {
  const doc: LayoutDoc = { ...store.draft, name, schema: LAYOUT_SCHEMA };
  const prev = store.layouts[name];
  const history = prev ? [{ rev: prev.rev, savedAtMs: prev.savedAtMs, doc: prev.doc }, ...prev.history].slice(0, HISTORY_CAP) : [];
  return {
    ...store,
    draft: doc,
    layouts: { ...store.layouts, [name]: { rev: (prev?.rev ?? 0) + 1, savedAtMs: nowMs, doc, history } },
  };
}

/** Open a saved layout (or one of its versions) as the draft. */
export function openLayout(store: LayoutStore, name: string, rev?: number): LayoutStore {
  const entry = store.layouts[name];
  if (!entry) return store;
  const version = rev === undefined || rev === entry.rev ? entry : entry.history.find((h) => h.rev === rev);
  return version ? { ...store, draft: version.doc } : store;
}

export function deleteLayout(store: LayoutStore, name: string): LayoutStore {
  if (!store.layouts[name]) return store;
  const layouts = { ...store.layouts };
  delete layouts[name];
  return { ...store, layouts };
}

export function setDraft(store: LayoutStore, draft: LayoutDoc): LayoutStore {
  return store.draft === draft ? store : { ...store, draft };
}
