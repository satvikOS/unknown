import { describe, expect, it, vi } from 'vitest';
import type { EventRecord, JournalEntry } from '../../api/types';
import { EventLog, eventsRoute, parseAuditRoute, sameFilters, type EventQuery } from './eventLog';
import { isZeroDecimal, sumDecimals, totalsByCurrency } from './exact';
import { EMPTY_JOURNAL, entryBalance, mergeJournalHead, mergeJournalPage, ownerKind, ownerSuggestions } from './journal';

// ------------------------------------------------------------------ fixtures

function ev(seq: number, type = 'T', hash = `h${seq}`): EventRecord {
  return {
    seq,
    event_id: `e${seq}`,
    type,
    schema_version: 1,
    command_id: `c${seq}`,
    correlation_id: `c${seq}`,
    principal_id: 'owner',
    account_key: 'SIM-1',
    session_id: null,
    order_id: null,
    instrument_id: null,
    event_time_ns: null,
    receipt_time_ns: 0,
    recorded_time_ns: 0,
    writer_epoch: 1,
    payload_hash: `p${seq}`,
    chain_hash: hash,
    info: {},
    changed_sections: [],
  };
}

/** A fake /api/events over an in-memory log with the server's semantics (seq > after, filter, limit). */
function server(log: EventRecord[]) {
  const calls: EventQuery[] = [];
  const fetcher = vi.fn(async (q: EventQuery) => {
    calls.push(q);
    return log.filter((e) => e.seq > q.after && (!q.type || e.type === q.type)).slice(0, Math.min(q.limit, 2000));
  });
  return { fetcher, calls };
}

const range = (from: number, to: number, type?: (n: number) => string) => Array.from({ length: to - from + 1 }, (_, i) => ev(from + i, type ? type(from + i) : 'T'));

function assertCoverage(log: EventLog, all: EventRecord[], type?: string) {
  const s = log.getSnapshot();
  const expected = all.filter((e) => e.seq > s.lo && e.seq <= s.hi && (!type || e.type === type)).map((e) => e.seq);
  expect(s.rows.map((e) => e.seq)).toEqual(expected);
}

// ------------------------------------------------------------------ event log

