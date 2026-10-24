import { registerFunction, type TerminalFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

/**
 * Command-line functions. They only open a view (ui/functions.ts): BUY and SELL open the ticket with the side chosen
 * for the security, LADDER the price ladder; nothing is sent until the owner confirms in the view.
 */
export const ORDER_FUNCTIONS: readonly TerminalFunction[] = [
  { mnemonic: 'BUY', workspace: 'orders', needsSecurity: true, help: 'Order ticket to buy the security, with the live pretrade preview', rest: ['~buy'] },
  { mnemonic: 'SELL', workspace: 'orders', needsSecurity: true, help: 'Order ticket to sell the security, with the live pretrade preview', rest: ['~sell'] },
  { mnemonic: 'LADDER', workspace: 'orders', needsSecurity: true, help: 'Price ladder: depth or level 1, place and cancel limit orders by click', rest: ['~ladder'] },
  { mnemonic: 'BLOTTER', workspace: 'orders', needsSecurity: false, help: 'Order blotter: live status, cancel and replace', rest: [] },
];
for (const fn of ORDER_FUNCTIONS) registerFunction(fn);

/** Orders: blotter, order detail, executions and the owner's order ticket (book Ch. 22). */
export const ordersWorkspace: WorkspaceDefinition = {
  id: 'orders',
  title: 'Orders',
  icon: 'orders',
  shortcut: 'g b',
  order: 30,
  load: () => import('./OrdersWorkspace').then((m) => m.OrdersWorkspace),
  description: 'Order ticket with live pretrade preview, price ladder, baskets, blotter with cancel and replace, executions',
  keywords: ['blotter', 'ticket', 'new order', 'buy', 'sell', 'intent', 'cancel', 'replace', 'ladder', 'depth', 'basket', 'fills', 'executions', 'holds', 'pretrade', 'preview'],
  commands: [
    { id: 'ticket', label: 'New order ticket', path: ['~new'], keywords: 'buy sell intent limit', hint: 'n' },
    { id: 'blotter', label: 'Blotter', path: [], keywords: 'open orders cancel', hint: 'l' },
    { id: 'executions', label: 'Executions and fees', path: ['~executions'], keywords: 'fills fee status', hint: 'e' },
    { id: 'ladder', label: 'Price ladder', path: ['~ladder'], keywords: 'depth dom click trade', hint: 'p' },
    { id: 'basket', label: 'Basket of intents', path: ['~basket'], keywords: 'list program several orders', hint: 'k' },
  ],
  keys: [
    { keys: 'n', action: 'new order ticket' },
    { keys: 'l', action: 'blotter' },
    { keys: 'e', action: 'executions' },
    { keys: 'p', action: 'price ladder' },
    { keys: 'k', action: 'basket' },
  ],
};
