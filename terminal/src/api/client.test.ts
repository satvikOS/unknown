import { describe, expect, it, vi } from 'vitest';
import {
  ApiClient,
  ApiError,
  buildQuery,
  CommandTransportError,
  commandNotFound,
  defaultCommandId,
  encodePathSegment,
  InvalidPassphraseError,
  NetworkError,
  RateLimitedError,
  StepUpCancelledError,
  StepUpRequiredError,
  UnauthorizedError,
} from './client';

interface Recorded {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
  cache: RequestCache | undefined;
}

type Reply = { status: number; body?: unknown; headers?: Record<string, string> } | Error;

/** Scripted fetch: replies are consumed in order; every request is recorded. */
function fakeFetch(replies: Reply[]) {
  const calls: Recorded[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: init?.method ?? 'GET',
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      cache: init?.cache,
    });
    const r = replies.shift();
    if (!r) throw new Error('unexpected request');
    if (r instanceof Error) throw r;
    const text = r.body === undefined ? '' : typeof r.body === 'string' ? r.body : JSON.stringify(r.body);
    // A 304 (like a 204) has no body; the Response constructor refuses one.
    return new Response(r.status === 304 || r.status === 204 ? null : text, { status: r.status, headers: { 'Content-Type': 'application/json', ...r.headers } });
  });
  return { impl: impl as unknown as typeof fetch, calls };
}

const accepted = (id: string, events = [41]) => ({
  status: 200,
  body: { command_id: id, accepted: true, duplicate: false, body: { started: true }, events },
});

