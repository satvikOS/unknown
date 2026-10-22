import { describe, expect, it, vi } from 'vitest';
import { QueryCache, queryKey } from './queries';
import { invalidationScope } from './runtime';
import { StreamStore, type EventSourceLike } from './stream';

/** A fetcher whose responses the test resolves by hand. */
function controllable() {
  const pending: { endpoint: string; resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = [];
  const fetcher = vi.fn(
    (endpoint: string) =>
      new Promise<unknown>((resolve, reject) => {
        pending.push({ endpoint, resolve, reject });
      }),
  );
  return { fetcher, pending };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('queryKey', () => {
  it('is stable across param order and ignores empty values', () => {
    expect(queryKey('/api/orders', { state: 'open', limit: 5 })).toBe(queryKey('/api/orders', { limit: 5, state: 'open', session: undefined }));
    expect(queryKey('/api/overview')).toBe('/api/overview');
  });
});

describe('QueryCache', () => {
  it('fetches on first subscribe and shares one entry between subscribers', async () => {
    const { fetcher, pending } = controllable();
    const cache = new QueryCache({ fetcher, now: () => 500, currentSeq: () => 13 });
    const a = vi.fn();
    const b = vi.fn();
    cache.subscribe('/api/overview', undefined, a);
    cache.subscribe('/api/overview', undefined, b);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(cache.getState('/api/overview')).toMatchObject({ status: 'loading', fetching: true });
    pending[0]!.resolve({ latest_seq: 13 });
    await flush();
    expect(cache.getState('/api/overview')).toMatchObject({
      status: 'success',
      data: { latest_seq: 13 },
      fetching: false,
      fetchedAtMs: 500,
      seqAtFetch: 13,
    });
    expect(a).toHaveBeenCalled();
    expect(b).toHaveBeenCalled();
  });

  it('refetches every subscribed entry when invalidated, keeping old data visible meanwhile', async () => {
    const { fetcher, pending } = controllable();
    const cache = new QueryCache({ fetcher });
    const unsub = cache.subscribe('/api/overview', undefined, () => undefined);
    cache.subscribe('/api/health', undefined, () => undefined);
    pending[0]!.resolve('o1');
    pending[1]!.resolve('h1');
    await flush();
    cache.invalidateAll();
    expect(fetcher).toHaveBeenCalledTimes(4);
    expect(cache.getState('/api/overview')).toMatchObject({ data: 'o1', fetching: true, status: 'success' });
    pending[2]!.resolve('o2');
    pending[3]!.resolve('h2');
    await flush();
    expect(cache.getState('/api/overview').data).toBe('o2');
    // Unsubscribed entries are not fetched on invalidation, but are on the next subscribe.
    unsub();
    cache.invalidateAll();
    expect(fetcher).toHaveBeenCalledTimes(5);
    expect(pending[4]!.endpoint).toBe('/api/health');
    cache.subscribe('/api/overview', undefined, () => undefined);
    expect(fetcher).toHaveBeenCalledTimes(6);
  });

  it('a bump during an in-flight fetch schedules exactly one more fetch (no stale final state)', async () => {
    const { fetcher, pending } = controllable();
    const cache = new QueryCache({ fetcher });
    cache.subscribe('/api/overview', undefined, () => undefined);
    // Three seq notifications arrive while the first fetch is still running.
    cache.invalidateAll();
    cache.invalidateAll();
    cache.invalidateAll();
    expect(fetcher).toHaveBeenCalledTimes(1);
    pending[0]!.resolve('pre-bump');
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(cache.getState('/api/overview')).toMatchObject({ data: 'pre-bump', fetching: true });
    pending[1]!.resolve('post-bump');
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(cache.getState('/api/overview')).toMatchObject({ data: 'post-bump', fetching: false });
  });

  it('keeps the last good data and exposes the error when a refetch fails', async () => {
    const { fetcher, pending } = controllable();
    const cache = new QueryCache({ fetcher });
    cache.subscribe('/api/overview', undefined, () => undefined);
    pending[0]!.resolve('good');
    await flush();
    cache.invalidateAll();
    pending[1]!.reject(new Error('core unreachable'));
    await flush();
    const s = cache.getState('/api/overview');
    expect(s.status).toBe('error');
    expect(s.data).toBe('good');
    expect((s.error as Error).message).toBe('core unreachable');
  });

  it('polls at the shortest interval any subscriber asked for, and stops when they leave', async () => {
    const intervals: { fn: () => void; ms: number; cleared: boolean }[] = [];
    const fetcher = vi.fn(async () => 'q');
    const cache = new QueryCache({
      fetcher,
      setInterval: (fn, ms) => {
        intervals.push({ fn, ms, cleared: false });
        return intervals.length - 1;
      },
      clearInterval: (h) => {
        intervals[h as number]!.cleared = true;
      },
    });
    const u1 = cache.subscribe('/api/quotes', undefined, () => undefined, 5000);
    const u2 = cache.subscribe('/api/quotes', undefined, () => undefined, 2000);
    const active = () => intervals.filter((i) => !i.cleared);
    expect(active().map((i) => i.ms)).toEqual([2000]);
    await flush();
    active()[0]!.fn();
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(2);
    u2();
    expect(active().map((i) => i.ms)).toEqual([5000]);
    u1();
    expect(active()).toEqual([]);
  });

  it('end to end: a stream seq notification refetches subscribed queries', async () => {
    let es: (EventSourceLike & { fire: (seq: number) => void; openNow: () => void }) | null = null;
    const stream = new StreamStore({
      createEventSource: () => {
        const listeners: ((ev: MessageEvent) => void)[] = [];
        const src = {
          readyState: 0,
          onopen: null as ((ev: Event) => void) | null,
          onerror: null as ((ev: Event) => void) | null,
          addEventListener: (_t: string, l: (ev: MessageEvent) => void) => listeners.push(l),
          close: () => undefined,
          fire: (seq: number) => listeners.forEach((l) => l({ data: JSON.stringify({ seq }) } as MessageEvent)),
          openNow: () => src.onopen?.(new Event('open')),
        };
        es = src;
        return src;
      },
    });
    const fetcher = vi.fn(async () => ({ n: fetcher.mock.calls.length }));
    const cache = new QueryCache({ fetcher, currentSeq: () => stream.getSnapshot().seq });
    let last = stream.getSnapshot().version;
    stream.subscribe(() => {
      const v = stream.getSnapshot().version;
      if (v !== last) {
        last = v;
        cache.invalidateAll();
      }
    });
    cache.subscribe('/api/overview', undefined, () => undefined);
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);
    stream.start();
    es!.openNow();
    es!.fire(13);
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(cache.getState('/api/overview').seqAtFetch).toBe(13);
    es!.fire(13); // no change
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(2);
    es!.fire(20); // gap
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
});

describe('QueryCache: data outside the core event log (seqDriven: false)', () => {
  const RESEARCH = '/api/company/9000001';

  it('a core seq refetches seq-driven entries only; mount, Refresh and an all-scope bump refetch research data too', async () => {
    const calls: string[] = [];
    const cache = new QueryCache({
      fetcher: async (endpoint) => {
        calls.push(endpoint);
        return endpoint;
      },
      currentSeq: () => 77,
    });
    cache.subscribe('/api/overview', undefined, () => undefined);
    cache.subscribe(RESEARCH, { as_of: '2026-10-22' }, () => undefined, { seqDriven: false });
    await flush();
    expect(calls).toEqual(['/api/overview', RESEARCH]);
    // Research data reflects no event-log seq, so it is not labelled with one.
    expect(cache.getState('/api/overview').seqAtFetch).toBe(77);
    expect(cache.getState(`${RESEARCH}?as_of=2026-10-22`).seqAtFetch).toBeNull();

    calls.length = 0;
    cache.invalidate('core'); // a new seq or a gap: the core wrote events
    cache.invalidate('core');
    await flush();
    expect(calls).toEqual(['/api/overview', '/api/overview']);

    calls.length = 0;
    cache.refetch(`${RESEARCH}?as_of=2026-10-22`); // the view's Refresh
    await flush();
    expect(calls).toEqual([RESEARCH]);

    calls.length = 0;
    cache.invalidate('all'); // reconnect, reset or a manual refresh-all
    await flush();
    expect(calls.sort()).toEqual(['/api/overview', RESEARCH].sort());

    calls.length = 0;
    cache.invalidateAll();
    await flush();
    expect(calls).toHaveLength(2);
  });

  it('is seq-driven while any subscriber wants that, and polls when asked', async () => {
    const calls: string[] = [];
    const cache = new QueryCache({
      fetcher: async (endpoint) => {
        calls.push(endpoint);
        return endpoint;
      },
    });
    const research = cache.subscribe(RESEARCH, undefined, () => undefined, { seqDriven: false });
    const core = cache.subscribe(RESEARCH, undefined, () => undefined, {});
    await flush();
    calls.length = 0;
    cache.invalidate('core');
    await flush();
    expect(calls).toEqual([RESEARCH]);
    core();
    calls.length = 0;
    cache.invalidate('core');
    await flush();
    expect(calls).toEqual([]);
    research();
    // Opening the view again fetches once (the data may have changed while nobody looked).
    cache.subscribe(RESEARCH, undefined, () => undefined, { seqDriven: false });
    await flush();
    expect(calls).toEqual([RESEARCH]);
  });

  it('wired to the stream as the runtime does: seq and gap skip research data; reconnect, reset and manual refetch it', async () => {
    let es: (EventSourceLike & { fire: (seq: number) => void; openNow: () => void; failNow: () => void }) | null = null;
    const stream = new StreamStore({
      createEventSource: () => {
        const listeners: ((ev: MessageEvent) => void)[] = [];
        const src = {
          readyState: 1,
          onopen: null as ((ev: Event) => void) | null,
          onerror: null as ((ev: Event) => void) | null,
          addEventListener: (_t: string, l: (ev: MessageEvent) => void) => listeners.push(l),
          close: () => undefined,
          fire: (seq: number) => listeners.forEach((l) => l({ data: JSON.stringify({ seq }) } as MessageEvent)),
          openNow: () => src.onopen?.(new Event('open')),
          failNow: () => src.onerror?.(new Event('error')),
        };
        es = src;
        return src;
      },
    });
    const counts = new Map<string, number>();
    const cache = new QueryCache({
      fetcher: async (endpoint) => {
        counts.set(endpoint, (counts.get(endpoint) ?? 0) + 1);
        return endpoint;
      },
      currentSeq: () => stream.getSnapshot().seq,
    });
    let last = stream.getSnapshot().version;
    stream.subscribe(() => {
      const { version, lastReason } = stream.getSnapshot();
      if (version !== last) {
        last = version;
        cache.invalidate(invalidationScope(lastReason));
      }
    });
    cache.subscribe('/api/overview', undefined, () => undefined);
    cache.subscribe(RESEARCH, undefined, () => undefined, { seqDriven: false });
    stream.start();
    es!.openNow();
    await flush();
    const seen = () => [counts.get('/api/overview') ?? 0, counts.get(RESEARCH) ?? 0];
    expect(seen()).toEqual([1, 1]);
    for (const seq of [10, 11, 15]) {
      es!.fire(seq); // first seq, next seq, a gap
      await flush();
    }
    expect(seen()).toEqual([4, 1]);
    es!.fire(3); // reset: a restored database
    await flush();
    expect(seen()).toEqual([5, 2]);
    stream.refreshAll(); // manual
    await flush();
    expect(seen()).toEqual([6, 3]);
    es!.failNow(); // interrupted; the browser reconnects
    es!.openNow();
    await flush();
    expect(seen()).toEqual([7, 4]);
  });
});
