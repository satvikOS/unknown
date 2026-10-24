/**
 * The owner's main path through the terminal, against the real core and API started by global setup.
 * The scenarios share one browser session and one account, in order: each builds on the last.
 */
import { expect, test, type Page, type Response } from '@playwright/test';
import {
  addDecimal,
  baseUrl,
  dismissNotices,
  drawer,
  expectEntryState,
  goKeys,
  header,
  login,
  requestStart,
  stepUpDialog,
  waitForStepUpExpiry,
  watchProblems,
} from './support/app';
import { PASSPHRASE, restartCore } from './support/backend';

test.describe.configure({ mode: 'serial' });

let page: Page;
let problems: string[];
const run = { sessionId: '', orderId: '', stopCommandId: '', reconciliationBeforeRestart: '' };

interface Preflight {
  blockers: string[];
  evidence: { epoch?: number; reconciliation?: string | null };
}

async function preflight(): Promise<Preflight> {
  const r = await page.request.get(`${baseUrl()}/api/preflight`);
  expect(r.status()).toBe(200);
  return (await r.json()) as Preflight;
}

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
  page = await context.newPage();
  problems = watchProblems(page);
});

test.afterAll(async () => {
  await page.context().close();
});

test('login rejects a wrong passphrase, then accepts the right one', async () => {
  await page.goto(`${baseUrl()}/`);
  await expect(page.getByRole('heading', { name: 'Owner sign-in' })).toBeVisible();
  await page.getByLabel('Passphrase').fill('not the owner passphrase');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('alert')).toHaveText('That passphrase was not accepted.');
  await expect(header(page)).toHaveCount(0);
  // The API backs off for 2 s after one failure (api/auth.py); wait it out rather than trip the 429.
  await page.waitForTimeout(2500);
  await login(page);
  await expect(page).toHaveURL(/#\/overview$/);
});

test('overview shows SIMULATED in text and colour, and names the simulated broker', async () => {
  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible();
  const badge = header(page).locator('.mode-badge');
  await expect(badge).toHaveText(/SIMULATED/);
  await expect(badge).toHaveAttribute('data-mode', 'SIMULATED');
  // Colour as well as text: the badge border and the band under the page top use the SIMULATED mode colour.
  const colours = await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--mode-simulated)';
    document.body.append(probe);
    const mode = getComputedStyle(probe).color;
    probe.remove();
    const badgeEl = document.querySelector('.topbar .mode-badge');
    const bandEl = document.querySelector('.mode-band');
    return {
      mode,
      badge: badgeEl ? getComputedStyle(badgeEl).borderTopColor : '',
      band: bandEl ? getComputedStyle(bandEl).backgroundColor : '',
    };
  });
  expect(colours.badge).toBe(colours.mode);
  expect(colours.band).toBe(colours.mode);
  await expect(page.locator('.mode-band')).toHaveAttribute('data-mode', 'SIMULATED');
  // The simulated broker is named, with the real-data rule: it prices only from a configured quote source (TEST here).
  await expect(header(page).locator('.badge', { hasText: 'SIM BROKER' })).toBeVisible();
  await expect(header(page).locator('.badge', { hasText: 'FIXTURE' })).toHaveCount(0);
  // The bootstrap leaves the account stopped until the owner presses START.
  await expectEntryState(page, 'STOPPED');
  await expect(page.getByRole('table', { name: 'Cash by owner bucket' })).toContainText(/300\.00\s*USD/);
});

