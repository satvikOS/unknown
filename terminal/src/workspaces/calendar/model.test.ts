import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CalendarWeeks, IssuerNotes } from './CalendarWorkspace';
import { DEFAULTS, addDays, calendarParams, dayLabel, mondayOf, parseCalendarRoute, readWatchlist, type CalEvent, type CalendarResponse } from './model';

const ev = (status: CalEvent['status'], extra: Partial<CalEvent> = {}): CalEvent => ({
  event_type: 'results_release', status, basis: status === 'projected' ? 'derived' : status === 'announced' ? 'announced' : 'filing',
  date: '2026-10-29', time: status === 'occurred' ? '2026-10-29T20:05:00.000000000Z' : null, form: status === 'projected' ? null : '8-K',
  accession: status === 'projected' ? null : '0009100002-26-000005', link: status === 'projected' ? null : 'https://www.sec.gov/x',
  label: status === 'projected' ? 'estimated (internal): 364 days after 8-K of 2025-10-30' : '8-K Item 2.02 accepted',
  entity_id: 'SEC-CIK-0009100002', cik: 9100002, name: 'Fixture Calendar Corp (synthetic)', ticker: 'FXCAL', event_label: 'Results release',
  week: '2026-10-26', ...extra,
});

describe('calendar model', () => {
  it('parses the route and computes Monday-based ranges', () => {
    expect(parseCalendarRoute(['tickers', 'aapl,msft'])).toEqual({ scope: 'tickers', tickers: 'AAPL,MSFT' });
    expect(parseCalendarRoute(['bogus'])).toEqual({ scope: 'watchlist', tickers: '' });
    expect(mondayOf('2026-10-24')).toBe('2026-10-19');
    expect(mondayOf('2026-10-19')).toBe('2026-10-19');
    expect(addDays('2026-12-30', 3)).toBe('2027-01-02');
    expect(dayLabel('2026-10-29')).toBe('Thu 2026-10-29');
  });

  it('builds parameters; an empty watchlist blocks the request with a reason', () => {
    expect(calendarParams(DEFAULTS, [], '2026-10-24')).toEqual({ blocked: expect.stringContaining('watchlist is empty') });
    const r = calendarParams({ ...DEFAULTS, weeks: 4 }, ['AAPL.XNAS', 'MSFT.XNAS'], '2026-10-24');
    expect(r).toEqual({ params: { from: '2026-10-19', to: '2026-11-15', issuers: 'AAPL.XNAS,MSFT.XNAS' } });
    const t = calendarParams({ ...DEFAULTS, scope: 'tickers', tickers: 'aapl jpm', statuses: ['occurred'], types: ['results_release'] }, [], '2026-10-24');
    expect(t).toEqual({ params: { from: '2026-10-19', to: '2027-01-17', issuers: 'AAPL,JPM', status: 'occurred', types: 'results_release' } });
    expect(calendarParams({ ...DEFAULTS, scope: 'all' }, [], '2026-10-24')).toEqual({ params: { from: '2026-10-19', to: '2027-01-17' } });
    expect(readWatchlist(JSON.stringify({ lists: [{ keys: ['AAPL.XNAS', 'bad key'] }] }))).toEqual(['AAPL.XNAS']);
  });
});

describe('calendar views', () => {
  const data: CalendarResponse = {
    as_of: '2026-10-24T00:00:00.000000000Z', from: '2026-10-19', to: '2027-01-17', count: 3, unresolved: ['NOPE'], event_types: {},
    legend: { occurred: 'filed on EDGAR', announced: 'stated in an 8-K', projected: 'estimated (internal)' },
    issuers: [{ entity_id: 'SEC-CIK-0009100002', name: 'Fixture Calendar Corp (synthetic)', status: 'OK', events: 3 },
      { entity_id: 'SEC-CIK-0009100009', name: null, status: 'NO_DATA', reason: 'NO_DATA', detail: 'no stored submissions payload for this entity' }],
    weeks: [{ week_start: '2026-10-26', events: [ev('occurred'), ev('announced', { evidence: 'will report results on October 29, 2026', time_text: '4:30 p.m. ET' }),
      ev('projected', { event_type: 'periodic_report', overdue: false })] }],
  };

  it('every entry shows its status and source; projected entries say they are estimated', () => {
    const html = renderToStaticMarkup(createElement(CalendarWeeks, { data }));
    expect(html).toContain('Week of 2026-10-26');
    expect(html).toContain('Occurred');
    expect(html).toContain('Announced');
    expect(html).toContain('Projected (estimated)');
    expect(html).toContain('estimated (internal): 364 days after');
    expect(html).toContain('20:05 UTC');
    expect(html).toContain('4:30 p.m. ET');
    expect(html).toContain('will report results on October 29, 2026');
    expect(html).toContain('data-reason="NOT_REPORTED"');          // the projection has no time: a reason, not a blank
  });

  it('says when there are no events, and lists issuers it could not use', () => {
    const empty = renderToStaticMarkup(createElement(CalendarWeeks, { data: { ...data, weeks: [] } }));
    expect(empty).toContain('No events in 2026-10-19 to 2027-01-17');
    const notes = renderToStaticMarkup(createElement(IssuerNotes, { data }));
    expect(notes).toContain('NOPE');
    expect(notes).toContain('no stored submissions payload');
  });
});
