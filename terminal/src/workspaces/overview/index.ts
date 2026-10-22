import type { WorkspaceDefinition } from '../types';

export const overviewWorkspace: WorkspaceDefinition = {
  id: 'overview',
  title: 'Overview',
  icon: 'overview',
  shortcut: 'g o',
  order: 10,
  load: () => import('./OverviewWorkspace').then((m) => m.OverviewWorkspace),
  description: 'Mode, control state, capital buckets, positions, sessions and exceptions',
  keywords: ['home', 'account', 'buckets', 'positions', 'capital', 'stop'],
};
