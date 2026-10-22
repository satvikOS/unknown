/**
 * Shared policy renderers: limit cards with units, policy sections, and the field-by-field diff
 * table. Used by the Policy and Risk workspaces.
 */
import type { ReactNode } from 'react';
import { DataTable, type Column } from '../../ui/DataTable';
import { StatusPill, type Tone } from '../../ui/primitives';
import '../risk/risk.css';
import type { Change, DiffRow } from './diff';
import { fieldSpec, formatField, humanizeKey, sectionTitle } from './fields';

/** One value with its unit and conversion; a type mismatch is flagged in text. */
export function FieldValue({ path, value, currency }: { path: string; value: unknown; currency: string }) {
  const f = formatField(path, value, currency);
  return (
    <span>
      <span className="mono">{f.text}</span>
      {f.detail ? <span className="muted"> ({f.detail})</span> : null}
      {f.warning ? (
        <>
          {' '}
          <StatusPill label={f.warning} tone="warn" compact />
        </>
      ) : null}
    </span>
  );
}

export function fieldLabel(path: string): string {
  return fieldSpec(path)?.label ?? humanizeKey(path);
}

/** Limit cards: label, value with unit, conversion and meaning. */
export function LimitCards({ entries, currency, ariaLabel }: { entries: [path: string, value: unknown][]; currency: string; ariaLabel: string }) {
  return (
    <dl className="rk-limits" aria-label={ariaLabel}>
      {entries.map(([path, value]) => {
        const spec = fieldSpec(path);
        const f = formatField(path, value, currency);
        return (
          <div key={path} className={`rk-limit${f.warning ? ' rk-warning' : ''}`}>
            <dt title={path}>{spec?.label ?? humanizeKey(path)}</dt>
            <dd>
              {f.text}
              {f.detail ? <span className="rk-detail">{f.detail}</span> : null}
              {f.warning ? <span className="rk-help">Value has the wrong type: {f.warning}.</span> : null}
              {spec ? <span className="rk-help">{spec.help}</span> : <span className="rk-help">Field not described by this terminal.</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

/** A section of a policy body as a definition list (known fields first, unknown fields raw). */
export function FieldList({ entries, currency, extra }: { entries: [path: string, value: unknown][]; currency: string; extra?: ReactNode }) {
  return (
    <dl style={{ margin: 0 }}>
      {entries.map(([path, value]) => {
        const spec = fieldSpec(path);
        return (
          <div key={path} className="rk-field-row">
            <dt>
              {spec?.label ?? humanizeKey(path)}
              <span className="rk-path">{path}</span>
            </dt>
            <dd>
              <FieldValue path={path} value={value} currency={currency} />
              {spec ? <span className="rk-help">{spec.help}</span> : <span className="rk-help">Not described by this terminal; shown as sent.</span>}
            </dd>
          </div>
        );
      })}
      {extra}
    </dl>
  );
}

const CHANGE: Record<Change, { label: string; tone: Tone }> = {
  changed: { label: 'changed', tone: 'warn' },
  added: { label: 'added', tone: 'info' },
  removed: { label: 'removed', tone: 'bad' },
  same: { label: 'same', tone: 'neutral' },
};

export function ChangePill({ change }: { change: Change }) {
  return <StatusPill label={CHANGE[change].label} tone={CHANGE[change].tone} compact />;
}

function DiffValue({ row, side, currency }: { row: DiffRow; side: 'a' | 'b'; currency: string }) {
  const present = side === 'a' ? 'a' in row : 'b' in row;
  if (!present) return <span className="muted">absent</span>;
  const value = side === 'a' ? row.a : row.b;
  const delta = side === 'b' && (row.listAdded?.length || row.listRemoved?.length);
  return (
    <span className={side === 'a' && row.change === 'changed' ? 'rk-old' : undefined}>
      <FieldValue path={row.path} value={value} currency={currency} />
      {delta ? (
        <span className="rk-list-delta">
          {row.listAdded?.length ? `+ ${row.listAdded.map((v) => (typeof v === 'string' ? v : JSON.stringify(v))).join(', ')}` : ''}
          {row.listAdded?.length && row.listRemoved?.length ? '  ' : ''}
          {row.listRemoved?.length ? `− ${row.listRemoved.map((v) => (typeof v === 'string' ? v : JSON.stringify(v))).join(', ')}` : ''}
        </span>
      ) : null}
    </span>
  );
}

export interface DiffTableProps {
  rows: readonly DiffRow[];
  /** Currency for the base and compared bodies (each body states its own). */
  currencyA: string;
  currencyB: string;
  baseLabel: string;
  compareLabel: string;
  caption: string;
  emptyText?: ReactNode;
  maxHeight?: string;
  /** Narrow layout: no section column (the field path names the section). */
  compact?: boolean;
}

export function DiffTable({ rows, currencyA, currencyB, baseLabel, compareLabel, caption, emptyText, maxHeight, compact }: DiffTableProps) {
  const columns: Column<DiffRow>[] = [
    ...(compact
      ? []
      : [
          {
            id: 'section',
            header: 'Section',
            sortValue: (r: DiffRow) => sectionTitle(r.section),
            cell: (r: DiffRow) => <span className="muted">{sectionTitle(r.section)}</span>,
          },
        ]),
    {
      id: 'field',
      header: 'Field',
      wrap: true,
      sortValue: (r) => r.path,
      cell: (r) => (
        <span>
          {fieldLabel(r.path)}
          <span className="rk-list-delta mono">{r.path}</span>
        </span>
      ),
    },
    { id: 'a', header: baseLabel, wrap: true, cell: (r) => <DiffValue row={r} side="a" currency={currencyA} /> },
    { id: 'b', header: compareLabel, wrap: true, cell: (r) => <DiffValue row={r} side="b" currency={currencyB} /> },
    { id: 'change', header: 'Change', sortValue: (r) => r.change, cell: (r) => <ChangePill change={r.change} /> },
  ];
  return (
    <DataTable
      caption={caption}
      captionHidden
      columns={columns}
      rows={rows}
      rowKey={(r) => r.path}
      emptyText={emptyText ?? 'No differences.'}
      maxHeight={maxHeight ?? 'min(60vh, 560px)'}
    />
  );
}
