/** Blotter export (ui/export.ts): every column is the core's own order record, so the whole table is owner data. */
import { nsStrOf } from '../../api/nsStrings';
import type { Instrument, Order } from '../../api/types';
import type { ExportColumn, ExportTable } from '../../ui/export';

const t = (o: Order, k: 'accepted_at_ns' | 'submitted_at_ns' | 'terminal_at_ns' | 'deadline_ns') => nsStrOf(o, k) ?? (o[k] as number | null);

export function blotterExport(rows: readonly Order[], instruments: ReadonlyMap<string, Instrument>, filters: Record<string, string>): ExportTable<Order> {
  const columns: ExportColumn<Order>[] = [
    { id: 'order_id', header: 'Order', value: (o) => o.order_id },
    { id: 'session', header: 'Session', value: (o) => o.session_id },
    { id: 'instrument', header: 'Instrument', value: (o) => o.instrument_id },
    { id: 'symbol', header: 'Symbol', value: (o) => instruments.get(o.instrument_id)?.symbol ?? null, reason: () => 'NOT_FOUND' },
    { id: 'side', header: 'Side', value: (o) => o.side },
    { id: 'type', header: 'Type', value: (o) => o.order_type },
    { id: 'tif', header: 'Time in force', value: (o) => o.time_in_force },
    { id: 'qty', header: 'Quantity', kind: 'decimal', value: (o) => o.quantity },
    { id: 'limit', header: 'Limit', kind: 'decimal', value: (o) => o.limit_price },
    { id: 'ccy', header: 'Currency', value: (o) => o.currency },
    { id: 'state', header: 'State', value: (o) => o.state },
    { id: 'cancel', header: 'Cancel state', value: (o) => o.cancel_state },
    { id: 'cum', header: 'Filled', kind: 'decimal', value: (o) => o.cum_qty },
    { id: 'leaves', header: 'Leaves', kind: 'decimal', value: (o) => o.leaves_qty },
    { id: 'avg', header: 'Average price', kind: 'decimal', value: (o) => o.avg_price, reason: () => 'NOT_APPLICABLE' },
    { id: 'notional', header: 'Notional filled', kind: 'decimal', value: (o) => o.notional_filled },
    { id: 'fees_est', header: 'Fees estimated', kind: 'decimal', value: (o) => o.fees_estimated },
    { id: 'fees_final', header: 'Fees final', kind: 'decimal', value: (o) => o.fees_final },
    { id: 'broker_id', header: 'Broker order id', value: (o) => o.broker_order_id, reason: () => 'PENDING' },
    { id: 'accepted', header: 'Accepted', kind: 'datetime', value: (o) => t(o, 'accepted_at_ns') },
    { id: 'submitted', header: 'Submitted', kind: 'datetime', value: (o) => t(o, 'submitted_at_ns'), reason: () => 'PENDING' },
    { id: 'terminal', header: 'Terminal', kind: 'datetime', value: (o) => t(o, 'terminal_at_ns'), reason: () => 'NOT_APPLICABLE' },
    { id: 'purpose', header: 'Purpose', value: (o) => o.purpose },
    { id: 'reason', header: 'Reason', value: (o) => o.reason, reason: () => 'NOT_APPLICABLE' },
  ];
  return {
    title: 'Orders',
    columns,
    rows,
    provenance: {
      source: 'Order blotter: GET /api/orders (core order records)',
      asOf: new Date().toISOString(),
      filters,
      notes: ['as of: the time the export was taken from the blotter shown on screen'],
    },
  };
}
