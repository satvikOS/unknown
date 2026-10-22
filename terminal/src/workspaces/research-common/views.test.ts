/**
 * Rendered shared parts (react-dom/server, no browser): what the owner reads, checked as text.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { RangeBars } from './charts';
import { JsonTree } from './parts';

describe('JSON tree', () => {
  it('never shows an integer the browser rounded as if it were exact', () => {
    // What JSON.parse makes of a nanosecond timestamp such as a pre-registration's registered_at_ns.
    const parsed = JSON.parse('{"registered_at_ns": 1761148672134721234, "seed": 11, "share": 0.25}') as Record<string, unknown>;
    expect(parsed.registered_at_ns).toBe(1761148672134721300);
    const html = renderToStaticMarkup(createElement(JsonTree, { value: parsed }));
    expect(html).toContain('≈1761148672134721300');
    expect(html).toContain('approximate');
    // Safe integers and floats are shown as they are.
    expect(html).toMatch(/>11</);
    expect(html).not.toContain('≈11');
    expect(html).toMatch(/>0\.25</);
  });
});

describe('range chart', () => {
  it('draws the reference marker (the owner price input) on the shared axis, with its exact text', () => {
    const rows = [{ label: 'WACC x g grid', min: 93.4126, max: 244.3858, minText: '93.4126', maxText: '244.3858' }];
    const html = renderToStaticMarkup(
      createElement(RangeBars, { title: 't', summary: 's', rows, format: String, unit: 'USD per share', marker: { value: 50, text: '50.00', label: 'price input' } }),
    );
    expect(html).toContain('price input 50.00');
    expect(html).toContain('<title>price input: 50.00 USD per share</title>');
    const none = renderToStaticMarkup(createElement(RangeBars, { title: 't', summary: 's', rows, format: String, unit: 'USD per share' }));
    expect(none).not.toContain('price input');
  });
});
