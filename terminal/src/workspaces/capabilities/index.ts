import type { WorkspaceDefinition } from '../types';
import { CapabilitiesWorkspace } from './CapabilitiesWorkspace';

export const capabilitiesWorkspace: WorkspaceDefinition = {
  id: 'capabilities',
  title: 'Capabilities',
  icon: 'capabilities',
  shortcut: 'g c',
  order: 900,
  component: CapabilitiesWorkspace,
  description: 'The capability register: what is built, specified or blocked, with sources',
  keywords: ['register', 'parity', 'roadmap', 'status', 'unimplemented'],
};
