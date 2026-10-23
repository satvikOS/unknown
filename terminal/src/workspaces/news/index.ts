import { registerFunction, type TerminalFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const newsWorkspace: WorkspaceDefinition = {
  id: 'news',
  title: 'News & filings',
  icon: 'research',
  shortcut: 'g n',
  order: 115,
  load: () => import('./NewsWorkspace').then((m) => m.NewsWorkspace),
  description: 'Live SEC EDGAR filings and official regulator and central-bank releases, with search, top stories and watchlist alerts',
  keywords: ['news', 'filings', 'edgar', '8-k', 'sec', 'federal reserve', 'ecb', 'rbi', 'sebi', 'press releases', 'alerts', 'top stories'],
  commands: [
    { id: 'tape', label: 'News tape', path: [] },
    { id: 'top', label: 'Top stories', path: ['top'] },
    { id: 'cn', label: 'Company news for the linked security', path: ['cn'] },
    { id: '8k', label: '8-K filings', path: ['form=8-K'] },
  ],
};

// Command-line mnemonics (ui/functions.ts): N opens the tape, TOP the top stories, CN company news for a security.
export const NEWS_FUNCTIONS: readonly TerminalFunction[] = [
  { mnemonic: 'N', workspace: 'news', needsSecurity: false, help: 'News and filings: live tape' },
  { mnemonic: 'TOP', workspace: 'news', needsSecurity: false, help: 'Top stories carried by several sources', rest: ['top'] },
  { mnemonic: 'CN', workspace: 'news', needsSecurity: true, help: 'Company news and filings for the security', rest: ['cn'] },
];

for (const fn of NEWS_FUNCTIONS) registerFunction(fn);
