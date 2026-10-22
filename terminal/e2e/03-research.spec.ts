/**
 * Research side: the Company, Markets & macro and Research workspaces against the real API.
 *
 * 1. Before any research data exists, each workspace shows the API's unavailable reason verbatim and the command
 *    that would populate it (never placeholder data); the formula registry (served from code) is available.
 * 2. `support/seed_research.py` then fills this run's research store (the SYNTHETIC SEC issuers of the backend
 *    fixtures, a FIXTURE Treasury curve and ECB rates) and research registry (the fixture study), exactly where
 *    the API reads them.
 * 3. Company: search, tearsheet blocks, point-in-time statements (a restatement is invisible before its filing),
 *    MISSING / NO_PRICE / INCOMPLETE reasons verbatim, show-calculation with the EV bridge, the DCF recipe, an
 *    UNAVAILABLE risk-free rate with its command, a REFUSED model, and peers.
 *    Macro: curve with a missing tenor, a comparison date, a past "as of" that hides later data, ECB rates and crosses.
 *    Research: experiments with FIXTURE labels and the holdout log, trials, a trial's return chart and manifest,
 *    artifacts with status history and gate verdicts, reports, specifications and formulas.
 * 4. Accessibility checks (support/a11y.ts) on every new view in both themes, and no sideways scroll at 390 px.
 *
 * All company checks use a fixed "as of" so the result does not depend on the day the suite runs.
 */
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { auditFocus, auditWithStats, type Violation } from './support/a11y';
import { baseUrl, goKeys, login, watchProblems } from './support/app';
import { BACKEND_SRC, REPO_DIR, TERMINAL_DIR, readState } from './support/backend';

test.describe.configure({ mode: 'serial' });

const CIK = 9000001;
const AS_OF = '2026-10-22T12:00:00Z';
const PRICE = { price: '50.00', currency: 'USD', source: 'FIXTURE price (e2e)', time: '2026-10-21T20:00:00Z' };

function main(page: Page) {
  return page.locator('#main');
}

async function settle(page: Page): Promise<void> {
  await expect(page.locator('h1#ws-title')).toBeVisible();
  await expect(page.getByText(/^Loading\b/)).toHaveCount(0, { timeout: 20_000 });
}

async function open(page: Page, hash: string): Promise<void> {
  await page.goto(`${baseUrl()}/${hash}`);
  await settle(page);
}

let seeded: { experiment: string; trial: string; holdoutTrial: string; artifact: string; report: string; spec: string } | null = null;

async function api<T>(page: Page, path: string): Promise<T> {
  const r = await page.request.get(`${baseUrl()}${path}`);
  expect(r.ok(), `${path} answered ${r.status()}`).toBe(true);
  return (await r.json()) as T;
}

test('before any research data: the API reason and the command that populates it', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);

  await open(page, `#/company/${CIK}`);
  await expect(main(page).locator('.rx-reason', { hasText: 'research store not initialised: ingest an issuer first (python -m unknown.data.sec ingest --cik <CIK>)' }).first()).toBeVisible();
  await expect(main(page).locator('code.rx-command', { hasText: 'python -m unknown.data.sec ingest --cik <CIK>' }).first()).toBeVisible();

  await open(page, '#/macro');
  await expect(main(page).locator('code.rx-command', { hasText: 'python -m unknown.data.sources.treasury ingest --month YYYYMM' })).toBeVisible();
  await expect(main(page).locator('code.rx-command', { hasText: 'python -m unknown.data.sources.ecb ingest --last 1' })).toBeVisible();

  await open(page, '#/research');
  await expect(main(page).getByText(/research registry not found at .*experiments\.sqlite; record an experiment or run `python -m unknown\.research\.study` first/)).toBeVisible();
  await expect(main(page).locator('code.rx-command', { hasText: 'python -m unknown.research.study' })).toBeVisible();
  // The formula registry is served from code, so it is available without a registry.
  await open(page, '#/research/formulas');
  await expect(main(page).getByRole('row', { name: /dsr@1/ })).toBeVisible();
  expect(problems.filter((p) => !/Failed to load resource/.test(p))).toEqual([]);
});

