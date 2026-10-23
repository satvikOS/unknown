/**
 * The runtime's own wiring of the state stream to the query cache (createRuntime), not a copy of it: a new seq
 * or a gap refetches data on core state only; a reset, a reconnect and a manual refresh refetch research data too.
 */
import { describe, expect, it } from 'vitest';
import { createRuntime } from './runtime';
import type { EventSourceLike } from './stream';

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('createRuntime: stream version bumps and seq-driven queries', () => {
  it('a core seq or gap does not refetch research data; reset, reconnect and refresh-all do', async () => {
    const counts = new Map<string, number>();
    const fetchImpl = (async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://terminal.test').pathname;
      counts.set(path, (counts.get(path) ?? 0) + 1);
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
    }) as typeof fetch;
    let es: (EventSourceLike & { fire: (seq: number) => void; openNow: () => void; failNow: () => void }) | null = null;
    const runtime = createRuntime({
      fetchImpl,
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
    const RESEARCH = '/api/company/9000001';
    runtime.queries.subscribe('/api/overview', undefined, () => undefined);
    runtime.queries.subscribe(RESEARCH, undefined, () => undefined, { seqDriven: false });
    runtime.stream.start();
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
    runtime.stream.refreshAll(); // manual
    await flush();
    expect(seen()).toEqual([6, 3]);
    es!.failNow(); // interrupted; the browser reconnects
    es!.openNow();
    await flush();
    expect(seen()).toEqual([7, 4]);
    runtime.stream.stop();
  });
});
