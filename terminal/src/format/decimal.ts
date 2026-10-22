/**
 * Exact decimal-string formatting and comparison.
 *
 * The API sends every amount, price and quantity as a decimal string (ADR-0004). This module never
 * converts them to JavaScript numbers: it only rearranges digits. Formatting therefore never rounds:
 * trailing zeros beyond the requested minimum fraction digits are dropped, and any non-zero digit is
 * always shown.
 */

export interface ParsedDecimal {
  negative: boolean;
  /** Integer digits without leading zeros ("0" for zero). */
  int: string;
  /** Fraction digits without trailing zeros ("" when integral). */
  frac: string;
}

const DECIMAL_RE = /^([+-])?(\d+)(?:\.(\d+))?$/;

/** Parse a plain decimal string ("-12.3400", "+5", "0.000"). Returns null for anything else. */
export function parseDecimal(value: string): ParsedDecimal | null {
  const m = DECIMAL_RE.exec(value.trim());
  if (!m) return null;
  const int = (m[2] ?? '0').replace(/^0+(?=\d)/, '');
  const frac = (m[3] ?? '').replace(/0+$/, '');
  const zero = /^0*$/.test(int) && frac === '';
  return { negative: m[1] === '-' && !zero, int: int === '' ? '0' : int, frac };
}

export function isDecimal(value: unknown): value is string {
  return typeof value === 'string' && parseDecimal(value) !== null;
}

/** -1, 0 or 1. Throws on malformed input so callers cannot silently mis-sort money. */
export function signOf(value: string): -1 | 0 | 1 {
  const p = parseDecimal(value);
  if (!p) throw new Error(`not a decimal string: ${JSON.stringify(value)}`);
  if (p.int === '0' && p.frac === '') return 0;
  return p.negative ? -1 : 1;
}

export function isZero(value: string): boolean {
  return signOf(value) === 0;
}

function compareMagnitude(a: ParsedDecimal, b: ParsedDecimal): -1 | 0 | 1 {
  if (a.int.length !== b.int.length) return a.int.length < b.int.length ? -1 : 1;
  if (a.int !== b.int) return a.int < b.int ? -1 : 1;
  const len = Math.max(a.frac.length, b.frac.length);
  const fa = a.frac.padEnd(len, '0');
  const fb = b.frac.padEnd(len, '0');
  if (fa === fb) return 0;
  return fa < fb ? -1 : 1;
}

/** Exact comparison of two decimal strings. Throws on malformed input. */
export function compareDecimal(a: string, b: string): -1 | 0 | 1 {
  const pa = parseDecimal(a);
  const pb = parseDecimal(b);
  if (!pa) throw new Error(`not a decimal string: ${JSON.stringify(a)}`);
  if (!pb) throw new Error(`not a decimal string: ${JSON.stringify(b)}`);
  const sa = pa.int === '0' && pa.frac === '' ? 0 : pa.negative ? -1 : 1;
  const sb = pb.int === '0' && pb.frac === '' ? 0 : pb.negative ? -1 : 1;
  if (sa !== sb) return sa < sb ? -1 : 1;
  if (sa === 0) return 0;
  const mag = compareMagnitude(pa, pb);
  return (sa < 0 ? -mag : mag) as -1 | 0 | 1;
}

/** U+2212 MINUS SIGN: typographically aligned with digits, announced as "minus" by screen readers. */
export const MINUS = '−';

export interface DecimalFormat {
  /** Pad the fraction to at least this many digits (zeros only; never rounds). Default 0. */
  minFraction?: number;
  /** Group integer digits in threes. Default true. */
  group?: boolean;
  /** Group separator. Default ",". */
  separator?: string;
  /** "auto": minus for negatives; "always": also "+" for positives (P&L); "never": magnitude only. */
  signDisplay?: 'auto' | 'always' | 'never';
}

function groupDigits(int: string, separator: string): string {
  let out = '';
  for (let i = 0; i < int.length; i++) {
    const fromEnd = int.length - i;
    out += int[i];
    if (fromEnd > 1 && fromEnd % 3 === 1) out += separator;
  }
  return out;
}

/**
 * Format a decimal string exactly. Malformed input is returned unchanged so the raw server value is
 * still visible (callers can detect it with `isDecimal`).
 *
 *   formatDecimal("1234567.891200", { minFraction: 2 }) === "1,234,567.8912"
 *   formatDecimal("300.000000", { minFraction: 2 })     === "300.00"
 *   formatDecimal("-0.005000", { minFraction: 2 })      === "−0.005"
 */
export function formatDecimal(value: string, fmt: DecimalFormat = {}): string {
  const p = parseDecimal(value);
  if (!p) return value;
  const minFraction = Math.max(0, fmt.minFraction ?? 0);
  const frac = p.frac.length >= minFraction ? p.frac : p.frac.padEnd(minFraction, '0');
  const int = fmt.group === false ? p.int : groupDigits(p.int, fmt.separator ?? ',');
  const body = frac ? `${int}.${frac}` : int;
  const zero = p.int === '0' && p.frac === '';
  const sign = fmt.signDisplay ?? 'auto';
  if (sign === 'never' || zero) return body;
  if (p.negative) return MINUS + body;
  return sign === 'always' ? `+${body}` : body;
}

/**
 * ISO 4217 minor units for currencies the platform may display. Unknown codes default to 2.
 * Display only: the ledger keeps its own (larger) scale and the terminal never truncates digits.
 */
const MINOR_UNITS: Record<string, number> = {
  USD: 2, EUR: 2, GBP: 2, CHF: 2, CAD: 2, AUD: 2, NZD: 2, HKD: 2, SGD: 2, SEK: 2, NOK: 2, DKK: 2,
  JPY: 0, KRW: 0, ISK: 0, CLP: 0, BHD: 3, KWD: 3, OMR: 3, JOD: 3, TND: 3,
};

export function currencyMinorUnits(currency: string): number {
  return MINOR_UNITS[currency.toUpperCase()] ?? 2;
}

/** "1,234.50 USD": exact amount with at least the currency's minor units, and its currency code. */
export function formatMoney(amount: string, currency: string, fmt: Omit<DecimalFormat, 'minFraction'> = {}): string {
  return `${formatDecimal(amount, { ...fmt, minFraction: currencyMinorUnits(currency) })} ${currency}`;
}

/** Quantities: grouped, no padding, trailing zeros dropped ("100.000000" -> "100"). */
export function formatQuantity(value: string): string {
  return formatDecimal(value, { minFraction: 0 });
}
