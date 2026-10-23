import type { WorkspaceDefinition } from '../types';

/** Orders: blotter, order detail, executions and the owner's order ticket (book Ch. 22). */
export const ordersWorkspace: WorkspaceDefinition = {
  id: 'orders',
  title: 'Orders',
  icon: 'orders',
  shortcut: 'g b',
  order: 30,
  load: () => import('./OrdersWorkspace').then((m) => m.OrdersWorkspace),
  description: 'Blotter, order detail, executions and the order ticket; controlled cancel',
  keywords: ['blotter', 'ticket', 'new order', 'buy', 'sell', 'intent', 'cancel', 'fills', 'executions', 'holds', 'pretrade'],
  commands: [
    { id: 'ticket', label: 'New order ticket', path: ['~new'], keywords: 'buy sell intent limit', hint: 'n' },
    { id: 'blotter', label: 'Blotter', path: [], keywords: 'open orders cancel', hint: 'l' },
    { id: 'executions', label: 'Executions and fees', path: ['~executions'], keywords: 'fills fee status', hint: 'e' },
  ],
  keys: [
    { keys: 'n', action: 'new order ticket' },
    { keys: 'l', action: 'blotter' },
    { keys: 'e', action: 'executions' },
  ],
};
