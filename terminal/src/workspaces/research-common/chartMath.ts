/**
 * Pure geometry for the inline SVG charts (no chart library). Values passed here are plotting positions only:
 * labels always show the API's exact text. Gaps stay gaps: a missing point splits a line, it is never
 * interpolated (CAP-MAC-07: a missing tenor stays missing).
 */

export interface Extent {
  min: number;
  max: number;
}

/** Min and max over the finite values; null when there are none. */
export function extent(values: readonly (number | null | undefined)[]): Extent | null {
  let min = Infinity;
  let max = -Infinity;
  for (const v of values) {
    if (v === null || v === undefined || !Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return min === Infinity ? null : { min, max };
}

/** Round tick values (1, 2, 2.5 or 5 x 10^k steps) covering [min, max]; at least two ticks. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) {
    const pad = min === 0 ? 1 : Math.abs(min) * 0.1;
    min -= pad;
    max += pad;
  }
  if (min > max) [min, max] = [max, min];
  const raw = (max - min) / Math.max(1, count - 1);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let v = start, i = 0; v <= end + step / 1e6 && i < 100; v += step, i++) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

/** Linear map from a domain onto a range (a zero-width domain maps to the range midpoint). */
export function linear(domain: readonly [number, number], range: readonly [number, number]): (v: number) => number {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  if (d1 === d0) return () => (r0 + r1) / 2;
  const k = (r1 - r0) / (d1 - d0);
  return (v: number) => r0 + (v - d0) * k;
}

export interface Pt {
  x: number;
  y: number;
}

/** Splits a sequence at null y values: each run of present points is drawn as its own line. */
export function segments(points: readonly { x: number; y: number | null }[]): Pt[][] {
  const out: Pt[][] = [];
  let cur: Pt[] = [];
  for (const p of points) {
    if (p.y === null || !Number.isFinite(p.y)) {
      if (cur.length) out.push(cur);
      cur = [];
    } else {
      cur.push({ x: p.x, y: p.y });
    }
  }
  if (cur.length) out.push(cur);
  return out;
}

/** SVG path data for a polyline ("M x y L x y ..."), rounded to 0.1 px. */
export function linePath(points: readonly Pt[]): string {
  return points.map((p, i) => `${i ? 'L' : 'M'}${round1(p.x)} ${round1(p.y)}`).join(' ');
}

function round1(v: number): number {
  return Math.round(v * 10) / 10;
}

export interface CumulativePoint {
  date: string;
  /** Growth of 1 minus 1 after this session: prod(1 + r) - 1 (a float statistic). */
  value: number;
}

/**
 * Cumulative simple return path from per-session simple returns (return.simple@1): the value after session t
 * is prod_{s<=t}(1 + r_s) - 1. Non-finite returns stop the path (it is not continued past bad data).
 */
export function cumulativeReturns(returns: readonly (readonly [string, number])[]): CumulativePoint[] {
  const out: CumulativePoint[] = [];
  let growth = 1;
  for (const [date, r] of returns) {
    if (!Number.isFinite(r)) break;
    growth *= 1 + r;
    out.push({ date, value: growth - 1 });
  }
  return out;
}

/** Peak-to-trough drawdown of a cumulative path (fraction of the running peak of growth), for the summary. */
export function maxDrawdown(path: readonly CumulativePoint[]): number {
  let peak = 1;
  let worst = 0;
  for (const p of path) {
    const g = 1 + p.value;
    if (g > peak) peak = g;
    const dd = (peak - g) / peak;
    if (dd > worst) worst = dd;
  }
  return worst;
}

/** Indices of at most `n` evenly spaced labels over `length` points, always including the first and last. */
export function labelIndices(length: number, n: number): number[] {
  if (length <= 0) return [];
  if (length <= n) return Array.from({ length }, (_, i) => i);
  const out = new Set<number>();
  for (let i = 0; i < n; i++) out.add(Math.round((i * (length - 1)) / (n - 1)));
  return [...out].sort((a, b) => a - b);
}
