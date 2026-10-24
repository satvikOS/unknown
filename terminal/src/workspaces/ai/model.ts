/**
 * AI workspace model: routes, defensive parsing of the /api/ai responses and display rules.
 * The server owns the shapes (backend/src/unknown/api/routes/ai_processes.py); anything unexpected parses to null
 * and the view says so instead of guessing.
 */
import { exactNs } from '../../api/nsStrings';

export const PROCESSES = ['brief', 'summaries', 'explainer'] as const;
export type ProcessId = (typeof PROCESSES)[number];
export const PROCESS_LABEL: Record<ProcessId, string> = {
  brief: 'Morning brief',
  summaries: 'Filing summaries',
  explainer: 'Move explainer',
};

export type AiRoute =
  { view: 'inbox'; process: ProcessId | null; symbol: string | null } | { view: 'note'; noteId: string } | { view: 'approvals' } | { view: 'cost' };

export function parseAiRoute(rest: readonly string[]): AiRoute {
  const [head, arg] = rest;
  if (head === 'note' && arg) return { view: 'note', noteId: arg };
  if (head === 'approvals') return { view: 'approvals' };
  if (head === 'cost') return { view: 'cost' };
  if (head === 'brief') return { view: 'inbox', process: 'brief', symbol: null };
  let process: ProcessId | null = null;
  let symbol: string | null = null;
  for (const p of rest) {
    const [k, v] = p.split('=', 2);
    if (k === 'process' && (PROCESSES as readonly string[]).includes(v ?? '')) process = v as ProcessId;
    if (k === 'symbol' && v && /^[A-Z][A-Z0-9.-]{0,9}$/.test(v)) symbol = v;
  }
  return { view: 'inbox', process, symbol };
}

export function inboxRoute(process: ProcessId | null, symbol: string | null): string[] {
  const out: string[] = [];
  if (process) out.push(`process=${process}`);
  if (symbol) out.push(`symbol=${symbol}`);
  return out;
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);

export interface NoteLabel {
  kind: 'AI' | 'data';
  modelIds: string[];
  promptTemplateHash: string | null;
  generated: string | null;
  advisory: string;
}

export interface NoteSummary {
  note_id: string;
  run_id: string;
  process: string;
  created_ns: number | null;
  created_ns_str: string | null;
  title: string;
  status: string;
  symbols: string[];
  ciks: string[];
  lines: number;
  flagged: number;
  headline: string | null;
  label: NoteLabel | null;
}

function parseLabel(v: unknown): NoteLabel | null {
  if (!isObj(v)) return null;
  const kind = v.kind === 'AI' ? 'AI' : v.kind === 'data' ? 'data' : null;
  if (!kind) return null;
  return {
    kind,
    modelIds: strs(v.model_ids),
    promptTemplateHash: str(v.prompt_template_hash),
    generated: str(v.generated),
    advisory: str(v.advisory) ?? '',
  };
}

export function parseNoteList(raw: unknown): NoteSummary[] | null {
  if (!isObj(raw) || !Array.isArray(raw.notes)) return null;
  const out: NoteSummary[] = [];
  for (const n of raw.notes) {
    if (!isObj(n) || !str(n.note_id) || !str(n.title)) return null;
    out.push({
      note_id: n.note_id as string,
      run_id: str(n.run_id) ?? '',
      process: str(n.process) ?? '',
      created_ns: num(n.created_ns),
      created_ns_str: exactNs(n, 'created_ns'),
      title: n.title as string,
      status: str(n.status) ?? '',
      symbols: strs(n.symbols),
      ciks: strs(n.ciks),
      lines: num(n.lines) ?? 0,
      flagged: num(n.flagged) ?? 0,
      headline: str(n.headline),
      label: parseLabel(n.label),
    });
  }
  return out;
}

