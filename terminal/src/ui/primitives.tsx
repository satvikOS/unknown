/**
 * Display primitives. Every amount shows its currency; every status shows text and a glyph, with
 * colour as a supplement only; every timestamp is a <time> element with a machine-readable value.
 */
import type { ReactNode } from 'react';
import type { AccountMode } from '../api/types';
import { currencyMinorUnits, formatDecimal, isDecimal, signOf } from '../format/decimal';
import { formatAge, formatTimestampMs, formatTimestampNsExact, isNsString, isoFromMs, nsStringToMs, nsToMs } from '../format/time';
import { useNow } from '../state/react';
import { ErrorValue, NoValue, type Reason } from './unavailable';

export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral';

const GLYPH: Record<Tone, string> = { ok: '●', warn: '▲', bad: '■', info: '◆', neutral: '○' };

// ------------------------------------------------------------------ Badge

export interface BadgeProps {
  children: ReactNode;
  tone?: Tone;
  title?: string;
}

/** Small uppercase label (e.g. SYSTEM, LIVE). */
export function Badge({ children, tone = 'neutral', title }: BadgeProps) {
  return (
    <span className={`badge tone-${tone}`} title={title}>
      {children}
    </span>
  );
}

/** The error detail for a value the API marks as derived from fixture (test) data: refused, never displayed. */
export const FIXTURE_REFUSED = 'Refused: derived from FIXTURE (test) data, which is not real data.';

/** In place of a value the API marks as fixture-derived: an error, never the value. */
export function FixtureRefused({ detail, label }: { detail?: string; label?: string }) {
  return <ErrorValue reason="ERROR" detail={detail ? `${FIXTURE_REFUSED} ${detail}` : FIXTURE_REFUSED} label={label} />;
}

const MODE_VAR: Record<AccountMode, string> = {
  SIMULATED: 'var(--mode-simulated)',
  PAPER: 'var(--mode-paper)',
  LIVE: 'var(--mode-live)',
};

export function modeColor(mode: string | null | undefined): string {
  return mode && mode in MODE_VAR ? MODE_VAR[mode as AccountMode] : 'var(--border-strong)';
}

/** Account mode in text and colour (ADR-0013: shown permanently). */
export function ModeBadge({ mode }: { mode: string | null | undefined }) {
  const known = mode === 'SIMULATED' || mode === 'PAPER' || mode === 'LIVE';
  return (
    <span
      className="badge mode-badge"
      data-mode={mode ?? 'UNKNOWN'}
      style={{ ['--mode-color' as string]: modeColor(mode) }}
      title={known ? `Account mode: ${mode}` : 'Account mode not yet known'}
    >
      <span aria-hidden="true">{mode === 'LIVE' ? '■' : '◆'}</span>
      {known ? mode : 'MODE UNKNOWN'}
    </span>
  );
}

// ------------------------------------------------------------------ StatusPill

export interface StatusPillProps {
  /** Visible status text; always present so status is never conveyed by colour alone. */
  label: string;
  tone: Tone;
  /** Extra explanation (tooltip and screen-reader text). */
  detail?: string;
  compact?: boolean;
}

export function StatusPill({ label, tone, detail, compact }: StatusPillProps) {
  return (
    <span className={`pill tone-${tone}${compact ? ' compact' : ''}`} title={detail}>
      <span className="pill-glyph" aria-hidden="true">
        {GLYPH[tone]}
      </span>
      <span className="pill-text">{label}</span>
      {detail ? <span className="sr-only">: {detail}</span> : null}
    </span>
  );
}

// ------------------------------------------------------------------ Money

/** How an amount must be read (book Ch. 22: realized, unrealized, settled, unsettled, protected). */
export type MoneyKind =
  | 'realized'
  | 'unrealized'
  | 'settled'
  | 'unsettled'
  | 'protected'
  | 'available'
  | 'held'
  | 'authorized'
  | 'estimated'
  | 'cost'
  | 'marked';

export interface MoneyProps {
  /** Exact decimal string from the API. null/undefined renders an em dash with `reason`. */
  amount: string | null | undefined;
  currency: string;
  /** Rendered as a label after the amount when `showKind` is set; always included in the tooltip. */
  kind?: MoneyKind;
  showKind?: boolean;
  /** P&L style: explicit "+" for gains, and sign colour as a supplement to the sign. */
  signed?: boolean;
  /** Why the amount is not available (machine-readable); default NOT_REPORTED. */
  reason?: Reason;
  /** Detail when the amount is not available, e.g. "no quote". */
  unavailable?: string;
}

export function Money({ amount, currency, kind, showKind, signed, reason, unavailable }: MoneyProps) {
  if (amount === null || amount === undefined) {
    return (
      <span className="money">
        <NoValue reason={reason ?? 'NOT_REPORTED'} detail={unavailable} label={kind} />
      </span>
    );
  }
  if (!isDecimal(amount)) {
    return (
      <span className="money">
        <ErrorValue reason="ERROR" detail={`The API sent "${amount}" ${currency}, which is not a decimal amount.`} label={kind} />
      </span>
    );
  }
  const text = formatDecimal(amount, {
    minFraction: currencyMinorUnits(currency),
    signDisplay: signed ? 'always' : 'auto',
  });
  const sign = signOf(amount);
  const cls = signed && sign !== 0 ? (sign > 0 ? ' pos' : ' neg') : '';
  return (
    <span className={`money${cls}`} title={`${amount} ${currency}${kind ? ` (${kind})` : ''}`}>
      <span className="amt">{text}</span>
      <span className="ccy">{currency}</span>
      {showKind && kind ? (
        <span className="kind">
          <Badge>{kind}</Badge>
        </span>
      ) : null}
    </span>
  );
}