describe('EventLog', () => {
  it('starts at a seq and pages forward with after=<last seq>', async () => {
    const all = range(1, 450);
    const { fetcher, calls } = server(all);
    const log = new EventLog(fetcher, { pageSize: 200 });
    await log.startAt({}, 0);
    expect(log.getSnapshot()).toMatchObject({ lo: 0, hi: 200, atTail: false });
    await log.loadForward();
    await log.loadForward();
    expect(calls.map((c) => c.after)).toEqual([0, 200, 400]);
    expect(log.getSnapshot()).toMatchObject({ hi: 450, atTail: true });
    assertCoverage(log, all);
  });

  it('follows the tail on new seqs only when at the tail', async () => {
    const all = range(1, 10);
    const { fetcher } = server(all);
    const log = new EventLog(fetcher, { pageSize: 200 });
    await log.startAt({}, 0);
    all.push(ev(11), ev(12));
    log.onSeq(12);
    await vi.waitFor(() => expect(log.getSnapshot().hi).toBe(12));
    assertCoverage(log, all);
    expect(log.getSnapshot().atTail).toBe(true);
  });

  it('a seq that arrives during a forward fetch causes exactly one more fetch', async () => {
    const all = range(1, 5);
    let release: (() => void) | null = null;
    const calls: number[] = [];
    const fetcher = vi.fn(async (q: EventQuery) => {
      calls.push(q.after);
      if (calls.length === 2) await new Promise<void>((r) => (release = r));
      return all.filter((e) => e.seq > q.after).slice(0, q.limit);
    });
    const log = new EventLog(fetcher, { pageSize: 200 });
    await log.startAt({}, 0);
    all.push(ev(6));
    log.onSeq(6);
    await vi.waitFor(() => expect(release).not.toBeNull());
    all.push(ev(7));
    log.onSeq(7);
    log.onSeq(7);
    release!();
    await vi.waitFor(() => expect(log.getSnapshot().loadingForward).toBe(false));
    // The blocked fetch already returned seq 7, so the single extra fetch asks after 7.
    expect(calls).toEqual([0, 5, 7]);
    expect(log.getSnapshot().hi).toBe(7);
  });

  it('starts at the latest seq and steps back through sparse filtered windows', async () => {
    const all = range(1, 3000, (n) => (n % 700 === 0 ? 'StopLatched' : 'Other'));
    const { fetcher, calls } = server(all);
    const log = new EventLog(fetcher, { pageSize: 200, backWindow: 500, latestMinRows: 3, maxBackSteps: 10 });
    await log.startLatest({ type: 'StopLatched' }, 3000);
    const s = log.getSnapshot();
    // Windows (2500,3000], (2000,2500], (1500,2000], (1000,1500]: three matches reach latestMinRows.
    expect(s.rows.map((e) => e.seq)).toEqual([1400, 2100, 2800]);
    expect(s.lo).toBe(1000);
    expect(s.hi).toBe(3000);
    expect(calls.every((c) => c.type === 'StopLatched')).toBe(true);
    assertCoverage(log, all, 'StopLatched');
    await log.loadBackward(); // stops at the first window with a match
    expect(log.getSnapshot()).toMatchObject({ lo: 500 });
    expect(log.getSnapshot().rows[0]?.seq).toBe(700);
    await log.loadBackward(); // nothing earlier: steps to the start of the log
    expect(log.getSnapshot().lo).toBe(0);
    assertCoverage(log, all, 'StopLatched');
  });

  it('unfiltered latest start shows the last window and nothing older than lo', async () => {
    const all = range(1, 1234);
    const { fetcher } = server(all);
    const log = new EventLog(fetcher, { pageSize: 200, backWindow: 500, latestMinRows: 100 });
    await log.startLatest({}, 1234);
    expect(log.getSnapshot()).toMatchObject({ lo: 734, hi: 1234, atTail: true });
    assertCoverage(log, all);
  });

  it('caps the window and moves the bounds so coverage stays exact', async () => {
    const all = range(1, 1000);
    const { fetcher } = server(all);
    const log = new EventLog(fetcher, { pageSize: 200, maxRows: 300 });
    await log.startAt({}, 0);
    await log.loadForward();
    await log.loadForward();
    const s = log.getSnapshot();
    expect(s.rows).toHaveLength(300);
    expect(s).toMatchObject({ lo: 300, hi: 600, trimmed: 300 });
    assertCoverage(log, all);
    await log.loadBackward();
    expect(log.getSnapshot()).toMatchObject({ lo: 0, atTail: false });
    expect(log.getSnapshot().rows).toHaveLength(300);
    assertCoverage(log, all);
  });

  it('refresh refetches the window, keeps following and reports a changed chain hash', async () => {
    const all = range(1, 50);
    const { fetcher } = server(all);
    const log = new EventLog(fetcher, { pageSize: 20 });
    await log.startAt({}, 0);
    await log.loadForward();
    await log.loadForward();
    expect(log.getSnapshot().atTail).toBe(true);
    all[9] = ev(10, 'T', 'different');
    all.push(ev(51));
    await log.refresh();
    await vi.waitFor(() => expect(log.getSnapshot().hi).toBe(51));
    expect(log.getSnapshot().changedUnderView).toBe(true);
    expect(log.getSnapshot().view).toBe(1); // a refresh is not a new view (the page keeps its scroll)
    assertCoverage(log, all);
  });

  it('ignores responses from a superseded start', async () => {
    const all = range(1, 30);
    let slow: ((v: EventRecord[]) => void) | null = null;
    const fetcher = vi.fn((q: EventQuery) =>
      q.type === 'slow' ? new Promise<EventRecord[]>((r) => (slow = r)) : Promise.resolve(all.filter((e) => e.seq > q.after).slice(0, q.limit)),
    );
    const log = new EventLog(fetcher, { pageSize: 200 });
    const first = log.startAt({ type: 'slow' }, 0);
    await log.startAt({}, 10);
    slow!([ev(1, 'slow')]);
    await first;
    expect(log.getSnapshot().rows.map((e) => e.seq)[0]).toBe(11);
    expect(log.getSnapshot().lo).toBe(10);
  });

  it('records fetch errors without losing loaded rows', async () => {
    const all = range(1, 5);
    let fail = false;
    const fetcher = vi.fn(async (q: EventQuery) => {
      if (fail) throw new Error('HTTP 503');
      return all.filter((e) => e.seq > q.after).slice(0, q.limit);
    });
    const log = new EventLog(fetcher);
    await log.startAt({}, 0);
    fail = true;
    all.push(ev(6));
    await log.loadForward();
    expect(log.getSnapshot().rows).toHaveLength(5);
    expect(String(log.getSnapshot().error)).toContain('503');
  });
});

