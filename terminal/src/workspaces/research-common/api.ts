/**
 * Typed reads of the research-side plugin routes (/api/company, /api/macro, /api/research).
 *
 * These routes are served by backend route modules (api/routes/company.py, research.py), not by the core
 * query map in src/api/types.ts, so their wire types live next to the workspaces that read them. The
 * query goes through the shared cache (`useQuery`): same caching, refetch on every stream version bump,
 * same provenance in the workspace header. Empty parameters are dropped, as the API ignores them.
 */
import type { Endpoint, ParamsOf } from '../../api/types';
import { encodePathSegment } from '../../api/client';
import { useQuery, type QueryOptions, type QueryResult } from '../../state/react';

export type ApiParams = Record<string, string | number | null | undefined>;

/** Drops undefined, null and blank values (the API treats a missing parameter as its default). */
export function cleanParams(params: ApiParams | undefined): Record<string, string | number> | undefined {
  if (!params) return undefined;
  const out: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    if (typeof v === 'string' && v.trim() === '') continue;
    out[k] = typeof v === 'string' ? v.trim() : v;
  }
  return Object.keys(out).length ? out : undefined;
}

/** `/api/<segments...>` with each segment encoded like the core client does (":" and "@" stay literal). */
export function apiPath(...segments: (string | number)[]): string {
  return '/api/' + segments.map((s) => encodePathSegment(String(s))).join('/');
}

/** GET a plugin route through the shared query cache; pass null to disable. */
export function useApi<T>(path: string | null, params?: ApiParams, options?: QueryOptions): QueryResult<T> {
  const p = cleanParams(params);
  return useQuery(path as Endpoint | null, p as ParamsOf<Endpoint> | undefined, options) as unknown as QueryResult<T>;
}
