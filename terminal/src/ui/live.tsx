/**
 * Live-number primitives: <PriceCell>, useTickFlash() and <DataTypeBadge>.
 *
 * A flash restarts a CSS animation on the element through a ref (no state, no re-render of the tree around it), so
 * a grid of hundreds of cells can tick without React re-rendering the grid. Decimals arrive as exact strings
 * (docs/design/marketdata-stream-contract.md) and are compared exactly, never through floats. A missing value
 * renders as an em dash with its reason (ui/unavailable.tsx), never as zero. Only real data types exist: REALTIME,
 * DELAYED and FROZEN. A value of any other data type is an error and is never displayed as data.
 */
import { memo, useLayoutEffect, useRef, type RefObject } from 'react';
import { ErrorValue, NoValue, StaleValue, type Reason } from './unavailable';

export type DataType = 'REALTIME' | 'DELAYED' | 'FROZEN';
export type TickDirection = 'up' | 'down' | 'unch';

export const DATA_TYPES: readonly DataType[] = ['REALTIME', 'DELAYED', 'FROZEN'];

/** True only for the real market-data types; anything else (including a missing type) is refused. */
export function isDataType(x: unknown): x is DataType {
  return typeof x === 'string' && (DATA_TYPES as readonly string[]).includes(x);
}

/** The error detail for a value whose data type is not a real market-data type. */
export function refusedTypeDetail(dataType: unknown, source?: string | null): string {
  const t = typeof dataType === 'string' && dataType ? dataType : 'missing';
  return `Refused: data type ${t}${source ? ` from ${source}` : ''} is not real market data.`;
}

const DECIMAL_RE = /^([+-])?(\d+)(?:\.(\d+))?$/;

/** Exact comparison of two decimal strings: -1, 0 or 1; null when either is not a plain decimal. */
export function compareDecimal(a: string, b: string): -1 | 0 | 1 | null {
  const ma = DECIMAL_RE.exec(a.trim());
  const mb = DECIMAL_RE.exec(b.trim());
  if (!ma || !mb) return null;
  const norm = (m: RegExpExecArray) => {
    const int = (m[2] ?? '0').replace(/^0+(?=\d)/, '');
    const frac = (m[3] ?? '').replace(/0+$/, '');
    const zero = /^0*$/.test(int) && frac === '';
    return { neg: m[1] === '-' && !zero, int, frac };
  };
  const x = norm(ma);
  const y = norm(mb);
  if (x.neg !== y.neg) return x.neg ? -1 : 1;
  let mag: -1 | 0 | 1 = 0;
  if (x.int.length !== y.int.length) mag = x.int.length < y.int.length ? -1 : 1;
  else if (x.int !== y.int) mag = x.int < y.int ? -1 : 1;
  else {
    const n = Math.max(x.frac.length, y.frac.length);
    const fx = x.frac.padEnd(n, '0');
    const fy = y.frac.padEnd(n, '0');
    mag = fx === fy ? 0 : fx < fy ? -1 : 1;
  }
  return x.neg ? ((-mag || 0) as -1 | 0 | 1) : mag;
}

/** Direction of a change from `prev` to `next` (unch when unknown or equal). */
export function tickDirection(prev: string | null | undefined, next: string | null | undefined): TickDirection {
  if (prev == null || next == null) return 'unch';
  const c = compareDecimal(next, prev);
  return c === 1 ? 'up' : c === -1 ? 'down' : 'unch';
}

/** Restart the flash animation on an element (usable from a stream callback without React). */
export function flashElement(el: Element | null, dir: TickDirection): void {
  if (!el || dir === 'unch') return;
  el.classList.remove('tick-flash-up', 'tick-flash-down');
  // Reading layout forces the browser to drop the old animation so re-adding the class restarts it.
  void (el as HTMLElement).offsetWidth;
  el.classList.add(dir === 'up' ? 'tick-flash-up' : 'tick-flash-down');
  el.setAttribute('data-dir', dir);
}

