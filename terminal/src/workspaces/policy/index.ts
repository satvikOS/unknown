import type { WorkspaceDefinition } from '../types';
import { PolicyWorkspace } from './PolicyWorkspace';

export const policyWorkspace: WorkspaceDefinition = {
  id: 'policy',
  title: 'Policy',
  icon: 'policy',
  shortcut: 'g y',
  order: 80,
  component: PolicyWorkspace,
  description: 'Published policy versions, field-by-field comparison, new versions and activation',
  keywords: ['limits', 'publish', 'activate', 'version', 'diff', 'compare', 'mandate', 'template', 'edit'],
  commands: [
    { id: 'draft', label: 'New policy version (draft from the active policy)', path: ['draft'], keywords: 'publish edit tighten limits' },
    { id: 'compare', label: 'Compare two policies', path: ['compare'], keywords: 'diff' },
  ],
};
