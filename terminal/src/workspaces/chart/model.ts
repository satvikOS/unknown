/**
 * Chart model (CAP-MKT-07): parses GET /api/charts/bars (backend/src/unknown/charts/service.py), rolls the last bar
 * forward from market-data stream events (docs/design/marketdata-stream-contract.md) and rebases comparisons.
 * Prices stay exact decimal strings; numbers are made only for drawing and studies.
 */
import { compareDecimal, isDecimal } from '../../format/decimal';
import type { Ohlcv } from './studies';

export const INTERVALS = ['1m', '5m', '15m', '30m', '1h', '1d', '1w', '1mo'] as const;
export type IntervalCode = (typeof INTERVALS)[number];
/** The only data types a chart draws; bars or ticks of any other type are refused (an error, never drawn). */
export const DATA_TYPES = ['REALTIME', 'DELAYED', 'FROZEN'] as const;
export type DataType = (typeof DATA_TYPES)[number];

export function isChartDataType(x: unknown): x is DataType {
  return typeof x === 'string' && (DATA_TYPES as readonly string[]).includes(x);
}

export interface ChartBar {
  startNs: string;
  endNs: string;
  /** UTC seconds of the bar start (chart time axis). */
  time: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string | null;
  partial: boolean;
  complete: boolean;
  /** Changed by stream events since the bars were fetched. */
  live: boolean;
}

export interface LiveHint {
  intervalNs: string | null;
  sessionOpenNs: string | null;
  sessionCloseNs: string | null;
}

export interface Gap {
  startNs: string;
  endNs: string;
  missing: number;
}

export interface SourceStatus {
  source: string;
  configured: boolean;
  covers: boolean;
  reason: string;
  error: string | null;
}

export interface BarsResponse {
  key: string;
  interval: string;
  available: boolean;
  source: string | null;
  dataType: DataType | null;
  /** Set when the bars were refused because their data type is not real market data; no bar is kept then. */
  refused: string | null;
  label: string | null;
  currency: string | null;
  adjustment: string | null;
  adjustmentNote: string | null;
  calendarId: string | null;
  timezone: string;
  bars: ChartBar[];
  gaps: Gap[];
  notes: string[];
  missingReason: string | null;
  live: LiveHint;
  sources: SourceStatus[];
}

const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const nsOk = (v: unknown): v is string => typeof v === 'string' && /^\d{1,20}$/.test(v);

export function nsToSeconds(ns: string): number {
  return Number(BigInt(ns) / 1_000_000_000n);
}

function parseBar(raw: unknown): ChartBar | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const [o, h, l, c] = [r.open, r.high, r.low, r.close];
  if (!nsOk(r.start_ns) || !nsOk(r.end_ns) || !isDecimal(o) || !isDecimal(h) || !isDecimal(l) || !isDecimal(c)) return null;
  return {
    startNs: r.start_ns,
    endNs: r.end_ns,
    time: nsToSeconds(r.start_ns),
    open: o,
    high: h,
    low: l,
    close: c,
    volume: isDecimal(r.volume) ? r.volume : null,
    partial: r.partial === true,
    complete: r.complete !== false,
    live: false,
  };
}

/**
 * Defensive parse: anything malformed is dropped. Bars whose data type is not REALTIME, DELAYED or FROZEN (or is
 * missing) are refused, fail closed: no bar is kept and `refused` says why.
 */
export function parseBarsResponse(raw: unknown): BarsResponse | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const dt = str(r.data_type);
  const dataType: DataType | null = isChartDataType(dt) ? dt : null;
  const session = (r.session ?? {}) as Record<string, unknown>;
  const live = (r.live ?? {}) as Record<string, unknown>;
  const parsed = (Array.isArray(r.bars) ? r.bars : []).map(parseBar).filter((b): b is ChartBar => b !== null);
  const refused =
    dataType === null && (parsed.length > 0 || dt !== null)
      ? `Refused bars${str(r.source) ? ` from ${str(r.source)}` : ''}: data type ${dt ?? 'missing'} is not real market data.`
      : null;
  const bars = refused ? [] : parsed;
  bars.sort((a, b) => a.time - b.time);
  return {
    key: str(r.key) ?? '',
    interval: str(r.interval) ?? '',
    available: r.available === true && bars.length > 0,
    source: str(r.source),
    dataType,
    refused,
    label: str(r.label),
    currency: str(r.currency),
    adjustment: str(r.adjustment),
    adjustmentNote: str(r.adjustment_note),
    calendarId: str(session.calendar_id),
    timezone: str(session.timezone) ?? 'UTC',
    bars,
    gaps: (Array.isArray(r.gaps) ? r.gaps : []).flatMap((g: Record<string, unknown>) =>
      nsOk(g?.start_ns) && nsOk(g?.end_ns) ? [{ startNs: g.start_ns, endNs: g.end_ns, missing: Number(g.missing_buckets) || 0 }] : [],
    ),
    notes: [
      ...(Array.isArray(r.notes) ? r.notes : []),
      ...(Array.isArray(session.notes) ? session.notes : []),
    ].filter((n): n is string => typeof n === 'string'),
    missingReason: str(r.missing_reason),
    live: {
      intervalNs: nsOk(live.interval_ns) ? live.interval_ns : null,
      sessionOpenNs: nsOk(live.session_open_ns) ? live.session_open_ns : null,
      sessionCloseNs: nsOk(live.session_close_ns) ? live.session_close_ns : null,
    },
    sources: (Array.isArray(r.sources_considered) ? r.sources_considered : []).map((s: Record<string, unknown>) => ({
      source: String(s?.source ?? ''),
      configured: s?.configured === true,
      covers: s?.covers === true,
      reason: String(s?.reason ?? ''),
      error: str(s?.error),
    })),
  };
}

