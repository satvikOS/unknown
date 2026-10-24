/**
 * Accessible data table: semantic <table> with column headers, sortable headers with aria-sort,
 * a sticky header, roving-focus keyboard row navigation (arrows, Home/End, PageUp/PageDown,
 * Enter to activate), right-aligned monospace numeric columns, and windowed rendering above
 * `virtualizeAbove` rows (aria-rowcount / aria-rowindex keep the full size announced). A cell renderer shows a
 * missing value with <NoValue reason> (ui/unavailable.tsx): inside the table the focused row shows every reason.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { sortRows, visibleRange, type SortDirection, type SortKind, type SortValue } from './tableSort';
import { InGridProvider } from './unavailable';

export interface Column<T> {
  id: string;
  /** Header text (also the accessible column name). */
  header: string;
  /** Longer explanation shown as the header tooltip. */
  description?: string;
  cell: (row: T) => ReactNode;
  /** Providing this makes the column sortable. Return decimal strings with sortKind 'decimal'. */
  sortValue?: (row: T) => SortValue;
  sortKind?: SortKind;
  align?: 'left' | 'right' | 'center';
  /** Right-aligned monospace (amounts, quantities, counts). Implies align 'right'. */
  numeric?: boolean;
  /** Allow the cell to wrap (long text). */
  wrap?: boolean;
  width?: string | number;
}

export interface DataTableProps<T> {
  /** Accessible table name; rendered as the caption unless `captionHidden`. */
  caption: string;
  captionHidden?: boolean;
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  initialSort?: { columnId: string; direction: SortDirection };
  /** Enter / Space / double-click on a row. */
  onRowActivate?: (row: T) => void;
  /** Accessible label for the activate action, e.g. "Open session". */
  activateLabel?: string;
  emptyText?: ReactNode;
  /** Render only visible rows when there are more than this many (default 500). */
  virtualizeAbove?: number;
  /** Row height in px used for windowing (default 28, matching --row-h). */
  rowHeight?: number;
  /** Scroll viewport height; the header sticks within it (default "min(70vh, 640px)"). */
  maxHeight?: string;
}

function alignOf<T>(c: Column<T>): 'left' | 'right' | 'center' {
  return c.numeric ? 'right' : (c.align ?? 'left');
}

