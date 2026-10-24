/**
 * Rendered ownership views (react-dom/server): what the owner reads, checked as text. FIXTURE values only.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { HoldersView, InsidersView, InstitutionalView } from './OwnershipWorkspace';
import type { HoldersSection, InsidersSection, InstitutionalSection } from './model';

const CMDS = ['python -m unknown.data.sources.edgar_13f ingest --cik 9100001'];

describe('ownership views', () => {
  it('institutional holders show change, % held and an em dash with a reason for an exit', () => {
    const s: InstitutionalSection = {
      status: 'OK', period: '2026-06-30', prior_period: '2026-03-31', holder_count: 1, total_shares_reported: '7500',
      shares_outstanding: { value: '100000', date: '2026-07-17' }, concentration: { top5: '0.0750000000', top10: null, top20: null },
      changes: { entries: 0, exits: 1, increases: 1, decreases: 0 },
      holders: [
        { manager_cik: 1, manager: 'Alpha Fixture Capital', shares: '7500', value: '1600000', value_unit: 'USD', change: 'INCREASE', prior_shares: '6000',
          change_shares: '1500', accession: '0000000001-26-000003', filing_date: '2026-08-25', pct_held: '0.0750000000', pct_reason: null,
          amendment_type: 'NEW HOLDINGS' },
        { manager_cik: 3, manager: 'Gamma Fixture LLC', shares: null, shares_reason: 'NOT_REPORTED', change: 'EXIT', prior_shares: '1500',
          change_shares: null, accession: '0000000003-26-000002', filing_date: null, pct_held: null, pct_reason: 'NOT_APPLICABLE' },
      ],
      coverage: [{ window: '01jun2026-31aug2026', period: '2026-06-30', holdings_reports_in_window: 3, holders_found: 2, filers_count: 3 }],
      notes: ['Managers that have not filed for the period yet are absent (unknown), not exits.'],
    };
    const html = renderToStaticMarkup(createElement(InstitutionalView, { s, commands: CMDS }));
    expect(html).toContain('7.50 %');
    expect(html).toContain('Increase');
    expect(html).toContain('Exit (not in filed report)');
    expect(html).toContain('data-reason="NOT_REPORTED"');
    expect(html).toContain('new holdings');
    expect(html).toContain('Data set 01jun2026-31aug2026');
    expect(html).not.toMatch(/>0<\/span>/);
  });

  it('a section without data shows the reason and the command, never an empty table', () => {
    const html = renderToStaticMarkup(createElement(InstitutionalView, {
      s: { status: 'NO_DATA', reason: 'NO_DATA', detail: 'no 13F holdings ingested for this issuer at the as-of time', holders: [] },
      commands: CMDS,
    }));
    expect(html).toContain('no 13F holdings ingested');
    expect(html).toContain('edgar_13f ingest');
    expect(html).not.toContain('<table');
  });

  it('insider rows show codes, a missing price as a reason and the net flow', () => {
    const s: InsidersSection = {
      status: 'OK', filings: 1, count: 1, ten_b5_one: 'not displayed: interpretation not confirmed',
      summary: { '90': { window_days: 90, from: '2026-07-22', to: '2026-10-20', purchases: '38000', sales: '220550.00', net: '-182550.00',
        purchase_count: 1, sale_count: 1, excluded_without_price: 1, unit: 'USD (Form 4 prices carry no currency tag)' } },
      transactions: [{ obs_id: 7, accession: '0009200001-26-000004', form: '4', filing_date: '2026-10-01', available_time: 'x', insider: 'Doe Jane (FIXTURE)',
        role: 'Chief Fixture Officer', table: 'non_derivative', kind: 'transaction', security: 'Common Stock', date: '2026-09-30', code: 'S',
        code_label: 'open market or private sale', category: 'open_market_sale', shares: '1000', price: null, price_reason: 'NOT_REPORTED',
        acquired_disposed: 'D', shares_after: '50000.25', direct_indirect: 'D', nature: null, superseded_by_amendment: true,
        links: { filing_index: 'https://www.sec.gov/Archives/edgar/data/9100001/000920000126000004/0009200001-26-000004-index.htm', document: null } }],
    };
    const html = renderToStaticMarkup(createElement(InsidersView, { s, commands: CMDS }));
    expect(html).toContain('−182,550.00');
    expect(html).toContain('1 without a price not valued');
    expect(html).toContain('open market or private sale');
    expect(html).toContain('data-reason="NOT_REPORTED"');
    expect(html).toContain('amended');
    expect(html).not.toContain('10b5-1 plan');
  });

  it('5 % holders list persons, and a free-text filing says it is not parsed', () => {
    const s: HoldersSection = {
      status: 'OK', count: 2, filings: [
        { accession: '0009300001-26-000001', form: 'SCHEDULE 13G', filing_date: '2026-08-01', parsed: true, event_date: '2026-06-30',
          index_url: 'https://www.sec.gov/x', persons: [{ name: 'Fixture Capital LP', aggregate_shares: '8100000', percent_of_class: '6.1', type: 'IA' }] },
        { accession: '0009400001-24-000001', form: 'SC 13G/A', filing_date: '2024-02-10', parsed: false, index_url: 'https://www.sec.gov/y', persons: [],
          reason: 'free-text schedule (before structured XML): amounts and percent are in the document, not parsed' },
      ],
    };
    const html = renderToStaticMarkup(createElement(HoldersView, { s, commands: CMDS }));
    expect(html).toContain('Fixture Capital LP');
    expect(html).toContain('6.1 %');
    expect(html).toContain('8,100,000');
    expect(html).toContain('not parsed');
  });
});
