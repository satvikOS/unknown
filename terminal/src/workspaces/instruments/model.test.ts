import { describe, expect, it } from 'vitest';
import type { Instrument } from '../../api/types';
import { attributeEntries, instrumentFlags, isFixture, mappingRows, matchInstrument, settlementLabel, tickAt, tickBands, tickSummary } from './model';

/** Shape copied from GET /api/instruments on a bootstrapped simulated core. */
const FIXA: Instrument = {
  asset_class: 'EQUITY',
  attributes: { fixture: true },
  currency: 'USD',
  instrument_id: 'FIX-EQ-1',
  inverse: false,
  leveraged: false,
  metadata_version: 1,
  min_quantity: '1',
  multiplier: '1',
  name: 'Fixture Equity A (simulation only)',
  quantity_increment: '1',
  settlement_currency: 'USD',
  settlement_days: 1,
  status: 'ACTIVE',
  symbol: 'FIXA',
  tick_rules: [
    { from_price: '1', tick: '0.01' },
    { from_price: '0', tick: '0.0001' },
  ],
  venue_mic: 'XNAS',
  mappings: [{ details: {}, external_id: 'FIX-EQ-1', instrument_id: 'FIX-EQ-1', provider: 'simulated', verified_at_ns: null, version: 1 }],
};

describe('tick rules', () => {
  it('orders rules into contiguous bands by from_price, exactly', () => {
    expect(tickBands(FIXA.tick_rules)).toEqual([
      { from: '0', to: '1', tick: '0.0001' },
      { from: '1', to: null, tick: '0.01' },
    ]);
    expect(tickSummary(FIXA.tick_rules)).toBe('0.0001 below 1 · 0.01 from 1');
    expect(tickSummary([{ from_price: '0', tick: '0.05' }])).toBe('0.05 from 0');
    expect(tickSummary([])).toBe('no tick rules');
  });

  it('finds the tick that applies at a price, including band edges', () => {
    expect(tickAt(FIXA.tick_rules, '0.9999')).toBe('0.0001');
    expect(tickAt(FIXA.tick_rules, '1')).toBe('0.01');
    expect(tickAt(FIXA.tick_rules, '1.00')).toBe('0.01');
    expect(tickAt(FIXA.tick_rules, '10.37')).toBe('0.01');
    expect(tickAt([{ from_price: '5', tick: '0.05' }], '4.99')).toBeNull();
    expect(tickAt(FIXA.tick_rules, null)).toBeNull();
    expect(tickAt(FIXA.tick_rules, 'abc')).toBeNull();
  });

  it('ignores malformed rules instead of mis-sorting them', () => {
    expect(tickBands([{ from_price: 'x', tick: '0.01' }, { from_price: '0', tick: '0.01' }])).toEqual([{ from: '0', to: null, tick: '0.01' }]);
  });
});

describe('flags and labels', () => {
  it('flags fixture data and non-active, leveraged or inverse products', () => {
    expect(isFixture(FIXA)).toBe(true);
    expect(isFixture({ attributes: {} })).toBe(false);
    expect(isFixture({ attributes: { fixture: 'true' } })).toBe(true);
    expect(instrumentFlags(FIXA).map((f) => f.label)).toEqual(['FIXTURE']);
    const odd = { ...FIXA, attributes: {}, status: 'HALTED', leveraged: true, inverse: true };
    expect(instrumentFlags(odd).map((f) => f.label)).toEqual(['HALTED', 'LEVERAGED', 'INVERSE']);
  });

  it('labels settlement cycles and orders attributes with the fixture marker first', () => {
    expect(settlementLabel(1)).toBe('T+1');
    expect(settlementLabel(0)).toBe('T+0');
    expect(attributeEntries({ 'ibkr.primaryExchange': 'NASDAQ', fixture: true, a: 1 }).map(([k]) => k)).toEqual(['fixture', 'a', 'ibkr.primaryExchange']);
    expect(attributeEntries(undefined)).toEqual([]);
    // A *_ns attribute is listed once, with the API's exact digits (JSON.parse rounded the number).
    const parsed = JSON.parse('{"listed_ns": 1792675849903999999, "listed_ns_str": "1792675849903999999", "lot": 1}') as Record<string, unknown>;
    expect(attributeEntries(parsed)).toEqual([['listed_ns', '1792675849903999999'], ['lot', 1]]);
  });
});

describe('mappings and search', () => {
  it('marks the highest version per provider as current', () => {
    const rows = mappingRows([
      { provider: 'ibkr', external_id: '265598', version: 1, instrument_id: 'X', verified_at_ns: null, details: {} },
      { provider: 'ibkr', external_id: '265599', version: 2, instrument_id: 'X', verified_at_ns: 1, details: {} },
      { provider: 'figi', external_id: 'BBG000B9XRY4', version: 1, instrument_id: 'X', verified_at_ns: null, details: {} },
    ]);
    expect(rows.map((r) => [r.provider, r.version, r.current])).toEqual([
      ['figi', 1, true],
      ['ibkr', 2, true],
      ['ibkr', 1, false],
    ]);
  });

  it('matches every token against symbol, name, ids, venue and provider ids', () => {
    expect(matchInstrument(FIXA, 'fixa')).toBe(true);
    expect(matchInstrument(FIXA, 'fixture xnas')).toBe(true);
    expect(matchInstrument(FIXA, 'simulated FIX-EQ-1')).toBe(true);
    expect(matchInstrument(FIXA, 'fixa nyse')).toBe(false);
    expect(matchInstrument(FIXA, '  ')).toBe(true);
  });
});
