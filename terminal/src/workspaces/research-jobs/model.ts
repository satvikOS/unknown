/**
 * Research jobs (ADR-0016; backend api/routes/research_jobs.py, agents/jobstore.py, agents/submission.py).
 *
 * Wire shapes are parsed defensively: a missing or malformed field is reported as absent, never
 * invented. The submit request is validated here with the same limits the server applies, so the
 * form can explain a refusal before sending; the server still decides (and its message is shown).
 * Money (spend caps, consumption) stays a decimal string and is compared exactly.
 */
import { compareDecimal, isDecimal } from '../../format/decimal';
import { ratioBps } from '../../format/exact';
import type { Instrument } from '../../api/types';
import type { Tone } from '../../ui/primitives';
import { NEW_SEGMENT } from './segments';

// ------------------------------------------------------------------ constants (mirroring the backend)

export const JOB_STATES = ['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'] as const;
export type JobState = (typeof JOB_STATES)[number];
export const TERMINAL_STATES: readonly JobState[] = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'];

/** agents/submission.py */
export const LIMITS = {
  question: 4000,
  maxOutputTokens: [256, 32_000] as const,
  deadlineSeconds: [60, 86_400] as const,
  horizonSeconds: [1, 31_536_000] as const,
};
export const DEFAULTS = { role: 'research', maxOutputTokens: 8000, deadlineSeconds: 3600, horizonSeconds: 7_776_000 };
/** agents/envelope.py ROLE_RE */
export const ROLE_RE = /^[a-z][a-z0-9_]{0,63}$/;
/** agents/pit_evidence.py */
export const MAX_IDENTIFIERS = 5;
export const MAX_FIELDS = 40;
export const IDENT_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
export const FIELD_RE = /^[A-Za-z][A-Za-z0-9_-]{0,31}:[A-Za-z][A-Za-z0-9_]{0,159}$/;
/** A plain decimal string: the server refuses JSON numbers and exponents for money. */
const PLAIN_DECIMAL_RE = /^\d+(\.\d+)?$/;

// ------------------------------------------------------------------ wire types

export interface MoneyAmount {
  currency: string;
  amount: string;
}

/** One row of GET /api/research/jobs (jobstore.JobStore.list). */
export interface JobSummary {
  job_id: string;
  role: string;
  state: string;
  outcome: string;
  reason: string;
  submitted_by: string;
  submitted_ns: number | null;
  deadline_ns: number | null;
  started_ns: number | null;
  finished_ns: number | null;
  start_attempts: number;
  cancel_requested: boolean;
  delivery: string;
  delivery_attempts: number;
  delivery_error: string;
  output_hash: string | null;
  delivered_ns: number | null;
  question: string | null;
  proposal_id: string | null;
  abstain: boolean | null;
}

/** POST /api/research/jobs */
export interface SubmitResponse {
  job_id: string;
  created: boolean;
  duplicate_of: string | null;
  state: string;
  evidence_spans: number | null;
  spend_cap: MoneyAmount | null;
  deadline_ns: number | null;
}

/** POST /api/research/jobs/<id>/cancel */
export interface CancelResponse {
  job_id: string;
  state: string;
  cancel_requested: boolean;
}

export interface Envelope {
  role: string | null;
  spend_cap: MoneyAmount | null;
  token_cap: number | null;
  max_turns: number | null;
  max_tool_calls: number | null;
  retry_budget: number | null;
  deadline_ns: number | null;
  created_ns: number | null;
  output_schema: string | null;
  schema_version: number | null;
  prompt_template_hash: string | null;
  input_hashes: string[];
  allowed_tools: string[];
}

/** contracts/research_proposal.schema.json v1 (model output; advisory only). */
export interface Proposal {
  abstain: boolean;
  hypothesis: string;
  reason: string;
  evidence: string[];
  assumptions: string[];
  horizon_seconds: number | null;
}