test('seed the run with the fixture study and the SEC, Treasury and ECB fixtures', async ({ page }) => {
  test.setTimeout(300_000);
  const s = readState();
  const out = execFileSync(s.python, [join(TERMINAL_DIR, 'e2e', 'support', 'seed_research.py'), '--home', s.home], {
    cwd: join(REPO_DIR, 'backend'),
    env: { ...process.env, UNKNOWN_HOME: s.home, PYTHONPATH: BACKEND_SRC, PYTHONDONTWRITEBYTECODE: '1' },
    encoding: 'utf8',
    timeout: 280_000,
  });
  const summary = JSON.parse(out.trim().split('\n').pop()!) as { issuers: number[] };
  expect(summary.issuers).toContain(CIK);

  await login(page);
  const exps = await api<{ data: { experiment_id: string }[] }>(page, '/api/research/experiments');
  const experiment = exps.data.find((e) => e.experiment_id.endsWith('-TREND'))!.experiment_id;
  const trials = await api<{ data: { trial_id: string; segment: string }[] }>(page, `/api/research/experiments/${experiment}/trials`);
  const arts = await api<{ data: { artifact_hash: string }[] }>(page, '/api/research/artifacts');
  const reps = await api<{ data: { report_id: string }[] }>(page, '/api/research/reports');
  const specs = await api<{ data: { spec_hash: string }[] }>(page, '/api/research/strategies');
  seeded = {
    experiment,
    trial: trials.data.find((t) => t.segment === 'DEVELOPMENT')!.trial_id,
    holdoutTrial: trials.data.find((t) => t.segment === 'HOLDOUT')!.trial_id,
    artifact: arts.data[0]!.artifact_hash,
    report: reps.data[0]!.report_id,
    spec: specs.data[0]!.spec_hash,
  };
});

