/** The chart never shows an example security: with nothing linked or in the route it asks for one. */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ChartWorkspace } from './ChartWorkspace';

describe('chart without a security', () => {
  it('renders an empty state that asks for a security, and requests no bars', () => {
    const html = renderToStaticMarkup(
      createElement(ChartWorkspace, { route: { workspace: 'chart', rest: [] }, navigate: () => undefined } as never),
    );
    expect(html).toContain('No security is linked.');
    expect(html).toContain('aria-label="Open security"');
    expect(html).not.toMatch(/IBM|example/i);
  });
});
