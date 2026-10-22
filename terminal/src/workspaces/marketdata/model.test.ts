/** Market-data status model. Shapes follow GET /api/marketdata/status; every value is a TEST FIXTURE. */
import { describe, expect, it } from 'vitest';
import {
  connectionSummary,
  consolidationText,
  countSubscriptions,
  deliveredVsRequested,
  entitlementIssues,
  lineUsePercent,
  parseStatus,
  parseSubscription,
  statusTone,
  type Subscription,
} from './model';

const FEED = { consolidation: 'non_consolidated', label: 'Fixture non-consolidated feed', evidence: 'fixture', observed_on: '2026-10-22' };

function sub(over: Record<string, unknown>): Record<string, unknown> {
  return {
    instrument_id: 'FIX-MD-LIVE',
    req_id: 1,
    current: true,
    contract: { conId: 900101, currency: 'USD', exchange: 'SMART', primaryExchange: '', secType: 'STK', symbol: '' },
    status: 'LIVE',
    live: true,
    requested_type: 'live',
    delivered_type: 1,
    delivered_type_name: 'live',
    source: 'ibkr:live:non_consolidated',
    consolidation: 'non_consolidated',
    feed: FEED,
    unavailable: null,
    connection_stale: null,
    resubscribe_required: false,
    cancel_required: false,
    quote_state: 'normal',
    age_ms: 0,
    ticks: 4,
    warnings: [],
    errors: [],
    fields: { bid: { present: true, null_reason: null, receipt_ns: 1 }, ask: { present: false, null_reason: 'no data currently available', receipt_ns: 1 } },
    analytics: null,
    analytics_missing_reason: 'no IBKR quote for this instrument in the core quote book (session not wired to the core, or no data yet)',
    ...over,
  };
}

const STATUS = {
  provider: 'ibkr',
  now_ns: 1792686634731855000,
  status_file: '/fixture/run/ibkr-marketdata-status.json',
  evidence_level: 'offline-tested; NOT paper-verified (ADR-0001)',
  available: true,
  status_file_age_ms: 6085,
  status_file_stale: false,
  status_file_stale_reason: null,
  connection: { account: 'DU******E1', client_id: 18, connected: true, data_farms_down: [], host: '127.0.0.1:4002', reason: null, upstream_down: null, verified_paper: true },
  line_budget: { capacity: 100, headroom: 96, in_use: 4, lines: 100, refused_locally: 0, tws_reserved_lines: 0, formula: 'mkt.line_headroom@1' },
  pacing: { in_window: 7, max_per_second: 1000, reserved: 200, violations: 0 },
  requested_market_data_type: 'live',
  entitlement_error_rate: { denominator: 7, numerator: 3, value: '3/7', formula: 'ibkr.entitlement_error_rate@1' },
  freshness_budgets_ms: { display: 5000, pre_trade: 2000, bad: 'x' },
  subscriptions: [
    sub({}),
    sub({ instrument_id: 'FIX-MD-DELAYED', status: 'DELAYED', live: false, delivered_type: 3, delivered_type_name: 'delayed', consolidation: 'unknown' }),
    sub({
      instrument_id: 'FIX-MD-NOSUB',
      status: 'UNAVAILABLE',
      live: false,
      delivered_type: null,
      delivered_type_name: 'unreported',
      consolidation: 'unknown',
      unavailable: { code: 354, state: 'NOT_SUBSCRIBED', cause: 'Not subscribed to requested market data', text: 'fixture', at_ns: 1 },
    }),
    sub({ instrument_id: 'FIX-MD-PARTIAL', warnings: ['IBKR 10090: Part of requested market data is not subscribed'] }),
    sub({ instrument_id: 'FIX-MD-OLD', current: false, status: 'CANCELLED', live: false }),
    { instrument_id: 7 },
  ],
  audit: [{ at_ns: 1, instrument_id: null, kind: 'SESSION_READY', detail: 'PAPER identity verified; client 18' }],
  anomalies: [],
  storage: 'no price values are written by the market-data session (CAP-IBKR-12)',
  written_at_ns: 1792686628646056000,
};

