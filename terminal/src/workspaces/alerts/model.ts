/**
 * Alerts workspace model (CAP-MKT-09, CAP-NEWS-13). Wire types of /api/alerts/* (backend api/routes/alerts.py)
 * and pure helpers: the new-rule form to the rule JSON the server validates, one-line descriptions, and the
 * evaluation state of a rule. The server is the authority on validation; the form checks only what it can show
 * next to a field. An alert informs; nothing here can reach an order.
 */
import { isDecimal } from '../../format/decimal';

export const RULE_KINDS = [
  { id: 'price_cross', label: 'Price crosses a level' },
  { id: 'pct_move', label: '% move from previous close' },
  { id: 'volume', label: 'Session volume reaches' },
  { id: 'study', label: 'Chart study value' },
  { id: 'time', label: 'Time' },
  { id: 'filing', label: 'New SEC filing' },
  { id: 'news', label: 'News or filing for an issuer' },
] as const;
export type RuleKind = (typeof RULE_KINDS)[number]['id'];
export const MARKET_KINDS: readonly RuleKind[] = ['price_cross', 'pct_move', 'volume', 'study'];

export interface Rule {
  kind: RuleKind;
  name: string;
  note?: string | null;
  repeat: 'once' | 'rearm';
  allow_delayed: boolean;
  key?: string;
  field?: string;
  level?: string;
  threshold?: string;
  direction?: 'up' | 'down' | 'either';
  hysteresis?: string;
  at?: string;
  study?: string;
  n?: number;
  compare?: 'value' | 'price';
  issuer?: string;
  forms?: string[];
  expires_at?: string;
}

export interface Evaluation {
  evaluating?: boolean;
  reason?: string | null;
  detail?: string | null;
  since_ns?: number;
  at_ns?: number;
}

export interface RuleRow {
  rule_id: string;
  created_ns: number;
  updated_ns: number;
  status: 'active' | 'paused' | 'triggered' | 'expired' | 'deleted';
  snooze_until_ns: number | null;
  rule: Rule;
  state: Record<string, unknown>;
  evaluation: Evaluation;
  triggers: number;
  last_trigger_ns: number | null;
}

export interface Trigger {
  observed?: string | null;
  threshold?: string | null;
  direction?: string | null;
  source?: string | null;
  data_type?: string | null;
  delayed?: boolean;
  label?: string | null;
  ts_source_ns?: number | string | null;
  ts_recv_ns?: number | string | null;
  form?: string | null;
  accession?: string | null;
  link?: string | null;
  study?: string;
  study_value?: number;
  [k: string]: unknown;
}

export interface AlertEvent {
  event_id: number;
  rule_id: string;
  fired_ns: number;
  kind: RuleKind;
  title: string;
  rule: Rule;
  trigger: Trigger;
  delayed: boolean;
  ack_ns: number | null;
  notify_status: 'pending' | 'delivered' | 'retrying' | 'failed' | 'disabled' | 'unavailable';
  notify_detail: string | null;
}

export interface AuditEntry {
  audit_id: number;
  ts_ns: number;
  actor: string;
  action: string;
  rule_id: string | null;
  event_id: number | null;
  detail: Record<string, unknown>;
}

export interface EngineStatus {
  engine: { started_ns: number | null; heartbeat_ns: number | null; keys: string[]; bus_error: string | null; rules_live: number; notifications: { state: string; detail: string | null } } | null;
  heartbeat_ns: number | null;
  engine_reason: string | null;
  engine_detail: string | null;
  notifications: { state: string; detail: string | null };
  now_ns: number;
}

export interface RuleForm {
  kind: RuleKind;
  name: string;
  key: string;
  field: string;
  level: string;
  threshold: string;
  direction: 'up' | 'down' | 'either';
  hysteresis: string;
  at: string;
  study: string;
  n: string;
  compare: 'value' | 'price';
  issuer: string;
  forms: string;
  repeat: 'once' | 'rearm';
  allowDelayed: boolean;
}

export function emptyForm(key: string | null): RuleForm {
  return { kind: 'price_cross', name: '', key: key ?? '', field: 'last', level: '', threshold: '', direction: 'either', hysteresis: '0', at: '', study: 'rsi', n: '14', compare: 'value', issuer: '', forms: '', repeat: 'rearm', allowDelayed: true };
}

