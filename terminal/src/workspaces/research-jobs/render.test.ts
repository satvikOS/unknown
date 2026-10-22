/**
 * Rendered proposal panel (react-dom/server, no DOM): model output is fenced off and labelled as not
 * advice, a proposal the core recorded is labelled advisory only, and the page says whether the core's
 * stored body is the job's own output. Bodies follow api/routes/research_jobs.py `_detail`.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ProposalPanel } from './JobDetailView';
import { canonicalJson, parseDetail, type JobDetail } from './model';

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

function detail(over: { state?: string; proposal?: unknown; recordedBody?: unknown; delivery?: string } = {}): JobDetail {
  const recorded = over.recordedBody === undefined ? undefined : { proposal_id: 'prop-1', author: 'agent.research', body: over.recordedBody, recorded_at_ns: 1_792_764_000_000_000_000 };
  const d = parseDetail({
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
  });
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

describe('canonical JSON', () => {
  it('ignores key order and nothing else', () => {
    expect(canonicalJson({ b: 1, a: [1, { d: 2, c: null }] })).toBe(canonicalJson({ a: [1, { c: null, d: 2 }], b: 1 }));
    expect(canonicalJson({ a: '1' })).not.toBe(canonicalJson({ a: 1 }));
    expect(canonicalJson({ a: [1, 2] })).not.toBe(canonicalJson({ a: [2, 1] }));
  });
});
