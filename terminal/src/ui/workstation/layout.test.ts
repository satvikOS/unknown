import { describe, expect, it } from 'vitest';
import { closePanel, panelsOf, resizeSplit, setActive, setLocked, splitPanel, TEMPLATES, templateLayout, updatePanel, validateLayout, type LayoutDoc } from './layout';
import { CORRUPT_KEY, deleteLayout, loadStore, openLayout, persistStore, saveLayout, setDraft, STORE_KEY, type KeyValueStorage } from './storage';

const single = (): LayoutDoc => templateLayout('single');

function memory(initial: Record<string, string> = {}): KeyValueStorage & { data: Record<string, string> } {
  const data = { ...initial };
  return {
    data,
    getItem: (k) => data[k] ?? null,
    setItem: (k, v) => void (data[k] = v),
    removeItem: (k) => void delete data[k],
  };
}

describe('workstation layout', () => {
  it('splits a panel, inserts siblings in the same direction and activates the new panel on the same channel', () => {
    let d = splitPanel(single(), 'p1', 'row');
    expect(d.root).toMatchObject({ kind: 'split', dir: 'row', sizes: [50, 50] });
    expect(d.active).toBe('p2');
    d = splitPanel(d, 'p2', 'row');
    expect(d.root).toMatchObject({ kind: 'split', sizes: [50, 25, 25] });
    d = splitPanel(d, 'p3', 'column');
    expect(panelsOf(d.root).map((p) => [p.id, p.channel])).toEqual([
      ['p1', 'default'],
      ['p2', 'default'],
      ['p3', 'default'],
      ['p4', 'default'],
    ]);
  });

  it('closes panels, collapses one-child splits, renormalises sizes and never closes the last panel', () => {
    let d = splitPanel(splitPanel(single(), 'p1', 'row'), 'p2', 'column');
    d = closePanel(d, 'p3');
    expect(d.root).toMatchObject({ kind: 'split', dir: 'row', children: [{ id: 'p1' }, { id: 'p2' }] });
    d = closePanel(d, 'p2');
    expect(d.root).toMatchObject({ kind: 'panel', id: 'p1' });
    expect(d.active).toBe('p1');
    expect(closePanel(d, 'p1')).toBe(d);
  });

  it('resizes a divider within 10% bounds and keeps the sum at 100', () => {
    let d = splitPanel(single(), 'p1', 'row');
    d = resizeSplit(d, 's1', 0, 25);
    expect(d.root).toMatchObject({ sizes: [75, 25] });
    d = resizeSplit(d, 's1', 0, 50);
    expect(d.root).toMatchObject({ sizes: [90, 10] });
    expect(resizeSplit(d, 's1', 1, 5)).toEqual(d);
  });

  it('lock mode refuses every structural change but still lets the owner pick the active panel', () => {
    const d = setLocked(splitPanel(single(), 'p1', 'row'), true);
    expect(splitPanel(d, 'p1', 'column')).toBe(d);
    expect(closePanel(d, 'p2')).toBe(d);
    expect(resizeSplit(d, 's1', 0, 10)).toBe(d);
    expect(updatePanel(d, 'p1', { workspace: 'risk' })).toBe(d);
    expect(setActive(d, 'p1').active).toBe('p1');
  });

  it('changing the workspace clears the sub-route; unlinked channels are per panel', () => {
    let d = updatePanel(single(), 'p1', { workspace: 'orders', rest: ['ord-1'] });
    d = updatePanel(d, 'p1', { workspace: 'risk' });
    expect(panelsOf(d.root)[0]).toMatchObject({ workspace: 'risk', rest: [] });
    d = splitPanel(updatePanel(d, 'p1', { channel: 'solo:p1' }), 'p1', 'row');
    expect(panelsOf(d.root)[1]?.channel).toBe('solo:p2');
  });

  it('validates untrusted documents: every template passes, malformed ones are rejected', () => {
    for (const t of TEMPLATES) expect(validateLayout(JSON.parse(JSON.stringify(t.build())))).toEqual(t.build());
    const good = JSON.parse(JSON.stringify(templateLayout('desk')));
    expect(validateLayout({ ...good, schema: 2 })).toBeNull();
    expect(validateLayout({ ...good, root: { ...good.root, sizes: [70, 70] } })).toBeNull();
    expect(validateLayout({ ...good, root: { kind: 'panel', id: 'p1', workspace: '../evil', rest: [], channel: 'default' } })).toBeNull();
    expect(validateLayout({ ...good, root: { kind: 'panel', id: 'p1', workspace: null, rest: [], channel: 'solo:p9' } })).toBeNull();
    expect(validateLayout({ ...good, active: 'p99' })?.active).toBe('p1');
  });
});

describe('layout persistence', () => {
  it('round-trips the draft and named versions, and restores an older version', () => {
    const s = memory();
    let store = loadStore(s, single).store;
    store = saveLayout(store, 'Morning', 1000);
    store = setDraft(store, splitPanel(store.draft, 'p1', 'row'));
    store = saveLayout(store, 'Morning', 2000);
    expect(persistStore(s, store)).toBeNull();
    const back = loadStore(s, single);
    expect(back.recovered).toBeNull();
    expect(back.store).toEqual(store);
    expect(back.store.layouts.Morning).toMatchObject({ rev: 2, history: [{ rev: 1 }] });
    const restored = openLayout(back.store, 'Morning', 1);
    expect(panelsOf(restored.draft.root)).toHaveLength(1);
    expect(deleteLayout(restored, 'Morning').layouts).toEqual({});
  });

  it('recovers from a corrupt store: opens the template, says so and sets the bad text aside', () => {
    const s = memory({ [STORE_KEY]: '{"schema":1,"draft":' });
    const r = loadStore(s, single);
    expect(r.recovered).toMatch(/could not be read.*set aside/);
    expect(r.store.draft).toEqual(single());
    expect(s.data[CORRUPT_KEY]).toBe('{"schema":1,"draft":');
    expect(loadStore(memory({ [STORE_KEY]: '{"schema":7}' }), single).recovered).toMatch(/schema 7/);
  });

  it('skips one damaged saved layout and keeps the rest', () => {
    const s = memory();
    const store = saveLayout(loadStore(s, single).store, 'Good', 1);
    const raw = JSON.parse(JSON.stringify(store));
    raw.layouts.Bad = { rev: 1, savedAtMs: 1, doc: { schema: 1, name: 'Bad', root: { kind: 'nope' } }, history: [] };
    s.setItem(STORE_KEY, JSON.stringify(raw));
    const r = loadStore(s, single);
    expect(Object.keys(r.store.layouts)).toEqual(['Good']);
    expect(r.recovered).toMatch(/Bad/);
  });

  it('never throws when storage is unavailable', () => {
    const denied = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => undefined,
    };
    expect(loadStore(denied, single).recovered).toMatch(/unavailable/);
    expect(persistStore(denied, loadStore(null, single).store)).toMatch(/could not be saved/);
    expect(persistStore(null, loadStore(null, single).store)).toMatch(/unavailable/);
  });
});
