import type { WorkspaceDefinition } from '../types';
import { OperationsWorkspace } from './OperationsWorkspace';

export const operationsWorkspace: WorkspaceDefinition = {
  id: 'operations',
  title: 'Operations',
  icon: 'operations',
  shortcut: 'g p',
  order: 60,
  component: OperationsWorkspace,
  description: 'Service health, control state, reconciliation runs, exceptions and fee estimates',
  keywords: ['health', 'reconciliation', 'exceptions', 'resolve', 'fees', 'ownership', 'broker', 'runbook'],
  commands: [
    { id: 'status', label: 'Health and control', path: ['status'], keywords: 'broker core preflight ownership' },
    { id: 'exceptions', label: 'Exceptions', path: ['exceptions'], keywords: 'cases resolve mismatch' },
    { id: 'reconciliation', label: 'Reconciliation runs', path: ['reconciliation'], keywords: 'differences watermark' },
    { id: 'fees', label: 'Fee estimates', path: ['fees'], keywords: 'accept estimate commission' },
  ],
};
