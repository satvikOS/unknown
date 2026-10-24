/** The unavailable-value primitive: em dash + reason, ERR, stale age; reasons the API sends are shown verbatim. */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { describeReason, ErrorValue, InGridProvider, NoValue, orNoValue, REASON_CODES, reasonSentence, StaleValue, toReason } from './unavailable';

const html = (el: Parameters<typeof renderToStaticMarkup>[0]) => renderToStaticMarkup(el);

describe('unavailable values', () => {
  it('renders an em dash in the muted token with the reason in the tooltip, data attribute and hidden text', () => {
    for (const reason of REASON_CODES.filter((r) => r !== 'ERROR')) {
      const out = html(createElement(NoValue, { reason, label: 'Bid' }));
      expect(out).toContain('nv-missing');
      expect(out).toContain('>—<');
      expect(out).toContain(`data-reason="${reason}"`);
      expect(out).toContain(`title="${describeReason(reason)}"`);
      expect(out).toContain(`Bid: not available. ${reason}:`);
      expect(out).not.toMatch(/N\/A|>0</);
    }
  });

  it('shows an unknown API reason verbatim', () => {
    expect(reasonSentence('LINE_BUDGET')).toBe('Reported by the source as LINE_BUDGET.');
    const out = html(createElement(NoValue, { reason: 'LINE_BUDGET', detail: 'over 100 lines' }));
    expect(out).toContain('data-reason="LINE_BUDGET"');
    expect(out).toContain('over 100 lines');
    expect(toReason('')).toBe('ERROR');
    expect(toReason(' NOT_ENTITLED ')).toBe('NOT_ENTITLED');
  });

  it('renders an error as ERR in the alert token, never as a dash', () => {
    const out = html(createElement(ErrorValue, { reason: 'ERROR', detail: 'HTTP 503' }));
    expect(out).toContain('nv-error');
    expect(out).toContain('>ERR<');
    expect(out).toContain('HTTP 503');
    expect(html(createElement(NoValue, { reason: 'ERROR' }))).toContain('>ERR<');
  });

  it('keeps a stale value with its age in the warning token', () => {
    const out = html(createElement(StaleValue, { ageMs: 4200, children: '10.00' }));
    expect(out).toContain('nv-stale');
    expect(out).toContain('10.00');
    expect(out).toContain('4.2 s');
    expect(out).toContain('data-reason="STALE"');
  });

  it('is a tab stop only when asked and never inside a grid', () => {
    expect(html(createElement(NoValue, { reason: 'NO_SOURCE' }))).not.toContain('tabindex');
    expect(html(createElement(NoValue, { reason: 'NO_SOURCE', focusable: true }))).toContain('tabindex="0"');
    expect(html(createElement(InGridProvider, null, createElement(NoValue, { reason: 'NO_SOURCE', focusable: true })))).not.toContain('tabindex');
  });

  it('orNoValue passes real values through and replaces absent ones', () => {
    expect(orNoValue('x', 'NOT_REPORTED')).toBe('x');
    expect(html(createElement('span', null, orNoValue(null, 'NOT_REPORTED')))).toContain('data-reason="NOT_REPORTED"');
    expect(html(createElement('span', null, orNoValue('', 'NO_SOURCE')))).toContain('data-reason="NO_SOURCE"');
  });
});
