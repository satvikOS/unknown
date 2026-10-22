/**
 * Workspace registry: the left navigation, the command palette and the "g <key>" shortcuts are all
 * built from this list. Register a workspace only when the backend service behind it exists
 * (ADR-0013 "no dead controls"); unbuilt capabilities belong on the Capabilities page.
 *
 * To add a workspace (see terminal/README.md):
 *   1. create src/workspaces/<id>/index.ts exporting a `WorkspaceDefinition`;
 *   2. import it here and add it to the array (order in the array does not matter; `order` does).
 *
 * Reserved shortcuts and nav order, so parallel work does not collide:
 *   overview g o (10)   sessions g s (20)   orders g b (30)   risk g r (40)
 *   instruments g i (50)   operations g p (60)   audit g a (70)   policy g y (80)
 *   capabilities g c (900)   portfolio g f (45)   marketdata g d (55)   research-jobs g j (85)
 */
import { auditWorkspace } from './audit';
import { capabilitiesWorkspace } from './capabilities';
import { instrumentsWorkspace } from './instruments';
import { marketDataWorkspace } from './marketdata';
import { operationsWorkspace } from './operations';
import { ordersWorkspace } from './orders';
import { overviewWorkspace } from './overview';
import { policyWorkspace } from './policy';
import { portfolioWorkspace } from './portfolio';
import { researchJobsWorkspace } from './research-jobs';
import { riskWorkspace } from './risk';
import { sessionsWorkspace } from './sessions';
import { validateRegistry, type WorkspaceDefinition } from './types';

export const WORKSPACES: readonly WorkspaceDefinition[] = validateRegistry([
  overviewWorkspace,
  capabilitiesWorkspace,
  ordersWorkspace,
  sessionsWorkspace,
  riskWorkspace,
  policyWorkspace,
  operationsWorkspace,
  auditWorkspace,
  instrumentsWorkspace,
  researchJobsWorkspace,
  marketDataWorkspace,
  portfolioWorkspace,
]);

export function findWorkspace(id: string): WorkspaceDefinition | undefined {
  return WORKSPACES.find((w) => w.id === id);
}

/** Link targets must exist: use this before rendering a cross-workspace link. */
export function hasWorkspace(id: string): boolean {
  return WORKSPACES.some((w) => w.id === id);
}

export const DEFAULT_WORKSPACE = WORKSPACES[0]?.id ?? 'overview';
