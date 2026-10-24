/**
 * AI workspace (CAP-AGT-04, CAP-AGT-11, CAP-AGT-13). Routes:
 *   #/ai                      notes inbox (filters: process=, symbol=)     #/ai/brief   morning briefs
 *   #/ai/note/<note_id>       one note with its cited lines and sources
 *   #/ai/approvals            approval queue                                 #/ai/cost    cost and quality
 * Everything here is advisory: no control in this workspace can place an order, start a session or move money.
 */
import { useEffect } from 'react';
import { routeHref } from '../../state/router';
import { usePluginPost } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import { ApprovalsView } from './ApprovalsView';
import { CostView } from './CostView';
import { InboxView } from './InboxView';
import { NoteView } from './NoteView';
import { parseAiRoute } from './model';
import './ai.css';

const TABS = [
  { id: 'inbox', label: 'Notes', path: [] as string[] },
  { id: 'approvals', label: 'Approval queue', path: ['approvals'] },
  { id: 'cost', label: 'Cost and quality', path: ['cost'] },
];

export function AiWorkspace({ route, navigate }: WorkspaceProps) {
  const r = parseAiRoute(route.rest);
  const post = usePluginPost();
  // The worker never opens the core database: the API records which symbols are held (no quantities) for the brief.
  useEffect(() => {
    post('/api/ai/holdings', {}).catch(() => undefined);
  }, [post]);
  const active = r.view === 'note' ? 'inbox' : r.view;
  return (
    <div className="ai-root">
      <nav className="ai-tabs" aria-label="AI views">
        {TABS.map((t) => (
          <a
            key={t.id}
            href={routeHref('ai', ...t.path)}
            aria-current={active === t.id ? 'page' : undefined}
            className={active === t.id ? 'active' : ''}
          >
            {t.label}
          </a>
        ))}
      </nav>
      {r.view === 'note' ? <NoteView key={r.noteId} noteId={r.noteId} navigate={navigate} /> : null}
      {r.view === 'inbox' ? <InboxView process={r.process} symbol={r.symbol} navigate={navigate} /> : null}
      {r.view === 'approvals' ? <ApprovalsView /> : null}
      {r.view === 'cost' ? <CostView /> : null}
    </div>
  );
}
