/**
 * Monitor model (CAP-MKT-08 watchlists, CAP-MKT-03 time & sales, CAP-MKT-06 depth): pure helpers, no React.
 * Decimals stay strings; arithmetic uses BigInt scaled integers (never floats).
 */
import { parseDecimal } from '../../format/decimal';
import { isRealDataType, refusedStatus, type DataType, type KeyStatus, type Quote, type StatusState } from '../../state/marketdata';

export const DEFAULT_KEYS = ['AAPL.XNAS', 'MSFT.XNAS', 'NVDA.XNAS', 'AMZN.XNAS', 'GOOGL.XNAS', 'META.XNAS', 'JPM.XNYS'];

export interface Watchlist {
  id: string;
  name: string;
  keys: string[];
}

export interface MonitorState {
  version: 1;
  lists: Watchlist[];
  active: string;
}

export function defaultState(extraKeys: readonly string[] = []): MonitorState {
  const lists: Watchlist[] = [{ id: 'us-large', name: 'US large caps', keys: [...DEFAULT_KEYS] }];
  if (extraKeys.length) lists.push({ id: 'core', name: 'Core instruments', keys: [...extraKeys] });
  return { version: 1, lists, active: 'us-large' };
}

// ---------------------------------------------------------------- keys
const MIC = /^[A-Z0-9]{4}$/;
const SYMBOL = /^[A-Z0-9][A-Z0-9.\-/]{0,23}$/;
const INSTRUMENT_ID = /^[A-Za-z0-9][A-Za-z0-9_\-:]{0,63}$/;

/** Same rule as backend marketdata/events.py parse_key: `<SYMBOL>.<MIC>` (any 4-character MIC) or a core id. */
export function splitKey(key: string): { symbol: string; mic: string | null } | null {
  if (!key || key.length > 64) return null;
  const dot = key.lastIndexOf('.');
  if (dot > 0 && dot < key.length - 1) {
    const mic = key.slice(dot + 1);
    const symbol = key.slice(0, dot);
    if (MIC.test(mic) && SYMBOL.test(symbol)) return { symbol, mic };
  }
  return INSTRUMENT_ID.test(key) ? { symbol: key, mic: null } : null;
}

/** Normalise user input: "aapl.xnas" -> "AAPL.XNAS"; a bare symbol gets `defaultMic`; null if invalid. */
export function normaliseKey(input: string, defaultMic = 'XNAS'): string | null {
  const t = input.trim();
  if (!t) return null;
  const upper = t.toUpperCase();
  if (/^[A-Z0-9.\-/]+\.[A-Z0-9]{4}$/.test(upper) && splitKey(upper)?.mic) return upper;
  if (/^[A-Z0-9][A-Z0-9\-/]{0,23}$/.test(upper) && !t.includes(':')) return `${upper}.${defaultMic}`;
  return splitKey(t) ? t : null;
}

export function addKey(list: Watchlist, input: string): { list: Watchlist; error: string | null } {
  const key = normaliseKey(input);
  if (!key) return { list, error: `"${input}" is not a security key (SYMBOL.MIC, e.g. AAPL.XNAS)` };
  if (list.keys.includes(key)) return { list, error: `${key} is already in ${list.name}` };
  if (list.keys.length >= 500) return { list, error: 'a watchlist holds at most 500 keys' };
  return { list: { ...list, keys: [...list.keys, key] }, error: null };
}

export function removeKey(list: Watchlist, key: string): Watchlist {
  return { ...list, keys: list.keys.filter((k) => k !== key) };
}

export function moveKey(list: Watchlist, key: string, delta: -1 | 1): Watchlist {
  const i = list.keys.indexOf(key);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= list.keys.length) return list;
  const keys = [...list.keys];
  [keys[i], keys[j]] = [keys[j] as string, keys[i] as string];
  return { ...list, keys };
}

// ---------------------------------------------------------------- persistence (per-browser convenience)
export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const STORAGE_KEY = 'unknown.monitor.v1';