export interface RecordedProposal {
  proposal_id: string;
  author: string | null;
  recorded_at_ns: number | null;
  body: Proposal | null;
  missing_from_core_db: boolean;
  error: string | null;
  /**
   * Whether the body the core recorded is identical, field for field, to the job's output (both as the
   * API returned them, not as parsed here). Null when either side is missing.
   */
  matches_job_output: boolean | null;
}

export interface JobEvent {
  seq: number;
  kind: string;
  at_ns: number | null;
  detail: string;
}

export interface AuditRecord {
  seq: number;
  kind: string;
  actor: string;
  at_ns: number | null;
  record: unknown;
  chain_hash: string;
}

export interface SpendReservation {
  reservation_id: string;
  purpose: string;
  provider: string;
  model_id: string;
  currency: string;
  reserved: string | null;
  actual: string | null;
  state: string;
  created_ns: number | null;
  closed_ns: number | null;
  note: string;
}

export interface Spend {
  ledger: 'present' | 'absent' | string;
  consumed: string | null;
  currency: string | null;
  reservations: SpendReservation[];
}

export interface EvidenceSpan {
  span_id: string;
  dataset_id: string;
  document_id: string;
  content_hash: string;
  text: string;
}

export interface JobDetail {
  job: JobSummary & { worker_id: string | null; discarded_output_hash: string | null; dedupe_key: string | null; updated_ns: number | null };
  request: { question: string; role: string; issuer_ids: string[]; fields: string[] | null } | null;
  task: string | null;
  evidence: { selection: { entity_ids: string[]; fields: string[]; as_of_ns: number | null } | null; spans: EvidenceSpan[] } | null;
  envelope: Envelope | null;
  proposal: Proposal | null;
  core_result: { accepted: boolean | null; duplicate: boolean | null; proposal_id: string | null; raw: unknown } | null;
  recorded_proposal: RecordedProposal | null;
  events: JobEvent[];
  audit: AuditRecord[];
  audit_chain: { ok: boolean; problems: string[] };
  spend: Spend;
}

// ------------------------------------------------------------------ parsing

type Rec = Record<string, unknown>;

export function isRecord(x: unknown): x is Rec {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}
const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : d);
const strOrNull = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const num = (v: unknown, d = 0): number => numOrNull(v) ?? d;
const boolOrNull = (v: unknown): boolean | null => (typeof v === 'boolean' ? v : null);
const strList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);

export function parseMoney(v: unknown): MoneyAmount | null {
  if (!isRecord(v) || typeof v.currency !== 'string' || !isDecimal(v.amount)) return null;
  return { currency: v.currency, amount: v.amount };
}

export function parseSummary(v: unknown): JobSummary | null {
  if (!isRecord(v) || typeof v.job_id !== 'string' || typeof v.state !== 'string') return null;
  return {
    job_id: v.job_id,
    role: str(v.role),
    state: v.state,
    outcome: str(v.outcome),
    reason: str(v.reason),
    submitted_by: str(v.submitted_by),
    submitted_ns: numOrNull(v.submitted_ns),
    deadline_ns: numOrNull(v.deadline_ns),
    started_ns: numOrNull(v.started_ns),
    finished_ns: numOrNull(v.finished_ns),
    start_attempts: num(v.start_attempts),
    cancel_requested: v.cancel_requested === true,
    delivery: str(v.delivery, 'NONE'),
    delivery_attempts: num(v.delivery_attempts),
    delivery_error: str(v.delivery_error),
    output_hash: strOrNull(v.output_hash),
    delivered_ns: numOrNull(v.delivered_ns),
    question: strOrNull(v.question),
    proposal_id: strOrNull(v.proposal_id),
    abstain: boolOrNull(v.abstain),
  };
}

export function parseJobList(v: unknown): JobSummary[] | null {
  if (!isRecord(v) || !Array.isArray(v.jobs)) return null;
  return v.jobs.map(parseSummary).filter((j): j is JobSummary => j !== null);
}

export function parseSubmitResponse(v: unknown): SubmitResponse | null {
  if (!isRecord(v) || typeof v.job_id !== 'string') return null;
  return {
    job_id: v.job_id,
    created: v.created === true,
    duplicate_of: strOrNull(v.duplicate_of),
    state: str(v.state),
    evidence_spans: numOrNull(v.evidence_spans),
    spend_cap: parseMoney(v.spend_cap),
    deadline_ns: numOrNull(v.deadline_ns),
  };
}

