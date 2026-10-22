/** Portfolio tabs: shared by the registry entry (palette commands, in the initial bundle) and the workspace chunk. */
export const PORTFOLIO_TABS = [
  { id: 'holdings', label: 'Holdings' },
  { id: 'performance', label: 'Performance' },
  { id: 'risk', label: 'Exposure and VaR' },
  { id: 'stress', label: 'Stress' },
  { id: 'whatif', label: 'What-if' },
  { id: 'allocation', label: 'Allocation' },
  { id: 'snapshots', label: 'Snapshots' },
] as const;
export type PortfolioTab = (typeof PORTFOLIO_TABS)[number]['id'];
