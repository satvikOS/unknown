/**
 * Plugin route access for workspaces whose endpoints are not in `api/types.ts` (backend
 * `api/routes/*.py`: portfolio, marketdata, research jobs).
 *
 * GETs go through the shared query cache (see `usePluginQuery`), so they get the same refetch on every
 * state-stream version bump, the same provenance in `WorkspaceFrame`, and the same 401 handling as the
 * typed endpoints. POSTs use `postPluginRoute` below: the owner session cookie plus the CSRF token the
 * API client already holds, one CSRF refresh on a 403 CSRF refusal (nothing reached the route), and the
 * client's error classes so `describeError` reads the server's message.
 */
import { ApiError, NetworkError, UnauthorizedError, buildQuery, type QueryValue } from '../../api/client';

export type PluginParams = Record<string, QueryValue>;

/** The pieces of the runtime a POST needs (the API client and the auth store). */
export interface PostDeps {
  csrf: () => string | null;
  /** Re-read the session (refreshes the CSRF token). Resolves to whether the session is still valid. */
  refreshSession: () => Promise<boolean>;
  /** Called on HTTP 401: the session has ended. */
  onUnauthorized: () => void;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

/** The API's `{"error": "..."}` message, or a trimmed text body. */
export function serverMessage(body: unknown): string | null {
  if (body && typeof body === 'object' && typeof (body as { error?: unknown }).error === 'string') return (body as { error: string }).error;
  if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 300);
  return null;
}

function isCsrfRefusal(status: number, body: unknown): boolean {
  return status === 403 && /csrf/i.test(serverMessage(body) ?? '');
}

/**
 * POST a JSON body to a plugin route. Resolves with the parsed 2xx body; throws `ApiError` (with the
 * server's message and status, e.g. 409 for a state conflict), `UnauthorizedError` or `NetworkError`.
 */
export async function postPluginRoute<T>(deps: PostDeps, path: string, body: unknown): Promise<T> {
  const f = deps.fetchImpl ?? globalThis.fetch;
  const url = (deps.baseUrl ?? '') + path;
  const send = async (): Promise<{ status: number; body: unknown }> => {
    const headers: Record<string, string> = { Accept: 'application/json', 'Content-Type': 'application/json' };
    const token = deps.csrf();
    if (token) headers['X-CSRF-Token'] = token;
    let res: Response;
    try {
      res = await f(url, { method: 'POST', headers, credentials: 'same-origin', cache: 'no-store', body: JSON.stringify(body) });
    } catch (err) {
      throw new NetworkError('POST', path, err);
    }
    return { status: res.status, body: await readBody(res) };
  };
  let res = await send();
  if (isCsrfRefusal(res.status, res.body) && (await deps.refreshSession())) res = await send();
  if (res.status >= 200 && res.status < 300) return res.body as T;
  const msg = serverMessage(res.body);
  if (res.status === 401) {
    const err = new UnauthorizedError('POST', path, msg, res.body);
    deps.onUnauthorized();
    throw err;
  }
  throw new ApiError('POST', path, res.status, msg, res.body);
}

/** Path plus a stable query string (the same encoding the typed client uses). */
export function pluginPath(endpoint: string, params?: PluginParams): string {
  return endpoint + buildQuery(params);
}
