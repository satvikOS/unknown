/**
 * Formatting shared by the Company, Macro and Research workspaces.
 *
 * Decimal strings from the API (amounts, rates, ratios) are never turned into JavaScript numbers here:
 * `shiftDecimal` only moves the decimal point. Research statistics arrive as JSON floats (the research
 * engine documents them as float statistics); `formatStat` and `formatFractionPct` format those, and say so.
 */
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
    const d = new Date(`${t}T00:00:00Z`);
    if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== t) return { ok: false, error: `${name}: ${t} is not a calendar date` };
    return { ok: true, value: t };
  }
  const m = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?$/.exec(t);
  if (!m) return { ok: false, error: `${name}: use a date (2026-08-15) or an instant with a zone (2026-08-15T20:00:00Z)` };
  if (!m[1]) return { ok: false, error: `${name} must include a time zone (e.g. 2026-10-22T20:00:00Z) or be a plain date` };
  if (Number.isNaN(Date.parse(t.replace(/(\.\d{3})\d+/, '$1')))) return { ok: false, error: `${name}: ${t} is not a valid instant` };
  return { ok: true, value: t };
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
 * A float statistic for display, with `digits` significant digits ("0.1297", "-0.00006447"). Research
 * metrics are floats by design; never use this for money or for API decimal strings.
 */
export function formatStat(x: number | null | undefined, digits = 4): string {
  if (x === null || x === undefined) return '—';
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
export function formatFractionPct(x: number | null | undefined, decimals = 2): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—';
  const v = x * 100;
  const s = v.toFixed(decimals);
  return `${/^-0\.?0*$/.test(s) ? s.slice(1) : s} %`;
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
