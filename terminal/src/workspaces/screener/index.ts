import { registerFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const screenerWorkspace: WorkspaceDefinition = {
  id: 'screener',
  title: 'Equity screener',
  icon: 'search',
  shortcut: 'g x',
  order: 111,
  load: () => import('./ScreenerWorkspace').then((m) => m.ScreenerWorkspace),
  description: 'Point-in-time screens over SEC and ESEF fundamentals, saved screens and reproducible as-of runs',
  keywords: ['screener', 'screen', 'eqs', 'equity screening', 'filter', 'fundamentals', 'ratios', 'as of', 'point in time', 'backtest'],
};

// Command-line mnemonic (ui/functions.ts): EQS opens the screener.
registerFunction({ mnemonic: 'EQS', workspace: 'screener', needsSecurity: false, help: 'Equity screener with point-in-time as-of runs' });