test('company: search, tearsheet, point-in-time statements, metrics, DCF and peers', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchProblems(page);
  await login(page);
  await goKeys(page, 'e');
  await expect(page).toHaveURL(/#\/company$/);
  await settle(page);

  // Search: candidates are ranked, never selected; each source states its status.
  await page.getByLabel('Name, ticker, CIK, ISIN or FIGI').fill('Fixture Widgets');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.getByRole('row', { name: /Fixture Widgets Inc \(synthetic\).*CIK 9000001.*NAME_PREFIX/ })).toBeVisible();
  await expect(main(page).getByText('no snapshot received at or before the as-of time (never ingested)')).toBeVisible();
  await page.getByRole('link', { name: 'Fixture Widgets Inc (synthetic)' }).click();
  await expect(page).toHaveURL(new RegExp(`#/company/${CIK}/tearsheet`));
  const controls = page.getByRole('form', { name: 'Point in time and price input' });
  await expect(controls).toBeVisible();
  await settle(page);

  // Point in time: the as-of control applies to every tab.
  await controls.getByLabel('As of', { exact: true }).fill(AS_OF);
  await controls.getByRole('button', { name: 'Apply' }).click();
  await expect(page).toHaveURL(/as_of%3D2026-10-22T12%3A00%3A00Z/);
  await settle(page);

  // Tearsheet: blocks fail independently and say why.
  await expect(main(page).locator('.rx-identity .rx-name')).toHaveText('Fixture Widgets Inc (synthetic)');
  await expect(main(page).getByText('the research tearsheet has no quote feed; supply price, price_currency, price_source and price_time to compute multiples')).toBeVisible();
  await expect(main(page).getByText('no licensed event calendar (e.g. Wall Street Horizon via IBKR) is subscribed; see filings for dates')).toBeVisible();
  await expect(main(page).getByRole('heading', { name: /Key figures/ })).toBeVisible();

  // Statements: the FY2025 revenue was restated by a 10-K/A accepted 2026-03-02.
  await page.getByRole('tab', { name: 'Statements' }).click();
  await settle(page);
  const restated = page.getByRole('button', { name: 'Revenue, FY2025: 450000, restated' });
  await expect(restated).toBeVisible();
  await expect(page.getByRole('button', { name: 'Research and development, FY2025: missing, NOT_REPORTED' })).toBeVisible();
  await restated.click();
  await expect(main(page).getByRole('heading', { name: 'Earlier values (restatement history)' })).toBeVisible();
  await expect(main(page).locator('.rx-cell-detail')).toContainText('0009000001-26-000005');
  await expect(main(page).locator('.rx-cell-detail')).toContainText('460,000.00');
  await expect(main(page).getByRole('heading', { name: 'Coverage' })).toBeVisible();
  // Before the amendment was accepted, the original value is what was known.
  await open(page, `#/company/${CIK}/statements/as_of=2026-02-01`);
  await expect(page.getByRole('button', { name: 'Revenue, FY2025: 460000' })).toBeVisible();
  // Quarters at that time end with FY2026 Q1; a quarter that cannot be derived says so verbatim.
  await page.getByRole('group', { name: 'Basis' }).getByRole('button', { name: 'Quarters' }).click();
  await settle(page);
  await expect(main(page).getByRole('columnheader', { name: /FY2026 Q1/ })).toBeVisible();
  await expect(main(page).getByRole('columnheader', { name: /FY2026 Q2/ })).toHaveCount(0);

  // Metrics without a price: price-based metrics are NO_PRICE with the API's reason.
  await open(page, `#/company/${CIK}/metrics/as_of=${AS_OF}`);
  await expect(page.getByRole('row', { name: /market cap.*NO_PRICE.*no price input supplied \(price, currency, source and time are required\)/ })).toBeVisible();
  // With a price input: EV is INCOMPLETE and says which components were not reported.
  await main(page).locator('summary', { hasText: 'Price input for multiples' }).click();
  await page.getByLabel('Price', { exact: true }).fill(PRICE.price);
  await page.getByLabel('Currency', { exact: true }).fill(PRICE.currency);
  await page.getByLabel('Source (required)').fill(PRICE.source);
  await page.getByLabel('Observed at (≤ as of)').fill(PRICE.time);
  await page.getByRole('form', { name: 'Point in time and price input' }).getByRole('button', { name: 'Apply' }).click();
  await settle(page);
  const ev = page.getByRole('row', { name: /ev standard/ }).first();
  await expect(ev).toContainText('INCOMPLETE');
  await expect(ev).toContainText('components not reported by the filer at 2026-06-27: preferred_stock, noncontrolling_interest; excluded from the sum (not treated as reported zeros)');
  await ev.dblclick();
  const calc = page.getByRole('heading', { name: 'Show calculation · ev_standard' });
  await expect(calc).toBeVisible();
  await expect(main(page).getByRole('table', { name: /Components and their contribution/ })).toContainText('NOT_REPORTED');
  await expect(main(page).getByText('FIXTURE price (e2e)').first()).toBeVisible();

  // DCF: the recipe, then an unavailable risk-free rate (with its command), then a refusal.
  await page.getByRole('tab', { name: 'DCF' }).click();
  await settle(page);
  await page.getByLabel('Terminal growth g (decimal, required)').fill('0.02');
  await page.getByLabel('FCFF per year (comma-separated)').fill('100, 110, 120');
  await page.getByLabel('Beta').fill('1.0');
  await page.getByLabel('Equity risk premium').fill('0.05');
  await page.getByLabel('Pre-tax cost of debt').fill('0.05');
  await page.getByLabel('Tax rate (optional: effective TTM)').fill('0.21');
  await page.getByLabel('Equity weight (optional with a price)').fill('0.8');
  await page.getByLabel('Debt weight (optional with a price)').fill('0.2');
  await page.getByRole('button', { name: 'Run DCF' }).click();
  await expect(main(page).getByRole('heading', { name: /WACC build-up · wacc_capm@1/ })).toBeVisible();
  await expect(main(page).getByRole('row', { name: /rf.*0\.0424.*US Treasury par yield 10Y on 2026-10-21/ })).toBeVisible();
  await expect(main(page).locator('.rx-tile', { hasText: 'Enterprise value' })).toContainText('1,845.03USD');
  await expect(main(page).locator('.rx-tile', { hasText: 'WACC' }).first()).toContainText('0.0818200000');
  await expect(main(page).getByRole('table', { name: /by WACC and g/ })).toBeVisible();
  await expect(main(page).getByRole('img', { name: /Per-share value ranges in USD/ })).toBeVisible();
  await page.getByLabel('Curve date (optional)').fill('2026-10-01');
  await page.getByRole('button', { name: 'Run DCF' }).click();
  await expect(main(page).getByText('no Treasury par yield for tenor 10Y available at the as-of time (ingest with python -m unknown.data.sources.treasury)')).toBeVisible();
  await expect(main(page).locator('code.rx-command', { hasText: 'python -m unknown.data.sources.treasury' })).toBeVisible();
  await page.getByRole('radio', { name: 'Give WACC' }).check();
  await page.getByLabel('WACC (decimal)').fill('0.09');
  await page.getByLabel('Terminal growth g (decimal, required)').fill('0.10');
  await page.getByRole('button', { name: 'Run DCF' }).click();
  await expect(main(page).getByText('WACC (0.09) must exceed terminal growth g (0.10) (book eq. 17.1)')).toBeVisible();
  await expect(main(page).getByText('REFUSED', { exact: true })).toBeVisible();

  // Peers: the rule, members and statistics.
  await page.getByRole('tab', { name: 'Peers' }).click();
  await settle(page);
  await expect(main(page).getByRole('heading', { name: /Peer group · peers_sic4@1/ })).toBeVisible();
  await expect(main(page).getByRole('link', { name: 'Fixture Peer Two (synthetic)' })).toBeVisible();
  await expect(main(page).getByRole('table', { name: /Peer statistics/ })).toContainText('PERCENTILE.INC');
  expect(problems.filter((p) => !/Failed to load resource/.test(p))).toEqual([]);
});

