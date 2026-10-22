import type { WorkspaceDefinition } from '../types';
import { RiskWorkspace } from './RiskWorkspace';

export const riskWorkspace: WorkspaceDefinition = {
  id: 'risk',
  title: 'Risk',
  icon: 'risk',
  shortcut: 'g r',
  order: 40,
  component: RiskWorkspace,
  description: 'Loss budgets, exposure, active limits, positions with marks, cash and availability',
  keywords: ['loss', 'budget', 'limits', 'exposure', 'drawdown', 'high-water', 'positions', 'cash', 'buckets', 'available', 'concentration'],
};
