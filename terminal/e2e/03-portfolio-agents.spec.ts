/**
 * Portfolio, Market data and Research jobs against the real core and API started by global setup.
 * Runs after 01-flow (which leaves a session, a filled FIXTURE position and a STOP/START history);
 * the portfolio checks that need a session say so and skip without one. Backend steps the terminal
 * cannot take itself (recording a valuation snapshot, writing a FIXTURE market-data status file,
 * writing the owner policy, running the model worker once) are in support/plugins.ts.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';
import { audit, auditWithStats, type Violation } from './support/a11y';
import { baseUrl, header, login, watchProblems } from './support/app';
import { E2E_PER_JOB_CAP, recordSnapshot, runWorkerOnce, writeMarketDataStatus, writeOwnerPolicyProviderDisabled } from './support/plugins';

test.describe.configure({ mode: 'serial' });

let page: Page;
let problems: string[];
const run = { sessionId: '', failedJobId: '', cancelledJobId: '' };

async function api<T>(path: string): Promise<T> {
  const r = await page.request.get(`${baseUrl()}${path}`);
  expect(r.status(), path).toBe(200);
  return (await r.json()) as T;
}

/** Exact decimal string as the terminal shows money: grouped, trailing zeros trimmed to 2 places. */
function money2(v: string): string {
  const neg = v.startsWith('-');
  const [i = '0', f = ''] = v.replace(/^-/, '').split('.');
  let frac = f.replace(/0+$/, '');
  if (frac.length < 2) frac = frac.padEnd(2, '0');
  return `${neg ? '−' : ''}${i.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${frac}`;
}

function main(): Locator {
  return page.locator('#main');
}

async function refresh(): Promise<void> {
  await main().getByRole('button', { name: 'Refresh' }).click();
}

async function settle(p: Page): Promise<void> {
  await expect(p.locator('h1#ws-title')).toBeVisible();
  await p.waitForTimeout(400);
  await expect(p.getByText(/^(Loading|Running preflight)\b/)).toHaveCount(0, { timeout: 15_000 });
}

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
  page = await context.newPage();
  problems = watchProblems(page);
  await login(page);
  const sessions = await api<{ session_id: string; state: string }[]>('/api/sessions');
  run.sessionId = sessions[0]?.session_id ?? '';
});

test.afterAll(async () => {
  await page.context().close();
});

// ------------------------------------------------------------------ portfolio

test('portfolio holdings show owners and cash exactly as the API computes them', async () => {
  await page.goto(`${baseUrl()}/#/portfolio`);
  await expect(page.getByRole('heading', { name: 'Portfolio', level: 1 })).toBeVisible();
  await expect(main().getByRole('tab', { name: 'Holdings' })).toHaveAttribute('aria-selected', 'true');
  type Owner = { owner: string; cash: { settled_cash: string; available_cash: string } };
  const h = await api<{ owners: Owner[]; positions: { instrument_id: string }[]; fixture_data: boolean }>('/api/portfolio/holdings?scope=account&basis=bid');
  const unalloc = h.owners.find((o) => o.owner === 'unallocated');
  expect(unalloc, 'the bootstrap authorizes capital into unallocated').toBeTruthy();
  const owners = main().getByRole('table', { name: 'Holdings by owner' });
  const row = owners.getByRole('row').filter({ hasText: /^unallocated/ });
  await expect(row).toContainText(`${money2(unalloc!.cash.settled_cash)}USD`);
  await expect(row).toContainText('system');
  await expect(main().getByText(/^NAV marked, USD$/)).toBeVisible();
  await expect(main().getByRole('list', { name: 'Marked NAV by owner' })).toContainText('Unallocated');
  if (h.positions.length) {
    const positions = main().getByRole('table', { name: 'Positions with marks' });
    await expect(positions.getByRole('row').filter({ hasText: h.positions[0]!.instrument_id })).toBeVisible();
  }
  if (h.fixture_data) await expect(main().locator('.pf-note').getByText('FIXTURE', { exact: true })).toBeVisible();
  // Scope to one owner: only that owner's row remains.
  await main().getByLabel('Scope').selectOption('owner:unallocated');
  await expect(owners.getByRole('row')).toHaveCount(2); // header + unallocated
  await main().getByLabel('Scope').selectOption('account');
});

