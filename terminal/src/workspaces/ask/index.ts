import { registerFunction, type TerminalFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const askWorkspace: WorkspaceDefinition = {
  id: 'ask',
  title: 'Ask AI',
  icon: 'agents',
  shortcut: 'g k',
  order: 86,
  load: () => import('./AskWorkspace').then((m) => m.AskWorkspace),
  description: 'AI analyst: questions answered from platform data with cited sources, tokens and cost (advisory only)',
  keywords: ['ai', 'ask', 'analyst', 'question', 'llm', 'claude', 'model', 'chat', 'summary', 'filings', 'citations'],
  commands: [{ id: 'new', label: 'Ask the AI analyst a question', path: [], keywords: 'ai chat question analyst' }],
};

// Command line (ui/functions.ts): ASK opens the analyst; `<SECURITY> ASK` links the security first, and the question is
// then asked about it.
export const ASK_FUNCTIONS: readonly TerminalFunction[] = [
  { mnemonic: 'ASK', workspace: 'ask', needsSecurity: false, help: 'Ask the AI analyst (cited, advisory); about the security when one is given' },
];

for (const fn of ASK_FUNCTIONS) registerFunction(fn);