describe('parseStatus', () => {
  it('reads the unavailable answer with its reason', () => {
    const s = parseStatus({ provider: 'ibkr', available: false, unavailable_reason: 'the IBKR market-data session is not running', evidence_level: 'offline-tested' });
    expect(s).toMatchObject({ available: false, unavailable_reason: 'the IBKR market-data session is not running', subscriptions: [] });
    expect(parseStatus({ provider: 'ibkr' })).toBeNull();
  });

  it('reads a full status, keeping only numeric budgets and well-formed subscriptions', () => {
    const s = parseStatus(STATUS);
    expect(s?.connection).toMatchObject({ connected: true, verified_paper: true, account: 'DU******E1' });
    expect(s?.freshness_budgets_ms).toEqual({ display: 5000, pre_trade: 2000 });
    expect(s?.subscriptions).toHaveLength(5);
    expect(s?.subscriptions[0]?.fields).toEqual([
      { name: 'bid', present: true, null_reason: null },
      { name: 'ask', present: false, null_reason: 'no data currently available' },
    ]);
    expect(s?.entitlement_error_rate?.value).toBe('3/7');
    expect(s?.subscriptions[0]?.con_id).toBe(900101);
  });

  it('keeps the original status of a subscription the API marked stale', () => {
    const p = parseSubscription(sub({ status: 'STATUS_FILE_STALE', live: false, reported_status: 'LIVE', reported_live: true, live_reason: 'status older than 60000 ms' }));
    expect(p).toMatchObject({ status: 'STATUS_FILE_STALE', reported_status: 'LIVE', live: false, live_reason: 'status older than 60000 ms' });
  });
});

describe('presentation', () => {
  const s = parseStatus(STATUS);
  const subs = s?.subscriptions ?? [];

  it('maps statuses to tones with only LIVE as ok', () => {
    expect(statusTone('LIVE')).toBe('ok');
    for (const st of ['DELAYED', 'DELAYED_FROZEN', 'FROZEN', 'STALE', 'STATUS_FILE_STALE', 'TYPE_UNREPORTED']) expect(statusTone(st)).toBe('warn');
    expect(statusTone('UNAVAILABLE')).toBe('bad');
    expect(statusTone('TYPE_MISMATCH')).toBe('bad');
    expect(statusTone('CANCELLED')).toBe('neutral');
  });

  it('never presents a non-consolidated feed as the national best bid/offer', () => {
    expect(consolidationText('non_consolidated').detail).toMatch(/not the national best bid\/offer/);
    expect(consolidationText('non_consolidated').detail).toMatch(/QUOTE_CONSOLIDATED/);
    expect(consolidationText('consolidated').tone).toBe('ok');
    expect(consolidationText('something').label).toBe('unknown');
  });

  it('shows the delivered type and flags a difference from the requested type', () => {
    expect(deliveredVsRequested(subs[0] as Subscription)).toEqual({ differs: false, text: 'live' });
    expect(deliveredVsRequested(subs[1] as Subscription)).toEqual({ differs: true, text: 'delayed (requested live)' });
    expect(deliveredVsRequested({ requested_type: 'live', delivered_type_name: null })).toEqual({ differs: true, text: 'unreported (requested live)' });
  });

  it('summarizes the connection, failing closed on a stale status file', () => {
    expect(connectionSummary(s?.connection ?? null, false).label).toBe('connected · paper verified');
    expect(connectionSummary(s?.connection ?? null, true).label).toBe('status stale');
    expect(connectionSummary({ ...(s?.connection as NonNullable<typeof s>['connection'] & object), data_farms_down: ['usfarm'] }, false).tone).toBe('warn');
    expect(connectionSummary({ connected: false, verified_paper: false, reason: 'HOST_DOWN', upstream_down: null, data_farms_down: [], client_id: 18, account: null, host: null, evidence_level: null }, false)).toMatchObject({
      label: 'not connected',
      detail: 'HOST_DOWN',
      tone: 'bad',
    });
    expect(connectionSummary(null, false).tone).toBe('bad');
  });

  it('lists entitlement problems: unavailable states block, 10090 warns', () => {
    expect(entitlementIssues(subs)).toEqual([
      { instrument_id: 'FIX-MD-NOSUB', code: 354, state: 'NOT_SUBSCRIBED', cause: 'Not subscribed to requested market data', blocking: true },
      { instrument_id: 'FIX-MD-PARTIAL', code: 10090, state: 'PARTIALLY_SUBSCRIBED', cause: 'Part of requested market data is not subscribed', blocking: false },
    ]);
  });

  it('counts current subscriptions only, and the line use', () => {
    expect(countSubscriptions(subs)).toEqual({ total: 4, live: 2, delayed: 1, unavailable: 1, other: 0 });
    expect(lineUsePercent(s?.line_budget ?? null)).toBe(4);
    expect(lineUsePercent(null)).toBeNull();
    // A stale status file: statuses are hidden behind STATUS_FILE_STALE, nothing is live, the reported status still counts.
    const stale = subs.map((x) => ({ ...x, status: 'STATUS_FILE_STALE', reported_status: x.status, live: false }));
    expect(countSubscriptions(stale)).toEqual({ total: 4, live: 0, delayed: 1, unavailable: 1, other: 2 });
  });
});