function defaultStore(): KeyValueStore | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function loadState(store: KeyValueStore | null = defaultStore()): MonitorState | null {
  try {
    const raw = store?.getItem(STORAGE_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as MonitorState;
    if (v?.version !== 1 || !Array.isArray(v.lists) || !v.lists.length) return null;
    const lists = v.lists
      .filter((l) => l && typeof l.id === 'string' && typeof l.name === 'string' && Array.isArray(l.keys))
      .map((l) => ({ id: l.id, name: l.name.slice(0, 60), keys: l.keys.filter((k) => typeof k === 'string' && splitKey(k)) }));
    if (!lists.length) return null;
    return { version: 1, lists, active: lists.some((l) => l.id === v.active) ? v.active : (lists[0] as Watchlist).id };
  } catch {
    return null;
  }
}

/** Never throws: storage can be absent, blocked or full; the monitor works without it. */
export function saveState(state: MonitorState, store: KeyValueStore | null = defaultStore()): boolean {
  try {
    store?.setItem(STORAGE_KEY, JSON.stringify(state));
    return !!store;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- decimal arithmetic
interface Scaled {
  n: bigint;
  scale: number;
}

/** Keeps the input's scale ("10.10" has scale 2), so a change of two 2-decimal prices has 2 decimals. */
function toScaled(v: string): Scaled | null {
  if (!parseDecimal(v)) return null;
  const m = /^([+-])?(\d+)(?:\.(\d+))?$/.exec(v.trim());
  if (!m) return null;
  const frac = m[3] ?? '';
  const n = BigInt((m[2] ?? '0') + frac) * (m[1] === '-' ? -1n : 1n);
  return { n, scale: frac.length };
}

function align(a: Scaled, b: Scaled): [bigint, bigint, number] {
  const s = Math.max(a.scale, b.scale);
  return [a.n * 10n ** BigInt(s - a.scale), b.n * 10n ** BigInt(s - b.scale), s];
}

function fromScaled(n: bigint, scale: number): string {
  const neg = n < 0n;
  const digits = (neg ? -n : n).toString().padStart(scale + 1, '0');
  const body = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
  return neg ? `-${body}` : body;
}

/** a - b, exact. */
export function subtract(a: string | null, b: string | null): string | null {
  if (a === null || b === null) return null;
  const x = toScaled(a);
  const y = toScaled(b);
  if (!x || !y) return null;
  const [p, q, s] = align(x, y);
  return fromScaled(p - q, s);
}

/** (last - prev) / prev x 100 to `places` decimals, half-even; null when either is missing or prev is zero. */
export function percentChange(last: string | null, prev: string | null, places = 2): string | null {
  if (last === null || prev === null) return null;
  const x = toScaled(last);
  const y = toScaled(prev);
  if (!x || !y || y.n === 0n) return null;
  const [p, q] = align(x, y);
  const num = (p - q) * 100n * 10n ** BigInt(places);
  let quo = num / q;
  const rem = num % q;
  const twice = 2n * (rem < 0n ? -rem : rem);
  const den = q < 0n ? -q : q;
  if (twice > den || (twice === den && quo % 2n !== 0n)) quo += (num < 0n) !== (q < 0n) ? -1n : 1n;
  return fromScaled(quo, places);
}

/** -1, 0 or 1 comparing two decimal strings; null when either is missing. */
export function direction(prev: string | null | undefined, next: string | null | undefined): -1 | 0 | 1 | null {
  if (prev == null || next == null) return null;
  const d = subtract(next, prev);
  if (d === null) return null;
  return d.startsWith('-') ? -1 : /^0(\.0*)?$/.test(d) ? 0 : 1;
}

// ---------------------------------------------------------------- labels
export interface SourceBadge {
  text: string;
  tone: 'ok' | 'warn' | 'bad' | 'info' | 'neutral';
  title: string;
}

/**
 * Every row shows its source and data type. A type that is not real market data (REALTIME, DELAYED, FROZEN) is an
 * error badge; the client never stores such a quote, so this is a second line of defence.
 */
export function sourceBadge(q: Pick<Quote, 'source' | 'data_type' | 'feed'> | undefined): SourceBadge {
  if (!q) return { text: 'NO DATA', tone: 'neutral', title: 'NOT_REPORTED: no quote received yet' };
  if (!isRealDataType(q.data_type)) {
    return { text: 'ERR', tone: 'bad', title: refusedStatus('', 'quote', q.source, q.data_type).reason };
  }
  const tone: Record<DataType, SourceBadge['tone']> = { REALTIME: 'ok', DELAYED: 'warn', FROZEN: 'warn' };
  const text = `${q.source} ${q.data_type}`;
  const title = `Source ${q.source}, ${q.data_type.toLowerCase()}${q.feed ? `, feed ${q.feed}` : ''}`;
  return { text, tone: tone[q.data_type], title };
}

export function statusTone(state: StatusState | undefined): SourceBadge['tone'] {
  switch (state) {
    case 'SUBSCRIBED':
      return 'ok';
    case 'STALE':
    case 'LINE_BUDGET':
      return 'warn';
    case 'NOT_ENTITLED':
    case 'UNAVAILABLE':
    case 'ENDED':
    case 'ERROR':
      return 'bad';
    default:
      return 'neutral';
  }
}

/** Why a watchlist row's values are missing (or in error, or stale): from the key's stream status and its quote. */
export interface RowMiss {
  /** NOT_ENTITLED, ERROR, NOT_REPORTED, or the stream's own state verbatim (e.g. UNAVAILABLE, LINE_BUDGET). */
  reason: string;
  detail: string | null;
  /** Set when the key is STALE: the age of the last quote (null when unknown). */
  staleAgeMs?: number | null;
}

export function rowMiss(q: Quote | undefined, st: KeyStatus | undefined, tsMs: number | null, nowMs: number): RowMiss {
  if (st && st.state === 'STALE') return { reason: 'STALE', detail: st.reason || null, staleAgeMs: tsMs === null ? null : Math.max(0, nowMs - tsMs) };
  if (st && st.state !== 'SUBSCRIBED') return { reason: st.state, detail: st.reason || null };
  if (!q) return { reason: 'NOT_REPORTED', detail: 'No quote received yet.' };
  return { reason: 'NOT_REPORTED', detail: null };
}

export interface Row {
  key: string;
  last: string | null;
  chg: string | null;
  pct: string | null;
  bid: string | null;
  ask: string | null;
  bidSize: string | null;
  askSize: string | null;
  volume: string | null;
  tsMs: number | null;
  badge: SourceBadge;
}

export function rowFor(key: string, q: Quote | undefined): Row {
  const recv = q?.ts_source_ns ?? q?.ts_recv_ns ?? null;
  return {
    key,
    last: q?.last ?? null,
    chg: subtract(q?.last ?? null, q?.prev_close ?? null),
    pct: percentChange(q?.last ?? null, q?.prev_close ?? null),
    bid: q?.bid ?? null,
    ask: q?.ask ?? null,
    bidSize: q?.bid_size ?? null,
    askSize: q?.ask_size ?? null,
    volume: q?.volume ?? null,
    tsMs: recv === null ? null : Number(recv) / 1e6,
    badge: sourceBadge(q),
  };
}

/** Cumulative depth per level (for the ladder's size bars), exact. */
export function cumulative(sizes: readonly (string | null)[]): string[] {
  let acc: string = '0';
  return sizes.map((s) => {
    acc = s === null ? acc : (subtract(acc, `-${s.replace(/^-/, '')}`) ?? acc);
    return acc;
  });
}