export type FormErrors = Partial<Record<keyof RuleForm, string>>;

const KEY_RE = /^[A-Z0-9][A-Z0-9.\-/]{0,30}\.[A-Z0-9]{4}$|^[A-Za-z0-9][A-Za-z0-9_\-:.]{0,63}$/;

/** The rule JSON for POST /api/alerts/rules, or the field errors to show. */
export function buildRule(f: RuleForm): { rule: Rule | null; errors: FormErrors } {
  const errors: FormErrors = {};
  const name = f.name.trim() || defaultName(f);
  const base: Rule = { kind: f.kind, name, repeat: f.kind === 'time' ? 'once' : f.repeat, allow_delayed: f.allowDelayed };
  const dec = (k: 'level' | 'threshold' | 'hysteresis', positive: boolean) => {
    const v = f[k].trim();
    if (!isDecimal(v) || (positive && Number(v) <= 0) || (k === 'hysteresis' && v.startsWith('-'))) errors[k] = positive ? 'a decimal greater than 0' : 'a decimal';
    return v;
  };
  const key = () => {
    const k = f.key.trim().toUpperCase();
    if (!KEY_RE.test(k)) errors.key = 'a security key such as AAPL.XNAS';
    return k;
  };
  let rule: Rule = base;
  switch (f.kind) {
    case 'price_cross':
      rule = { ...base, key: key(), field: f.field, level: dec('level', true), direction: f.direction, hysteresis: dec('hysteresis', false) };
      break;
    case 'pct_move': {
      const pct = f.threshold.trim();
      if (!isDecimal(pct) || Number(pct) <= 0 || Number(pct) >= 1000) errors.threshold = 'a percentage such as 5';
      rule = { ...base, key: key(), threshold: isDecimal(pct) ? percentToFraction(pct) : pct, direction: f.direction, hysteresis: '0' };
      break;
    }
    case 'volume':
      rule = { ...base, key: key(), threshold: dec('threshold', true) };
      break;
    case 'study': {
      const n = Number(f.n);
      if (!Number.isInteger(n) || n < 2 || n > 500) errors.n = 'an integer from 2 to 500';
      rule = { ...base, key: key(), study: f.study, n, compare: f.compare, direction: f.direction, hysteresis: dec('hysteresis', false) };
      if (f.compare === 'value') rule.level = dec('level', false);
      break;
    }
    case 'time': {
      const d = new Date(f.at);
      if (!f.at || Number.isNaN(d.getTime())) errors.at = 'a date and time';
      else rule = { ...base, at: d.toISOString() };
      break;
    }
    default: {
      const issuer = f.issuer.trim().toUpperCase();
      if (!/^(\d{1,10}|[A-Z][A-Z0-9.\-]{0,14})$/.test(issuer)) errors.issuer = 'CIK digits or a ticker';
      const forms = f.forms.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
      if (forms.some((x) => !/^[A-Z0-9][A-Z0-9\-/ ]{0,15}$/.test(x))) errors.forms = 'form types such as 8-K, 10-Q';
      rule = { ...base, issuer, forms };
    }
  }
  return Object.keys(errors).length ? { rule: null, errors } : { rule, errors };
}

/** "5" (percent) -> "0.05" exactly, by moving the decimal point. */
export function percentToFraction(pct: string): string {
  const [i = '0', f = ''] = pct.replace(/^\+/, '').split('.');
  const whole = i.padStart(3, '0');
  const intPart = whole.slice(0, -2).replace(/^0+(?=\d)/, '');
  const frac = (whole.slice(-2) + f).replace(/0+$/, '');
  return frac ? `${intPart}.${frac}` : intPart;
}

function defaultName(f: RuleForm): string {
  const k = f.key.trim().toUpperCase();
  switch (f.kind) {
    case 'price_cross':
      return `${k} ${f.field} ${f.direction === 'down' ? '≤' : f.direction === 'up' ? '≥' : 'crosses'} ${f.level.trim()}`;
    case 'pct_move':
      return `${k} moves ${f.threshold.trim()}%`;
    case 'volume':
      return `${k} volume ${f.threshold.trim()}`;
    case 'study':
      return `${k} ${f.study.toUpperCase()} ${f.n}${f.compare === 'value' ? ` crosses ${f.level.trim()}` : ' vs price'}`;
    case 'time':
      return `Time ${f.at}`;
    default:
      return `${f.kind === 'filing' ? 'Filings' : 'News'} ${f.issuer.trim().toUpperCase()}`;
  }
}

