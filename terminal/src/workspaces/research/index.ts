import type { WorkspaceDefinition } from '../types';

export const researchWorkspace: WorkspaceDefinition = {
  id: 'research',
  title: 'Research',
  icon: 'research',
  shortcut: 'g q',
  order: 130,
  load: () => import('./ResearchWorkspace').then((m) => m.ResearchWorkspace),
  description: 'Experiments, trials with manifests and returns, strategy specs, artifacts with gate verdicts, reports, formulas',
  keywords: ['experiment', 'trial', 'backtest', 'pre-registration', 'holdout', 'manifest', 'artifact', 'promotion', 'evidence', 'gate', 'report', 'formula', 'strategy', 'fixture'],
  commands: [
    { id: 'artifacts', label: 'Strategy artifacts and gate verdicts', path: ['artifacts'], keywords: 'promotion evidence status' },
    { id: 'reports', label: 'Research reports', path: ['reports'], keywords: 'study report' },
    { id: 'formulas', label: 'Formula registry', path: ['formulas'], keywords: 'formula id version' },
  ],
};
