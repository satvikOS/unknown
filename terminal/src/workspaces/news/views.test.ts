/**
 * Rendered news views (react-dom/server, no browser): what the owner reads, checked as text.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SourcesPanel, Tape } from './NewsWorkspace';
import type { NewsItem, NewsSourceRow } from './model';

const src = (source_id: string, name: string, verdict: string, extra: Partial<NewsSourceRow> = {}): NewsSourceRow => ({
  source_id, name, kind: 'regulator', region: 'US', provider_id: source_id, verdict, pollable: verdict !== 'blocked',
  refusal: null, last_ok: null, last_poll: null, last_status: null, last_error: null, ...extra,
});

const SOURCES = [
  src('sec-filings', 'SEC EDGAR', 'allowed', { last_ok: '2026-10-23T21:40:00.000000000Z', last_status: 'OK' }),
  src('sec-press', 'SEC press', 'allowed', { last_status: 'FAILED', last_error: 'FetchError: 404 Not Found' }),
  src('nse', 'NSE announcements', 'blocked', { refusal: 'terms prohibit automated data collection' }),
];

const row = (id: number, extra: Partial<NewsItem> = {}): NewsItem => ({
  id, source_id: 'sec-filings', kind: 'filing', guid: `g${id}`, title: `8-K - FIXTURE ROBOTICS INC ${id}`, summary: null,
  summary_truncated: false, link: null, category: '8-K', published: null, published_precision: null,
  first_seen: '2026-10-23T21:40:05.000000000Z', first_seen_ns: '1792000000000000000', form: '8-K', accession: null,
  items: [{ code: '2.02', title: null, type: 'results' }], filed_date: null, size: null, cluster: null, duplicate_of: null,
  flags: [], entities: [], snippet: null, ...extra,
});

describe('news tape', () => {
  it('shows source, first-seen time, form items and flashes a row that just arrived', () => {
    const html = renderToStaticMarkup(createElement(Tape, {
      rows: [row(2), row(1, { duplicate_of: 9, snippet: 'x \u0002merger\u0003 y' })], sources: SOURCES, flash: new Set([2]),
      selected: null, onSelect: () => undefined, loading: false,
    }));
    expect(html).toContain('class="nw-flash"');
    expect(html).toContain('SEC EDGAR');
    expect(html).toContain('21:40:05');
    expect(html).toContain('8-K 2.02');
    expect(html).toContain('Published: not given by the feed');
    expect(html).toContain('<mark>merger</mark>');
    expect(html).toContain(' · copy');
  });

  it('an empty tape says so instead of showing zero rows silently', () => {
    const html = renderToStaticMarkup(createElement(Tape, { rows: [], sources: SOURCES, flash: new Set<number>(), selected: null, onSelect: () => undefined, loading: false }));
    expect(html).toContain('No items match');
  });
});

describe('news sources', () => {
  it('lists blocked sources with the reason and failed polls with their error', () => {
    const html = renderToStaticMarkup(createElement(SourcesPanel, { sources: SOURCES }));
    expect(html).toContain('not fetched: terms prohibit automated data collection');
    expect(html).toContain('last poll failed: FetchError: 404 Not Found');
    expect(html).toContain('last success 2026-10-23 21:40:00 UTC');
    expect(html).toContain('not polled yet');
  });
});
