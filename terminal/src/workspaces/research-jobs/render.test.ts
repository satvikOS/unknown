/**
 * Rendered proposal panel (react-dom/server, no DOM): model output is fenced off and labelled as not
 * advice, a proposal the core recorded is labelled advisory only, and the page says whether the core's
 * stored body is the job's own output. Bodies follow api/routes/research_jobs.py `_detail`.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ProposalPanel } from './JobDetailView';
import { canonicalJson, parseDetail, parseProposal, type JobDetail } from './model';

const JOB_ID = 'rj-29cadbd2dafbf9350d61';
const OUTPUT = {
  abstain: false,
  assumptions: [],
  evidence: ['verified span S2: "concept=us-gaap:Revenues ... value=1200000" -- Revenue was 1200000 USD for fiscal 2025.'],
  horizon_seconds: 7_776_000,
  hypothesis: 'Revenue grew in fiscal 2025.',
  job_id: JOB_ID,
  reason: 'both annual values are in the evidence',
  schema_version: 1,
};

type Over = { state?: string; proposal?: unknown; recordedBody?: unknown; delivery?: string };

function rawDetail(over: Over = {}): Record<string, unknown> {
  const recorded = over.recordedBody === undefined ? undefined : { proposal_id: 'prop-1', author: 'agent.research', body: over.recordedBody, recorded_at_ns: 1_792_764_000_000_000_000 };
  return {
    job: { job_id: JOB_ID, role: 'research', state: over.state ?? 'SUCCEEDED', outcome: 'succeeded', reason: 'ok', delivery: over.delivery ?? 'DELIVERED', cancel_requested: false, start_attempts: 1 },
    request: { question: 'Did revenue grow?', role: 'research', issuer_ids: ['SEC-CIK-0000320193'], fields: null },
    envelope: { spend_cap: { currency: 'USD', amount: '0.50' } },
    proposal: over.proposal === undefined ? OUTPUT : over.proposal,
    core_result: recorded ? { accepted: true, duplicate: false, body: { proposal_id: 'prop-1' } } : null,
    recorded_proposal: recorded ?? null,
    events: [],
    audit: [],
    audit_chain: { ok: true, problems: [] },
    spend: { ledger: 'present', consumed: '0.031', currency: 'USD', reservations: [] },
  };
}

function detail(over: Over = {}): JobDetail {
  const d = parseDetail(rawDetail(over));
  if (!d) throw new Error('fixture did not parse');
  return d;
}

const render = (d: JobDetail) => renderToStaticMarkup(createElement(ProposalPanel, { d }));

describe('proposal panel', () => {
  it('fences model text off as not advice and labels the recorded proposal advisory only', () => {
    const html = render(detail({ recordedBody: OUTPUT }));
    expect(html).toContain('model output');
    expect(html).toContain('Not advice.');
    expect(html).toContain('Hypothesis (model text)');
    expect(html).toContain('Revenue grew in fiscal 2025.');
    expect(html).toContain('recorded by the core');
    expect(html).toContain('Advisory only: it cannot reserve cash, place an order or change policy.');
    expect(html).toContain('identical');
    expect(html).not.toContain('differs');
    // The model text sits inside the fenced group, after its label.
    const fence = html.indexOf('aria-label="Model output: the job&#x27;s proposal"');
    expect(fence).toBeGreaterThan(-1);
    expect(html.indexOf('Revenue grew in fiscal 2025.')).toBeGreaterThan(fence);
  });

  it('says so when the core holds a different body, even in a field the page does not display', () => {
    const html = render(detail({ recordedBody: { ...OUTPUT, job_id: 'rj-someone-else' } }));
    expect(html).toContain('differs');
    expect(html).not.toContain('>identical<');
  });

  it('shows an abstention as one, with no hypothesis', () => {
    const html = render(detail({ proposal: { ...OUTPUT, abstain: true, hypothesis: '' }, recordedBody: undefined, delivery: 'PENDING' }));
    expect(html).toContain('The model abstained');
    expect(html).toContain('Why it abstained (model text)');
    expect(html).not.toContain('Hypothesis (model text)');
    expect(html).toContain('delivery PENDING');
  });

  it('shows no model output for a job that produced none', () => {
    const html = render(detail({ state: 'FAILED', proposal: null, recordedBody: undefined, delivery: 'NONE' }));
    expect(html).toContain('No proposal: the job ended failed.');
    expect(html).not.toContain('model output');
  });
});

/** research_proposal v2 as agents/runner.py build_proposal emits it (TEST FIXTURE values). */
const OUTPUT_V2 = {
  schema_version: 2,
  job_id: JOB_ID,
  as_of_ns: 1_792_764_000_000_000_000,
  hypothesis: 'Revenue grew in fiscal 2025.',
  claims: [
    { text: 'Revenue was 1200000 USD for fiscal 2025.', quote: 'concept=us-gaap:Revenues period=2024-09-29..2025-09-27 unit=USD value=1200000', evidence_indexes: [0] },
    { text: 'Revenue was 1000000 USD for fiscal 2024.', quote: null, evidence_indexes: [1] },
  ],
  evidence: [
    { span_id: 'S2', dataset_id: 'sec.companyfacts', ref_type: 'observation', ref_id: 'obs:41', content_hash: 'a'.repeat(64), observed_at_ns: 1_758_931_200_000_000_000, available_at_ns: 1_761_955_200_000_000_000 },
    { span_id: 'S1', dataset_id: 'sec.companyfacts', ref_type: 'observation', ref_id: 'obs:17', content_hash: 'b'.repeat(64), observed_at_ns: 1_727_395_200_000_000_000, available_at_ns: 1_730_419_200_000_000_000 },
  ],
  instrument_ids: ['FIX-EQ-1'],
  entity_ids: ['SEC-CIK-0000000001'],
  observation_window: { from_ns: 1_727_395_200_000_000_000, to_ns: 1_758_931_200_000_000_000 },
  horizon_seconds: 7_776_000,
  assumptions: ['[unsupported claim, not evidence] Margins widened.'],
  forecast: null,
  abstain: false,
  reason: 'both annual values are in the evidence',
};

