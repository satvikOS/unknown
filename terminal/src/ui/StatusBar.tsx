/**
 * Status bar along the bottom: market-data stream state with one source/data-type badge per source, publish-to-paint
 * latency, refused keys, and the link channel and security the command line targets. Market-data fields come from
 * useStreamHealth(), which is null until a market-data client registers a provider: the bar then says so instead
 * of inventing a state. No broker or source is assumed.
 */
import type { ReactNode } from 'react';
import { useCommandTarget } from './CommandLine';
import { channelInfo, isUnlinked, useLinkedSecurity } from './link';
import { DataTypeBadge } from './live';
import { useUpDown } from './marketPrefs';
import { StatusPill, type Tone } from './primitives';
import { useStreamHealth, type StreamHealth } from './streamHealth';
import { NoValue } from './unavailable';

const STATE_TONE: Record<StreamHealth['state'], Tone> = { live: 'ok', connecting: 'info', stale: 'warn', down: 'bad' };
const STATE_LABEL: Record<StreamHealth['state'], string> = { live: 'MD STREAMING', connecting: 'MD CONNECTING', stale: 'MD STALE', down: 'MD DOWN' };

function ms(v: number | null | undefined): ReactNode {
  return v == null ? <NoValue reason="NOT_REPORTED" /> : v < 10 ? v.toFixed(1) : String(Math.round(v));
}

function MarketData() {
  const h = useStreamHealth();
  if (!h) {
    return (
      <span className="sb-item muted" title="No market-data client is registered in this build, so no stream state is shown.">
        Market data: no client registered
      </span>
    );
  }
  return (
    <>
      <span className="sb-item">
        <StatusPill label={STATE_LABEL[h.state]} tone={STATE_TONE[h.state]} detail={h.detail ?? `Market-data stream ${h.state}`} compact />
      </span>
      {h.sources.map((s) => (
        <span key={`${s.source}|${s.dataType}`} className="sb-item">
          <DataTypeBadge type={s.dataType} source={s.source} />
          <span className="num muted">{s.keys}</span>
        </span>
      ))}
      <span className="sb-item num" title="Publish-to-paint latency of market-data events">
        <span className="muted">lat p50</span> {ms(h.latencyMs?.p50)} <span className="muted">p99</span> {ms(h.latencyMs?.p99)} <span className="muted">ms</span>
      </span>
      {h.refused ? (
        <span className="sb-item">
          <StatusPill label={`${h.refused} REFUSED`} tone="warn" detail="Keys refused by the line budget, entitlements or the source" compact />
        </span>
      ) : null}
    </>
  );
}

export function StatusBar() {
  const target = useCommandTarget();
  const channel = target?.channel ?? 'default';
  const security = useLinkedSecurity(channel);
  const info = channelInfo(channel);
  const upDown = useUpDown();
  return (
    <footer className="statusbar" aria-label="Status bar">
      <MarketData />
      <span className="sb-spacer" />
      <span className="sb-item hide-sm" title="Price colour convention (change it in the command palette)">
        <span className="tick-up">▲</span> <span className="muted">{upDown === 'green-up' ? 'green' : 'red'}</span>{' '}
        <span className="tick-down">▼</span> <span className="muted">{upDown === 'green-up' ? 'red' : 'green'}</span>
      </span>
      <span className="sb-item" title={`Commands go to ${target?.label ?? 'nowhere yet'}`}>
        <span className="link-chip-dot" style={{ ['--chip' as string]: info?.color ?? 'var(--text-muted)' }} aria-hidden="true" />
        <span className="muted">{isUnlinked(channel) ? 'Unlinked' : info ? `Ch ${info.num}` : channel}</span>
        <span className="num">{security ?? <NoValue reason="NOT_REPORTED" />}</span>
      </span>
    </footer>
  );
}
