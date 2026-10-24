import { registerFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const ownershipWorkspace: WorkspaceDefinition = {
  id: 'ownership',
  title: 'Ownership',
  icon: 'instruments',
  shortcut: 'g u',
  order: 112,
  load: () => import('./OwnershipWorkspace').then((m) => m.OwnershipWorkspace),
  description: 'Institutional holders (13F), insider transactions (Forms 3/4/5) and 5 % holders (13D/13G) from SEC EDGAR',
  keywords: ['ownership', 'holders', 'institutional', '13f', 'insider', 'form 4', '13d', '13g', 'beneficial owner', 'concentration', 'own'],
};

// Command-line mnemonic (ui/functions.ts): OWN opens the ownership of the security typed before it or linked.
registerFunction({ mnemonic: 'OWN', workspace: 'ownership', needsSecurity: true, help: 'Ownership: institutional, insider and 5 % holders' });
