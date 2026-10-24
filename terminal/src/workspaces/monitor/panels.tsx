/**
 * Monitor panels: flashing cells, the watchlist grid, time & sales, depth and the exported TickerTape.
 * All read the shared market-data client (state/marketdata.ts); none assumes a broker.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { formatDecimal } from '../../format/decimal';
import { formatTimestampMs } from '../../format/time';
import { useMarketData, useMarketDataKeys, type Trade } from '../../state/marketdata';
import { setLinkedSecurity } from '../../ui/link';
import { Badge } from '../../ui/primitives';
import { ErrorValue, InGridProvider, NoValue, StaleValue } from '../../ui/unavailable';
import { cumulative, direction, rowFor, rowMiss, sourceBadge, splitKey, statusTone, type RowMiss, type Watchlist } from './model';
import './monitor.css';

/** A decimal for display; null only when the value is absent (callers render <NoValue> then). */
export function fmt(v: string | null, places?: number): string | null {
  if (v === null) return null;
  return formatDecimal(v, { minFraction: places ?? 0 });
}

/**
 * One watchlist value: the error when the key is in error (never a cached value), the em dash with the row's
 * reason when absent, the value with its age when the key is stale, else the value.
 */
export function valueOrReason(text: string | null, miss: RowMiss, label?: string): ReactNode {
  if (miss.reason === 'ERROR') return <ErrorValue reason="ERROR" detail={miss.detail} label={label} />;
  if (text === null) return <NoValue reason={miss.reason} detail={miss.detail} label={label} />;
  if (miss.staleAgeMs !== undefined) return <StaleValue ageMs={miss.staleAgeMs}>{text}</StaleValue>;
  return text;
}

/** A value cell that flashes up/down when its value changes (CSS animation restarted by a key change). */
export function FlashCell({ value, text, className }: { value: string | null; text: ReactNode; className?: string }) {
  const prev = useRef<string | null>(value);
  const [flash, setFlash] = useState<{ dir: -1 | 1; n: number } | null>(null);
  useEffect(() => {
    const d = direction(prev.current, value);
    prev.current = value;
    if (d === 1 || d === -1) setFlash((f) => ({ dir: d, n: (f?.n ?? 0) + 1 }));
  }, [value]);
  const cls = flash ? (flash.dir > 0 ? 'mon-flash-up' : 'mon-flash-down') : '';
  return (
    <td className={`mon-num ${className ?? ''}`}>
      <span key={flash?.n ?? 0} className={cls}>
        {text}
      </span>
    </td>
  );
}

export function signTone(v: string | null): string {
  if (v === null) return '';
  return v.startsWith('-') ? 'mon-down' : /^0(\.0*)?$/.test(v) ? '' : 'mon-up';
}

export interface WatchlistGridProps {
  list: Watchlist;
  selected: string | null;
  onRemove: (key: string) => void;
  onMove: (key: string, delta: -1 | 1) => void;
}

