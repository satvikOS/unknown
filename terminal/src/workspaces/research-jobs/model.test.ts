/** Research jobs model. Every id, amount and text below is a TEST FIXTURE shaped like the API's responses. */
import { describe, expect, it } from 'vitest';
import {
  EMPTY_FORM,
  addIssuer,
  auditSummary,
  buildSubmit,
  cancelAvailability,
  issuerForInstrument,
  needsPolling,
  parseCancelResponse,
  parseDetail,
  parseJobList,
  parseJobsRoute,
  parseSubmitResponse,
  proposalHeadline,
  reasonSource,
  shortQuestion,
  spendUse,
  splitList,
  stateExplanation,
  stateTone,
  type SubmitForm,
} from './model';

const form = (over: Partial<SubmitForm> = {}): SubmitForm => ({ ...EMPTY_FORM, question: 'Did revenue grow?', acknowledgedSpend: true, ...over });

const LIST_ROW = {
  abstain: null,
  cancel_requested: false,
  deadline_ns: 1792767600000000000,
  delivered_ns: null,
  delivery: 'NONE',
  delivery_attempts: 0,
  delivery_error: '',
  finished_ns: 1792764000000000000,
  job_id: 'rj-fixture0001',
  outcome: 'denied_by_policy',
  output_hash: null,
  proposal_id: null,
  question: 'Did revenue grow in fiscal 2025?',
  reason: 'no provider is enabled in the owner policy; paid model calls are off',
  role: 'research',
  start_attempts: 1,
  started_ns: 1792764000000000000,
  state: 'FAILED',
  submitted_by: 'owner',
  submitted_ns: 1792764000000000000,
};

const DETAIL = {
  job: { ...LIST_ROW, state: 'SUCCEEDED', outcome: 'succeeded', reason: 'ok', delivery: 'DELIVERED', worker_id: 'w1', dedupe_key: 'k', updated_ns: 1, discarded_output_hash: null },
  request: { question: 'Did revenue grow in fiscal 2025?', role: 'research', issuer_ids: ['FIXA'], fields: ['us-gaap:Revenues'] },
  task: 'task text',
  evidence: {
    selection: { entity_ids: ['SEC-CIK-0000000001'], fields: ['us-gaap:Revenues'], as_of_ns: 1792764000000000000 },
    spans: [{ span_id: 'S1', dataset_id: 'sec.companyfacts', document_id: 'd', content_hash: 'h', text: 'concept=us-gaap:Revenues value=1000000' }],
  },
  envelope: {
    allowed_tools: [],
    created_ns: 1792764000000000000,
    deadline_ns: 1792767600000000000,
    input_hashes: ['a', 'b'],
    job_id: 'rj-fixture0001',
    max_tool_calls: 0,
    max_turns: 4,
    output_schema: 'research_answer',
    prompt_template_hash: 'p',
    retry_budget: 2,
    role: 'research',
    schema_version: 1,
    spend_cap: { amount: '2.00', currency: 'USD' },
    token_cap: 500000,
  },
  proposal: { abstain: false, assumptions: [], evidence: ['verified span S1'], horizon_seconds: 7776000, hypothesis: 'Revenue grew.', job_id: 'rj-fixture0001', reason: 'both values', schema_version: 1 },
  core_result: { accepted: true, body: { proposal_id: 'prop-fixture' }, duplicate: false, events: [2] },
  recorded_proposal: { author: 'agent.research', body: { abstain: false, assumptions: [], evidence: [], horizon_seconds: 1, hypothesis: 'Revenue grew.', job_id: 'x', reason: 'r', schema_version: 1 }, proposal_id: 'prop-fixture', recorded_at_ns: 5 },
  events: [{ at_ns: 1, detail: '{"by":"owner"}', kind: 'queued', seq: 1 }],
  audit: [{ actor: 'worker', at_ns: 1, chain_hash: 'c', kind: 'job_started', record: { kind: 'job_started', attempt: 1, envelope: { big: true } }, recorded_ns: 1, seq: 1 }],
  audit_chain: { ok: true, problems: [] },
  spend: {
    consumed: '0.01240000',
    currency: 'USD',
    ledger: 'present',
    reservations: [{ actual: '0.01240000', closed_ns: 1, created_ns: 1, currency: 'USD', model_id: 'm', note: '', provider: 'anthropic', purpose: 'generate', reservation_id: 'r1', reserved: '0.04480000', state: 'SETTLED' }],
  },
};

