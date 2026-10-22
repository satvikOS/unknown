import { describe, expect, it } from 'vitest';
import { findWorkspace } from '../registry';

describe('risk and policy registration', () => {
  it('registers Risk (g r, 40) and Policy (g y, 80) with their reserved shortcuts', () => {
    expect(findWorkspace('risk')).toMatchObject({ title: 'Risk', shortcut: 'g r', order: 40, icon: 'risk' });
    expect(findWorkspace('policy')).toMatchObject({ title: 'Policy', shortcut: 'g y', order: 80, icon: 'policy' });
  });
});
