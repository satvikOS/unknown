import { describe, expect, it, vi } from 'vitest';
import { QueryCache, queryKey } from './queries';
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
