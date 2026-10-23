/**
 * Monitor (CAP-MKT-08 watchlists, CAP-MKT-03 time & sales, CAP-MKT-06 depth): live quotes from the broker-agnostic
 * market-data bus (GET /api/marketdata/stream; ADR-0018). Every row shows its source and data type; SYNTHETIC data
 * (the default when no real source is configured) is always labelled. Panels follow useLinkedSecurity().
 *
 * Watchlists are kept in this browser (localStorage); they hold security keys, never broker ids.
 */
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useQuery } from '../../state/react';
import { useStreamHealth } from '../../state/marketdata';
import { useLinkedSecurity } from '../../ui/link';
import { Badge } from '../../ui/primitives';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import type { WorkspaceProps } from '../types';
import { addKey, defaultState, loadState, moveKey, removeKey, saveState, type MonitorState, type Watchlist } from './model';
import { DepthPanel, TimeAndSales, WatchlistGrid } from './panels';
import './monitor.css';

function useMonitorState(): [MonitorState, (s: MonitorState) => void] {
  const instruments = useQuery('/api/instruments');
  const [state, setState] = useState<MonitorState>(() => loadState() ?? defaultState());
  const demoKeys = useMemo(
    () => (instruments.data ?? []).filter((i) => i.symbol && i.venue_mic).map((i) => `${i.symbol}.${i.venue_mic}`),
    [instruments.data],
  );
  useEffect(() => {
    // Add the core's demo instruments once, as their own list, when the owner has not stored lists yet.
    if (!demoKeys.length || loadState() || state.lists.some((l) => l.id === 'demo')) return;
    setState(defaultState(demoKeys));
  }, [demoKeys, state.lists]);
  const update = (s: MonitorState) => {
    setState(s);
    saveState(s);
  };
  return [state, update];
}

export function StreamHealthLine() {
  const h = useStreamHealth();
  const tone = h.state === 'live' ? 'ok' : h.state === 'idle' ? 'neutral' : 'warn';
  return (
    <div className="mon-health" aria-live="polite">
      <Badge tone={tone}>{`stream ${h.state}`}</Badge>
      <span>{h.keys} keys</span>
      {Object.entries(h.sources).map(([src, types]) => (
        <Badge key={src} tone={types.includes('SYNTHETIC') ? 'info' : types.includes('REALTIME') ? 'ok' : 'warn'}>
          {types.includes('SYNTHETIC') && src === 'SYNTHETIC' ? 'SYNTHETIC data: not market data' : `${src}: ${types.join(', ')}`}
        </Badge>
      ))}
      <span title="Browser receipt minus bus publish time over recent quotes and trades">
        latency p50 {h.receiptP50Ms === null ? '—' : `${h.receiptP50Ms.toFixed(1)} ms`} / p99{' '}
        {h.receiptP99Ms === null ? '—' : `${h.receiptP99Ms.toFixed(1)} ms`}
      </span>
      {h.gaps ? <span>{h.gaps} gaps, {h.resyncs} resyncs</span> : null}
      {h.overflows ? <Badge tone="warn">{`${h.overflows} trade overflow notices`}</Badge> : null}
    </div>
  );
}

export function MonitorWorkspace(_props: WorkspaceProps) {
  const [state, setState] = useMonitorState();
  const linked = useLinkedSecurity();
  const [input, setInput] = useState('');
  const [error, setError] = useState<string | null>(null);
  const list = state.lists.find((l) => l.id === state.active) ?? (state.lists[0] as Watchlist);
  const focus = linked ?? list.keys[0] ?? null;

  const replaceList = (next: Watchlist) =>
    setState({ ...state, lists: state.lists.map((l) => (l.id === next.id ? next : l)) });

  const onAdd = (e: FormEvent) => {
    e.preventDefault();
    const r = addKey(list, input);
    setError(r.error);
    if (!r.error) {
      replaceList(r.list);
      setInput('');
    }
  };

  const newList = () => {
    const id = `list-${Date.now().toString(36)}`;
    setState({ ...state, lists: [...state.lists, { id, name: `List ${state.lists.length + 1}`, keys: [] }], active: id });
  };

  const deleteList = () => {
    if (state.lists.length <= 1) return;
    const lists = state.lists.filter((l) => l.id !== list.id);
    setState({ ...state, lists, active: (lists[0] as Watchlist).id });
  };

  return (
    <WorkspaceFrame
      title="Monitor"
      description="Live watchlists, time & sales and depth from the configured market-data sources. Every row shows its source and data type."
    >
      <StreamHealthLine />
      <div className="mon-toolbar">
        <div className="mon-lists" role="tablist" aria-label="Watchlists">
          {state.lists.map((l) => (
            <button
              key={l.id}
              type="button"
              role="tab"
              aria-selected={l.id === list.id}
              className={l.id === list.id ? 'mon-tab mon-tab-on' : 'mon-tab'}
              onClick={() => setState({ ...state, active: l.id })}
            >
              {l.name} <span className="mon-muted">{l.keys.length}</span>
            </button>
          ))}
          <button type="button" className="mon-tab" onClick={newList}>
            + New list
          </button>
        </div>
        <label className="mon-rename">
          <span className="mon-sr">List name</span>
          <input
            value={list.name}
            maxLength={60}
            onChange={(e) => replaceList({ ...list, name: e.target.value || 'Untitled' })}
            aria-label="List name"
          />
        </label>
        <button type="button" onClick={deleteList} disabled={state.lists.length <= 1}>
          Delete list
        </button>
        <form className="mon-add" onSubmit={onAdd}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Add security, e.g. AAPL.XNAS"
            aria-label="Add security key"
            spellCheck={false}
          />
          <button type="submit">Add</button>
        </form>
      </div>
      {error ? (
        <p className="mon-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="mon-layout">
        <div className="mon-main">
          <WatchlistGrid
            list={list}
            selected={focus}
            onRemove={(k) => replaceList(removeKey(list, k))}
            onMove={(k, d) => replaceList(moveKey(list, k, d))}
          />
        </div>
        {focus ? (
          <div className="mon-side">
            <TimeAndSales securityKey={focus} />
            <DepthPanel securityKey={focus} />
          </div>
        ) : null}
      </div>
    </WorkspaceFrame>
  );
}
