/**
 * Audit > Event log: the hash-chained event log over GET /api/events, filtered by type, session or
 * order, paged by seq (infinite scroll forward, "load earlier" backward), following the live tail.
 *
 * The log is append-only and addressed by seq, so a coalesced notification (a stream "gap") only
 * appends: fetching after the last loaded seq cannot miss an event. A reconnect or manual refresh
 * refetches the whole window; a lower seq (reset: e.g. a restored database) restarts the view.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { describeError } from '../../api/client';
import { useQuery, useRuntime, useStream } from '../../state/react';
import { Badge, StatusPill } from '../../ui/primitives';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { Segmented, TabPanel } from '../operations/parts';
import { EventLogTable } from './EventLogTable';
import { EventLog, eventsRoute, type AuditRoute, type EventFilters } from './eventLog';

const FILTER_LABEL: Record<keyof EventFilters, string> = { type: 'Type', session: 'Session', order: 'Order' };

export function EventsTab({
  bar,
  route,
  navigate,
  verifyAction,
}: {
  bar: ReactNode;
  route: AuditRoute;
  navigate: (workspace: string, ...rest: string[]) => void;
  verifyAction: ReactNode;
}) {
  const { client } = useRuntime();
  const stream = useStream();
  const log = useMemo(
    () => new EventLog((q) => client.get('/api/events', q), { pageSize: 200, backWindow: 500, maxRows: 2000, latestMinRows: 100 }),
    [client],
  );
  const snap = useSyncExternalStore(log.subscribe, log.getSnapshot);
  const { filters, after } = route;
  const filtered = !!(filters.type || filters.session || filters.order);
  const startKey = JSON.stringify([filters.type ?? '', filters.session ?? '', filters.order ?? '', after]);
  const startedKey = useRef<string | null>(null);
  const latestRef = useRef(stream.seq);
  latestRef.current = stream.seq;
  const seqReady = after !== null || stream.seq !== null;

  const start = () => {
    startedKey.current = startKey;
    if (after === null) void log.startLatest(filters, latestRef.current ?? 0);
    else void log.startAt(filters, after);
  };

  // (Re)start when the route's filters or start position change, once the latest seq is known.
  useEffect(() => {
    if (startedKey.current === startKey || !seqReady) return;
    start();
  }, [startKey, seqReady]);

  // React to the state stream. Counters (not only lastReason) so a batched render cannot hide a reset.
  const seen = useRef({ version: stream.version, resets: stream.resets, reconnects: stream.reconnects });
  useEffect(() => {
    const prev = seen.current;
    if (stream.version === prev.version) return;
    seen.current = { version: stream.version, resets: stream.resets, reconnects: stream.reconnects };
    if (!startedKey.current) return;
    if (stream.resets !== prev.resets) {
      startedKey.current = null;
      start();
    } else if (stream.reconnects !== prev.reconnects || stream.lastReason === 'manual') {
      void log.refresh();
    } else {
      log.onSeq(stream.seq);
    }
  }, [stream.version]);

  // Types seen in this view, for the filter suggestions.
  const typesSeen = useRef(new Set<string>());
  for (const r of snap.rows) typesSeen.current.add(r.type);

  // Expanded rows.
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());
  const toggle = (seq: number) =>
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });

  // ---------------------------------------------------------------- scrolling
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const sentinelVisible = useRef(false);
  const nearBottom = useRef(true);
  const firstSeqRef = useRef<number | null>(null);
  const heightRef = useRef(0);
  const viewRef = useRef(snap.view);

  useEffect(() => {
    const root = scrollRef.current;
    const target = sentinelRef.current;
    if (!root || !target || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(
      (entries) => {
        sentinelVisible.current = entries.some((e) => e.isIntersecting);
        const s = log.getSnapshot();
        if (sentinelVisible.current && s.started && !s.atTail && !s.loadingForward) void log.loadForward();
      },
      { root, rootMargin: '0px 0px 200px 0px' },
    );
    io.observe(target);
    return () => io.disconnect();
  }, [log]);

  // Keep loading while the sentinel stays in view (tall screens), and keep the tail pinned.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const first = snap.rows[0]?.seq ?? null;
    if (viewRef.current !== snap.view) {
      viewRef.current = snap.view;
      // The observer reports again for the new content; a stale "visible" would load a page unasked.
      sentinelVisible.current = false;
      // A new view: latest starts at the bottom, a seq start at the top.
      el.scrollTop = after === null ? el.scrollHeight : 0;
    } else if (first !== null && firstSeqRef.current !== null && first < firstSeqRef.current) {
      // Rows were prepended ("load earlier"): keep the visible rows where they were.
      el.scrollTop += el.scrollHeight - heightRef.current;
    } else if (nearBottom.current && snap.atTail) {
      el.scrollTop = el.scrollHeight;
    }
    firstSeqRef.current = first;
    heightRef.current = el.scrollHeight;
    if (sentinelVisible.current && snap.started && !snap.atTail && !snap.loadingForward) void log.loadForward();
  }, [snap, after, log]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    nearBottom.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
  };

  // ---------------------------------------------------------------- navigation
  const go = (f: EventFilters, a: number | null) => navigate('audit', ...eventsRoute(f, a));
  const [draft, setDraft] = useState<EventFilters>(filters);
  useEffect(() => setDraft(filters), [startKey]);
  const [seqInput, setSeqInput] = useState('');
  const [wantSuggestions, setWantSuggestions] = useState(false);
  const sessions = useQuery(wantSuggestions ? '/api/sessions' : null);
  const orders = useQuery(wantSuggestions ? '/api/orders' : null, { limit: 500 });
  const mode: 'latest' | 'start' | 'seq' = after === null ? 'latest' : after === 0 ? 'start' : 'seq';
  const newer = stream.seq !== null && !snap.atTail && !filtered ? Math.max(0, stream.seq - snap.hi) : null;

  return (
    <WorkspaceFrame
      title="Audit"
      description="The hash-chained event log, the double-entry journal and a full verification of both. Read-only."
      actions={
        <>
          <button type="button" className="btn small" onClick={() => void log.refresh()} disabled={!snap.started || snap.refreshing}>
            Reload window
          </button>
          {verifyAction}
        </>
      }
    >
      {bar}
      <TabPanel idBase="audit" current="events">
        <form
          className="ops-toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            go(draft, after);
          }}
          onFocus={() => setWantSuggestions(true)}
          aria-label="Event filters"
        >
          {(['type', 'session', 'order'] as const).map((k) => (
            <div className="field" key={k}>
              <label htmlFor={`ev-f-${k}`}>{FILTER_LABEL[k]}</label>
              <input
                id={`ev-f-${k}`}
                className="input mono"
                list={`ev-f-${k}-list`}
                value={draft[k] ?? ''}
                placeholder={k === 'type' ? 'e.g. StopLatched' : `any ${k}`}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
                style={{ width: k === 'type' ? 220 : 240 }}
              />
              <datalist id={`ev-f-${k}-list`}>
                {(k === 'type'
                  ? [...typesSeen.current].sort()
                  : k === 'session'
                    ? (sessions.data ?? []).map((s) => s.session_id)
                    : (orders.data ?? []).map((o) => o.order_id)
                ).map((v) => (
                  <option key={v} value={v} />
                ))}
              </datalist>
            </div>
          ))}
          <button type="submit" className="btn primary">
            Apply filters
          </button>
          {filtered ? (
            <button type="button" className="btn" onClick={() => go({}, after)}>
              Clear filters
            </button>
          ) : null}
          <span className="spacer" />
          <div className="field">
            <span className="ops-hint" id="ev-start-label">
              Start from
            </span>
            <Segmented
              label="Start from"
              value={mode}
              onChange={(m) => {
                if (m === 'latest') go(filters, null);
                else if (m === 'start') go(filters, 0);
                else document.getElementById('ev-seq')?.focus();
              }}
              options={[
                { value: 'latest', label: 'Latest' },
                { value: 'start', label: 'Beginning' },
                { value: 'seq', label: after !== null && after > 0 ? `Seq ${after + 1}` : 'Seq…' },
              ]}
            />
          </div>
          <div className="field">
            <label htmlFor="ev-seq">Jump to seq</label>
            <span style={{ display: 'inline-flex', gap: 6 }}>
              <input
                id="ev-seq"
                className="input mono"
                inputMode="numeric"
                value={seqInput}
                placeholder="e.g. 120"
                style={{ width: 100 }}
                onChange={(e) => setSeqInput(e.target.value.replace(/[^\d]/g, ''))}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && seqInput) {
                    e.preventDefault();
                    go(filters, Math.max(0, Number(seqInput) - 1));
                  }
                }}
              />
              <button type="button" className="btn" disabled={!seqInput} onClick={() => go(filters, Math.max(0, Number(seqInput) - 1))}>
                Go
              </button>
            </span>
          </div>
        </form>

        {filtered ? (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', margin: '0 0 8px' }}>
            <span className="ops-hint">Filtered by</span>
            {(['type', 'session', 'order'] as const)
              .filter((k) => filters[k])
              .map((k) => (
                <span key={k} style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
                  <Badge tone="info">
                    {FILTER_LABEL[k]}: {filters[k]}
                  </Badge>
                  <button
                    type="button"
                    className="btn small ghost"
                    aria-label={`Remove ${FILTER_LABEL[k]} filter`}
                    onClick={() => go({ ...filters, [k]: undefined }, after)}
                  >
                    ×
                  </button>
                </span>
              ))}
          </div>
        ) : null}

        {snap.error ? (
          <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', marginBottom: 8 }}>
            <strong>Events could not be loaded.</strong> {describeError(snap.error)}{' '}
            <button type="button" className="btn small" onClick={() => void log.refresh()}>
              Try again
            </button>
          </div>
        ) : null}
        {snap.changedUnderView ? (
          <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', marginBottom: 8 }}>
            <strong>An event already on screen came back with a different chain hash.</strong> The log changed under this view (for example a
            restored database). Run Verify.
          </div>
        ) : null}

        <div className="ops-hint" aria-live="polite" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', margin: '0 0 6px' }}>
          <span>
            {snap.started ? (
              <>
                Window seq {snap.lo + 1}–{snap.hi} · {snap.rows.length.toLocaleString('en-US')} event{snap.rows.length === 1 ? '' : 's'} loaded
                {snap.trimmed ? ` (window capped at ${log.maxRows.toLocaleString('en-US')}; ${snap.trimmed.toLocaleString('en-US')} scrolled out)` : ''}
              </>
            ) : seqReady ? (
              'Loading…'
            ) : (
              'Waiting for the state stream to report the latest seq…'
            )}
          </span>
          {snap.started ? (
            snap.atTail ? (
              <StatusPill label="following live" tone="ok" compact detail="New events are appended as the core records them" />
            ) : (
              <StatusPill
                label={newer !== null ? `${newer.toLocaleString('en-US')} newer not loaded` : 'more below'}
                tone="info"
                compact
                detail="Scroll down to load them"
              />
            )
          ) : null}
        </div>

        <section className="panel">
          <div className="panel-body flush">
            {snap.lo > 0 && snap.started ? (
              <div className="audit-sentinel" style={{ borderBottom: '1px solid var(--border)' }}>
                <button type="button" className="btn small" disabled={snap.loadingBackward} onClick={() => void log.loadBackward()}>
                  {snap.loadingBackward ? 'Loading earlier…' : `Load earlier events (before seq ${snap.lo + 1})`}
                </button>
              </div>
            ) : snap.started ? (
              <div className="audit-sentinel" style={{ borderBottom: '1px solid var(--border)' }}>
                Start of the event log.
              </div>
            ) : null}
            <div ref={scrollRef} className="table-wrap audit-log" style={{ maxHeight: 'min(68vh, 760px)' }} onScroll={onScroll}>
              <EventLogTable
                rows={snap.rows}
                expanded={expanded}
                onToggle={toggle}
                onFilter={(f) => go({ ...filters, ...f }, after)}
                caption={`Event log, seq ${snap.lo + 1} to ${snap.hi}`}
              />
              <div ref={sentinelRef} className="audit-sentinel">
                {!snap.started ? null : snap.loadingForward ? (
                  'Loading…'
                ) : snap.atTail ? (
                  'Latest event loaded; new events appear here.'
                ) : (
                  <button type="button" className="btn small" onClick={() => void log.loadForward()}>
                    Load more
                  </button>
                )}
              </div>
            </div>
          </div>
        </section>
        <p className="ops-hint">
          Focus a row and press Enter (or click +) to see its info JSON, identifiers, times and hashes. Links: <code>#/audit/events/type=…/session=…/order=…/after=…</code>
        </p>
      </TabPanel>
    </WorkspaceFrame>
  );
}
