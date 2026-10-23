/**
 * Live-number primitives: <PriceCell>, useTickFlash() and <DataTypeBadge>.
 *
 * A flash restarts a CSS animation on the element through a ref (no state, no re-render of the tree around it), so
 * a grid of hundreds of cells can tick without React re-rendering the grid. Decimals arrive as exact strings
 * (docs/design/marketdata-stream-contract.md) and are compared exactly, never through floats. A missing value
 * renders as a dash, never as zero. A SYNTHETIC value always carries its label.
 */
import { memo, useLayoutEffect, useRef, type RefObject } from 'react';

export type DataType = 'REALTIME' | 'DELAYED' | 'FROZEN' | 'SYNTHETIC';
export type TickDirection = 'up' | 'down' | 'unch';

export const DATA_TYPES: readonly DataType[] = ['REALTIME', 'DELAYED', 'FROZEN', 'SYNTHETIC'];

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

const SHORT: Record<DataType, string> = { REALTIME: 'RT', DELAYED: 'DLY', FROZEN: 'FRZ', SYNTHETIC: 'SYN' };
const LONG: Record<DataType, string> = {
  REALTIME: 'Real-time data',
  DELAYED: 'Delayed data',
  FROZEN: 'Frozen data: the last value from a closed session',
  SYNTHETIC: 'Synthetic data: generated, not from any market',
};

/** Source / data-type label. `compact` uses the short form (DLY, FRZ, SYN); the full word is in the tooltip. */
export function DataTypeBadge({ type, source, compact }: { type: DataType; source?: string | null; compact?: boolean }) {
  const text = compact ? SHORT[type] : type;
  const title = `${LONG[type]}${source ? ` from ${source}` : ''}`;
  return (
    <span className={`badge src-badge src-${type.toLowerCase()}`} title={title}>
      {source && !compact ? `${source} · ${text}` : text}
    </span>
  );
}

export interface PriceCellProps {
  /** Exact decimal string from the stream; null or undefined renders a dash. */
  value: string | null | undefined;
  /** Data type of the value; anything but REALTIME shows a compact label. SYNTHETIC is always labelled. */
  dataType?: DataType | null;
  source?: string | null;
  /** 'none' hides the DELAYED/FROZEN label where a column header already says it; SYNTHETIC is still shown. */
  badge?: 'auto' | 'none';
  /** The value is stale (no update within the expected interval): dimmed and marked. */
  stale?: boolean;
  /** Accessible name, e.g. "AAPL.XNAS last". */
  label?: string;
  className?: string;
}

/** One live number: tabular digits, flash on change, direction glyph (colour is never the only cue). */
export const PriceCell = memo(function PriceCell({ value, dataType, source, badge = 'auto', stale, label, className }: PriceCellProps) {
  const ref = useTickFlash<HTMLSpanElement>(value ?? null);
  const showBadge = !!dataType && dataType !== 'REALTIME' && (badge === 'auto' || dataType === 'SYNTHETIC');
  return (
    <span className={`price-cell num${stale ? ' is-stale' : ''}${className ? ` ${className}` : ''}`}>
      {label ? <span className="sr-only">{label} </span> : null}
      <span ref={ref} className="price-value" data-dir="unch">
        {value ?? '—'}
      </span>
      {stale ? (
        <span className="stale-mark" title="Stale: no update within the expected interval">
          STALE
        </span>
      ) : null}
      {showBadge && dataType ? <DataTypeBadge type={dataType} source={source} compact /> : null}
    </span>
  );
});
