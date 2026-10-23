/**
 * Workstation layout document (CAP-UX-04): a tree of splits and panels. Pure functions only; every operation
 * returns a new document and leaves a locked document unchanged. A layout holds references (workspace ids,
 * route segments, link channels), never data values, credentials or account identifiers.
 */
import { LINK_CHANNELS, UNLINKED_PREFIX } from '../link';

export const LAYOUT_SCHEMA = 1;
export const MAX_PANELS = 16;
const MIN_SIZE = 10;

export interface PanelNode {
  kind: 'panel';
  id: string;
  /** Workspace id shown in the panel; null shows the workspace picker. */
  workspace: string | null;
  rest: string[];
  /** Link channel id (LINK_CHANNELS) or `solo:<panel id>` when unlinked. */
  channel: string;
}

export interface SplitNode {
  kind: 'split';
  id: string;
  /** 'row': children side by side (vertical divider); 'column': stacked (horizontal divider). */
  dir: 'row' | 'column';
  /** Percentages, one per child, summing to 100. */
  sizes: number[];
  children: LayoutNode[];
}

export type LayoutNode = PanelNode | SplitNode;

export interface LayoutDoc {
  schema: typeof LAYOUT_SCHEMA;
  name: string;
  root: LayoutNode;
  /** Id of the active panel (commands from the command line land there). */
  active: string;
  locked: boolean;
}

export function panelsOf(node: LayoutNode): PanelNode[] {
  return node.kind === 'panel' ? [node] : node.children.flatMap(panelsOf);
}

function idsOf(node: LayoutNode): string[] {
  return node.kind === 'panel' ? [node.id] : [node.id, ...node.children.flatMap(idsOf)];
}

/** Next unused id with the given prefix (p1, p2, ... / s1, s2, ...). */
export function nextId(root: LayoutNode, prefix: 'p' | 's'): string {
  let max = 0;
  for (const id of idsOf(root)) {
    const m = new RegExp(`^${prefix}(\\d+)$`).exec(id);
    if (m) max = Math.max(max, Number(m[1]));
  }
  return `${prefix}${max + 1}`;
}

function mapNode(node: LayoutNode, fn: (n: LayoutNode) => LayoutNode): LayoutNode {
  const mapped = node.kind === 'split' ? { ...node, children: node.children.map((c) => mapNode(c, fn)) } : node;
  return fn(mapped);
}

