import type { WorkspaceDefinition } from '../types';

export const instrumentsWorkspace: WorkspaceDefinition = {
  id: 'instruments',
  title: 'Instruments',
  icon: 'instruments',
  shortcut: 'g i',
  order: 50,
  load: () => import('./InstrumentsWorkspace').then((m) => m.InstrumentsWorkspace),
  description: 'Instrument master: tick rules, multiplier, settlement cycle, provider mappings, latest quote',
  keywords: ['symbol', 'reference data', 'tick', 'mapping', 'conid', 'fixture', 'quote'],
};
