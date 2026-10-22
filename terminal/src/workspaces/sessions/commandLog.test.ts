import { describe, expect, it, vi } from 'vitest';
import { findWorkspace } from '../registry';
import { createdSessionId, isInFlight, SessionCommandLog } from './commandLog';

describe('SessionCommandLog', () => {
  it('keeps the newest command per session first and notifies listeners', () => {
    const log = new SessionCommandLog(3);
    const listener = vi.fn();
    log.subscribe(listener);
    log.record({ commandId: 'c1', kind: 'start_session', sessionId: 's1', atMs: 1 });
    log.record({ commandId: 'c2', kind: 'suspend_session', sessionId: 's1', atMs: 2 });
    log.record({ commandId: 'c3', kind: 'cancel_session', sessionId: 's2', atMs: 3 });
    expect(log.latestFor('s1')?.commandId).toBe('c2');
    expect(log.forSession('s1').map((r) => r.commandId)).toEqual(['c2', 'c1']);
    expect(listener).toHaveBeenCalledTimes(3);
    log.record({ commandId: 'c4', kind: 'end_session', sessionId: 's3', atMs: 4 });
    expect(log.getSnapshot().map((r) => r.commandId)).toEqual(['c4', 'c3', 'c2']); // bounded
  });

  it('binds a create_session command to the session the core returned', () => {
    const log = new SessionCommandLog();
    log.record({ commandId: 'c1', kind: 'create_session', sessionId: null, atMs: 1 });
    expect(log.latestFor('ses-new')).toBeUndefined();
    log.bind('c1', 'ses-new');
    expect(log.latestFor('ses-new')?.kind).toBe('create_session');
    const snap = log.getSnapshot();
    log.bind('c1', 'ses-new'); // no-op keeps the snapshot identity
    expect(log.getSnapshot()).toBe(snap);
    log.bind('missing', 'x');
    expect(log.getSnapshot()).toBe(snap);
  });

  it('re-recording the same id replaces it rather than duplicating', () => {
    const log = new SessionCommandLog();
    log.record({ commandId: 'c1', kind: 'start_session', sessionId: 's1', atMs: 1 });
    log.record({ commandId: 'c1', kind: 'start_session', sessionId: 's1', atMs: 2 });
    expect(log.getSnapshot()).toHaveLength(1);
    log.clear();
    expect(log.getSnapshot()).toEqual([]);
  });
});

describe('command helpers', () => {
  it('treats sending and pending as in flight only', () => {
    expect(isInFlight({ status: 'sending' })).toBe(true);
    expect(isInFlight({ status: 'pending' })).toBe(true);
    for (const s of ['applied', 'rejected', 'failed', 'cancelled'] as const) expect(isInFlight({ status: s })).toBe(false);
    expect(isInFlight(undefined)).toBe(false);
  });

  it('reads the new session id only from an accepted response', () => {
    const resp = (accepted: boolean, body: Record<string, unknown>) => ({ response: { command_id: 'c', accepted, duplicate: false, body } });
    expect(createdSessionId(resp(true, { session_id: 'ses-1', state: 'WAITING_FUNDS' }))).toBe('ses-1');
    expect(createdSessionId(resp(false, { code: 'MANDATE_INVALID', message: 'm', details: {} }))).toBeNull();
    expect(createdSessionId(resp(true, {}))).toBeNull();
    expect(createdSessionId({ response: null })).toBeNull();
  });
});

describe('sessions workspace registration', () => {
  it('is registered with its reserved shortcut and order', () => {
    const w = findWorkspace('sessions');
    expect(w).toMatchObject({ id: 'sessions', shortcut: 'g s', order: 20, icon: 'sessions' });
  });
});
