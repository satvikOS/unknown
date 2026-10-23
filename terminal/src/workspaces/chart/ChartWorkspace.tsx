/**
 * Chart (CAP-MKT-07): bars from GET /api/charts/bars for the linked security, drawn with TradingView Lightweight
 * Charts, with studies (studies.ts), a % comparison, annotations kept in this browser, and the last bar rolled
 * forward from the market-data stream (feed.ts). The header always names the source and data type; SYNTHETIC
 * data is never shown without its warning.
 *
 * Route: `#/chart` (follows the linked security) or `#/chart/<KEY>` (e.g. `#/chart/SAP.XETR`).
 */
import {
  AreaSeries,
  BarSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { formatDecimal } from '../../format/decimal';
import { charKeysEnabled } from '../../ui/charKeys';
import { isTypingTarget } from '../../ui/keys';
import { useLinkedSecurity } from '../../ui/link';
import { Badge, type Tone } from '../../ui/primitives';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { usePluginQuery } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import { loadAnnotations, newAnnotation, saveAnnotations, type Annotation } from './annotations';
import { chartFeed, type FeedState } from './feed';
import {
  INTERVALS,
  applyTick,
  commonStart,
  isIntraday,
  parseBarsResponse,
  percentFrom,
  sessionKeyFor,
  sourceLabel,
  toOhlcv,
  type ChartBar,
  type DataType,
  type IntervalCode,
} from './model';
import { STUDIES, STUDY_VERSION, atr, bollinger, ema, macd, rsi, sma, vwap, type Series, type StudyId } from './studies';
import './chart.css';

export const DEFAULT_KEY = 'IBM.XNYS';
export const ATTRIBUTION_NOTICE = 'TradingView Lightweight Charts™ Copyright (c) 2026 TradingView, Inc.';
export const ATTRIBUTION_URL = 'https://www.tradingview.com/';
const CHART_TYPES = ['candles', 'ohlc', 'line', 'area'] as const;
type ChartType = (typeof CHART_TYPES)[number];
const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._\-/]{0,63}$/;


const TYPE_TONE: Record<DataType, Tone> = { REALTIME: 'ok', DELAYED: 'info', FROZEN: 'warn', SYNTHETIC: 'warn' };

function cssVar(name: string, fallback: string): string {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch {
    return fallback;
  }
}

function palette() {
  return {
    text: cssVar('--text', '#222'),
    muted: cssVar('--text-muted', '#777'),
    border: cssVar('--border', '#ddd'),
    bg: cssVar('--surface', '#fff'),
    up: cssVar('--ok', '#2e7d32'),
    down: cssVar('--bad', '#c62828'),
    accent: cssVar('--accent', '#1565c0'),
    info: cssVar('--info', '#1565c0'),
    warn: cssVar('--warn', '#b26a00'),
    neutral: cssVar('--neutral', '#777'),
  };
}

const ts = (s: number) => s as UTCTimestamp;
const line = (times: readonly number[], values: Series) =>
  values.flatMap((v, i) => (v === null ? [] : [{ time: ts(times[i]!), value: v }]));

interface Legend {
  bar: ChartBar;
  change: string | null;
}

