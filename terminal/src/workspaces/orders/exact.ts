/**
 * Exact decimal arithmetic on decimal strings for the order workstation (scaled BigInt, never floating point).
 * Used only to lay out a price ladder and to split the core's own required cash into notional and fee envelope;
 * every money decision stays in the core.
 */

export interface Scaled {
  n: bigint;
  scale: number;
}

const DECIMAL_RE = /^([+-])?(\d+)(?:\.(\d+))?$/;

/** The decimal at the scale it is written with ("1.20" keeps two fraction digits, as the core wrote it). */
export function toScaled(value: string): Scaled | null {
  const m = DECIMAL_RE.exec(value.trim());
  if (!m) return null;
  const frac = m[3] ?? '';
  const n = BigInt((m[2] ?? '0') + frac);
  return { n: m[1] === '-' ? -n : n, scale: frac.length };
}

function rescale(s: Scaled, scale: number): bigint {
  return s.n * 10n ** BigInt(scale - s.scale);
}

/** Decimal string with exactly `scale` fraction digits (or none when scale is 0). */
export function fromScaled(s: Scaled): string {
  const neg = s.n < 0n;
  const digits = (neg ? -s.n : s.n).toString().padStart(s.scale + 1, '0');
  const int = digits.slice(0, digits.length - s.scale);
  const frac = digits.slice(digits.length - s.scale);
  return `${neg ? '-' : ''}${int}${s.scale ? `.${frac}` : ''}`;
}

function binary(a: string, b: string, op: (x: bigint, y: bigint) => bigint): string | null {
  const x = toScaled(a);
  const y = toScaled(b);
  if (!x || !y) return null;
  const scale = Math.max(x.scale, y.scale);
  return fromScaled({ n: op(rescale(x, scale), rescale(y, scale)), scale });
}

export const addExact = (a: string, b: string) => binary(a, b, (x, y) => x + y);
export const subExact = (a: string, b: string) => binary(a, b, (x, y) => x - y);

export function mulExact(a: string, b: string): string | null {
  const x = toScaled(a);
  const y = toScaled(b);
  if (!x || !y) return null;
  return fromScaled({ n: x.n * y.n, scale: x.scale + y.scale });
}

/** Round toward +infinity to `scale` fraction digits (the core's CEILING for reservations). */
export function ceilToScale(value: string, scale: number): string | null {
  const x = toScaled(value);
  if (!x) return null;
  if (x.scale <= scale) return fromScaled({ n: rescale(x, scale), scale });
  const d = 10n ** BigInt(x.scale - scale);
  const q = x.n / d;
  const up = x.n % d !== 0n && x.n > 0n ? 1n : 0n;
  return fromScaled({ n: q + up, scale });
}

export function cmpExact(a: string, b: string): -1 | 0 | 1 | null {
  const d = subExact(a, b);
  if (d === null) return null;
  const s = toScaled(d);
  return !s || s.n === 0n ? 0 : s.n > 0n ? 1 : -1;
}

/** Number of fraction digits a decimal string is written with ("302.011672" -> 6). */
export function writtenScale(value: string): number {
  const dot = value.indexOf('.');
  return dot < 0 ? 0 : value.length - dot - 1;
}

/** `price + k * tick`, written at the larger of the two scales. */
export function stepPrice(price: string, tick: string, k: number): string | null {
  const p = toScaled(price);
  const t = toScaled(tick);
  if (!p || !t || !Number.isInteger(k)) return null;
  const scale = Math.max(p.scale, t.scale);
  return fromScaled({ n: rescale(p, scale) + BigInt(k) * rescale(t, scale), scale });
}

/** Largest multiple of `tick` at or below `price` (a grid anchor). */
export function floorToTick(price: string, tick: string): string | null {
  const p = toScaled(price);
  const t = toScaled(tick);
  if (!p || !t || t.n <= 0n) return null;
  const scale = Math.max(p.scale, t.scale);
  const pn = rescale(p, scale);
  const tn = rescale(t, scale);
  const q = pn >= 0n ? pn / tn : -((-pn + tn - 1n) / tn);
  return fromScaled({ n: q * tn, scale });
}