test('markets and macro: par curve with gaps, comparison, point in time, ECB rates', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  await goKeys(page, 'm');
  await expect(page).toHaveURL(/#\/macro$/);
  await settle(page);
  await expect(page.getByRole('row', { name: /US Treasury daily par yield curve.*OK.*27.*2026-10-20 → 2026-10-21/ })).toBeVisible();
  await expect(main(page).getByRole('img', { name: 'Treasury par yield curve on 2026-10-21, in percent' })).toBeVisible();
  await expect(main(page).getByText('Missing tenors on 2026-10-21:')).toBeVisible();
  await expect(page.getByRole('row', { name: /1\.5M missing on 2026-10-21/ })).toBeVisible();
  await expect(main(page).locator('.rx-identity')).toContainText('10Y − 2Y slope 36 basis points');

  await page.getByLabel('Curve date (empty = latest)').fill('2026-10-20');
  await page.getByLabel('Compare with date').fill('2026-10-21');
  await page.getByRole('button', { name: 'Show' }).first().click();
  await settle(page);
  await expect(main(page).getByRole('img', { name: 'Treasury par yield curve on 2026-10-20 and 2026-10-21, in percent' })).toBeVisible();
  await expect(main(page).locator('.rx-identity')).toContainText('10Y − 2Y slope 35 basis points');
  await expect(page.getByRole('row', { name: /^10Y 10 4\.21 4\.24/ })).toBeVisible();

  // Before the 2026-10-21 curve was published (22:30 UTC) only 2026-10-20 is visible: no look-ahead.
  await open(page, '#/macro/as_of=2026-10-21T22:00:00Z');
  await expect(page.getByRole('row', { name: /US Treasury daily par yield curve.*14.*2026-10-20 → 2026-10-20/ })).toBeVisible();
  await expect(main(page).locator('.rx-identity')).toContainText('curve date 2026-10-20');

  await open(page, '#/macro/base=USD');
  await expect(main(page).getByText(/published for information purposes only/)).toBeVisible();
  await expect(page.getByRole('row', { name: /^USD 1\.1634 USD per 1 EUR 2026-10-21/ })).toBeVisible();
  await expect(page.getByRole('row', { name: /^EUR 1\.1634000000 USD per 1 EUR/ })).toBeVisible();
  expect(problems.filter((p) => !/Failed to load resource/.test(p))).toEqual([]);
});

