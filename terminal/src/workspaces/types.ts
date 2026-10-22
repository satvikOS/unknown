/** Workspace registry contract (ADR-0013: workspaces appear only when backed by implemented services). */
import type { ComponentType } from 'react';
import type { Route } from '../state/router';
import type { IconName } from '../ui/Icon';

export interface WorkspaceProps {
  /** Current route; `route.rest` holds sub-path segments, e.g. ['ses-123'] for `#/sessions/ses-123`. */
  route: Route;
  /** Navigate to another workspace (or a sub-path of this one). */
  navigate: (workspace: string, ...rest: string[]) => void;
}

/** A palette entry that opens a view of this workspace: navigation only; commands with effects run from their forms. */
export interface WorkspaceCommand {
  /** Unique within the workspace. */
  id: string;
  label: string;
  /** Route segments under the workspace, e.g. ['~new'] for `#/orders/~new`. */
  path: readonly string[];
  keywords?: string;
  hint?: string;
}

/** A shortcut handled inside the workspace, listed in the "?" help. */
export interface WorkspaceKey {
  keys: string;
  action: string;
}

export interface WorkspaceDefinition {
  /** URL segment and stable id: lower-case letters, digits and hyphens. */
  id: string;
  /** Navigation and palette title. */
  title: string;
  icon?: IconName;
  /** Two-key sequence "g <key>" (press g, then the key). Must be unique across the registry. */
  shortcut: string;
  component: ComponentType<WorkspaceProps>;
  /** One line shown under the title and in the palette. */
  description?: string;
  /** Navigation order, ascending (default 100). */
  order?: number;
  /** Extra words the command palette matches. */
  keywords?: string[];
  /** Palette entries for this workspace's views (shown under "Go to"). */
  commands?: readonly WorkspaceCommand[];
  /** Workspace-local single-key shortcuts; they must honour `charKeysEnabled()` (WCAG 2.1.4). */
  keys?: readonly WorkspaceKey[];
}

const ID_RE = /^[a-z][a-z0-9-]{0,31}$/;
const SHORTCUT_RE = /^g [a-z0-9]$/;

/** Validates ids and shortcuts (unique, well-formed) and sorts by `order`. Throws on a bad registry. */
export function validateRegistry(defs: readonly WorkspaceDefinition[]): readonly WorkspaceDefinition[] {
  const ids = new Set<string>();
  const keys = new Map<string, string>();
  for (const d of defs) {
    if (!ID_RE.test(d.id)) throw new Error(`workspace id ${JSON.stringify(d.id)} must match ${ID_RE}`);
    if (ids.has(d.id)) throw new Error(`duplicate workspace id ${d.id}`);
    ids.add(d.id);
    if (!SHORTCUT_RE.test(d.shortcut)) throw new Error(`workspace ${d.id}: shortcut ${JSON.stringify(d.shortcut)} must look like "g x"`);
    const prev = keys.get(d.shortcut);
    if (prev) throw new Error(`workspace ${d.id}: shortcut "${d.shortcut}" already used by ${prev}`);
    keys.set(d.shortcut, d.id);
    const commandIds = new Set<string>();
    for (const c of d.commands ?? []) {
      if (commandIds.has(c.id)) throw new Error(`workspace ${d.id}: duplicate palette command id ${c.id}`);
      commandIds.add(c.id);
    }
  }
  return [...defs].sort((a, b) => (a.order ?? 100) - (b.order ?? 100) || a.title.localeCompare(b.title));
}
