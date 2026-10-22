import type { WorkspaceDefinition } from '../types';
import { NEW_SEGMENT } from './model';
import { ResearchJobsWorkspace } from './ResearchJobsWorkspace';

export const researchJobsWorkspace: WorkspaceDefinition = {
  id: 'research-jobs',
  title: 'Research jobs',
  icon: 'agents',
  shortcut: 'g j',
  order: 85,
  component: ResearchJobsWorkspace,
  description: 'Questions for the bounded research agents: queue, spend caps, advisory proposals, audit trail',
  keywords: ['agent', 'model', 'question', 'proposal', 'spend', 'cap', 'audit', 'llm', 'worker', 'cancel'],
  commands: [{ id: 'ask', label: 'Ask a research question', path: [NEW_SEGMENT], keywords: 'new job agent model' }],
};