export function parseCancelResponse(v: unknown): CancelResponse | null {
  if (!isRecord(v) || typeof v.job_id !== 'string') return null;
  return { job_id: v.job_id, state: str(v.state), cancel_requested: v.cancel_requested === true };
}

export function parseEnvelope(v: unknown): Envelope | null {
  if (!isRecord(v)) return null;
  return {
    role: strOrNull(v.role),
    spend_cap: parseMoney(v.spend_cap),
    token_cap: numOrNull(v.token_cap),
    max_turns: numOrNull(v.max_turns),
    max_tool_calls: numOrNull(v.max_tool_calls),
    retry_budget: numOrNull(v.retry_budget),
    deadline_ns: numOrNull(v.deadline_ns),
    created_ns: numOrNull(v.created_ns),
    output_schema: strOrNull(v.output_schema),
    schema_version: numOrNull(v.schema_version),
    prompt_template_hash: strOrNull(v.prompt_template_hash),
    input_hashes: strList(v.input_hashes),
    allowed_tools: strList(v.allowed_tools),
  };
}

export function parseProposal(v: unknown): Proposal | null {
  if (!isRecord(v) || typeof v.abstain !== 'boolean') return null;
  return {
    abstain: v.abstain,
    hypothesis: str(v.hypothesis),
    reason: str(v.reason),
    evidence: strList(v.evidence),
    assumptions: strList(v.assumptions),
    horizon_seconds: numOrNull(v.horizon_seconds),
  };
}

