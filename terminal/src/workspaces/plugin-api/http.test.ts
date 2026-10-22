import { describe, expect, it, vi } from 'vitest';
import { ApiError, NetworkError, UnauthorizedError, describeError } from '../../api/client';
import { pluginPath, postPluginRoute, serverMessage, type PostDeps } from './http';

function response(status: number, body: unknown): Response {
  return new Response(body === undefined ? '' : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function deps(fetchImpl: typeof fetch, over: Partial<PostDeps> = {}): PostDeps {
  let token = 'tok-1';
  return {
    csrf: () => token,
    refreshSession: async () => {
      token = 'tok-2';
      return true;
    },
    onUnauthorized: vi.fn(),
    fetchImpl,
    ...over,
  };
}

describe('postPluginRoute', () => {
  it('sends JSON with the CSRF token and returns the 2xx body', async () => {
    const fetchImpl = vi.fn(async () => response(200, { job_id: 'rj-1', state: 'QUEUED' }));
    const out = await postPluginRoute<{ job_id: string }>(deps(fetchImpl as unknown as typeof fetch), '/api/research/jobs', { question: 'q' });
    expect(out.job_id).toBe('rj-1');
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/research/jobs');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('tok-1');
    expect(init.body).toBe('{"question":"q"}');
  });

  it('refreshes the session once after a CSRF refusal and resends with the new token', async () => {
    const seen: string[] = [];
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      seen.push((init.headers as Record<string, string>)['X-CSRF-Token'] ?? '');
      return seen.length === 1 ? response(403, { error: 'CSRF token missing or invalid' }) : response(200, { ok: true });
    });
    await postPluginRoute(deps(fetchImpl as unknown as typeof fetch), '/api/x', {});
    expect(seen).toEqual(['tok-1', 'tok-2']);
  });

  it('does not retry a non-CSRF 403 and keeps the server message', async () => {
    const fetchImpl = vi.fn(async () => response(403, { error: 'forbidden for this role' }));
    const err = await postPluginRoute(deps(fetchImpl as unknown as typeof fetch), '/api/x', {}).catch((e: unknown) => e);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(403);
  });

  it('maps 409 to an ApiError whose message is the server reason (shown verbatim)', async () => {
    const fetchImpl = vi.fn(async () =>
      response(409, { error: 'paid model calls are disabled: there is no owner policy (<UNKNOWN_HOME>/config/agents.json); see docs/agents/README.md' }),
    );
    const err = await postPluginRoute(deps(fetchImpl as unknown as typeof fetch), '/api/research/jobs', { question: 'q' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect(describeError(err)).toMatch(/^paid model calls are disabled/);
  });

  it('signals an ended session on 401', async () => {
    const onUnauthorized = vi.fn();
    const fetchImpl = vi.fn(async () => response(401, { error: 'not authenticated' }));
    const err = await postPluginRoute(deps(fetchImpl as unknown as typeof fetch, { onUnauthorized }), '/api/x', {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('reports a transport failure as a NetworkError', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('fetch failed');
    });
    const err = await postPluginRoute(deps(fetchImpl as unknown as typeof fetch), '/api/x', {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(NetworkError);
  });
});

describe('helpers', () => {
  it('builds stable query strings and reads server messages', () => {
    expect(pluginPath('/api/portfolio/holdings', { scope: 'system', basis: 'bid', empty: '', none: undefined })).toBe(
      '/api/portfolio/holdings?basis=bid&scope=system',
    );
    expect(serverMessage({ error: 'x' })).toBe('x');
    expect(serverMessage('  plain  ')).toBe('plain');
    expect(serverMessage(null)).toBeNull();
  });
});
