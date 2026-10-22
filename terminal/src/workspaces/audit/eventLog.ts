/**
 * Event-log window over GET /api/events (seq-ordered, `after` exclusive, `limit` <= 2000).
 *
 * Coverage invariant: `rows` holds EVERY event matching the filters with lo < seq <= hi, ascending.
 *   - forward  (infinite scroll / live tail): after=hi, limit=pageSize; hi := last seq returned.
 *     A short page means the tail was reached (`atTail`); new seqs are then fetched automatically.
 *   - backward ("load earlier"): after=max(0, lo-W), limit=W. That seq window holds at most W
 *     events, so the page is complete for it; rows with seq <= lo are kept and lo := lo-W.
 *   - refresh (stream gap / reconnect / manual): refetch (lo, hi] page by page and replace rows;
 *     a changed chain hash for a seq already shown is reported (`changedUnderView`).
 * The window is capped at `maxRows`: trimming drops rows from the far end and moves lo or hi so the
 * coverage statement stays true. Responses from a superseded generation are ignored.
 */
import type { EventRecord } from '../../api/types';

export interface EventFilters {
  type?: string;
  session?: string;
  order?: string;
}

export interface EventQuery extends EventFilters {
  after: number;
  limit: number;
}

export type EventFetcher = (q: EventQuery) => Promise<EventRecord[]>;

export interface EventLogSnapshot {
  rows: readonly EventRecord[];
  lo: number;
  hi: number;
  atTail: boolean;
  loadingForward: boolean;
  loadingBackward: boolean;
  refreshing: boolean;
  error: unknown;
  /** Bumps on every start/refresh; responses from an older generation are dropped. */
  generation: number;
  /** Bumps on every start only (a new view: the page resets its scroll position). */
  view: number;
  /** A refresh found a different chain hash for a seq that was on screen (e.g. a restored database). */
  changedUnderView: boolean;
  /** How many rows trimming has dropped from either end since the last start. */
  trimmed: number;
  started: boolean;
}

export interface EventLogOptions {
  pageSize?: number;
  backWindow?: number;
  maxRows?: number;
  /** Backward windows tried per "load earlier" before giving up on finding a match. */
  maxBackSteps?: number;
  /** "Latest" start keeps stepping back until this many rows are loaded (or seq 0 is reached). */
  latestMinRows?: number;
}

export const MAX_API_LIMIT = 2000;

type Listener = () => void;

const EMPTY: EventLogSnapshot = {
  rows: [],
  lo: 0,
  hi: 0,
  atTail: false,
  loadingForward: false,
  loadingBackward: false,
  refreshing: false,
  error: null,
  generation: 0,
  view: 0,
  changedUnderView: false,
  trimmed: 0,
  started: false,
};

export class EventLog {
  private snap: EventLogSnapshot = EMPTY;
  private filters: EventFilters = {};
  private readonly listeners = new Set<Listener>();
  private forwardAgain = false;
  readonly pageSize: number;
  readonly backWindow: number;
  readonly maxRows: number;
  readonly maxBackSteps: number;
  readonly latestMinRows: number;