/** Canonical JSON (sorted keys) for comparing two wire values exactly. */
export function canonicalJson(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(',')}]`;
  if (isRecord(v)) {
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonicalJson(v[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(v) ?? 'null';
}

function parseRecorded(v: unknown, jobOutput: unknown): RecordedProposal | null {
  if (!isRecord(v) || typeof v.proposal_id !== 'string') return null;
  return {
    proposal_id: v.proposal_id,
    author: strOrNull(v.author),
    recorded_at_ns: numOrNull(v.recorded_at_ns),
    body: parseProposal(v.body),
    missing_from_core_db: v.missing_from_core_db === true,
    error: strOrNull(v.error),
    matches_job_output: isRecord(v.body) && isRecord(jobOutput) ? canonicalJson(v.body) === canonicalJson(jobOutput) : null,
  };
}

function parseSpend(v: unknown): Spend {
  if (!isRecord(v)) return { ledger: 'absent', consumed: null, currency: null, reservations: [] };
  const reservations: SpendReservation[] = Array.isArray(v.reservations)
    ? v.reservations.filter(isRecord).map((r) => ({
        reservation_id: str(r.reservation_id),
        purpose: str(r.purpose),
        provider: str(r.provider),
        model_id: str(r.model_id),
        currency: str(r.currency),
        reserved: isDecimal(r.reserved) ? r.reserved : null,
        actual: isDecimal(r.actual) ? r.actual : null,
        state: str(r.state),
        created_ns: numOrNull(r.created_ns),
        closed_ns: numOrNull(r.closed_ns),
        note: str(r.note),
      }))
    : [];
  return { ledger: str(v.ledger, 'absent'), consumed: isDecimal(v.consumed) ? v.consumed : null, currency: strOrNull(v.currency), reservations };
}

export function parseDetail(v: unknown): JobDetail | null {
  if (!isRecord(v)) return null;
  const summary = parseSummary(v.job);
  if (!summary) return null;
  const job = v.job as Rec;
  const req = isRecord(v.request)
    ? { question: str(v.request.question), role: str(v.request.role), issuer_ids: strList(v.request.issuer_ids), fields: Array.isArray(v.request.fields) ? strList(v.request.fields) : null }
    : null;
  const ev = isRecord(v.evidence)
    ? {
        selection: isRecord(v.evidence.selection)
          ? { entity_ids: strList(v.evidence.selection.entity_ids), fields: strList(v.evidence.selection.fields), as_of_ns: numOrNull(v.evidence.selection.as_of_ns) }
          : null,
        spans: Array.isArray(v.evidence.spans)
          ? v.evidence.spans.filter(isRecord).map((s) => ({
              span_id: str(s.span_id),
              dataset_id: str(s.dataset_id),
              document_id: str(s.document_id),
              content_hash: str(s.content_hash),
              text: str(s.text),
            }))
          : [],
      }
    : null;
  const core = isRecord(v.core_result)
    ? {
        accepted: boolOrNull(v.core_result.accepted),
        duplicate: boolOrNull(v.core_result.duplicate),
        proposal_id: isRecord(v.core_result.body) ? strOrNull(v.core_result.body.proposal_id) : null,
        raw: v.core_result,
      }
    : null;
  const chain = isRecord(v.audit_chain) ? { ok: v.audit_chain.ok === true, problems: strList(v.audit_chain.problems) } : { ok: false, problems: ['audit chain status missing from the response'] };
  return {
    job: { ...summary, worker_id: strOrNull(job.worker_id), discarded_output_hash: strOrNull(job.discarded_output_hash), dedupe_key: strOrNull(job.dedupe_key), updated_ns: numOrNull(job.updated_ns) },
    request: req,
    task: strOrNull(v.task),
    evidence: ev,
    envelope: parseEnvelope(v.envelope),
    proposal: parseProposal(v.proposal),
    core_result: core,
    recorded_proposal: parseRecorded(v.recorded_proposal, v.proposal),
    events: Array.isArray(v.events)
      ? v.events.filter(isRecord).map((e) => ({ seq: num(e.seq), kind: str(e.kind), at_ns: numOrNull(e.at_ns), detail: str(e.detail) }))
      : [],
    audit: Array.isArray(v.audit)
      ? v.audit.filter(isRecord).map((a) => ({ seq: num(a.seq), kind: str(a.kind), actor: str(a.actor), at_ns: numOrNull(a.at_ns), record: a.record, chain_hash: str(a.chain_hash) }))
      : [],
    audit_chain: chain,
    spend: parseSpend(v.spend),
  };
}

// ------------------------------------------------------------------ presentation logic

export function stateTone(state: string): Tone {
  switch (state) {
    case 'SUCCEEDED':
      return 'ok';
    case 'QUEUED':
    case 'RUNNING':
      return 'info';
    case 'FAILED':
      return 'bad';
    case 'CANCELLED':
    case 'EXPIRED':
      return 'warn';
    default:
      return 'neutral';
  }
}

/** What the state means for the owner, in words (never implies the job did more than it did). */
export function stateExplanation(j: Pick<JobSummary, 'state' | 'cancel_requested' | 'start_attempts'>): string {
  switch (j.state) {
    case 'QUEUED':
      return j.start_attempts > 0
        ? `Queued again after ${j.start_attempts} start attempt(s): a worker restarted before anything was sent.`
        : 'Queued: no model worker has claimed it yet. Nothing has been sent to a model provider and nothing has been spent.';
    case 'RUNNING':
      return j.cancel_requested
        ? 'Running, cancel requested: the worker stops at the next turn boundary and discards any output.'
        : 'Running in the model worker. It may be spending money now, within its spend cap.';
    case 'SUCCEEDED':
      return 'Finished with a schema-valid answer or an abstention. The proposal is advisory only.';
    case 'FAILED':
      return 'Failed: the reason below says where it stopped. Any reservation whose outcome is unknown stays reserved.';
    case 'CANCELLED':
      return 'Cancelled by the owner. Any output produced after the cancel was discarded.';
    case 'EXPIRED':
      return 'Expired: its deadline passed before it could run or finish.';
    default:
      return `State ${j.state} is not one this terminal knows.`;
  }
}

/**
 * Who wrote a job's `reason` (jobstore.JobStore.cancel and finish, worker.py, runner.py).
 *
 * - Only a cancel of a QUEUED job stores the owner's own text; the job store then records a
 *   `cancelled` event. A cancel of a RUNNING job keeps no owner text: when the run stops, the job
 *   store or the worker writes the reason ("cancelled while running; ... output discarded").
 * - A `schema_invalid` reason lists the schema errors of the model's rejected output, which can
 *   include property names the model chose, so it can quote model text.
 * - Every other reason is written by the worker or the runner.
 *
 * `events` are the job-store events of the detail view; without them a cancel's author is not known.
 */
export function reasonSource(j: Pick<JobSummary, 'outcome'>, events?: readonly Pick<JobEvent, 'kind'>[]): string {
  if (j.outcome === 'cancelled_by_owner') {
    if (!events) return "the owner's cancel reason or the job store's note, not model text";
    return events.some((e) => e.kind === 'cancelled')
      ? "the owner's cancel reason, not model text"
      : "written by the job store or worker when the owner's cancel stopped the run (the owner's text is not kept for a running job), not model text";
  }
  if (j.outcome === 'schema_invalid') return "written by the runner; it lists schema errors and can quote parts of the model's rejected output";
  return 'recorded by the worker or runner, not model text';
}

/**
 * QUEUED cancels at once and keeps the owner's reason; RUNNING records a cancel request and the job
 * store keeps no reason text for it (jobstore.JobStore.cancel).
 */
export function cancelAvailability(j: Pick<JobSummary, 'state' | 'cancel_requested'>): { allowed: boolean; why: string } {
  if (j.state === 'QUEUED') return { allowed: true, why: 'A queued job is cancelled at once, and your reason is recorded as the job reason.' };
  if (j.state === 'RUNNING') {
    return j.cancel_requested
      ? { allowed: false, why: 'A cancel is already requested; the worker stops at the next turn boundary.' }
      : {
          allowed: true,
          why: 'A running job stops at the next turn boundary and its output is discarded. The API records the cancel request but not your reason text for a running job.',
        };
  }
  return { allowed: false, why: `A ${j.state.toLowerCase()} job has finished; there is nothing to cancel.` };
}

/** The job store is outside the core event log: poll while anything can still change. */
export function needsPolling(jobs: readonly Pick<JobSummary, 'state' | 'delivery'>[]): boolean {
  return jobs.some((j) => j.state === 'QUEUED' || j.state === 'RUNNING' || j.delivery === 'PENDING');
}

export function deliveryTone(delivery: string): Tone {
  switch (delivery) {
    case 'DELIVERED':
      return 'ok';
    case 'PENDING':
      return 'info';
    case 'REJECTED':
    case 'CONFLICT':
      return 'bad';
    default:
      return 'neutral';
  }
}

export interface SpendUse {
  consumed: string;
  cap: string;
  currency: string;
  /** consumed / cap in basis points (ceil); null without a positive cap. */
  bps: bigint | null;
  over: boolean;
}

/**
 * Consumed spend against the job's cap. Null when the ledger has no rows for the job, the currency is
 * unknown or differs from the cap's (never converted), or either amount is missing.
 */
export function spendUse(spend: Spend, cap: MoneyAmount | null): SpendUse | null {
  if (!cap || spend.consumed === null) return null;
  const currency = spend.currency ?? (spend.reservations.length === 0 ? cap.currency : null);
  if (currency !== cap.currency) return null;
  return { consumed: spend.consumed, cap: cap.amount, currency, bps: ratioBps(spend.consumed, cap.amount, 'ceil'), over: compareDecimal(spend.consumed, cap.amount) > 0 };
}

/** One line on what a proposal is; the text inside it is model output, never advice. */
export function proposalHeadline(p: Proposal): string {
  return p.abstain ? 'The model abstained: it did not find enough evidence to answer.' : 'The model stated a hypothesis with cited evidence.';
}

/** A proposal horizon in words plus exact seconds; a horizon under a day is never shown as "0 days". */
export function horizonText(seconds: number): string {
  const exact = `${seconds.toLocaleString('en-US')} s`;
  if (seconds >= 86_400) {
    const days = seconds % 86_400 === 0 ? String(seconds / 86_400) : (seconds / 86_400).toFixed(1);
    return `${days} ${days === '1' ? 'day' : 'days'} (${exact})`;
  }
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} hours (${exact})`;
  return exact;
}