export type LineStatus = 'verified' | 'uncertain' | 'unsupported';
export interface NoteLine {
  text: string;
  status: LineStatus;
  citations: string[];
  quote: string | null;
  checks: string[];
}
export interface Citation {
  id: string;
  kind: string;
  title: string;
  source: string | null;
  link: string | null;
  published: string | null;
  firstSeen: string | null;
  datasetId: string | null;
  apiPath: string | null;
  security: string | null;
}
export interface NoteItem {
  citation: string;
  title: string;
  kind: string;
  form: string | null;
  published: string | null;
  why: string;
  sentToModel: boolean;
  notSentReason: string | null;
}
export interface NoteDetail {
  noteId: string;
  process: string;
  title: string;
  status: string;
  createdNs: number | null;
  createdNsStr: string | null;
  contentHash: string;
  contentHashOk: boolean;
  label: NoteLabel | null;
  headline: string | null;
  explanation: string | null;
  lines: NoteLine[];
  dataLines: { text: string; citations: string[]; label: string }[];
  items: NoteItem[];
  unavailable: { what: string; reason: string }[];
  citations: Record<string, Citation>;
  window: { from: string | null; to: string | null } | null;
  ai: { status: string; reason: string | null };
  cost: {
    amount: string | null;
    currency: string | null;
    promptTokens: number | null;
    outputTokens: number | null;
  };
}

function parseCitation(id: string, v: unknown): Citation | null {
  if (!isObj(v)) return null;
  const open = isObj(v.open) ? v.open : {};
  return {
    id,
    kind: str(v.kind) ?? 'unknown',
    title: str(v.title) ?? id,
    source: str(v.source),
    link: str(v.link),
    published: str(v.published),
    firstSeen: str(v.first_seen) ?? str(v.available),
    datasetId: str(v.dataset_id),
    apiPath: str(open.api),
    security: str(open.security),
  };
}

export function parseNoteDetail(raw: unknown): NoteDetail | null {
  if (!isObj(raw) || !isObj(raw.note)) return null;
  const n = raw.note;
  const b = isObj(n.body) ? n.body : null;
  if (!b || !str(n.note_id)) return null;
  const lines: NoteLine[] = [];
  for (const l of Array.isArray(b.lines) ? b.lines : []) {
    if (!isObj(l) || !str(l.text)) return null;
    const status = l.status === 'verified' || l.status === 'uncertain' ? l.status : 'unsupported';
    lines.push({
      text: l.text as string,
      status,
      citations: strs(l.citations),
      quote: str(l.quote),
      checks: strs(l.checks),
    });
  }
  const citations: Record<string, Citation> = {};
  if (isObj(b.citations)) {
    for (const [id, c] of Object.entries(b.citations)) {
      const parsed = parseCitation(id, c);
      if (parsed) citations[id] = parsed;
    }
  }
  const items: NoteItem[] = (Array.isArray(b.items) ? b.items : []).filter(isObj).map((i) => ({
    citation: str(i.citation) ?? '',
    title: str(i.title) ?? '',
    kind: str(i.kind) ?? '',
    form: str(i.form),
    published: str(i.published),
    why: str(i.why) ?? '',
    sentToModel: i.sent_to_model === true,
    notSentReason: str(i.not_sent_reason),
  }));
  const ai = isObj(b.ai) ? b.ai : {};
  const cost = isObj(b.cost) ? b.cost : {};
  const win = isObj(b.window) ? { from: str(b.window.from), to: str(b.window.to) } : null;
  return {
    noteId: n.note_id as string,
    process: str(n.process) ?? '',
    title: str(n.title) ?? '',
    status: str(n.status) ?? '',
    createdNs: num(n.created_ns),
    createdNsStr: exactNs(n, 'created_ns'),
    contentHash: str(n.content_hash) ?? '',
    contentHashOk: n.content_hash_ok === true,
    label: parseLabel(b.label),
    headline: str(b.headline),
    explanation: str(b.explanation),
    lines,
    dataLines: (Array.isArray(b.data_lines) ? b.data_lines : []).filter(isObj).map((d) => ({
      text: str(d.text) ?? '',
      citations: strs(d.citations),
      label: str(d.label) ?? 'data',
    })),
    items,
    unavailable: (Array.isArray(b.unavailable) ? b.unavailable : [])
      .filter(isObj)
      .map((u) => ({ what: str(u.what) ?? '', reason: str(u.reason) ?? '' })),
    citations,
    window: win,
    ai: { status: str(ai.status) ?? 'not_run', reason: str(ai.reason) },
    cost: {
      amount: str(cost.amount),
      currency: str(cost.currency),
      promptTokens: num(cost.prompt_tokens),
      outputTokens: num(cost.output_tokens),
    },
  };
}

