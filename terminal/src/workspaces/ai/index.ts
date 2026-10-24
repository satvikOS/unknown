import { registerFunction, type TerminalFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const aiWorkspace: WorkspaceDefinition = {
  id: 'ai',
  title: 'AI notes',
  icon: 'agents',
  shortcut: 'g k',
  order: 87,
  load: () => import('./AiWorkspace').then((m) => m.AiWorkspace),
  description: 'Advisory AI notes with citations (morning brief, filing summaries, move explainer), the approval queue and AI cost',
  keywords: ['ai', 'brief', 'morning brief', 'summary', 'explain', 'move', 'approval', 'queue', 'cost', 'spend', 'tokens', 'model'],
  commands: [
    { id: 'inbox', label: 'AI notes inbox', path: [] },
    { id: 'brief', label: 'Morning briefs', path: ['brief'] },
    {
      id: 'approvals',
      label: 'AI approval queue',
      path: ['approvals'],
      keywords: 'approve reject proposal',
    },
    {
      id: 'cost',
      label: 'AI cost and quality',
      path: ['cost'],
      keywords: 'spend budget tokens latency',
    },
  ],
};

// Command-line mnemonics (ui/functions.ts): BRIEF, AIQ (approval queue) and AICOST open this workspace's views.
export const AI_FUNCTIONS: readonly TerminalFunction[] = [
  {
    mnemonic: 'BRIEF',
    workspace: 'ai',
    needsSecurity: false,
    help: 'AI morning briefs (advisory, cited)',
    rest: ['brief'],
  },
  {
    mnemonic: 'AIQ',
    workspace: 'ai',
    needsSecurity: false,
    help: 'AI approval queue: approve or reject the exact proposal',
    rest: ['approvals'],
  },
  {
    mnemonic: 'AICOST',
    workspace: 'ai',
    needsSecurity: false,
    help: 'AI spend by day, role and process against the caps',
    rest: ['cost'],
  },
];

for (const fn of AI_FUNCTIONS) registerFunction(fn);