  constructor(
    private readonly fetcher: EventFetcher,
    options: EventLogOptions = {},
  ) {
    this.pageSize = Math.min(MAX_API_LIMIT, options.pageSize ?? 200);
    this.backWindow = Math.min(MAX_API_LIMIT, options.backWindow ?? 500);
    this.maxRows = options.maxRows ?? 2000;
    this.maxBackSteps = options.maxBackSteps ?? 10;
    this.latestMinRows = options.latestMinRows ?? 100;
  }

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };

  getSnapshot = (): EventLogSnapshot => this.snap;

  private set(patch: Partial<EventLogSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    for (const l of [...this.listeners]) l();
  }

  get currentFilters(): EventFilters {
    return this.filters;
  }

  private query(after: number, limit: number): EventQuery {
    const q: EventQuery = { after, limit };
    if (this.filters.type) q.type = this.filters.type;
    if (this.filters.session) q.session = this.filters.session;
    if (this.filters.order) q.order = this.filters.order;
    return q;
  }

  /** Show events after `after` (exclusive), then follow forward. */
  async startAt(filters: EventFilters, after: number): Promise<void> {
    this.filters = { ...filters };
    const after0 = Math.max(0, Math.floor(after));
    this.set({ ...EMPTY, lo: after0, hi: after0, generation: this.snap.generation + 1, view: this.snap.view + 1, started: true });
    await this.loadForward();
  }

  /** Show the most recent matching events up to `latestSeq`, then follow the tail. */
  async startLatest(filters: EventFilters, latestSeq: number): Promise<void> {
    this.filters = { ...filters };
    const top = Math.max(0, Math.floor(latestSeq));
    this.set({ ...EMPTY, lo: top, hi: top, atTail: true, generation: this.snap.generation + 1, view: this.snap.view + 1, started: true });
    let steps = 0;
    while (this.snap.lo > 0 && this.snap.rows.length < this.latestMinRows && steps < this.maxBackSteps) {
      const before = this.snap.generation;
      await this.loadBackward(1);
      if (this.snap.generation !== before || this.snap.error) return;
      steps += 1;
    }
    // Anything recorded while stepping back.
    await this.loadForward();
  }

  async loadForward(): Promise<void> {
    if (!this.snap.started) return;
    if (this.snap.loadingForward || this.snap.refreshing) {
      this.forwardAgain = true;
      return;
    }
    const gen = this.snap.generation;
    this.set({ loadingForward: true, error: null });
    try {
      do {
        this.forwardAgain = false;
        const after = this.snap.hi;
        const page = await this.fetcher(this.query(after, this.pageSize));
        if (this.snap.generation !== gen) return;
        const fresh = page.filter((e) => e.seq > after).sort((a, b) => a.seq - b.seq);
        const hi = fresh.length ? fresh[fresh.length - 1]!.seq : after;
        const atTail = page.length < this.pageSize;
        this.set({ rows: [...this.snap.rows, ...fresh], hi, atTail });
        this.trimFront();
        // A request (scroll or new seq) that arrived during the fetch gets exactly one more page.
      } while (this.forwardAgain);
    } catch (error) {
      if (this.snap.generation === gen) this.set({ error });
    } finally {
      if (this.snap.generation === gen) this.set({ loadingForward: false });
    }
  }

  /** Step the lower bound back by up to `steps` windows, stopping at the first window with rows. */
  async loadBackward(steps = this.maxBackSteps): Promise<void> {
    if (!this.snap.started || this.snap.loadingBackward || this.snap.refreshing || this.snap.lo <= 0) return;
    const gen = this.snap.generation;
    this.set({ loadingBackward: true, error: null });
    try {
      for (let i = 0; i < steps && this.snap.lo > 0; i++) {
        const lo = this.snap.lo;
        const after = Math.max(0, lo - this.backWindow);
        const page = await this.fetcher(this.query(after, this.backWindow));
        if (this.snap.generation !== gen) return;
        const earlier = page.filter((e) => e.seq > after && e.seq <= lo).sort((a, b) => a.seq - b.seq);
        this.set({ rows: [...earlier, ...this.snap.rows], lo: after });
        this.trimBack();
        if (earlier.length) break;
      }
    } catch (error) {
      if (this.snap.generation === gen) this.set({ error });
    } finally {
      if (this.snap.generation === gen) this.set({ loadingBackward: false });
    }
  }

  /** The stream reported a new seq: follow it when the view is at the tail. */
  onSeq(seq: number | null): void {
    if (!this.snap.started || seq === null) return;
    if (this.snap.atTail && seq > this.snap.hi) void this.loadForward();
  }

  /** Refetch everything in the window (stream gap, reconnect or manual refresh). */
  async refresh(): Promise<void> {
    if (!this.snap.started) return;
    const gen = this.snap.generation + 1;
    const { lo, hi, rows: old, atTail: wasTail } = this.snap;
    const oldHash = new Map(old.map((e) => [e.seq, e.chain_hash]));
    this.set({ refreshing: true, generation: gen, error: null, loadingForward: false, loadingBackward: false });
    try {
      const rows: EventRecord[] = [];
      let after = lo;
      let atTail = false;
      for (let pages = 0; pages < Math.ceil(this.maxRows / this.pageSize) + 1; pages++) {
        const page = await this.fetcher(this.query(after, this.pageSize));
        if (this.snap.generation !== gen) return;
        const fresh = page.filter((e) => e.seq > after).sort((a, b) => a.seq - b.seq);
        rows.push(...fresh);
        if (page.length < this.pageSize) {
          atTail = true;
          break;
        }
        after = fresh.length ? fresh[fresh.length - 1]!.seq : after;
        if (after >= hi) break;
      }
      const changed = rows.some((e) => oldHash.has(e.seq) && oldHash.get(e.seq) !== e.chain_hash);
      const newHi = rows.length ? Math.max(hi, rows[rows.length - 1]!.seq) : atTail ? hi : lo;
      this.set({ rows, hi: newHi, atTail, changedUnderView: this.snap.changedUnderView || changed });
      this.trimFront();
    } catch (error) {
      if (this.snap.generation === gen) this.set({ error });
    } finally {
      if (this.snap.generation === gen) this.set({ refreshing: false });
    }
    if (this.snap.generation === gen && !this.snap.error && (this.forwardAgain || (wasTail && !this.snap.atTail))) {
      // Keep following the tail (or serve a scroll that arrived during the refresh).
      void this.loadForward();
    }
  }

  private trimFront(): void {
    const extra = this.snap.rows.length - this.maxRows;
    if (extra <= 0) return;
    const dropped = this.snap.rows.slice(0, extra);
    this.set({ rows: this.snap.rows.slice(extra), lo: dropped[dropped.length - 1]!.seq, trimmed: this.snap.trimmed + extra });
  }

  private trimBack(): void {
    const extra = this.snap.rows.length - this.maxRows;
    if (extra <= 0) return;
    const kept = this.snap.rows.slice(0, this.maxRows);
    this.set({ rows: kept, hi: kept[kept.length - 1]!.seq, atTail: false, trimmed: this.snap.trimmed + extra });
  }
}

