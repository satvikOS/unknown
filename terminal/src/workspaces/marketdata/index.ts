import type { WorkspaceDefinition } from '../types';

export const marketDataWorkspace: WorkspaceDefinition = {
  id: 'marketdata',
  title: 'Market data',
  icon: 'markets',
  shortcut: 'g d',
  order: 55,
  load: () => import('./MarketDataWorkspace').then((m) => m.MarketDataWorkspace),
  description: 'IBKR market-data session: subscriptions, delivered type, consolidation, entitlement errors, lines',
  keywords: ['ibkr', 'quotes', 'feed', 'entitlement', 'delayed', 'live', 'consolidated', 'subscription', 'lines', 'pacing'],
};