test('green START asks for the passphrase again (HTTP 428) and retries with the same command id', async () => {
  const starts: { status: number; commandId: string }[] = [];
  const onResponse = async (r: Response) => {
    if (!r.url().endsWith('/api/commands') || r.request().method() !== 'POST') return;
    const body = r.request().postDataJSON() as { kind?: string; command_id?: string };
    if (body.kind === 'start') starts.push({ status: r.status(), commandId: body.command_id ?? '' });
  };
  page.on('response', onResponse);
  await waitForStepUpExpiry(page);

  await header(page).getByRole('button', { name: 'START', exact: true }).click();
  const d = drawer(page);
  await expect(d.getByRole('heading', { name: 'Preflight and start' })).toBeFocused();
  await expect(d.getByText('Preflight clear: no blockers')).toBeVisible();
  await d.getByRole('button', { name: /REQUEST START/ }).click();

  const prompt = stepUpDialog(page);
  await expect(prompt).toBeVisible();
  await expect(prompt.getByLabel('Passphrase')).toBeFocused();
  const promptedId = (await prompt.locator('.cmd-id').innerText()).trim();
  expect(promptedId).toMatch(/^ui\.start\.[0-9a-f]+$/);
  await prompt.getByLabel('Passphrase').fill('still not the passphrase');
  await prompt.getByRole('button', { name: 'Confirm' }).click();
  await expect(prompt.getByRole('alert')).toHaveText('That passphrase was not accepted.');
  await page.waitForTimeout(2500); // reauth failures share the login backoff
  await prompt.getByLabel('Passphrase').fill(PASSPHRASE);
  await prompt.getByRole('button', { name: 'Confirm' }).click();
  await expect(prompt).toBeHidden();

  const status = d.locator('.callout').filter({ hasText: 'START command' });
  await expect(status.getByText(/^applied \(state seq \d+ observed\)$/)).toBeVisible();
  await expect(status.locator('.cmd-id')).toHaveText(promptedId);
  await expectEntryState(page, 'ENTRIES PERMITTED');
  page.off('response', onResponse);
  expect(starts.map((s) => s.status)).toEqual([428, 200]);
  expect(new Set(starts.map((s) => s.commandId))).toEqual(new Set([promptedId]));
  await page.keyboard.press('Escape');
  await expect(d).toBeHidden();
});

test('create a session request: with entries permitted it is funded and starts', async () => {
  await goKeys(page, 's');
  await expect(page.getByRole('heading', { name: 'Sessions', level: 1 })).toBeVisible();
  await page.getByRole('button', { name: 'New session request' }).click();
  const dlg = page.getByRole('dialog', { name: 'New session request' });
  await dlg.getByLabel(/^Seed/).fill('100.00');
  await dlg.getByLabel(/^Target terminal equity/).fill('105.00');
  await expect(dlg).toContainText('5.00%');
  await dlg.getByRole('button', { name: 'Record request' }).click();
  const title = page.getByRole('heading', { name: /^Session ses-/, level: 1 });
  await expect(title).toBeVisible();
  run.sessionId = (await title.innerText()).replace(/^Session /, '').trim();
  const lifecycle = page.locator('section[aria-labelledby="ses-life-h"]');
  await expect(lifecycle.locator('header').getByText('RUNNING', { exact: true })).toBeVisible();
});

test('a BUY at the TEST ask + 0.02 is accepted and shows FILLED in the blotter', async () => {
  await goKeys(page, 'b');
  await expect(page.getByRole('heading', { name: 'Orders', level: 1 })).toBeVisible();
  await page.locator('h1#ws-title').focus();
  await page.keyboard.press('n');
  await expect(page.getByRole('heading', { name: 'Order ticket' })).toBeVisible();
  await page.getByLabel('Session', { exact: true }).selectOption(run.sessionId);
  await page.getByLabel('Instrument').selectOption('FIX-EQ-1');
  const quote = page.locator('section[aria-labelledby="tkt-quote-h"]');
  // The quote names its source (the suite's TEST feed) and is shown as data, never as FIXTURE or SYNTHETIC.
  await expect(quote).toContainText(/source\s*TEST/);
  await expect(quote).not.toContainText(/FIXTURE|SYNTHETIC/);
  await page.getByRole('radio', { name: 'BUY' }).check();
  await page.getByLabel(/^Quantity/).fill('1');
  await page.getByLabel(/^Deadline/).fill('300');
  // Copy the live ask, then add 0.02 (exact decimal arithmetic) so the limit crosses the TEST ask.
  await dismissNotices(page);
  await page.getByRole('button', { name: /^ask / }).click();
  const ask = await page.getByLabel(/^Limit price/).inputValue();
  expect(ask).toMatch(/^\d+\.\d{2}$/);
  await page.getByLabel(/^Limit price/).fill(addDecimal(ask, '0.02'));
  await page.getByRole('button', { name: 'Review order' }).click();
  await expect(page.getByRole('heading', { name: 'Review the intent' })).toBeVisible();
  await page.getByRole('button', { name: 'Send intent to the core' }).click();
  await expect(page.getByText(/^ACCEPTED: APPROVED$/)).toBeVisible();
  run.orderId = (await page.locator('.ord-result-body a.cmd-id').first().innerText()).trim();
  expect(run.orderId).toMatch(/^own-[0-9a-f]{20}$/);

  await page.locator('h1#ws-title').focus();
  await page.keyboard.press('l');
  await page.getByRole('radio', { name: 'All' }).check();
  const blotter = page.getByRole('table', { name: /^All orders/ });
  const row = blotter.locator('tbody tr', { hasText: run.orderId });
  // The simulated broker fills a BUY once its limit is at or above the TEST ask; the quote can move away
  // before the order reaches the simulated broker, so allow it time to come back.
  await expect(row.getByText('FILLED', { exact: true })).toBeVisible({ timeout: 90_000 });

  // The position appears on the overview with a mark that states its source (TEST) and age.
  await goKeys(page, 'o');
  const positions = page.getByRole('table', { name: 'Open positions by owner and instrument' });
  const pos = positions.locator('tbody tr', { hasText: 'FIX-EQ-1' });
  await expect(pos).toBeVisible();
  await expect(pos).toContainText(/bid from TEST age \d+ ms/);
  await expect(pos.locator('.nv-error')).toHaveCount(0);
});

