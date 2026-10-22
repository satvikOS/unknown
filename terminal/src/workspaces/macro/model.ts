/**
 * Markets and macro workspace model: wire types of /api/macro (backend api/routes/company.py `macro`,
 * data/sources/treasury.py and ecb.py), the route and the chart series. Values stay decimal strings;
 * plotting positions are the only numbers derived from them.
 */
import type { ChartPoint } from '../research-common/charts';

export interface MacroSeries {
  dataset: string;
  title: string;
  observations: number;
  first_date: string | null;
  last_date: string | null;
  newest_available_time: string | null;
  status: string;
  reason?: string;
}

export interface MacroCatalog {
  series: MacroSeries[];
  as_of: string;
  not_available: string[];
}

export interface CurvePoint {
  tenor: string;
  /** Years as written by the API ("1/12", "0.25", "10"). */
  years: string;
  value_pct: string;
  obs_id: number;
  available_time: string;
  publication_id: string;
  flags: string[];
}

export interface CurveSlope {
  value_bp: string;
  formula: string;
  unit: string;
  inputs: number[];
  curve_date: string;
  label: string;
}

export interface CurveResponse {
  status: string;
  reason?: string;
  curve?: string;
  curve_date?: string;
  as_of: string;
  unit?: string;
  points?: CurvePoint[];
  missing_tenors?: string[];
  slope_10y_2y?: CurveSlope | null;
  dataset: string;
  entity_id?: string;
  attribution?: string;
  method_note?: string;
  requested_date?: string;
  fallback?: string;
}

export interface FxRate {
  currency: string;
  rate: string;
  orientation: string;
  reference_date: string;
  obs_id: number;
  available_time: string;
  flags: string[];
}

export interface FxCross {
  value: string;
  meaning: string;
  formula: string;
  inputs: number[];
  reference_dates: string[];
  flags: string[];
  label: string;
  rounding: string;
}

export interface FxResponse {
  status: string;
  reason?: string;
  as_of: string;
  requested_date: string | null;
  rates: Record<string, FxRate>;
  stale: string[];
  unavailable: string[];
  dataset: string;
  attribution: string;
  use_note: string;
  normalized?: { base: string; rates?: Record<string, FxCross>; status?: string; reason?: string };
}

/** Tenor order of the Treasury par curve (data/sources/treasury.py ORDER). */
export const TENORS = ['1M', '1.5M', '2M', '3M', '4M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '20Y', '30Y'] as const;

// ------------------------------------------------------------------ route

export const MACRO_KEYS = ['as_of', 'date', 'compare', 'fx_date', 'base'] as const;
export type MacroKey = (typeof MACRO_KEYS)[number];
export type MacroOptions = Partial<Record<MacroKey, string>>;

/** `#/macro/as_of=…/date=…/compare=…/fx_date=…/base=…` */
export function parseMacroRoute(rest: readonly string[]): MacroOptions {
  const out: MacroOptions = {};
  for (const seg of rest) {
    const i = seg.indexOf('=');
    if (i <= 0) continue;
    const k = seg.slice(0, i);
    const v = seg.slice(i + 1).trim();
    if (v && (MACRO_KEYS as readonly string[]).includes(k)) out[k as MacroKey] = v;
  }
  return out;
}

export function macroRoute(o: MacroOptions): string[] {
  return MACRO_KEYS.filter((k) => o[k]?.trim()).map((k) => `${k}=${o[k]!.trim()}`);
}

export function isDate(text: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
  const d = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === text;
}

// ------------------------------------------------------------------ chart data

/**
 * One chart point per tenor in curve order (x = tenor index). A tenor the curve does not carry is a null
 * point, so the line breaks there: missing tenors are never interpolated (CAP-MAC-07).
 */
export function curveSeries(points: readonly CurvePoint[] | undefined, unit = 'percent'): ChartPoint[] {
  const by = new Map((points ?? []).map((p) => [p.tenor, p]));
  const extra = (points ?? []).filter((p) => !(TENORS as readonly string[]).includes(p.tenor)).map((p) => p.tenor);
  return [...TENORS, ...extra].map((t, i) => {
    const p = by.get(t);
    const y = p ? Number(p.value_pct) : null;
    return { x: i, y: y !== null && Number.isFinite(y) ? y : null, label: p ? `${t}: ${p.value_pct} ${unit} (obs ${p.obs_id}, available ${p.available_time})` : `${t}: missing` };
  });
}

/** Tenors present in either curve, for axis labels (index positions match `curveSeries`). */
export function tenorTicks(...curves: (readonly CurvePoint[] | undefined)[]): { x: number; label: string }[] {
  const extra = new Set<string>();
  for (const c of curves) for (const p of c ?? []) if (!(TENORS as readonly string[]).includes(p.tenor)) extra.add(p.tenor);
  return [...TENORS, ...extra].map((t, i) => ({ x: i, label: t }));
}

/** Rates sorted by currency code, with the stale and unavailable lists the API reports. */
export function fxRows(fx: FxResponse): FxRate[] {
  return Object.values(fx.rates).sort((a, b) => a.currency.localeCompare(b.currency));
}

export function crossRows(fx: FxResponse): (FxCross & { currency: string })[] {
  const r = fx.normalized?.rates;
  if (!r) return [];
  return Object.entries(r)
    .map(([currency, c]) => ({ currency, ...c }))
    .sort((a, b) => (a.currency === 'EUR' ? -1 : b.currency === 'EUR' ? 1 : a.currency.localeCompare(b.currency)));
}
