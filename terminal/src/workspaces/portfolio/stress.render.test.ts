/**
 * Rendered stress results (react-dom/server, no DOM). Bodies follow analytics/stress.py: fee_revision
 * (pending estimates, open-buy envelopes, a broker-commission reaction), stale_quote (fresh NAV before
 * and after) and the shock scenarios' `unmapped` positions. Every id and amount is a TEST FIXTURE.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { scenarioParams, type Rec } from './model';
import { CoreReaction, PnlSummary, ScenarioDetails, bodyScenario } from './StressTab';
import { intentText } from './WhatIfTab';

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

const feeRevision: Rec = {
  kind: 'stress',
  scenario: 'fee_revision',
  label: 'HYPOTHETICAL',
  scope: 'account',
  core_seq: 57,
  parameters: {
    factor: '2',
    model_now: { per_share: '0.005', minimum: '1.00', maximum_bps_of_notional: 100 },
    model_revised: { per_share: '0.0100000000', minimum: '2.0000000000', maximum_bps_of_notional: 200, note: 'scaled' },
  },
  pending_estimates: [
    { exec_key: 'SIM-EXEC-1', owner: 'session:S1', currency: 'USD', estimate: '1.000000', revised_final: '2.000000', additional_cost: '1.000000' },
  ],
  additional_cost_by_currency: { USD: '1.000000' },
  exit_costs: [{ owner: 'session:S1', instrument_id: 'FIX-EQ-1', exit_fee_current_model: '1.000000', exit_fee_revised: '2.000000', difference: '1.000000' }],
  open_buy_envelopes: [
    { order_id: 'ord-1', fee_envelope_remaining: '1.050000', revised_fee: '2.000000', under_reserved_by: '0.950000' },
    { order_id: 'ord-2', fee_envelope_remaining: '5.000000', revised_fee: '2.000000', under_reserved_by: null },
  ],
  core_reaction: {
    events: [{ type: 'BrokerCommissionBooked', info: { exec_key: 'SIM-EXEC-1' } }],
    available_cash_after: [{ owner: 'session:S1', currency: 'USD', available: '87.000000' }],
    open_exceptions_after: ['FEE_ABOVE_ENVELOPE'],
  },
  note: 'the observation path always books the broker\'s final fee; an unexpected amount reduces available cash and may open an exception, it is never discarded (ADR-0006)',
};

describe('fee revision: every result the backend computes is shown', () => {
  const usd = (id: string) => (id === 'FIX-EQ-1' ? 'USD' : null);

  it('lists pending estimates with their additional cost, and flags under-reserved open buys', () => {
    const html = renderToStaticMarkup(createElement(ScenarioDetails, { body: feeRevision, scenario: 'fee_revision', instrumentCurrency: usd }));
    const t = text(html);
    expect(t).toContain('Additional fees on pending estimates, USD');
    expect(html).toContain('SIM-EXEC-1');
    expect(t).toMatch(/1\.00 USD .*2\.00 USD .*1\.00 USD/);
    expect(t).not.toContain('No pending fee estimates to finalise.');
    expect(t).toContain('1 open buy order(s) under-reserved');
    expect(html).toContain('ord-1');
    expect(t).toContain('0.95 (currency not reported)');
    expect(t).toContain('covered');
    expect(t).not.toContain('No open buy envelopes to recompute.');
  });

  it('labels exit fees with the instrument currency, or says it is not reported', () => {
    expect(text(renderToStaticMarkup(createElement(ScenarioDetails, { body: feeRevision, scenario: 'fee_revision', instrumentCurrency: usd })))).toContain(
      'fee now 1.00 USD , revised 2.00 USD , difference 1.00 USD',
    );
    const unknown = text(renderToStaticMarkup(createElement(ScenarioDetails, { body: feeRevision, scenario: 'fee_revision' })));
    expect(unknown).toContain('fee now 1.00 (currency not reported)');
    expect(unknown).not.toMatch(/fee now 1\.000000/);
  });

  it('says when there is nothing pending', () => {
    const empty = { ...feeRevision, pending_estimates: [], open_buy_envelopes: [], additional_cost_by_currency: {} };
    const t = text(renderToStaticMarkup(createElement(ScenarioDetails, { body: empty, scenario: 'fee_revision' })));
    expect(t).toContain('No pending fee estimates to finalise.');
    expect(t).toContain('No open buy envelopes to recompute.');
  });

  it('reports the fee reaction as booked commissions, not as a valuation with no stop latch', () => {
    const t = text(renderToStaticMarkup(createElement(CoreReaction, { body: feeRevision })));
    expect(t).not.toContain('no stop latch');
    expect(t).not.toContain('valuation command ran');
    expect(t).toContain('booked the revised final fees as broker commissions');
    expect(t).toContain('BrokerCommissionBooked');
    expect(t).toMatch(/Available cash after, .*S1.* \+87\.00 USD/);
    expect(t).toContain('1 open exception(s)');
    expect(t).toContain('FEE_ABOVE_ENVELOPE');
  });

  it('keeps the valuation reaction for the shock scenarios', () => {
    const shock = { core_reaction: { computed_on_core_seq: 57, events: [], stop_latched_after: false, valuation_ok_after: true, breach_after: null } };
    const t = text(renderToStaticMarkup(createElement(CoreReaction, { body: shock })));
    expect(t).toContain('no stop latch');
    expect(t).toContain('ran on a copy of core seq 57');
  });
});

describe('stale quotes: fresh NAV before and after', () => {
  it('shows the fresh NAV becoming unavailable', () => {
    const body: Rec = {
      kind: 'stress',
      scenario: 'stale_quote',
      parameters: { delay_s: 900, haircut: '-0.05', order_freshness_budget_ms: 5000 },
      became_stale: [{ owner: 'session:S1', instrument_id: 'FIX-EQ-1', age_ms: 900_100 }],
      fresh_nav_before: { USD: '1000.000000' },
      fresh_nav_after: { USD: null },
      valuation_status_after: { USD: 'stale_marks' },
      conservative_values: [{ owner: 'session:S1', instrument_id: 'FIX-EQ-1', last_mark: '10.00', age_ms: 900_100, value_at_last_mark: '100.000000', value_after_haircut: '95.000000' }],
      order_gate: [],
    };
    const t = text(renderToStaticMarkup(createElement(ScenarioDetails, { body, scenario: 'stale_quote', instrumentCurrency: () => 'USD' })));
    expect(t).toContain('Fresh NAV, USD');
    expect(t).toMatch(/1,000\.00 USD → —.*STALE: .*Not every mark is fresh\./);
    expect(t).toContain('after a -0.05 haircut 95.00 USD');
    expect(scenarioParams(body)).toBe('delay_s 900, haircut -0.05, order_freshness_budget_ms 5000');
  });
});

describe('shock scenarios: positions left out of the scenario P&L', () => {
  it('lists unpriced positions instead of folding them silently into the total', () => {
    const body: Rec = {
      pnl_by_currency: { USD: '-10.000000' },
      pnl_by_owner: [{ owner: 'session:S1', currency: 'USD', pnl: '-10.000000' }],
      unmapped: [{ instrument_id: 'FIX-EQ-9', owner: 'session:S1', reason: 'unpriced: cannot be revalued', cost_basis: '50.00' }],
    };
    const t = text(renderToStaticMarkup(createElement(PnlSummary, { body })));
    expect(t).toContain('1 position(s) not revalued and left out of the scenario P&amp;L');
    expect(t).toContain('FIX-EQ-9 (session:S1): unpriced: cannot be revalued');
  });

  it('shows nested parameters only in their own panel', () => {
    expect(scenarioParams({ parameters: { gap: '-0.10', per_instrument: {} } })).toBe('gap -0.10');
    expect(scenarioParams({})).toBe('');
  });
});

describe('a result is read against what it was computed for, not the form', () => {
  it('lays out a stress body by its own scenario while a different run loads', () => {
    // The page keeps the previous body while the next run loads; the form may already say fee_revision.
    expect(bodyScenario({ kind: 'stress', scenario: 'overnight_gap' })).toBe('overnight_gap');
    expect(bodyScenario({ kind: 'stress', scenario: 'fee_revision' })).toBe('fee_revision');
    expect(bodyScenario({ kind: 'stress', scenario: 'something_else' })).toBeNull();
    expect(bodyScenario(null)).toBeNull();
  });

  it('states the what-if intent the API evaluated', () => {
    const intent = { account_key: 'SIM-1', session_id: 'ses-1', order_id: 'whatif-intent-1', instrument_id: 'FIX-EQ-1', side: 'BUY', quantity: '1', limit_price: '10.05' };
    expect(intentText(intent)).toBe('BUY 1 FIX-EQ-1 at limit 10.05 for session ses-1');
    expect(intentText(undefined)).toBe('not reported by the API');
  });
});
