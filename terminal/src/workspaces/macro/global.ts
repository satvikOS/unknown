/**
 * Global macro (CAP-MAC-05/06/09): types and pure helpers for `GET /api/macro/global` (cross-country comparison) and
 * `GET /api/macro/global/series` (backend `api/routes/macro_global.py`, `data/sources/macro_global.py`).
 *
 * One column is one source and one definition (World Bank annual, OECD monthly, BIS policy rate, FRED with an owner
 * key); the API never merges two sources into one number and neither does this view.
 */
import type { ChartSeries } from '../research-common/charts';

export interface GlobalColumn {
  column_id: string;
  indicator: string;
  provider: string;
  dataset: string;
  series_code: string;
  label: string;
  unit: string;
  frequency: string;
  definition: string;
  attribution: string;
}

export type CellStatus = 'OK' | 'NOT_COVERED' | 'NOT_INGESTED' | 'NOT_AVAILABLE_AT_AS_OF';

export interface GlobalCell {
  column_id: string;
  dataset: string;
  provider: string;
  status: CellStatus;
  value: string | null;
  reason?: string;
  command?: string | null;
  area?: string;
  entity_id?: string;
  period_start?: string;
  period_end?: string;
  period_kind?: string;
  unit?: string;
  obs_id?: number;
  available_time?: string;
  flags?: string[];
  note?: string;
}

export interface GlobalCountry {
  code: string;
  iso3: string;
  name: string;
  currency: string;
  euro_area: boolean;
  eu_member: boolean;
  venues: string[];
}

export interface DatasetStatus {
  dataset: string;
  observations: number;
  first_period_end: string | null;
  last_period_end: string | null;
  newest_available_time: string | null;
  status: string;
  command: string | null;
}

export interface BlockedSource {
  provider_id: string;
  name: string;
  dataset: string;
  reason: string;
  verified_on: string;
}

export interface GlobalComparison {
  as_of: string;
  columns: GlobalColumn[];
  rows: { country: GlobalCountry; cells: Record<string, GlobalCell> }[];
  datasets: Record<string, DatasetStatus>;
  fred: { status: string; key_file: string; reason?: string; attribution?: string };
  blocked_sources: BlockedSource[];
  note: string;
}

export interface SeriesPoint {
  period_start: string | null;
  period_end: string;
  period_kind: string;
  value: string | null;
  missing_reason: string | null;
  unit: string;
  obs_id: number;
  available_time: string;
  flags: string[];
}

export interface GlobalSeries {
  as_of: string;
  column: GlobalColumn;
  series: { country: string; area?: string; status: string; reason?: string; command?: string | null; title?: string; points: SeriesPoint[] }[];
}

/** Period label: 2025, 2026-09, 2026-Q3 or a day. */
export function periodLabel(c: { period_kind?: string; period_start?: string | null; period_end?: string }): string {
  const end = c.period_end ?? '';
  switch (c.period_kind) {
    case 'YEAR':
      return end.slice(0, 4);
    case 'MONTH':
      return end.slice(0, 7);
    case 'QUARTER':
      return `${end.slice(0, 4)}-Q${Math.floor((Number(end.slice(5, 7)) - 1) / 3) + 1}`;
    default:
      return end;
  }
}

/** What a table cell shows: the exact value text, or the status (never zero). */
export function cellText(c: GlobalCell | undefined): string {
  if (!c) return 'NOT_INGESTED';
  return c.status === 'OK' && c.value !== null ? c.value : c.status;
}

/** Countries from the `countries` route option ("US,IN,DE"); unknown codes are dropped. */
export function parseCountries(text: string | undefined, known: readonly string[]): string[] {
  if (!text) return [];
  const want = text.split(',').map((x) => x.trim().toUpperCase());
  return known.filter((k) => want.includes(k));
}

const TONES = ['accent', 'info', 'warn', 'ok', 'bad', 'muted'] as const;

/**
 * Chart series from a series response: x = the period end (ms since epoch), y = the value as a number for plotting
 * only (the exact text is in each point's label). Missing values are gaps, never zero.
 */
export function chartSeries(r: GlobalSeries): ChartSeries[] {
  return r.series
    .filter((s) => s.status === 'OK' && s.points.length)
    .map((s, i) => ({
      id: s.country,
      label: s.area && s.area !== s.country ? `${s.country} (${s.area})` : s.country,
      tone: TONES[i % TONES.length],
      dashed: i >= TONES.length,
      markers: s.points.length <= 40,
      points: s.points.map((p) => ({
        x: Date.parse(`${p.period_end}T00:00:00Z`),
        y: p.value === null ? null : Number(p.value),
        label: `${s.country} ${periodLabel(p)} ${p.value ?? p.missing_reason} ${p.unit} (obs ${p.obs_id})`,
      })),
    }));
}

/** Year ticks spanning the chart's points. */
export function yearTicks(series: readonly ChartSeries[]): { x: number; label: string }[] {
  const xs = series.flatMap((s) => s.points.map((p) => p.x));
  if (!xs.length) return [];
  const y0 = new Date(Math.min(...xs)).getUTCFullYear();
  const y1 = new Date(Math.max(...xs)).getUTCFullYear();
  const out = [];
  for (let y = y0; y <= y1 + 1; y++) out.push({ x: Date.UTC(y, 0, 1), label: String(y) });
  return out;
}