describe('parsing', () => {
  it('reads the job list and drops malformed rows instead of inventing fields', () => {
    const rows = parseJobList({ jobs: [LIST_ROW, { job_id: 7 }, 'x'] });
    expect(rows).toHaveLength(1);
    expect(rows?.[0]).toMatchObject({ job_id: 'rj-fixture0001', state: 'FAILED', abstain: null, cancel_requested: false });
    expect(parseJobList({})).toBeNull();
  });

  it('reads a full detail record', () => {
    const d = parseDetail(DETAIL);
    expect(d?.job.worker_id).toBe('w1');
    expect(d?.envelope?.spend_cap).toEqual({ currency: 'USD', amount: '2.00' });
    expect(d?.proposal?.hypothesis).toBe('Revenue grew.');
    expect(d?.core_result?.proposal_id).toBe('prop-fixture');
    expect(d?.recorded_proposal?.author).toBe('agent.research');
    expect(d?.spend.reservations[0]?.actual).toBe('0.01240000');
    expect(d?.audit_chain.ok).toBe(true);
    expect(d?.evidence?.spans[0]?.span_id).toBe('S1');
  });

  it('treats a missing audit chain block as not verified, and a JSON-number cap as absent', () => {
    const d = parseDetail({ ...DETAIL, audit_chain: undefined, envelope: { ...DETAIL.envelope, spend_cap: { currency: 'USD', amount: 2 } } });
    expect(d?.audit_chain.ok).toBe(false);
    expect(d?.envelope?.spend_cap).toBeNull();
  });

  it('reads submit and cancel responses', () => {
    expect(
      parseSubmitResponse({ created: true, deadline_ns: 9, duplicate_of: null, evidence_spans: 0, job_id: 'rj-1', spend_cap: { amount: '0.50', currency: 'USD' }, state: 'QUEUED' }),
    ).toMatchObject({ job_id: 'rj-1', created: true, spend_cap: { amount: '0.50', currency: 'USD' } });
    expect(parseCancelResponse({ job_id: 'rj-1', state: 'CANCELLED', cancel_requested: true })).toEqual({ job_id: 'rj-1', state: 'CANCELLED', cancel_requested: true });
    expect(parseSubmitResponse(null)).toBeNull();
  });
});

describe('states, cancel and polling', () => {
  it('gives every state a tone and an explanation that does not overstate what happened', () => {
    expect(stateTone('QUEUED')).toBe('info');
    expect(stateTone('FAILED')).toBe('bad');
    expect(stateTone('SUCCEEDED')).toBe('ok');
    expect(stateTone('MYSTERY')).toBe('neutral');
    expect(stateExplanation({ state: 'QUEUED', cancel_requested: false, start_attempts: 0 })).toMatch(/nothing has been spent/);
    expect(stateExplanation({ state: 'RUNNING', cancel_requested: false, start_attempts: 1 })).toMatch(/may be spending money/);
    expect(stateExplanation({ state: 'QUEUED', cancel_requested: false, start_attempts: 2 })).toMatch(/2 start attempt/);
  });

  it('attributes a job reason to whoever wrote it, never to the model', () => {
    expect(reasonSource({ outcome: 'cancelled_by_owner' })).toBe("the owner's cancel reason, not model text");
    expect(reasonSource({ outcome: 'denied_by_policy' })).toBe('recorded by the worker or runner, not model text');
  });

  it('allows cancel only where the job store does', () => {
    expect(cancelAvailability({ state: 'QUEUED', cancel_requested: false }).allowed).toBe(true);
    expect(cancelAvailability({ state: 'RUNNING', cancel_requested: false }).allowed).toBe(true);
    expect(cancelAvailability({ state: 'RUNNING', cancel_requested: true }).allowed).toBe(false);
    for (const s of ['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED']) expect(cancelAvailability({ state: s, cancel_requested: false }).allowed).toBe(false);
  });

  it('polls while a job can still change, including a pending proposal delivery', () => {
    expect(needsPolling([{ state: 'FAILED', delivery: 'NONE' }])).toBe(false);
    expect(needsPolling([{ state: 'QUEUED', delivery: 'NONE' }])).toBe(true);
    expect(needsPolling([{ state: 'SUCCEEDED', delivery: 'PENDING' }])).toBe(true);
  });
});

describe('spend against the cap', () => {
  it('compares consumed spend with the cap exactly, in the same currency only', () => {
    const d = parseDetail(DETAIL);
    if (!d) throw new Error('fixture');
    const use = spendUse(d.spend, d.envelope?.spend_cap ?? null);
    expect(use).toMatchObject({ consumed: '0.01240000', cap: '2.00', currency: 'USD', over: false });
    expect(use?.bps).toBe(62n); // 0.0124 / 2.00 = 0.62% -> 62 bps
    expect(spendUse({ ...d.spend, currency: 'EUR' }, { currency: 'USD', amount: '2.00' })).toBeNull();
    expect(spendUse({ ledger: 'absent', consumed: null, currency: null, reservations: [] }, { currency: 'USD', amount: '2.00' })).toBeNull();
    // A present ledger without rows for this job: zero consumed, in the cap's currency.
    expect(spendUse({ ledger: 'present', consumed: '0', currency: null, reservations: [] }, { currency: 'USD', amount: '0.50' })).toMatchObject({ bps: 0n, over: false });
    expect(spendUse({ ledger: 'present', consumed: '0.60', currency: 'USD', reservations: [] }, { currency: 'USD', amount: '0.50' })?.over).toBe(true);
  });
});

