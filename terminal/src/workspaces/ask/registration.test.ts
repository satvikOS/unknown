import { describe, expect, it } from 'vitest';
import { listFunctions, parseCommand } from '../../ui/functions';
import { WORKSPACES, findWorkspace } from '../registry';

describe('ask registration', () => {
  it('registers Ask AI (g k, 86) as a lazily loaded workspace', () => {
    const w = findWorkspace('ask');
    expect(w).toMatchObject({ title: 'Ask AI', shortcut: 'g k', order: 86, icon: 'agents' });
    expect(typeof w?.load).toBe('function');
  });

  it('opens with ASK and with <SECURITY> ASK on the command line', () => {
    const fns = listFunctions(WORKSPACES);
    const plain = parseCommand('ASK', fns);
    expect(plain).toMatchObject({ kind: 'function', fn: { mnemonic: 'ASK', workspace: 'ask' }, security: null });
    const withSecurity = parseCommand('AAPL.XNAS ASK <GO>', fns);
    expect(withSecurity).toMatchObject({ kind: 'function', fn: { workspace: 'ask' }, security: 'AAPL.XNAS' });
  });
});
