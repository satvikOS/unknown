/**
 * Hash router: `#/<workspace>/<rest...>`. Works behind any static server (the API's SPA fallback
 * or Vite) and keeps each workspace addressable and bookmarkable.
 */

export interface Route {
  workspace: string;
  /** Remaining path segments, decoded (e.g. a session id for `#/sessions/ses-1`). */
  rest: string[];
}

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#\/?/, '');
  const parts = path
    .split('/')
    .filter(Boolean)
    .map((p) => {
      try {
        return decodeURIComponent(p);
      } catch {
        return p;
      }
    });
  return { workspace: parts[0] ?? '', rest: parts.slice(1) };
}

export function routeHref(workspace: string, ...rest: string[]): string {
  return '#/' + [workspace, ...rest].map((p) => encodeURIComponent(p)).join('/');
}

type Listener = () => void;

export class RouterStore {
  private route: Route;
  private readonly listeners = new Set<Listener>();

  constructor() {
    this.route = parseHash(typeof window === 'undefined' ? '' : window.location.hash);
    if (typeof window !== 'undefined') {
      window.addEventListener('hashchange', () => {
        this.route = parseHash(window.location.hash);
        for (const l of [...this.listeners]) l();
      });
    }
  }

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): Route => this.route;

  navigate(workspace: string, ...rest: string[]): void {
    const href = routeHref(workspace, ...rest);
    if (window.location.hash !== href) window.location.hash = href;
  }
}
