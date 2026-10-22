import { describe, expect, it, vi } from 'vitest';
import { ApiError, CommandTransportError, NetworkError, StepUpCancelledError } from '../api/client';
import type { CommandResponse, CommandResult } from '../api/types';
import { CommandTracker, canRecheck, canRetry, failedText, lookupNote, type CommandSender, type SeqSource } from './commands';

function seqSource(live = true) {
  const waiters: { seq: number; resolve: () => void }[] = [];
  let current = 0;
  const src: SeqSource & { advance: (n: number) => void } = {
    waitForSeq: (seq) =>
      seq <= current ? Promise.resolve() : new Promise<void>((resolve) => waiters.push({ seq, resolve })),
    refreshAll: vi.fn(),
    isLive: () => live,
    advance: (n: number) => {
      current = n;
      for (const w of waiters.splice(0)) {
        if (w.seq <= n) w.resolve();
        else waiters.push(w);
      }
    },
  };
  return src;
}

/** 404 COMMAND_NOT_FOUND as server.py `_command_result` sends it. */
function notFound(commandId: string, definitive: boolean): ApiError {
  const message = definitive
    ? `no command ${commandId} is recorded for the owner, and the core has finished every request it received before this query`
    : `no result is stored for command ${commandId}. The core did not answer (OSError: timed out)`;
  return new ApiError('GET', `/api/commands/${commandId}`, 404, message, {
    error: message,
    code: 'COMMAND_NOT_FOUND',
    command_id: commandId,
    source: definitive ? 'core' : 'database',
    definitive,
  });
}

function stored(commandId: string, over: Partial<CommandResult> = {}): CommandResult {
  return {
    command_id: commandId,
    kind: 'stop',
    status: 'ACCEPTED',
    accepted: true,
    body: { stop_latched: true },
    events: [7],
    payload_hash: 'h',
    principal_id: 'owner',
    recorded_time_ns: 1761148672134721234,
    recorded_time_ns_str: '1761148672134721234',
    source: 'core',
    ...over,
  };
}

function sender(
  impl: (kind: string, payload: unknown, commandId: string) => Promise<CommandResponse>,
  lookup: (commandId: string) => Promise<CommandResult> = async (id) => {
    throw notFound(id, true);
  },
): CommandSender {
  return {
    sendCommand: (kind, payload, options) => impl(kind, payload, options?.commandId ?? 'missing'),
    commandResult: (id) => lookup(id),
  };
}