test('performance and snapshots: the no-history reason, then one recorded snapshot', async () => {
  await page.goto(`${baseUrl()}/#/portfolio/snapshots`);
  await expect(main().getByText('Valuation snapshots unavailable.')).toBeVisible();
  await expect(main().locator('code', { hasText: 'python -m unknown.analytics.snapshots record --account <key>' })).toBeVisible();
  recordSnapshot();
  await refresh();
  const runs = main().getByRole('table', { name: 'Valuation snapshot runs' });
  await expect(runs.getByRole('row')).toHaveCount(2);
  await expect(runs).toContainText('FIXTURE');
  await main().getByRole('tab', { name: 'Performance' }).click();
  await expect(page).toHaveURL(/#\/portfolio\/performance$/);
  await expect(main().getByText('No time-weighted return.')).toBeVisible();
  await expect(main().getByText(/fewer than two complete valuations/)).toBeVisible();
  await expect(main().getByText('Fewer than two complete snapshots: no day to decompose.')).toBeVisible();
});

test('exposure and VaR state their basis; the backtest says it has no day yet', async () => {
  await page.goto(`${baseUrl()}/#/portfolio/risk`);
  await expect(main().getByText(/VaR does not represent the worst-case loss/)).toBeVisible();
  await expect(main().getByRole('heading', { name: 'Exposure' })).toBeVisible();
  const h = await api<{ positions: unknown[] }>('/api/portfolio/holdings?scope=account');
  if (h.positions.length) await expect(main().getByText(/^Gross \/ net, USD$/)).toBeVisible();
  else await expect(main().getByText('No positions in this scope: no exposure.')).toBeVisible();
  await expect(main().getByText(/No backtest day yet/)).toBeVisible();
  await main().getByRole('button', { name: '95%' }).click();
  await expect(main().getByText(/Confidence 0\.95/)).toBeVisible();
});

test('stress: a hypothetical overnight gap computed on a private copy of the core', async () => {
  await page.goto(`${baseUrl()}/#/portfolio/stress`);
  await main().getByLabel(/^Gap/).fill('-1');
  await expect(main().getByText('A shock must be greater than -1 (a fall of less than 100%).')).toBeVisible();
  await expect(main().getByRole('button', { name: 'Run scenario' })).toBeDisabled();
  await main().getByLabel(/^Gap/).fill('-0.10');
  // Compare the table with the response the page itself received: FIXTURE quotes move every step, so a
  // second request would value the positions at other prices and could cross a threshold differently.
  const shown = page.waitForResponse((r) => r.url().includes('/api/portfolio/stress/overnight_gap') && r.request().method() === 'GET');
  await main().getByRole('button', { name: 'Run scenario' }).click();
  const response = await shown;
  expect(response.status()).toBe(200);
  await expect(main().getByText('HYPOTHETICAL', { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(main().getByText(/computed on a private copy/i).first()).toBeVisible();
  await expect(main().getByText(/on core seq \d+/).first()).toBeVisible();
  const h = await api<{ positions: unknown[] }>('/api/portfolio/holdings?scope=account');
  if (h.positions.length) await expect(main().getByText(/^Scenario P&L, USD$/)).toBeVisible();
  // Every loss threshold shows its level with its currency and the backend's crossed decision, never a guess.
  type Threshold = { threshold: string; owner?: string; level: string; crossed: boolean | null };
  const s = (await response.json()) as { thresholds: Threshold[]; parameters: { gap: string } };
  expect(s.parameters.gap).toBe('-0.10');
  const table = main().getByRole('table', { name: 'Loss thresholds under the scenario' });
  if (s.thresholds.length) await expect(table.locator('tbody tr')).toHaveCount(s.thresholds.length);
  for (const t of s.thresholds) {
    const row = table.locator('tbody tr').filter({ hasText: t.threshold }).filter({ hasText: t.owner ? 'session:' : 'system' });
    await expect(row.locator('td').nth(4), `${t.threshold} level`).toHaveText(`${money2(t.level)}USD`);
    const crossed = row.locator('td').nth(2);
    if (t.crossed === false) await expect(crossed, `${t.threshold} crossed`).toHaveText('no');
    else await expect(crossed.locator('.pill-text'), `${t.threshold} crossed`).toHaveText(t.crossed ? 'crossed' : 'unknown');
  }
});

test('what-if evaluates an intent through the real gate and creates no order', async () => {
  test.skip(!run.sessionId, 'needs the session that 01-flow creates');
  const ordersBefore = (await api<unknown[]>('/api/orders')).length;
  const quotes = await api<Record<string, { ask: string | null }>>('/api/quotes');
  const ask = quotes['FIX-EQ-1']?.ask ?? '10.00';
  await page.goto(`${baseUrl()}/#/portfolio/whatif`);
  await main().getByLabel('Session').selectOption(run.sessionId);
  await main().getByLabel('Instrument').selectOption('FIX-EQ-1');
  await main().getByLabel('Quantity').fill('1');
  await main().getByLabel('Limit price').fill(ask);
  await main().getByRole('button', { name: 'Evaluate (read-only)' }).click();
  await expect(main().getByText('HYPOTHETICAL', { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(main().locator('.pill-text').filter({ hasText: /^would be (approved|refused)/ })).toBeVisible();
  await expect(main().getByRole('heading', { name: 'Pretrade decision (hypothetical)' })).toBeVisible();
  await expect(main().getByRole('table', { name: /Pretrade checks for the hypothetical intent/ })).toBeVisible();
  expect((await api<unknown[]>('/api/orders')).length, 'a what-if never creates an order').toBe(ordersBefore);
});

test('allocation proposals are labelled PROPOSAL ONLY and offer no way to send them', async () => {
  await page.goto(`${baseUrl()}/#/portfolio/allocation`);
  if (run.sessionId) await main().getByLabel('Scope').selectOption(`owner:session:${run.sessionId}`);
  await expect(main().getByText('PROPOSAL ONLY', { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(main().getByRole('table', { name: 'Current and proposed weights' })).toBeVisible();
  const buttons = await main().getByRole('button').allInnerTexts();
  expect(buttons.filter((b) => /send|place|submit|execute|buy|sell/i.test(b)), 'no control can turn a proposal into an order').toEqual([]);
});

// ------------------------------------------------------------------ market data

test('market data: no session, then FIXTURE status with delivered types, consolidation and entitlement errors', async () => {
  await page.goto(`${baseUrl()}/#/marketdata`);
  await expect(page.getByRole('heading', { name: 'Market data', level: 1 })).toBeVisible();
  await expect(main().getByText('No market-data session status.')).toBeVisible();
  await expect(main().getByText(/market-data session is not running/)).toBeVisible();

  writeMarketDataStatus(0);
  await refresh();
  await expect(main().locator('.pill-text', { hasText: 'connected · paper verified' })).toBeVisible();
  await expect(main().getByText('3/7')).toBeVisible();
  const subs = main().getByRole('table', { name: 'Market-data subscriptions' });
  const live = subs.getByRole('row').filter({ hasText: 'FIX-MD-LIVE' });
  await expect(live).toContainText('LIVE');
  await expect(live).toContainText('non-consolidated');
  const delayed = subs.getByRole('row').filter({ hasText: 'FIX-MD-DELAYED' });
  await expect(delayed).toContainText('delayed (requested live)');
  await expect(delayed).toContainText('differs');
  const nosub = subs.getByRole('row').filter({ hasText: 'FIX-MD-NOSUB' });
  await expect(nosub).toContainText('UNAVAILABLE');
  await expect(nosub).toContainText('NOT_SUBSCRIBED');
  await expect(subs.getByRole('row').filter({ hasText: 'FIX-MD-COMPETE' })).toContainText('COMPETING_SESSION');
  await expect(subs.getByRole('row').filter({ hasText: 'FIX-MD-PARTIAL' })).toContainText('PARTIALLY_SUBSCRIBED');

  await nosub.getByRole('link', { name: 'FIX-MD-NOSUB' }).click();
  await expect(page).toHaveURL(/#\/marketdata\/FIX-MD-NOSUB$/);
  const detail = main().locator('section', { has: page.getByRole('heading', { name: 'Subscription FIX-MD-NOSUB' }) });
  await expect(detail).toContainText('IBKR 354');
  await expect(detail).toContainText('Not subscribed to requested market data');
  await expect(detail).toContainText('The status file holds no prices');
});

test('market data: a stale status file reads as not live', async () => {
  writeMarketDataStatus(300);
  await refresh();
  await expect(main().getByText('Status file is stale.')).toBeVisible();
  const subs = main().getByRole('table', { name: 'Market-data subscriptions' });
  await expect(subs.getByRole('row').filter({ hasText: 'FIX-MD-LIVE' })).toContainText('STATUS_FILE_STALE');
  await expect(main().locator('.pill-text', { hasText: 'status stale' })).toBeVisible();
  await expect(main().getByText(/0 live/)).toBeVisible();
});

// ------------------------------------------------------------------ research jobs

async function ask(question: string, opts: { acknowledge?: boolean } = {}): Promise<void> {
  await page.goto(`${baseUrl()}/#/research-jobs/~new`);
  await expect(page.getByRole('heading', { name: 'Ask a research question', level: 1 })).toBeVisible();
  await main().getByRole('textbox', { name: 'Question' }).fill(question);
  if (opts.acknowledge !== false) await main().getByLabel(/I understand that this job may spend money/).check();
  await main().getByRole('button', { name: 'Queue research job' }).click();
}

test('research jobs: the spend notice is required, and without an owner policy the API refuses', async () => {
  await page.goto(`${baseUrl()}/#/research-jobs`);
  await expect(page.getByRole('heading', { name: 'Research jobs', level: 1 })).toBeVisible();
  await expect(main().getByText('No research jobs yet. Ask a question to queue one.')).toBeVisible();
  await ask('Did revenue grow in fiscal 2025?', { acknowledge: false });
  await expect(main().getByText('Confirm that this job may spend money up to its cap.')).toBeVisible();
  await expect(main().getByText('A research job may spend money.')).toBeVisible();
  await main().getByLabel(/I understand that this job may spend money/).check();
  await main().getByRole('button', { name: 'Queue research job' }).click();
  const refusal = main().getByRole('alert').filter({ hasText: 'The API did not queue the job.' });
  await expect(refusal).toContainText('paid model calls are disabled');
});

test('research jobs: queued with its spend cap, a duplicate returns the same job, cancel', async () => {
  writeOwnerPolicyProviderDisabled();
  const q1 = 'E2E question one: did revenue grow?';
  await ask(q1);
  const result = main().getByRole('status').filter({ hasText: 'Job queued.' });
  await expect(result).toContainText('QUEUED');
  await expect(result).toContainText(`${E2E_PER_JOB_CAP} USD`);
  await expect(result).toContainText('Nothing has been sent to a model yet.');
  const jobId = (await result.locator('a.cmd-id').innerText()).trim();
  expect(jobId).toMatch(/^rj-[0-9a-f]+$/);

  await ask(q1);
  const dup = main().getByRole('status').filter({ hasText: 'No new job: an equivalent job already exists.' });
  await expect(dup.locator('a.cmd-id')).toHaveText(jobId);
  // The spend panel now reports the cap the API recorded on that job.
  await expect(main().getByText(/The most recent job \(/)).toContainText(`capped at ${E2E_PER_JOB_CAP} USD`);

  await dup.locator('a.cmd-id').click();
  await expect(page).toHaveURL(new RegExp(`#/research-jobs/${jobId}$`));
  const state = main().locator('section', { has: page.getByRole('heading', { name: 'State' }) });
  await expect(state.locator('.pill-text').first()).toHaveText('QUEUED');
  await expect(state).toContainText('nothing has been spent');
  await expect(main().getByText(/the spend ledger does not exist yet/)).toBeVisible();
  await expect(main().getByText('No proposal yet: the job has not finished.')).toBeVisible();
  await state.getByLabel('Cancel reason (optional)').fill('end-to-end check');
  await state.getByRole('button', { name: 'Cancel job' }).click();
  await expect(state.getByRole('status').filter({ hasText: 'Cancelled.' })).toBeVisible();
  await expect(state.locator('.pill-text').first()).toHaveText('CANCELLED');
  run.cancelledJobId = jobId;
});

test('research jobs: the real worker refuses a job before any model call; FAILED with its audit trail', async () => {
  await ask('E2E question two: what were total assets?');
  const result = main().getByRole('status').filter({ hasText: 'Job queued.' });
  const jobId = (await result.locator('a.cmd-id').innerText()).trim();
  runWorkerOnce();
  await page.goto(`${baseUrl()}/#/research-jobs/${jobId}`);
  const state = main().locator('section', { has: page.getByRole('heading', { name: 'State' }) });
  await expect(state.locator('.pill-text').first()).toHaveText('FAILED');
  await expect(state).toContainText('denied_by_policy');
  await expect(state).toContainText('no provider is enabled in the owner policy');
  await expect(state).toContainText('paid model calls are off');
  await expect(state).toContainText('not model text');
  await expect(main().getByText('No proposal: the job ended failed.')).toBeVisible();
  await expect(main().locator('.pill-text', { hasText: 'hash chain verified' })).toBeVisible();
  const audit = main().getByRole('table', { name: 'Audit trail records for this job' });
  await expect(audit.getByRole('row').filter({ hasText: 'job_started' })).toBeVisible();
  await expect(audit.getByRole('row').filter({ hasText: 'job_finished' })).toContainText('FAILED');
  const spend = main().locator('section', { has: page.getByRole('heading', { name: 'Caps and spend' }) });
  await expect(spend).toContainText(`${E2E_PER_JOB_CAP} USD`);
  await expect(spend).toContainText('0.00 USD (0.00% of the cap)');
  run.failedJobId = jobId;

  await page.goto(`${baseUrl()}/#/research-jobs`);
  const list = main().getByRole('table', { name: /Research jobs, newest first/ });
  await expect(list.getByRole('row').filter({ hasText: 'E2E question two' })).toContainText('FAILED');
  await expect(list.getByRole('row').filter({ hasText: 'E2E question one' })).toContainText('CANCELLED');
  await main().getByRole('button', { name: 'Failed', exact: true }).click();
  await expect(list.getByRole('row')).toHaveCount(2);
});

// ------------------------------------------------------------------ accessibility and layout

function routes(): string[] {
  const r = [
    '#/portfolio',
    '#/portfolio/performance',
    '#/portfolio/risk',
    '#/portfolio/stress',
    '#/portfolio/whatif',
    '#/portfolio/allocation',
    '#/portfolio/snapshots',
    '#/marketdata',
    '#/marketdata/FIX-MD-NOSUB',
    '#/research-jobs',
    '#/research-jobs/~new',
  ];
  if (run.failedJobId) r.push(`#/research-jobs/${run.failedJobId}`);
  return r;
}

for (const scheme of ['dark', 'light'] as const) {
  test(`portfolio, market data and research jobs pass the accessibility checks (${scheme} theme)`, async ({ browser }) => {
    test.setTimeout(180_000);
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
    const p = await context.newPage();
    const probs = watchProblems(p);
    await login(p);
    const found: Violation[] = [];
    let minRatio = Infinity;
    for (const r of routes()) {
      await p.goto(`${baseUrl()}/${r}`);
      await settle(p);
      const a = await auditWithStats(p, r);
      found.push(...a.violations);
      // The checks must have had something to check on every view (a silent check cannot pass vacuously).
      expect.soft(a.stats.textChecked, `text elements checked on ${r}`).toBeGreaterThan(20);
      expect.soft(a.stats.targetsChecked, `targets checked on ${r}`).toBeGreaterThan(10);
      minRatio = Math.min(minRatio, a.stats.minTextRatio);
    }
    console.log(`[a11y ${scheme}] ${routes().length} views, lowest text contrast ${minRatio.toFixed(2)}:1`);
    // A run stress result and an open lineage block are part of what the owner reads.
    await p.goto(`${baseUrl()}/#/portfolio/stress`);
    await settle(p);
    await p.getByRole('button', { name: 'Run scenario' }).click();
    await expect(p.getByText('HYPOTHETICAL', { exact: true })).toBeVisible({ timeout: 30_000 });
    await p.locator('details.pf-lineage > summary').first().click();
    found.push(...(await audit(p, 'stress result')));
    expect(found, found.map((v) => `[${v.where}] ${v.rule} ${v.target}: ${v.detail}`).join('\n')).toEqual([]);
    expect(probs.filter((x) => !/Failed to load resource/.test(x))).toEqual([]);
    await context.close();
  });
}

test('narrow screen: no sideways page scroll and STOP stays visible on these workspaces', async ({ browser }) => {
  test.setTimeout(120_000);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: 'dark' });
  const p = await context.newPage();
  await login(p);
  for (const r of routes()) {
    await p.goto(`${baseUrl()}/${r}`);
    await settle(p);
    const { sw, cw } = await p.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
    expect.soft(sw, `${r} scrolls sideways (${sw} > ${cw})`).toBeLessThanOrEqual(cw);
    await expect.soft(header(p).getByRole('button', { name: 'STOP', exact: true }), `STOP visible on ${r}`).toBeInViewport();
  }
  await context.close();
});

test('no page errors; console errors only for the expected refusals', async () => {
  // 409 (job refused without an owner policy) is an expected response the page shows to the owner.
  expect(problems.filter((p) => !/Failed to load resource: the server responded with a status of (401|409|428|503)/.test(p))).toEqual([]);
});
