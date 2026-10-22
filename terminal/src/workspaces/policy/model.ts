/**
 * Policy workspace logic: sub-routes, activation pre-checks (mirroring core/handlers/admin.py
 * activate_policy; the core repeats every one), affected sessions, and the unsent-draft store.
 */
import type { Account, PolicyRecord, RiskState, Session } from '../../api/types';
import { compareDecimal, isDecimal } from '../../format/decimal';

// ------------------------------------------------------------------ routes

export type PolicyView =
  { kind: 'list' } | { kind: 'detail'; hash: string } | { kind: 'compare'; a: string | null; b: string | null } | { kind: 'draft'; base: string | null };

/** `#/policy`, `#/policy/<hash>`, `#/policy/compare/<a>/<b>`, `#/policy/draft[/<base hash>]`. */
export function parsePolicyRoute(rest: readonly string[]): PolicyView {
  const [head, x, y] = rest;
  if (!head) return { kind: 'list' };
  if (head === 'compare') return { kind: 'compare', a: x ?? null, b: y ?? null };
  if (head === 'draft') return { kind: 'draft', base: x ?? null };
  return { kind: 'detail', hash: head };
}

// ------------------------------------------------------------------ effects of activation

/** What activation changes and what it does not (core: PolicyActivated note + where the active policy is read). */
export const ACTIVATION_EFFECTS: readonly string[] = [
  'Publishing records a new, immutable version. It changes nothing until it is activated.',
  'Activation applies to future sessions: sessions created afterwards copy this session template into their mandate, including successor sessions, which copy the template active when they are created.',
  'Existing mandates are unchanged: sessions that already exist keep the seed, target, drawdown and loss limits, and the policy hash, they were created with.',
  'Account limits, eligibility, fees and wind-down settings are read from the active policy each time the core evaluates, so from activation they govern the whole account, including new orders for existing sessions.',
];

export const TERMINAL_SESSION_STATES: ReadonlySet<string> = new Set(['COMPLETED_SETTLED', 'CLOSED', 'CANCELLED']);

/** Sessions whose mandates stay as they are (every session that has not finished). */
export function sessionsKeepingMandates(sessions: readonly Session[]): Session[] {
  return sessions.filter((s) => !TERMINAL_SESSION_STATES.has(s.state));
}

// ------------------------------------------------------------------ pre-checks

export interface ActivationCheck {
  /** The core's rejection code when this check fails. */
  code: string;
  label: string;
  /** true passes, false fails, null when the input is not loaded. */
  ok: boolean | null;
  detail: string;
  /** A failure the core will certainly reject (immutable facts), as opposed to state that may change. */
  definite: boolean;
}

type Body = Record<string, unknown>;

function limitOf(body: Body, key: string): unknown {
  const l = body.account_limits;
  return l && typeof l === 'object' ? (l as Body)[key] : undefined;
}

/** The checks activate_policy applies, evaluated on what the terminal can see. */
export function activationChecks(body: Body, account: Account | null | undefined, risk: RiskState | null | undefined): ActivationCheck[] {
  const mode = typeof body.mode === 'string' ? body.mode : null;
  const ccy = typeof body.currency === 'string' ? body.currency : null;
  const cap = limitOf(body, 'authorized_capital');
  const checks: ActivationCheck[] = [
    {
      code: 'POLICY_MODE_MISMATCH',
      label: 'Mode matches the account',
      ok: account ? mode === account.mode : null,
      detail: account ? `policy ${mode ?? 'without a mode'}, account ${account.mode}` : 'account not loaded',
      definite: true,
    },
    {
      code: 'POLICY_CURRENCY_MISMATCH',
      label: 'Currency matches the account',
      ok: account ? ccy === account.base_currency : null,
      detail: account ? `policy ${ccy ?? 'without a currency'}, account ${account.base_currency}` : 'account not loaded',
      definite: true,
    },
    {
      code: 'LIVE_NOT_RELEASED',
      label: 'Not a LIVE policy',
      ok: mode !== 'LIVE',
      detail: mode === 'LIVE' ? 'LIVE policies cannot be activated in this release' : `mode ${mode ?? 'unknown'}`,
      definite: true,
    },
  ];
  const capOk = risk && typeof cap === 'string' && isDecimal(cap) && isDecimal(risk.authorized_total) ? compareDecimal(risk.authorized_total, cap) <= 0 : null;
  checks.push({
    code: 'AUTHORIZED_ABOVE_NEW_LIMIT',
    label: 'Capital already authorized fits the new limit',
    ok: capOk,
    detail:
      risk && typeof cap === 'string'
        ? `authorized ${risk.authorized_total} ${risk.currency}, new limit ${cap} ${ccy ?? ''}`.trim()
        : 'authorized total or limit not available',
    definite: false,
  });
  return checks;
}

/** A definite failure means the core will reject the activation; the button stays disabled. */
export function definiteFailure(checks: readonly ActivationCheck[]): ActivationCheck | undefined {
  return checks.find((c) => c.definite && c.ok === false);
}

// ------------------------------------------------------------------ helpers

export function policyByHash(policies: readonly PolicyRecord[] | undefined, hash: string | null | undefined): PolicyRecord | undefined {
  return hash ? policies?.find((p) => p.policy_hash === hash) : undefined;
}

/** Newest first by publication time (ties by hash for a stable order). */
export function newestFirst(policies: readonly PolicyRecord[]): PolicyRecord[] {
  return [...policies].sort((a, b) => b.published_at_ns - a.published_at_ns || a.policy_hash.localeCompare(b.policy_hash));
}

/** Default pair for the compare view: base = active (else oldest), compare = newest other policy. */
export function defaultComparison(policies: readonly PolicyRecord[], activeHash: string | null | undefined): { a: string; b: string } | null {
  if (policies.length < 2) return null;
  const sorted = newestFirst(policies);
  const a = activeHash && policies.some((p) => p.policy_hash === activeHash) ? activeHash : (sorted[sorted.length - 1] as PolicyRecord).policy_hash;
  const b = sorted.find((p) => p.policy_hash !== a)?.policy_hash;
  return b ? { a, b } : null;
}

// ------------------------------------------------------------------ unsent draft (per-viewer convenience)

export interface StoredDraft {
  baseHash: string;
  text: string;
  savedAtMs: number;
}

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const DRAFT_KEY = 'unknown.terminal.policy-draft.v1';

function defaultStore(): KeyValueStore | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

/** Never throws: storage can be absent, blocked or full; the editor works without it. */
export function loadDraft(store: KeyValueStore | null = defaultStore()): StoredDraft | null {
  try {
    const raw = store?.getItem(DRAFT_KEY);
    if (!raw) return null;
    const d = JSON.parse(raw) as Partial<StoredDraft>;
    return typeof d.baseHash === 'string' && typeof d.text === 'string' && typeof d.savedAtMs === 'number'
      ? { baseHash: d.baseHash, text: d.text, savedAtMs: d.savedAtMs }
      : null;
  } catch {
    return null;
  }
}

export function saveDraft(draft: StoredDraft, store: KeyValueStore | null = defaultStore()): boolean {
  try {
    store?.setItem(DRAFT_KEY, JSON.stringify(draft));
    return Boolean(store);
  } catch {
    return false;
  }
}

export function clearDraft(store: KeyValueStore | null = defaultStore()): void {
  try {
    store?.removeItem(DRAFT_KEY);
  } catch {
    /* storage unavailable: nothing to clear */
  }
}
