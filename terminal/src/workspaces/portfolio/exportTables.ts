/**
 * Export tables for the portfolio grids (ui/export.ts). Quantities, cost and owners are ledger data (owner-generated);
 * marks and everything derived from them (market value, unrealized P&L, weights) carry the quote's market-data source
 * and are exported only when that source's rights allow it.
 */
import { OWNER, marketSource, nsToIso, type ExportColumn, type ExportSource, type ExportTable } from '../../ui/export';
import { dec, isRec, str, type Rec } from './model';

function markSource(p: Rec): ExportSource {
  const m = isRec(p.mark) ? p.mark : {};
  if (p.fixture === true || p.instrument_fixture === true) return { kind: 'licensed', source: 'fixture' };
  return marketSource(str(m.source), m.delayed !== true);
}

export function positionsExport(body: Rec, rows: readonly Rec[], filters: Record<string, string>): ExportTable<Rec> {
  const mark = (p: Rec): Rec => (isRec(p.mark) ? p.mark : {});
  const w = (p: Rec) => (isRec(p.weights) ? dec(p.weights.scope_nav) : null);
  const columns: ExportColumn<Rec>[] = [
    { id: 'owner', header: 'Owner', value: (p) => str(p.owner) },
    { id: 'instrument', header: 'Instrument', value: (p) => str(p.instrument_id) },
    { id: 'symbol', header: 'Symbol', value: (p) => str(p.symbol), reason: () => 'NOT_REPORTED' },
    { id: 'currency', header: 'Currency', value: (p) => str(p.currency) },
    { id: 'qty', header: 'Quantity', kind: 'decimal', value: (p) => dec(p.quantity), source: OWNER },
    { id: 'cost', header: 'Cost basis', kind: 'decimal', value: (p) => dec(p.cost_basis), source: OWNER },
    { id: 'avg', header: 'Average cost', kind: 'decimal', value: (p) => dec(p.average_cost), reason: () => 'NOT_APPLICABLE' },
    { id: 'mark', header: 'Mark', kind: 'decimal', value: (p) => dec(mark(p).price), reason: (p) => str(mark(p).status) ?? 'NO_DATA', source: markSource },
    { id: 'mark_status', header: 'Mark status', value: (p) => str(mark(p).status), source: markSource },
    { id: 'mv', header: 'Market value', kind: 'decimal', value: (p) => dec(p.market_value), reason: () => 'NO_DATA', source: markSource },
    { id: 'upl', header: 'Unrealized', kind: 'decimal', value: (p) => dec(p.unrealized), reason: () => 'NO_DATA', source: markSource },
    { id: 'w', header: 'Weight of scope NAV', kind: 'decimal', value: w, reason: () => 'NO_DATA', source: markSource },
  ];
  return {
    title: 'Positions',
    columns,
    rows,
    provenance: {
      source: 'Positions: GET /api/portfolio/holdings (core ledger; marks from the core quote book)',
      asOf: nsToIso(body.as_of_ns),
      asOfReason: 'NOT_REPORTED',
      filters,
      notes: [`core seq ${String(body.core_seq ?? 'not reported')}`, 'amounts are exact decimals in each row currency'],
    },
  };
}
