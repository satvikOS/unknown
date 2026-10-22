import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { countBy, normalizeRegister } from './capabilities/normalize';
import { findWorkspace, hasWorkspace, WORKSPACES } from './registry';
import { validateRegistry, type WorkspaceDefinition } from './types';

const Dummy = () => null;
const def = (id: string, shortcut: string, order?: number): WorkspaceDefinition => ({ id, title: id, shortcut, load: () => Promise.resolve(Dummy), order });
const HERE = dirname(fileURLToPath(import.meta.url));

describe('workspace registry', () => {
  it('registers overview and capabilities with unique shortcuts', () => {
    // Other workspaces register alongside these (each checks its own entry in its own tests).
    expect(WORKSPACES.map((w) => w.id)).toEqual(expect.arrayContaining(['overview', 'capabilities']));
    expect(WORKSPACES[0]?.id).toBe('overview');
    expect(new Set(WORKSPACES.map((w) => w.shortcut)).size).toBe(WORKSPACES.length);
    expect(hasWorkspace('overview')).toBe(true);
    expect(hasWorkspace('no-such-workspace')).toBe(false);
    expect(findWorkspace('capabilities')?.shortcut).toBe('g c');
  });

  it('rejects duplicate ids, duplicate shortcuts and malformed entries', () => {
    expect(() => validateRegistry([def('a', 'g a'), def('a', 'g b')])).toThrow(/duplicate workspace id/);
    expect(() => validateRegistry([def('a', 'g a'), def('b', 'g a')])).toThrow(/already used/);
    expect(() => validateRegistry([def('Bad Id', 'g a')])).toThrow(/must match/);
    expect(() => validateRegistry([def('a', 'ctrl+a')])).toThrow(/must look like/);
  });

  it('rejects duplicate palette command ids within a workspace', () => {
    const cmd = { id: 'x', label: 'X', path: [] };
    expect(() => validateRegistry([{ ...def('a', 'g a'), commands: [cmd, cmd] }])).toThrow(/duplicate palette command id x/);
    expect(validateRegistry([{ ...def('a', 'g a'), commands: [cmd] }, { ...def('b', 'g b'), commands: [cmd] }])).toHaveLength(2);
  });

  it('every registered palette command and workspace key is well formed', () => {
    for (const w of WORKSPACES) {
      for (const c of w.commands ?? []) {
        expect(c.label.trim(), `${w.id}:${c.id}`).not.toBe('');
        expect(c.path.every((seg) => seg !== '' && !seg.includes('/')), `${w.id}:${c.id}`).toBe(true);
      }
      for (const k of w.keys ?? []) {
        // Workspace keys are single characters and never collide with the "g" sequence prefix or "?".
        expect(k.keys, w.id).toMatch(/^[a-z0-9]$/);
        expect(['g', '?']).not.toContain(k.keys);
      }
    }
  });

  it('rejects an entry without a loader', () => {
    const bad = { id: 'a', title: 'a', shortcut: 'g a' } as unknown as WorkspaceDefinition;
    expect(() => validateRegistry([bad])).toThrow(/load must be a function/);
  });

  it('keeps every workspace component out of the registry: index.ts files import no component module statically', () => {
    // The registry is in the initial bundle; a static import of a component (a .tsx module) would pull that whole
    // workspace back into it. Components arrive through `load` (a dynamic import), one chunk per workspace.
    const dirs = readdirSync(HERE, { withFileTypes: true }).filter((d) => d.isDirectory() && existsSync(join(HERE, d.name, 'index.ts')));
    expect(dirs.length).toBeGreaterThanOrEqual(WORKSPACES.length);
    for (const d of dirs) {
      const file = join(HERE, d.name, 'index.ts');
      const source = readFileSync(file, 'utf8');
      for (const m of source.matchAll(/^import\s+(type\s+)?[^;]*?from\s+'([^']+)';/gms)) {
        if (m[1]) continue; // type-only imports are erased
        const target = resolve(dirname(file), m[2] ?? '');
        expect(existsSync(`${target}.tsx`), `${d.name}/index.ts imports ${m[2]} statically`).toBe(false);
      }
      expect(source, `${d.name}/index.ts`).toMatch(/load: \(\) => import\('\.\/\w+'\)/);
    }
  });

  it('every registered workspace loads a component', async () => {
    for (const w of WORKSPACES) {
      const component = await w.load();
      expect(typeof component, w.id).toBe('function');
    }
  });

  it('orders by `order`, then title', () => {
    expect(validateRegistry([def('z', 'g z', 5), def('b', 'g b'), def('a', 'g a')]).map((d) => d.id)).toEqual(['z', 'a', 'b']);
  });
});

describe('capability register normalization', () => {
  it('reports the unpublished register with the API note', () => {
    const r = normalizeRegister({ capabilities: [], note: 'capability register not yet published' });
    expect(r.published).toBe(false);
    expect(r.note).toBe('capability register not yet published');
    expect(r.rows).toEqual([]);
  });

  it('reads register schema_version 1 (implementation.status, sources, areas)', () => {
    const reg = normalizeRegister({
      schema_version: 1,
      description: 'd',
      status_vocabulary: ['proposed', 'specified', 'implemented'],
      areas: { instruments: { verification: { verified_on: '2026-10-22', urls_checked: 49, urls_supported: 41, urls_supported_after_correction: 49 } } },
      capabilities: [
        {
          id: 'CAP-INS-01',
          area: 'instruments',
          title: 'Security search',
          priority: 'P1',
          reference_scope: [{ product: 'Vendor A', url: 'https://example.org/a' }, { product: 'Vendor A' }, { product: 'Vendor B' }],
          source_verification: { verified_on: '2026-10-22' },
          implementation: { status: 'proposed', evidence: [], gaps: ['not yet implemented'], requirements: [] },
        },
        'bare entry',
      ],
    });
    expect(reg).toMatchObject({ published: true, schemaVersion: 1, description: 'd', statusVocabulary: ['proposed', 'specified', 'implemented'] });
    expect(reg.areas).toEqual([{ area: 'instruments', verifiedOn: '2026-10-22', urlsChecked: 49, urlsSupported: 49 }]);
    expect(reg.rows[0]).toMatchObject({
      id: 'CAP-INS-01',
      title: 'Security search',
      area: 'instruments',
      priority: 'P1',
      status: 'proposed',
      verifiedOn: '2026-10-22',
      products: ['Vendor A', 'Vendor B'],
      gaps: ['not yet implemented'],
    });
    expect(reg.rows[1]).toMatchObject({ id: '2', title: 'bare entry' });
    expect(countBy(reg.rows, (r) => r.status)).toEqual([
      ['proposed', 1],
      ['unstated', 1],
    ]);
  });

  it('falls back to common field names for other shapes', () => {
    expect(normalizeRegister([{ capability_id: 'C1', name: 't', status: 'tested' }]).rows[0]).toMatchObject({ id: 'C1', title: 't', status: 'tested' });
  });
});
