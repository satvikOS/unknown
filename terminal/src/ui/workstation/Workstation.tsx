/**
 * Multi-panel workstation (CAP-UX-03 linked panels, CAP-UX-04 layouts): a tiled split layout where each panel hosts
 * any registered workspace and follows one link channel. Layouts autosave as a draft, save as named versions,
 * restore older versions, apply templates, lock, and recover from a corrupt saved document. Dividers resize with
 * the pointer or, when focused, with the arrow keys (WCAG 2.5.7). The command line targets the active panel.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { useRuntime } from '../../state/react';
import { WorkspaceHost } from '../../workspaces/lazy';
import { findWorkspace, hasWorkspace, WORKSPACES } from '../../workspaces/registry';
import { pushCommandTarget } from '../functions';
import { channelInfo, LINK_CHANNELS, LinkChannelProvider, unlinkedChannel, isUnlinked, useLinkedSecurity } from '../link';
import { PanelContext } from '../panel';
import {
  closePanel,
  panelsOf,
  resizeSplit,
  setActive,
  setLocked,
  splitPanel,
  TEMPLATES,
  templateLayout,
  updatePanel,
  type LayoutDoc,
  type LayoutNode,
  type PanelNode,
  type SplitNode,
} from './layout';
import { loadStore, openLayout, persistStore, saveLayout, setDraft, type KeyValueStorage, type LayoutStore } from './storage';
import './workstation.css';

function storage(): KeyValueStorage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function defaultTemplate(): LayoutDoc {
  return templateLayout(hasWorkspace('monitor') && hasWorkspace('chart') ? 'trader' : 'desk');
}

const noop = () => undefined;

/** Navigation inside a panel's own workspace is allowed while the layout is locked; changing workspace is not. */
function navigatePanel(d: LayoutDoc, id: string, workspace: string, rest: readonly string[]): LayoutDoc {
  const p = panelsOf(d.root).find((x) => x.id === id);
  if (!p || (d.locked && p.workspace !== workspace)) return d;
  return setLocked(updatePanel(setLocked(d, false), id, { workspace, rest: [...rest] }), d.locked);
}

interface PanelProps {
  node: PanelNode;
  number: number;
  active: boolean;
  locked: boolean;
  canClose: boolean;
  onActivate: (id: string) => void;
  onChange: (id: string, patch: Partial<Pick<PanelNode, 'workspace' | 'rest' | 'channel'>>) => void;
  onNavigate: (id: string, workspace: string, rest: readonly string[]) => void;
  onSplit: (id: string, dir: 'row' | 'column') => void;
  onClose: (id: string) => void;
}

