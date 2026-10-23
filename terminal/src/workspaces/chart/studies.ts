/**
 * Technical studies (CAP-MKT-07 analytics_definitions), computed by the platform; values are display numbers
 * (IEEE doubles), never money. Each output array is aligned with the input; `null` means "warming up".
 *
 * Conventions (study version chart.studies@1):
 * - SMA_n(t) = mean(x[t-n+1..t]); null for t < n-1.
 * - EMA_n: alpha = 2/(n+1), seeded with SMA_n at t = n-1.
 * - Bollinger(n,k): middle SMA_n, bands SMA_n +/- k x population standard deviation over n.
 * - RSI_n (Wilder): first average gain/loss = simple mean of the first n changes (at t = n), then
 *   avg = (avg x (n-1) + current) / n; RSI = 100 - 100/(1+RS); avg loss 0 gives 100 (50 when both are 0).
 * - MACD(f,s,g) = EMA_f - EMA_s (from t = s-1); signal = EMA_g of MACD seeded with the SMA of its first g values;
 *   histogram = MACD - signal.
 * - ATR_n (Wilder, TA-Lib seeding): TR_t = max(H-L, |H-C[t-1]|, |L-C[t-1]|) from t = 1; ATR at t = n is the mean
 *   of TR_1..TR_n, then (ATR x (n-1) + TR) / n.
 * - VWAP: cumulative sum(typical x volume) / sum(volume), typical = (H+L+C)/3, reset whenever `sessionOf` changes;
 *   null while cumulative volume is 0 or a volume is unknown.
 */
export const STUDY_VERSION = 'chart.studies@1';

export interface Ohlcv {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export type Series = (number | null)[];

export function sma(x: readonly number[], n: number): Series {
  const out: Series = new Array(x.length).fill(null);
  let sum = 0;
  for (let i = 0; i < x.length; i++) {
    sum += x[i]!;
    if (i >= n) sum -= x[i - n]!;
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

/** EMA over a series that may start with nulls (e.g. MACD); seeding starts at the first non-null value. */
export function ema(x: readonly (number | null)[], n: number): Series {
  const out: Series = new Array(x.length).fill(null);
  const alpha = 2 / (n + 1);
  const first = x.findIndex((v) => v !== null);
  if (first < 0) return out;
  let prev: number | null = null;
  let seed = 0;
  for (let i = first; i < x.length; i++) {
    const v = x[i]!;
    if (v === null) return out;
    if (prev === null) {
      seed += v;
      if (i - first === n - 1) prev = seed / n;
      else continue;
    } else {
      prev = alpha * v + (1 - alpha) * prev;
    }
    out[i] = prev;
  }
  return out;
}

export interface Bands {
  middle: Series;
  upper: Series;
  lower: Series;
}

export function bollinger(x: readonly number[], n = 20, k = 2): Bands {
  const middle = sma(x, n);
  const upper: Series = new Array(x.length).fill(null);
  const lower: Series = new Array(x.length).fill(null);
  for (let i = n - 1; i < x.length; i++) {
    const m = middle[i] as number;
    let v = 0;
    for (let j = i - n + 1; j <= i; j++) v += (x[j]! - m) ** 2;
    const sd = Math.sqrt(v / n);
    upper[i] = m + k * sd;
    lower[i] = m - k * sd;
  }
  return { middle, upper, lower };
}

export function rsi(x: readonly number[], n = 14): Series {
  const out: Series = new Array(x.length).fill(null);
  if (x.length <= n) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= n; i++) {
    const d = x[i]! - x[i - 1]!;
    if (d > 0) gain += d;
    else loss -= d;
  }
  gain /= n;
  loss /= n;
  const value = () => (loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss));
  out[n] = value();
  for (let i = n + 1; i < x.length; i++) {
    const d = x[i]! - x[i - 1]!;
    gain = (gain * (n - 1) + Math.max(d, 0)) / n;
    loss = (loss * (n - 1) + Math.max(-d, 0)) / n;
    out[i] = value();
  }
  return out;
}

export interface Macd {
  macd: Series;
  signal: Series;
  histogram: Series;
}

export function macd(x: readonly number[], fast = 12, slow = 26, signalN = 9): Macd {
  const f = ema(x, fast);
  const s = ema(x, slow);
  const line: Series = x.map((_, i) => (f[i] === null || s[i] === null ? null : (f[i] as number) - (s[i] as number)));
  const signal = ema(line, signalN);
  const histogram: Series = line.map((m, i) => (m === null || signal[i] === null ? null : m - (signal[i] as number)));
  return { macd: line, signal, histogram };
}

export function atr(bars: readonly Ohlcv[], n = 14): Series {
  const out: Series = new Array(bars.length).fill(null);
  if (bars.length <= n) return out;
  const tr = (i: number) => {
    const b = bars[i]!;
    const pc = bars[i - 1]!.close;
    return Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
  };
  let a = 0;
  for (let i = 1; i <= n; i++) a += tr(i);
  a /= n;
  out[n] = a;
  for (let i = n + 1; i < bars.length; i++) {
    a = (a * (n - 1) + tr(i)) / n;
    out[i] = a;
  }
  return out;
}

export function vwap(bars: readonly Ohlcv[], sessionOf: (b: Ohlcv) => string = () => ''): Series {
  const out: Series = new Array(bars.length).fill(null);
  let key: string | null = null;
  let pv = 0;
  let vol = 0;
  let broken = false;
  bars.forEach((b, i) => {
    const k = sessionOf(b);
    if (k !== key) {
      key = k;
      pv = 0;
      vol = 0;
      broken = false;
    }
    if (b.volume === null) broken = true;
    if (broken) return;
    pv += ((b.high + b.low + b.close) / 3) * b.volume!;
    vol += b.volume!;
    out[i] = vol > 0 ? pv / vol : null;
  });
  return out;
}

export type StudyId = 'sma' | 'ema' | 'bollinger' | 'vwap' | 'rsi' | 'macd' | 'atr';

export interface StudyDef {
  id: StudyId;
  label: string;
  pane: 'price' | 'lower';
  params: string;
}

export const STUDIES: readonly StudyDef[] = [
  { id: 'sma', label: 'SMA 20', pane: 'price', params: 'n=20, close' },
  { id: 'ema', label: 'EMA 50', pane: 'price', params: 'n=50, close, SMA seed' },
  { id: 'bollinger', label: 'Bollinger 20, 2', pane: 'price', params: 'n=20, k=2, population sd' },
  { id: 'vwap', label: 'VWAP', pane: 'price', params: 'session-anchored, typical price' },
  { id: 'rsi', label: 'RSI 14', pane: 'lower', params: 'n=14, Wilder' },
  { id: 'macd', label: 'MACD 12, 26, 9', pane: 'lower', params: 'EMA 12 - EMA 26, signal EMA 9' },
  { id: 'atr', label: 'ATR 14', pane: 'lower', params: 'n=14, Wilder (TA-Lib seed)' },
];