/** One line for a rule, matching the server's alerts/rules.py describe(). */
export function describeRule(r: Rule): string {
  switch (r.kind) {
    case 'price_cross':
      return `${r.key} ${r.field} crosses ${r.level} (${r.direction})${r.hysteresis && r.hysteresis !== '0' ? `, hysteresis ${r.hysteresis}` : ''}`;
    case 'pct_move':
      return `${r.key} moves ${fractionToPercent(r.threshold ?? '')}% from the previous close (${r.direction})`;
    case 'volume':
      return `${r.key} session volume reaches ${r.threshold}`;
    case 'time':
      return `at ${r.at}`;
    case 'study':
      return r.compare === 'value' ? `${r.key} ${String(r.study).toUpperCase()} ${r.n} crosses ${r.level} (${r.direction})` : `${r.key} last price crosses ${String(r.study).toUpperCase()} ${r.n} (${r.direction})`;
    default:
      return `new ${r.kind === 'filing' ? 'filing' : 'news'}${r.forms?.length ? ` ${r.forms.join('/')}` : ''} for ${r.issuer}`;
  }
}

/** "0.05" -> "5" by moving the decimal point (display only). */
export function fractionToPercent(frac: string): string {
  if (!isDecimal(frac)) return frac;
  const neg = frac.startsWith('-');
  const [i = '0', f = ''] = frac.replace('-', '').split('.');
  const s = (i + f.padEnd(2, '0').slice(0, 2)).replace(/^0+(?=\d)/, '');
  const rest = f.slice(2);
  return `${neg ? '-' : ''}${s}${rest ? `.${rest}` : ''}`;
}

export type RuleTone = 'ok' | 'warn' | 'bad' | 'neutral' | 'info';

/** The rule's state as shown in the list: status first, then whether it is being evaluated, then snooze. */
export function ruleState(r: RuleRow, nowNs: number): { label: string; tone: RuleTone; detail: string | null } {
  if (r.status === 'paused') return { label: 'Paused', tone: 'neutral', detail: null };
  if (r.status === 'triggered') return { label: 'Triggered (once)', tone: 'info', detail: null };
  if (r.status === 'expired') return { label: 'Expired', tone: 'neutral', detail: null };
  if (r.evaluation && r.evaluation.evaluating === false) return { label: 'Not evaluating', tone: 'bad', detail: `${r.evaluation.reason ?? ''}: ${r.evaluation.detail ?? ''}` };
  if (r.snooze_until_ns && r.snooze_until_ns > nowNs) return { label: 'Snoozed', tone: 'warn', detail: null };
  if (r.evaluation?.evaluating) return { label: 'Evaluating', tone: 'ok', detail: null };
  return { label: 'Waiting for data', tone: 'warn', detail: MARKET_KINDS.includes(r.rule.kind) ? 'no quote has been evaluated yet' : null };
}

/** The observed value and threshold of a trigger in one line. */
export function triggerSummary(e: AlertEvent): string {
  const t = e.trigger;
  if (e.kind === 'news' || e.kind === 'filing') return [t.form, t.accession, t.observed].filter(Boolean).join(' · ');
  const parts = [`observed ${t.observed ?? '?'}`];
  if (t.threshold) parts.push(`threshold ${t.threshold}`);
  if (t.direction) parts.push(t.direction);
  if (t.study) parts.push(`${t.study} = ${typeof t.study_value === 'number' ? t.study_value.toPrecision(6) : '?'}`);
  if (t.source) parts.push(`${t.source} ${t.data_type ?? ''}`.trim());
  return parts.join(' · ');
}

export const SNOOZE_CHOICES = [
  { minutes: 15, label: '15 min' },
  { minutes: 60, label: '1 hour' },
  { minutes: 240, label: '4 hours' },
  { minutes: 1440, label: '1 day' },
] as const;