export function ChartWorkspace({ route, navigate }: WorkspaceProps) {
  const linked = useLinkedSecurity();
  const routeKey = route.rest[0] && KEY_RE.test(route.rest[0]) ? route.rest[0] : null;
  const key = routeKey ?? linked ?? DEFAULT_KEY;
  const [interval, setInterval_] = useState<IntervalCode>('1d');
  const [chartType, setChartType] = useState<ChartType>('candles');
  const [showVolume, setShowVolume] = useState(true);
  const [studies, setStudies] = useState<ReadonlySet<StudyId>>(new Set<StudyId>(['sma']));
  const [compareKey, setCompareKey] = useState<string | null>(null);
  const [compareDraft, setCompareDraft] = useState('');
  const [keyDraft, setKeyDraft] = useState('');
  const [annotations, setAnnotations] = useState<Annotation[]>(() => loadAnnotations(key));
  const [persisted, setPersisted] = useState(true);
  const [noteDraft, setNoteDraft] = useState('');
  const [legend, setLegend] = useState<Legend | null>(null);
  const [feedState, setFeedState] = useState<FeedState>('connecting');
  const [liveSource, setLiveSource] = useState<string | null>(null);
  const [bars, setBars] = useState<ChartBar[]>([]);

  const q = usePluginQuery<unknown>('/api/charts/bars', { key, interval }, { seqDriven: false });
  const cq = usePluginQuery<unknown>(compareKey ? '/api/charts/bars' : null, { key: compareKey ?? '', interval }, { seqDriven: false });
  const data = useMemo(() => parseBarsResponse(q.data), [q.data]);
  const cmp = useMemo(() => parseBarsResponse(cq.data), [cq.data]);

  useEffect(() => setBars(data?.bars ?? []), [data]);
  useEffect(() => setAnnotations(loadAnnotations(key)), [key]);

  // ------------------------------------------------------------------ chart objects
  const box = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const mainRef = useRef<ISeriesApi<SeriesType> | null>(null);
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const extraRef = useRef<ISeriesApi<SeriesType>[]>([]);
  const linesRef = useRef<IPriceLine[]>([]);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const crossPrice = useRef<number | null>(null);
  const crossTime = useRef<number | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const p = palette();
    const chart = createChart(box.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: p.bg },
        textColor: p.text,
        attributionLogo: true,
        panes: { separatorColor: p.border },
      },
      grid: { vertLines: { color: p.border }, horzLines: { color: p.border } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: p.border },
      leftPriceScale: { borderColor: p.border, visible: false },
      timeScale: { borderColor: p.border, timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;
    // The library lays the chart out in a table and draws an attribution logo: name both for assistive technology.
    const host = box.current;
    const label = () => {
      for (const t of host.querySelectorAll('table')) {
        t.setAttribute('role', 'presentation');
        t.setAttribute('aria-label', 'Chart canvas');
      }
      for (const a of host.querySelectorAll('a')) {
        a.setAttribute('aria-label', 'Charting library by TradingView');
        for (const svg of a.querySelectorAll('svg')) svg.setAttribute('aria-hidden', 'true');
      }
    };
    label();
    const frame = requestAnimationFrame(label);
    return () => {
      cancelAnimationFrame(frame);
      chart.remove();
      chartRef.current = null;
      mainRef.current = null;
      volRef.current = null;
      extraRef.current = [];
      linesRef.current = [];
      markersRef.current = null;
    };
  }, []);

  const ohlcv = useMemo(() => toOhlcv(bars), [bars]);
  const final = useMemo(() => ohlcv.filter((_, i) => bars[i]?.complete), [ohlcv, bars]);

  // Draw series whenever the data, type, studies or comparison change.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const p = palette();
    for (const s of [mainRef.current, volRef.current, ...extraRef.current]) if (s) chart.removeSeries(s);
    extraRef.current = [];
    linesRef.current = [];
    markersRef.current = null;
    const synthetic = data?.dataType === 'SYNTHETIC';
    const gapTimes = new Set((data?.gaps ?? []).map((g) => Number(BigInt(g.startNs) / 1_000_000_000n)));
    const whitespace = [...gapTimes].filter((t) => !bars.some((b) => b.time === t)).map((t) => ({ time: ts(t) }));
    const byTime = <T extends { time: UTCTimestamp }>(rows: (T | { time: UTCTimestamp })[]) =>
      rows.sort((a, b) => (a.time as number) - (b.time as number));
    const liveColor = p.accent;
    let main: ISeriesApi<SeriesType>;
    if (chartType === 'candles' || chartType === 'ohlc') {
      const rows = bars.map((b) => ({
        time: ts(b.time),
        open: Number(b.open),
        high: Number(b.high),
        low: Number(b.low),
        close: Number(b.close),
        ...(b.complete ? {} : { color: liveColor, wickColor: liveColor, borderColor: liveColor }),
      }));
      main =
        chartType === 'candles'
          ? chart.addSeries(CandlestickSeries, { upColor: p.up, downColor: p.down, borderVisible: false, wickUpColor: p.up, wickDownColor: p.down })
          : chart.addSeries(BarSeries, { upColor: p.up, downColor: p.down });
      main.setData(byTime([...rows, ...whitespace]));
    } else {
      const rows = bars.map((b) => ({ time: ts(b.time), value: Number(b.close) }));
      main =
        chartType === 'line'
          ? chart.addSeries(LineSeries, { color: synthetic ? p.warn : p.accent, lineWidth: 2 })
          : chart.addSeries(AreaSeries, { lineColor: p.accent, topColor: `${p.accent}55`, bottomColor: `${p.accent}05` });
      main.setData(byTime([...rows, ...whitespace]));
    }
    mainRef.current = main;
    if (showVolume) {
      const vol = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1);
      vol.setData(
        bars.flatMap((b) =>
          b.volume === null ? [] : [{ time: ts(b.time), value: Number(b.volume), color: Number(b.close) >= Number(b.open) ? `${p.up}88` : `${p.down}88` }],
        ),
      );
      volRef.current = vol;
    }
    // Studies over final (complete) bars only.
    const times = final.map((b) => b.time);
    const closes = final.map((b) => b.close);
    const lowerPane = showVolume ? 2 : 1;
    const add = (values: Series, color: string, pane = 0, style: LineStyle = LineStyle.Solid, title = '') => {
      const s = chart.addSeries(LineSeries, { color, lineWidth: 1, lineStyle: style, priceLineVisible: false, lastValueVisible: false, title }, pane);
      s.setData(line(times, values));
      extraRef.current.push(s);
    };
    if (studies.has('sma')) add(sma(closes, 20), p.info, 0, LineStyle.Solid, 'SMA 20');
    if (studies.has('ema')) add(ema(closes, 50), p.warn, 0, LineStyle.Solid, 'EMA 50');
    if (studies.has('bollinger')) {
      const b = bollinger(closes, 20, 2);
      add(b.upper, p.muted, 0, LineStyle.Dashed, 'BB up');
      add(b.middle, p.muted, 0, LineStyle.Dotted, 'BB mid');
      add(b.lower, p.muted, 0, LineStyle.Dashed, 'BB low');
    }
    if (studies.has('vwap')) add(vwap(final, isIntraday(interval) ? sessionKeyFor(data?.timezone ?? 'UTC') : () => ''), p.accent, 0, LineStyle.Dotted, 'VWAP');
    let pane = lowerPane;
    if (studies.has('rsi')) add(rsi(closes, 14), p.info, pane++, LineStyle.Solid, 'RSI 14');
    if (studies.has('macd')) {
      const m = macd(closes);
      add(m.macd, p.info, pane, LineStyle.Solid, 'MACD');
      add(m.signal, p.warn, pane, LineStyle.Solid, 'signal');
      const h = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane++);
      h.setData(m.histogram.flatMap((v, i) => (v === null ? [] : [{ time: ts(times[i]!), value: v, color: v >= 0 ? `${p.up}99` : `${p.down}99` }])));
      extraRef.current.push(h);
    }
    if (studies.has('atr')) add(atr(final, 14), p.neutral, pane++, LineStyle.Solid, 'ATR 14');
    // Comparison: both series rebased to % from their common start, on the left scale.
    const cmpBars = cmp ? toOhlcv(cmp.bars) : [];
    const t0 = commonStart(ohlcv, cmpBars);
    if (compareKey && cmp && t0 !== null && cmp.dataType !== null && (cmp.dataType === 'SYNTHETIC') === synthetic) {
      for (const [rows, color, title] of [
        [percentFrom(ohlcv, t0), p.accent, `${key} %`],
        [percentFrom(cmpBars, t0), p.warn, `${compareKey} %`],
      ] as const) {
        const s = chart.addSeries(LineSeries, { color, lineWidth: 2, priceScaleId: 'left', priceLineVisible: false, title, priceFormat: { type: 'percent' } });
        s.setData(rows.map((r) => ({ time: ts(r.time), value: r.value })));
        extraRef.current.push(s);
      }
      chart.priceScale('left').applyOptions({ visible: true });
    } else {
      chart.priceScale('left').applyOptions({ visible: false });
    }
    const panes = chart.panes();
    panes.forEach((pn, i) => pn.setStretchFactor(i === 0 ? 4 : 1));
    for (const a of annotations) {
      if (a.kind === 'hline') linesRef.current.push(main.createPriceLine({ price: Number(a.price), color: p.warn, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: a.text }));
    }
    const notes = annotations.filter((a) => a.kind === 'note' && a.time !== null).sort((a, b) => (a.time as number) - (b.time as number));
    markersRef.current = createSeriesMarkers(
      main,
      notes.map((a) => ({ time: ts(a.time as number), position: 'aboveBar' as const, color: p.warn, shape: 'arrowDown' as const, text: a.text })),
    );
  }, [bars, final, ohlcv, data, cmp, compareKey, chartType, showVolume, studies, annotations, interval, key]);

  // Fit when a new series arrives (not on every live tick).
  useEffect(() => {
    chartRef.current?.timeScale().fitContent();
  }, [data, chartType]);

  // Crosshair legend with exact OHLCV.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const handler = (param: MouseEventParams<Time>) => {
      crossTime.current = typeof param.time === 'number' ? param.time : null;
      crossPrice.current = param.point && mainRef.current ? mainRef.current.coordinateToPrice(param.point.y) : null;
      const i = crossTime.current === null ? bars.length - 1 : bars.findIndex((b) => b.time === crossTime.current);
      const cur = bars[i];
      if (!cur) return setLegend(null);
      const prev = i > 0 ? bars[i - 1] : null;
      const change = prev && Number(prev.close) !== 0 ? (((Number(cur.close) - Number(prev.close)) / Number(prev.close)) * 100).toFixed(2) : null;
      setLegend({ bar: cur, change });
    };
    chart.subscribeCrosshairMove(handler);
    return () => chart.unsubscribeCrosshairMove(handler);
  }, [bars]);

  // ------------------------------------------------------------------ live updates
  const refetchRef = useRef(q.refetch);
  refetchRef.current = q.refetch;
  const lastRefetch = useRef(0);
  useEffect(() => {
    if (!data || !data.available) return;
    setFeedState('connecting');
    setLiveSource(null);
    return chartFeed().subscribe([key], {
      onTick: (k, tick, source) => {
        if (k !== key) return;
        setBars((cur) => {
          const r = applyTick(cur, data.live, data.dataType, tick);
          if (r.needsRefetch && Date.now() - lastRefetch.current > 5000) {
            lastRefetch.current = Date.now();
            queueMicrotask(() => refetchRef.current());
          }
          if (r.changed) setLiveSource(`${source ?? 'stream'} · ${tick.dataType}`);
          return r.changed ? r.bars : cur;
        });
      },
      onState: setFeedState,
    });
  }, [key, data]);

  // ------------------------------------------------------------------ annotations
  const updateAnnotations = useCallback(
    (next: Annotation[]) => {
      setAnnotations(next);
      setPersisted(saveAnnotations(key, next));
    },
    [key],
  );
  const lastBar = bars[bars.length - 1];
  const addLine = useCallback(() => {
    const price = crossPrice.current !== null ? crossPrice.current.toFixed(4) : lastBar?.close;
    if (!price) return;
    updateAnnotations([...annotations, newAnnotation('hline', price, null, noteDraft.trim() || `line ${formatDecimal(price)}`)]);
    setNoteDraft('');
  }, [annotations, lastBar, noteDraft, updateAnnotations]);
  const addNote = useCallback(() => {
    const bar = bars.find((b) => b.time === crossTime.current) ?? lastBar;
    if (!bar) return;
    updateAnnotations([...annotations, newAnnotation('note', bar.close, bar.time, noteDraft.trim() || 'note')]);
    setNoteDraft('');
  }, [annotations, bars, lastBar, noteDraft, updateAnnotations]);

  // ------------------------------------------------------------------ keyboard
  useEffect(() => {
    let prev = { key: '', at: 0 };
    const onKey = (e: KeyboardEvent) => {
      const before = prev;
      prev = { key: e.key.toLowerCase(), at: Date.now() };
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || e.repeat) return;
      if (isTypingTarget(e.target) || document.querySelector('.overlay') || !charKeysEnabled()) return;
      if (before.key === 'g' && prev.at - before.at < 1500) return; // second key of a "g x" sequence
      const n = Number(e.key);
      if (n >= 1 && n <= INTERVALS.length) setInterval_(INTERVALS[n - 1]!);
      else if (e.key === 'c') setChartType((t) => CHART_TYPES[(CHART_TYPES.indexOf(t) + 1) % CHART_TYPES.length]!);
      else if (e.key === 'v') setShowVolume((v) => !v);
      else if (e.key === 'h') addLine();
      else if (e.key === 'n') addNote();
      else if (e.key === 'f') chartRef.current?.timeScale().fitContent();
      else if (e.key === 'Escape' && compareKey) setCompareKey(null);
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [addLine, addNote, compareKey]);

  const toggleStudy = (id: StudyId) =>
    setStudies((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const shown = legend?.bar ?? lastBar ?? null;
  const fmt = (v: string | null) => (v === null ? '—' : formatDecimal(v));
  const synthetic = data?.dataType === 'SYNTHETIC';

  return (
    <WorkspaceFrame
      title="Chart"
      description="Price chart for the linked security: bars from the configured source, studies, comparison and annotations."
      queries={[q]}
    >
      <QueryError what="Chart bars" error={q.error} />
      <div className="chart-ws">
        <div className="chart-ws-bar" role="toolbar" aria-label="Chart controls">
          <form
            className="chart-ws-key"
            onSubmit={(e) => {
              e.preventDefault();
              const k = keyDraft.trim().toUpperCase();
              if (KEY_RE.test(k)) navigate('chart', k);
              setKeyDraft('');
            }}
          >
            <strong title="Security key (SYMBOL.MIC)">{key}</strong>
            {!routeKey && !linked ? <span className="chart-ws-muted">(no linked security: example)</span> : null}
            <input aria-label="Open security" placeholder="SYMBOL.MIC" value={keyDraft} onChange={(e) => setKeyDraft(e.target.value)} />
          </form>
          <div className="chart-ws-seg" role="group" aria-label="Interval">
            {INTERVALS.map((iv, i) => (
              <button key={iv} type="button" className={`btn small${iv === interval ? ' primary' : ' ghost'}`} aria-pressed={iv === interval} title={`Key ${i + 1}`} onClick={() => setInterval_(iv)}>
                {iv}
              </button>
            ))}
          </div>
          <div className="chart-ws-seg" role="group" aria-label="Chart type">
            {CHART_TYPES.map((t) => (
              <button key={t} type="button" className={`btn small${t === chartType ? ' primary' : ' ghost'}`} aria-pressed={t === chartType} onClick={() => setChartType(t)}>
                {t}
              </button>
            ))}
            <button type="button" className={`btn small${showVolume ? ' primary' : ' ghost'}`} aria-pressed={showVolume} onClick={() => setShowVolume((v) => !v)}>
              volume
            </button>
          </div>
        </div>
        <div className="chart-ws-bar" aria-label="Data status">
          {data ? (
            <Badge tone={data.dataType ? TYPE_TONE[data.dataType] : 'neutral'} title={data.sources.map((s) => `${s.source}: ${s.reason}${s.error ? ` (error: ${s.error})` : ''}`).join('\n')}>
              {sourceLabel(data)}
            </Badge>
          ) : null}
          {data?.adjustment ? <Badge title={data.adjustmentNote ?? undefined}>{data.adjustment}</Badge> : null}
          {data ? <Badge title="Session calendar and time zone">{`${data.calendarId ?? 'no calendar'} · ${data.timezone}`}</Badge> : null}
          {data?.currency ? <Badge>{data.currency}</Badge> : null}
          {data && data.gaps.length ? <Badge tone="warn" title="Missing buckets inside the range are drawn as gaps">{`${data.gaps.length} gap${data.gaps.length > 1 ? 's' : ''}`}</Badge> : null}
          {data?.available ? (
            <Badge tone={feedState === 'open' ? 'ok' : 'neutral'} title="Last bar updates from GET /api/marketdata/stream">
              {feedState === 'open' ? `live${liveSource ? `: ${liveSource}` : ''}` : feedState === 'connecting' ? 'live: connecting' : 'live: stream unavailable'}
            </Badge>
          ) : null}
        </div>
        {synthetic ? (
          <p className="chart-ws-synthetic" role="status">
            {data?.label ?? 'SYNTHETIC: generated demo data, not market data'}. No real bar source is configured for {key}.
          </p>
        ) : null}
        <div className="chart-ws-legend" aria-live="off">
          {shown ? (
            <>
              <span>O {fmt(shown.open)}</span>
              <span>H {fmt(shown.high)}</span>
              <span>L {fmt(shown.low)}</span>
              <span>C {fmt(shown.close)}</span>
              <span>V {fmt(shown.volume)}</span>
              {legend?.change ? <span>{`${legend.change}%`}</span> : null}
              {!shown.complete ? <span className="chart-ws-live">in progress</span> : null}
              {shown.partial ? <span className="chart-ws-muted">partial</span> : null}
            </>
          ) : (
            <span className="chart-ws-muted">{data?.missingReason ?? (q.status === 'loading' ? 'loading bars…' : 'no bars')}</span>
          )}
        </div>
        <div className="chart-ws-canvas" ref={box} />
        <div className="chart-ws-bar" role="group" aria-label="Studies">
          {STUDIES.map((s) => (
            <button key={s.id} type="button" className={`btn small${studies.has(s.id) ? ' primary' : ' ghost'}`} aria-pressed={studies.has(s.id)} title={`${s.params} (${STUDY_VERSION}; final bars only)`} onClick={() => toggleStudy(s.id)}>
              {s.label}
            </button>
          ))}
          <form
            className="chart-ws-key"
            onSubmit={(e) => {
              e.preventDefault();
              const k = compareDraft.trim().toUpperCase();
              setCompareKey(KEY_RE.test(k) ? k : null);
            }}
          >
            <input aria-label="Compare with" placeholder="compare SYMBOL.MIC" value={compareDraft} onChange={(e) => setCompareDraft(e.target.value)} />
            {compareKey ? (
              <button type="button" className="btn small ghost" onClick={() => setCompareKey(null)}>
                clear {compareKey}
              </button>
            ) : null}
          </form>
          {compareKey && cmp && cmp.dataType && (cmp.dataType === 'SYNTHETIC') !== synthetic ? (
            <span className="chart-ws-muted">not compared: one series is SYNTHETIC</span>
          ) : null}
        </div>
        <div className="chart-ws-bar" role="group" aria-label="Annotations">
          <input aria-label="Annotation text" placeholder="label" value={noteDraft} onChange={(e) => setNoteDraft(e.target.value)} />
          <button type="button" className="btn small" onClick={addLine} title="Key h: at the crosshair price (or the last close)">
            + line
          </button>
          <button type="button" className="btn small" onClick={addNote} title="Key n: on the crosshair bar (or the last bar)">
            + note
          </button>
          {annotations.map((a) => (
            <span key={a.id} className="chart-ws-chip">
              {a.kind === 'hline' ? `— ${formatDecimal(a.price)}` : '▼'} {a.text}
              <button type="button" className="btn small ghost" aria-label={`Remove ${a.text}`} onClick={() => updateAnnotations(annotations.filter((x) => x.id !== a.id))}>
                ×
              </button>
            </span>
          ))}
          {!persisted ? <span className="chart-ws-muted">this browser did not save annotations</span> : null}
        </div>
        {data?.notes.length ? (
          <ul className="chart-ws-notes">
            {data.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        ) : null}
        <p className="chart-ws-attribution">
          Charts: {ATTRIBUTION_NOTICE}{' '}
          <a href={ATTRIBUTION_URL} target="_blank" rel="noopener noreferrer">
            {ATTRIBUTION_URL}
          </a>
        </p>
      </div>
    </WorkspaceFrame>
  );
}
