/**
 * ASK (AI analyst) view model: routes, defensive parsing of the API bodies (backend api/routes/ai.py), the live
 * stream reducer (GET /api/ai/stream events) and the answer's citation markup. Pure functions only, so every rule is
 * unit-tested without a DOM. Money stays a decimal string end to end: costs are never parsed into floats.
 */

export const THREAD_RE = /^thr-[0-9a-f]{32}$/;
export const JOB_RE = /^ask-[0-9a-f]{32}$/;
export const REF_RE = /^R[0-9]{1,4}$/;
export const MAX_QUESTION = 4000;

// ---------------------------------------------------------------------------- routes
export function parseAskRoute(rest: readonly string[]): { threadId: string | null } {
  const first = rest[0];
  return { threadId: first && THREAD_RE.test(first) ? first : null };
}

export function askRoute(threadId: string | null): string[] {
  return threadId ? [threadId] : [];
}

// ---------------------------------------------------------------------------- API bodies
type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) ? v : null);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);

export interface ModelChoice {
  provider: string;
  model: string;
  effort: string | null;
}

export interface AiStatus {
  enabled: boolean;
  disabledReason: string | null;
  label: string | null;
  models: ModelChoice[];
  caps: { currency: string; perJob: string; perDay: string; lifetime: string | null } | null;
  spentToday: string | null;
  tools: string[];
}

export function parseStatus(body: unknown): AiStatus | null {
  if (!isObj(body) || typeof body.enabled !== 'boolean') return null;
  const caps = isObj(body.caps) ? body.caps : null;
  return {
    enabled: body.enabled,
    disabledReason: str(body.disabled_reason),
    label: str(body.label),
    models: (Array.isArray(body.models) ? body.models : [])
      .filter(isObj)
      .map((m) => ({ provider: str(m.provider) ?? '', model: str(m.model) ?? '', effort: str(m.effort) }))
      .filter((m) => m.model),
    caps:
      caps && str(caps.currency) && str(caps.per_job) && str(caps.per_day)
        ? { currency: str(caps.currency)!, perJob: str(caps.per_job)!, perDay: str(caps.per_day)!, lifetime: str(caps.lifetime) }
        : null,
    spentToday: str(body.spent_today),
    tools: strs(body.tools),
  };
}

export interface ThreadSummary {
  threadId: string;
  title: string;
  turns: number;
}

export function parseThreads(body: unknown): ThreadSummary[] {
  if (!isObj(body) || !Array.isArray(body.threads)) return [];
  return body.threads
    .filter(isObj)
    .map((t) => ({ threadId: str(t.thread_id) ?? '', title: str(t.title) ?? '', turns: int(t.turns) ?? 0 }))
    .filter((t) => THREAD_RE.test(t.threadId));
}

export interface Citation {
  ref: string;
  tool: string;
  arguments: unknown;
  decision: string;
  ruleId: string;
  datasets: string[];
  status: string;
}

export interface LineCheck {
  line: number;
  refs: string[];
  status: 'cited' | 'unsourced';
  numbersUnmatched: string[];
}

export interface AnswerCheck {
  lines: LineCheck[];
  cited: number;
  unsourced: number;
  invalidRefs: string[];
  numbersUnmatched: number;
}

export interface Usage {
  inputTokens: number | null;
  outputTokens: number | null;
  cacheReadTokens: number | null;
  cacheWriteTokens: number | null;
}

export interface Answer {
  status: string;
  label: string | null;
  text: string | null;
  reason: string | null;
  check: AnswerCheck | null;
  citations: Citation[];
  model: string | null;
  effort: string | null;
  turns: number | null;
  toolCalls: number | null;
  usage: Usage | null;
  cost: string | null;
  currency: string | null;
  costExact: boolean | null;
}

function parseUsage(u: unknown): Usage | null {
  if (!isObj(u)) return null;
  return {
    inputTokens: int(u.input_tokens),
    outputTokens: int(u.output_tokens),
    cacheReadTokens: int(u.cache_read_tokens),
    cacheWriteTokens: int(u.cache_write_tokens),
  };
}

export function parseCitation(c: unknown): Citation | null {
  if (!isObj(c) || !str(c.ref)) return null;
  return {
    ref: str(c.ref)!,
    tool: str(c.tool) ?? '',
    arguments: c.arguments ?? null,
    decision: str(c.decision) ?? '',
    ruleId: str(c.rule_id) ?? '',
    datasets: strs(c.datasets),
    status: str(c.status) ?? '',
  };
}

function parseCheck(c: unknown): AnswerCheck | null {
  if (!isObj(c) || !Array.isArray(c.lines)) return null;
  return {
    lines: c.lines.filter(isObj).map((l) => ({
      line: int(l.line) ?? -1,
      refs: strs(l.refs),
      status: l.status === 'cited' ? 'cited' : 'unsourced',
      numbersUnmatched: strs(l.numbers_unmatched),
    })),
    cited: int(c.cited) ?? 0,
    unsourced: int(c.unsourced) ?? 0,
    invalidRefs: strs(c.invalid_refs),
    numbersUnmatched: int(c.numbers_unmatched) ?? 0,
  };
}

