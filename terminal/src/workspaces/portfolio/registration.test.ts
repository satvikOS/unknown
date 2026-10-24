import { describe, expect, it } from 'vitest';
import { findWorkspace } from '../registry';

describe('portfolio registration', () => {
  it('registers Portfolio (g f, 45) with a palette entry per tab after holdings', () => {
    const w = findWorkspace('portfolio');
    expect(w).toMatchObject({ title: 'Portfolio', shortcut: 'g f', order: 45 });
    expect(w?.commands?.map((c) => c.path[0])).toEqual(['performance', 'risk', 'market', 'stress', 'whatif', 'allocation', 'snapshots']);
  });
});
