/**
 * Workspaces load on demand. The registry (navigation, palette, shortcuts) is static and small; each
 * workspace's component is a dynamic import (`WorkspaceDefinition.load`), so Vite emits one chunk per
 * workspace and the initial bundle carries only the shell. `WorkspaceHost` renders the current workspace
 * with a loading line while its chunk arrives and a contained error if the chunk cannot be loaded (for
 * example after the API started serving a newer build): the header, STOP and navigation keep working.
 */
import { Component, Suspense, lazy, useEffect, useRef, type LazyExoticComponent, type ReactNode } from 'react';
import type { WorkspaceComponent, WorkspaceDefinition, WorkspaceProps } from './types';

const components = new Map<string, LazyExoticComponent<WorkspaceComponent>>();

/** The lazy component for a workspace, created once per workspace id (React.lazy must keep its identity). */
export function workspaceComponent(def: WorkspaceDefinition): LazyExoticComponent<WorkspaceComponent> {
  let c = components.get(def.id);
  if (!c) {
    c = lazy(() => def.load().then((component) => ({ default: component })));
    components.set(def.id, c);
  }
  return c;
}

/** Start fetching a workspace's chunk (on hover or focus of its navigation link); failures surface on render. */
export function preloadWorkspace(def: WorkspaceDefinition): void {
  def.load().catch(() => undefined);
}

interface BoundaryProps {
  title: string;
  children: ReactNode;
}

interface BoundaryState {
  error: unknown;
}

/** Contains a workspace that failed to load or to render, so the rest of the terminal stays usable. */
export class WorkspaceBoundary extends Component<BoundaryProps, BoundaryState> {
  override state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): BoundaryState {
    return { error: error ?? new Error('unknown error') };
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    const message = error instanceof Error ? error.message : String(error);
    return (
      <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', margin: '8px 0' }}>
        <strong>The {this.props.title} workspace could not be shown.</strong> {message}
        <p className="muted" style={{ margin: '6px 0 0' }}>
          The header, STOP and the other workspaces still work. If the terminal was updated while this page was open, reload it to fetch the new
          version.
        </p>
        <button type="button" className="btn small" style={{ marginTop: 6 }} onClick={() => window.location.reload()}>
          Reload the terminal
        </button>
      </div>
    );
  }
}

/** Runs once when the boundary's content (the loaded workspace) has been committed. */
function Ready({ onReady }: { onReady: () => void }) {
  const ran = useRef(false);
  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    onReady();
  }, [onReady]);
  return null;
}

export interface WorkspaceHostProps extends WorkspaceProps {
  workspace: WorkspaceDefinition;
  /** Called once the workspace's own content is on screen (its `#ws-title` exists), e.g. to move focus there. */
  onReady: (workspaceId: string) => void;
}

/** The current workspace: loading line, then the component; keyed by id so a failure stays with its workspace. */
export function WorkspaceHost({ workspace, route, navigate, onReady }: WorkspaceHostProps) {
  const View = workspaceComponent(workspace);
  return (
    <WorkspaceBoundary key={workspace.id} title={workspace.title}>
      <Suspense
        fallback={
          <p className="muted ws-loading" role="status">
            Loading the {workspace.title} workspace…
          </p>
        }
      >
        <View route={route} navigate={navigate} />
        <Ready onReady={() => onReady(workspace.id)} />
      </Suspense>
    </WorkspaceBoundary>
  );
}