test('research: experiments, trials, artifacts with gate verdicts, reports, specifications', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchProblems(page);
  const s = seeded!;
  await login(page);
  await goKeys(page, 'q');
  await expect(page).toHaveURL(/#\/research$/);
  await settle(page);
  await expect(page.getByRole('tab', { name: /Experiments\s*3/ })).toBeVisible();
  const row = page.getByRole('row', { name: new RegExp(`${s.experiment}.*FIXTURE`) });
  await expect(row).toBeVisible();
  await row.dblclick();
  await expect(page).toHaveURL(new RegExp(`#/research/experiments/${s.experiment}`));
  await settle(page);
  await expect(main(page).getByRole('heading', { name: 'Pre-registration' })).toBeVisible();
  await expect(main(page).locator('.rx-identity')).toContainText('FIXTURE');
  await expect(main(page).getByRole('heading', { name: 'Holdout access log · 1' })).toBeVisible();
  await expect(main(page).getByText('single pre-registered holdout evaluation')).toBeVisible();
  await expect(main(page).getByText('Trials, all statuses (M)')).toBeVisible();
  await page.getByLabel('Segment').selectOption('HOLDOUT');
  await settle(page);
  await expect(main(page).getByRole('heading', { name: 'Trials · 1 in HOLDOUT' })).toBeVisible();
  await expect(page.getByRole('row', { name: new RegExp(`${s.holdoutTrial}.*HOLDOUT.*SUCCEEDED.*\\[holdout\\]`) })).toBeVisible();

  await open(page, `#/research/trials/${s.trial}`);
  await expect(main(page).getByRole('img', { name: /^Cumulative return of fast_span=\d+,slow_span=\d+, / })).toBeVisible();
  await expect(main(page).locator("figcaption").getByText(/sessions; cumulative .* \(compounded from simple returns, return\.simple@1\)/)).toBeVisible();
  await expect(main(page).getByText('Final NAV')).toBeVisible();
  await expect(main(page).getByRole('heading', { name: 'Money (USD)' })).toBeVisible();
  await expect(main(page).getByRole('heading', { name: 'Reproducibility manifest' })).toBeVisible();
  await expect(main(page).getByText('unknown.research.engine.daily@1').first()).toBeVisible();
  await open(page, `#/research/trials/${s.holdoutTrial}`);
  await expect(main(page).getByRole('heading', { name: 'Holdout window' })).toBeVisible();

  await open(page, `#/research/artifacts/${s.artifact}`);
  await expect(main(page).getByRole('table', { name: /Append-only transitions/ })).toContainText('reproducible');
  await expect(main(page).getByText(/The API re-verified the whole transition log/)).toBeVisible();
  const bootstrap = main(page).locator('li', { hasText: 'PAIRED_BOOTSTRAP' }).first();
  await expect(bootstrap).toContainText('FAIL');
  await expect(bootstrap).toContainText(/conservative lower bound .* does not exceed the hurdle/);
  await expect(main(page).locator('li', { hasText: 'REPLAY_EXACT' }).first()).toContainText('PASS');

  await open(page, `#/research/reports/${s.report}`);
  await expect(main(page).locator('.rx-identity')).toContainText('TREND_DAILY_V1 versus baselines on SYNTHETIC fixture series');
  await expect(main(page).locator('.rx-identity')).toContainText('FIXTURE');
  await expect(main(page).getByText(/not market data, not evidence of profitability/).first()).toBeVisible();

  await open(page, `#/research/strategies/${s.spec}`);
  await expect(main(page).getByText('FIXTURE DATA ALLOWED')).toBeVisible();
  await open(page, '#/research/formulas');
  await page.getByLabel('Filter formulas').fill('deflated');
  await expect(page.getByRole('row', { name: /dsr@1 deflated Sharpe ratio/ })).toBeVisible();
  expect(problems.filter((p) => !/Failed to load resource/.test(p))).toEqual([]);
});

