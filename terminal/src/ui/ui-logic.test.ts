import { describe, expect, it, vi } from 'vitest';
import { SequenceMatcher } from './keys';
import { filterPalette, type PaletteItem } from './palette';
import { compareValues, sortRows, visibleRange } from './tableSort';

describe('table sorting', () => {
  it('sorts decimal strings exactly and keeps empty values last in both directions', () => {
    const rows = [{ v: '10.5' }, { v: null }, { v: '9.75' }, { v: '-2' }, { v: '100' }, { v: '10.50' }];
    const asc = sortRows(rows, (r) => r.v, 'decimal', 'ascending').map((r) => r.v);
    expect(asc).toEqual(['-2', '9.75', '10.5', '10.50', '100', null]);
    const desc = sortRows(rows, (r) => r.v, 'decimal', 'descending').map((r) => r.v);
    expect(desc).toEqual(['100', '10.5', '10.50', '9.75', '-2', null]);
  });

  it('beats float comparison on large values', () => {
    expect(compareValues('9007199254740993', '9007199254740992', 'decimal')).toBe(1);
  });

  it('sorts text naturally and is stable', () => {
    const rows = [{ k: 'ses-10', i: 0 }, { k: 'ses-2', i: 1 }, { k: 'ses-2', i: 2 }];
    expect(sortRows(rows, (r) => r.k, 'text', 'ascending').map((r) => r.i)).toEqual([1, 2, 0]);
  });

  it('computes the virtualization window', () => {
    expect(visibleRange(10_000, 0, 560, 28, 8)).toEqual({ start: 0, end: 28 });
    expect(visibleRange(10_000, 2800, 560, 28, 8)).toEqual({ start: 92, end: 128 });
    expect(visibleRange(50, 10_000, 560, 28, 8)).toEqual({ start: 41, end: 50 }); // scrolled past the end: clamp
    expect(visibleRange(0, 0, 560, 28)).toEqual({ start: 0, end: 0 });
  });
});

describe('palette filtering', () => {
  const item = (id: string, label: string, group = 'G', keywords?: string): PaletteItem => ({ id, label, group, keywords, run: vi.fn() });
  const items = [
    item('a', 'Overview', 'Workspaces', 'home account'),
    item('b', 'STOP: block new entries', 'Control', 'halt emergency'),
    item('c', 'Show stop progress', 'Control'),
    item('d', 'Capabilities', 'Workspaces'),
  ];
  it('requires every token and ranks prefix matches first', () => {
    expect(filterPalette(items, 'stop').map((i) => i.id)).toEqual(['b', 'c']);
    expect(filterPalette(items, 'halt').map((i) => i.id)).toEqual(['b']);
    expect(filterPalette(items, 'stop prog').map((i) => i.id)).toEqual(['c']);
    expect(filterPalette(items, '').map((i) => i.id)).toEqual(['a', 'b', 'c', 'd']);
    expect(filterPalette(items, 'zzz')).toEqual([]);
  });
});

describe('two-key shortcuts', () => {
  it('matches "g o" within the timeout only', () => {
    const m = new SequenceMatcher(['g o', 'g c'], 1000);
    expect(m.feed('g', 0)).toBeNull();
    expect(m.feed('o', 500)).toBe('g o');
    expect(m.feed('g', 1000)).toBeNull();
    expect(m.feed('c', 2500)).toBeNull();
    expect(m.feed('x', 2600)).toBeNull();
    expect(m.feed('G', 3000)).toBeNull();
    expect(m.feed('C', 3100)).toBe('g c');
  });
});
