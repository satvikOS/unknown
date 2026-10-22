/**
 * Timestamp and age formatting. Server timestamps are integer nanoseconds (approximate as JS numbers,
 * see src/api/types.ts); everything here works at millisecond resolution.
 */

export function nsToMs(ns: number): number {
  return Math.floor(ns / 1_000_000);
}

function pad(n: number, width = 2): string {
  return String(n).padStart(width, '0');
}

export type TimeZoneMode = 'utc' | 'local';

/** "2026-10-22 13:30:28 UTC" (or local time without suffix). `ms` adds ".002". */
export function formatTimestampMs(ms: number, zone: TimeZoneMode = 'utc', withMs = false): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return 'invalid time';
  const utc = zone === 'utc';
  const y = utc ? d.getUTCFullYear() : d.getFullYear();
  const mo = (utc ? d.getUTCMonth() : d.getMonth()) + 1;
  const da = utc ? d.getUTCDate() : d.getDate();
  const h = utc ? d.getUTCHours() : d.getHours();
  const mi = utc ? d.getUTCMinutes() : d.getMinutes();
  const s = utc ? d.getUTCSeconds() : d.getSeconds();
  const msPart = withMs ? `.${pad(utc ? d.getUTCMilliseconds() : d.getMilliseconds(), 3)}` : '';
  return `${y}-${pad(mo)}-${pad(da)} ${pad(h)}:${pad(mi)}:${pad(s)}${msPart}${utc ? ' UTC' : ''}`;
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
