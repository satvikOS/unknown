import { describe, expect, it } from 'vitest';
import { charKeysEnabled, setCharKeys } from './charKeys';

describe('single-character shortcut preference (WCAG 2.1.4)', () => {
  it('is on by default and can be turned off and on without storage', () => {
    expect(charKeysEnabled()).toBe(true);
    setCharKeys(false);
    expect(charKeysEnabled()).toBe(false);
    setCharKeys(true);
    expect(charKeysEnabled()).toBe(true);
  });
});