function Panel({ node, number, active, locked, canClose, onActivate, onChange, onNavigate, onSplit, onClose }: PanelProps) {
  const { router } = useRuntime();
  const security = useLinkedSecurity(node.channel);
  const def = node.workspace ? findWorkspace(node.workspace) : undefined;
  const info = channelInfo(node.channel);
  const panelInfo = useMemo(() => ({ panelId: node.id, number }), [node.id, number]);
  const route = useMemo(() => ({ workspace: node.workspace ?? '', rest: node.rest }), [node.workspace, node.rest]);
  const navigate = useCallback(
    (ws: string, ...rest: string[]) => {
      // Inside a panel, navigation stays in the panel; a locked panel may only move within its own workspace.
      if (hasWorkspace(ws) && (!locked || ws === node.workspace)) onNavigate(node.id, ws, rest);
      else router.navigate(ws, ...rest);
    },
    [locked, node.id, node.workspace, onNavigate, router],
  );
  const activate = () => onActivate(node.id);
  return (
    <section
      className={`ws-panel${active ? ' is-active' : ''}`}
      aria-label={`Panel ${number}${def ? `: ${def.title}` : ''}`}
      onPointerDownCapture={activate}
      onFocusCapture={activate}
    >
      <div className="ws-panel-bar">
        <span className="link-chip" style={{ ['--chip' as string]: info?.color ?? 'var(--text-muted)' }}>
          <span className="link-chip-dot" aria-hidden="true" />
          <select
            aria-label={`Panel ${number} link channel`}
            value={isUnlinked(node.channel) ? 'none' : node.channel}
            disabled={locked}
            onChange={(e) => onChange(node.id, { channel: e.target.value === 'none' ? unlinkedChannel(node.id) : e.target.value })}
          >
            {LINK_CHANNELS.map((c) => (
              <option key={c.id} value={c.id}>
                {c.num} {c.name}
              </option>
            ))}
            <option value="none">Unlinked</option>
          </select>
        </span>
        <select
          className="ws-panel-ws"
          aria-label={`Panel ${number} workspace`}
          value={node.workspace ?? ''}
          disabled={locked}
          onChange={(e) => onChange(node.id, { workspace: e.target.value || null, rest: [] })}
        >
          <option value="">Choose…</option>
          {node.workspace && !def ? <option value={node.workspace}>{node.workspace} (not installed)</option> : null}
          {WORKSPACES.map((w) => (
            <option key={w.id} value={w.id}>
              {w.title}
            </option>
          ))}
        </select>
        <span className="ws-panel-sec num" title={`Security on this panel's channel`}>
          {security ?? <span className="muted">no security</span>}
        </span>
        <span className="ws-panel-actions">
          <button type="button" className="btn ghost icon-btn" disabled={locked} onClick={() => onSplit(node.id, 'row')} aria-label={`Split panel ${number} side by side`} title="Split side by side">
            <span aria-hidden="true">◫</span>
          </button>
          <button type="button" className="btn ghost icon-btn" disabled={locked} onClick={() => onSplit(node.id, 'column')} aria-label={`Split panel ${number} top and bottom`} title="Split top and bottom">
            <span aria-hidden="true">⊟</span>
          </button>
          <button type="button" className="btn ghost icon-btn" disabled={locked || !canClose} onClick={() => onClose(node.id)} aria-label={`Close panel ${number}`} title="Close panel">
            <span aria-hidden="true">✕</span>
          </button>
        </span>
      </div>
      <div className="ws-panel-body">
        <PanelContext.Provider value={panelInfo}>
          <LinkChannelProvider channel={node.channel}>
            {!node.workspace ? (
              <p className="muted ws-panel-empty">Choose a workspace for panel {number} above, or type a function in the command line (for example MON).</p>
            ) : !def ? (
              <div className="callout ws-panel-empty" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
                The <span className="mono">{node.workspace}</span> workspace is not installed in this build. Choose another workspace for this panel.
              </div>
            ) : (
              <WorkspaceHost workspace={def} route={route} navigate={navigate} onReady={noop} />
            )}
          </LinkChannelProvider>
        </PanelContext.Provider>
      </div>
    </section>
  );
}

interface SplitterProps {
  split: SplitNode;
  index: number;
  locked: boolean;
  onResize: (splitId: string, index: number, delta: number) => void;
}

