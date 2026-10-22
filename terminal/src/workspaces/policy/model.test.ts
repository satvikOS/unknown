import { describe, expect, it } from 'vitest';
import type { Account, PolicyRecord, RiskState, Session } from '../../api/types';
import {
  activationChecks,
  clearDraft,
  defaultComparison,
  definiteFailure,
  loadDraft,
  newestFirst,
  parsePolicyRoute,
  saveDraft,
  sessionsKeepingMandates,
  type KeyValueStore,
} from './model';

const account: Account = {
  account_key: 'SIM-1',
  provider: 'simulated',
  mode: 'SIMULATED',
  base_currency: 'USD',
  registered_at_ns: 0,
  registered_by: 'owner',
};
const risk = { authorized_total: '300.000000', currency: 'USD' } as RiskState;
const body = (over: Record<string, unknown> = {}, cap = '300.00') => ({
  mode: 'SIMULATED',
  currency: 'USD',
  account_limits: { authorized_capital: cap },
  ...over,
});

const rec = (hash: string, at: number): PolicyRecord => ({ policy_hash: hash, name: hash, published_by: 'owner', published_at_ns: at, body: {} as never });

describe('policy routes', () => {
  it('parses list, detail, compare and draft views', () => {
    expect(parsePolicyRoute([])).toEqual({ kind: 'list' });
    expect(parsePolicyRoute(['abc123'])).toEqual({ kind: 'detail', hash: 'abc123' });
    expect(parsePolicyRoute(['compare'])).toEqual({ kind: 'compare', a: null, b: null });
    expect(parsePolicyRoute(['compare', 'a1', 'b2'])).toEqual({ kind: 'compare', a: 'a1', b: 'b2' });
    expect(parsePolicyRoute(['draft'])).toEqual({ kind: 'draft', base: null });
    expect(parsePolicyRoute(['draft', 'h'])).toEqual({ kind: 'draft', base: 'h' });
  });
});

describe('activation pre-checks (mirror of activate_policy)', () => {
  it('passes a matching policy', () => {
    const checks = activationChecks(body(), account, risk);
    expect(checks.every((c) => c.ok === true)).toBe(true);
    expect(definiteFailure(checks)).toBeUndefined();
  });

  it('flags mode, currency and LIVE as definite failures', () => {
    expect(definiteFailure(activationChecks(body({ mode: 'PAPER' }), account, risk))?.code).toBe('POLICY_MODE_MISMATCH');
    expect(definiteFailure(activationChecks(body({ currency: 'EUR' }), account, risk))?.code).toBe('POLICY_CURRENCY_MISMATCH');
    const live = activationChecks(body({ mode: 'LIVE' }), { ...account, mode: 'LIVE' }, risk);
    expect(definiteFailure(live)?.code).toBe('LIVE_NOT_RELEASED');
  });

  it('compares authorized capital exactly and does not block on it', () => {
    const lower = activationChecks(body({}, '299.999999'), account, risk);
    const cap = lower.find((c) => c.code === 'AUTHORIZED_ABOVE_NEW_LIMIT')!;
    expect(cap.ok).toBe(false);
    expect(cap.definite).toBe(false);
    expect(definiteFailure(lower)).toBeUndefined();
    expect(activationChecks(body({}, '300'), account, risk).find((c) => c.code === 'AUTHORIZED_ABOVE_NEW_LIMIT')!.ok).toBe(true);
  });

  it('reports unknown inputs as not evaluated rather than passing', () => {
    const checks = activationChecks(body(), null, null);
    expect(checks.find((c) => c.code === 'POLICY_MODE_MISMATCH')!.ok).toBeNull();
    expect(checks.find((c) => c.code === 'AUTHORIZED_ABOVE_NEW_LIMIT')!.ok).toBeNull();
  });
});

describe('policy lists', () => {
  it('orders newest first and picks the active policy as the comparison base', () => {
    const ps = [rec('old', 1), rec('mid', 2), rec('new', 3)];
    expect(newestFirst(ps).map((p) => p.policy_hash)).toEqual(['new', 'mid', 'old']);
    expect(defaultComparison(ps, 'mid')).toEqual({ a: 'mid', b: 'new' });
    expect(defaultComparison(ps, 'new')).toEqual({ a: 'new', b: 'mid' });
    expect(defaultComparison(ps, null)).toEqual({ a: 'old', b: 'new' });
    expect(defaultComparison([rec('only', 1)], 'only')).toBeNull();
  });

  it('keeps mandates for every session that has not finished', () => {
    const s = (id: string, state: string) => ({ session_id: id, state }) as Session;
    const kept = sessionsKeepingMandates([s('a', 'RUNNING'), s('b', 'WAITING_FUNDS'), s('c', 'CLOSED'), s('d', 'COMPLETED_SETTLED'), s('e', 'FAILED_REVIEW')]);
    expect(kept.map((x) => x.session_id)).toEqual(['a', 'b', 'e']);
  });
});

describe('unsent draft storage', () => {
  function memory(): KeyValueStore & { data: Map<string, string> } {
    const data = new Map<string, string>();
    return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v), removeItem: (k) => void data.delete(k) };
  }

  it('round-trips a draft and clears it', () => {
    const m = memory();
    expect(saveDraft({ baseHash: 'h', text: '{}', savedAtMs: 5 }, m)).toBe(true);
    expect(loadDraft(m)).toEqual({ baseHash: 'h', text: '{}', savedAtMs: 5 });
    clearDraft(m);
    expect(loadDraft(m)).toBeNull();
  });

  it('never throws when storage is unavailable or corrupt', () => {
    const broken: KeyValueStore = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('quota');
      },
      removeItem: () => {
        throw new Error('blocked');
      },
    };
    expect(loadDraft(broken)).toBeNull();
    expect(saveDraft({ baseHash: 'h', text: '', savedAtMs: 1 }, broken)).toBe(false);
    expect(() => clearDraft(broken)).not.toThrow();
    expect(loadDraft(null)).toBeNull();
    const m = memory();
    m.data.set('unknown.terminal.policy-draft.v1', '{not json');
    expect(loadDraft(m)).toBeNull();
  });
});
