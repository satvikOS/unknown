/**
 * The export control every grid uses (CAP-UX-07; the rules are in ./export.ts). A disclosure button opens three
 * actions (CSV, CSV with semicolons, XLSX); the result, including any column the data rights excluded, is announced
 * in a polite live region next to it. The table is built only when an action is chosen.
 */
import { useId, useRef, useState } from 'react';
import { runExport, type ExportFormat, type ExportTable } from './export';
import './export.css';

export interface ExportButtonProps<T> {
  /** Builds the table to export from what the grid shows now (rows after its filters). */
  table: () => ExportTable<T>;
  label?: string;
  disabled?: boolean;
}

const FORMATS: { id: ExportFormat; label: string }[] = [
  { id: 'csv', label: 'CSV' },
  { id: 'csv-semicolon', label: 'CSV (semicolon)' },
  { id: 'xlsx', label: 'XLSX' },
];

export function ExportButton<T>({ table, label = 'Export', disabled }: ExportButtonProps<T>) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const menuId = useId();
  const btn = useRef<HTMLButtonElement>(null);

  const run = async (f: ExportFormat) => {
    setOpen(false);
    setBusy(true);
    try {
      setStatus(await runExport(table(), f));
    } catch (err) {
      setStatus(`Export failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
      btn.current?.focus();
    }
  };

  return (
    <span className="xp">
      <button
        ref={btn}
        type="button"
        className="btn small"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled || busy}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setOpen(false);
        }}
      >
        {busy ? 'Exporting…' : label}
      </button>
      {open ? (
        <span id={menuId} className="xp-menu" role="group" aria-label={`${label} format`}>
          {FORMATS.map((f) => (
            <button
              key={f.id}
              type="button"
              className="btn ghost small"
              onClick={() => void run(f.id)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  setOpen(false);
                  btn.current?.focus();
                }
              }}
            >
              {f.label}
            </button>
          ))}
        </span>
      ) : null}
      <span className="xp-status" role="status" aria-live="polite">
        {status}
      </span>
    </span>
  );
}