export function parseAnswer(a: unknown): Answer | null {
  if (!isObj(a) || !str(a.status)) return null;
  return {
    status: str(a.status)!,
    label: str(a.label),
    text: str(a.text),
    reason: str(a.reason),
    check: parseCheck(a.check),
    citations: (Array.isArray(a.citations) ? a.citations : []).map(parseCitation).filter((c): c is Citation => c !== null),
    model: str(a.model),
    effort: str(a.effort),
    turns: int(a.turns),
    toolCalls: int(a.tool_calls),
    usage: parseUsage(a.usage),
    cost: str(a.cost),
    currency: str(a.currency),
    costExact: typeof a.cost_exact === 'boolean' ? a.cost_exact : null,
  };
}

export interface Turn {
  seq: number;
  jobId: string;
  question: string;
  security: string | null;
  answer: Answer | null;
  jobState: string | null;
}

export interface ThreadView {
  threadId: string;
  title: string;
  turns: Turn[];
}

export function parseThread(body: unknown): ThreadView | null {
  if (!isObj(body) || !str(body.thread_id) || !Array.isArray(body.turns)) return null;
  return {
    threadId: str(body.thread_id)!,
    title: str(body.title) ?? '',
    turns: body.turns.filter(isObj).map((t) => ({
      seq: int(t.seq) ?? 0,
      jobId: str(t.job_id) ?? '',
      question: str(t.question) ?? '',
      security: str(t.security),
      answer: parseAnswer(t.answer),
      jobState: isObj(t.job) ? str(t.job.state) : null,
    })),
  };
}

export const TERMINAL_STATES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED']);

/** The thread's question still being worked on (its job not finished and no answer stored), if any. */
export function activeTurn(thread: ThreadView | null): Turn | null {
  const last = thread?.turns[thread.turns.length - 1];
  if (!last || last.answer) return null;
  return last.jobState && TERMINAL_STATES.has(last.jobState) ? null : last;
}

export interface AskBody {
  question: string;
  thread_id?: string;
  security?: string;
}

export function buildAsk(question: string, threadId: string | null, security: string | null): { body: AskBody } | { error: string } {
  const q = question.trim();
  if (!q) return { error: 'Type a question.' };
  if (q.length > MAX_QUESTION) return { error: `A question is at most ${MAX_QUESTION} characters.` };
  return { body: { question: q, ...(threadId ? { thread_id: threadId } : {}), ...(security ? { security } : {}) } };
}

export function parseAskResponse(body: unknown): { jobId: string; threadId: string } | null {
  if (!isObj(body) || !str(body.job_id) || !str(body.thread_id)) return null;
  return JOB_RE.test(str(body.job_id)!) && THREAD_RE.test(str(body.thread_id)!) ? { jobId: str(body.job_id)!, threadId: str(body.thread_id)! } : null;
}

// ---------------------------------------------------------------------------- live stream
export interface LiveTool {
  ref: string | null;
  tool: string;
  arguments: unknown;
  status: string | null;
  decision: string | null;
}

export interface LiveState {
  jobId: string;
  phase: 'connecting' | 'queued' | 'running' | 'done';
  turn: number;
  thinking: boolean;
  /** Streamed text per model turn; a turn that ended in tool calls is interim (the model's working notes). */
  segments: { turn: number; text: string; interim: boolean }[];
  tools: LiveTool[];
  model: string | null;
  effort: string | null;
  usage: Usage | null;
  cost: string | null;
  currency: string | null;
  costExact: boolean | null;
  answer: Answer | null;
  done: { state: string; outcome: string | null; reason: string | null } | null;
}

export function initialLive(jobId: string): LiveState {
  return {
    jobId,
    phase: 'connecting',
    turn: 0,
    thinking: false,
    segments: [],
    tools: [],
    model: null,
    effort: null,
    usage: null,
    cost: null,
    currency: null,
    costExact: null,
    answer: null,
    done: null,
  };
}

export type StreamEvent = { kind: string; data: unknown };

