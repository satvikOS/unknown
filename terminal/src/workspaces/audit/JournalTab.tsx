/**
 * Audit > Journal: double-entry journal entries with their postings (GET /api/ledger/entries), newest
 * first, optionally only entries touching one owner. Each entry's per-currency balance is re-added
 * exactly in the browser (BigInt) as a visible check; the ledger's own verification is server-side.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import type { JournalEntry } from '../../api/types';
import { describeError } from '../../api/client';
import { useQuery, useRuntime, useStream } from '../../state/react';
import { Badge, Money, ShortId, StatusPill } from '../../ui/primitives';
import { WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import { TabPanel } from '../operations/parts';
import { EMPTY_JOURNAL, entryBalance, mergeJournalHead, mergeJournalPage, ownerKind, ownerSuggestions, type JournalPageState } from './journal';

const PAGE = 100;
const MAX_RELOAD = 2000;

function OwnerCell({ owner, highlight }: { owner: string; highlight: boolean }) {
  const k = ownerKind(owner);
  const tone = k.kind === 'session' || k.kind === 'unallocated' || k.kind === 'buffer' ? 'info' : k.kind === 'protected' ? 'ok' : k.kind === 'exception' ? 'warn' : 'neutral';
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center', fontWeight: highlight ? 650 : undefined }}>
      <Badge tone={tone}>{k.kind}</Badge>
      {k.ref ? (
        k.kind === 'session' || k.kind === 'protected' ? (
          <WorkspaceLink to="sessions" rest={[k.ref]}>
            <ShortId value={k.ref} head={14} />
          </WorkspaceLink>
        ) : k.kind === 'exception' ? (
          <WorkspaceLink to="operations" rest={['exceptions', k.ref]}>
            <ShortId value={k.ref} head={14} />
          </WorkspaceLink>
        ) : (
          <ShortId value={k.ref} head={14} />
        )
      ) : k.kind === 'other' ? (
        <span className="cmd-id">{owner}</span>
      ) : null}
    </span>
  );
}

function EntryRows({ e, owner }: { e: JournalEntry; owner: string | null }) {
  const bal = entryBalance(e);
  return (
    <tbody className="journal-entry">
      <tr>
        <td className="entry-head right num">
          <WorkspaceLink to="audit" rest={['events', `after=${Math.max(0, e.event_seq - 1)}`]}>
            {e.event_seq}
          </WorkspaceLink>
        </td>
        <td className="entry-head">
          <ShortId value={e.entry_id} head={18} />
        </td>
        <td className="entry-head">
          <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
            <code className="mono">{e.kind}</code>
            <StatusPill
              label={bal.balanced ? 'balanced' : 'UNBALANCED'}
              tone={bal.balanced ? 'ok' : 'bad'}
              detail={`Sum of postings per currency: ${bal.totals.map(([c, t]) => `${t} ${c}`).join(', ')}`}
              compact
            />
          </span>
        </td>
        <td className="entry-head wrap" colSpan={3}>
          {e.memo}
          {e.reverses ? (
            <span className="muted">
              {' '}
              · reverses <ShortId value={e.reverses} head={18} />
            </span>
          ) : null}
        </td>
      </tr>
      {e.lines.map((l) => (
        <tr key={l.line}>
          <td />
          <td className="indent num muted">line {l.line}</td>
          <td>
            <code className="mono">{l.account_code}</code>
          </td>
          <td>
            <OwnerCell owner={l.owner} highlight={owner !== null && l.owner === owner} />
          </td>
          <td className="right num">
            <Money amount={l.amount} currency={l.currency} />
          </td>
          <td>
            {l.instrument_id ? (
              <WorkspaceLink to="instruments" rest={[l.instrument_id]}>
                <span className="mono">{l.instrument_id}</span>
              </WorkspaceLink>
            ) : (
              <span className="muted">—</span>
            )}
          </td>
        </tr>
      ))}
    </tbody>
  );
}

export function JournalTab({
  bar,
  owner,
  navigate,
  actions,
}: {
  bar: ReactNode;
  owner: string | null;
  navigate: (workspace: string, ...rest: string[]) => void;
  actions: ReactNode;
}) {
  const { client } = useRuntime();
  const stream = useStream();
  const [state, setState] = useState<JournalPageState>(EMPTY_JOURNAL);
  const [loading, setLoading] = useState<'reload' | 'more' | 'head' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const gen = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;
  const busy = useRef(false);
  /** A new seq arrived while another load was running: fetch the head once it finishes. */
  const pendingHead = useRef(false);

  const fetchPage = useCallback(
    (before: number | null, limit: number) =>
      client.get('/api/ledger/entries', {
        limit,
        ...(owner ? { owner } : {}),
        ...(before !== null ? { before } : {}),
      }),
    [client, owner],
  );

  const reload = useCallback(
    async (limit: number) => {
      const g = ++gen.current;
      busy.current = true;
      setLoading('reload');
      setError(null);
      try {
        const page = await fetchPage(null, limit);
        if (g !== gen.current) return;
        setState(mergeJournalPage(EMPTY_JOURNAL, page, limit, null));
        setLoaded(true);
      } catch (e) {
        if (g === gen.current) setError(e);
      } finally {
        if (g === gen.current) {
          busy.current = false;
          setLoading(null);
        }
      }
    },
    [fetchPage],
  );

  const loadMore = useCallback(async () => {
    const cur = stateRef.current;
    if (busy.current || cur.cursor === null || !loaded) return;
    const g = gen.current;
    busy.current = true;
    setLoading('more');
    try {
      const page = await fetchPage(cur.cursor, PAGE);
      if (g !== gen.current) return;
      setState((s) => mergeJournalPage(s, page, PAGE, cur.cursor));
    } catch (e) {
      if (g === gen.current) setError(e);
    } finally {
      if (g === gen.current) {
        busy.current = false;
        setLoading(null);
      }
    }
  }, [fetchPage, loaded]);

  const loadHead = useCallback(async () => {
    if (busy.current) return;
    const g = gen.current;
    busy.current = true;
    setLoading('head');
    try {
      const page = await fetchPage(null, PAGE);
      if (g !== gen.current) return;
      const merged = mergeJournalHead(stateRef.current, page, PAGE);
      busy.current = false;
      if (merged === null) void reload(PAGE);
      else setState(merged);
    } catch (e) {
      if (g === gen.current) setError(e);
    } finally {
      if (g === gen.current) {
        busy.current = false;
        setLoading(null);
      }
    }
  }, [fetchPage, reload]);

  useEffect(() => {
    setState(EMPTY_JOURNAL);
    setLoaded(false);
    void reload(PAGE);
  }, [reload]);

  const seen = useRef({ version: stream.version, resets: stream.resets, reconnects: stream.reconnects });
  useEffect(() => {
    const prev = seen.current;
    if (stream.version === prev.version) return;
    seen.current = { version: stream.version, resets: stream.resets, reconnects: stream.reconnects };
    if (!loaded) return;
    if (stream.resets !== prev.resets || stream.reconnects !== prev.reconnects || stream.lastReason === 'manual') {
      pendingHead.current = false;
      void reload(Math.min(MAX_RELOAD, Math.max(PAGE, stateRef.current.entries.length)));
    } else if (busy.current) {
      pendingHead.current = true;
    } else {
      void loadHead();
    }
  }, [stream.version, loaded, reload, loadHead, stream.resets, stream.reconnects, stream.lastReason]);

  // Infinite scroll.
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const visible = useRef(false);
  useEffect(() => {
    const root = scrollRef.current;
    const target = sentinelRef.current;
    if (!root || !target || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(
      (entries) => {
        visible.current = entries.some((x) => x.isIntersecting);
        if (visible.current) void loadMore();
      },
      { root, rootMargin: '0px 0px 200px 0px' },
    );
    io.observe(target);
    return () => io.disconnect();
  }, [loadMore]);
  useLayoutEffect(() => {
    if (loading !== null) return;
    if (pendingHead.current) {
      pendingHead.current = false;
      void loadHead();
    } else if (visible.current && state.cursor !== null) {
      void loadMore();
    }
  }, [state, loading, loadMore, loadHead]);

  const [draft, setDraft] = useState(owner ?? '');
  useEffect(() => setDraft(owner ?? ''), [owner]);
  const [wantOwners, setWantOwners] = useState(false);
  const buckets = useQuery(wantOwners ? '/api/buckets' : null);
  const suggestions = ownerSuggestions(state.entries, (buckets.data ?? []).map((b) => b.owner));
  const unbalanced = state.entries.filter((e) => !entryBalance(e).balanced).length;

  return (
    <WorkspaceFrame
      title="Audit"
      description="The hash-chained event log, the double-entry journal and a full verification of both. Read-only."
      actions={actions}
    >
      {bar}
      <TabPanel idBase="audit" current="journal">
        <form
          className="ops-toolbar"
          aria-label="Journal filter"
          onFocus={() => setWantOwners(true)}
          onSubmit={(e) => {
            e.preventDefault();
            const v = draft.trim();
            navigate('audit', ...(v ? ['journal', `owner=${v}`] : ['journal']));
          }}
        >
          <div className="field">
            <label htmlFor="je-owner">Owner (entries with at least one posting for this owner)</label>
            <input
              id="je-owner"
              className="input mono"
              list="je-owner-list"
              value={draft}
              placeholder="any owner, e.g. unallocated or session:ses-…"
              autoComplete="off"
              spellCheck={false}
              style={{ width: 340 }}
              onChange={(e) => setDraft(e.target.value)}
            />
            <datalist id="je-owner-list">
              {suggestions.map((o) => (
                <option key={o} value={o} />
              ))}
            </datalist>
          </div>
          <button type="submit" className="btn primary">
            Apply
          </button>
          {owner ? (
            <button type="button" className="btn" onClick={() => navigate('audit', 'journal')}>
              All owners
            </button>
          ) : null}
          <span className="spacer" />
          <span className="ops-hint" aria-live="polite">
            {loaded ? `${state.entries.length.toLocaleString('en-US')} entr${state.entries.length === 1 ? 'y' : 'ies'} loaded` : 'Loading…'}
            {unbalanced ? (
              <strong style={{ color: 'var(--bad)' }}>
                {' '}
                · {unbalanced} unbalanced
              </strong>
            ) : loaded && state.entries.length ? (
              ' · every loaded entry balances per currency'
            ) : null}
          </span>
        </form>
        {error ? (
          <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', marginBottom: 8 }}>
            <strong>Journal entries could not be loaded.</strong> {describeError(error)}{' '}
            <button type="button" className="btn small" onClick={() => void reload(PAGE)}>
              Try again
            </button>
          </div>
        ) : null}
        {state.possiblySkipped ? (
          <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 8 }}>
            One event wrote more journal entries than a page holds; paging past it may have skipped some of its entries. Filter by owner to see
            them all.
          </div>
        ) : null}
        <section className="panel">
          <div className="panel-body flush">
            <div ref={scrollRef} className="table-wrap" style={{ maxHeight: 'min(70vh, 780px)' }}>
              <table className="data">
                <caption className="sr-only">
                  Journal entries{owner ? ` touching owner ${owner}` : ''}, newest first, each followed by its postings
                </caption>
                <thead>
                  <tr>
                    <th scope="col" className="right" aria-sort="descending">
                      <span className="th-inner">
                        Event seq <span aria-hidden="true">▼</span>
                      </span>
                    </th>
                    <th scope="col">
                      <span className="th-inner">Entry / line</span>
                    </th>
                    <th scope="col">
                      <span className="th-inner" title="Entry kind with its per-currency balance check, then each posting's account code">
                        Kind / account
                      </span>
                    </th>
                    <th scope="col">
                      <span className="th-inner">Owner</span>
                    </th>
                    <th scope="col" className="right">
                      <span className="th-inner" title="Signed posting amount: debit positive, credit negative">
                        Amount (debit +, credit −)
                      </span>
                    </th>
                    <th scope="col">
                      <span className="th-inner">Instrument</span>
                    </th>
                  </tr>
                </thead>
                {state.entries.map((e) => (
                  <EntryRows key={e.entry_id} e={e} owner={owner} />
                ))}
                {loaded && state.entries.length === 0 ? (
                  <tbody>
                    <tr>
                      <td colSpan={6} className="empty">
                        {owner ? `No journal entry touches owner ${owner}.` : 'No journal entries yet.'}
                      </td>
                    </tr>
                  </tbody>
                ) : null}
              </table>
              <div ref={sentinelRef} className="audit-sentinel">
                {!loaded ? null : loading === 'more' ? (
                  'Loading older entries…'
                ) : state.cursor === null ? (
                  'Oldest entry loaded.'
                ) : (
                  <button type="button" className="btn small" onClick={() => void loadMore()}>
                    Load older entries
                  </button>
                )}
              </div>
            </div>
          </div>
        </section>
        <p className="ops-hint">
          Amounts are exact decimal strings from the ledger (6 decimal places), shown with their currency. Owner badges: outside = not authorized
          for trading; unallocated and buffer = system capital; session = a session’s working capital; protected = completed proceeds; external = manual trades outside the system’s sessions; exception = amounts held against an open case.
        </p>
      </TabPanel>
    </WorkspaceFrame>
  );
}