test('red STOP shows its command id and progress, and a new BUY is rejected with STOP_LATCH', async () => {
  await header(page).getByRole('button', { name: 'STOP', exact: true }).click();
  const d = drawer(page);
  await expect(d).toBeVisible();
  const status = d.locator('.callout').filter({ hasText: 'STOP command' });
  await expect(status.locator('.cmd-id')).toHaveText(/^ui\.stop\.[0-9a-f]+$/);
  run.stopCommandId = (await status.locator('.cmd-id').innerText()).trim();
  await expect(status.getByText(/^applied/)).toBeVisible();
  await expectEntryState(page, 'STOPPED');
  const steps = d.getByRole('list', { name: 'Stop progress steps' });
  await expect(steps).toBeVisible();
  await expect(steps.getByRole('listitem')).toHaveCount(5);
  await expect(steps.getByRole('listitem').first()).toContainText(/entries blocked/i);
  await expect(d.locator('dt', { hasText: 'Latched by command' }).locator('xpath=following-sibling::dd[1]')).toHaveText(run.stopCommandId);
  await expect(d.getByRole('table', { name: 'Residual positions after stop' })).toContainText('FIX-EQ-1');
  await expect(d.getByText(/^Liquidation on stop: NONE\./)).toBeVisible();
  run.reconciliationBeforeRestart = (await preflight()).evidence.reconciliation ?? '';
  await page.keyboard.press('Escape');

  await page.goto(`${baseUrl()}/#/orders/~new/FIX-EQ-1`);
  await expect(page.getByRole('heading', { name: 'Order ticket' })).toBeVisible();
  await page.getByLabel('Session', { exact: true }).selectOption(run.sessionId);
  await page.getByRole('radio', { name: 'BUY' }).check();
  await expect(page.getByText(/Entries are STOPPED/)).toBeVisible();
  await page.getByLabel(/^Quantity/).fill('1');
  await page.getByRole('button', { name: /^ask / }).click();
  await dismissNotices(page);
  await page.getByRole('button', { name: 'Review order' }).click();
  await page.getByRole('button', { name: 'Send intent to the core' }).click();
  await expect(page.getByText(/^REJECTED: STOP_LATCH$/)).toBeVisible();
  await page.getByLabel('Failed checks only').check();
  const checks = page.getByRole('table', { name: /^Pretrade checks for the rejected intent/ });
  await expect(checks.locator('tbody tr')).toHaveCount(1);
  await expect(checks.locator('tbody tr')).toContainText('STOP_LATCH');
});

test('the stop persists through a restart of the core process', async () => {
  const before = await preflight();
  const { oldPid, newPid } = await restartCore();
  expect(newPid).not.toBe(oldPid);
  await page.reload();
  await expect(header(page).getByText('CORE OK', { exact: true })).toBeVisible();
  await expectEntryState(page, 'STOPPED');
  const after = await preflight();
  expect(after.evidence.epoch).toBe((before.evidence.epoch ?? 0) + 1);
  // The latch is the one recorded before the restart, not a new one.
  await header(page).getByRole('button', { name: 'START', exact: true }).click();
  const d = drawer(page);
  await expect(d.locator('dt', { hasText: 'Latched by command' }).locator('xpath=following-sibling::dd[1]')).toHaveText(run.stopCommandId);
  await expect(d.locator('dt', { hasText: 'Reason' }).locator('xpath=following-sibling::dd[1]')).toHaveText('OWNER_TERMINAL_STOP');
});

