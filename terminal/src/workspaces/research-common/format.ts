/**
 * Formatting shared by the Company, Macro and Research workspaces.
 *
 * Decimal strings from the API (amounts, rates, ratios) are never turned into JavaScript numbers here:
 * `shiftDecimal` only moves the decimal point. Research statistics arrive as JSON floats (the research
 * engine documents them as float statistics); `formatStat` and `formatFractionPct` format those, and say so.
 */
import type { ReactElement } from 'react';
import { missingNumber, noValue } from '../../ui/unavailable';
import { formatDecimal, parseDecimal } from '../../format/decimal';

/** Multiply a decimal string by 10^places exactly: ("0.3878787879", 2) -> "38.78787879". Null if not a decimal. */
export function shiftDecimal(value: string, places: number): string | null {
  const p = parseDecimal(value);
  if (!p || !Number.isInteger(places)) return null;
  let int = p.int;
  let frac = p.frac;
  if (places >= 0) {
    const move = frac.slice(0, places).padEnd(places, '0');
    int = (int + move).replace(/^0+(?=\d)/, '');
    frac = frac.slice(places);
  } else {
    const n = -places;
    const padded = int.padStart(n + 1, '0');
    frac = padded.slice(padded.length - n) + frac;
    int = padded.slice(0, padded.length - n).replace(/^0+(?=\d)/, '');
  }
  frac = frac.replace(/0+$/, '');
  const zero = /^0*$/.test(int) && frac === '';
  const body = frac ? `${int}.${frac}` : int;
  return p.negative && !zero ? `-${body}` : body;
}

/** "0.3878787879" (a ratio) -> "38.78787879 %", exact. */
export function ratioAsPercent(value: string): string | null {
  const s = shiftDecimal(value, 2);
  return s === null ? null : `${formatDecimal(s)} %`;
}

/** True for an ISO 4217-shaped currency code (the API's money units: "USD", "EUR"). */
export function isCurrencyCode(unit: string | null | undefined): boolean {
  return typeof unit === 'string' && /^[A-Z]{3}$/.test(unit);
}

/**
 * Epoch milliseconds of an API instant ("2026-10-22T16:17:52.134721000Z" or "...+00:00"). The API writes
 * nanosecond fractions; they are cut to milliseconds for display only. Null when it is not an instant.
 */
export function isoToMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(iso.trim());
  if (!m) return null;
  const ms = (m[3] ?? '').slice(0, 3).padEnd(3, '0');
  const t = Date.parse(`${m[1]}T${m[2]}.${ms}${m[4]}`);
  return Number.isNaN(t) ? null : t;
}

export type InstantCheck = { ok: true; value: string } | { ok: false; error: string };

/**
 * The API's as-of rule (routes/company.py parse_instant): empty means now; a plain date YYYY-MM-DD means the end
 * of that UTC day; an instant must carry a zone ("Z" or "+hh:mm"). Checked here so the form can say why before
 * the server answers 400; the server stays the authority.
 */
export function checkInstant(text: string, name = 'as of'): InstantCheck {
  const t = text.trim();
  if (!t) return { ok: true, value: '' };
  if (/^\d{4}-\d{2}-\d{2}$/.test(t)) {
    if (!isCalendarDate(t)) return { ok: false, error: `${name}: ${t} is not a calendar date` };
    return { ok: true, value: t };
  }
  const m = /^(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?$/.exec(t);
  if (!m) return { ok: false, error: `${name}: use a date (2026-08-15) or an instant with a zone (2026-08-15T20:00:00Z)` };
  if (!m[2]) return { ok: false, error: `${name} must include a time zone (e.g. 2026-10-22T20:00:00Z) or be a plain date` };
  // Date.parse rolls an impossible day over (2026-02-30 becomes 2026-03-02); the API refuses it, so check the date part.
  if (!isCalendarDate(m[1]!) || Number.isNaN(Date.parse(t.replace(/(\.\d{3})\d+/, '$1')))) return { ok: false, error: `${name}: ${t} is not a valid instant` };
  return { ok: true, value: t };
}

/** True for a real calendar date written YYYY-MM-DD (no rollover: 2026-02-30 is false). */
export function isCalendarDate(text: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
  const d = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === text;
}

/**
 * Commands that would populate a source, as named in an API "unavailable" reason, e.g.
 * "research store not initialised: ingest an issuer first (python -m unknown.data.sec ingest --cik <CIK>)" or
 * "... run `python -m unknown.research.study` first". Returned verbatim, in order, without duplicates.
 */
export function extractCommands(reason: string | null | undefined): string[] {
  if (!reason) return [];
  const out: string[] = [];
  const add = (c: string) => {
    const s = c.trim().replace(/[.,]+$/, '');
    if (s && !out.includes(s)) out.push(s);
  };
  for (const m of reason.matchAll(/`([^`]*python[^`]*)`/g)) add(m[1] ?? '');
  const unquoted = reason.replace(/`[^`]*`/g, ' ');
  for (const m of unquoted.matchAll(/python3? -m [^;()`]+/g)) add(m[0]);
  return out;
}

