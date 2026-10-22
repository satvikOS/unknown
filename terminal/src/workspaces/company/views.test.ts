/**
 * Rendered Company views (react-dom/server, no browser): what the owner reads, checked as text.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { FilingsContent, PeerGroup } from './model';
import { FilingsTable, PeerGroupPanel } from './Peers';

const filing = (accession: string, acceptance: string, primary: string | null) => ({
  accession,
  form: '10-K',
  filing_date: '2025-11-01',
  report_date: '2025-09-27',
  acceptance_time_raw: acceptance,
  items: [],
  flags: [],
  primary_document_description: '10-K',
  is_xbrl: true,
  file_number: null,
  links: { filing_index: `https://www.sec.gov/Archives/edgar/data/9000001/${accession}-index.htm`, primary_document: primary },
});

const filings: FilingsContent = {
  cik: 9000001,
  as_of: '2026-10-22T12:00:00.000000000Z',
  count: 3,
  filings: [filing('0009000001-25-000004', '2025-11-01T06:01:36.000Z', null), filing('0009000001-25-000003', '2025-08-01T16:30:00.000Z', 'https://www.sec.gov/x.htm')],
  not_yet_accepted_at_as_of: 0,
  coverage_note: 'Main submissions file only.',
  acceptance_note: 'acceptanceDateTime as published in the submissions JSON; on fetched samples it is later than true UTC (CAP-CO-06), so the as-of filter is conservative.',
};

describe('filings table', () => {
  it('shows acceptanceDateTime as published, never converted as if it were true UTC', () => {
    const html = renderToStaticMarkup(createElement(FilingsTable, { f: filings }));
    // The SEC value carries a "Z" but is later than true UTC by the Eastern offset (filings.py, CAP-CO-06): the
    // terminal must not re-render it as "2025-11-01 06:01:36 UTC".
    expect(html).toContain('2025-11-01T06:01:36.000Z');
    expect(html).toContain('2025-08-01T16:30:00.000Z');
    expect(html).not.toContain('06:01:36 UTC');
    expect(html).not.toContain('<time');
  });

  it('says when the list is cut short, and shows a description without a link when there is none', () => {
    const html = renderToStaticMarkup(createElement(FilingsTable, { f: filings }));
    expect(html).toContain('Filings · 2 of 3 accepted by as of');
    expect(html).not.toContain('href=""');
  });
});

describe('peer group', () => {
  const g: PeerGroup = {
    target: { entity_id: 'SEC-CIK-0009000001', name: 'Fixture Widgets Inc (synthetic)', cik: 9000001, sic: '3571' },
    as_of: '2026-02-01T23:59:59.999999999Z',
    rule: { id: 'peers_sic4@1', description: 'same 4-digit SIC', sic: '3571', size_band: 'none' },
    members: [{ entity_id: 'SEC-CIK-0009000002', name: 'Fixture Peer Two (synthetic)', cik: 9000002, sic: '3571' }],
    excluded: [],
    caveats: [],
  };

  it('opens a member at the same point in time, without this issuer price input', () => {
    const html = renderToStaticMarkup(
      createElement(PeerGroupPanel, { g, opts: { as_of: '2026-02-01', price: '50.00', price_currency: 'USD', price_source: 'FIXTURE price', price_time: '2026-01-30T20:00:00Z' } }),
    );
    expect(html).toContain('href="#/company/9000002/tearsheet/as_of%3D2026-02-01"');
    expect(html).not.toContain('price%3D');
  });
});
