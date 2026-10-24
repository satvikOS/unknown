/**
 * Rendered note (react-dom/server, no DOM): the AI label names the model, the overview is marked uncited, every
 * unverified line is flagged, unsent sources say so, and missing data renders the Unavailable em dash with its reason.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { NoteBody } from './NoteView';
import { parseNoteDetail } from './model';

const RAW = {
  note: {
    note_id: 'note-1',
    run_id: 'aip-brief-1',
    process: 'brief',
    created_ns: 1_792_815_131_918_007_000,
    title: 'Morning brief 2026-10-24',
    status: 'ok',
    content_hash: 'a'.repeat(64),
    content_hash_ok: true,
    body: {
      label: { kind: 'AI', model_ids: ['model-x'], prompt_template_hash: 'b'.repeat(64), generated: 'g', advisory: 'AI-generated advisory note.' },
      headline: 'Three routine filings.',
      lines: [
        { text: 'Filed three 424B2 supplements.', status: 'verified', citations: ['news:108'], quote: '424B2', checks: [] },
        { text: 'Markets will rally.', status: 'unsupported', citations: [], quote: null, checks: ['no_citation'] },
      ],
      data_lines: [],
      items: [
        { citation: 'news:108', title: '424B2 - JPMorgan', kind: 'filing', form: '424B2', why: 'filing', sent_to_model: true },
        { citation: 'news:223', title: 'ECB survey', kind: 'central_bank', why: 'central-bank release', sent_to_model: false, not_sent_reason: 'local only' },
      ],
      unavailable: [{ what: 'price move for JPM', reason: 'no stored daily bars' }],
      citations: { 'news:108': { kind: 'filing', title: '424B2 - JPMorgan' } },
      ai: { status: 'ok', reason: 'ok' },
      cost: { amount: '0.01893200', currency: 'USD', prompt_tokens: 2543, output_tokens: 438 },
    },
  },
};

describe('AI note rendering', () => {
  it('labels AI text, flags unverified lines and marks unsent sources and missing data', () => {
    const note = parseNoteDetail(RAW);
    if (!note) throw new Error('fixture did not parse');
    const html = renderToStaticMarkup(createElement(NoteBody, { note, onCite: () => undefined }));
    expect(html).toContain('AI · model-x');
    expect(html).toContain('overview, uncited');
    expect(html).toContain('No source: this line cites nothing the terminal can open.');
    expect(html.match(/class="flagged"/g)?.length).toBe(1);
    expect(html).toContain('not sent');
    expect(html).toContain('—');
    expect(html).toContain('no stored daily bars');
    expect(html).toContain('0.018932 USD');
  });
});