describe('submit form', () => {
  it('sends only the question when every option is left blank (server defaults and the owner per-job cap apply)', () => {
    expect(buildSubmit(form())).toEqual({ body: { question: 'Did revenue grow?' }, errors: {} });
  });

  it('refuses to send without the spend acknowledgement', () => {
    const r = buildSubmit(form({ acknowledgedSpend: false }));
    expect(r.body).toBeNull();
    expect(r.errors.acknowledgedSpend).toMatch(/may spend money/);
  });

  it('applies the server limits: question length, role pattern, issuers, concepts, cap and integers', () => {
    expect(buildSubmit(form({ question: '   ' })).errors.question).toBeDefined();
    expect(buildSubmit(form({ question: 'x'.repeat(4001) })).errors.question).toMatch(/4001/);
    expect(buildSubmit(form({ role: 'Research' })).errors.role).toBeDefined();
    expect(buildSubmit(form({ issuers: 'A B C D E F' })).errors.issuers).toMatch(/At most 5/);
    expect(buildSubmit(form({ issuers: 'bad id!' })).errors.issuers).toMatch(/"id!"/);
    expect(buildSubmit(form({ fields: 'us-gaap:Revenues' })).errors.fields).toMatch(/need at least one issuer/);
    expect(buildSubmit(form({ issuers: 'AAPL', fields: 'Revenues' })).errors.fields).toMatch(/taxonomy:Concept/);
    expect(buildSubmit(form({ spendCap: '1e2' })).errors.spendCap).toBeDefined();
    expect(buildSubmit(form({ spendCap: '0.00' })).errors.spendCap).toMatch(/more than zero/);
    expect(buildSubmit(form({ maxOutputTokens: '100' })).errors.maxOutputTokens).toMatch(/256/);
    expect(buildSubmit(form({ deadlineSeconds: '60.5' })).errors.deadlineSeconds).toMatch(/whole number/);
    expect(buildSubmit(form({ horizonSeconds: '0' })).errors.horizonSeconds).toBeDefined();
  });

  it('builds the full body with exact strings for money', () => {
    const r = buildSubmit(
      form({ role: 'macro_view', issuers: 'AAPL, SEC-CIK-0000789019 AAPL', fields: 'us-gaap:Revenues\nus-gaap:Assets', spendCap: '0.50', maxOutputTokens: '2000', deadlineSeconds: '600', horizonSeconds: '86400' }),
    );
    expect(r.body).toEqual({
      question: 'Did revenue grow?',
      role: 'macro_view',
      issuer_ids: ['AAPL', 'SEC-CIK-0000789019'],
      fields: ['us-gaap:Revenues', 'us-gaap:Assets'],
      spend_cap: '0.50',
      max_output_tokens: 2000,
      deadline_seconds: 600,
      horizon_seconds: 86400,
    });
  });

  it('maps an instrument to its issuer: CIK first, then the ticker; never a fixture instrument', () => {
    expect(issuerForInstrument({ symbol: 'AAPL', attributes: { 'sec.cik': 320193 } })).toEqual({ id: 'SEC-CIK-0000320193', basis: 'cik' });
    expect(issuerForInstrument({ symbol: 'MSFT', attributes: {} })).toEqual({ id: 'MSFT', basis: 'ticker' });
    expect(issuerForInstrument({ symbol: 'FIXA', attributes: { fixture: true } })).toMatchObject({ id: null });
    expect(addIssuer('AAPL', 'MSFT')).toBe('AAPL, MSFT');
    expect(addIssuer('AAPL, MSFT', 'MSFT')).toBe('AAPL, MSFT');
    expect(splitList(' a,,b ;c a ')).toEqual(['a', 'b', 'c']);
  });
});

describe('display helpers', () => {
  it('labels proposals as model statements, routes, and summarizes audit records compactly', () => {
    expect(proposalHeadline({ abstain: true, hypothesis: '', reason: '', evidence: [], assumptions: [], horizon_seconds: null })).toMatch(/abstained/);
    expect(parseJobsRoute([])).toEqual({ view: 'list' });
    expect(parseJobsRoute(['~new'])).toEqual({ view: 'new' });
    expect(parseJobsRoute(['rj-1'])).toEqual({ view: 'job', jobId: 'rj-1' });
    expect(shortQuestion('a  b\nc', 10)).toBe('a b c');
    expect(shortQuestion('x'.repeat(20), 10)).toBe(`${'x'.repeat(9)}…`);
    expect(auditSummary({ kind: 'job_started', attempt: 1, envelope: { big: true }, empty: [] })).toBe('attempt: 1');
  });
});
