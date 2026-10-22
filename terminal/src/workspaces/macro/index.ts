import type { WorkspaceDefinition } from '../types';
import { MacroWorkspace } from './MacroWorkspace';

export const macroWorkspace: WorkspaceDefinition = {
  id: 'macro',
  title: 'Markets & macro',
  icon: 'markets',
  shortcut: 'g m',
  order: 120,
  component: MacroWorkspace,
  description: 'US Treasury par yield curve by date and ECB euro reference rates, with availability times',
  keywords: ['markets', 'macro', 'treasury', 'yield curve', 'par yield', 'rates', 'ecb', 'fx', 'reference rates', 'currency'],
};
