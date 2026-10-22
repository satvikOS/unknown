/** Sessions workspace: `#/sessions` lists sessions; `#/sessions/<session_id>` shows one. */
import { useEffect, useRef } from 'react';
import type { WorkspaceProps } from '../types';
import { SessionDetailView } from './SessionDetail';
import { SessionList } from './SessionList';

export function SessionsWorkspace({ route, navigate }: WorkspaceProps) {
  const id = route.rest[0] ?? null;
  // Moving between the list and a session keeps the workspace, so the shell does not move focus:
  // put it on the new page title (announced by screen readers). The first render is left alone.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    document.getElementById('ws-title')?.focus();
  }, [id]);
  return id ? <SessionDetailView key={id} id={id} /> : <SessionList navigate={navigate} />;
}
