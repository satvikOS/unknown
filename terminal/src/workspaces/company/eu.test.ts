/**
 * EU issuer helpers (CAP-CO-03): LEI check digits, route round trip, statement ordering, SEC pairing input, and the
 * search source label that puts SEC and ESEF issuers side by side. Values are synthetic.
 */
import { describe, expect, it } from 'vitest';
import { sourceLabel } from './CompanyWorkspace';
import { cellText, checkCik, euRoute, euSections, leiValid, parseEuRoute } from './eu';
import type { SearchEuIssuer, SearchIssuer } from './model';

describe('EU issuer helpers', () => {
  it('checks LEI check digits like the backend', () => {
    expect(leiValid('724500Y6DUVHQD6OXN27')).toBe(true);
    expect(leiValid('529900d6bf99lw9r2e68')).toBe(true);
    expect(leiValid('724500Y6DUVHQD6OXN28')).toBe(false);
    expect(leiValid('SAP')).toBe(false);
  });

  it('round-trips the route and ignores unknown options', () => {
    const segs = euRoute({ lei: '724500Y6DUVHQD6OXN27', tab: 'compare', opts: { as_of: '2026-06-30', sec: '937966' } });
    expect(segs).toEqual(['eu', '724500Y6DUVHQD6OXN27', 'compare', 'as_of=2026-06-30', 'sec=937966']);
    expect(parseEuRoute([...segs, 'price=1'])).toEqual({ lei: '724500Y6DUVHQD6OXN27', tab: 'compare', opts: { as_of: '2026-06-30', sec: '937966' } });
    expect(parseEuRoute(['320193', 'statements'])).toBeNull();
    expect(parseEuRoute(['eu', 'x'])?.tab).toBe('statements');
  });

  it('orders sections and shows missing cells by reason', () => {
    const line = { item: 'x', label: 'X', unit: 'EUR', rule: 'r', concepts: [], fallback: [], cells: [] };
    expect(euSections({ statements: { cash_flow: [line], income: [line], balance: [] } }).map(([k]) => k)).toEqual(['income', 'cash_flow']);
    expect(cellText({ value: null, missing_reason: 'NOT_REPORTED' })).toBe('NOT_REPORTED');
  });

  it('accepts a CIK for the SEC pairing and nothing else', () => {
    expect(checkCik('CIK0000937966')).toEqual({ ok: true, value: '937966' });
    expect(checkCik('')).toEqual({ ok: true, value: '' });
    expect(checkCik('ASML').ok).toBe(false);
  });

  it('labels source and currency of SEC and ESEF candidates', () => {
    const eu: SearchEuIssuer = {
      kind: 'eu_issuer',
      lei: 'L',
      entity_id: 'ESEF-LEI-L',
      name: 'X',
      match: 'NAME_PREFIX',
      countries: ['NL'],
      esef_status: 'OK',
      reason: null,
      reporting_currency: 'EUR',
      filings: 3,
      source: 'filings.xbrl.org (ESEF)',
      source_label: 'ESEF annual reports (EU, IFRS)',
      retrieved_at: null,
      ingested: true,
      listings: [],
    };
    const sec: SearchIssuer = { kind: 'issuer', cik: 1, entity_id: 'SEC-CIK-0000000001', name: 'Y', listings: [], match: 'NAME_PREFIX', ingested: true };
    expect(sourceLabel(eu)).toBe('ESEF annual reports (EU, IFRS) · EUR');
    expect(sourceLabel(sec)).toBe('SEC EDGAR (US filer)');
    expect(sourceLabel({ ...eu, reporting_currency: null })).toContain('currency not yet known');
  });
});
