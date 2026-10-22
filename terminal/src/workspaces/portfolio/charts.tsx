/**
 * Inline SVG charts (no chart dependency; DEPENDENCIES.md). Each chart is a summary: the same
 * figures are in a table next to it. Money bars are scaled with exact BigInt ratios (`exactBars`);
 * series of statistical floats (unit values, VaR) are drawn from the floats the API returns.
 * Colours are the shared tokens; sign and exceptions are also shown by position and shape.
 */
import { useCallback, useEffect, useRef, useState, type RefCallback } from 'react';
import { formatMoney } from '../../format/decimal';
import { exactBars, fmtFloatMoney, linePath, type BacktestDay } from './model';

/**
 * The rendered width of the chart's container, so the SVG viewBox is drawn at 1:1 and its text stays at
 * the CSS font size on every screen (a fixed viewBox would scale 11 px labels to 20 px on a wide panel).
 */
function useChartWidth(fallback = 640): [RefCallback<HTMLElement>, number] {
  const [width, setWidth] = useState(fallback);
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el) return;
    const update = () => {
      const w = Math.round(el.getBoundingClientRect().width);
      if (w > 0) setWidth((prev) => (Math.abs(prev - w) > 1 ? Math.max(280, w) : prev));
    };
    update();
    if (typeof ResizeObserver !== 'undefined') {
      observer.current = new ResizeObserver(update);
      observer.current.observe(el);
    }
  }, []);
  useEffect(() => () => observer.current?.disconnect(), []);
  return [ref, width];
}

// ------------------------------------------------------------------ horizontal bars (exact)

export interface BarItem {
  key: string;
  label: string;
  value: string | null;
  currency: string;
  /** Extra text after the amount (e.g. a percentage). */
  note?: string;
}

