/**
 * Timestamp and age formatting. Server timestamps are integer nanoseconds (approximate as JS numbers,
 * see src/api/types.ts); most of this works at millisecond resolution. Where the exact value is shown, the
 * API's `*_ns_str` decimal string is read with BigInt (`nsStringToMs`, `formatTimestampNsExact`): no float
 * ever rounds a nanosecond (or, near a millisecond boundary, the millisecond).
 */

export function nsToMs(ns: number): number {
  return Math.floor(ns / 1_000_000);
}

function pad(n: number, width = 2): string {
  return String(n).padStart(width, '0');
}

export type TimeZoneMode = 'utc' | 'local';

function formatWithFraction(ms: number, zone: TimeZoneMode, fraction: (d: Date, utc: boolean) => string): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return 'invalid time';
  const utc = zone === 'utc';
  const y = utc ? d.getUTCFullYear() : d.getFullYear();
  const mo = (utc ? d.getUTCMonth() : d.getMonth()) + 1;
  const da = utc ? d.getUTCDate() : d.getDate();
  const h = utc ? d.getUTCHours() : d.getHours();
  const mi = utc ? d.getUTCMinutes() : d.getMinutes();
  const s = utc ? d.getUTCSeconds() : d.getSeconds();
  return `${y}-${pad(mo)}-${pad(da)} ${pad(h)}:${pad(mi)}:${pad(s)}${fraction(d, utc)}${utc ? ' UTC' : ''}`;
}

/** "2026-10-22 13:30:28 UTC" (or local time without suffix). `ms` adds ".002". */
export function formatTimestampMs(ms: number, zone: TimeZoneMode = 'utc', withMs = false): string {
  return formatWithFraction(ms, zone, (d, utc) => (withMs ? `.${pad(utc ? d.getUTCMilliseconds() : d.getMilliseconds(), 3)}` : ''));
}

const NS_INTEGER = /^-?\d+$/;
const NS_PER_MS = 1_000_000n;
const NS_PER_S = 1_000_000_000n;

function floorDiv(n: bigint, d: bigint): bigint {
  const q = n / d;
  return n % d !== 0n && n < 0n ? q - 1n : q;
}

/** Whether `ns` is the API's exact nanosecond string (a decimal integer). */
export function isNsString(ns: unknown): ns is string {
  return typeof ns === 'string' && NS_INTEGER.test(ns);
}

/** Milliseconds since the epoch (floor) of an exact `*_ns_str`, computed without floats; null when malformed. */
export function nsStringToMs(ns: string): number | null {
  return isNsString(ns) ? Number(floorDiv(BigInt(ns), NS_PER_MS)) : null;
}

/** "2026-10-22 13:30:28.123456789 UTC": every nanosecond of the API's `*_ns_str`, exactly. */
export function formatTimestampNsExact(ns: string, zone: TimeZoneMode = 'utc'): string {
  if (!isNsString(ns)) return 'invalid time';
  const v = BigInt(ns);
  const seconds = floorDiv(v, NS_PER_S);
  const nanos = (v - seconds * NS_PER_S).toString().padStart(9, '0');
  return formatWithFraction(Number(seconds) * 1000, zone, () => `.${nanos}`);
}

export function formatTimestampNs(ns: number, zone: TimeZoneMode = 'utc', withMs = false): string {
  return formatTimestampMs(nsToMs(ns), zone, withMs);
}

/** Machine-readable value for <time dateTime>. */
export function isoFromMs(ms: number): string {
  const d = new Date(ms);
  return Number.isNaN(d.getTime()) ? '' : d.toISOString();
}

/** Compact age: "850 ms", "4.2 s", "37 s", "4 m 03 s", "2 h 05 m", "3 d 04 h". Negative ages mean clock skew. */
export function formatAge(ms: number): string {
  if (!Number.isFinite(ms)) return 'unknown';
  if (ms < 0) return `${formatAge(-ms)} ahead`;
  if (ms < 1000) return `${Math.floor(ms)} ms`;
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.floor(s)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m ${pad(Math.floor(s % 60))} s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${pad(m % 60)} m`;
  const d = Math.floor(h / 24);
  return `${d} d ${pad(h % 24)} h`;
}

/** "13:30:28" in UTC or local time, for the header clock. */
export function formatClock(ms: number, zone: TimeZoneMode): string {
  const d = new Date(ms);
  return zone === 'utc'
    ? `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
    : `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
