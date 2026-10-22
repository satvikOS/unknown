/**
 * Command palette (Cmd/Ctrl+K): jump to workspaces and to sessions, orders and instruments (when
 * their workspaces are registered), and run the commands the owner is allowed to run here.
 * ARIA combobox + listbox with aria-activedescendant; arrows move, Enter runs, Escape closes.
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Modal } from './Modal';
import { filterPalette, type PaletteItem } from './palette';

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  items: readonly PaletteItem[];
  /** Shown under the list while entity sources load or fail. */
  status?: string;
}

export function CommandPalette({ open, onClose, items, status }: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const baseId = useId();
  const listId = `${baseId}-list`;

  useEffect(() => {
    if (open) {
      setQuery('');
      setActive(0);
    }
  }, [open]);

  const results = useMemo(() => filterPalette(items, query), [items, query]);
  const activeIndex = Math.min(active, Math.max(0, results.length - 1));
  const optionId = (i: number) => `${baseId}-opt-${i}`;

  useEffect(() => {
    listRef.current?.querySelector(`#${CSS.escape(optionId(activeIndex))}`)?.scrollIntoView({ block: 'nearest' });
  });

  const run = (item: PaletteItem | undefined) => {
    if (!item) return;
    onClose();
    // Let the dialog unmount (and restore focus) before the action moves focus elsewhere.
    setTimeout(() => item.run(), 0);
  };

  const grouped = query.trim() === '';
  let lastGroup = '';

  return (
    <Modal open={open} onClose={onClose} labelledBy={`${baseId}-label`} initialFocus={inputRef} className="palette">
      <label id={`${baseId}-label`} htmlFor={`${baseId}-input`} className="sr-only">
        Search workspaces, records and commands
      </label>
      <input
        ref={inputRef}
        id={`${baseId}-input`}
        className="palette-input"
        role="combobox"
        aria-expanded="true"
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={results.length ? optionId(activeIndex) : undefined}
        placeholder="Jump to… or run a command"
        value={query}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
        }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActive((activeIndex + 1) % Math.max(1, results.length));
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActive((activeIndex - 1 + results.length) % Math.max(1, results.length));
          } else if (e.key === 'Home' && e.ctrlKey) {
            setActive(0);
          } else if (e.key === 'End' && e.ctrlKey) {
            setActive(results.length - 1);
          } else if (e.key === 'Enter') {
            e.preventDefault();
            run(results[activeIndex]);
          }
        }}
      />
      <ul ref={listRef} id={listId} className="palette-list" role="listbox" aria-label="Results">
        {results.length === 0 ? (
          <li role="presentation" className="empty">
            No match.
          </li>
        ) : null}
        {results.map((item, i) => {
          const header = grouped && item.group !== lastGroup ? item.group : null;
          lastGroup = item.group;
          return [
            header ? (
              <li key={`g-${item.group}`} role="presentation" className="palette-group">
                {header}
              </li>
            ) : null,
            <li
              key={item.id}
              id={optionId(i)}
              role="option"
              aria-selected={i === activeIndex}
              className={`palette-option${item.danger ? ' danger' : ''}`}
              onMouseMove={() => setActive(i)}
              onClick={() => run(item)}
            >
              <span className="label">{item.label}</span>
              {!grouped ? <span className="badge">{item.group}</span> : null}
              {item.hint ? <span className="hint">{item.hint}</span> : null}
            </li>,
          ];
        })}
      </ul>
      <div className="modal-actions" style={{ justifyContent: 'space-between' }}>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          {status ?? `${results.length} result${results.length === 1 ? '' : 's'}`}
        </span>
        <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
          <kbd>↑</kbd> <kbd>↓</kbd> move · <kbd>Enter</kbd> run · <kbd>Esc</kbd> close
        </span>
      </div>
    </Modal>
  );
}
