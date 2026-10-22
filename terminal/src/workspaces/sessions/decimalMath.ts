/**
 * Exact decimal arithmetic on API decimal strings, using BigInt mantissas (never JS floats).
 *
 * The sessions workspace derives a few figures from server values: the implied required net return
 * of a mandate, a session's marked equity and its change against the seed. These follow the core's
 * own rules (backend/src/unknown/domain/fixed.py): a value is an integer mantissa with a decimal
 * scale, addition aligns scales, and division rounds explicitly. Results are decimal strings that
 * are only ever displayed, never stored or sent back as money.
 */

export interface Fixed {
  /** Integer mantissa: value = m / 10^s. */
  m: bigint;
  /** Number of fraction digits (>= 0). */
  s: number;
}

export type Rounding = 'HALF_EVEN' | 'FLOOR' | 'CEILING' | 'DOWN';

const RE = /^([+-])?(\d+)(?:\.(\d+))?$/;
const MAX_SCALE = 18;

/** Parse a plain decimal string exactly, keeping its scale ("-12.340" -> m=-12340, s=3). */
export function parseFixed(text: string): Fixed | null {
  const match = RE.exec(text.trim());
  if (!match) return null;
  const frac = match[3] ?? '';
  if (frac.length > MAX_SCALE) return null;
  const m = BigInt((match[2] ?? '0') + frac);
  return { m: match[1] === '-' ? -m : m, s: frac.length };
}

function pow10(n: number): bigint {
  return 10n ** BigInt(n);
}

/** Canonical text preserving scale, like the core's Fixed.to_str(): {m:-1050, s:3} -> "-1.050". */
export function fixedToString(f: Fixed): string {
  const neg = f.m < 0n;
  let digits = (neg ? -f.m : f.m).toString();
  if (f.s === 0) return (neg ? '-' : '') + digits;
  digits = digits.padStart(f.s + 1, '0');
  return `${neg ? '-' : ''}${digits.slice(0, -f.s)}.${digits.slice(-f.s)}`;
}

function align(a: Fixed, b: Fixed): [bigint, bigint, number] {
  const s = Math.max(a.s, b.s);
  return [a.m * pow10(s - a.s), b.m * pow10(s - b.s), s];
}

export function addFixed(a: Fixed, b: Fixed): Fixed {
  const [x, y, s] = align(a, b);
  return { m: x + y, s };
}

export function subFixed(a: Fixed, b: Fixed): Fixed {
  const [x, y, s] = align(a, b);
  return { m: x - y, s };
}

export function cmpFixed(a: Fixed, b: Fixed): -1 | 0 | 1 {
  const [x, y] = align(a, b);
  return x === y ? 0 : x < y ? -1 : 1;
}

export function isPositive(f: Fixed): boolean {
  return f.m > 0n;
}

/** Floor division with a non-negative remainder (Python's divmod for a positive denominator). */
function floorDivMod(n: bigint, d: bigint): [bigint, bigint] {
  let q = n / d;
  let r = n % d;
  if (r !== 0n && r < 0n) {
    q -= 1n;
    r += d;
  }
  return [q, r];
}

/** Integer quotient under a rounding mode: a port of the core's round_div. */
export function roundDiv(numerator: bigint, denominator: bigint, rounding: Rounding): bigint {
  if (denominator === 0n) throw new RangeError('division by zero');
  let n = numerator;
  let d = denominator;
  if (d < 0n) {
    n = -n;
    d = -d;
  }
  const [q, r] = floorDivMod(n, d);
  if (r === 0n) return q;
  switch (rounding) {
    case 'FLOOR':
      return q;
    case 'CEILING':
      return q + 1n;
    case 'DOWN':
      return n >= 0n ? q : q + 1n;
    case 'HALF_EVEN': {
      const twice = 2n * r;
      if (twice !== d) return twice > d ? q + 1n : q;
      return q % 2n === 0n ? q : q + 1n;
    }
  }
}

/** a / b at `scale` fraction digits, rounded explicitly (the core's Fixed.div). */
export function divFixed(a: Fixed, b: Fixed, scale: number, rounding: Rounding): Fixed {
  if (b.m === 0n) throw new RangeError('division by zero');
  const numerator = a.m * pow10(scale + b.s);
  const denominator = b.m * pow10(a.s);
  return { m: roundDiv(numerator, denominator, rounding), s: scale };
}

/** Multiply by an integer; the scale is unchanged (the core's Fixed * int). */
export function mulInt(a: Fixed, n: bigint): Fixed {
  return { m: a.m * n, s: a.s };
}

/** Sum of decimal strings, exact. Returns null if any value is not a plain decimal. */
export function sumDecimals(values: readonly string[]): string | null {
  let total: Fixed = { m: 0n, s: 0 };
  for (const v of values) {
    const f = parseFixed(v);
    if (!f) return null;
    total = addFixed(total, f);
  }
  return fixedToString(total);
}

/** a - b as an exact decimal string, or null for malformed input. */
export function subDecimals(a: string, b: string): string | null {
  const x = parseFixed(a);
  const y = parseFixed(b);
  return x && y ? fixedToString(subFixed(x, y)) : null;
}

/** Exact comparison of two decimal strings; null for malformed input. */
export function compareDecimals(a: string, b: string): -1 | 0 | 1 | null {
  const x = parseFixed(a);
  const y = parseFixed(b);
  return x && y ? cmpFixed(x, y) : null;
}

/**
 * Implied required net return in percent, computed exactly as the core does when it records a
 * session request (core/handlers/sessions.py create_session):
 *   ((target - seed) / seed) rounded HALF_EVEN to 4 decimal places, then multiplied by 100.
 * "100.00" -> "105.00" gives "5.0000"; "1" -> "10000" gives "999900.0000".
 * Returns null when either value is malformed or the seed is not positive.
 */
export function requiredReturnPct(seed: string, target: string): string | null {
  const s = parseFixed(seed);
  const t = parseFixed(target);
  if (!s || !t || s.m <= 0n) return null;
  return fixedToString(mulInt(divFixed(subFixed(t, s), s, 4, 'HALF_EVEN'), 100n));
}
