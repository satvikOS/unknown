import { describe, expect, it } from 'vitest';
import { findWorkspace } from '../registry';

describe('market data registration', () => {
  it('registers Market data (g d, 55)', () => {
    expect(findWorkspace('marketdata')).toMatchObject({ title: 'Market data', shortcut: 'g d', order: 55, icon: 'markets' });
  });
});