export function DataTable<T>({
  caption,
  captionHidden,
  columns,
  rows,
  rowKey,
  initialSort,
  onRowActivate,
  activateLabel,
  emptyText = 'No rows.',
  virtualizeAbove = 500,
  rowHeight = 28,
  maxHeight = 'min(70vh, 640px)',
}: DataTableProps<T>) {
  const [sort, setSort] = useState<{ columnId: string; direction: SortDirection } | null>(initialSort ?? null);
  const [active, setActive] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(600);
  const wrapRef = useRef<HTMLDivElement>(null);
  const pendingFocus = useRef<number | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows.slice();
    const col = columns.find((c) => c.id === sort.columnId);
    if (!col?.sortValue) return rows.slice();
    return sortRows(rows, col.sortValue, col.sortKind ?? (col.numeric ? 'number' : 'text'), sort.direction);
  }, [rows, columns, sort]);

  const total = sorted.length;
  const virtual = total > virtualizeAbove;
  const range = virtual ? visibleRange(total, scrollTop, viewport, rowHeight) : { start: 0, end: total };
  const activeIndex = Math.min(active, Math.max(0, total - 1));

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el || !virtual) return;
    setViewport(el.clientHeight);
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setViewport(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, [virtual]);

  // Focus the requested row after it has been rendered (it may have been outside the window).
  useEffect(() => {
    if (pendingFocus.current === null) return;
    const idx = pendingFocus.current;
    const row = wrapRef.current?.querySelector<HTMLTableRowElement>(`tr[data-index="${idx}"]`);
    if (row) {
      pendingFocus.current = null;
      row.focus({ preventScroll: virtual });
      if (!virtual) row.scrollIntoView({ block: 'nearest' });
    }
  });

  const moveTo = useCallback(
    (idx: number) => {
      if (total === 0) return;
      const next = Math.max(0, Math.min(total - 1, idx));
      setActive(next);
      pendingFocus.current = next;
      const el = wrapRef.current;
      if (virtual && el) {
        const header = rowHeight; // sticky header occupies about one row
        const top = next * rowHeight;
        if (top < el.scrollTop) el.scrollTop = top;
        else if (top + rowHeight > el.scrollTop + el.clientHeight - header) {
          el.scrollTop = top + rowHeight - el.clientHeight + header;
        }
        setScrollTop(el.scrollTop);
      }
    },
    [total, virtual, rowHeight],
  );

  const onKeyDown = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    const page = Math.max(1, Math.floor(viewport / rowHeight) - 1);
    switch (e.key) {
      case 'ArrowDown':
        moveTo(activeIndex + 1);
        break;
      case 'ArrowUp':
        moveTo(activeIndex - 1);
        break;
      case 'Home':
        moveTo(0);
        break;
      case 'End':
        moveTo(total - 1);
        break;
      case 'PageDown':
        moveTo(activeIndex + page);
        break;
      case 'PageUp':
        moveTo(activeIndex - page);
        break;
      case 'Enter':
      case ' ': {
        const row = sorted[activeIndex];
        if (onRowActivate && row !== undefined && e.target instanceof HTMLTableRowElement) {
          onRowActivate(row);
          break;
        }
        return;
      }
      default:
        return;
    }
    e.preventDefault();
  };

  const toggleSort = (c: Column<T>) => {
    setSort((cur) =>
      cur && cur.columnId === c.id
        ? { columnId: c.id, direction: cur.direction === 'ascending' ? 'descending' : 'ascending' }
        : { columnId: c.id, direction: c.numeric ? 'descending' : 'ascending' },
    );
  };

  const visible = sorted.slice(range.start, range.end);
  const topPad = range.start * rowHeight;
  const bottomPad = (total - range.end) * rowHeight;

  return (
    <div
      className="table-wrap"
      ref={wrapRef}
      style={{ maxHeight }}
      onScroll={virtual ? (e) => setScrollTop(e.currentTarget.scrollTop) : undefined}
    >
      <table className="data" aria-rowcount={total + 1}>
        <caption className={captionHidden ? 'sr-only' : undefined}>
          {caption}
          {onRowActivate ? <span className="sr-only">. Use arrow keys to move between rows and Enter to {activateLabel ?? 'open'} a row.</span> : null}
        </caption>
        <thead>
          <tr aria-rowindex={1}>
            {columns.map((c) => {
              const align = alignOf(c);
              const dir = sort?.columnId === c.id ? sort.direction : undefined;
              return (
                <th
                  key={c.id}
                  scope="col"
                  className={align === 'right' ? 'right' : undefined}
                  aria-sort={c.sortValue ? (dir ?? 'none') : undefined}
                  title={c.description}
                  style={c.width !== undefined ? { width: c.width } : undefined}
                >
                  {c.sortValue ? (
                    <button type="button" onClick={() => toggleSort(c)}>
                      {c.header}
                      <span className="sort-glyph" aria-hidden="true">
                        {dir === 'ascending' ? '▲' : dir === 'descending' ? '▼' : ''}
                      </span>
                    </button>
                  ) : (
                    <span className="th-inner">{c.header}</span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <InGridProvider>
        <tbody onKeyDown={onKeyDown}>
          {total === 0 ? (
            <tr>
              <td colSpan={columns.length} className="empty">
                {emptyText}
              </td>
            </tr>
          ) : null}
          {topPad > 0 ? (
            <tr className="spacer" aria-hidden="true">
              <td colSpan={columns.length} style={{ height: topPad }} />
            </tr>
          ) : null}
          {visible.map((row, i) => {
            const index = range.start + i;
            return (
              <tr
                key={rowKey(row)}
                data-index={index}
                aria-rowindex={index + 2}
                tabIndex={index === activeIndex ? 0 : -1}
                data-active={index === activeIndex ? 'true' : undefined}
                onFocus={() => setActive(index)}
                onDoubleClick={onRowActivate ? () => onRowActivate(row) : undefined}
                style={virtual ? { height: rowHeight } : undefined}
              >
                {columns.map((c) => {
                  const align = alignOf(c);
                  const cls = [align !== 'left' ? align : '', c.numeric ? 'num' : '', c.wrap ? 'wrap' : '']
                    .filter(Boolean)
                    .join(' ');
                  return (
                    <td key={c.id} className={cls || undefined}>
                      {c.cell(row)}
                    </td>
                  );
                })}
              </tr>
            );
          })}
          {bottomPad > 0 ? (
            <tr className="spacer" aria-hidden="true">
              <td colSpan={columns.length} style={{ height: bottomPad }} />
            </tr>
          ) : null}
        </tbody>
        </InGridProvider>
      </table>
    </div>
  );
}
