import { describe, expect, it, vi } from 'vitest';
import { ApiError, CommandTransportError, NetworkError, StepUpCancelledError } from '../api/client';
import type { CommandResponse } from '../api/types';
import { CommandTracker, type CommandSender, type SeqSource } from './commands';

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

function sender(impl: (kind: string, payload: unknown, commandId: string) => Promise<CommandResponse>): CommandSender {
  return {
    sendCommand: (kind, payload, options) => impl(kind, payload, options?.commandId ?? 'missing'),
  };
}

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

  it('an unknown outcome is failed + retryable with the SAME id and payload', async () => {
    const calls: string[] = [];
    let fail = true;
    const client = sender(async (_k, payload, id) => {
      calls.push(`${id}:${JSON.stringify(payload)}`);
      if (fail) throw new CommandTransportError('stop', id, new NetworkError('POST', '/api/commands', new TypeError('Failed to fetch')));
      return { command_id: id, accepted: true, duplicate: true, body: {} };
    });
    const seq = seqSource();
    const tracker = new CommandTracker(client, seq, { newCommandId: () => 'ui.stop.net' });
    const t = await tracker.send('stop', { account_key: 'SIM-1' }, { label: 'STOP' });
    expect(t).toMatchObject({ status: 'failed', outcomeUnknown: true });
    expect(seq.refreshAll).toHaveBeenCalled();
    fail = false;
    const r = await tracker.retry('ui.stop.net');
    expect(r?.status).toBe('applied');
    expect(calls).toEqual(['ui.stop.net:{"account_key":"SIM-1"}', 'ui.stop.net:{"account_key":"SIM-1"}']);
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
