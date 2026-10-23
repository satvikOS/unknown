import type { WorkspaceDefinition } from '../types';

export const auditWorkspace: WorkspaceDefinition = {
  id: 'audit',
  title: 'Audit',
  icon: 'audit',
  shortcut: 'g a',
  order: 70,
  load: () => import('./AuditWorkspace').then((m) => m.AuditWorkspace),
  description: 'Hash-chained event log, double-entry journal and chain/ledger verification',
  keywords: ['events', 'log', 'journal', 'ledger', 'postings', 'verify', 'chain', 'hash'],
  commands: [
    { id: 'events', label: 'Event log', path: ['events'], keywords: 'tail seq' },
    { id: 'journal', label: 'Journal entries and postings', path: ['journal'], keywords: 'ledger double entry' },
    { id: 'verify', label: 'Verify the event chain and ledger', path: ['verify'], keywords: 'hash integrity check' },
  ],
};
