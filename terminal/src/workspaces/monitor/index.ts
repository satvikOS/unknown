import type { WorkspaceDefinition } from '../types';

export const monitorWorkspace: WorkspaceDefinition = {
  id: 'monitor',
  title: 'Monitor',
  icon: 'markets',
  shortcut: 'g w',
  order: 52,
  load: () => import('./MonitorWorkspace').then((m) => m.MonitorWorkspace),
  description: 'Live watchlists, time & sales and depth from any configured market-data source',
  keywords: ['watchlist', 'monitor', 'quotes', 'live', 'time and sales', 'tape', 'depth', 'level 2', 'ticker'],
};