describe('audit routes', () => {
  it('parses tabs, filters, start position and owner', () => {
    expect(parseAuditRoute([])).toEqual({ tab: 'events', filters: {}, after: null, owner: null });
    expect(parseAuditRoute(['events', 'type=StopLatched', 'session=ses-1', 'after=41', 'bogus', 'order='])).toEqual({
      tab: 'events',
      filters: { type: 'StopLatched', session: 'ses-1' },
      after: 41,
      owner: null,
    });
    expect(parseAuditRoute(['journal', 'owner=session:ses-1'])).toMatchObject({ tab: 'journal', owner: 'session:ses-1' });
    expect(parseAuditRoute(['verify'])).toMatchObject({ tab: 'verify' });
    expect(parseAuditRoute(['events', 'after=-3'])).toMatchObject({ after: null });
  });

  it('round-trips event routes and compares filters', () => {
    const segs = eventsRoute({ order: 'ord-1', type: 'X' }, 7);
    expect(segs).toEqual(['events', 'type=X', 'order=ord-1', 'after=7']);
    expect(parseAuditRoute(segs)).toEqual({ tab: 'events', filters: { type: 'X', order: 'ord-1' }, after: 7, owner: null });
    expect(sameFilters({ type: 'X' }, { type: 'X', session: '' })).toBe(true);
    expect(sameFilters({ type: 'X' }, {})).toBe(false);
  });
});

// ------------------------------------------------------------------ exact sums and journal

describe('exact decimal sums', () => {
  it('adds without floating point and keeps the largest scale', () => {
    expect(sumDecimals(['0.1', '0.2'])).toBe('0.3');
    expect(sumDecimals(['300.000000', '-300.000000'])).toBe('0.000000');
    expect(sumDecimals(['9007199254740993.000001', '1'])).toBe('9007199254740994.000001');
    expect(sumDecimals(['-0.5', '0.25'])).toBe('-0.25');
    expect(sumDecimals([])).toBe('0');
    expect(isZeroDecimal('-0.000')).toBe(true);
    expect(() => sumDecimals(['1e3'])).toThrow();
  });

  it('totals per currency', () => {
    expect(
      totalsByCurrency([
        { currency: 'USD', amount: '10.5' },
        { currency: 'EUR', amount: '-1' },
        { currency: 'USD', amount: '-10.25' },
      ]),
    ).toEqual([
      ['EUR', '-1'],
      ['USD', '0.25'],
    ]);
  });
});

function je(id: string, seq: number, lines: [string, string, string][] = [['USD', 'outside', '1'], ['USD', 'outside', '-1']]): JournalEntry {
  return {
    entry_id: id,
    event_seq: seq,
    account_key: 'SIM-1',
    kind: 'K',
    memo: '',
    reverses: null,
    lines: lines.map(([currency, owner, amount], i) => ({ line: i + 1, account_code: 'CASH', owner, currency, instrument_id: '', amount })),
  };
}