// ------------------------------------------------------------------ Qty

export interface QtyProps {
  value: string | null | undefined;
  /** Optional unit suffix, e.g. "sh". */
  unit?: string;
  /** Why the quantity is not available; default NOT_REPORTED. */
  reason?: Reason;
}

export function Qty({ value, unit, reason }: QtyProps) {
  if (value === null || value === undefined) return <NoValue reason={reason ?? 'NOT_REPORTED'} />;
  if (!isDecimal(value)) return <ErrorValue reason="ERROR" detail={`The API sent "${value}", which is not a decimal quantity.`} />;
  return (
    <span className="num" title={value}>
      {formatDecimal(value)}
      {unit ? <span className="muted"> {unit}</span> : null}
    </span>
  );
}

// ------------------------------------------------------------------ Timestamp & Age

export interface TimestampProps {
  /** Server nanoseconds (preferred) */
  ns?: number | null;
  /**
   * The API's exact `*_ns_str` for the same instant. When given it is used instead of `ns` (no float rounding),
   * the tooltip shows every nanosecond, and `withNs` shows them in the text.
   */
  nsStr?: string | null;
  /** or browser milliseconds. */
  ms?: number | null;
  zone?: 'utc' | 'local';
  withMs?: boolean;
  /** All nine fraction digits (needs `nsStr`). */
  withNs?: boolean;
  /** Text when absent, for a state that is a fact (e.g. "not finished"). */
  never?: string;
  /** When absent the time is a missing value: an em dash with this reason instead of the `never` text. */
  reason?: Reason;
}

export function Timestamp({ ns, nsStr, ms, zone = 'utc', withMs = false, withNs = false, never = 'never', reason }: TimestampProps) {
  const exact = isNsString(nsStr) ? nsStr : null;
  const at = ms ?? (exact !== null ? nsStringToMs(exact) : ns !== null && ns !== undefined ? nsToMs(ns) : null);
  if (at === null || at === undefined) return reason ? <NoValue reason={reason} /> : <span className="muted">{never}</span>;
  const other = zone === 'utc' ? 'local' : 'utc';
  return (
    <time className="num" dateTime={isoFromMs(at)} title={exact !== null ? formatTimestampNsExact(exact, other) : formatTimestampMs(at, other, true)}>
      {exact !== null && withNs ? formatTimestampNsExact(exact, zone) : formatTimestampMs(at, zone, withMs)}
    </time>
  );
}

export interface AgeProps {
  /** Age since a server timestamp (ns). */
  sinceNs?: number | null;
  /** Age reported by the server (e.g. a quote's age_ms) ... */
  ageMs?: number | null;
  /** ... at this browser time; the displayed age keeps growing from there. */
  observedAtMs?: number | null;
  /** Server-minus-browser clock offset for `sinceNs` (ms). */
  clockOffsetMs?: number;
  /** Beyond this age the value is labelled "stale" (text + amber). */
  staleAfterMs?: number;
}

export function Age({ sinceNs, ageMs, observedAtMs, clockOffsetMs = 0, staleAfterMs }: AgeProps) {
  const now = useNow(1000);
  let age: number | null = null;
  if (ageMs !== null && ageMs !== undefined) {
    age = ageMs + (observedAtMs ? Math.max(0, now - observedAtMs) : 0);
  } else if (sinceNs !== null && sinceNs !== undefined) {
    age = now + clockOffsetMs - nsToMs(sinceNs);
  }
  if (age === null) return <NoValue reason="NOT_REPORTED" detail="The age of this value is not known." />;
  const stale = staleAfterMs !== undefined && age > staleAfterMs;
  return (
    <span className={`num${stale ? ' tone-warn' : ''}`} style={stale ? { color: 'var(--warn)' } : undefined}>
      {formatAge(age)}
      {stale ? ' (stale)' : ''}
    </span>
  );
}

// ------------------------------------------------------------------ misc

export function Kbd({ keys }: { keys: string }) {
  return (
    <span className="kbd-hint" aria-hidden="true">
      {keys.split(' ').map((k, i) => (
        <kbd key={i} style={{ marginLeft: i ? 2 : 0 }}>
          {k}
        </kbd>
      ))}
    </span>
  );
}

/** Short hash for display; full value in the tooltip. */
export function ShortId({ value, head = 10 }: { value: string | null | undefined; head?: number }) {
  if (!value) return <span className="muted">none</span>;
  return (
    <span className="cmd-id" title={value}>
      {value.length > head + 2 ? `${value.slice(0, head)}…` : value}
    </span>
  );
}

export function YesNo({ value, yes = 'yes', no = 'no', goodWhen }: { value: boolean; yes?: string; no?: string; goodWhen?: boolean }) {
  const tone: Tone = goodWhen === undefined ? 'neutral' : value === goodWhen ? 'ok' : 'warn';
  return <StatusPill label={value ? yes : no} tone={tone} compact />;
}