function Splitter({ split, index, locked, onResize }: SplitterProps) {
  const ref = useRef<HTMLDivElement>(null);
  const last = useRef<number | null>(null);
  const horizontal = split.dir === 'row';
  const a = split.sizes[index] ?? 0;
  const b = split.sizes[index + 1] ?? 0;
  const onKeyDown = (e: ReactKeyboardEvent) => {
    const step = e.shiftKey ? 10 : 2;
    const back = horizontal ? 'ArrowLeft' : 'ArrowUp';
    const fwd = horizontal ? 'ArrowRight' : 'ArrowDown';
    if (e.key === back || e.key === fwd) {
      e.preventDefault();
      onResize(split.id, index, e.key === fwd ? step : -step);
    } else if (e.key === 'Home' || e.key === 'End') {
      e.preventDefault();
      onResize(split.id, index, e.key === 'End' ? b : -a);
    }
  };
  const onPointerDown = (e: ReactPointerEvent) => {
    if (locked || e.button !== 0) return;
    e.preventDefault();
    ref.current?.setPointerCapture(e.pointerId);
    last.current = horizontal ? e.clientX : e.clientY;
  };
  const onPointerMove = (e: ReactPointerEvent) => {
    if (last.current === null) return;
    const box = ref.current?.parentElement?.getBoundingClientRect();
    const size = box ? (horizontal ? box.width : box.height) : 0;
    if (!size) return;
    const pos = horizontal ? e.clientX : e.clientY;
    const delta = ((pos - last.current) / size) * 100;
    if (Math.abs(delta) < 0.2) return;
    last.current = pos;
    onResize(split.id, index, delta);
  };
  const end = () => {
    last.current = null;
  };
  return (
    <div
      ref={ref}
      className={`ws-splitter ${horizontal ? 'is-v' : 'is-h'}`}
      role="separator"
      aria-orientation={horizontal ? 'vertical' : 'horizontal'}
      aria-label={horizontal ? 'Resize the panels left and right' : 'Resize the panels above and below'}
      aria-valuenow={Math.round(a)}
      aria-valuemin={10}
      aria-valuemax={Math.round(a + b - 10)}
      aria-disabled={locked || undefined}
      tabIndex={locked ? -1 : 0}
      onKeyDown={locked ? undefined : onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
    />
  );
}

interface TreeProps {
  node: LayoutNode;
  doc: LayoutDoc;
  numbers: Map<string, number>;
  panelCount: number;
  handlers: Omit<PanelProps, 'node' | 'number' | 'active' | 'locked' | 'canClose'> & { onResize: SplitterProps['onResize'] };
}

function Tree({ node, doc, numbers, panelCount, handlers }: TreeProps) {
  if (node.kind === 'panel') {
    return (
      <Panel
        node={node}
        number={numbers.get(node.id) ?? 0}
        active={doc.active === node.id}
        locked={doc.locked}
        canClose={panelCount > 1}
        onActivate={handlers.onActivate}
        onChange={handlers.onChange}
        onNavigate={handlers.onNavigate}
        onSplit={handlers.onSplit}
        onClose={handlers.onClose}
      />
    );
  }
  return (
    <div className={`ws-split is-${node.dir}`}>
      {node.children.flatMap((c, i) => {
        const cell = (
          <Cell key={c.id} grow={node.sizes[i] ?? 0}>
            <Tree node={c} doc={doc} numbers={numbers} panelCount={panelCount} handlers={handlers} />
          </Cell>
        );
        if (i === node.children.length - 1) return [cell];
        return [cell, <Splitter key={`${node.id}-div-${i}`} split={node} index={i} locked={doc.locked} onResize={handlers.onResize} />];
      })}
    </div>
  );
}

function Cell({ grow, children }: { grow: number; children: ReactNode }) {
  return (
    <div className="ws-cell" style={{ flex: `${grow} 1 0` }}>
      {children}
    </div>
  );
}

function formatSaved(ms: number): string {
  const d = new Date(ms);
  return `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 16)} UTC`;
}

export function Workstation() {
  const [initial] = useState(() => loadStore(storage(), defaultTemplate));
  const [store, setStore] = useState<LayoutStore>(initial.store);
  const [notice, setNotice] = useState<string | null>(initial.recovered);
  const [name, setName] = useState(initial.store.draft.name);
  const doc = store.draft;
  const panels = panelsOf(doc.root);
  const numbers = useMemo(() => new Map(panels.map((p, i) => [p.id, i + 1])), [panels]);

  // Autosave the draft on every change; a storage failure is shown, never thrown.
  const [storageError, setStorageError] = useState<string | null>(null);
  useEffect(() => {
    setStorageError(persistStore(storage(), store));
  }, [store]);

  const edit = useCallback((fn: (d: LayoutDoc) => LayoutDoc) => setStore((s) => setDraft(s, fn(s.draft))), []);
  const handlers = useMemo(
    () => ({
      onActivate: (id: string) => edit((d) => setActive(d, id)),
      onChange: (id: string, patch: Partial<Pick<PanelNode, 'workspace' | 'rest' | 'channel'>>) => edit((d) => updatePanel(d, id, patch)),
      onNavigate: (id: string, workspace: string, rest: readonly string[]) => edit((d) => navigatePanel(d, id, workspace, rest)),
      onSplit: (id: string, dir: 'row' | 'column') => edit((d) => splitPanel(d, id, dir)),
      onClose: (id: string) => edit((d) => closePanel(d, id)),
      onResize: (splitId: string, index: number, delta: number) => edit((d) => resizeSplit(d, splitId, index, delta)),
    }),
    [edit],
  );

  // The command line opens functions in the active panel and publishes securities on its channel.
  const active = panels.find((p) => p.id === doc.active) ?? panels[0];
  const activeNumber = active ? (numbers.get(active.id) ?? 1) : 1;
  useEffect(() => {
    if (!active) return undefined;
    return pushCommandTarget({
      channel: active.channel,
      label: `panel ${activeNumber}`,
      open: (workspace, rest) => {
        if (workspace === 'workstation') return null;
        if (doc.locked && workspace !== active.workspace) return `The layout is locked: unlock it to change what panel ${activeNumber} shows.`;
        if (!hasWorkspace(workspace)) return `The ${workspace} workspace is not installed in this build.`;
        setStore((s) => setDraft(s, navigatePanel(s.draft, active.id, workspace, rest)));
        return null;
      },
    });
  }, [active, activeNumber, doc.locked]);

  const saved = store.layouts[name.trim()];
  const savedNames = Object.keys(store.layouts).sort();
  const save = () => {
    const n = name.trim().slice(0, 60);
    if (!n) {
      setNotice('Give the layout a name before saving it.');
      return;
    }
    setStore((s) => saveLayout(s, n, Date.now()));
    setNotice(`Saved "${n}" as version ${(store.layouts[n]?.rev ?? 0) + 1}.`);
  };
  const open = (n: string, rev?: number) => {
    setStore((s) => openLayout(s, n, rev));
    setName(n);
    setNotice(rev === undefined ? `Opened "${n}".` : `Restored version ${rev} of "${n}" (save to keep it as the newest version).`);
  };
  const applyTemplate = (id: string) => {
    const t = TEMPLATES.find((x) => x.id === id);
    if (!t || doc.locked) return;
    setStore((s) => setDraft(s, t.build()));
    setName(t.name);
    setNotice(`Template "${t.name}" applied. Workspaces that are not installed show a chooser.`);
  };

  return (
    <div className="workstation">
      <div className="ws-toolbar">
        <h1 id="ws-title" tabIndex={-1} className="ws-toolbar-title">
          Workstation
        </h1>
        <label className="ws-field">
          <span>Layout</span>
          <input value={name} maxLength={60} onChange={(e) => setName(e.target.value)} aria-label="Layout name" />
        </label>
        <button type="button" className="btn small" onClick={save}>
          Save
        </button>
        <label className="ws-field">
          <span>Open</span>
          <select aria-label="Open a saved layout" value="" onChange={(e) => e.target.value && open(e.target.value)} disabled={!savedNames.length}>
            <option value="">{savedNames.length ? 'Saved layouts…' : 'None saved yet'}</option>
            {savedNames.map((n) => (
              <option key={n} value={n}>
                {n} (v{store.layouts[n]!.rev})
              </option>
            ))}
          </select>
        </label>
        <label className="ws-field">
          <span>Versions</span>
          <select
            aria-label="Restore a saved version of this layout"
            value=""
            disabled={!saved}
            onChange={(e) => e.target.value && open(name.trim(), Number(e.target.value))}
          >
            <option value="">{saved ? `v${saved.rev}, saved ${formatSaved(saved.savedAtMs)}` : 'Not saved yet'}</option>
            {saved
              ? [saved, ...saved.history].map((v) => (
                  <option key={v.rev} value={v.rev}>
                    v{v.rev} ({formatSaved(v.savedAtMs)})
                  </option>
                ))
              : null}
          </select>
        </label>
        <label className="ws-field">
          <span>Template</span>
          <select aria-label="Apply a layout template" value="" disabled={doc.locked} onChange={(e) => e.target.value && applyTemplate(e.target.value)}>
            <option value="">Templates…</option>
            {TEMPLATES.map((t) => (
              <option key={t.id} value={t.id} title={t.description}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className={`btn small${doc.locked ? ' is-on' : ''}`} aria-pressed={doc.locked} onClick={() => edit((d) => setLocked(d, !d.locked))}>
          {doc.locked ? 'Locked' : 'Lock layout'}
        </button>
        <span className="ws-toolbar-note muted">
          {panels.length} panel{panels.length === 1 ? '' : 's'} · commands go to panel {activeNumber}
        </span>
      </div>
      {notice || storageError ? (
        <div className="callout ws-notice" role="status" style={{ ['--tone' as string]: storageError || initial.recovered === notice ? 'var(--warn)' : 'var(--info)' }}>
          <span>{storageError ?? notice}</span>
          <button
            type="button"
            className="btn small"
            onClick={() => {
              setNotice(null);
              setStorageError(null);
            }}
          >
            Dismiss
          </button>
        </div>
      ) : null}
      <div className="ws-grid">
        <Tree node={doc.root} doc={doc} numbers={numbers} panelCount={panels.length} handlers={handlers} />
      </div>
    </div>
  );
}

export default Workstation;
