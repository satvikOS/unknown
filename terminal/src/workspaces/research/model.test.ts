import { describe, expect, it } from 'vitest';
import { findWorkspace, WORKSPACES } from '../registry';
import {
  artifactTone,
  gateSummary,
  isTrialView,
  manifestCurrency,
  moneyField,
  numberField,
  parseResearchRoute,
  provenanceLabel,
  segmentCounts,
  statBlocks,
  totalTrials,
  truthy,
} from './model';

describe('research registration', () => {
  it('is registered on g q after company and macro, with unique shortcuts overall', () => {
    expect(findWorkspace('research')).toMatchObject({ title: 'Research', shortcut: 'g q', order: 130 });
    expect(new Set(WORKSPACES.map((w) => w.shortcut)).size).toBe(WORKSPACES.length);
    const ids = WORKSPACES.map((w) => w.id);
    expect(ids.indexOf('company')).toBeLessThan(ids.indexOf('macro'));
    expect(ids.indexOf('macro')).toBeLessThan(ids.indexOf('research'));
  });
});

describe('research route', () => {
  it('parses every view', () => {
    expect(parseResearchRoute([])).toEqual({ tab: 'experiments', id: null });
    expect(parseResearchRoute(['experiments'])).toEqual({ tab: 'experiments', id: null });
    expect(parseResearchRoute(['experiments', 'FS1-TREND'])).toEqual({ tab: 'experiments', id: 'FS1-TREND', segment: null });
    expect(parseResearchRoute(['experiments', 'FS1-TREND', 'segment=HOLDOUT'])).toEqual({ tab: 'experiments', id: 'FS1-TREND', segment: 'HOLDOUT' });
    expect(parseResearchRoute(['experiments', 'FS1-TREND', 'segment=BOGUS'])).toEqual({ tab: 'experiments', id: 'FS1-TREND', segment: null });
    const t = parseResearchRoute(['trials', 'FS1-TREND:T00001']);
    expect(isTrialView(t) && t.trial).toBe('FS1-TREND:T00001');
    expect(parseResearchRoute(['artifacts', 'abc'])).toEqual({ tab: 'artifacts', id: 'abc' });
    expect(parseResearchRoute(['reports'])).toEqual({ tab: 'reports', id: null });
    expect(parseResearchRoute(['formulas', 'x'])).toEqual({ tab: 'formulas', id: null });
    expect(parseResearchRoute(['nonsense'])).toEqual({ tab: 'experiments', id: null });
  });
});

describe('provenance labels', () => {
  it('labels FIXTURE and UNKNOWN ORIGIN only where provenance says so', () => {
    expect(provenanceLabel({ source: 's', fixture: true, label: 'FIXTURE' })).toBe('FIXTURE');
    expect(provenanceLabel({ source: 's', fixture: null, label: 'UNKNOWN_ORIGIN' })).toBe('UNKNOWN ORIGIN');
    expect(provenanceLabel({ source: 's', fixture: false, label: null })).toBeNull();
    expect(provenanceLabel({ source: 's' })).toBeNull();
    expect(provenanceLabel(null)).toBeNull();
    // reports carry fixture as an integer column
    expect(truthy(1)).toBe(true);
    expect(truthy(0)).toBe(false);
    expect(truthy(true)).toBe(true);
  });
});

describe('trial counts and metrics', () => {
  it('lists every segment in protocol order, zeros included, plus unknown ones', () => {
    const c = { HOLDOUT: 1, DEVELOPMENT: 6, NEW_SEGMENT: 2 };
    expect(segmentCounts(c).map((s) => `${s.segment}:${s.n}`)).toEqual([
      'DEVELOPMENT:6',
      'WALK_FORWARD:0',
      'SENSITIVITY:0',
      'STRESS:0',
      'BASELINE:0',
      'REPLAY:0',
      'HOLDOUT:1',
      'NEW_SEGMENT:2',
    ]);
    expect(totalTrials(c)).toBe(9);
  });

  it('reads money from the metrics and its currency from the manifest', () => {
    const m = { final_nav: '135073.660374', costs: { net_pnl: '35073.660374', commission: '125.465000' }, sharpe_per_period: 0.13, n: 524, bad: 'x' };
    expect(moneyField(m, 'final_nav')).toBe('135073.660374');
    expect(moneyField(m, 'commission')).toBe('125.465000');
    expect(moneyField(m, 'gross_pnl')).toBeNull();
    expect(numberField(m, 'sharpe_per_period')).toBe(0.13);
    expect(numberField(m, 'bad')).toBeNull();
    expect(manifestCurrency({ config: { currency: 'USD' } })).toBe('USD');
    expect(manifestCurrency({ config: {} })).toBeNull();
    expect(manifestCurrency(null)).toBeNull();
  });

  it('summarises gate verdicts and maps artifact statuses to tones', () => {
    expect(
      gateSummary([
        { kind: 'REPLAY_EXACT', passed: true },
        { kind: 'PAIRED_BOOTSTRAP', passed: false },
        { kind: 'DSR', passed: false },
        { kind: 'DSR', passed: false },
      ]),
    ).toEqual({ passed: 1, failed: ['DSR', 'PAIRED_BOOTSTRAP'] });
    expect(artifactTone('active')).toBe('ok');
    expect(artifactTone('reproducible')).toBe('warn');
    expect(artifactTone('quarantined')).toBe('bad');
  });

  it('finds flat, holdout and walk-forward statistic blocks in a stable order', () => {
    const flat = { sharpe_per_period: 0.1, n: 524 };
    expect(statBlocks(flat)).toEqual([{ key: null, m: flat }]);
    const h = { full_run: { n: 750, total_return: 0.4 }, holdout: { n: 225, total_return: 0.05 } };
    expect(statBlocks(h).map((b) => b.key)).toEqual(['holdout', 'full_run']);
    expect(statBlocks({ folds: 5, oos: { n: 274 } }).map((b) => b.key)).toEqual(['oos']);
    expect(statBlocks({ folds: 5 })).toEqual([]);
    expect(statBlocks(null)).toEqual([]);
  });
});