describe('proposal panel, schema version 2', () => {
  it('shows the verified claims and the typed evidence references instead of "None cited."', () => {
    const d = detail({ proposal: OUTPUT_V2, recordedBody: OUTPUT_V2 });
    const html = render(d);
    expect(html).not.toContain('None cited.');
    expect(html).toContain('Verified claims (2)');
    expect(html).toContain('Revenue was 1200000 USD for fiscal 2025.');
    expect(html).toContain('Cites [0] S2.');
    expect(html).toContain('Evidence references (2)');
    expect(html).toContain('obs:41');
    expect(html).toContain('obs:17');
    expect(html).toContain('FIX-EQ-1');
    expect(html).toContain('SEC-CIK-0000000001');
    expect(html).toContain('none (the agent layer never emits one)');
    expect(html).toContain('identical');
    // Claims are model text inside the fence; the typed fields come after it, in their own group.
    const fence = html.indexOf('aria-label="Model output: the job&#x27;s proposal"');
    const typed = html.indexOf('aria-label="Typed fields from the job and the research store"');
    expect(fence).toBeGreaterThan(-1);
    expect(html.indexOf('Revenue was 1200000 USD for fiscal 2025.')).toBeGreaterThan(fence);
    expect(typed).toBeGreaterThan(html.indexOf('Revenue was 1200000 USD for fiscal 2025.'));
    expect(html.indexOf('obs:41')).toBeGreaterThan(typed);
  });

  it("shows the core's label, and none for a version-1 proposal", () => {
    const d = detail({ proposal: OUTPUT_V2, recordedBody: OUTPUT_V2 });
    if (d.recorded_proposal) d.recorded_proposal.label = 'QUALITATIVE';
    const html = render(d);
    expect(html).toContain('QUALITATIVE');
    expect(html).toContain('It must not feed a strategy feature.');
    const v1 = render(detail({ recordedBody: OUTPUT }));
    expect(v1).toContain('none (the core labels only schema version 2)');
    expect(v1).toContain('Cited evidence (1)');
    expect(v1).toContain('free-text evidence');
  });

  it('parses the typed fields only from a version-2 body, and keeps version-1 text evidence apart', () => {
    const p = parseProposal(OUTPUT_V2);
    expect(p?.evidence).toEqual([]);
    expect(p?.evidence_refs.map((r) => r.ref_id)).toEqual(['obs:41', 'obs:17']);
    expect(p?.claims.map((c) => c.evidence_indexes)).toEqual([[0], [1]]);
    expect(p?.instrument_ids).toEqual(['FIX-EQ-1']);
    expect(p?.has_forecast).toBe(false);
    const old = parseProposal(OUTPUT);
    expect(old?.evidence).toHaveLength(1);
    expect(old?.evidence_refs).toEqual([]);
    expect(old?.as_of_ns).toBeNull();
    // A label the core recorded is read from the recorded row.
    const d = parseDetail({ ...rawDetail(), recorded_proposal: { proposal_id: 'prop-1', author: 'agent.research', body: OUTPUT_V2, recorded_at_ns: 1, label: 'ABSTENTION' } });
    expect(d?.recorded_proposal?.label).toBe('ABSTENTION');
  });
});

describe('canonical JSON', () => {
  it('ignores key order and nothing else', () => {
    expect(canonicalJson({ b: 1, a: [1, { d: 2, c: null }] })).toBe(canonicalJson({ a: [1, { c: null, d: 2 }], b: 1 }));
    expect(canonicalJson({ a: '1' })).not.toBe(canonicalJson({ a: 1 }));
    expect(canonicalJson({ a: [1, 2] })).not.toBe(canonicalJson({ a: [2, 1] }));
  });
});