export function shortQuestion(q: string | null, max = 90): string {
  if (!q) return '(no question recorded)';
  const one = q.replace(/\s+/g, ' ').trim();
  return one.length > max ? `${one.slice(0, max - 1)}…` : one;
}

// ------------------------------------------------------------------ submit form

export interface SubmitForm {
  question: string;
  role: string;
  /** Free text: comma, space or newline separated PIT entity ids or tickers. */
  issuers: string;
  /** Free text: taxonomy:Concept list; only with issuers. Blank uses the server default concepts. */
  fields: string;
  /** Decimal string in the owner's currency; blank uses the owner's per-job cap. */
  spendCap: string;
  maxOutputTokens: string;
  deadlineSeconds: string;
  horizonSeconds: string;
  /** The owner confirmed that the job may spend money up to its cap. */
  acknowledgedSpend: boolean;
}

export const EMPTY_FORM: SubmitForm = {
  question: '',
  role: DEFAULTS.role,
  issuers: '',
  fields: '',
  spendCap: '',
  maxOutputTokens: '',
  deadlineSeconds: '',
  horizonSeconds: '',
  acknowledgedSpend: false,
};

export type FormField = keyof SubmitForm;

export interface SubmitBody {
  question: string;
  role?: string;
  issuer_ids?: string[];
  fields?: string[];
  spend_cap?: string;
  max_output_tokens?: number;
  deadline_seconds?: number;
  horizon_seconds?: number;
}

