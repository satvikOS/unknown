/**
 * Rendered answer (react-dom/server, no DOM): labelled AI, citations open their tool result, statements without a
 * source and numbers not found in the source are flagged, and a missing cost is an em dash with its reason.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AnswerView, LiveView } from './AskWorkspace';
import { initialLive, parseAnswer, type Answer } from './model';

const answer = parseAnswer({
  status: 'answered',
  label: 'AI-generated, advisory only: no order, session or money movement can follow from it.',
  text: '## Latest 10-Q\n- Revenue was 109,417,000,000 USD [R4].\n- Apple will beat estimates next quarter.\n- Margin was 51% [R4].',
  check: {
    lines: [
      { line: 1, refs: ['R4'], status: 'cited', numbers_unmatched: [] },
      { line: 2, refs: [], status: 'unsourced' },
      { line: 3, refs: ['R4'], status: 'cited', numbers_unmatched: ['51%'] },
    ],
    cited: 2, unsourced: 1, invalid_refs: [], numbers_unmatched: 1,
  },
  citations: [{ ref: 'R4', tool: 'company_financials', arguments: { cik: '320193' }, decision: 'executed', rule_id: 'allowed',
    datasets: ['sec.companyfacts'], status: 'ok' }],
  model: 'claude-opus-5-5', effort: 'medium', usage: { input_tokens: 8, output_tokens: 2306, cache_read_tokens: 6682, cache_write_tokens: 17728 },
  cost: '0.1361284000', currency: 'USD', cost_exact: true,
}) as Answer;

describe('ask answer view', () => {
  it('labels the answer AI, links citations and flags unsourced lines and unmatched numbers', () => {
    const html = renderToStaticMarkup(createElement(AnswerView, { answer, onOpen: () => undefined }));
    expect(html).toContain('>AI<');
    expect(html).toContain('advisory only');
    expect(html).toContain('title="Open tool result R4"');
    expect(html).toContain('no source');
    expect(html).toContain('not in source: 51%');
    expect(html).toContain('claude-opus-5-5');
    expect(html).toContain('0.1361284 USD');
    expect(html).toContain('2,306');
  });

  it('shows a cancel control while running and an em dash for a cost not yet known', () => {
    const html = renderToStaticMarkup(createElement(LiveView, { live: { ...initialLive('ask-x'), phase: 'running' }, onOpen: () => undefined,
      onCancel: () => undefined, cancelling: false }));
    expect(html).toContain('>Cancel<');
    expect(html).toContain('—');
    expect(html).toContain('data-reason="PENDING"');
  });
});