// ------------------------------------------------------------------ routes

export type AuditTab = 'events' | 'journal' | 'verify';

export interface AuditRoute {
  tab: AuditTab;
  filters: EventFilters;
  /** Start position for the event log: `after=N` shows events from seq N+1; null means latest. */
  after: number | null;
  /** Journal owner filter. */
  owner: string | null;
}

const FILTER_KEYS = ['type', 'session', 'order'] as const;

/** `#/audit/events/type=X/session=Y/order=Z/after=N`, `#/audit/journal/owner=O`, `#/audit/verify`. */
export function parseAuditRoute(rest: readonly string[]): AuditRoute {
  const tab: AuditTab = rest[0] === 'journal' || rest[0] === 'verify' ? rest[0] : 'events';
  const filters: EventFilters = {};
  let after: number | null = null;
  let owner: string | null = null;
  for (const seg of rest.slice(1)) {
    const i = seg.indexOf('=');
    if (i <= 0) continue;
    const k = seg.slice(0, i);
    const v = seg.slice(i + 1).trim();
    if (!v) continue;
    if ((FILTER_KEYS as readonly string[]).includes(k)) filters[k as (typeof FILTER_KEYS)[number]] = v;
    else if (k === 'after' && /^\d+$/.test(v)) after = Number(v);
    else if (k === 'owner') owner = v;
  }
  return { tab, filters, after, owner };
}

/** Route segments (after the workspace id) for an event-log view. */
export function eventsRoute(filters: EventFilters, after: number | null = null): string[] {
  const segs = ['events'];
  for (const k of FILTER_KEYS) {
    const v = filters[k]?.trim();
    if (v) segs.push(`${k}=${v}`);
  }
  if (after !== null) segs.push(`after=${after}`);
  return segs;
}

export function sameFilters(a: EventFilters, b: EventFilters): boolean {
  return FILTER_KEYS.every((k) => (a[k] ?? '') === (b[k] ?? ''));
}
