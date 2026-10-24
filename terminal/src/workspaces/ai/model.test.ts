import { describe, expect, it } from 'vitest';
import { listFunctions, parseCommand } from '../../ui/functions';
import { findWorkspace, WORKSPACES } from '../registry';
import { capShare, inboxRoute, lineFlag, parseAiRoute, parseApprovals, parseDashboard, parseNoteDetail, parseNoteList, trimAmount } from './model';

const NOTE = {
  note: {
    note_id: 'note-1',
    run_id: 'aip-brief-1',
    process: 'brief',
    created_ns: 1,
    title: 'Morning brief 2026-10-23',
    status: 'ok',
    content_hash: 'a'.repeat(64),
    content_hash_ok: true,
    symbols: ['JPM'],
    ciks: ['19617'],
    body: {
      label: {
        kind: 'AI',
        model_ids: ['m-1'],
        prompt_template_hash: 'b'.repeat(64),
        generated: '2026-10-24T04:12:11Z',
        advisory: 'advisory',
      },
      headline: 'Three routine filings.',
      lines: [
        {
          text: 'Filed three 424B2.',
          status: 'verified',
          citations: ['news:108'],
          quote: '424B2',
          checks: [],
        },
        {
          text: 'Markets will rally.',
          status: 'unsupported',
          citations: [],
          quote: null,
          checks: ['no_citation'],
        },
        {
          text: 'Cited, unquoted.',
          status: 'uncertain',
          citations: ['news:108'],
          quote: null,
          checks: [],
        },
      ],
      data_lines: [],
      items: [
        {
          citation: 'news:108',
          title: '424B2 - JPMorgan',
          kind: 'filing',
          form: '424B2',
          published: null,
          why: 'filing',
          sent_to_model: true,
          not_sent_reason: null,
        },
      ],
      unavailable: [{ what: 'price move for JPM', reason: 'no stored daily bars' }],
      citations: {
        'news:108': {
          kind: 'filing',
          title: '424B2 - JPMorgan',
          link: 'https://www.sec.gov/x',
          open: { api: '/api/news/item?id=108' },
        },
      },
      window: { from: '2026-10-23T04:12:11Z', to: '2026-10-24T04:12:11Z' },
      ai: { status: 'ok', reason: 'ok' },
      cost: {
        amount: '0.01893200',
        currency: 'USD',
        prompt_tokens: 2543,
        output_tokens: 438,
      },
    },
  },
};

describe('ai workspace model', () => {
  it('parses routes and builds inbox routes', () => {
    expect(parseAiRoute([])).toEqual({
      view: 'inbox',
      process: null,
      symbol: null,
    });
    expect(parseAiRoute(['brief'])).toEqual({
      view: 'inbox',
      process: 'brief',
      symbol: null,
    });
    expect(parseAiRoute(['process=explainer', 'symbol=AAPL'])).toEqual({
      view: 'inbox',
      process: 'explainer',
      symbol: 'AAPL',
    });
    expect(parseAiRoute(['process=nope', 'symbol=<script>'])).toEqual({
      view: 'inbox',
      process: null,
      symbol: null,
    });
    expect(parseAiRoute(['note', 'note-1'])).toEqual({
      view: 'note',
      noteId: 'note-1',
    });
    expect(parseAiRoute(['approvals']).view).toBe('approvals');
    expect(parseAiRoute(['cost']).view).toBe('cost');
    expect(inboxRoute('summaries', 'JPM')).toEqual(['process=summaries', 'symbol=JPM']);
  });

  it('parses a note and flags every line that is not verified', () => {
    const n = parseNoteDetail(NOTE)!;
    expect(n.label?.kind).toBe('AI');
    expect(n.citations['news:108']?.apiPath).toBe('/api/news/item?id=108');
    expect(n.lines.map(lineFlag)).toEqual([
      null,
      'No source: this line cites nothing the terminal can open.',
      'Cited, but its wording was not checked against the source (no quote).',
    ]);
    expect(n.cost.amount).toBe('0.01893200');
    expect(parseNoteDetail({ note: { note_id: 'x' } })).toBeNull();
    expect(
      parseNoteDetail({
        note: {
          ...NOTE.note,
          body: { ...NOTE.note.body, lines: [{ status: 'verified' }] },
        },
      }),
    ).toBeNull();
  });

  it('refuses unexpected list shapes instead of guessing', () => {
    expect(
      parseNoteList({
        notes: [{ note_id: 'n', title: 't', lines: 2, flagged: 1 }],
      })?.[0]?.flagged,
    ).toBe(1);
    expect(parseNoteList({ notes: [{ title: 't' }] })).toBeNull();
    expect(parseNoteList({})).toBeNull();
    expect(
      parseApprovals({
        approvals: [
          {
            approval_id: 'apq-1',
            content_hash: 'c'.repeat(64),
            state: 'PENDING',
          },
        ],
      })?.[0]?.state,
    ).toBe('PENDING');
    expect(parseApprovals({ approvals: [{ approval_id: 'apq-1' }] })).toBeNull();
  });

  it('parses the dashboard and computes cap shares without rounding amounts', () => {
    const proc = {
      enabled: true,
      per_day: '0.50',
      per_run: '0.50',
      spent_today: '0.05',
      runs: 2,
      failed: 0,
      prompt_tokens: 10,
      output_tokens: 5,
      cost: '0.05',
      latency_ms: { p50: 1000, p95: 2000 },
      quality: {
        verified: 1,
        unsupported: 1,
        lines: 2,
        unsupported_rate: '50.0',
      },
      outcomes: { ok: 2 },
    };
    const d = parseDashboard({
      today: '2026-10-24',
      spend: {
        currency: 'USD',
        today: '0.05',
        by_day: [{ day: '2026-10-24', total: '0.05', by_role: { ai_brief: '0.05' } }],
      },
      processes: { brief: proc, summaries: proc, explainer: proc },
      audit_chain: { ok: true },
      approvals: { PENDING: 1 },
    })!;
    expect(d.processes.map((p) => p.process)).toEqual(['brief', 'summaries', 'explainer']);
    expect(d.byDay[0]?.byRole).toEqual({ ai_brief: '0.05' });
    expect(d.auditOk).toBe(true);
    expect(parseDashboard({ spend: {}, processes: {} })).toBeNull();
    expect(capShare('0.05', '0.50')).toBe(10);
    expect(capShare('1', null)).toBeNull();
    expect(trimAmount('0.01893200')).toBe('0.018932');
    expect(trimAmount('2.00000000')).toBe('2.00');
    expect(trimAmount('0.50')).toBe('0.50');
  });
});

describe('ai workspace registration', () => {
  it('registers AI notes (g l, 87) and the BRIEF, AIQ and AICOST functions', () => {
    expect(findWorkspace('ai')).toMatchObject({
      title: 'AI notes',
      shortcut: 'g l',
      order: 87,
      icon: 'agents',
    });
    const fns = listFunctions(WORKSPACES);
    for (const [m, rest] of [
      ['BRIEF', ['brief']],
      ['AIQ', ['approvals']],
      ['AICOST', ['cost']],
    ] as const) {
      expect(fns.find((f) => f.mnemonic === m)).toMatchObject({
        workspace: 'ai',
        needsSecurity: false,
        rest,
      });
      expect(parseCommand(m, fns)).toMatchObject({ kind: 'function' });
    }
  });
});