describe('ApiClient reads', () => {
  it('builds typed GETs with stable query strings and returns the JSON body', async () => {
    const { impl, calls } = fakeFetch([{ status: 200, body: [] }]);
    const client = new ApiClient({ fetchImpl: impl });
    const rows = await client.get('/api/orders', { state: 'open', limit: 50, session: undefined });
    expect(rows).toEqual([]);
    expect(calls[0]?.url).toBe('/api/orders?limit=50&state=open');
    expect(calls[0]?.method).toBe('GET');
    expect(calls[0]?.headers['X-CSRF-Token']).toBeUndefined();
  });

  it('revalidates an ETag-tagged read with If-None-Match: a 304 returns the kept body, a 200 replaces it', async () => {
    const v1 = { capabilities: [{ id: 'CAP-1' }] };
    const v2 = { capabilities: [{ id: 'CAP-1' }, { id: 'CAP-2' }] };
    const { impl, calls } = fakeFetch([
      { status: 200, body: v1, headers: { ETag: 'W/"d1"' } },
      { status: 304, headers: { ETag: 'W/"d1"' } },
      { status: 200, body: v2, headers: { ETag: 'W/"d2"' } },
      { status: 304 },
      { status: 200, body: v2 }, // served without a tag: nothing is kept
      { status: 200, body: v2 },
    ]);
    const client = new ApiClient({ fetchImpl: impl });
    const first = await client.get('/api/capabilities');
    expect(first).toEqual(v1);
    expect(await client.get('/api/capabilities')).toBe(first); // 304: the very body that was kept
    expect(await client.get('/api/capabilities')).toEqual(v2); // the register changed: a full 200
    expect(await client.get('/api/capabilities')).toEqual(v2);
    expect(await client.get('/api/capabilities')).toEqual(v2);
    await client.get('/api/capabilities');
    expect(calls.map((c) => [c.headers['If-None-Match'] ?? null, c.cache])).toEqual([
      [null, 'no-cache'],
      ['W/"d1"', 'no-store'], // our own validator: the API's 304 must reach the client, not the browser cache
      ['W/"d1"', 'no-store'],
      ['W/"d2"', 'no-store'],
      ['W/"d2"', 'no-store'],
      [null, 'no-cache'],
    ]);
  });

  it('never sends a validator for a response without an ETag, and forgets validators on sign-out', async () => {
    const { impl, calls } = fakeFetch([
      { status: 200, body: { mode: 'SIMULATED' } },
      { status: 200, body: { mode: 'SIMULATED' } },
      { status: 200, body: { capabilities: [] }, headers: { ETag: 'W/"d1"' } },
      { status: 200, body: { capabilities: [] }, headers: { ETag: 'W/"d1"' } },
      accepted('ui.stop.c1'),
    ]);
    const client = new ApiClient({ fetchImpl: impl });
    await client.get('/api/overview');
    await client.get('/api/overview');
    await client.get('/api/capabilities');
    client.forgetValidated();
    await client.get('/api/capabilities');
    await client.sendCommand('stop', { account_key: 'SIM-1' }, { commandId: 'ui.stop.c1' });
    expect(calls.map((c) => [c.method, c.url, c.headers['If-None-Match'] ?? null, c.cache])).toEqual([
      ['GET', '/api/overview', null, 'no-cache'],
      ['GET', '/api/overview', null, 'no-cache'],
      ['GET', '/api/capabilities', null, 'no-cache'],
      ['GET', '/api/capabilities', null, 'no-cache'],
      ['POST', '/api/commands', null, 'no-store'],
    ]);
  });

  it('a 304 with nothing kept is an error, never an empty register', async () => {
    const { impl } = fakeFetch([{ status: 304 }]);
    const client = new ApiClient({ fetchImpl: impl });
    await expect(client.get('/api/capabilities')).rejects.toMatchObject({ status: 304 });
  });

  it('401 on a read calls onUnauthorized and throws UnauthorizedError', async () => {
    const onUnauthorized = vi.fn();
    const { impl } = fakeFetch([{ status: 401, body: { error: 'login required' } }]);
    const client = new ApiClient({ fetchImpl: impl, onUnauthorized });
    await expect(client.get('/api/overview')).rejects.toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('surfaces the server message for other errors', async () => {
    const { impl } = fakeFetch([{ status: 503, body: { error: 'core database unavailable: locked' } }]);
    const client = new ApiClient({ fetchImpl: impl });
    const err = await client.get('/api/overview').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(503);
    expect((err as ApiError).serverMessage).toBe('core database unavailable: locked');
    expect((err as ApiError).message).toContain('core database unavailable');
  });

  it('wraps transport failures in NetworkError', async () => {
    const { impl } = fakeFetch([new TypeError('Failed to fetch')]);
    const client = new ApiClient({ fetchImpl: impl });
    await expect(client.get('/api/health')).rejects.toBeInstanceOf(NetworkError);
  });

  it('addresses detail endpoints with encoded path segments', async () => {
    const { impl, calls } = fakeFetch([{ status: 200, body: { session_id: 'a b' } }]);
    const client = new ApiClient({ fetchImpl: impl });
    await client.sessionDetail('a b');
    expect(calls[0]?.url).toBe('/api/sessions/a%20b');
    expect(encodePathSegment('ses:1@x+y')).toBe('ses:1@x+y');
    expect(encodePathSegment('a/b')).toBe('a%2Fb');
  });

  it('looks up a stored command result by id, with "/" kept inside the segment', async () => {
    const stored = { command_id: 'ui.start/1', kind: 'start', status: 'ACCEPTED', accepted: true, body: {}, events: [7] };
    const { impl, calls } = fakeFetch([{ status: 200, body: stored }]);
    const client = new ApiClient({ fetchImpl: impl });
    const r = await client.commandResult('ui.start/1');
    expect(calls[0]?.url).toBe('/api/commands/ui.start%2F1');
    expect(r.status).toBe('ACCEPTED');
  });

  it('reads a 404 COMMAND_NOT_FOUND as "not recorded", definitive only when the API says so', async () => {
    const answer = (definitive: boolean) => ({
      status: 404,
      body: { error: `no command x is recorded (${definitive ? 'core' : 'database'})`, code: 'COMMAND_NOT_FOUND', command_id: 'x', source: definitive ? 'core' : 'database', definitive },
    });
    const { impl } = fakeFetch([answer(true), answer(false), { status: 404, body: { error: 'unknown endpoint', code: 'NOT_FOUND' } }, { status: 503, body: { error: 'core database unavailable' } }]);
    const client = new ApiClient({ fetchImpl: impl });
    const errs: unknown[] = [];
    for (let i = 0; i < 4; i++) errs.push(await client.commandResult('x').catch((e: unknown) => e));
    expect(commandNotFound(errs[0])).toEqual({ definitive: true, message: 'no command x is recorded (core)' });
    expect(commandNotFound(errs[1])).toEqual({ definitive: false, message: 'no command x is recorded (database)' });
    // Any other failure is not an answer about the command.
    expect(commandNotFound(errs[2])).toBeNull();
    expect(commandNotFound(errs[3])).toBeNull();
    expect(commandNotFound(new Error('x'))).toBeNull();
  });
});

describe('ApiClient auth', () => {
  it('stores the CSRF token from login and echoes it on POSTs', async () => {
    const { impl, calls } = fakeFetch([
      { status: 200, body: { csrf_token: 'tok-1', authenticated_at_ns: 1 } },
      accepted('ui.stop.1'),
    ]);
    const client = new ApiClient({ fetchImpl: impl });
    await client.login('correct horse battery staple');
    expect(client.csrf).toBe('tok-1');
    await client.sendCommand('stop', { account_key: 'SIM-1' }, { commandId: 'ui.stop.1' });
    expect(calls[1]?.headers['X-CSRF-Token']).toBe('tok-1');
    expect(calls[1]?.body).toEqual({ kind: 'stop', payload: { account_key: 'SIM-1' }, command_id: 'ui.stop.1' });
  });

  it('a wrong passphrase is InvalidPassphraseError, not a logout', async () => {
    const onUnauthorized = vi.fn();
    const { impl } = fakeFetch([{ status: 401, body: { error: 'invalid passphrase' } }]);
    const client = new ApiClient({ fetchImpl: impl, onUnauthorized });
    await expect(client.login('nope')).rejects.toBeInstanceOf(InvalidPassphraseError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('backoff is RateLimitedError with the server message', async () => {
    const { impl } = fakeFetch([{ status: 429, body: { error: 'too many failed attempts; wait before retrying' } }]);
    const client = new ApiClient({ fetchImpl: impl });
    const err = await client.login('x').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(RateLimitedError);
    expect((err as RateLimitedError).serverMessage).toContain('too many failed attempts');
  });

  it('reauth with an expired session is a logout, with a wrong passphrase is not', async () => {
    const onUnauthorized = vi.fn();
    const { impl } = fakeFetch([
      { status: 401, body: { error: 'invalid passphrase' } },
      { status: 401, body: { error: 'login required' } },
    ]);
    const client = new ApiClient({ fetchImpl: impl, onUnauthorized });
    await expect(client.reauth('bad')).rejects.toBeInstanceOf(InvalidPassphraseError);
    expect(onUnauthorized).not.toHaveBeenCalled();
    await expect(client.reauth('x')).rejects.toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('refreshes a stale CSRF token once and retries (nothing reached the core)', async () => {
    const { impl, calls } = fakeFetch([
      { status: 403, body: { error: 'missing or invalid CSRF token' } },
      { status: 200, body: { authenticated: true, authenticated_at_ns: 5, csrf_token: 'fresh', now_ns: 6 } },
      accepted('ui.stop.9'),
    ]);
    const client = new ApiClient({ fetchImpl: impl });
    client.setCsrfToken('stale');
    const r = await client.sendCommand('stop', { account_key: 'SIM-1' }, { commandId: 'ui.stop.9' });
    expect(r.accepted).toBe(true);
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual(['POST /api/commands', 'GET /api/auth/session', 'POST /api/commands']);
    expect(calls[2]?.headers['X-CSRF-Token']).toBe('fresh');
  });
});

describe('ApiClient commands: 428 step-up flow', () => {
  it('on 428 prompts, re-authenticates and retries with the SAME command id', async () => {
    const { impl, calls } = fakeFetch([
      { status: 428, body: { error: 're-authentication required for this command' } },
      { status: 200, body: { authenticated_at_ns: 99 } }, // POST /api/auth/reauth from inside the prompt
      accepted('ui.start.abc', [77]),
    ]);
    const client = new ApiClient({ fetchImpl: impl });
    client.setCsrfToken('tok');
    const stepUp = vi.fn(async () => {
      await client.reauth('correct horse battery staple');
      return true;
    });
    client.setStepUpHandler(stepUp);
    const r = await client.sendCommand('start', { account_key: 'SIM-1' }, { commandId: 'ui.start.abc' });
    expect(stepUp).toHaveBeenCalledWith({
      kind: 'start',
      commandId: 'ui.start.abc',
      reason: 're-authentication required for this command',
    });
    expect(r).toEqual({ command_id: 'ui.start.abc', accepted: true, duplicate: false, body: { started: true }, events: [77] });
    const posts = calls.filter((c) => c.url === '/api/commands');
    expect(posts).toHaveLength(2);
    expect(posts.map((p) => (p.body as { command_id: string }).command_id)).toEqual(['ui.start.abc', 'ui.start.abc']);
    expect(calls[1]?.url).toBe('/api/auth/reauth');
    expect(calls[1]?.body).toEqual({ passphrase: 'correct horse battery staple' });
  });

  it('a dismissed prompt throws StepUpCancelledError and sends nothing else', async () => {
    const { impl, calls } = fakeFetch([{ status: 428, body: { error: 're-authentication required for this command' } }]);
    const client = new ApiClient({ fetchImpl: impl, stepUp: async () => false });
    const err = await client.sendCommand('start', { account_key: 'SIM-1' }, { commandId: 'ui.start.x' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(StepUpCancelledError);
    expect((err as StepUpCancelledError).commandId).toBe('ui.start.x');
    expect(calls).toHaveLength(1);
  });

  it('a prompt that throws is treated as cancelled', async () => {
    const { impl } = fakeFetch([{ status: 428, body: { error: 'x' } }]);
    const client = new ApiClient({
      fetchImpl: impl,
      stepUp: async () => {
        throw new Error('dialog torn down');
      },
    });
    await expect(client.sendCommand('start', { account_key: 'SIM-1' })).rejects.toBeInstanceOf(StepUpCancelledError);
  });

  it('a second 428 after re-authentication is StepUpRequiredError (no loop)', async () => {
    const { impl, calls } = fakeFetch([
      { status: 428, body: { error: 're-authentication required for this command' } },
      { status: 428, body: { error: 're-authentication required for this command' } },
    ]);
    const client = new ApiClient({ fetchImpl: impl, stepUp: async () => true });
    await expect(client.sendCommand('start', { account_key: 'SIM-1' })).rejects.toBeInstanceOf(StepUpRequiredError);
    expect(calls).toHaveLength(2);
  });

  it('without a handler a 428 is StepUpRequiredError carrying the command id', async () => {
    const { impl } = fakeFetch([{ status: 428, body: { error: 'reauth' } }]);
    const client = new ApiClient({ fetchImpl: impl });
    const err = await client.sendCommand('activate_policy', { account_key: 'A', policy_hash: 'h' }, { commandId: 'ui.x.1' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(StepUpRequiredError);
    expect((err as StepUpRequiredError).commandId).toBe('ui.x.1');
  });
});

describe('ApiClient commands: outcomes', () => {
  it('returns deterministic rejections as data (not errors)', async () => {
    const body = { code: 'PREFLIGHT_FAILED', message: 'RECONCILIATION_REQUIRED', details: { blockers: ['RECONCILIATION_REQUIRED'] } };
    const { impl } = fakeFetch([{ status: 200, body: { command_id: 'c1', accepted: false, duplicate: false, body, events: [12] } }]);
    const client = new ApiClient({ fetchImpl: impl });
    const r = await client.sendCommand('start', { account_key: 'SIM-1' }, { commandId: 'c1' });
    expect(r.accepted).toBe(false);
    expect(r.body).toEqual(body);
  });

  it('a 503 means the outcome is unknown: retry with the same id is advised', async () => {
    const { impl } = fakeFetch([{ status: 503, body: { error: 'core unavailable; command not executed: timed out' } }]);
    const client = new ApiClient({ fetchImpl: impl });
    const err = (await client.sendCommand('stop', { account_key: 'SIM-1' }, { commandId: 'ui.stop.z' }).catch((e: unknown) => e)) as CommandTransportError;
    expect(err).toBeInstanceOf(CommandTransportError);
    expect(err.commandId).toBe('ui.stop.z');
    expect(err.outcomeUnknown).toBe(true);
    expect(err.message).toContain('same command id');
  });

  it('a network failure is also outcome-unknown', async () => {
    const { impl } = fakeFetch([new TypeError('Failed to fetch')]);
    const client = new ApiClient({ fetchImpl: impl });
    const err = (await client.sendCommand('stop', { account_key: 'SIM-1' }).catch((e: unknown) => e)) as CommandTransportError;
    expect(err).toBeInstanceOf(CommandTransportError);
    expect(err.outcomeUnknown).toBe(true);
    expect(err.cause).toBeInstanceOf(NetworkError);
  });

  it('a 403 for a kind the terminal may not send is refused before the core', async () => {
    const { impl } = fakeFetch([{ status: 403, body: { error: "command 'process_started' is not available from the terminal" } }]);
    const client = new ApiClient({ fetchImpl: impl });
    const err = (await client.sendCommand('stop', { account_key: 'SIM-1' }).catch((e: unknown) => e)) as CommandTransportError;
    expect(err.outcomeUnknown).toBe(false);
    expect(err.status).toBe(403);
    expect(err.message).toContain('refused before reaching the core');
  });

  it('a 409 command id conflict reached the core: it says the core refused it, not that it never arrived', async () => {
    const body = {
      error: 'command id ui.stop.c reused with a different payload; the original command keeps its result',
      code: 'COMMAND_ID_CONFLICT',
      command_id: 'ui.stop.c',
      outcome: 'NOT_EXECUTED',
    };
    const { impl } = fakeFetch([{ status: 409, body }]);
    const client = new ApiClient({ fetchImpl: impl });
    const err = (await client.sendCommand('stop', { account_key: 'SIM-1' }, { commandId: 'ui.stop.c' }).catch((e: unknown) => e)) as CommandTransportError;
    expect(err).toBeInstanceOf(CommandTransportError);
    expect(err.outcomeUnknown).toBe(false);
    expect(err.message).toContain('refused by the core; not executed');
    expect(err.message).not.toContain('before reaching the core');
    // A definitive refusal that carries the API's code and message (the tracker records it as rejected).
    expect(err.conflict).toBe(true);
    expect(err.code).toBe('COMMAND_ID_CONFLICT');
    expect(err.serverMessage).toBe(body.error);
    expect(err.status).toBe(409);
  });

  it('only a 409 is a conflict; other refusals keep their own code', async () => {
    const { impl } = fakeFetch([
      { status: 400, body: { error: 'the core refused the command as invalid: bad field', code: 'COMMAND_INVALID', outcome: 'NOT_EXECUTED' } },
      { status: 504, body: { error: 'no answer from the core', code: 'COMMAND_OUTCOME_UNKNOWN', outcome: 'UNKNOWN' } },
    ]);
    const client = new ApiClient({ fetchImpl: impl });
    const invalid = (await client.sendCommand('stop', { account_key: 'SIM-1' }).catch((e: unknown) => e)) as CommandTransportError;
    expect(invalid).toMatchObject({ conflict: false, code: 'COMMAND_INVALID', outcomeUnknown: false });
    const unknown = (await client.sendCommand('stop', { account_key: 'SIM-1' }).catch((e: unknown) => e)) as CommandTransportError;
    expect(unknown).toMatchObject({ conflict: false, code: 'COMMAND_OUTCOME_UNKNOWN', outcomeUnknown: true });
  });

  it('a 401 on a command logs out', async () => {
    const onUnauthorized = vi.fn();
    const { impl } = fakeFetch([{ status: 401, body: { error: 'login required' } }]);
    const client = new ApiClient({ fetchImpl: impl, onUnauthorized });
    await expect(client.sendCommand('stop', { account_key: 'SIM-1' })).rejects.toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});

describe('helpers', () => {
  it('generates command ids the core accepts', () => {
    const id = defaultCommandId('stop');
    expect(id).toMatch(/^ui\.stop\.[0-9a-f]{24}$/);
    expect(id).toMatch(/^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,159}$/); // backend domain/ids.py
    expect(defaultCommandId('stop')).not.toBe(id);
  });

  it('builds sorted query strings and drops empty values', () => {
    expect(buildQuery(undefined)).toBe('');
    expect(buildQuery({ b: 2, a: 'x y', c: undefined, d: null, e: '' })).toBe('?a=x%20y&b=2');
  });
});
