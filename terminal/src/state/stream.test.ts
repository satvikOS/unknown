import { describe, expect, it, vi } from 'vitest';
import { EVENT_SOURCE_CLOSED, EVENT_SOURCE_CONNECTING, StreamStore, type EventSourceLike } from './stream';

class FakeEventSource implements EventSourceLike {
  readyState = EVENT_SOURCE_CONNECTING;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, ((ev: MessageEvent) => void)[]>();
  constructor(readonly url: string) {}
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close(): void {
    this.closed = true;
    this.readyState = EVENT_SOURCE_CLOSED;
  }
  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }
  seq(n: number): void {
    for (const l of this.listeners.get('seq') ?? []) l({ data: JSON.stringify({ seq: n }) } as MessageEvent);
  }
  raw(data: string): void {
    for (const l of this.listeners.get('seq') ?? []) l({ data } as MessageEvent);
  }
  /** Browser-style transient error: it will retry by itself. */
  drop(): void {
    this.readyState = EVENT_SOURCE_CONNECTING;
    this.onerror?.(new Event('error'));
  }
  /** HTTP error (e.g. 401): the browser gives up. */
  refuse(): void {
    this.readyState = EVENT_SOURCE_CLOSED;
    this.onerror?.(new Event('error'));
  }
}

function setup(opts: { onConnectionRefused?: () => void } = {}) {
  const sources: FakeEventSource[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const store = new StreamStore({
    url: '/api/stream',
    createEventSource: (url) => {
      const es = new FakeEventSource(url);
      sources.push(es);
      return es;
    },
    now: () => 1000,
    setTimer: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimer: () => undefined,
    ...opts,
  });
  return { store, sources, timers };
}

describe('StreamStore sequencing', () => {
  it('bumps the version on every new seq and records the reason', () => {
    const { store, sources } = setup();
    store.start();
    const es = sources[0]!;
    es.open();
    expect(store.getSnapshot().status).toBe('live');
    const v0 = store.getSnapshot().version;
    es.seq(13);
    expect(store.getSnapshot()).toMatchObject({ seq: 13, version: v0 + 1, lastReason: 'seq' });
    es.seq(14);
    expect(store.getSnapshot()).toMatchObject({ seq: 14, version: v0 + 2, lastReason: 'seq', gaps: 0 });
  });

  it('a repeated seq does not refetch', () => {
    const { store, sources } = setup();
    store.start();
    sources[0]!.open();
    sources[0]!.seq(5);
    const v = store.getSnapshot().version;
    sources[0]!.seq(5);
    expect(store.getSnapshot().version).toBe(v);
  });

  it('detects gaps (coalesced or missed notifications) and refetches', () => {
    const { store, sources } = setup();
    store.start();
    sources[0]!.open();
    sources[0]!.seq(10);
    const v = store.getSnapshot().version;
    sources[0]!.seq(17);
    expect(store.getSnapshot()).toMatchObject({ seq: 17, gaps: 1, lastReason: 'gap', version: v + 1 });
  });

  it('treats a lower seq as a reset (restored database) and refetches', () => {
    const { store, sources } = setup();
    store.start();
    sources[0]!.open();
    sources[0]!.seq(50);
    sources[0]!.seq(3);
    expect(store.getSnapshot()).toMatchObject({ seq: 3, resets: 1, lastReason: 'reset' });
  });

  it('refetches everything on reconnect after a dropped connection', () => {
    const { store, sources } = setup();
    store.start();
    const es = sources[0]!;
    es.open();
    es.seq(20);
    es.drop();
    expect(store.getSnapshot().status).toBe('reconnecting');
    const v = store.getSnapshot().version;
    es.open(); // the browser reconnected the same EventSource
    expect(store.getSnapshot()).toMatchObject({ status: 'live', reconnects: 1, lastReason: 'reconnect', version: v + 1 });
    // The server resends the current seq on connect; unchanged seq does not refetch twice.
    es.seq(20);
    expect(store.getSnapshot().version).toBe(v + 1);
  });

  it('reconnects itself with backoff when the browser gives up, and checks the session', () => {
    const refused = vi.fn();
    const { store, sources, timers } = setup({ onConnectionRefused: refused });
    store.start();
    sources[0]!.open();
    sources[0]!.seq(7);
    sources[0]!.refuse();
    expect(refused).toHaveBeenCalledTimes(1);
    expect(sources[0]!.closed).toBe(true);
    expect(timers.map((t) => t.ms)).toEqual([1000]);
    timers[0]!.fn();
    expect(sources).toHaveLength(2);
    sources[1]!.refuse();
    expect(timers.map((t) => t.ms)).toEqual([1000, 2000]);
    timers[1]!.fn();
    const v = store.getSnapshot().version;
    sources[2]!.open();
    expect(store.getSnapshot()).toMatchObject({ status: 'live', lastReason: 'reconnect', version: v + 1 });
    sources[2]!.seq(9);
    expect(store.getSnapshot()).toMatchObject({ seq: 9, gaps: 1 });
  });

  it('ignores malformed notifications but reports them', () => {
    const { store, sources } = setup();
    store.start();
    sources[0]!.open();
    const v = store.getSnapshot().version;
    sources[0]!.raw('not json');
    sources[0]!.raw('{"seq": -1}');
    expect(store.getSnapshot().version).toBe(v);
    expect(store.getSnapshot().lastError).toContain('malformed');
  });

  it('waitForSeq resolves once the stream reaches the target', async () => {
    const { store, sources } = setup();
    store.start();
    sources[0]!.open();
    sources[0]!.seq(3);
    let done = false;
    const p = store.waitForSeq(5).then(() => {
      done = true;
    });
    sources[0]!.seq(4);
    await Promise.resolve();
    expect(done).toBe(false);
    sources[0]!.seq(6);
    await p;
    expect(done).toBe(true);
    await expect(store.waitForSeq(2)).resolves.toBeUndefined();
  });

  it('manual refresh bumps the version; stop closes the source', () => {
    const { store, sources } = setup();
    store.start();
    sources[0]!.open();
    const v = store.getSnapshot().version;
    store.refreshAll();
    expect(store.getSnapshot()).toMatchObject({ version: v + 1, lastReason: 'manual' });
    store.stop();
    expect(sources[0]!.closed).toBe(true);
    expect(store.getSnapshot().status).toBe('idle');
  });
});
