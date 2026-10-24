import { registerFunction } from '../../ui/functions';
import type { WorkspaceDefinition } from '../types';

export const calendarWorkspace: WorkspaceDefinition = {
  id: 'calendar',
  title: 'Events calendar',
  icon: 'sessions',
  shortcut: 'g v',
  order: 116,
  load: () => import('./CalendarWorkspace').then((m) => m.CalendarWorkspace),
  description: 'Results releases and periodic reports by week from SEC filings: occurred, announced or projected (labelled)',
  keywords: ['calendar', 'events', 'earnings', 'results', 'earnings date', '8-k 2.02', '10-q', '10-k', 'watchlist', 'evts'],
  commands: [
    { id: 'watchlist', label: 'Calendar for the monitor watchlist', path: ['watchlist'] },
    { id: 'all', label: 'Calendar for all ingested issuers', path: ['all'] },
  ],
};

// Command-line mnemonic (ui/functions.ts): EVTS opens the events calendar for the watchlist.
registerFunction({ mnemonic: 'EVTS', workspace: 'calendar', needsSecurity: false, help: 'Events calendar: results dates from SEC filings' });