/** A line that is not verified is flagged: no citation, an unknown span, a quote not found, or a quote not checked. */
export function lineFlag(line: NoteLine): string | null {
  if (line.status === 'verified') return null;
  if (line.citations.length === 0) return 'No source: this line cites nothing the terminal can open.';
  if (line.status === 'uncertain') return 'Cited, but its wording was not checked against the source (no quote).';
  return 'Not supported: the quote is not in the cited source.';
}

export function statusTone(status: string): 'ok' | 'warn' | 'bad' | 'info' | 'neutral' {
  if (status === 'ok') return 'ok';
  if (status === 'abstained' || status === 'data_only' || status === 'no_items') return 'info';
  if (status === 'ai_unavailable') return 'warn';
  return 'neutral';
}

export const STATUS_LABEL: Record<string, string> = {
  ok: 'AI note',
  abstained: 'nothing material',
  no_items: 'no items',
  data_only: 'data only',
  ai_unavailable: 'AI unavailable',
};

export interface Approval {
  approval_id: string;
  kind: string;
  title: string;
  source: string;
  source_id: string;
  state: string;
  content: unknown;
  content_hash: string;
  created_ns: number | null;
  expires_ns: number | null;
  decided_ns: number | null;
  nsStr: { created: string | null; expires: string | null; decided: string | null };
  decided_by: string | null;
  decision_reason: string | null;
}

export function parseApprovals(raw: unknown): Approval[] | null {
  if (!isObj(raw) || !Array.isArray(raw.approvals)) return null;
  const out: Approval[] = [];
  for (const a of raw.approvals) {
    if (!isObj(a) || !str(a.approval_id) || !str(a.content_hash)) return null;
    out.push({
      approval_id: a.approval_id as string,
      kind: str(a.kind) ?? '',
      title: str(a.title) ?? '',
      source: str(a.source) ?? '',
      source_id: str(a.source_id) ?? '',
      state: str(a.state) ?? '',
      content: a.content,
      content_hash: a.content_hash as string,
      created_ns: num(a.created_ns),
      expires_ns: num(a.expires_ns),
      decided_ns: num(a.decided_ns),
      nsStr: { created: exactNs(a, 'created_ns'), expires: exactNs(a, 'expires_ns'), decided: exactNs(a, 'decided_ns') },
      decided_by: str(a.decided_by),
      decision_reason: str(a.decision_reason),
    });
  }
  return out;
}

export interface ProcessCost {
  process: ProcessId;
  enabled: boolean;
  perDay: string | null;
  perRun: string | null;
  spentToday: string;
  runs: number;
  failed: number;
  promptTokens: number;
  outputTokens: number;
  cost: string;
  p50: number | null;
  p95: number | null;
  verified: number;
  unsupported: number;
  lines: number;
  unsupportedRate: string | null;
  outcomes: Record<string, number>;
}
export interface Dashboard {
  today: string;
  currency: string | null;
  spendToday: string;
  spendMonth: string;
  spendLifetime: string;
  unconfirmed: string;
  ownerCaps: {
    perDay: string;
    perJob: string;
    lifetime: string | null;
    perMonth: string | null;
  } | null;
  byDay: { day: string; total: string; byRole: Record<string, string> }[];
  processes: ProcessCost[];
  approvals: Record<string, number>;
  auditOk: boolean;
  problems: string[];
}

