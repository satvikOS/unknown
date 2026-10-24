/** Instrument master export (ui/export.ts): the core's registered instrument records, owner data. */
import type { Instrument } from '../../api/types';
import type { ExportColumn, ExportTable } from '../../ui/export';

export function instrumentsExport(rows: readonly Instrument[], filters: Record<string, string>): ExportTable<Instrument> {
  const columns: ExportColumn<Instrument>[] = [
    { id: 'id', header: 'Instrument', value: (i) => i.instrument_id },
    { id: 'symbol', header: 'Symbol', value: (i) => i.symbol },
    { id: 'name', header: 'Name', value: (i) => i.name },
    { id: 'class', header: 'Asset class', value: (i) => i.asset_class },
    { id: 'venue', header: 'Venue (MIC)', value: (i) => i.venue_mic },
    { id: 'ccy', header: 'Currency', value: (i) => i.currency },
    { id: 'sccy', header: 'Settlement currency', value: (i) => i.settlement_currency },
    { id: 'inc', header: 'Quantity increment', kind: 'decimal', value: (i) => i.quantity_increment },
    { id: 'min', header: 'Minimum quantity', kind: 'decimal', value: (i) => i.min_quantity },
    { id: 'mult', header: 'Multiplier', kind: 'decimal', value: (i) => i.multiplier },
    { id: 'sdays', header: 'Settlement days', kind: 'decimal', value: (i) => String(i.settlement_days) },
    { id: 'status', header: 'Status', value: (i) => i.status },
    { id: 'version', header: 'Metadata version', kind: 'decimal', value: (i) => String(i.metadata_version) },
    { id: 'fixture', header: 'Fixture (not a real security)', value: (i) => (i.attributes.fixture === true ? 'yes' : 'no') },
  ];
  return {
    title: 'Instrument master',
    rows,
    columns,
    provenance: {
      source: 'Instrument master: GET /api/instruments (core instrument records)',
      asOf: new Date().toISOString(),
      filters,
      notes: ['as of: the time the export was taken from the list shown on screen'],
    },
  };
}