/**
 * The documented commands that populate each research-side source, complete with the arguments their command line
 * requires (data/sec.py, data/sources/treasury.py and ecb.py `main`). The SEC, Treasury
 * and ECB ones are exactly what the backend's own unavailable reasons write (api/routes/company.py NO_STORE and
 * macro). They are shown only for a source the API reports as
 * unavailable, so a reason that names no command, or only part of one, still says how to fill it.
 */
export const POPULATE = {
  sec: 'python -m unknown.data.sec ingest --cik <CIK>',
  treasury: 'python -m unknown.data.sources.treasury ingest --month YYYYMM',
  ecb: 'python -m unknown.data.sources.ecb ingest --last 1',
} as const;

/**
 * Commands to show under an unavailable reason: those the reason names (verbatim, first), then the documented
 * ones. A named command that is a prefix of a documented one ("python -m unknown.data.sources.treasury" in
 * "(ingest with python -m unknown.data.sources.treasury)") is replaced by the complete documented command.
 */
export function populateCommands(reason: string | null | undefined, documented: readonly string[] = []): string[] {
  const named = extractCommands(reason).filter((c) => !documented.some((d) => d !== c && d.startsWith(`${c} `)));
  const out = [...named];
  for (const d of documented) if (!out.includes(d)) out.push(d);
  return out;
}

/**
 * A float statistic for display, with `digits` significant digits ("0.1297", "-0.00006447"). Research
 * metrics are floats by design; never use this for money or for API decimal strings.
 */
export function formatStat(x: number | null | undefined, digits = 4): string | ReactElement {
  if (x === null || x === undefined) return noValue('NOT_REPORTED');
  if (!Number.isFinite(x)) return String(x);
  if (x === 0) return '0';
  const abs = Math.abs(x);
  if (abs >= 1e-6 && abs < 1e15) {
    const s = x.toPrecision(digits);
    if (!s.includes('e')) return s.includes('.') ? s.replace(/\.?0+$/, '') : s;
  }
  return x.toExponential(digits - 1);
}

/** A float fraction as a percentage with fixed decimals: 0.35073 -> "35.07 %". */
export function formatFractionPct(x: number | null | undefined, decimals = 2): string | ReactElement {
  const missing = missingNumber(x);
  if (missing || x === null || x === undefined) return missing ?? noValue('NOT_REPORTED');
  const v = x * 100;
  const s = v.toFixed(decimals);
  return `${/^-0\.?0*$/.test(s) ? s.slice(1) : s} %`;
}

/** "12" when everything is listed, "50 of 73" when the API cut the list short (never the total alone). */
export function shownOf(shown: number, total: number): string {
  return shown === total ? String(total) : `${shown} of ${total}`;
}

/** "SEC-CIK-0009000001" -> 9000001; plain digits also accepted. Null for anything else. */
export function cikOf(entityId: string | null | undefined): number | null {
  if (!entityId) return null;
  const m = /^(?:SEC-CIK-|CIK)?0*(\d{1,10})$/i.exec(entityId.trim());
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/** "net_income_growth_yoy" -> "net income growth yoy" (display only; the id stays visible next to it). */
export function humanize(id: string): string {
  return id.replace(/_/g, ' ');
}