export function parseDashboard(raw: unknown): Dashboard | null {
  if (!isObj(raw) || !isObj(raw.spend) || !isObj(raw.processes)) return null;
  const s = raw.spend;
  const caps = isObj(raw.owner_caps) ? raw.owner_caps : null;
  const processes: ProcessCost[] = [];
  for (const p of PROCESSES) {
    const v = raw.processes[p];
    if (!isObj(v)) return null;
    const lat = isObj(v.latency_ms) ? v.latency_ms : {};
    const q = isObj(v.quality) ? v.quality : {};
    const outcomes: Record<string, number> = {};
    if (isObj(v.outcomes)) for (const [k, n] of Object.entries(v.outcomes)) if (typeof n === 'number') outcomes[k] = n;
    processes.push({
      process: p,
      enabled: v.enabled === true,
      perDay: str(v.per_day),
      perRun: str(v.per_run),
      spentToday: str(v.spent_today) ?? '0',
      runs: num(v.runs) ?? 0,
      failed: num(v.failed) ?? 0,
      promptTokens: num(v.prompt_tokens) ?? 0,
      outputTokens: num(v.output_tokens) ?? 0,
      cost: str(v.cost) ?? '0',
      p50: num(lat.p50),
      p95: num(lat.p95),
      verified: num(q.verified) ?? 0,
      unsupported: num(q.unsupported) ?? 0,
      lines: num(q.lines) ?? 0,
      unsupportedRate: str(q.unsupported_rate),
      outcomes,
    });
  }
  const byDay = (Array.isArray(s.by_day) ? s.by_day : []).filter(isObj).map((d) => {
    const roles: Record<string, string> = {};
    if (isObj(d.by_role)) for (const [k, v] of Object.entries(d.by_role)) if (typeof v === 'string') roles[k] = v;
    return {
      day: str(d.day) ?? '',
      total: str(d.total) ?? '0',
      byRole: roles,
    };
  });
  const approvals: Record<string, number> = {};
  if (isObj(raw.approvals)) for (const [k, v] of Object.entries(raw.approvals)) if (typeof v === 'number') approvals[k] = v;
  return {
    today: str(raw.today) ?? '',
    currency: str(s.currency),
    spendToday: str(s.today) ?? '0',
    spendMonth: str(s.month) ?? '0',
    spendLifetime: str(s.lifetime) ?? '0',
    unconfirmed: str(s.unconfirmed) ?? '0',
    ownerCaps: caps
      ? {
          perDay: str(caps.per_day) ?? '',
          perJob: str(caps.per_job) ?? '',
          lifetime: str(caps.lifetime),
          perMonth: str(caps.per_month),
        }
      : null,
    byDay,
    processes,
    approvals,
    auditOk: isObj(raw.audit_chain) && raw.audit_chain.ok === true,
    problems: strs(raw.problems),
  };
}

/** Share of a cap used, 0..100 (decimal strings; display only). Null when there is no cap. */
export function capShare(spent: string, cap: string | null): number | null {
  if (cap === null) return null;
  const c = Number(cap);
  const s = Number(spent);
  if (!Number.isFinite(c) || !Number.isFinite(s) || c <= 0) return null;
  return Math.min(100, Math.round((s / c) * 1000) / 10);
}

/** Trim trailing zeros of a ledger amount for display ("0.01893200" -> "0.018932"); never rounds. */
export function trimAmount(a: string): string {
  if (!/^-?\d+(\.\d+)?$/.test(a)) return a;
  if (!a.includes('.')) return a;
  const t = a.replace(/0+$/, '');
  return t.endsWith('.') ? `${t}00` : t.split('.')[1]!.length < 2 ? `${t}0` : t;
}