function views(): string[] {
  const s = seeded!;
  return [
    '#/company',
    `#/company/search/q=Fixture`,
    `#/company/${CIK}/tearsheet/as_of=${AS_OF}`,
    `#/company/${CIK}/statements/as_of=${AS_OF}`,
    `#/company/${CIK}/metrics/as_of=${AS_OF}/price=${PRICE.price}/price_currency=USD/price_source=FIXTURE price (e2e)/price_time=${PRICE.time}`,
    `#/company/${CIK}/dcf/as_of=${AS_OF}`,
    `#/company/${CIK}/peers/as_of=${AS_OF}`,
    `#/company/${CIK}/filings/as_of=${AS_OF}`,
    '#/macro',
    '#/macro/date=2026-10-20/compare=2026-10-21/base=USD',
    '#/research',
    `#/research/experiments/${s.experiment}`,
    `#/research/trials/${s.trial}`,
    '#/research/artifacts',
    `#/research/artifacts/${s.artifact}`,
    '#/research/strategies',
    `#/research/strategies/${s.spec}`,
    '#/research/reports',
    `#/research/reports/${s.report}`,
    '#/research/formulas',
  ];
}

for (const scheme of ['dark', 'light'] as const) {
  test(`research-side views pass the accessibility checks (${scheme} theme)`, async ({ browser }) => {
    test.setTimeout(240_000);
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
    const page = await context.newPage();
    const problems = watchProblems(page);
    await login(page);
    const found: Violation[] = [];
    for (const r of views()) {
      await open(page, r);
      await page.waitForTimeout(300);
      const a = await auditWithStats(page, r);
      found.push(...a.violations);
      expect.soft(a.stats.textChecked, `text elements checked on ${r}`).toBeGreaterThan(20);
      const shots = process.env.E2E_SCREENSHOTS;
      if (shots) await page.screenshot({ path: `${shots}/${scheme}-${r.replace(/[^a-z0-9~-]+/gi, '_').replace(/^_+/, '').slice(0, 80)}.png`, fullPage: true });
    }
    // Keyboard focus indicators through the statement cells, then a form, its radio groups and a computed DCF result.
    await open(page, `#/company/${CIK}/statements/as_of=${AS_OF}`);
    found.push(...(await auditFocus(page, 'statements (Tab order)', 60)));
    await open(page, `#/company/${CIK}/dcf/as_of=${AS_OF}`);
    found.push(...(await auditFocus(page, 'dcf form (Tab order)', 60)));
    await page.getByLabel('Terminal growth g (decimal, required)').fill('0.02');
    await page.getByLabel('FCFF per year (comma-separated)').fill('100, 110, 120');
    await page.getByRole('radio', { name: 'Give WACC' }).check();
    await page.getByLabel('WACC (decimal)').fill('0.09');
    await page.getByRole('button', { name: 'Run DCF' }).click();
    await expect(main(page).getByRole('heading', { name: /Valuation · dcf_fcff_gordon@1/ })).toBeVisible();
    found.push(...(await auditWithStats(page, 'dcf result')).violations);
    const report = found.map((v) => `[${v.where}] ${v.rule} ${v.target}: ${v.detail}`).join('\n');
    expect(found, report).toEqual([]);
    expect(problems.filter((p) => !/Failed to load resource/.test(p))).toEqual([]);
    await context.close();
  });
}

test('research-side views fit a 390 px screen without sideways page scroll', async ({ browser }) => {
  test.setTimeout(180_000);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: 'dark' });
  const page = await context.newPage();
  await login(page);
  for (const r of views()) {
    await open(page, r);
    const { sw, cw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
    expect.soft(sw, `${r} at 390px scrolls sideways (${sw} > ${cw})`).toBeLessThanOrEqual(cw);
  }
  await context.close();
});
