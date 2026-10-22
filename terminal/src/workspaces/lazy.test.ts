/** On-demand workspaces (lazy.tsx): one lazy component per id, a loading line, a contained failure. */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { prerender } from 'react-dom/static';
import { describe, expect, it, vi } from 'vitest';
import type { Route } from '../state/router';
import { preloadWorkspace, WorkspaceBoundary, workspaceComponent, WorkspaceHost } from './lazy';
import type { WorkspaceDefinition, WorkspaceProps } from './types';

const route = { workspace: 'probe', rest: [] } as unknown as Route;
const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

function def(id: string, load: WorkspaceDefinition['load']): WorkspaceDefinition {
  return { id, title: `Probe ${id}`, shortcut: 'g z', load };
}

async function prerenderText(element: ReturnType<typeof createElement>): Promise<string> {
  const { prelude } = await prerender(element);
  return text(await new Response(prelude).text());
}

describe('lazy workspaces', () => {
  it('creates one lazy component per workspace id and loads nothing until it is rendered or preloaded', () => {
    const load = vi.fn(() => Promise.resolve(() => null));
    const d = def('one', load);
    expect(workspaceComponent(d)).toBe(workspaceComponent(d));
    expect(load).not.toHaveBeenCalled();
    preloadWorkspace(d);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('shows a loading line while the chunk is on its way, and no workspace title yet', () => {
    const d = def('pending', () => new Promise(() => undefined));
    const html = renderToStaticMarkup(createElement(WorkspaceHost, { workspace: d, route, navigate: () => undefined, onReady: () => undefined }));
    expect(text(html)).toBe('Loading the Probe pending workspace…');
    expect(html).toContain('role="status"');
    expect(html).not.toContain('ws-title');
  });

  it('renders the loaded component with the route and navigate it was given', async () => {
    const seen: WorkspaceProps[] = [];
    const View = (p: WorkspaceProps) => {
      seen.push(p);
      return createElement('h1', { id: 'ws-title' }, `at ${p.route.workspace}`);
    };
    const d = def('loaded', () => Promise.resolve(View));
    const navigate = () => undefined;
    const out = await prerenderText(createElement(WorkspaceHost, { workspace: d, route, navigate, onReady: () => undefined }));
    expect(out).toBe('at probe');
    expect(seen.at(-1)?.navigate).toBe(navigate);
  });

  it('a chunk that fails to load is contained, with the reason and a reload button', () => {
    const state = WorkspaceBoundary.getDerivedStateFromError(new Error('Failed to fetch dynamically imported module: /assets/RiskWorkspace-x.js'));
    const boundary = new WorkspaceBoundary({ title: 'Risk', children: null });
    boundary.state = state;
    const html = renderToStaticMarkup(createElement('div', null, boundary.render()));
    expect(html).toContain('role="alert"');
    expect(text(html)).toContain('The Risk workspace could not be shown. Failed to fetch dynamically imported module: /assets/RiskWorkspace-x.js');
    expect(text(html)).toContain('The header, STOP and the other workspaces still work.');
    expect(text(html)).toContain('Reload the terminal');
  });

  it('preloading swallows a failure (it surfaces when the workspace renders)', async () => {
    const load = vi.fn(() => Promise.reject(new Error('offline')));
    preloadWorkspace(def('broken', load));
    await new Promise((r) => setTimeout(r, 0));
    expect(load).toHaveBeenCalledTimes(1);
  });
});