/**
 * Flash the referenced element up or down whenever `value` changes. Returns the ref to attach (or pass your own).
 * The first value never flashes; null values never flash.
 */
export function useTickFlash<T extends HTMLElement = HTMLElement>(value: string | null | undefined, ref?: RefObject<T | null>): RefObject<T | null> {
  const own = useRef<T | null>(null);
  const target = ref ?? own;
  const prev = useRef<string | null | undefined>(value);
  useLayoutEffect(() => {
    const before = prev.current;
    prev.current = value;
    if (before === value) return;
    flashElement(target.current, tickDirection(before, value));
  }, [value, target]);
  return target;
}

const SHORT: Record<DataType, string> = { REALTIME: 'RT', DELAYED: 'DLY', FROZEN: 'FRZ' };
const LONG: Record<DataType, string> = {
  REALTIME: 'Real-time data',
  DELAYED: 'Delayed data',
  FROZEN: 'Frozen data: the last value from a closed session',
};

/**
 * Source / data-type label. `compact` uses the short form (RT, DLY, FRZ); the full word is in the tooltip. A type
 * that is not a real market-data type renders as an error, never as a label.
 */
export function DataTypeBadge({ type, source, compact }: { type: DataType | string; source?: string | null; compact?: boolean }) {
  if (!isDataType(type)) return <ErrorValue reason="ERROR" detail={refusedTypeDetail(type, source)} />;
  const text = compact ? SHORT[type] : type;
  const title = `${LONG[type]}${source ? ` from ${source}` : ''}`;
  return (
    <span className={`badge src-badge src-${type.toLowerCase()}`} title={title}>
      {source && !compact ? `${source} · ${text}` : text}
    </span>
  );
}

export interface PriceCellProps {
  /** Exact decimal string from the stream; null or undefined renders an em dash with `reason`. */
  value: string | null | undefined;
  /** Why the value is missing (shown with the dash); default NOT_REPORTED. */
  reason?: Reason;
  /** Extra detail for the reason (e.g. the stream status message). */
  reasonDetail?: string | null;
  /** Data type of the value; DELAYED/FROZEN show a compact label. Any other type is an error, never a value. */
  dataType?: DataType | string | null;
  source?: string | null;
  /** 'none' hides the DELAYED/FROZEN label where a column header already says it. */
  badge?: 'auto' | 'none';
  /** The value is stale (no update within the expected interval): kept, with its age in the warning token. */
  stale?: boolean;
  /** Age of a stale value (ms). */
  ageMs?: number | null;
  /** Accessible name, e.g. "AAPL.XNAS last". */
  label?: string;
  className?: string;
}

/** One live number: tabular digits, flash on change, direction glyph (colour is never the only cue). */
export const PriceCell = memo(function PriceCell({ value, reason, reasonDetail, dataType, source, badge = 'auto', stale, ageMs, label, className }: PriceCellProps) {
  const refused = dataType !== null && dataType !== undefined && !isDataType(dataType);
  const shown = refused ? null : (value ?? null);
  const ref = useTickFlash<HTMLSpanElement>(shown);
  const cls = `price-cell num${stale ? ' is-stale' : ''}${className ? ` ${className}` : ''}`;
  if (refused) {
    return (
      <span className={cls}>
        <ErrorValue reason="ERROR" detail={refusedTypeDetail(dataType, source)} label={label} />
      </span>
    );
  }
  if (shown === null) {
    return (
      <span className={cls}>
        <NoValue reason={reason ?? 'NOT_REPORTED'} detail={reasonDetail} label={label} />
      </span>
    );
  }
  const showBadge = isDataType(dataType) && dataType !== 'REALTIME' && badge === 'auto';
  const number = (
    <span ref={ref} className="price-value" data-dir="unch">
      {shown}
    </span>
  );
  return (
    <span className={cls}>
      {label ? <span className="sr-only">{label} </span> : null}
      {stale ? <StaleValue ageMs={ageMs}>{number}</StaleValue> : number}
      {showBadge && isDataType(dataType) ? <DataTypeBadge type={dataType} source={source} compact /> : null}
    </span>
  );
});
