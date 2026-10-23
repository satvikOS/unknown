/**
 * Instrument master helpers (domain/instruments.py): tick bands, fixture flag, mappings, search.
 * Prices and ticks stay decimal strings; comparisons are exact (compareDecimal).
 */
import { exactNs, isNsStringSibling } from '../../api/nsStrings';
import type { Instrument, ProviderMapping } from '../../api/types';
import { compareDecimal, isDecimal } from '../../format/decimal';

export interface TickBand {
  /** Applies to prices >= from ... */
  from: string;
  /** ... and < to (null: no upper bound). */
  to: string | null;
  tick: string;
}

/** Tick rules as contiguous bands, ordered by from_price (a rule applies until the next rule's from_price). */
export function tickBands(rules: Instrument['tick_rules']): TickBand[] {
  const valid = rules.filter((r) => isDecimal(r.from_price) && isDecimal(r.tick));
  const sorted = [...valid].sort((a, b) => compareDecimal(a.from_price, b.from_price));
  return sorted.map((r, i) => ({ from: r.from_price, to: sorted[i + 1]?.from_price ?? null, tick: r.tick }));
}

/** Tick size that applies at `price`, or null when no band covers it (below the first from_price). */
export function tickAt(rules: Instrument['tick_rules'], price: string | null | undefined): string | null {
  if (!price || !isDecimal(price)) return null;
  let tick: string | null = null;
  for (const b of tickBands(rules)) {
    if (compareDecimal(price, b.from) >= 0) tick = b.tick;
  }
  return tick;
}

/** One-line summary: "0.0001 below 1 · 0.01 from 1". */
export function tickSummary(rules: Instrument['tick_rules']): string {
  const bands = tickBands(rules);
  if (bands.length === 0) return 'no tick rules';
  if (bands.length === 1) return `${bands[0]!.tick} from ${bands[0]!.from}`;
  return bands.map((b) => (b.to !== null ? `${b.tick} below ${b.to}` : `${b.tick} from ${b.from}`)).join(' · ');
}

/** Simulation-only reference data: the core marks fixture instruments with attributes.fixture = true. */
export function isFixture(i: Pick<Instrument, 'attributes'>): boolean {
  const f = i.attributes?.fixture;
  return f === true || f === 'true' || f === 1;
}

export interface InstrumentFlag {
  label: string;
  tone: 'warn' | 'bad' | 'info';
  detail: string;
}

export function instrumentFlags(i: Instrument): InstrumentFlag[] {
  const flags: InstrumentFlag[] = [];
  if (isFixture(i)) flags.push({ label: 'FIXTURE', tone: 'warn', detail: 'Fixture instrument for simulation only; not real reference data.' });
  if (i.status !== 'ACTIVE') flags.push({ label: i.status, tone: 'bad', detail: `Instrument status ${i.status}.` });
  if (i.leveraged) flags.push({ label: 'LEVERAGED', tone: 'info', detail: 'Leveraged product (policy eligibility decides tradability).' });
  if (i.inverse) flags.push({ label: 'INVERSE', tone: 'info', detail: 'Inverse product (policy eligibility decides tradability).' });
  return flags;
}

export function settlementLabel(days: number): string {
  return Number.isInteger(days) && days >= 0 ? `T+${days}` : String(days);
}

export interface MappingRow extends ProviderMapping {
  /** The highest version per provider is current; older versions are superseded history. */
  current: boolean;
}

export function mappingRows(mappings: readonly ProviderMapping[]): MappingRow[] {
  const latest = new Map<string, number>();
  for (const m of mappings) latest.set(m.provider, Math.max(latest.get(m.provider) ?? -Infinity, m.version));
  return [...mappings]
    .sort((a, b) => a.provider.localeCompare(b.provider) || b.version - a.version)
    .map((m) => ({ ...m, current: latest.get(m.provider) === m.version }));
}

/** Case-insensitive match on symbol, name, id, asset class, venue and provider ids. */
export function matchInstrument(i: Instrument, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const hay = [i.symbol, i.name, i.instrument_id, i.asset_class, i.venue_mic, i.currency, ...i.mappings.flatMap((m) => [m.provider, m.external_id])]
    .join(' ')
    .toLowerCase();
  return q.split(/\s+/).every((t) => hay.includes(t));
}

/**
 * Attributes sorted by key, with the fixture marker first. A `*_ns` value is given as the API's exact `*_ns_str`
 * (JSON.parse rounded the number), and the sibling itself is not listed as an attribute.
 */
export function attributeEntries(attributes: Record<string, unknown> | null | undefined): [string, unknown][] {
  const attrs = attributes ?? {};
  return Object.entries(attrs)
    .filter(([k]) => !isNsStringSibling(k, attrs))
    .map(([k, v]): [string, unknown] => [k, exactNs(attrs, k) ?? v])
    .sort(([a], [b]) => (a === 'fixture' ? -1 : b === 'fixture' ? 1 : a.localeCompare(b)));
}
