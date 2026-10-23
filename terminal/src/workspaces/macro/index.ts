import { registerFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const macroWorkspace: WorkspaceDefinition = {
  id: 'macro',
  title: 'Markets & macro',
  icon: 'markets',
  shortcut: 'g m',
  order: 120,
  load: () => import('./MacroWorkspace').then((m) => m.MacroWorkspace),
  description: 'US Treasury par yield curve, ECB euro reference rates and cross-country GDP growth, CPI, unemployment and policy rates (World Bank, OECD, BIS, FRED)',
  keywords: ['markets', 'macro', 'treasury', 'yield curve', 'par yield', 'rates', 'ecb', 'fx', 'reference rates', 'currency', 'gdp', 'inflation', 'cpi', 'unemployment', 'policy rate', 'world bank', 'oecd', 'bis', 'fred', 'india', 'europe'],
};

// Command-line mnemonic for the cross-country comparison (ui/functions.ts).
registerFunction({ mnemonic: 'ECST', workspace: 'macro', needsSecurity: false, help: 'Cross-country macro: GDP growth, CPI, unemployment, policy rates' });
