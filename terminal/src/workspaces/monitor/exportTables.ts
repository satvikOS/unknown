/**
 * Watchlist export (ui/export.ts). The list of securities is the owner's own data; every quote field is licensed
 * market data from the bus source that served the key, so it is exported only where that source's rights allow it
 * (real-time exchange data needs redistribution rights; a source without a rights record is excluded).
 */
import type { KeyStatus, Quote } from '../../state/marketdata';
import { OWNER, marketSource, nsToIso, type ExportColumn, type ExportSource, type ExportTable } from '../../ui/export';
import { rowFor, rowMiss, type Watchlist } from './model';

export interface WatchRow {
  key: string;
  quote: Quote | undefined;
  status: KeyStatus | undefined;
}

const src = (r: WatchRow): ExportSource => (r.quote ? marketSource(r.quote.source, r.quote.data_type === 'REALTIME') : OWNER);

export function watchlistExport(list: Watchlist, rows: readonly WatchRow[], nowMs: number): ExportTable<WatchRow> {
  const row = (r: WatchRow) => rowFor(r.key, r.quote);
  const why = (r: WatchRow) => rowMiss(r.quote, r.status, row(r).tsMs, nowMs).reason;
  const q = (id: string, header: string, pick: (x: ReturnType<typeof rowFor>) => string | null): ExportColumn<WatchRow> => ({
    id,
    header,
    kind: 'decimal',
    value: (r) => pick(row(r)),
    reason: why,
    source: src,
  });
  return {
    title: `Watchlist ${list.name}`,
    rows,
    columns: [
      { id: 'key', header: 'Security', value: (r) => r.key },
      q('last', 'Last', (x) => x.last),
      q('chg', 'Change', (x) => x.chg),
      q('pct', 'Change %', (x) => x.pct),
      q('bid', 'Bid', (x) => x.bid),
      q('ask', 'Ask', (x) => x.ask),
      q('bsz', 'Bid size', (x) => x.bidSize),
      q('asz', 'Ask size', (x) => x.askSize),
      q('vol', 'Volume', (x) => x.volume),
      { id: 'ts', header: 'Quote time', kind: 'datetime', value: (r) => r.quote?.ts_source_ns ?? r.quote?.ts_recv_ns ?? null, reason: why, source: src },
      { id: 'source', header: 'Source', value: (r) => r.quote?.source ?? null, reason: why },
      { id: 'type', header: 'Data type', value: (r) => r.quote?.data_type ?? null, reason: why },
    ],
    provenance: {
      source: 'Monitor watchlist: the market-data stream (GET /api/marketdata/stream), latest quote per key',
      asOf: nsToIso(String(BigInt(Math.round(nowMs)) * 1_000_000n)),
      filters: { watchlist: list.name },
      notes: ['quote values are licensed market data; columns a source may not export are excluded and listed'],
    },
  };
}
