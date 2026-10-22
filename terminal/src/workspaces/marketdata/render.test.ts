/**
 * Rendered subscription detail (react-dom/server, no DOM). The analytics block follows
 * api/routes/marketdata.py `quote_analytics`: mid and spread are prices per share in the quote's
 * currency (`inputs.currency`). Every id and price is a TEST FIXTURE.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SubscriptionDetail } from './MarketDataWorkspace';
import { parseSubscription } from './model';

function sub(currency: string | null) {
  const s = parseSubscription({
    instrument_id: 'FIX-MD-LIVE',
    req_id: 1,
    current: true,
    status: 'LIVE',
    live: true,
    requested_type: 'live',
    delivered_type: 1,
    delivered_type_name: 'live',
    consolidation: 'non_consolidated',
    ticks: 4,
    analytics: {
      inputs: { bid: '10.01', ask: '10.03', currency },
      quote_state: 'normal',
      mid: '10.020',
      spread: '0.02',
      spread_bps: '19.9601',
      age_ms: 1200,
      stale: { display: false },
      live: true,
    },
  });
  if (!s) throw new Error('fixture did not parse');
  return s;
}

describe('subscription detail: quote analytics', () => {
  it('shows mid and spread with the quote currency', () => {
    const html = renderToStaticMarkup(createElement(SubscriptionDetail, { s: sub('USD') }));
    expect(html).toContain('mid 10.02 USD');
    expect(html).toContain('spread 0.02 USD');
    expect(html).toContain('19.9601 bps');
  });

  it('says when the quote currency is not reported instead of showing a bare price', () => {
    const html = renderToStaticMarkup(createElement(SubscriptionDetail, { s: sub(null) }));
    expect(html).toContain('mid 10.02 (currency not reported)');
  });
});