/** One bar per row, lengths proportional to |value| (exact); negative values are marked with a minus and hatched. */
export function BarList({ items, label }: { items: readonly BarItem[]; label: string }) {
  const bars = new Map(exactBars(items.map((i) => ({ key: i.key, value: i.value }))).map((b) => [b.key, b]));
  return (
    <ul className="pf-bars" aria-label={label}>
      {items.map((i) => {
        const b = bars.get(i.key);
        const pct = b ? b.bps / 100 : 0;
        return (
          <li key={i.key}>
            <span className="pf-bar-label">{i.label}</span>
            <svg className="pf-bar-track" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true" focusable="false">
              <rect x="0" y="0" width="100" height="10" className="pf-track-bg" />
              {b ? <rect x="0" y="1" width={pct} height="8" rx="1" className={b.negative ? 'pf-fill-neg' : 'pf-fill'} /> : null}
            </svg>
            <span className="pf-bar-value num">
              {i.value === null ? 'unavailable' : formatMoney(i.value, i.currency)}
              {i.note ? <span className="pf-hint"> {i.note}</span> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

// ------------------------------------------------------------------ line (statistical series)

export function LineChart({ values, labels, title, format, height = 150 }: { values: readonly number[]; labels: readonly string[]; title: string; format: (v: number) => string; height?: number }) {
  const [ref, W] = useChartWidth();
  const padL = 72;
  const padB = 22;
  const plotW = W - padL - 8;
  const plotH = height - padB - 8;
  const g = linePath(values, plotW, plotH, 6);
  if (!g.points.length) return null;
  const last = g.points[g.points.length - 1];
  const summary = `${title}: ${values.length} points from ${labels[0] ?? '?'} to ${labels[labels.length - 1] ?? '?'}, low ${format(g.min)}, high ${format(g.max)}, last ${format(values[values.length - 1] ?? NaN)}.`;
  return (
    <figure className="pf-figure" ref={ref}>
      <svg viewBox={`0 0 ${W} ${height}`} className="pf-chart" role="img" aria-label={summary}>
        <g transform={`translate(${padL} 8)`}>
          <line x1="0" x2={plotW} y1="6" y2="6" className="pf-grid" />
          <line x1="0" x2={plotW} y1={plotH - 6} y2={plotH - 6} className="pf-grid" />
          <path d={g.d} className="pf-line" />
          {g.points.map((p) => (
            <circle key={p.i} cx={p.x} cy={p.y} r="7" className="pf-hit">
              <title>{`${labels[p.i] ?? ''}: ${format(p.v)}`}</title>
            </circle>
          ))}
          {last ? <circle cx={last.x} cy={last.y} r="4" className="pf-dot" /> : null}
        </g>
        <text x={padL - 6} y="16" textAnchor="end" className="pf-axis">
          {format(g.max)}
        </text>
        <text x={padL - 6} y={8 + plotH - 2} textAnchor="end" className="pf-axis">
          {format(g.min)}
        </text>
        <text x={padL} y={height - 6} className="pf-axis">
          {labels[0] ?? ''}
        </text>
        <text x={W - 8} y={height - 6} textAnchor="end" className="pf-axis">
          {labels[labels.length - 1] ?? ''}
        </text>
      </svg>
      <figcaption className="pf-hint">{title}. The table below lists every value.</figcaption>
    </figure>
  );
}

// ------------------------------------------------------------------ signed bars (exact money)

export function SignedBarChart({ items, currency, title, height = 150 }: { items: readonly { key: string; label: string; value: string }[]; currency: string; title: string; height?: number }) {
  const [ref, W] = useChartWidth();
  const bars = exactBars(items.map((i) => ({ key: i.key, value: i.value })));
  if (!bars.length) return null;
  const padL = 8;
  const plotW = W - padL * 2;
  const half = (height - 24) / 2;
  const zeroY = 8 + half;
  const step = plotW / bars.length;
  const bw = Math.max(1, Math.min(18, step - 2));
  const label = new Map(items.map((i) => [i.key, i.label]));
  const gains = bars.filter((b) => !b.negative && b.bps > 0).length;
  const losses = bars.filter((b) => b.negative).length;
  return (
    <figure className="pf-figure" ref={ref}>
      <svg viewBox={`0 0 ${W} ${height}`} className="pf-chart" role="img" aria-label={`${title}: ${bars.length} days, ${gains} gains above the line, ${losses} losses below it.`}>
        <line x1={padL} x2={W - padL} y1={zeroY} y2={zeroY} className="pf-zero" />
        {bars.map((b, i) => {
          const h = (b.bps / 10000) * (half - 2);
          const x = padL + i * step + (step - bw) / 2;
          return (
            <rect key={b.key} x={x} y={b.negative ? zeroY + 1 : zeroY - 1 - h} width={bw} height={Math.max(h, 0.5)} rx="1" className={b.negative ? 'pf-fill-loss' : 'pf-fill-gain'}>
              <title>{`${label.get(b.key) ?? b.key}: ${formatMoney(b.value, currency, { signDisplay: 'always' })}`}</title>
            </rect>
          );
        })}
        <text x={padL} y={height - 4} className="pf-axis">
          {items[0]?.label ?? ''}
        </text>
        <text x={W - padL} y={height - 4} textAnchor="end" className="pf-axis">
          {items[items.length - 1]?.label ?? ''}
        </text>
      </svg>
      <figcaption className="pf-hint">{title}: gains above the line, losses below. The table below lists every day.</figcaption>
    </figure>
  );
}

// ------------------------------------------------------------------ VaR backtest

export function BacktestChart({ days, currency, height = 170 }: { days: readonly BacktestDay[]; currency: string; height?: number }) {
  const [ref, W] = useChartWidth();
  const vals = days.flatMap((d) => [d.forecast ?? 0, d.realized ?? 0]);
  if (!days.length) return null;
  const max = Math.max(0, ...vals);
  const min = Math.min(0, ...vals);
  const span = max - min || 1;
  const padL = 72;
  const plotW = W - padL - 8;
  const top = 14;
  const plotH = height - top - 22;
  const y = (v: number) => top + ((max - v) * plotH) / span;
  const step = plotW / days.length;
  const bw = Math.max(1, Math.min(14, step - 3));
  const xc = (i: number) => padL + i * step + step / 2;
  const line = days
    .map((d, i) => (d.forecast === null ? null : `${xc(i).toFixed(2)} ${y(d.forecast).toFixed(2)}`))
    .filter((p): p is string => p !== null)
    .map((p, i) => `${i ? 'L' : 'M'}${p}`)
    .join(' ');
  const exceptions = days.filter((d) => d.exception).length;
  return (
    <figure className="pf-figure" ref={ref}>
      <svg
        viewBox={`0 0 ${W} ${height}`}
        className="pf-chart"
        role="img"
        aria-label={`VaR backtest: ${days.length} days, realized loss bars against the 1-day VaR forecast line; ${exceptions} exception(s) marked with triangles.`}
      >
        <line x1={padL} x2={W - 8} y1={y(0)} y2={y(0)} className="pf-zero" />
        {days.map((d, i) => {
          if (d.realized === null) return null;
          const yr = y(d.realized);
          const y0 = y(0);
          return (
            <rect key={d.date} x={xc(i) - bw / 2} y={Math.min(yr, y0)} width={bw} height={Math.max(Math.abs(y0 - yr), 0.5)} rx="1" className="pf-fill-neutral">
              <title>{`${d.date}: realized loss ${fmtFloatMoney(d.realized, currency)}, VaR ${fmtFloatMoney(d.forecast, currency)}${d.exception ? ' (exception)' : ''}`}</title>
            </rect>
          );
        })}
        {line ? <path d={line} className="pf-line" /> : null}
        {days.map((d, i) =>
          d.exception && d.realized !== null ? (
            <path key={`x-${d.date}`} d={`M${xc(i)} ${y(d.realized) - 9} l5 -8 l-10 0 z`} className="pf-exception">
              <title>{`${d.date}: exception, loss ${fmtFloatMoney(d.realized, currency)} above VaR ${fmtFloatMoney(d.forecast, currency)}`}</title>
            </path>
          ) : null,
        )}
        <text x={padL - 6} y={top + 4} textAnchor="end" className="pf-axis">
          {fmtFloatMoney(max, currency)}
        </text>
        <text x={padL - 6} y={y(0) + 4} textAnchor="end" className="pf-axis">
          0
        </text>
        <text x={padL} y={height - 4} className="pf-axis">
          {days[0]?.date ?? ''}
        </text>
        <text x={W - 8} y={height - 4} textAnchor="end" className="pf-axis">
          {days[days.length - 1]?.date ?? ''}
        </text>
      </svg>
      <figcaption className="pf-legend">
        <span>
          <svg width="14" height="10" aria-hidden="true" focusable="false">
            <rect x="2" y="1" width="10" height="8" rx="1" className="pf-fill-neutral" />
          </svg>
          realized loss (gain below zero)
        </span>
        <span>
          <svg width="18" height="10" aria-hidden="true" focusable="false">
            <path d="M1 5 L17 5" className="pf-line" />
          </svg>
          1-day VaR forecast
        </span>
        <span>
          <svg width="14" height="12" aria-hidden="true" focusable="false">
            <path d="M7 11 l5 -9 l-10 0 z" className="pf-exception" />
          </svg>
          exception (loss above VaR)
        </span>
      </figcaption>
    </figure>
  );
}

/** A small inline bar for a weight (statistical float, 0..1 of NAV). */
export function WeightBar({ w }: { w: number | null }) {
  if (w === null || !Number.isFinite(w)) return null;
  const pct = Math.max(0, Math.min(100, w * 100));
  return (
    <svg className="pf-wbar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <rect x="0" y="0" width="100" height="8" className="pf-track-bg" />
      <rect x="0" y="1" width={pct} height="6" rx="1" className="pf-fill" />
    </svg>
  );
}
