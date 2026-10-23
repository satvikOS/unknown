/**
 * Market-data stream health for the status bar. The shell knows nothing about any data source or broker: a
 * market-data client registers a provider (registerStreamHealthProvider) and useStreamHealth() returns null until
 * one does. Fields follow docs/design/marketdata-stream-contract.md.
 */
import { useSyncExternalStore } from 'react';
import type { DataType } from './live';

export interface StreamSourceHealth {
  /** Provider id as sent on the wire (`source`), e.g. the broker or vendor adapter id. */
  source: string;
  dataType: DataType;
  /** Keys currently subscribed from this source with this data type. */
  keys: number;
}

export interface StreamHealth {
  state: 'connecting' | 'live' | 'stale' | 'down';
  sources: readonly StreamSourceHealth[];
  /** Publish-to-paint latency in milliseconds (null until measured). */
  latencyMs: { p50: number | null; p99: number | null } | null;
  /** Wall-clock ms of the last event or heartbeat. */
  lastEventMs: number | null;
  /** Keys refused (LINE_BUDGET, NOT_ENTITLED, UNAVAILABLE). */
  refused?: number;
  detail?: string;
}

export interface StreamHealthProvider {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => StreamHealth;
}

let provider: StreamHealthProvider | null = null;
const listeners = new Set<() => void>();
let unsubProvider: (() => void) | null = null;

function notify(): void {
  for (const l of [...listeners]) l();
}

/** Install the market-data client's health provider; returns a function that removes it. */
export function registerStreamHealthProvider(p: StreamHealthProvider): () => void {
  unsubProvider?.();
  provider = p;
  unsubProvider = p.subscribe(notify);
  notify();
  return () => {
    if (provider !== p) return;
    unsubProvider?.();
    unsubProvider = null;
    provider = null;
    notify();
  };
}

export function streamHealth(): StreamHealth | null {
  return provider ? provider.getSnapshot() : null;
}

/** The registered provider's snapshot, or null when no market-data client is registered. */
export function useStreamHealth(): StreamHealth | null {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    streamHealth,
    () => null,
  );
}