/** The header label: source and data type; refused bars say ERR. */
export function sourceLabel(r: Pick<BarsResponse, 'source' | 'dataType' | 'refused'>): string {
  if (r.refused) return `ERR · ${r.source ?? 'unknown source'}`;
  if (!r.source) return 'no source';
  return `${r.source} · ${r.dataType ?? 'data type not reported'}`;
}

// ------------------------------------------------------------------ exact decimal addition (volume)
function scaled(v: string, scale: number): bigint {
  const neg = v.startsWith('-');
  const [i, f = ''] = (neg ? v.slice(1) : v).split('.');
  const n = BigInt(i + f.padEnd(scale, '0'));
  return neg ? -n : n;
}

export function addDecimal(a: string, b: string): string {
  const scale = Math.max((a.split('.')[1] ?? '').length, (b.split('.')[1] ?? '').length);
  const sum = scaled(a, scale) + scaled(b, scale);
  const neg = sum < 0n;
  const digits = (neg ? -sum : sum).toString().padStart(scale + 1, '0');
  const out = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
  return neg ? `-${out}` : out;
}

const maxD = (a: string, b: string) => (compareDecimal(a, b) >= 0 ? a : b);
const minD = (a: string, b: string) => (compareDecimal(a, b) <= 0 ? a : b);

// ------------------------------------------------------------------ live updates
export interface Tick {
  price: string;
  size: string | null;
  tsNs: string;
  dataType: string;
  /** Trades add volume and may open a new bar; a quote's `last` only moves the current bar. */
  kind: 'trade' | 'quote';
}

export interface LiveResult {
  bars: ChartBar[];
  changed: boolean;
  /** The tick is past what the hint can place (e.g. a new session): refetch the bars. */
  needsRefetch: boolean;
}

/**
 * Applies one tick to the last bar. A tick whose data type is not real market data, or a series without a real data
 * type, is ignored: nothing but real data is ever drawn. Ticks older than the last bar are ignored (history is final).
 */
export function applyTick(bars: readonly ChartBar[], hint: LiveHint, seriesType: DataType | null, tick: Tick): LiveResult {
  const same = { bars: bars as ChartBar[], changed: false, needsRefetch: false };
  if (!isChartDataType(tick.dataType) || !isChartDataType(seriesType)) return same;
  if (!isDecimal(tick.price) || !nsOk(tick.tsNs) || bars.length === 0) return same;
  const t = BigInt(tick.tsNs);
  const last = bars[bars.length - 1]!;
  if (t < BigInt(last.startNs)) return same;
  if (t < BigInt(last.endNs)) {
    const volume =
      tick.kind === 'trade' && last.volume !== null && tick.size !== null && isDecimal(tick.size) ? addDecimal(last.volume, tick.size) : last.volume;
    const next: ChartBar = { ...last, high: maxD(last.high, tick.price), low: minD(last.low, tick.price), close: tick.price, volume, live: true };
    return { bars: [...bars.slice(0, -1), next], changed: true, needsRefetch: false };
  }
  if (tick.kind !== 'trade') return same;
  const { intervalNs, sessionOpenNs, sessionCloseNs } = hint;
  if (intervalNs && sessionOpenNs && sessionCloseNs) {
    const open = BigInt(sessionOpenNs);
    const close = BigInt(sessionCloseNs);
    const step = BigInt(intervalNs);
    if (t >= open && t < close) {
      const start = open + ((t - open) / step) * step;
      const end = start + step < close ? start + step : close;
      const bar: ChartBar = {
        startNs: start.toString(),
        endNs: end.toString(),
        time: Number(start / 1_000_000_000n),
        open: tick.price,
        high: tick.price,
        low: tick.price,
        close: tick.price,
        volume: tick.size !== null && isDecimal(tick.size) ? tick.size : null,
        partial: true,
        complete: false,
        live: true,
      };
      return { bars: [...bars.slice(0, -1), { ...last, complete: true }, bar], changed: true, needsRefetch: false };
    }
  }
  return { bars: bars as ChartBar[], changed: false, needsRefetch: true };
}

// ------------------------------------------------------------------ numbers for drawing
export function toOhlcv(bars: readonly ChartBar[]): Ohlcv[] {
  return bars.map((b) => ({
    time: b.time,
    open: Number(b.open),
    high: Number(b.high),
    low: Number(b.low),
    close: Number(b.close),
    volume: b.volume === null ? null : Number(b.volume),
  }));
}

/** Rebased comparison in percent: 100 x (P(t) / P(t0) - 1), t0 = the first bar at or after `startTime`. */
export function percentFrom(bars: readonly Ohlcv[], startTime: number): { time: number; value: number }[] {
  const i0 = bars.findIndex((b) => b.time >= startTime);
  const first = bars[i0];
  if (!first || first.close === 0) return [];
  const base = first.close;
  return bars.slice(i0).map((b) => ({ time: b.time, value: (b.close / base - 1) * 100 }));
}

/** Common start of two series: the later of the two first bars. */
export function commonStart(a: readonly Ohlcv[], b: readonly Ohlcv[]): number | null {
  if (!a.length || !b.length) return null;
  return Math.max(a[0]!.time, b[0]!.time);
}

/** Session key for VWAP resets: the trading date of a bar in the venue's time zone. */
export function sessionKeyFor(timezone: string): (b: { time: number }) => string {
  let fmt: Intl.DateTimeFormat;
  try {
    fmt = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' });
  } catch {
    fmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit' });
  }
  return (b) => fmt.format(new Date(b.time * 1000));
}

export function isIntraday(interval: string): boolean {
  return interval.endsWith('m') || interval.endsWith('h');
}
