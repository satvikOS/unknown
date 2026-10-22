/**
 * Portfolio (book Ch. 6, 9; CAP-PORT-01..13; backend api/routes/portfolio.py). Read-only analytics:
 * holdings by owner, P&L and returns, exposure and VaR with its backtest, stress scenarios, the
 * what-if sandbox and allocation proposals, plus the valuation snapshots they read.
 * Route: `#/portfolio/<tab>`. The scope (account, system or one owner) is shared across tabs.
 */
import { useState } from 'react';
import { useQuery } from '../../state/react';
import { RouteTabs } from '../operations/parts';
import { usePluginQuery } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import { AllocationTab } from './AllocationTab';
import { HoldingsTab } from './HoldingsTab';
import { ofKind, parsePortfolioRoute, PORTFOLIO_TABS, recs, scopeOptions, str, type PortfolioTab } from './model';
import { useFormulas } from './parts';
import { PerformanceTab } from './PerformanceTab';
import { RiskTab } from './RiskTab';
import { SnapshotsTab } from './SnapshotsTab';
import { StressTab } from './StressTab';
import { WhatIfTab } from './WhatIfTab';
import './pf.css';

/** Marks move without core events, so holdings poll like the risk view's quotes. */
export const HOLDINGS_POLL_MS = 5000;

export function PortfolioWorkspace({ route, navigate }: WorkspaceProps) {
  const { tab } = parsePortfolioRoute(route.rest);
  const [scope, setScope] = useState('account');
  const [basis, setBasis] = useState<'bid' | 'mid' | 'last'>('bid');
  const sessions = useQuery('/api/sessions');
  const formulas = useFormulas();
  const holdingsQ = usePluginQuery<unknown>(tab === 'holdings' ? '/api/portfolio/holdings' : null, { scope, basis }, { pollMs: HOLDINGS_POLL_MS });
  const holdings = ofKind(holdingsQ.data, 'holdings');
  const owners = holdings ? recs(holdings.owners).map((o) => str(o.owner)).filter((o): o is string => o !== null) : [];
  const options = scopeOptions(owners, (sessions.data ?? []).map((s) => s.session_id));
  const bar = <RouteTabs<PortfolioTab> idBase="pf" label="Portfolio sections" tabs={PORTFOLIO_TABS} current={tab} onSelect={(t) => navigate('portfolio', t)} />;
  const common = { bar, scope, setScope, options, formulas };

  switch (tab) {
    case 'performance':
      return <PerformanceTab {...common} />;
    case 'risk':
      return <RiskTab {...common} />;
    case 'stress':
      return <StressTab {...common} />;
    case 'whatif':
      return <WhatIfTab bar={bar} formulas={formulas} />;
    case 'allocation':
      return <AllocationTab {...common} />;
    case 'snapshots':
      return <SnapshotsTab bar={bar} />;
    default:
      return <HoldingsTab q={holdingsQ} basis={basis} setBasis={setBasis} {...common} />;
  }
}