describe('journal paging', () => {
  it('ends when a page is short', () => {
    const s = mergeJournalPage(EMPTY_JOURNAL, [je('a', 5), je('b', 3)], 200, null);
    expect(s).toEqual({ entries: [je('a', 5), je('b', 3)], cursor: null, possiblySkipped: false });
  });

  it('continues from seq+1 so an event with several entries is not split, de-duplicating the overlap', () => {
    // Server holds: seq 9 (x), seq 8 (y1, y2, y3), seq 7 (z). Page size 3.
    const p1 = [je('x', 9), je('y1', 8), je('y2', 8)];
    const s1 = mergeJournalPage(EMPTY_JOURNAL, p1, 3, null);
    expect(s1.cursor).toBe(9);
    const p2 = [je('y1', 8), je('y2', 8), je('y3', 8)]; // before=9
    const s2 = mergeJournalPage(s1, p2, 3, 9);
    // seq+1 again would repeat the same page: fall back to before=8 and report the possible skip.
    expect(s2.cursor).toBe(8);
    expect(s2.possiblySkipped).toBe(true);
    expect(s2.entries.map((e) => e.entry_id)).toEqual(['x', 'y1', 'y2', 'y3']);
    const s3 = mergeJournalPage(s2, [je('z', 7)], 3, 8);
    expect(s3.cursor).toBeNull();
    expect(s3.entries.map((e) => e.entry_id)).toEqual(['x', 'y1', 'y2', 'y3', 'z']);
  });

  it('progresses normally across distinct seqs', () => {
    const s1 = mergeJournalPage(EMPTY_JOURNAL, [je('a', 10), je('b', 9)], 2, null);
    expect(s1.cursor).toBe(10);
    const s2 = mergeJournalPage(s1, [je('b', 9), je('c', 8)], 2, 10);
    expect(s2).toMatchObject({ cursor: 9, possiblySkipped: false });
    expect(s2.entries.map((e) => e.entry_id)).toEqual(['a', 'b', 'c']);
  });

  it('merges new head entries, and asks for a reload when the head page may hide a gap', () => {
    const s = mergeJournalPage(EMPTY_JOURNAL, [je('b', 5), je('a', 4)], 2, null);
    const same = mergeJournalHead(s, [je('b', 5), je('a', 4)], 2);
    expect(same).toBe(s);
    const one = mergeJournalHead(s, [je('c', 6), je('b', 5)], 2);
    expect(one?.entries.map((e) => e.entry_id)).toEqual(['c', 'b', 'a']);
    expect(one?.cursor).toBe(s.cursor);
    expect(mergeJournalHead(s, [je('e', 8), je('d', 7)], 2)).toBeNull();
    expect(mergeJournalHead(EMPTY_JOURNAL, [je('x', 1)], 2)?.entries).toHaveLength(1);
  });

  it('checks that each entry balances per currency exactly', () => {
    expect(entryBalance(je('a', 1, [['USD', 'unallocated', '300.000000'], ['USD', 'outside', '-300.000000']]))).toEqual({
      balanced: true,
      totals: [['USD', '0.000000']],
    });
    expect(entryBalance(je('b', 1, [['USD', 'outside', '0.10'], ['USD', 'outside', '-0.09']])).balanced).toBe(false);
  });

  it('classifies owners and suggests filter values', () => {
    expect(ownerKind('session:ses-1')).toEqual({ kind: 'session', ref: 'ses-1' });
    expect(ownerKind('protected:ses-1')).toEqual({ kind: 'protected', ref: 'ses-1' });
    expect(ownerKind('outside')).toEqual({ kind: 'outside', ref: null });
    expect(ownerKind('weird')).toEqual({ kind: 'other', ref: null });
    expect(ownerSuggestions([je('a', 1, [['USD', 'session:s1', '1'], ['USD', 'outside', '-1']])])).toEqual([
      'buffer',
      'external',
      'outside',
      'session:s1',
      'unallocated',
    ]);
  });
});
