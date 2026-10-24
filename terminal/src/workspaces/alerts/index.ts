import { registerFunction, type TerminalFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const alertsWorkspace: WorkspaceDefinition = {
  id: 'alerts',
  title: 'Alerts',
  icon: 'markets',
  shortcut: 'g t',
  order: 116,
  load: () => import('./AlertsWorkspace').then((m) => m.AlertsWorkspace),
  description: 'Server-side alerts on price crosses, % moves, volume, time, chart studies, news and filings, with an inbox, snooze and audit; alerts never act on orders',
  keywords: ['alerts', 'alarm', 'notify', 'notification', 'price alert', 'filing alert', 'news alert', 'rsi', 'inbox', 'snooze'],
  commands: [
    { id: 'inbox', label: 'Alert inbox', path: [] },
    { id: 'rules', label: 'Alert rules', path: ['rules'] },
    { id: 'audit', label: 'Alert audit trail', path: ['audit'] },
  ],
};

// Command-line mnemonics (ui/functions.ts): ALRT opens the inbox, ALRTR the rules.
export const ALERT_FUNCTIONS: readonly TerminalFunction[] = [
  { mnemonic: 'ALRT', workspace: 'alerts', needsSecurity: false, help: 'Alerts: inbox of fired alerts' },
  { mnemonic: 'ALRTR', workspace: 'alerts', needsSecurity: false, help: 'Alert rules: create, pause, snooze, delete', rest: ['rules'] },
];

for (const fn of ALERT_FUNCTIONS) registerFunction(fn);