function round(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Split a panel: the new panel goes after it (right for 'row', below for 'column') on the same channel. */
export function splitPanel(doc: LayoutDoc, panelId: string, dir: 'row' | 'column', workspace: string | null = null): LayoutDoc {
  if (doc.locked || panelsOf(doc.root).length >= MAX_PANELS) return doc;
  const target = panelsOf(doc.root).find((p) => p.id === panelId);
  if (!target) return doc;
  const pid = nextId(doc.root, 'p');
  const fresh: PanelNode = {
    kind: 'panel',
    id: pid,
    workspace,
    rest: [],
    channel: target.channel.startsWith(UNLINKED_PREFIX) ? `${UNLINKED_PREFIX}${pid}` : target.channel,
  };
  let done = false;
  // Same direction as the parent: insert a sibling and halve the panel's share.
  let root = mapNode(doc.root, (n) => {
    if (done || n.kind !== 'split' || n.dir !== dir) return n;
    const i = n.children.findIndex((c) => c.kind === 'panel' && c.id === panelId);
    if (i < 0) return n;
    done = true;
    const half = round((n.sizes[i] ?? 0) / 2);
    const sizes = [...n.sizes];
    sizes.splice(i, 1, half, round((n.sizes[i] ?? 0) - half));
    const children = [...n.children];
    children.splice(i + 1, 0, fresh);
    return { ...n, sizes, children };
  });
  if (!done) {
    const sid = nextId(doc.root, 's');
    root = mapNode(doc.root, (n) =>
      n.kind === 'panel' && n.id === panelId ? { kind: 'split', id: sid, dir, sizes: [50, 50], children: [n, fresh] } : n,
    );
  }
  return { ...doc, root, active: pid };
}

/** Close a panel (never the last one); a split left with one child is replaced by that child. */
export function closePanel(doc: LayoutDoc, panelId: string): LayoutDoc {
  if (doc.locked) return doc;
  const all = panelsOf(doc.root);
  if (all.length <= 1 || !all.some((p) => p.id === panelId)) return doc;
  const prune = (n: LayoutNode): LayoutNode | null => {
    if (n.kind === 'panel') return n.id === panelId ? null : n;
    const kept: { c: LayoutNode; s: number }[] = [];
    n.children.forEach((c, i) => {
      const r = prune(c);
      if (r) kept.push({ c: r, s: n.sizes[i] ?? 0 });
    });
    if (kept.length === 0) return null;
    if (kept.length === 1) return kept[0]!.c;
    const total = kept.reduce((a, k) => a + k.s, 0) || 1;
    return { ...n, children: kept.map((k) => k.c), sizes: kept.map((k) => round((k.s / total) * 100)) };
  };
  const root = prune(doc.root) ?? doc.root;
  const remaining = panelsOf(root);
  const active = remaining.some((p) => p.id === doc.active) ? doc.active : (remaining[0]?.id ?? doc.active);
  return { ...doc, root, active };
}

/** Change what a panel shows or which channel it follows. */
export function updatePanel(doc: LayoutDoc, panelId: string, patch: Partial<Pick<PanelNode, 'workspace' | 'rest' | 'channel'>>): LayoutDoc {
  if (doc.locked) return doc;
  let changed = false;
  const root = mapNode(doc.root, (n) => {
    if (n.kind !== 'panel' || n.id !== panelId) return n;
    changed = true;
    return { ...n, ...patch, rest: patch.rest ? [...patch.rest] : patch.workspace !== undefined && patch.workspace !== n.workspace ? [] : n.rest };
  });
  return changed ? { ...doc, root } : doc;
}

/** Move the divider after child `index` of a split by `delta` percentage points (each side keeps at least 10%). */
export function resizeSplit(doc: LayoutDoc, splitId: string, index: number, delta: number): LayoutDoc {
  if (doc.locked || !Number.isFinite(delta)) return doc;
  const root = mapNode(doc.root, (n) => {
    if (n.kind !== 'split' || n.id !== splitId || index < 0 || index >= n.children.length - 1) return n;
    const a = n.sizes[index] ?? 0;
    const b = n.sizes[index + 1] ?? 0;
    const d = Math.max(MIN_SIZE - a, Math.min(b - MIN_SIZE, delta));
    const sizes = [...n.sizes];
    sizes[index] = round(a + d);
    sizes[index + 1] = round(b - d);
    return { ...n, sizes };
  });
  return { ...doc, root };
}

export function setActive(doc: LayoutDoc, panelId: string): LayoutDoc {
  return doc.active === panelId || !panelsOf(doc.root).some((p) => p.id === panelId) ? doc : { ...doc, active: panelId };
}

export function setLocked(doc: LayoutDoc, locked: boolean): LayoutDoc {
  return doc.locked === locked ? doc : { ...doc, locked };
}

const WS_RE = /^[a-z][a-z0-9-]{0,31}$/;
const ID_RE = /^[ps]\d{1,4}$/;
const CHANNEL_IDS = new Set(LINK_CHANNELS.map((c) => c.id));

function validNode(x: unknown, depth: number, seen: Set<string>): LayoutNode | null {
  if (!x || typeof x !== 'object' || depth > 8) return null;
  const n = x as Record<string, unknown>;
  if (typeof n.id !== 'string' || !ID_RE.test(n.id) || seen.has(n.id)) return null;
  seen.add(n.id);
  if (n.kind === 'panel') {
    const ws = n.workspace;
    if (!(ws === null || (typeof ws === 'string' && WS_RE.test(ws)))) return null;
    if (!Array.isArray(n.rest) || n.rest.length > 8 || !n.rest.every((s) => typeof s === 'string' && s.length <= 200)) return null;
    const ch = n.channel;
    if (typeof ch !== 'string' || !(CHANNEL_IDS.has(ch) || ch === `${UNLINKED_PREFIX}${n.id}`)) return null;
    return { kind: 'panel', id: n.id, workspace: ws, rest: [...(n.rest as string[])], channel: ch };
  }
  if (n.kind === 'split') {
    if (n.dir !== 'row' && n.dir !== 'column') return null;
    if (!Array.isArray(n.children) || n.children.length < 2 || !Array.isArray(n.sizes) || n.sizes.length !== n.children.length) return null;
    if (!n.sizes.every((s) => typeof s === 'number' && Number.isFinite(s) && s >= 1)) return null;
    const total = (n.sizes as number[]).reduce((a, b) => a + b, 0);
    if (Math.abs(total - 100) > 1) return null;
    const children: LayoutNode[] = [];
    for (const c of n.children) {
      const v = validNode(c, depth + 1, seen);
      if (!v) return null;
      children.push(v);
    }
    return { kind: 'split', id: n.id, dir: n.dir, sizes: [...(n.sizes as number[])], children };
  }
  return null;
}

/** A validated copy of an untrusted layout document, or null (wrong schema, malformed tree, too many panels). */
export function validateLayout(x: unknown): LayoutDoc | null {
  if (!x || typeof x !== 'object') return null;
  const d = x as Record<string, unknown>;
  if (d.schema !== LAYOUT_SCHEMA || typeof d.name !== 'string' || !d.name.trim() || d.name.length > 60) return null;
  const root = validNode(d.root, 0, new Set());
  if (!root) return null;
  const panels = panelsOf(root);
  if (panels.length > MAX_PANELS) return null;
  const active = typeof d.active === 'string' && panels.some((p) => p.id === d.active) ? d.active : panels[0]!.id;
  return { schema: LAYOUT_SCHEMA, name: d.name.trim(), root, active, locked: d.locked === true };
}

export interface LayoutTemplate {
  id: string;
  name: string;
  description: string;
  build: () => LayoutDoc;
}

function panel(id: string, workspace: string | null, channel = 'default'): PanelNode {
  return { kind: 'panel', id, workspace, rest: [], channel };
}

/** Our own templates (no vendor layouts copied). Workspaces that are not installed show the picker. */
export const TEMPLATES: readonly LayoutTemplate[] = [
  {
    id: 'single',
    name: 'Single panel',
    description: 'One panel on channel 1',
    build: () => ({ schema: LAYOUT_SCHEMA, name: 'Single panel', root: panel('p1', 'overview'), active: 'p1', locked: false }),
  },
  {
    id: 'trader',
    name: 'Trader',
    description: 'Monitor on the left; chart above company on the right; all on channel 1',
    build: () => ({
      schema: LAYOUT_SCHEMA,
      name: 'Trader',
      root: {
        kind: 'split',
        id: 's1',
        dir: 'row',
        sizes: [40, 60],
        children: [
          panel('p1', 'monitor'),
          { kind: 'split', id: 's2', dir: 'column', sizes: [60, 40], children: [panel('p2', 'chart'), panel('p3', 'company')] },
        ],
      },
      active: 'p1',
      locked: false,
    }),
  },
  {
    id: 'desk',
    name: 'Desk overview',
    description: 'Overview, orders, risk and market data in a 2 x 2 grid',
    build: () => ({
      schema: LAYOUT_SCHEMA,
      name: 'Desk overview',
      root: {
        kind: 'split',
        id: 's1',
        dir: 'column',
        sizes: [50, 50],
        children: [
          { kind: 'split', id: 's2', dir: 'row', sizes: [50, 50], children: [panel('p1', 'overview'), panel('p2', 'orders')] },
          { kind: 'split', id: 's3', dir: 'row', sizes: [50, 50], children: [panel('p3', 'risk'), panel('p4', 'marketdata')] },
        ],
      },
      active: 'p1',
      locked: false,
    }),
  },
  {
    id: 'compare',
    name: 'Compare two securities',
    description: 'Two columns on channels 1 and 2, each a chart above a company page',
    build: () => ({
      schema: LAYOUT_SCHEMA,
      name: 'Compare two securities',
      root: {
        kind: 'split',
        id: 's1',
        dir: 'row',
        sizes: [50, 50],
        children: [
          { kind: 'split', id: 's2', dir: 'column', sizes: [50, 50], children: [panel('p1', 'chart'), panel('p2', 'company')] },
          { kind: 'split', id: 's3', dir: 'column', sizes: [50, 50], children: [panel('p3', 'chart', 'ch2'), panel('p4', 'company', 'ch2')] },
        ],
      },
      active: 'p1',
      locked: false,
    }),
  },
];

export const DEFAULT_TEMPLATE = 'trader';

export function templateLayout(id: string): LayoutDoc {
  return (TEMPLATES.find((t) => t.id === id) ?? TEMPLATES.find((t) => t.id === DEFAULT_TEMPLATE)!).build();
}
