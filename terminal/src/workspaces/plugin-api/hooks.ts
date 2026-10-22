/** React bindings for plugin routes (see http.ts). */
import { useCallback } from 'react';
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