test('START again once the restarted core has reconciled', async () => {
  // A new process must reconcile against the broker before start is possible (CLAUDE.md invariant).
  await expect.poll(async () => (await preflight()).blockers, { timeout: 60_000 }).toEqual([]);
  const evidence = (await preflight()).evidence;
  expect(evidence.reconciliation).toBeTruthy();
  expect(evidence.reconciliation).not.toBe(run.reconciliationBeforeRestart);
  const { commandId } = await requestStart(page, { preflightTimeout: 30_000 });
  expect(commandId).toMatch(/^ui\.start\./);
  await expectEntryState(page, 'ENTRIES PERMITTED');
  await page.keyboard.press('Escape');
});

test('audit verify reports no chain or ledger problems', async () => {
  await goKeys(page, 'a');
  await expect(page.getByRole('heading', { name: 'Audit', level: 1 })).toBeVisible();
  await page.getByRole('tab', { name: /^Verify/ }).click();
  await page.getByRole('button', { name: 'Verify now' }).click();
  await expect(page.getByText('verified: no problems', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Event chain', exact: true })).toContainText('Every payload hash and chain link recomputes exactly.');
  await expect(page.getByRole('region', { name: 'Ledger', exact: true })).toContainText('Every ledger invariant holds.');
});

test('keyboard: Cmd/Ctrl+K opens the palette, which navigates to Sessions and to a session', async () => {
  await page.locator('#main').focus();
  await page.keyboard.press('ControlOrMeta+k');
  const search = page.getByRole('combobox', { name: 'Search workspaces, records and commands' });
  await expect(search).toBeFocused();
  await search.fill('Sessions');
  await expect(page.getByRole('option', { name: /^Sessions/ }).first()).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/sessions$/);
  await expect(page.getByRole('heading', { name: 'Sessions', level: 1 })).toBeVisible();
  await expect(page.locator('h1#ws-title')).toBeFocused();

  // The other modifier also opens it; a session id jumps to that record.
  await page.keyboard.press(process.platform === 'darwin' ? 'Control+k' : 'Meta+k');
  await expect(search).toBeFocused();
  await search.fill(run.sessionId.slice(0, 16));
  await expect(page.getByRole('option', { name: new RegExp(run.sessionId) })).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: `Session ${run.sessionId}`, level: 1 })).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  await expect(search).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(search).toBeHidden();

  // Workspace-registered palette commands open their views.
  await page.keyboard.press('ControlOrMeta+k');
  await search.fill('new order ticket');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/orders\/~new$/);
  await expect(page.getByRole('heading', { name: 'Order ticket' })).toBeVisible();
});

test('keyboard: single-character shortcuts are listed in the help and can be turned off (WCAG 2.1.4)', async () => {
  await page.locator('#main').focus();
  await page.keyboard.press('Shift+?');
  const help = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
  await expect(help).toBeVisible();
  await expect(help.getByRole('row', { name: /Orders: new order ticket/ })).toBeVisible();
  const toggle = help.getByRole('checkbox', { name: /Single-character shortcuts/ });
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await help.getByRole('button', { name: 'Close' }).click();
  await expect(page.getByRole('link', { name: /^New order/ }).locator('kbd')).toHaveCount(0);
  await goKeys(page, 'a');
  await expect(page).toHaveURL(/#\/orders\/~new$/); // "g a" ignored while off
  await page.keyboard.press('ControlOrMeta+k');
  const search = page.getByRole('combobox', { name: 'Search workspaces, records and commands' });
  await search.fill('turn on single-character');
  await page.keyboard.press('Enter');
  // The palette runs its action after it closes; the Orders view keys show their hints again once on.
  await expect(page.getByRole('link', { name: /^New order/ }).locator('kbd')).toBeVisible();
  await goKeys(page, 'a');
  await expect(page.getByRole('heading', { name: 'Audit', level: 1 })).toBeVisible();
});

test('no uncaught page errors or console errors other than expected HTTP statuses', async () => {
  // Chrome logs every non-2xx response; the run deliberately causes 401 (wrong passphrase), 428 (step-up)
  // and 503 (core restarting). Anything else is a defect.
  const unexpected = problems.filter((p) => !/Failed to load resource: the server responded with a status of (401|428|503)/.test(p));
  expect(unexpected).toEqual([]);
});
