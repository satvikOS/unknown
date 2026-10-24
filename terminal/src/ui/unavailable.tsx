/**
 * The one way the terminal shows a value it does not have (owner rule 2026-10-23: only real data and real streams).
 *
 * - <NoValue reason>: an em dash (—) in the muted token. The reason code and a sentence are in the tooltip, in
 *   visually hidden text for assistive technology, and in `data-reason` for machines; when the row or card that
 *   holds it has keyboard focus the code is shown inline next to the dash.
 * - <ErrorValue reason>: "ERR" in the alert token, with the reason the same way. An error is never shown as a dash
 *   or as a value.
 * - <StaleValue ageMs>: the last value, kept, with its age in the warning token.
 *
 * A missing value is never rendered as 0, "N/A", a blank or a made-up value. Reasons the API sends that are not in
 * the vocabulary below are shown verbatim.
 */
import { createContext, createElement, useContext, type ReactElement, type ReactNode } from 'react';
import { formatAge } from '../format/time';

export const REASON_CODES = ['NOT_ENTITLED', 'NO_SOURCE', 'NOT_REPORTED', 'NOT_APPLICABLE', 'STALE', 'ERROR'] as const;
export type ReasonCode = (typeof REASON_CODES)[number];
/** A known code, or any reason string the API sent (shown verbatim). */
export type Reason = ReasonCode | (string & {});

const SENTENCE: Record<ReasonCode, string> = {
  NOT_ENTITLED: 'Not entitled: this account has no data entitlement for this value.',
  NO_SOURCE: 'No source: no real data source is configured for this value.',
  NOT_REPORTED: 'Not reported: the source has not reported this value.',
  NOT_APPLICABLE: 'Not applicable: this value does not apply here.',
  STALE: 'Stale: no update within the expected interval.',
  ERROR: 'Error: the value could not be obtained.',
};

export function isReasonCode(reason: string): reason is ReasonCode {
  return (REASON_CODES as readonly string[]).includes(reason);
}

/** The human sentence for a reason; an unknown reason is quoted verbatim. */
export function reasonSentence(reason: string): string {
  return isReasonCode(reason) ? SENTENCE[reason] : `Reported by the source as ${reason}.`;
}

/** "CODE: sentence detail" (the tooltip and the accessible text). */
export function describeReason(reason: string, detail?: string | null): string {
  const d = detail && detail.trim() ? ` ${detail.trim()}` : '';
  return `${reason}: ${reasonSentence(reason)}${d}`;
}

/** Normalise an API reason (e.g. a stream status state) to a display reason; empty means ERROR. */
export function toReason(raw: string | null | undefined): Reason {
  const r = (raw ?? '').trim();
  return r === '' ? 'ERROR' : r;
}

/**
 * Inside a grid whose rows take keyboard focus the marker itself is not a tab stop (the focused row shows every
 * reason inline instead); elsewhere a marker can opt in to focus with `focusable`.
 */
const InGrid = createContext(false);
export function InGridProvider({ children }: { children: ReactNode }) {
  return <InGrid.Provider value={true}>{children}</InGrid.Provider>;
}

export interface NoValueProps {
  /** Machine-readable reason: a code from REASON_CODES or the API's own reason string. */
  reason: Reason;
  /** Extra detail appended to the sentence (e.g. the API's message). */
  detail?: string | null;
  /** What the value is, for assistive technology (e.g. "AAPL.XNAS last"). */
  label?: string;
  /** Make the marker a tab stop outside grids so its reason can be read by keyboard. */
  focusable?: boolean;
  className?: string;
}

function a11yText(kind: string, label: string | undefined, text: string): string {
  return `${label ? `${label}: ` : ''}${kind}. ${text}`;
}

/** A value that is not available: an em dash with its reason. ERROR renders as <ErrorValue>. */
export function NoValue({ reason, detail, label, focusable, className }: NoValueProps) {
  const inGrid = useContext(InGrid);
  if (reason === 'ERROR') return <ErrorValue reason={reason} detail={detail} label={label} focusable={focusable} className={className} />;
  const text = describeReason(reason, detail);
  return (
    <span
      className={`nv nv-missing${className ? ` ${className}` : ''}`}
      data-reason={reason}
      title={text}
      tabIndex={focusable && !inGrid ? 0 : undefined}
    >
      <span aria-hidden="true">—</span>
      <span className="sr-only">{a11yText('not available', label, text)}</span>
    </span>
  );
}

/** An error in place of a value: "ERR" in the alert token, with the reason. */
export function ErrorValue({ reason = 'ERROR', detail, label, focusable, className }: Partial<NoValueProps>) {
  const inGrid = useContext(InGrid);
  const code = reason ?? 'ERROR';
  const text = describeReason(code, detail);
  return (
    <span
      className={`nv nv-error${className ? ` ${className}` : ''}`}
      data-reason={code}
      title={text}
      tabIndex={focusable && !inGrid ? 0 : undefined}
    >
      <span aria-hidden="true">ERR</span>
      <span className="sr-only">{a11yText('error', label, text)}</span>
    </span>
  );
}

export interface StaleValueProps {
  children: ReactNode;
  /** Age of the value (ms); null when the age is not known. */
  ageMs: number | null | undefined;
  label?: string;
}

/** The last value of a stale series, kept and marked with its age in the warning token. */
export function StaleValue({ children, ageMs, label }: StaleValueProps) {
  const age = ageMs === null || ageMs === undefined ? null : formatAge(ageMs);
  const text = describeReason('STALE', age ? `Last update ${age} ago.` : null);
  return (
    <span className="nv nv-stale" data-reason="STALE" title={text}>
      {children}
      <span className="nv-age" aria-hidden="true">
        {age ?? 'STALE'}
      </span>
      <span className="sr-only">{a11yText('stale', label, text)}</span>
    </span>
  );
}

/** A <NoValue> element, for formatting helpers in .ts files that cannot write JSX. */
export function noValue(reason: Reason, detail?: string | null): ReactElement {
  return createElement(NoValue, { reason, detail });
}

/** The marker for a number that is absent (NOT_REPORTED) or not finite (NOT_APPLICABLE), or null when it is usable. */
export function missingNumber(x: number | null | undefined): ReactElement | null {
  if (x === null || x === undefined) return noValue('NOT_REPORTED');
  if (!Number.isFinite(x)) return noValue('NOT_APPLICABLE', `The statistic is ${String(x)}.`);
  return null;
}

/**
 * Text for a formatted value inside prose, a tooltip or a summary (where an element cannot go): the value, or the
 * reason in words ("not reported", "not applicable", "error") for a <NoValue> marker.
 */
export function asText(v: string | ReactElement): string {
  if (typeof v === 'string') return v;
  const reason = (v.props as { reason?: unknown }).reason;
  return typeof reason === 'string' ? reason.toLowerCase().replace(/_/g, ' ') : 'not reported';
}

/** `value` when present, otherwise <NoValue reason>. Use for any text value that may be absent. */
export function orNoValue(value: ReactNode, reason: Reason, detail?: string | null): ReactNode {
  return value === null || value === undefined || value === '' ? <NoValue reason={reason} detail={detail} /> : value;
}