export function splitList(text: string): string[] {
  const out: string[] = [];
  for (const t of text.split(/[\s,;]+/)) {
    const v = t.trim();
    if (v && !out.includes(v)) out.push(v);
  }
  return out;
}

function intField(text: string, [lo, hi]: readonly [number, number], label: string): { value?: number; error?: string } {
  const t = text.trim();
  if (!t) return {};
  if (!/^\d+$/.test(t)) return { error: `${label} must be a whole number` };
  const n = Number(t);
  if (n < lo || n > hi) return { error: `${label} must be between ${lo.toLocaleString('en-US')} and ${hi.toLocaleString('en-US')}` };
  return { value: n };
}

/**
 * Validate the form with the server's limits and build the request body. Blank optional fields are
 * left out, so the server applies its defaults (and the owner's per-job cap). Nothing is sent unless
 * the owner acknowledged that the job may spend money.
 */
export function buildSubmit(form: SubmitForm): { body: SubmitBody | null; errors: Partial<Record<FormField, string>> } {
  const errors: Partial<Record<FormField, string>> = {};
  const question = form.question;
  if (!question.trim()) errors.question = 'Enter a question.';
  else if (question.length > LIMITS.question) errors.question = `The question is ${question.length} characters; the limit is ${LIMITS.question}.`;
  const role = form.role.trim() || DEFAULTS.role;
  if (!ROLE_RE.test(role)) errors.role = 'Role: lower-case letters, digits and underscores, starting with a letter (at most 64).';
  const issuers = splitList(form.issuers);
  if (issuers.length > MAX_IDENTIFIERS) errors.issuers = `At most ${MAX_IDENTIFIERS} issuers (got ${issuers.length}).`;
  else {
    const bad = issuers.find((i) => !IDENT_RE.test(i));
    if (bad) errors.issuers = `${JSON.stringify(bad)} is not a valid entity id or ticker.`;
  }
  const fields = splitList(form.fields);
  if (fields.length && !issuers.length) errors.fields = 'Concepts need at least one issuer.';
  else if (fields.length > MAX_FIELDS) errors.fields = `At most ${MAX_FIELDS} concepts (got ${fields.length}).`;
  else {
    const bad = fields.find((f) => !FIELD_RE.test(f));
    if (bad) errors.fields = `${JSON.stringify(bad)} is not taxonomy:Concept (for example us-gaap:Revenues).`;
  }
  const cap = form.spendCap.trim();
  if (cap) {
    if (!PLAIN_DECIMAL_RE.test(cap)) errors.spendCap = 'Spend cap must be a plain decimal amount, for example 0.50.';
    else if (compareDecimal(cap, '0') <= 0) errors.spendCap = 'Spend cap must be more than zero.';
  }
  const maxOut = intField(form.maxOutputTokens, LIMITS.maxOutputTokens, 'Maximum output tokens');
  if (maxOut.error) errors.maxOutputTokens = maxOut.error;
  const deadline = intField(form.deadlineSeconds, LIMITS.deadlineSeconds, 'Deadline (seconds)');
  if (deadline.error) errors.deadlineSeconds = deadline.error;
  const horizon = intField(form.horizonSeconds, LIMITS.horizonSeconds, 'Horizon (seconds)');
  if (horizon.error) errors.horizonSeconds = horizon.error;
  if (!form.acknowledgedSpend) errors.acknowledgedSpend = 'Confirm that this job may spend money up to its cap.';
  if (Object.keys(errors).length) return { body: null, errors };
  const body: SubmitBody = { question };
  if (role !== DEFAULTS.role) body.role = role;
  if (issuers.length) body.issuer_ids = issuers;
  if (fields.length) body.fields = fields;
  if (cap) body.spend_cap = cap;
  if (maxOut.value !== undefined) body.max_output_tokens = maxOut.value;
  if (deadline.value !== undefined) body.deadline_seconds = deadline.value;
  if (horizon.value !== undefined) body.horizon_seconds = horizon.value;
  return { body, errors };
}

