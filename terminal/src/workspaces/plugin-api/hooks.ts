/** React bindings for plugin routes (see http.ts). */
import { useCallback, useRef, useState } from 'react';
import type { Endpoint, ParamsOf } from '../../api/types';
import { useQuery, useRuntime, type QueryOptions, type QueryResult } from '../../state/react';
import { postPluginRoute, type PluginParams, type PostDeps } from './http';

/**
 * `useQuery` for a plugin GET route that `api/types.ts` does not list. The cache, refetch-on-version,
 * polling and error handling are the shared ones; only the response type is supplied by the caller,
 * who must parse it defensively (the server owns the shape).
 */
export function usePluginQuery<T>(endpoint: string | null, params?: PluginParams, options?: QueryOptions): QueryResult<T> {
  // The query cache and the client's GET accept any path; the endpoint union only types the built-in routes.
  return useQuery(endpoint as Endpoint | null, params as ParamsOf<Endpoint>, options ?? {}) as unknown as QueryResult<T>;
}

/** A POST function bound to this runtime's session, CSRF token and 401 handling. */
export function usePluginPost(): <T>(path: string, body: unknown) => Promise<T> {
  const rt = useRuntime();
  return useCallback(
    <T,>(path: string, body: unknown) => {
      const deps: PostDeps = {
        csrf: () => rt.client.csrf,
        refreshSession: async () => {
          try {
            return (await rt.client.session()).authenticated;
          } catch {
            return false;
          }
        },
        onUnauthorized: () => rt.auth.markUnauthenticated(),
      };
      return postPluginRoute<T>(deps, path, body);
    },
    [rt],
  );
}

export interface OneShot<T> {
  status: 'idle' | 'loading' | 'success' | 'error';
  data: T | undefined;
  error: unknown;
  /** Browser time when the current data or error arrived. */
  atMs: number | null;
  run: (endpoint: string, params?: PluginParams) => Promise<void>;
}

/**
 * A GET that runs only when asked and is never refetched on stream version bumps: for results computed
 * on a snapshot of the core (what-if, stress), which the view labels with that snapshot's sequence and
 * marks stale when the core moves on, instead of silently recomputing them. A later run supersedes an
 * earlier one still in flight.
 */
export function usePluginGetOnce<T>(): OneShot<T> {
  const rt = useRuntime();
  const [state, setState] = useState<Omit<OneShot<T>, 'run'>>({ status: 'idle', data: undefined, error: null, atMs: null });
  const latest = useRef(0);
  const run = useCallback(
    async (endpoint: string, params?: PluginParams) => {
      const n = ++latest.current;
      setState((s) => ({ ...s, status: 'loading', error: null }));
      try {
        const data = (await rt.client.get(endpoint as Endpoint, params as ParamsOf<Endpoint>)) as unknown as T;
        if (n === latest.current) setState({ status: 'success', data, error: null, atMs: Date.now() });
      } catch (error) {
        if (n === latest.current) setState({ status: 'error', data: undefined, error, atMs: Date.now() });
      }
    },
    [rt],
  );
  return { ...state, run };
}