export function WatchlistGrid({ list, selected, onRemove, onMove }: WatchlistGridProps) {
  const client = useMarketData();
  useMarketDataKeys(list.keys);
  if (!list.keys.length) return <p className="mon-empty">This watchlist is empty. Add a key such as AAPL.XNAS above.</p>;
  return (
    <div className="mon-scroll">
      <table className="mon-table" aria-label={`Watchlist ${list.name}`}>
        <thead>
          <tr>
            <th scope="col">Security</th>
            <th scope="col" className="mon-num">Last</th>
            <th scope="col" className="mon-num">Chg</th>
            <th scope="col" className="mon-num">%Chg</th>
            <th scope="col" className="mon-num">Bid</th>
            <th scope="col" className="mon-num">Ask</th>
            <th scope="col" className="mon-num">Bid sz</th>
            <th scope="col" className="mon-num">Ask sz</th>
            <th scope="col" className="mon-num">Volume</th>
            <th scope="col">Time (UTC)</th>
            <th scope="col">Source</th>
            <th scope="col"><span className="mon-sr">Actions</span></th>
          </tr>
        </thead>
        <InGridProvider>
        <tbody>
          {list.keys.map((key, i) => {
            const q = client.quote(key);
            const st = client.status(key);
            const r = rowFor(key, q);
            const miss = rowMiss(q, st, r.tsMs, Date.now());
            const v = (x: string | null, places?: number, suffix = '') => {
              const t = fmt(x, places);
              return valueOrReason(t === null ? null : `${t}${suffix}`, miss);
            };
            const problem = st && st.state !== 'SUBSCRIBED' ? st : null;
            return (
              <tr
                key={key}
                className={key === selected ? 'mon-selected' : undefined}
                onClick={() => setLinkedSecurity(key)}
                aria-selected={key === selected}
              >
                <th scope="row" className="mon-key">
                  <span className="mon-sym">{splitKey(key)?.symbol ?? key}</span>
                  <span className="mon-mic">{splitKey(key)?.mic ?? ''}</span>
                </th>
                <FlashCell value={r.last} text={v(r.last)} />
                <td className={`mon-num ${signTone(r.chg)}`}>{v(r.chg)}</td>
                <td className={`mon-num ${signTone(r.pct)}`}>{v(r.pct, 2, '%')}</td>
                <FlashCell value={r.bid} text={v(r.bid)} />
                <FlashCell value={r.ask} text={v(r.ask)} />
                <td className="mon-num">{v(r.bidSize)}</td>
                <td className="mon-num">{v(r.askSize)}</td>
                <td className="mon-num">{v(r.volume)}</td>
                <td className="mon-time">
                  {r.tsMs === null ? <NoValue reason={miss.reason} detail={miss.detail} /> : formatTimestampMs(r.tsMs, 'utc', true).slice(11)}
                </td>
                <td>
                  {problem?.state === 'ERROR' ? (
                    <ErrorValue reason="ERROR" detail={problem.reason} />
                  ) : problem ? (
                    <Badge tone={statusTone(problem.state)} title={problem.reason}>
                      {problem.state.replace('_', ' ')}
                    </Badge>
                  ) : (
                    <Badge tone={r.badge.tone} title={r.badge.title}>
                      {r.badge.text}
                    </Badge>
                  )}
                </td>
                <td className="mon-actions" onClick={(e) => e.stopPropagation()}>
                  <button type="button" aria-label={`Move ${key} up`} disabled={i === 0} onClick={() => onMove(key, -1)}>
                    ↑
                  </button>
                  <button
                    type="button"
                    aria-label={`Move ${key} down`}
                    disabled={i === list.keys.length - 1}
                    onClick={() => onMove(key, 1)}
                  >
                    ↓
                  </button>
                  <button type="button" aria-label={`Remove ${key}`} onClick={() => onRemove(key)}>
                    ×
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
        </InGridProvider>
      </table>
    </div>
  );
}

export function TimeAndSales({ securityKey, limit = 60 }: { securityKey: string; limit?: number }) {
  const client = useMarketData();
  useMarketDataKeys([securityKey]);
  const trades: readonly Trade[] = client.tradesFor(securityKey).slice(0, limit);
  const q = client.quote(securityKey);
  const st = client.status(securityKey);
  return (
    <section className="mon-panel" aria-label={`Time and sales ${securityKey}`}>
      <header className="mon-panel-head">
        <h2>Time &amp; sales</h2>
        <span className="mon-panel-key">{securityKey}</span>
        <Badge tone={sourceBadge(q).tone} title={sourceBadge(q).title}>
          {sourceBadge(q).text}
        </Badge>
      </header>
      {st?.state === 'ERROR' ? (
        <p className="mon-empty" role="alert">
          <ErrorValue reason="ERROR" detail={st.reason} /> {st.reason}
        </p>
      ) : trades.length === 0 ? (
        <p className="mon-empty">No prints received for {securityKey} yet.</p>
      ) : (
        <div className="mon-scroll mon-tape">
          <table className="mon-table" aria-label={`Time and sales ${securityKey}`}>
            <thead>
              <tr>
                <th scope="col">Time (UTC)</th>
                <th scope="col" className="mon-num">Price</th>
                <th scope="col" className="mon-num">Size</th>
                <th scope="col">Venue</th>
                <th scope="col">Cond.</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => {
                const next = trades[i + 1];
                const d = next ? direction(next.price, t.price) : null;
                const ts = t.ts_source_ns ?? t.ts_recv_ns;
                return (
                  <tr key={t.seq}>
                    <td className="mon-time">{ts === null ? <NoValue reason="NOT_REPORTED" /> : formatTimestampMs(Number(ts) / 1e6, 'utc', true).slice(11)}</td>
                    <td className={`mon-num ${d === 1 ? 'mon-up' : d === -1 ? 'mon-down' : ''}`}>{fmt(t.price)}</td>
                    <td className="mon-num">{fmt(t.size) ?? <NoValue reason="NOT_REPORTED" />}</td>
                    <td>{t.exchange ?? <NoValue reason="NOT_REPORTED" />}</td>
                    <td className="mon-cond">{t.conditions.join(' ') || <NoValue reason="NOT_REPORTED" detail="The print carries no condition codes." />}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function DepthPanel({ securityKey }: { securityKey: string }) {
  const client = useMarketData();
  useMarketDataKeys([securityKey]);
  const book = client.depth(securityKey);
  const bids = book.bid?.levels ?? [];
  const asks = book.ask?.levels ?? [];
  const cumBid = cumulative(bids.map((l) => l.size));
  const cumAsk = cumulative(asks.map((l) => l.size));
  const rows = Math.max(bids.length, asks.length);
  const src = book.bid ?? book.ask;
  const st = client.status(securityKey);
  const sizeOrDash = (x: string | null | undefined) => (x === undefined ? '' : (fmt(x) ?? <NoValue reason="NOT_REPORTED" />));
  return (
    <section className="mon-panel" aria-label={`Depth ${securityKey}`}>
      <header className="mon-panel-head">
        <h2>Depth</h2>
        <span className="mon-panel-key">{securityKey}</span>
        {src ? (
          <Badge tone={sourceBadge(src).tone} title={sourceBadge(src).title}>
            {sourceBadge(src).text}
          </Badge>
        ) : null}
      </header>
      {st?.state === 'ERROR' ? (
        <p className="mon-empty" role="alert">
          <ErrorValue reason="ERROR" detail={st.reason} /> {st.reason}
        </p>
      ) : rows === 0 ? (
        <p className="mon-empty">
          No depth for {securityKey}: the serving source does not provide it, or it is not entitled (see the source
          status in the watchlist).
        </p>
      ) : (
        <table className="mon-table mon-depth" aria-label={`Market depth ${securityKey}`}>
          <thead>
            <tr>
              <th scope="col" className="mon-num">Cum</th>
              <th scope="col" className="mon-num">Bid size</th>
              <th scope="col" className="mon-num">Bid</th>
              <th scope="col" className="mon-num">Ask</th>
              <th scope="col" className="mon-num">Ask size</th>
              <th scope="col" className="mon-num">Cum</th>
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: rows }, (_, i) => (
              <tr key={i}>
                <td className="mon-num mon-muted">{bids[i] ? fmt(cumBid[i] ?? null) : ''}</td>
                <td className="mon-num">{bids[i] ? sizeOrDash(bids[i].size) : ''}</td>
                <td className="mon-num mon-up">{bids[i] ? fmt(bids[i].price) : ''}</td>
                <td className="mon-num mon-down">{asks[i] ? fmt(asks[i].price) : ''}</td>
                <td className="mon-num">{asks[i] ? sizeOrDash(asks[i].size) : ''}</td>
                <td className="mon-num mon-muted">{asks[i] ? fmt(cumAsk[i] ?? null) : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