/**
 * The research-store identifier for an instrument's issuer: its SEC CIK as a PIT entity id when the
 * instrument master records one (attribute `sec.cik`), otherwise its symbol as a ticker. Fixture
 * instruments have no issuer, so none is offered.
 */
export function issuerForInstrument(i: Pick<Instrument, 'symbol' | 'attributes'>): { id: string; basis: 'cik' | 'ticker' } | { id: null; reason: string } {
  if (i.attributes?.fixture === true) return { id: null, reason: 'FIXTURE instrument: it has no issuer in the research store' };
  const cik = i.attributes?.['sec.cik'];
  if ((typeof cik === 'number' && Number.isInteger(cik) && cik > 0) || (typeof cik === 'string' && /^\d{1,10}$/.test(cik))) {
    return { id: `SEC-CIK-${String(cik).padStart(10, '0')}`, basis: 'cik' };
  }
  if (i.symbol && IDENT_RE.test(i.symbol)) return { id: i.symbol, basis: 'ticker' };
  return { id: null, reason: 'the instrument has no SEC CIK attribute and no usable symbol' };
}

export function addIssuer(text: string, id: string): string {
  const list = splitList(text);
  if (list.includes(id)) return text;
  return [...list, id].join(', ');
}

// ------------------------------------------------------------------ routes

export { NEW_SEGMENT };

export type JobsRoute = { view: 'list' } | { view: 'new' } | { view: 'job'; jobId: string };

export function parseJobsRoute(rest: readonly string[]): JobsRoute {
  const first = rest[0];
  if (!first) return { view: 'list' };
  if (first === NEW_SEGMENT) return { view: 'new' };
  return { view: 'job', jobId: first };
}

/** Audit records carry floats as decimal text already; this is a compact one-line summary for the table. */
export function auditSummary(record: unknown): string {
  if (!isRecord(record)) return '';
  const skip = new Set(['kind', 'at_ns', 'envelope', 'evidence']);
  const parts: string[] = [];
  for (const [k, v] of Object.entries(record)) {
    if (skip.has(k) || v === null || v === undefined || v === '') continue;
    if (Array.isArray(v) && v.length === 0) continue;
    const text = typeof v === 'string' ? v : JSON.stringify(v);
    parts.push(`${k}: ${text.length > 120 ? `${text.slice(0, 119)}…` : text}`);
  }
  return parts.join('; ');
}
