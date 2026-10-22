/**
 * Rendered output of the performance and VaR backtest panels (react-dom/server, no DOM): the IRR
 * uniqueness flag decides whether a rate is shown at all, and every backtest exception is marked in
 * the chart, the table and the text. Bodies follow analytics/performance.py and analytics/var.py.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { PerformancePanel } from './PerformanceTab';
import { BacktestPanel } from './RiskTab';
import type { Rec } from './model';

const formulas = new Map();

function performance(moneyWeighted: Rec): Rec {
  return {
    kind: 'performance',
    currency: 'USD',
    scope: 'owner:session:S1',
    as_of_ns: 1_794_578_400_000_000_000,
    period: { start: '2026-10-20', end: '2026-11-13', calendar_days: 24 },
    twr: 0.00808,
    twr_annualized: null,
    twr_annualized_reason: 'period of 24 days is shorter than one year: not annualized (GIPS 2.A.12)',
    modified_dietz: 0.00808,
    money_weighted: moneyWeighted,
    drawdown: { max_drawdown: 0.004, current_drawdown: 0.0039, peak_date: '2026-10-22', trough_date: '2026-10-23', recovery_date: '2026-10-25' },
    statistics: { n: 24, periods_per_year: 252 },
    unit_values: [
      { date: '2026-10-20', unit_value: 1000 },
      { date: '2026-10-21', unit_value: 1002.5 },
    ],
    subperiods: [],
    lineage: { formulas: ['perf.irr@2'] },
  };
}

function irrKpi(html: string): string {
  const start = html.indexOf('Money-weighted (IRR)');
  expect(start, 'the IRR figure is rendered').toBeGreaterThan(-1);
  return html.slice(start, html.indexOf('Max drawdown', start));
}

describe('performance panel: money-weighted return and its uniqueness flag', () => {
  it('shows an annualized rate only for a unique root', () => {
    const html = renderToStaticMarkup(
      createElement(PerformancePanel, { body: performance({ days: 400, irr_annualized: 0.0731, period_mwr: 0.0801, reason: null }), formulas }),
    );
    const kpi = irrKpi(html);
    expect(kpi).toContain('+7.31% a year');
    expect(kpi).toContain('unique root');
  });

  it('flags several roots as not unique and shows no rate', () => {
    const reason = '3 sign changes: the money-weighted return is not unique';
    const html = renderToStaticMarkup(createElement(PerformancePanel, { body: performance({ irr_annualized: null, reason }), formulas }));
    const kpi = irrKpi(html);
    expect(kpi).toContain('not unique');
    expect(kpi).toContain(reason);
    expect(kpi).not.toMatch(/a year|period</);
    expect(kpi).not.toMatch(/[+−]\d+\.\d+%/);
  });

  it('flags a cash-flow equation with no root', () => {
    const reason = 'no rate in (-99.9999%, 1e8%) solves the cash-flow equation';
    const kpi = irrKpi(renderToStaticMarkup(createElement(PerformancePanel, { body: performance({ irr_annualized: null, reason }), formulas })));
    expect(kpi).toContain('no solution');
    expect(kpi).not.toMatch(/[+−]\d+\.\d+%/);
  });

  it('shows only the period MWR under one year (GIPS 2.A.12)', () => {
    const kpi = irrKpi(
      renderToStaticMarkup(
        createElement(PerformancePanel, {
          body: performance({ days: 24, irr_annualized: null, period_mwr: 0.00808, reason: 'period of 24 days is shorter than one year: only the period MWR is shown (GIPS 2.A.12)' }),
          formulas,
        }),
      ),
    );
    expect(kpi).toContain('+0.808% period');
    expect(kpi).toContain('unique root, period only');
    expect(kpi).not.toContain('a year');
  });
});

describe('VaR backtest panel: exceptions', () => {
  const body: Rec = {
    kind: 'var_backtest',
    currency: 'USD',
    alpha: '0.99',
    window: 250,
    kupiec: { exceptions: 2, observations: 5, expected_exceptions: 0.05, p_value: 0.0012, lr: 10.5, df: 1, exception_rate: 0.4 },
    days: [
      { date: '2026-10-20', var_forecast: 1.5, realized_loss: 0.2, exception: false },
      { date: '2026-10-21', var_forecast: 1.5, realized_loss: 2.25, exception: true },
      { date: '2026-10-22', var_forecast: 1.6, realized_loss: -0.4, exception: false },
      { date: '2026-10-23', var_forecast: 1.6, realized_loss: 3.1, exception: true },
      { date: '2026-10-24', var_forecast: 1.7, realized_loss: 0.5, exception: false },
    ],
    skipped: [{ date: '2026-10-19', reason: 'no position has a mark and a return history' }],
    lineage: { formulas: ['risk.var_hist@1', 'risk.kupiec_pof@1'] },
  };

  it('marks each exception by shape in the chart and lists it in words', () => {
    const html = renderToStaticMarkup(createElement(BacktestPanel, { body, formulas }));
    expect(html.match(/class="pf-exception"/g)?.length, 'one triangle per exception day, plus the legend glyph').toBe(3);
    expect(html).toContain('2 exception(s)');
    expect(html).toContain('2 exception(s) marked with triangles');
    expect(html).toContain('2026-10-21 (loss 2.25 USD vs VaR 1.50 USD)');
    expect(html).toContain('2026-10-23 (loss 3.10 USD vs VaR 1.60 USD)');
    expect(html).toContain('More exceptions than expected: the VaR understates losses.');
    expect(html).toContain('1 day(s) skipped');
  });

  it('draws no exception marker when there is none', () => {
    const calm = { ...body, kupiec: { ...(body.kupiec as Rec), exceptions: 0 }, days: (body.days as Rec[]).map((d) => ({ ...d, exception: false, realized_loss: 0.1 })) };
    const html = renderToStaticMarkup(createElement(BacktestPanel, { body: calm, formulas }));
    expect(html.match(/class="pf-exception"/g)?.length, 'only the legend glyph').toBe(1);
    expect(html).toContain('0 exception(s)');
  });
});
