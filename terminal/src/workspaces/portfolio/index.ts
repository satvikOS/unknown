import type { WorkspaceDefinition } from '../types';
import { PORTFOLIO_TABS } from './tabs';

export const portfolioWorkspace: WorkspaceDefinition = {
  id: 'portfolio',
  title: 'Portfolio',
  icon: 'generic',
  shortcut: 'g f',
  order: 45,
  load: () => import('./PortfolioWorkspace').then((m) => m.PortfolioWorkspace),
  description: 'Holdings by owner, P&L, TWR/IRR, exposure, VaR and backtest, stress, what-if, allocation proposals',
  keywords: ['holdings', 'nav', 'pnl', 'twr', 'irr', 'return', 'drawdown', 'exposure', 'var', 'stress', 'what-if', 'allocation', 'snapshots', 'sharpe'],
  commands: PORTFOLIO_TABS.filter((t) => t.id !== 'holdings').map((t) => ({ id: t.id, label: `Portfolio: ${t.label}`, path: [t.id] })),
};
