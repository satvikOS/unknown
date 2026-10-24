/**
 * Rendered Holdings tab (react-dom/server, no DOM). Bodies follow analytics/holdings.py `holdings`:
 * valuation statuses are complete | stale_marks | unpriced_positions, and the cash ladder and the
 * authorized total are computed over the whole account whatever the scope. Every id and amount is a
 * TEST FIXTURE.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { QueryResult } from '../../state/react';
import { HoldingsTab } from './HoldingsTab';
import { valuationTone, type Rec } from './model';

function owner(name: string, status: string, nav: string | null): Rec {
  return {
    owner: name,
    kind: name.startsWith('session:') ? 'session' : name,
    system_owned: true,
    currency: 'USD',
    cash: { settled_cash: '90.000000', available_cash: '90.000000', liquid_value: '90.000000' },
    market_value_fresh: '0.000000',
    nav_marked: nav,
    nav_fresh: status === 'complete' ? nav : null,
    valuation_status: status,
    unpriced_positions: status === 'unpriced_positions' ? [{ instrument_id: 'FIX-EQ-9', quantity: '1', cost_basis: '10.00', reason: 'no quote in the supplied snapshot' }] : [],
  };
}

function holdings(scope: string): Rec {
  return {
    kind: 'holdings',
    account_key: 'SIM-1',
    scope,
    as_of_ns: 1_792_764_000_000_000_000,
    core_seq: 42,
    base_currency: 'USD',
    mark_policy: { basis: 'bid', max_age_ms: 5000, source: 'supplied quote snapshot (core quote book)' },
    positions: [],
    owners: [owner('session:S1', 'unpriced_positions', null)],
    totals: { USD: { currency: 'USD', owners: 1, liquid_value: '90.000000', available_cash: '90.000000', nav_marked: null, nav_fresh: null, valuation_status: 'unpriced_positions' } },
    cash_ladder: [
      {
        currency: 'USD',
        ledger_settled_cash: '1000.000000',
        receivable_unsettled: '0.000000',
        payable_unsettled: '0.000000',
        fees_accrued: '0.000000',
        reserved_cash_holds: '0.000000',
        unallocated_available: '910.000000',
        broker_settled_cash: null,
        broker_settled_cash_reason: 'broker cash is evidence held by reconciliation reports',
      },
    ],
    authorized_total: '1000.000000',
    fixture_data: false,
  };
}

function render(body: Rec, scope: string): string {
  const q = { status: 'success', data: body, error: null, fetching: false, fetchedAtMs: null, seqAtFetch: 42, key: 'k', refetch: () => {} } as unknown as QueryResult<unknown>;
  return renderToStaticMarkup(
    createElement(HoldingsTab, { bar: null, q, scope, setScope: () => {}, basis: 'bid', setBasis: () => {}, options: [], formulas: new Map() }),
  );
}

describe('holdings: fixture data is refused', () => {
  it('never renders an answer computed from FIXTURE data: it shows an error instead', () => {
    const body = { ...holdings('owner:session:S1'), fixture_data: true, fixture_note: 'contains FIXTURE quotes' };
    const html = render(body, 'owner:session:S1');
    expect(html).toContain('>ERR<');
    expect(html).toContain('Holdings not shown.');
    expect(html).toContain('contains FIXTURE quotes');
    expect(html).not.toContain('unpriced_positions');
    expect(html).not.toContain('910.00');
  });
});

describe('holdings: valuation status tones', () => {
  it('marks an owner and a total with an unpriced position as bad, never neutral', () => {
    const html = render(holdings('owner:session:S1'), 'owner:session:S1');
    const pills = html.match(/<span class="pill tone-[a-z]+ compact"[^>]*><span class="pill-glyph"[^>]*>[^<]*<\/span><span class="pill-text">unpriced_positions<\/span>/g) ?? [];
    expect(pills.length, 'the total and the owner row both show the status').toBeGreaterThanOrEqual(2);
    for (const p of pills) expect(p).toContain('tone-bad');
    expect(valuationTone('unpriced_positions')).toBe('bad');
    expect(valuationTone('stale_marks')).toBe('warn');
    expect(valuationTone('complete')).toBe('ok');
    expect(valuationTone('something new')).toBe('neutral');
  });
});

describe('holdings: account-wide figures under a narrower scope', () => {
  it('labels the cash ladder as the whole account, not the selected scope', () => {
    const html = render(holdings('owner:session:S1'), 'owner:session:S1');
    expect(html).toContain('Cash ladder, whole account');
    expect(html).toContain('every owner, not only scope owner:session:S1');
    // The ladder's settled cash is the account's 1,000.00, larger than the session's own 90.00.
    expect(html).toContain('1,000.00');
    expect(html).not.toContain('No cash in this scope');
  });

  it('does not label the account authorized total with the selected scope', () => {
    const html = render(holdings('owner:session:S1'), 'owner:session:S1');
    const start = html.indexOf('Authorized capital');
    expect(start).toBeGreaterThan(-1);
    const kpi = html.slice(start, start + 600);
    expect(kpi).not.toContain('scope owner:session:S1');
    expect(kpi).toContain('authorized total, whatever the scope');
  });
});

describe('holdings: NAV by owner', () => {
  it('names an owner whose NAV is unavailable instead of leaving it out of the chart silently', () => {
    const html = render(holdings('account'), 'account');
    expect(html).toContain('No owner has a positive marked NAV in this scope.');
    expect(html).toContain('Not in the chart: Session S1 (USD): NAV unavailable, an unpriced position.');
  });

  it('names an owner with a negative NAV', () => {
    const body = holdings('account');
    body.owners = [owner('unallocated', 'complete', '910.000000'), owner('session:S2', 'complete', '-4.500000')];
    const html = render(body, 'account');
    expect(html).toContain('Marked NAV by owner');
    expect(html).toContain('Not in the chart: Session S2 (USD): negative NAV.');
    expect(html).not.toContain('Unallocated (USD)');
  });
});
