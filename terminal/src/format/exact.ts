/**
 * Exact decimal arithmetic on API decimal strings (ADR-0004), using BigInt scaled integers.
 *
 * Shared by the workspaces that derive figures for display (risk: a loss from the high-water mark, a
 * sum of position values, a usage ratio; policy: limit differences; audit: journal balances). They are
 * computed here without JavaScript floats: every
 * operand is parsed into an integer and a scale, and results are exact. Nothing is rounded except
 * where a function says so (ratios in basis points), and the rounding direction is explicit.
 */
export interface Scaled {
  /** The value times 10^scale. */
  units: bigint;
  /** Number of fraction digits as written (trailing zeros keep the server's scale). */
  scale: number;
}

const DECIMAL_RE = /^([+-])?(\d+)(?:\.(\d+))?$/;

/** Parse a decimal string exactly. Throws on malformed input so a bad value is never summed silently. */
export function toScaled(value: string): Scaled {
  const m = DECIMAL_RE.exec(value.trim());
  if (!m) throw new Error(`not a decimal string: ${JSON.stringify(value)}`);
  const frac = m[3] ?? '';
  const digits = BigInt((m[2] ?? '0') + frac);
  return { units: m[1] === '-' ? -digits : digits, scale: frac.length };
}

function pow10(n: number): bigint {
  return 10n ** BigInt(n);
}

function rescaleUp(v: Scaled, scale: number): bigint {
  return v.units * pow10(scale - v.scale);
}

/** Render with exactly `scale` fraction digits ("-1.350000"). */
export function fromScaled(units: bigint, scale: number): string {
  const negative = units < 0n;
  const abs = (negative ? -units : units).toString().padStart(scale + 1, '0');
  const int = abs.slice(0, abs.length - scale);
  const frac = scale > 0 ? `.${abs.slice(abs.length - scale)}` : '';
  return `${negative ? '-' : ''}${int}${frac}`;
}

function align(a: string, b: string): { a: bigint; b: bigint; scale: number } {
  const sa = toScaled(a);
  const sb = toScaled(b);
  const scale = Math.max(sa.scale, sb.scale);
  return { a: rescaleUp(sa, scale), b: rescaleUp(sb, scale), scale };
}

export function addDecimal(a: string, b: string): string {
  const x = align(a, b);
  return fromScaled(x.a + x.b, x.scale);
}

export function subDecimal(a: string, b: string): string {
  const x = align(a, b);
  return fromScaled(x.a - x.b, x.scale);
}

export function mulDecimal(a: string, b: string): string {
  const sa = toScaled(a);
  const sb = toScaled(b);
  return fromScaled(sa.units * sb.units, sa.scale + sb.scale);
}

/** Exact sum; "0" for an empty list. */
export function sumDecimals(values: readonly string[]): string {
  return values.reduce((acc, v) => addDecimal(acc, v), '0');
}

export function negateDecimal(a: string): string {
  const s = toScaled(a);
  return fromScaled(-s.units, s.scale);
}

export function maxDecimal(a: string, b: string): string {
  const x = align(a, b);
  return x.a >= x.b ? a : b;
}

/** max(0, a): a loss or usage that cannot go below zero. */
export function clampAtZero(a: string): string {
  return toScaled(a).units < 0n ? '0' : a;
}

export type RatioRounding = 'floor' | 'ceil' | 'half-even';

function divRound(n: bigint, d: bigint, rounding: RatioRounding): bigint {
  // d > 0 is guaranteed by the caller.
  const q = n / d; // truncates toward zero
  const r = n % d;
  if (r === 0n) return q;
  const negative = n < 0n;
  switch (rounding) {
    case 'floor':
      return negative ? q - 1n : q;
    case 'ceil':
      return negative ? q : q + 1n;
    case 'half-even': {
      const twice = (r < 0n ? -r : r) * 2n;
      const away = negative ? q - 1n : q + 1n;
      if (twice > d) return away;
      if (twice < d) return q;
      return q % 2n === 0n ? q : away;
    }
  }
}

/**
 * numerator / denominator in basis points (1 bp = 0.01%), rounded as requested. Returns null when the
 * denominator is not positive (no meaningful ratio).
 */
export function ratioBps(numerator: string, denominator: string, rounding: RatioRounding = 'ceil'): bigint | null {
  const x = align(numerator, denominator);
  if (x.b <= 0n) return null;
  return divRound(x.a * 10_000n, x.b, rounding);
}

/** Basis points as an exact percentage with two decimals: 1533n -> "15.33%", 99990000n -> "999,900.00%". */
export function bpsToPercent(bps: bigint | number): string {
  const v = typeof bps === 'number' ? BigInt(Math.trunc(bps)) : bps;
  const negative = v < 0n;
  const abs = negative ? -v : v;
  const int = (abs / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const frac = (abs % 100n).toString().padStart(2, '0');
  return `${negative ? '−' : ''}${int}.${frac}%`;
}
