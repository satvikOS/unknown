import { describe, expect, it } from 'vitest';
import { listFunctions } from '../../ui/functions';
import { WORKSPACES, findWorkspace } from '../registry';
import { ALERT_FUNCTIONS } from './index';
import { buildRule, describeRule, emptyForm, fractionToPercent, percentToFraction, ruleState, triggerSummary, type AlertEvent, type RuleRow } from './model';

describe('new-rule form', () => {
  it('builds a price cross with exact decimals and a default name', () => {
    const { rule, errors } = buildRule({ ...emptyForm('aapl.xnas'), level: '200.50', direction: 'up', hysteresis: '0.25' });
    expect(errors).toEqual({});
    expect(rule).toEqual({ kind: 'price_cross', name: 'AAPL.XNAS last ≥ 200.50', repeat: 'rearm', allow_delayed: true, key: 'AAPL.XNAS', field: 'last', level: '200.50', direction: 'up', hysteresis: '0.25' });
  });

  it('turns a percentage into an exact fraction and back', () => {
    expect(['5', '12.5', '150', '100', '0.5'].map(percentToFraction)).toEqual(['0.05', '0.125', '1.5', '1', '0.005']);
    expect(['0.05', '0.125', '1.5'].map(fractionToPercent)).toEqual(['5', '12.5', '150']);
    expect(buildRule({ ...emptyForm('AAPL.XNAS'), kind: 'pct_move', threshold: '5' }).rule?.threshold).toBe('0.05');
  });

  it('reports field errors and builds nothing', () => {
    const r = buildRule({ ...emptyForm(null), level: 'abc' });
    expect(r.rule).toBeNull();
    expect(Object.keys(r.errors).sort()).toEqual(['key', 'level']);
    expect(buildRule({ ...emptyForm('X.XNAS'), kind: 'study', n: '1', level: '70' }).errors.n).toBeDefined();
    expect(buildRule({ ...emptyForm(null), kind: 'filing', issuer: 'a b' }).errors.issuer).toBeDefined();
  });

  it('builds a filing rule and a real-time-only study rule', () => {
    expect(buildRule({ ...emptyForm(null), kind: 'filing', issuer: '320193', forms: '8-k, 10-Q' }).rule).toMatchObject({ kind: 'filing', issuer: '320193', forms: ['8-K', '10-Q'] });
    const s = buildRule({ ...emptyForm('AAPL.XNAS'), kind: 'study', study: 'rsi', n: '14', level: '70', allowDelayed: false }).rule!;
    expect(s).toMatchObject({ study: 'rsi', n: 14, compare: 'value', level: '70', allow_delayed: false });
    expect(describeRule(s)).toBe('AAPL.XNAS RSI 14 crosses 70 (either)');
  });
});

describe('rule state and trigger lines', () => {
  const row = (over: Partial<RuleRow>): RuleRow => ({ rule_id: 'r', created_ns: 0, updated_ns: 0, status: 'active', snooze_until_ns: null, rule: { kind: 'price_cross', name: 'n', repeat: 'rearm', allow_delayed: true, key: 'A.XNAS' }, state: {}, evaluation: {}, triggers: 0, last_trigger_ns: null, ...over });

  it('shows not-evaluating before snooze, and waiting when nothing was evaluated', () => {
    expect(ruleState(row({ evaluation: { evaluating: false, reason: 'NOT_CONNECTED', detail: 'gateway down' } }), 10).label).toBe('Not evaluating');
    expect(ruleState(row({ snooze_until_ns: 20, evaluation: { evaluating: true } }), 10).label).toBe('Snoozed');
    expect(ruleState(row({}), 10)).toMatchObject({ label: 'Waiting for data', tone: 'warn' });
    expect(ruleState(row({ status: 'paused' }), 10).label).toBe('Paused');
  });

  it('summarises a delayed market trigger and a filing', () => {
    const e = { kind: 'price_cross', trigger: { observed: '101', threshold: '100', direction: 'up', source: 'IBKR', data_type: 'DELAYED' } } as unknown as AlertEvent;
    expect(triggerSummary(e)).toBe('observed 101 · threshold 100 · up · IBKR DELAYED');
    const f = { kind: 'filing', trigger: { form: '8-K', accession: '0000320193-26-000002', observed: 'Apple 8-K' } } as unknown as AlertEvent;
    expect(triggerSummary(f)).toBe('8-K · 0000320193-26-000002 · Apple 8-K');
  });
});

describe('registration', () => {
  it('registers the workspace and the ALRT functions', () => {
    expect(findWorkspace('alerts')?.shortcut).toBe('g t');
    expect(ALERT_FUNCTIONS.map((f) => f.mnemonic)).toEqual(['ALRT', 'ALRTR']);
    expect(listFunctions(WORKSPACES).find((f) => f.mnemonic === 'ALRTR')?.rest).toEqual(['rules']);
  });
});
