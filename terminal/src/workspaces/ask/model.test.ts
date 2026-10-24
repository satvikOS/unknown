import { describe, expect, it } from 'vitest';
import {
  activeTurn,
  askRoute,
  buildAsk,
  exactCost,
  initialLive,
  lineKind,
  liveReducer,
  parseAnswer,
  parseAskRoute,
  parseStatus,
  parseThread,
  resultBody,
  splitLine,
  type LiveState,
  type StreamEvent,
} from './model';

const THREAD = `thr-${'a'.repeat(32)}`;
const JOB = `ask-${'b'.repeat(32)}`;

describe('ask routes and requests', () => {
  it('parses a thread route and refuses anything else', () => {
    expect(parseAskRoute([THREAD])).toEqual({ threadId: THREAD });
    expect(parseAskRoute(['../x'])).toEqual({ threadId: null });
    expect(askRoute(null)).toEqual([]);
  });

  it('builds the ask body with the thread and linked security, or says what is wrong', () => {
    expect(buildAsk('  Summarise AAPL  ', THREAD, 'AAPL.XNAS')).toEqual({ body: { question: 'Summarise AAPL', thread_id: THREAD, security: 'AAPL.XNAS' } });
    expect(buildAsk('q', null, null)).toEqual({ body: { question: 'q' } });
    expect(buildAsk('   ', null, null)).toEqual({ error: 'Type a question.' });
    expect('error' in buildAsk('x'.repeat(4001), null, null)).toBe(true);
  });
});

describe('ask API bodies', () => {
  it('reads the status with the model and effort from configuration', () => {
    const st = parseStatus({ enabled: true, models: [{ provider: 'anthropic', model: 'claude-opus-5-5', effort: 'medium' }],
      caps: { currency: 'USD', per_job: '2.00', per_day: '15.00', lifetime: '200.00' }, spent_today: '0.1361284000', tools: ['company_search'] });
    expect(st?.models[0]).toEqual({ provider: 'anthropic', model: 'claude-opus-5-5', effort: 'medium' });
    expect(st?.caps?.perJob).toBe('2.00');
    expect(parseStatus({ models: [] })).toBeNull();
  });

  it('finds the question still running in a thread', () => {
    const t = parseThread({ thread_id: THREAD, title: 't', turns: [
      { seq: 1, job_id: JOB, question: 'q1', answer: { status: 'answered', text: 'a [R1].' }, job: { state: 'SUCCEEDED' } },
      { seq: 2, job_id: `ask-${'c'.repeat(32)}`, question: 'q2', answer: null, job: { state: 'RUNNING' } },
    ] });
    expect(activeTurn(t)?.question).toBe('q2');
    const done = parseThread({ thread_id: THREAD, turns: [{ seq: 1, job_id: JOB, question: 'q', answer: null, job: { state: 'FAILED' } }] });
    expect(activeTurn(done)).toBeNull();
  });

  it('keeps cost as an exact decimal string', () => {
    expect(exactCost('0.1361284000')).toBe('0.1361284');
    expect(exactCost('0.0000000000')).toBe('0.00');
    expect(exactCost('2')).toBe('2.00');
    const a = parseAnswer({ status: 'answered', cost: '0.1361284000', currency: 'USD', cost_exact: true, citations: [{ ref: 'R1', tool: 'company_filings' }] });
    expect(a?.cost).toBe('0.1361284000');
    expect(a?.citations[0]?.ref).toBe('R1');
  });
});

describe('ask live stream', () => {
  const run = (events: StreamEvent[]): LiveState => events.reduce(liveReducer, initialLive(JOB));

  it('streams text, marks a turn that called tools as interim, and keeps usage and cost', () => {
    const s = run([
      { kind: 'job', data: { state: 'QUEUED' } },
      { kind: 'status', data: { state: 'RUNNING' } },
      { kind: 'turn', data: { turn: 1, model: 'claude-opus-5-5', effort: 'medium' } },
      { kind: 'text', data: { turn: 1, text: 'Looking ' } },
      { kind: 'text', data: { turn: 1, text: 'that up.' } },
      { kind: 'segment_end', data: { turn: 1 } },
      { kind: 'tool', data: { ref: 'R1', tool: 'company_filings', arguments: { cik: '320193' } } },
      { kind: 'tool_result', data: { ref: 'R1', status: 'ok', decision: 'executed' } },
      { kind: 'turn', data: { turn: 2 } },
      { kind: 'text', data: { turn: 2, text: 'Apple filed [R1].' } },
      { kind: 'usage', data: { input_tokens: 8, output_tokens: 2306, cache_read_tokens: 6682, cache_write_tokens: 17728, cost: '0.1361284000', currency: 'USD', cost_exact: true } },
    ]);
    expect(s.phase).toBe('running');
    expect(s.segments).toEqual([{ turn: 1, text: 'Looking that up.', interim: true }, { turn: 2, text: 'Apple filed [R1].', interim: false }]);
    expect(s.tools[0]).toMatchObject({ ref: 'R1', status: 'ok' });
    expect([s.model, s.effort, s.cost, s.usage?.outputTokens]).toEqual(['claude-opus-5-5', 'medium', '0.1361284000', 2306]);
  });

  it('drops a turn reset by a retry and ends on done', () => {
    const s = run([
      { kind: 'turn', data: { turn: 1 } },
      { kind: 'text', data: { turn: 1, text: 'partial' } },
      { kind: 'reset', data: { turn: 1 } },
      { kind: 'done', data: { state: 'CANCELLED', outcome: 'cancelled_by_owner', reason: 'owner cancel' } },
    ]);
    expect(s.segments).toEqual([]);
    expect([s.phase, s.done?.state]).toEqual(['done', 'CANCELLED']);
  });
});

describe('ask answer markup', () => {
  it('splits citations and bold text', () => {
    expect(splitLine('**Revenue:** 109,417,000,000 USD [R4, R2].')).toEqual([
      { kind: 'bold', text: 'Revenue:' },
      { kind: 'text', text: ' 109,417,000,000 USD ' },
      { kind: 'ref', ref: 'R4' },
      { kind: 'ref', ref: 'R2' },
      { kind: 'text', text: '.' },
    ]);
  });

  it('classifies headings, bullets and sub-bullets', () => {
    expect(lineKind('## Latest 10-Q')).toEqual({ kind: 'h', body: 'Latest 10-Q' });
    expect(lineKind('- one fact [R1]')).toEqual({ kind: 'bullet', body: 'one fact [R1]' });
    expect(lineKind('  - a detail')).toEqual({ kind: 'sub', body: 'a detail' });
    expect(lineKind('')).toEqual({ kind: 'blank', body: '' });
  });

  it('opens a tool result inside its untrusted-data markers', () => {
    const r = resultBody('<<<UNTRUSTED tool_result id=R1 dataset=sec.submissions nonce=ab>>>\n{"status":"UNAVAILABLE","reason_code":"NO_DATA"}\n<<<END UNTRUSTED nonce=ab>>>');
    expect(r.header).toContain('id=R1');
    expect(r.json).toEqual({ status: 'UNAVAILABLE', reason_code: 'NO_DATA' });
    expect(resultBody('tool call refused (not_allowlisted): x').json).toBeNull();
  });
});
