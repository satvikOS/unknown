import { describe, expect, it } from 'vitest';
import { executeCommand, listFunctions, parseCommand } from '../../ui/functions';
import { WORKSPACES } from '../registry';
import {
  filtered,
  formLabel,
  issuerLabel,
  listParams,
  mergeLive,
  newsRoute,
  parseNewsRoute,
  publishedLabel,
  splitSnippet,
  watchlistIssuers,
  type NewsItem,
} from './model';

function item(id: number, extra: Partial<NewsItem> = {}): NewsItem {
  return {
    id, source_id: 'sec-filings', kind: 'filing', guid: `g${id}`, title: `t${id}`, summary: null, summary_truncated: false,
    link: null, category: null, published: '2026-10-15T20:05:01.000000000Z', published_precision: 'second',
    first_seen: '2026-10-15T20:05:40.000000000Z', first_seen_ns: '1792000000000000000', form: '8-K', accession: null,
    items: [], filed_date: null, size: null, cluster: null, duplicate_of: null, flags: [], entities: [], snippet: null, ...extra,
  };
}

describe('news route', () => {
  it('parses and builds the tape, top and company views with filters', () => {
    expect(parseNewsRoute([])).toEqual({ view: 'tape' });
    const o = parseNewsRoute(['cn', 'form=8-K', 'q=bank merger', 'bogus=1', 'since=24h']);
    expect(o).toEqual({ view: 'cn', form: '8-K', q: 'bank merger', since: '24h' });
    expect(newsRoute(o)).toEqual(['cn', 'form=8-K', 'q=bank merger', 'since=24h']);
    expect(parseNewsRoute(newsRoute({ view: 'top' }))).toEqual({ view: 'top' });
  });

  it('company news needs the linked security and sends it as the issuer', () => {
    expect(listParams({ view: 'cn' }, null)).toBeNull();
    expect(listParams({ view: 'cn', issuer: 'IGNORED' }, 'FXRB.XNAS')).toEqual({ limit: '200', issuer: 'FXRB.XNAS' });
    expect(listParams({ view: 'tape', source: 'sec-filings', q: '"2.02"' }, 'FXRB.XNAS')).toEqual({ limit: '200', source: 'sec-filings', q: '"2.02"' });
    expect(filtered({ view: 'tape' })).toBe(false);
    expect(filtered({ view: 'cn' })).toBe(true);
    expect(filtered({ view: 'tape', form: '4' })).toBe(true);
  });
});

describe('live merge and labels', () => {
  it('merges stream items newest first without duplicates', () => {
    const merged = mergeLive([item(3), item(1)], [item(4), item(3, { title: 'updated' })], 3);
    expect(merged.map((i) => i.id)).toEqual([4, 3, 1]);
    expect(merged[1]?.title).toBe('updated');
    expect(mergeLive([item(1), item(2)], [item(3)], 2).map((i) => i.id)).toEqual([3, 2]);
  });

  it('never invents a time the feed did not give', () => {
    expect(publishedLabel({ published: null, published_precision: null })).toBe('not given by the feed');
    expect(publishedLabel({ published: '2026-10-22T18:30:00.000000000Z', published_precision: 'day' })).toBe('2026-10-22 (date only)');
    expect(publishedLabel({ published: '2026-10-15T20:05:01.000000000Z', published_precision: 'second' })).toBe('2026-10-15 20:05:01 UTC');
  });

  it('labels issuers with the snapshot ticker and forms with their 8-K items', () => {
    const ent = { scheme: 'SEC_CIK', name: 'FIXTURE ROBOTICS INC', method: 'edgar_feed', confidence: 1, figi: null,
      identifiers_as_of: null, identifier_source: null, evidence: null };
    const f = item(1, {
      entities: [{ ...ent, value: '7777777', role: 'reporting', tickers: [], name: 'Doe Jane' },
        { ...ent, value: '1234567', role: 'issuer', tickers: [{ ticker: 'FXRB', exchange: 'Nasdaq' }] }],
      items: [{ code: '2.02', title: null, type: 'results' }, { code: '9.01', title: null, type: 'financial_statements_exhibits' }],
    });
    expect(issuerLabel(f)).toBe('FXRB');
    expect(formLabel(f)).toBe('8-K 2.02,9.01');
    expect(issuerLabel(item(2))).toBe('');
    expect(formLabel(item(3, { form: null, category: 'Enforcement Actions' }))).toBe('Enforcement Actions');
  });

  it('splits search snippets at the highlight markers', () => {
    expect(splitSnippet('the \u0002approved\u0003 application')).toEqual([
      { text: 'the ', hit: false }, { text: 'approved', hit: true }, { text: ' application', hit: false }]);
    expect(splitSnippet(null)).toEqual([]);
  });

  it('reads alert issuers from the monitor watchlists and ignores bad storage', () => {
    const raw = JSON.stringify({ version: 1, active: 'a', lists: [{ id: 'a', name: 'A', keys: ['FXRB.XNAS', 'EXHB.XNYS'] },
      { id: 'b', name: 'B', keys: ['FXRB.XNAS', '<script>', 7] }] });
    expect(watchlistIssuers(raw)).toEqual(['FXRB.XNAS', 'EXHB.XNYS']);
    expect(watchlistIssuers('{not json')).toEqual([]);
    expect(watchlistIssuers(null)).toEqual([]);
  });
});

describe('news functions', () => {
  it('registers N, TOP and CN on the command line', () => {
    const fns = listFunctions(WORKSPACES);
    const opened: [string, readonly string[]][] = [];
    const deps = (linked: string | null) => ({
      installed: () => true,
      target: { channel: 'default', label: 'panel 1', open: (w: string, rest: readonly string[]) => { opened.push([w, rest]); return null; } },
      linked: () => linked,
      setLinked: () => undefined,
    });
    expect(executeCommand(parseCommand('N', fns), deps(null)).ok).toBe(true);
    expect(executeCommand(parseCommand('TOP', fns), deps(null)).ok).toBe(true);
    expect(executeCommand(parseCommand('CN', fns), deps(null)).ok).toBe(false);
    expect(executeCommand(parseCommand('FXRB.XNAS CN', fns), deps(null)).ok).toBe(true);
    expect(opened).toEqual([['news', []], ['news', ['top']], ['news', ['cn']]]);
  });
});
