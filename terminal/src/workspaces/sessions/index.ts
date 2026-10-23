import type { WorkspaceDefinition } from '../types';

/** Backed by GET /api/sessions, GET /api/sessions/{id} and the session owner commands (ADR-0008, ADR-0013). */
export const sessionsWorkspace: WorkspaceDefinition = {
  id: 'sessions',
  title: 'Sessions',
  icon: 'sessions',
  shortcut: 'g s',
  order: 20,
  load: () => import('./SessionsWorkspace').then((m) => m.SessionsWorkspace),
  description: 'Seed, goal, P&L, protection, settlement, successor',
  keywords: ['mandate', 'successor', 'seed', 'target', 'funding', 'wind-down', 'protected', 'settlement'],
};
