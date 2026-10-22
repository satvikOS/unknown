import { describe, expect, it } from 'vitest';
import { findWorkspace } from '../registry';

describe('research jobs registration', () => {
  it('registers Research jobs (g j, 85) with the ask command', () => {
    const w = findWorkspace('research-jobs');
    expect(w).toMatchObject({ title: 'Research jobs', shortcut: 'g j', order: 85, icon: 'agents' });
    expect(w?.commands?.map((c) => c.path)).toEqual([['~new']]);
  });
});