export function liveReducer(s: LiveState, e: StreamEvent): LiveState {
  const d = isObj(e.data) ? e.data : {};
  switch (e.kind) {
    case 'job': {
      const state = str(d.state);
      if (state && TERMINAL_STATES.has(state)) return s;
      return { ...s, phase: state === 'RUNNING' ? 'running' : 'queued' };
    }
    case 'status':
      return { ...s, phase: 'running' };
    case 'turn':
      return { ...s, phase: 'running', turn: int(d.turn) ?? s.turn + 1, model: str(d.model) ?? s.model, effort: str(d.effort) ?? s.effort, thinking: false };
    case 'thinking':
      return { ...s, thinking: true };
    case 'text': {
      const text = str(d.text) ?? '';
      const turn = int(d.turn) ?? s.turn;
      const segs = [...s.segments];
      const last = segs[segs.length - 1];
      if (last && last.turn === turn && !last.interim) segs[segs.length - 1] = { ...last, text: last.text + text };
      else segs.push({ turn, text, interim: false });
      return { ...s, thinking: false, segments: segs };
    }
    case 'segment_end':
      return { ...s, segments: s.segments.map((g) => (g.turn === (int(d.turn) ?? s.turn) ? { ...g, interim: true } : g)) };
    case 'reset':
      return { ...s, segments: s.segments.filter((g) => g.turn !== (int(d.turn) ?? s.turn)) };
    case 'tool_pending':
      return { ...s, thinking: false };
    case 'tool':
      return { ...s, tools: [...s.tools, { ref: str(d.ref), tool: str(d.tool) ?? '', arguments: d.arguments ?? null, status: null, decision: null }] };
    case 'tool_result': {
      const ref = str(d.ref);
      return { ...s, tools: s.tools.map((t) => (t.ref === ref ? { ...t, status: str(d.status), decision: str(d.decision) } : t)) };
    }
    case 'usage':
      return {
        ...s,
        usage: parseUsage(d),
        cost: str(d.cost),
        currency: str(d.currency),
        costExact: typeof d.cost_exact === 'boolean' ? d.cost_exact : s.costExact,
        model: str(d.model) ?? s.model,
        effort: str(d.effort) ?? s.effort,
      };
    case 'answer':
      return { ...s, answer: parseAnswer(d), thinking: false };
    case 'done':
      return { ...s, phase: 'done', thinking: false, done: { state: str(d.state) ?? 'FAILED', outcome: str(d.outcome), reason: str(d.reason) } };
    default:
      return s;
  }
}

// ---------------------------------------------------------------------------- answer markup
export type Piece = { kind: 'text'; text: string } | { kind: 'ref'; ref: string } | { kind: 'bold'; text: string };

/** Split one answer line into text, **bold** and citation refs ([R1] or [R1, R3]). */
export function splitLine(line: string): Piece[] {
  const out: Piece[] = [];
  const re = /\[(\s*R\d{1,4}(?:\s*[,;]\s*R\d{1,4})*\s*)\]|\*\*([^*]+)\*\*/g;
  let at = 0;
  for (let m = re.exec(line); m; m = re.exec(line)) {
    if (m.index > at) out.push({ kind: 'text', text: line.slice(at, m.index) });
    if (m[1] !== undefined) for (const ref of m[1].match(/R\d{1,4}/g) ?? []) out.push({ kind: 'ref', ref });
    else out.push({ kind: 'bold', text: m[2] ?? '' });
    at = m.index + m[0].length;
  }
  if (at < line.length) out.push({ kind: 'text', text: line.slice(at) });
  return out;
}

export type LineKind = 'h' | 'bullet' | 'sub' | 'p' | 'blank';

export function lineKind(line: string): { kind: LineKind; body: string } {
  if (!line.trim()) return { kind: 'blank', body: '' };
  const h = /^#{1,6}\s+(.*)$/.exec(line.trim());
  if (h) return { kind: 'h', body: h[1] ?? '' };
  const b = /^(\s*)[-*•]\s+(.*)$/.exec(line);
  if (b) return { kind: (b[1] ?? '').length >= 2 ? 'sub' : 'bullet', body: b[2] ?? '' };
  return { kind: 'p', body: line };
}

export function checksByLine(check: AnswerCheck | null): Map<number, LineCheck> {
  return new Map((check?.lines ?? []).map((l) => [l.line, l]));
}

/** An exact decimal string without trailing zeros (never rounded through a float): "0.1361284000" -> "0.1361284". */
export function exactCost(amount: string): string {
  const m = /^(-?\d+)(?:\.(\d+))?$/.exec(amount);
  if (!m) return amount;
  return `${m[1]}.${(m[2] ?? '').replace(/0+$/, '').padEnd(2, '0')}`;
}

/** The tool result body inside its untrusted-data markers (the model saw exactly this block). */
export function resultBody(content: string): { header: string | null; json: unknown; text: string } {
  const lines = content.split('\n');
  const header = lines[0]?.startsWith('<<<UNTRUSTED') ? lines[0] : null;
  const body = header && lines[lines.length - 1]?.startsWith('<<<END UNTRUSTED') ? lines.slice(1, -1).join('\n') : content;
  try {
    return { header, json: JSON.parse(body) as unknown, text: body };
  } catch {
    return { header, json: null, text: body };
  }
}

export function answerTone(status: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (status === 'answered') return 'ok';
  if (status === 'truncated' || status === 'refused' || status === 'cancelled') return 'warn';
  if (status === 'failed' || status === 'expired') return 'bad';
  return 'neutral';
}
