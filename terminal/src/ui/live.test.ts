import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { linkedSecurity, LinkChannelProvider, parseSecurityKey, setLinkedSecurity, subscribeLinkedSecurity, useLinkedSecurity } from './link';
import { compareDecimal, flashElement, PriceCell, tickDirection } from './live';
import { registerStreamHealthProvider, streamHealth, type StreamHealth } from './streamHealth';
import { headerWidgets, registerHeaderWidget } from './headerWidgets';

describe('live numbers', () => {
  it('compares decimal strings exactly, never through floats', () => {
    expect(compareDecimal('0.1', '0.10')).toBe(0);
    expect(compareDecimal('100.000000000000000001', '100')).toBe(1);
    expect(compareDecimal('-1.5', '-1.25')).toBe(-1);
    expect(compareDecimal('-0', '0.00')).toBe(0);
    expect(compareDecimal('9', '10')).toBe(-1);
    expect(compareDecimal('abc', '1')).toBeNull();
    expect(tickDirection('187.20', '187.21')).toBe('up');
    expect(tickDirection('187.21', '187.2')).toBe('down');
    expect(tickDirection(null, '1')).toBe('unch');
  });

  it('restarts the flash class on the element without React', () => {
    const classes = new Set<string>(['tick-flash-down']);
    const attrs: Record<string, string> = {};
    const el = {
      classList: { add: (c: string) => classes.add(c), remove: (...cs: string[]) => cs.forEach((c) => classes.delete(c)) },
      setAttribute: (k: string, v: string) => (attrs[k] = v),
      offsetWidth: 10,
    } as unknown as Element;
    flashElement(el, 'up');
    expect([...classes]).toEqual(['tick-flash-up']);
    expect(attrs['data-dir']).toBe('up');
    flashElement(el, 'unch');
    expect([...classes]).toEqual(['tick-flash-up']);
  });

  it('renders a missing value as an em dash with its reason, never as zero', () => {
    const html = (props: Parameters<typeof PriceCell>[0]) => renderToStaticMarkup(createElement(PriceCell, props));
    const missing = html({ value: null, reason: 'NOT_ENTITLED', label: 'AAPL.XNAS last' });
    expect(missing).toContain('—');
    expect(missing).toContain('data-reason="NOT_ENTITLED"');
    expect(missing).toContain('AAPL.XNAS last: not available');
    expect(missing).not.toMatch(/>0</);
    expect(html({ value: undefined })).toContain('data-reason="NOT_REPORTED"');
    expect(html({ value: '1.00', dataType: 'DELAYED', badge: 'none' })).not.toContain('DLY');
    expect(html({ value: '1.00', dataType: 'DELAYED' })).toContain('DLY');
    expect(html({ value: '1.00', dataType: 'REALTIME' })).not.toContain('src-badge');
    const stale = html({ value: '1.00', stale: true, ageMs: 65_000 });
    expect(stale).toContain('nv-stale');
    expect(stale).toContain('1 m 05 s');
    expect(stale).toContain('1.00');
  });

  it('renders a value whose data type is not a real market-data type as an error, never as data', () => {
    const html = (props: Parameters<typeof PriceCell>[0]) => renderToStaticMarkup(createElement(PriceCell, props));
    for (const t of ['SYNTHETIC', 'FIXTURE', 'SAMPLE']) {
      const out = html({ value: '187.21', dataType: t, source: 'X' });
      expect(out).toContain('ERR');
      expect(out).toContain('nv-error');
      expect(out).not.toContain('187.21');
      expect(out).toContain(`data type ${t} from X is not real market data`);
    }
  });
});

describe('linked security channels', () => {
  it('keeps channels independent and notifies only the changed channel', () => {
    let calls = 0;
    const off = subscribeLinkedSecurity('ch3', () => (calls += 1));
    setLinkedSecurity('AAPL.XNAS', 'ch4');
    expect(calls).toBe(0);
    setLinkedSecurity('SAP.XETR', 'ch3');
    setLinkedSecurity('SAP.XETR', 'ch3');
    expect(calls).toBe(1);
    expect(linkedSecurity('ch3')).toBe('SAP.XETR');
    expect(linkedSecurity('ch4')).toBe('AAPL.XNAS');
    off();
  });

  it('reads the enclosing panel channel by default and keeps the old default-channel API', () => {
    setLinkedSecurity('RELIANCE.XNSE', 'ch5');
    setLinkedSecurity('MSFT.XNAS');
    const Probe = () => createElement('span', null, useLinkedSecurity() ?? 'none');
    expect(renderToStaticMarkup(createElement(Probe))).toBe('<span>MSFT.XNAS</span>');
    expect(renderToStaticMarkup(createElement(LinkChannelProvider, { channel: 'ch5' }, createElement(Probe)))).toBe('<span>RELIANCE.XNSE</span>');
    expect(parseSecurityKey('BRK.B.XNYS')).toEqual({ symbol: 'BRK.B', mic: 'XNYS' });
  });
});

describe('shell registries', () => {
  it('stream health is null until a market-data client registers a provider', () => {
    expect(streamHealth()).toBeNull();
    const snap: StreamHealth = { state: 'live', sources: [{ source: 'ANY', dataType: 'DELAYED', keys: 3 }], latencyMs: { p50: 4, p99: 20 }, lastEventMs: 1 };
    const off = registerStreamHealthProvider({ subscribe: () => () => undefined, getSnapshot: () => snap });
    expect(streamHealth()).toBe(snap);
    off();
    expect(streamHealth()).toBeNull();
  });

  it('header widgets register in order, replace by id and unregister', () => {
    const A = () => null;
    const offB = registerHeaderWidget({ id: 'b', label: 'B', component: A, order: 20 });
    const offA = registerHeaderWidget({ id: 'a', label: 'A', component: A, order: 10 });
    expect(headerWidgets().map((w) => w.id)).toEqual(['a', 'b']);
    registerHeaderWidget({ id: 'a', label: 'A2', component: A, order: 30 });
    expect(headerWidgets().map((w) => w.label)).toEqual(['B', 'A2']);
    offA();
    expect(headerWidgets()).toHaveLength(2);
    offB();
    expect(headerWidgets().map((w) => w.id)).toEqual(['a']);
  });
});
