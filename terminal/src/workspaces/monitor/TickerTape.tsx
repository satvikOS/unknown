/** TickerTape: exported for the header (the coordinator wires it in); see panels.tsx for the monitor panels. */
import { useMarketData, useMarketDataKeys } from '../../state/marketdata';
import { setLinkedSecurity } from '../../ui/link';
import { DEFAULT_KEYS, rowFor, splitKey } from './model';
import { fmt, signTone } from './panels';
import './monitor.css';

const DASH = '—';

/**
 * Ticker tape for the header (the coordinator wires it in): symbol, last and %chg with each source's label.
 * Clicking an item links the security. Motion stops under prefers-reduced-motion.
 */
export function TickerTape({ keys = DEFAULT_KEYS }: { keys?: readonly string[] }) {
  const client = useMarketData();
  useMarketDataKeys(keys);
  const items = (copy: boolean) => keys.map((key) => {
    const r = rowFor(key, client.quote(key));
    return (
      <button
        key={`${copy ? 'b' : 'a'}${key}`}
        type="button"
        className="mon-tick"
        onClick={() => setLinkedSecurity(key)}
        title={r.badge.title}
        aria-hidden={copy || undefined}
        tabIndex={copy ? -1 : undefined}
      >
        <span className="mon-sym">{splitKey(key)?.symbol ?? key}</span>
        <span className="mon-num">{fmt(r.last)}</span>
        <span className={`mon-num ${signTone(r.pct)}`}>{r.pct === null ? DASH : `${fmt(r.pct, 2)}%`}</span>
        {r.badge.text === 'SYNTHETIC' || r.badge.tone !== 'ok' ? <span className="mon-tick-badge">{r.badge.text}</span> : null}
      </button>
    );
  });
  return (
    <div className="mon-tape-bar" role="marquee" aria-label="Ticker tape">
      <div className="mon-tape-track">
        {items(false)}
        {items(true)}
      </div>
    </div>
  );
}
