/** Monitor model: keys, watchlist editing and persistence, exact change arithmetic, source labels. Fixture data. */
import { describe, expect, it } from 'vitest';
import {
  addKey, cumulative, defaultState, direction, loadState, moveKey, normaliseKey, percentChange, removeKey, rowFor,
  saveState, sourceBadge, splitKey, subtract, DEFAULT_KEYS,
} from './model';

describe('monitor model', () => {
  it('parses and normalises security keys like the backend', () => {
    expect(splitKey('AAPL.XNAS')).toEqual({ symbol: 'AAPL', mic: 'XNAS' });
    expect(splitKey('SPY.ARCX')).toEqual({ symbol: 'SPY', mic: 'ARCX' });
    expect(splitKey('BRK.B.XNYS')).toEqual({ symbol: 'BRK.B', mic: 'XNYS' });
    expect(splitKey('FIX-INSTR-1')).toEqual({ symbol: 'FIX-INSTR-1', mic: null });
    expect(normaliseKey(' aapl.xnas ')).toBe('AAPL.XNAS');
    expect(normaliseKey('nvda')).toBe('NVDA.XNAS');
    expect(normaliseKey('bad key')).toBeNull();
  });

  it('edits watchlists and refuses duplicates and invalid keys', () => {
    const list = defaultState().lists[0]!;
    expect(list.keys).toEqual(DEFAULT_KEYS);
    expect(addKey(list, 'AAPL.XNAS').error).toMatch(/already/);
    expect(addKey(list, 'not a key!').error).toMatch(/not a security key/);
    const added = addKey(list, 'spy.arcx').list;
    expect(added.keys.at(-1)).toBe('SPY.ARCX');
    expect(moveKey(added, 'SPY.ARCX', -1).keys.at(-2)).toBe('SPY.ARCX');
    expect(removeKey(added, 'SPY.ARCX').keys).toEqual(DEFAULT_KEYS);
  });

  it('persists lists and survives broken storage', () => {
    const mem = new Map<string, string>();
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) };
    const state = defaultState(['FIX-1.XNAS']);
    expect(saveState(state, store)).toBe(true);
    expect(loadState(store)).toEqual(state);
    mem.set('unknown.monitor.v1', '{"version":1,"lists":[{"id":"x","name":"x","keys":["OK.XNAS","bad key"]}],"active":"y"}');
    expect(loadState(store)).toEqual({ version: 1, lists: [{ id: 'x', name: 'x', keys: ['OK.XNAS'] }], active: 'x' });
    const broken = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('full'); } };
    expect(loadState(broken)).toBeNull();
    expect(saveState(state, broken)).toBe(false);
  });

  it('computes change and percent change exactly (half-even), never with floats', () => {
    expect(subtract('10.10', '9.9')).toBe('0.20');
    expect(subtract('0.1', '0.3')).toBe('-0.2');
    expect(subtract('187.21', '185.00')).toBe('2.21');
    expect(percentChange('101', '100')).toBe('1.00');
    expect(percentChange('99.995', '100')).toBe('0.00');
    expect(percentChange('99.985', '100')).toBe('-0.02');
    expect(percentChange('1.00005', '1', 4)).toBe('0.0050');
    expect(percentChange('100.125', '100', 3)).toBe('0.125');
    expect(percentChange('100.0125', '100', 3)).toBe('0.012');
    expect(percentChange('1', '0')).toBeNull();
    expect(percentChange(null, '1')).toBeNull();
    expect(direction('10.00', '10.01')).toBe(1);
    expect(direction('10.01', '10.0100')).toBe(0);
    expect(direction(null, '1')).toBeNull();
    expect(cumulative(['100', null, '250'])).toEqual(['100', '100', '350']);
  });

  it('labels every row with its source; a non-market data type is an error badge', () => {
    const bad = sourceBadge({ source: 'GEN', data_type: 'SYNTHETIC' as never });
    expect(bad).toMatchObject({ text: 'ERR', tone: 'bad' });
    expect(bad.title).toMatch(/data type SYNTHETIC is not real market data/);
    expect(sourceBadge({ source: 'IBKR', data_type: 'DELAYED' })).toMatchObject({ text: 'IBKR DELAYED', tone: 'warn' });
    expect(sourceBadge({ source: 'ALPACA', data_type: 'REALTIME', feed: 'alpaca:iex' }).title).toMatch(/alpaca:iex/);
    expect(sourceBadge(undefined).text).toBe('NO DATA');
    const r = rowFor('AAPL.XNAS', undefined);
    expect([r.last, r.chg, r.pct, r.badge.text]).toEqual([null, null, null, 'NO DATA']);
  });
});