const lost = (id: string) => new CommandTransportError('stop', id, new NetworkError('POST', '/api/commands', new TypeError('Failed to fetch')));

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('CommandTracker (no optimistic updates)', () => {
  it('shows the command id while sending, then pending until the stream reaches its events', async () => {
    let release: (r: CommandResponse) => void = () => undefined;
    const client = sender(() => new Promise((r) => (release = r)));
    const seq = seqSource();
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.1' });
    const p = tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(tracker.get('ui.stop.1')).toMatchObject({ status: 'sending', commandId: 'ui.stop.1', label: 'STOP' });
    release({ command_id: 'ui.stop.1', accepted: true, duplicate: false, body: { stop_latched: true }, events: [40, 42] });
    const t = await p;
    expect(t).toMatchObject({ status: 'pending', awaitSeq: 42 });
    seq.advance(41);
    await flush();
    expect(tracker.get('ui.stop.1')?.status).toBe('pending');
    seq.advance(42);
    await flush();
    expect(tracker.get('ui.stop.1')?.status).toBe('applied');
  });

  it('records deterministic rejections with their code', async () => {
    const client = sender(async (_k, _p, id) => ({
      command_id: id,
      accepted: false,
      duplicate: false,
      body: { code: 'PREFLIGHT_FAILED', message: 'RECONCILIATION_REQUIRED', details: { blockers: ['RECONCILIATION_REQUIRED'] } },
      events: [9],
    }));
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.start.1' });
    const t = await tracker.send('start', { account_key: 'SIM-1' }, { label: 'START' });
    expect(t.status).toBe('rejected');
    expect(t.rejection?.code).toBe('PREFLIGHT_FAILED');
    expect(t.error).toBe('PREFLIGHT_FAILED: RECONCILIATION_REQUIRED');
  });

  it('a duplicate (stored result, no new events) is applied immediately and triggers a refetch', async () => {
    const client = sender(async (_k, _p, id) => ({ command_id: id, accepted: true, duplicate: true, body: {} }));
    const seq = seqSource();
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.dup' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t.status).toBe('applied');
    expect(seq.refreshAll).toHaveBeenCalledTimes(1);
  });

  it('an unknown outcome asks for the stored result first; "not recorded" (definitive) offers a resend with the SAME id and payload', async () => {
    const calls: string[] = [];
    let fail = true;
    const client = sender(
      async (_k, payload, id) => {
        calls.push(`POST ${id}:${JSON.stringify(payload)}`);
        if (fail) throw lost(id);
        return { command_id: id, accepted: true, duplicate: true, body: {} };
      },
      async (id) => {
        calls.push(`GET ${id}`);
        throw notFound(id, true);
      },
    );
    const seq = seqSource();
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.net' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    // The send resolves after the lookup: not executed, and known to be (the core finished every earlier request).
    expect(t).toMatchObject({ status: 'failed', outcomeUnknown: false, lookup: { state: 'not_recorded', definitive: true } });
    expect(t.error).toBe('stop ui.stop.net was not executed: the core has no record of it.');
    expect(canRetry(t)).toBe(true);
    expect(canRecheck(t)).toBe(false);
    expect(lookupNote(t)).toMatch(/^Not executed: the core has no record of this command id/);
    expect(failedText(t)).toBe('not executed');
    expect(seq.refreshAll).toHaveBeenCalled();
    expect(calls).toEqual(['POST ui.stop.net:{"account_key":"SIM-1"}', 'GET ui.stop.net']);
    fail = false;
    const r = await tracker.retry('ui.stop.net');
    expect(r?.status).toBe('applied');
    expect(calls).toEqual(['POST ui.stop.net:{"account_key":"SIM-1"}', 'GET ui.stop.net', 'POST ui.stop.net:{"account_key":"SIM-1"}']);
  });

  it('an unknown outcome the core DID record is adopted from the stored result: pending, then applied; nothing is resent', async () => {
    const calls: string[] = [];
    const client = sender(
      async (_k, _p, id) => {
        calls.push(`POST ${id}`);
        throw lost(id);
      },
      async (id) => {
        calls.push(`GET ${id}`);
        return stored(id, { events: [40, 42], source: 'core' });
      },
    );
    const seq = seqSource();
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.rec' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t).toMatchObject({ status: 'pending', awaitSeq: 42, outcomeUnknown: false, error: null, lookup: { state: 'found', source: 'core' } });
    expect(t.response).toMatchObject({ command_id: 'ui.stop.rec', accepted: true, duplicate: true, body: { stop_latched: true } });
    expect(lookupNote(t)).toBe('Outcome recovered from the stored result (GET /api/commands/<id>, answered by the core); nothing was resent.');
    expect(canRetry(t)).toBe(false);
    seq.advance(42);
    await flush();
    expect(tracker.get('ui.stop.rec')?.status).toBe('applied');
    expect(calls).toEqual(['POST ui.stop.rec', 'GET ui.stop.rec']);
  });

  it('a stored rejection found after an unknown outcome is shown as the rejection with its code', async () => {
    const rejection = { code: 'PREFLIGHT_FAILED', message: 'RECONCILIATION_REQUIRED', details: { blockers: ['RECONCILIATION_REQUIRED'] } };
    const client = sender(
      async (_k, _p, id) => {
        throw new CommandTransportError('start', id, new ApiError('POST', '/api/commands', 504, 'no answer from the core', { code: 'COMMAND_OUTCOME_UNKNOWN', outcome: 'UNKNOWN' }));
      },
      async (id) => stored(id, { kind: 'start', status: 'REJECTED', accepted: false, body: rejection, events: [9], source: 'database' }),
    );
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.start.rej' });
    const t = await tracker.send('start', { account_key: 'SIM-1' }, { label: 'START' });
    expect(t).toMatchObject({ status: 'rejected', rejection, error: 'PREFLIGHT_FAILED: RECONCILIATION_REQUIRED', lookup: { state: 'found', source: 'database' } });
  });

  it('a provisional "not recorded" keeps the outcome unknown; a retry queries again before it resends with the same id', async () => {
    const calls: string[] = [];
    const client = sender(
      async (_k, _p, id) => {
        calls.push(`POST ${id}`);
        if (calls.length === 1) throw lost(id);
        return { command_id: id, accepted: true, duplicate: false, body: {}, events: [5] };
      },
      async (id) => {
        calls.push(`GET ${id}`);
        throw notFound(id, false);
      },
    );
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.stop.prov' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t).toMatchObject({ status: 'failed', outcomeUnknown: true, lookup: { state: 'not_recorded', definitive: false } });
    expect(lookupNote(t)).toMatch(/^Still unknown: no result is stored for command ui\.stop\.prov\. The core did not answer/);
    expect(failedText(t)).toBe('outcome unknown');
    expect(canRetry(t)).toBe(true);
    expect(canRecheck(t)).toBe(true);
    const r = await tracker.retry('ui.stop.prov');
    expect(calls).toEqual(['POST ui.stop.prov', 'GET ui.stop.prov', 'GET ui.stop.prov', 'POST ui.stop.prov']);
    expect(r).toMatchObject({ status: 'pending', awaitSeq: 5, lookup: null });
  });

  it('a retry whose lookup now finds the result adopts it instead of resending', async () => {
    const calls: string[] = [];
    let recorded = false;
    const client = sender(
      async (_k, _p, id) => {
        calls.push(`POST ${id}`);
        throw lost(id);
      },
      async (id) => {
        calls.push(`GET ${id}`);
        if (!recorded) throw notFound(id, false);
        return stored(id, { events: [3] });
      },
    );
    const seq = seqSource();
    seq.advance(10);
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.late' });
    await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    recorded = true; // the core ran the request it held
    const r = await tracker.retry('ui.stop.late');
    await flush();
    expect(tracker.get('ui.stop.late')?.status).toBe('applied');
    expect(r?.lookup).toMatchObject({ state: 'found' });
    expect(calls).toEqual(['POST ui.stop.late', 'GET ui.stop.late', 'GET ui.stop.late']);
  });

  it('a lookup that fails keeps the outcome unknown; recheck asks again without resending', async () => {
    const calls: string[] = [];
    let apiDown = true;
    const client = sender(
      async (_k, _p, id) => {
        calls.push(`POST ${id}`);
        throw lost(id);
      },
      async (id) => {
        calls.push(`GET ${id}`);
        if (apiDown) throw new NetworkError('GET', `/api/commands/${id}`, new TypeError('Failed to fetch'));
        return stored(id, { events: [] });
      },
    );
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.stop.down' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t).toMatchObject({ status: 'failed', outcomeUnknown: true, lookup: { state: 'failed' } });
    expect(lookupNote(t)).toMatch(/^Outcome still unknown: the stored result could not be read \(API unreachable for GET/);
    expect(canRecheck(t)).toBe(true);
    apiDown = false;
    const r = await tracker.recheck('ui.stop.down');
    expect(r).toMatchObject({ status: 'applied', lookup: { state: 'found' } });
    expect(calls).toEqual(['POST ui.stop.down', 'GET ui.stop.down', 'GET ui.stop.down']);
    expect(tracker.recheck('ui.stop.down')).toBeNull();
  });

  it('offers no retry while the lookup is running', async () => {
    let answer: (r: CommandResult) => void = () => undefined;
    const client = sender(
      async (_k, _p, id) => {
        throw lost(id);
      },
      () => new Promise<CommandResult>((r) => (answer = r)),
    );
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.stop.wait' });
    const p = tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    await flush();
    const during = tracker.get('ui.stop.wait')!;
    expect(during).toMatchObject({ status: 'failed', outcomeUnknown: true, lookup: { state: 'querying' } });
    expect(canRetry(during)).toBe(false);
    expect(canRecheck(during)).toBe(false);
    expect(tracker.retry('ui.stop.wait')).toBeNull();
    expect(lookupNote(during)).toMatch(/^Outcome unknown: asking the API for the stored result/);
    expect(failedText(during)).toBe('outcome unknown; checking the stored result');
    answer(stored('ui.stop.wait', { events: [] }));
    expect((await p).status).toBe('applied');
  });

  it('never adopts a stored result of another kind', async () => {
    const client = sender(
      async (_k, _p, id) => {
        throw lost(id);
      },
      async (id) => stored(id, { kind: 'start' }),
    );
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.stop.kind' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t).toMatchObject({ status: 'failed', outcomeUnknown: true, lookup: { state: 'failed', message: 'the stored command ui.stop.kind is a start, not a stop' } });
  });

  it('a 409 command id conflict is a definitive refusal with the API code: rejected, never looked up or retried', async () => {
    const calls: string[] = [];
    const body = {
      error:
        'command ui.stop.c reused with a different payload; the original command keeps its result and this request was not executed. Use a new command id for a different command.',
      code: 'COMMAND_ID_CONFLICT',
      command_id: 'ui.stop.c',
      outcome: 'NOT_EXECUTED',
      difference: 'payload',
    };
    const client = sender(
      async (_k, _p, id) => {
        calls.push(`POST ${id}`);
        throw new CommandTransportError('stop', id, new ApiError('POST', '/api/commands', 409, body.error, body));
      },
      async (id) => {
        calls.push(`GET ${id}`);
        return stored(id);
      },
    );
    const seq = seqSource();
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.c' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t).toMatchObject({
      status: 'rejected',
      outcomeUnknown: false,
      lookup: null,
      rejection: { code: 'COMMAND_ID_CONFLICT', message: body.error, details: { http_status: 409, outcome: 'NOT_EXECUTED', difference: 'payload' } },
    });
    expect(t.error).toBe(`COMMAND_ID_CONFLICT: ${body.error}`);
    expect(canRetry(t)).toBe(false);
    expect(tracker.retry('ui.stop.c')).toBeNull();
    expect(calls).toEqual(['POST ui.stop.c']);
    expect(seq.refreshAll).not.toHaveBeenCalled();
  });

  it('a refusal before the core is failed but not outcome-unknown', async () => {
    const client = sender(async (_k, _p, id) => {
      throw new CommandTransportError('stop', id, new ApiError('POST', '/api/commands', 400, 'payload must be an object', null));
    });
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.stop.bad' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t).toMatchObject({ status: 'failed', outcomeUnknown: false });
    expect(tracker.retry('ui.stop.bad')).not.toBeNull();
  });

  it('a cancelled step-up is recorded as cancelled (nothing executed)', async () => {
    const client = sender(async (_k, _p, id) => {
      throw new StepUpCancelledError(id);
    });
    const tracker = new CommandTracker(client, seqSource(), { newCommandId: () => 'ui.start.c' });
    const t = await tracker.send('start', { account_key: 'SIM-1' }, { label: 'START' });
    expect(t.status).toBe('cancelled');
    expect(tracker.retry('ui.start.c')).toBeNull();
  });

  it('refreshes directly when the stream is not live at accept time', async () => {
    const client = sender(async (_k, _p, id) => ({ command_id: id, accepted: true, duplicate: false, body: {}, events: [3] }));
    const seq = seqSource(false);
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.offline' });
    await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(seq.refreshAll).toHaveBeenCalledTimes(1);
    expect(tracker.get('ui.stop.offline')?.status).toBe('pending');
  });
});
