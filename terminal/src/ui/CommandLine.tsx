/**
 * Command line, always visible under the header: `<SECURITY> <FUNCTION> <GO>`. Enter (or the GO button) runs it;
 * Tab or a click accepts a suggestion; Up/Down walk suggestions when shown, otherwise the history; Esc clears.
 * F1 opens the function list and F2 focuses the command line from anywhere. A function only opens a view: nothing
 * typed here places, changes or cancels an order.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { hasWorkspace, WORKSPACES } from '../workspaces/registry';
import {
  applySuggestion,
  commandTarget,
  executeCommand,
  functionsVersion,
  listFunctions,
  loadHistory,
  parseCommand,
  pushHistory,
  saveHistory,
  subscribeCommandTarget,
  subscribeFunctions,
  suggestFunctions,
  type TerminalFunction,
} from './functions';
import { channelInfo, isUnlinked, linkedSecurity, setLinkedSecurity, useLinkedSecurity } from './link';
import { Modal } from './Modal';

export function installed(workspace: string): boolean {
  return workspace === 'workstation' || hasWorkspace(workspace);
}

export function useFunctions(): TerminalFunction[] {
  const v = useSyncExternalStore(subscribeFunctions, functionsVersion, functionsVersion);
  return useMemo(() => {
    void v;
    return listFunctions(WORKSPACES);
  }, [v]);
}

export function useCommandTarget() {
  return useSyncExternalStore(subscribeCommandTarget, commandTarget, commandTarget);
}

/** The function list (HELP <GO> or F1). */
export function FunctionHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  const fns = useFunctions();
  return (
    <Modal open={open} onClose={onClose} labelledBy="fn-help-title">
      <header>
        <h2 id="fn-help-title">Functions</h2>
      </header>
      <div className="modal-body">
        <p className="muted" style={{ marginTop: 0 }}>
          Type a security and a function, then Enter: <span className="mono">AAPL.XNAS GP</span>. A security is{' '}
          <span className="mono">SYMBOL.MIC</span> (for example <span className="mono">RELIANCE.XNSE</span>,{' '}
          <span className="mono">SAP.XETR</span>). F2 jumps to the command line from anywhere.
        </p>
        <table className="data">
          <caption className="sr-only">Functions</caption>
          <thead>
            <tr>
              <th scope="col">
                <span className="th-inner">Function</span>
              </th>
              <th scope="col">
                <span className="th-inner">Opens</span>
              </th>
              <th scope="col">
                <span className="th-inner">Security</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {fns.map((f) => (
              <tr key={f.mnemonic}>
                <td>
                  <span className="fn-mnemonic">{f.mnemonic}</span>
                </td>
                <td className="wrap">
                  {f.help}
                  {installed(f.workspace) ? null : <span className="muted"> (not installed in this build)</span>}
                </td>
                <td>{f.needsSecurity ? 'needed' : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="modal-actions">
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}

export function CommandLine({ onHelp }: { onHelp: () => void }) {
  const fns = useFunctions();
  const target = useCommandTarget();
  const channel = target?.channel ?? 'default';
  const security = useLinkedSecurity(channel);
  const [text, setText] = useState('');
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [history, setHistory] = useState<string[]>(() => loadHistory());
  const [histIndex, setHistIndex] = useState<number | null>(null);
  const [sel, setSel] = useState(0);
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const suggestions = useMemo(() => (open ? suggestFunctions(text, fns) : []), [open, text, fns]);
  const showList = open && suggestions.length > 0;

  const run = useCallback(
    (raw: string) => {
      const outcome = executeCommand(parseCommand(raw, fns), {
        installed,
        target: commandTarget(),
        linked: (ch) => linkedSecurity(ch),
        setLinked: (key, ch) => setLinkedSecurity(key, ch),
      });
      setMessage({ ok: outcome.ok, text: outcome.message });
      if (outcome.showHelp) onHelp();
      if (raw.trim()) {
        const next = pushHistory(history, raw);
        setHistory(next);
        saveHistory(next);
      }
      if (outcome.ok) setText('');
      setHistIndex(null);
      setOpen(false);
    },
    [fns, history, onHelp],
  );

  // F1 (function list) and F2 (focus the command line) work from anywhere unless a dialog owns the keyboard.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      if (e.key === 'F1') {
        e.preventDefault();
        onHelp();
      } else if (e.key === 'F2' && !document.querySelector('.overlay')) {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onHelp]);

  const accept = (m: string) => {
    setText(`${applySuggestion(text, m)} `);
    setOpen(false);
    setSel(0);
    inputRef.current?.focus();
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const down = e.key === 'ArrowDown';
      if (showList) {
        setSel((s) => (s + (down ? 1 : -1) + suggestions.length) % suggestions.length);
        return;
      }
      if (!history.length) return;
      const i = histIndex === null ? (down ? null : history.length - 1) : histIndex + (down ? 1 : -1);
      if (i === null || i >= history.length) {
        setHistIndex(null);
        setText('');
      } else {
        const j = Math.max(0, i);
        setHistIndex(j);
        setText(history[j] ?? '');
      }
      return;
    }
    if (e.key === 'Tab' && showList) {
      e.preventDefault();
      accept(suggestions[sel]?.mnemonic ?? '');
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      run(text);
      return;
    }
    if (e.key === 'Escape') {
      if (showList) setOpen(false);
      else if (text) setText('');
      else inputRef.current?.blur();
      e.stopPropagation();
    }
  };

  const info = channelInfo(channel);
  const chipLabel = isUnlinked(channel) ? 'Unlinked' : info ? `${info.num} ${info.name}` : channel;
  const activeId = showList ? `cmd-opt-${sel}` : undefined;
  return (
    <form
      className="cmdbar"
      aria-label="Command line"
      onSubmit={(e) => {
        e.preventDefault();
        run(text);
      }}
    >
      <span className="cmdbar-target" title={`Commands go to ${target?.label ?? 'nowhere yet'} on link channel ${chipLabel}`}>
        <span className="link-chip-dot" style={{ ['--chip' as string]: info?.color ?? 'var(--text-muted)' }} aria-hidden="true" />
        <span className="hide-sm">{chipLabel}</span>
        <span className="num cmdbar-sec">{security ?? '—'}</span>
      </span>
      <div className="cmdbar-field">
        <input
          ref={inputRef}
          className="cmdbar-input"
          value={text}
          role="combobox"
          aria-label="Command: security, function, then Enter"
          aria-expanded={showList}
          aria-controls="cmd-suggest"
          aria-autocomplete="list"
          aria-activedescendant={activeId}
          aria-keyshortcuts="F2"
          autoComplete="off"
          spellCheck={false}
          placeholder="AAPL.XNAS GP  ·  MON  ·  HELP"
          onChange={(e) => {
            setText(e.target.value);
            setOpen(true);
            setSel(0);
            setHistIndex(null);
          }}
          onKeyDown={onKeyDown}
          onBlur={() => setOpen(false)}
        />
        <ul id="cmd-suggest" role="listbox" aria-label="Functions" className="cmd-suggest" hidden={!showList}>
          {suggestions.map((f, i) => (
            <li
              key={f.mnemonic}
              id={`cmd-opt-${i}`}
              role="option"
              aria-selected={i === sel}
              onMouseDown={(e) => {
                e.preventDefault();
                accept(f.mnemonic);
              }}
            >
              <span className="fn-mnemonic">{f.mnemonic}</span>
              <span className="cmd-help">
                {f.help}
                {installed(f.workspace) ? '' : ' (not installed)'}
              </span>
            </li>
          ))}
        </ul>
      </div>
      <button type="submit" className="btn cmd-go">
        GO
      </button>
      <span className={`cmdbar-msg${message && !message.ok ? ' is-error' : ''}`} role="status" aria-live="polite">
        {message?.text ?? ''}
      </span>
    </form>
  );
}
