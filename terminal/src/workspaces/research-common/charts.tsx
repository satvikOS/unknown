/**
 * Inline SVG charts (no dependency). Each chart is an <svg role="img"> with a title and a text summary, and
 * point markers carry the API's exact value text as a tooltip. Gaps in a series are drawn as gaps.
 * Colours come from the theme tokens, so both themes work.
 */
import { useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { extent, labelIndices, linePath, linear, niceTicks, segments } from './chartMath';

const TONE: Record<string, string> = {
  accent: 'var(--accent)',
  info: 'var(--info)',
  warn: 'var(--warn)',
  muted: 'var(--text-muted)',
  ok: 'var(--ok)',
  bad: 'var(--bad)',
};

/** Width of an element, tracked with ResizeObserver (falls back to `initial` before layout or without it). */
function useWidth(initial: number): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(initial);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = Math.floor(el.getBoundingClientRect().width);
      if (w > 0) setWidth(w);
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export interface ChartPoint {
  x: number;
  /** null = no value (a gap; never interpolated). */
  y: number | null;
  /** Exact text for the marker tooltip, e.g. "10Y 4.24 percent (obs 2953)". */
  label?: string;
}

export interface ChartSeries {
  id: string;
  label: string;
  points: readonly ChartPoint[];
  tone?: keyof typeof TONE;
  dashed?: boolean;
  markers?: boolean;
}

export interface LineChartProps {
  /** Accessible name of the image. */
  title: string;
  /** One or two sentences read by screen readers and shown under the chart. */
  summary: string;
  series: readonly ChartSeries[];
  /** Candidate x-axis labels; thinned to fit the width. */
  xTicks: readonly { x: number; label: string }[];
  xDomain?: readonly [number, number];
  yFormat: (v: number) => string;
  /** Axis title, e.g. "percent" or "cumulative return, % (float statistic)". */
  yUnit: string;
  zeroLine?: boolean;
  height?: number;
}

export function LineChart({ title, summary, series, xTicks, xDomain, yFormat, yUnit, zeroLine, height = 240 }: LineChartProps) {
  const [ref, width] = useWidth(640);
  const m = { left: 58, right: 14, top: 12, bottom: 30 };
  const plotW = Math.max(40, width - m.left - m.right);
  const plotH = Math.max(40, height - m.top - m.bottom);
  const ys = series.flatMap((s) => s.points.map((p) => p.y));
  const yExt = extent(zeroLine ? [...ys, 0] : ys);
  const xs = series.flatMap((s) => s.points.map((p) => p.x));
  const xExt = xDomain ? { min: xDomain[0], max: xDomain[1] } : extent(xs);
  if (!yExt || !xExt) {
    return (
      <p className="muted" role="note">
        {title}: no values to plot.
      </p>
    );
  }
  const ticks = niceTicks(yExt.min, yExt.max, 5);
  const y0 = ticks[0] ?? yExt.min;
  const y1 = ticks[ticks.length - 1] ?? yExt.max;
  const sx = linear([xExt.min, xExt.max], [m.left, m.left + plotW]);
  const sy = linear([y0, y1], [m.top + plotH, m.top]);
  const maxLabels = Math.max(2, Math.floor(plotW / 44));
  const shownX = labelIndices(xTicks.length, maxLabels).map((i) => xTicks[i]!);

  return (
    <figure className="rx-chart">
      <div ref={ref} className="rx-chart-box">
        <svg width={width} height={height} role="img" aria-label={title} focusable="false">
          <title>{title}</title>
          <desc>{summary}</desc>
          {ticks.map((t) => (
            <g key={`y${t}`}>
              <line x1={m.left} x2={m.left + plotW} y1={sy(t)} y2={sy(t)} className={t === 0 && zeroLine ? 'rx-zero' : 'rx-grid'} />
              <text x={m.left - 6} y={sy(t)} dy="0.32em" textAnchor="end" className="rx-axis">
                {yFormat(t)}
              </text>
            </g>
          ))}
          {shownX.map((t) => (
            <g key={`x${t.x}`}>
              <line x1={sx(t.x)} x2={sx(t.x)} y1={m.top + plotH} y2={m.top + plotH + 4} className="rx-grid" />
              <text x={sx(t.x)} y={m.top + plotH + 18} textAnchor="middle" className="rx-axis">
                {t.label}
              </text>
            </g>
          ))}
          <text x={12} y={m.top + plotH / 2} transform={`rotate(-90 12 ${m.top + plotH / 2})`} textAnchor="middle" className="rx-axis">
            {yUnit}
          </text>
          {series.map((s) => {
            const color = TONE[s.tone ?? 'accent'];
            return (
              <g key={s.id} style={{ color }}>
                {segments(s.points).map((seg, i) =>
                  seg.length > 1 ? (
                    <path
                      key={i}
                      d={linePath(seg.map((p) => ({ x: sx(p.x), y: sy(p.y) })))}
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={1.8}
                      strokeDasharray={s.dashed ? '5 4' : undefined}
                    />
                  ) : null,
                )}
                {s.markers || s.points.length < 2
                  ? s.points.map((p, i) =>
                      p.y === null ? null : (
                        <circle key={i} cx={sx(p.x)} cy={sy(p.y)} r={3} fill="currentColor">
                          {p.label ? <title>{p.label}</title> : null}
                        </circle>
                      ),
                    )
                  : null}
              </g>
            );
          })}
        </svg>
      </div>
      <figcaption className="rx-chart-caption">
        {series.length > 1 ? (
          <span className="rx-legend">
            {series.map((s) => (
              <span key={s.id} className="rx-legend-item">
                <svg width="22" height="10" aria-hidden="true" focusable="false" style={{ color: TONE[s.tone ?? 'accent'] }}>
                  <line x1="1" x2="21" y1="5" y2="5" stroke="currentColor" strokeWidth="2" strokeDasharray={s.dashed ? '5 4' : undefined} />
                </svg>
                {s.label}
              </span>
            ))}
          </span>
        ) : null}
        <span>{summary}</span>
      </figcaption>
    </figure>
  );
}

export interface RangeRow {
  label: string;
  min: number;
  max: number;
  /** Exact text of the bounds, as the API sent them. */
  minText: string;
  maxText: string;
}

/** A reference value drawn across every bar (the owner's price input on a football field). */
export interface RangeMarker {
  value: number;
  /** Exact text of the value, as the API sent it. */
  text: string;
  label: string;
}

/** Horizontal min-max bars on one shared axis (a valuation "football field"), with an optional reference marker. */
export function RangeBars({
  title,
  summary,
  rows,
  format,
  unit,
  marker,
}: {
  title: string;
  summary: string;
  rows: readonly RangeRow[];
  format: (v: number) => string;
  unit: string;
  marker?: RangeMarker | null;
}) {
  const [ref, width] = useWidth(640);
  const m = { left: 150, right: 18, top: marker ? 22 : 8, bottom: 30 };
  const rowH = 26;
  const height = m.top + m.bottom + rowH * rows.length;
  const ext = extent([...rows.flatMap((r) => [r.min, r.max]), marker?.value]);
  if (!ext || !rows.length) return null;
  const ticks = niceTicks(ext.min, ext.max, 5);
  const plotW = Math.max(40, width - m.left - m.right);
  const sx = linear([ticks[0] ?? ext.min, ticks[ticks.length - 1] ?? ext.max], [m.left, m.left + plotW]);
  const maxLabels = Math.max(2, Math.floor(plotW / 60));
  const shown = labelIndices(ticks.length, maxLabels).map((i) => ticks[i]!);
  return (
    <figure className="rx-chart">
      <div ref={ref} className="rx-chart-box">
        <svg width={width} height={height} role="img" aria-label={title} focusable="false">
          <title>{title}</title>
          <desc>{summary}</desc>
          {shown.map((t) => (
            <g key={t}>
              <line x1={sx(t)} x2={sx(t)} y1={m.top} y2={height - m.bottom} className="rx-grid" />
              <text x={sx(t)} y={height - m.bottom + 16} textAnchor="middle" className="rx-axis">
                {format(t)}
              </text>
            </g>
          ))}
          {rows.map((r, i) => {
            const y = m.top + i * rowH + rowH / 2;
            const x0 = sx(r.min);
            const x1 = Math.max(sx(r.max), x0 + 2);
            return (
              <g key={r.label}>
                <text x={m.left - 8} y={y} dy="0.32em" textAnchor="end" className="rx-axis">
                  {r.label}
                </text>
                <rect x={x0} y={y - 7} width={x1 - x0} height={14} rx={2} className="rx-bar">
                  <title>{`${r.label}: ${r.minText} to ${r.maxText} ${unit}`}</title>
                </rect>
              </g>
            );
          })}
          {marker && Number.isFinite(marker.value) ? (
            <g className="rx-marker">
              <line x1={sx(marker.value)} x2={sx(marker.value)} y1={m.top - 4} y2={height - m.bottom} strokeDasharray="4 3">
                <title>{`${marker.label}: ${marker.text} ${unit}`}</title>
              </line>
              <text
                x={sx(marker.value)}
                y={m.top - 8}
                textAnchor={sx(marker.value) > m.left + plotW * 0.8 ? 'end' : sx(marker.value) < m.left + plotW * 0.2 ? 'start' : 'middle'}
                className="rx-axis"
              >
                {`${marker.label} ${marker.text}`}
              </text>
            </g>
          ) : null}
        </svg>
      </div>
      <figcaption className="rx-chart-caption">
        <span>{summary}</span>
      </figcaption>
    </figure>
  );
}
