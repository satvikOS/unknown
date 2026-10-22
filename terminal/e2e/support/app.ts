/** Page helpers shared by the end-to-end scenarios. */
import { expect, type Locator, type Page } from '@playwright/test';
import { PASSPHRASE, STEP_UP_WINDOW_S, readState } from './backend';

export function baseUrl(): string {
  return process.env.E2E_BASE_URL ?? readState().baseUrl;
}

/** Console errors and uncaught page errors, collected per page; the scenarios assert none occurred. */
export function watchProblems(page: Page): string[] {
  const problems: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error') problems.push(`console.error: ${m.text()}`);
  });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
  return problems;
}

export function header(page: Page): Locator {
  return page.getByRole('banner', { name: 'Terminal status and controls' });
}

/** The entry-state pill in the header (STOPPED, ENTRIES PERMITTED, ...): text, not colour. */
export async function expectEntryState(page: Page, label: string, timeout = 20_000): Promise<void> {
  await expect(header(page).locator('.pill-text').getByText(label, { exact: true })).toBeVisible({ timeout });
}

export async function login(page: Page): Promise<void> {
  await page.goto(`${baseUrl()}/`);
  await page.getByLabel('Passphrase').fill(PASSPHRASE);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(header(page).locator('.mode-badge')).toHaveText(/SIMULATED/);
  // The stream pill is hidden on narrow screens; it must still be connected.
  await expect(header(page).getByText('STREAMING', { exact: true })).toBeAttached();
}

/** Press "g <key>" with focus on the workspace (not in a field). */
export async function goKeys(page: Page, key: string): Promise<void> {
  await page.locator('#main').focus();
  await page.keyboard.press('g');
  await page.keyboard.press(key);
}

export async function dismissNotices(page: Page): Promise<void> {
  const buttons = page.locator('.notices .notice button');
  for (let n = await buttons.count(); n > 0; n--) await buttons.first().click();
}

/** Seconds since the server recorded this browser session's last passphrase entry. */
export async function authAgeS(page: Page): Promise<number> {
  const r = await page.request.get(`${baseUrl()}/api/auth/session`);
  const s = (await r.json()) as { authenticated_at_ns: number | null; now_ns: number };
  if (s.authenticated_at_ns === null) throw new Error('not authenticated');
  // Whole-second precision is enough here; these numbers exceed 2^53 and are never used for money.
  return Math.floor(s.now_ns / 1e9) - Math.floor(s.authenticated_at_ns / 1e9);
}

/** Wait until the API's (shortened) step-up window has passed, so the next start answers 428. */
export async function waitForStepUpExpiry(page: Page): Promise<void> {
  await expect.poll(() => authAgeS(page), { timeout: (STEP_UP_WINDOW_S + 20) * 1000, intervals: [1000] }).toBeGreaterThan(STEP_UP_WINDOW_S + 1);
}

export function drawer(page: Page): Locator {
  return page.getByRole('dialog', { name: 'Control' });
}

export function stepUpDialog(page: Page): Locator {
  return page.getByRole('dialog', { name: 'Confirm with your passphrase' });
}

/** Opens the START drawer, waits for a clear preflight and requests start; answers a step-up prompt if one appears. */
export async function requestStart(page: Page, opts: { preflightTimeout?: number } = {}): Promise<{ commandId: string; steppedUp: boolean }> {
  await header(page).getByRole('button', { name: 'START', exact: true }).click();
  const d = drawer(page);
  await expect(d).toBeVisible();
  await expect(d.getByText('Preflight clear: no blockers')).toBeVisible({ timeout: opts.preflightTimeout ?? 20_000 });
  await d.getByRole('button', { name: /REQUEST START/ }).click();
  const prompt = stepUpDialog(page);
  const startSection = d.locator('section', { has: page.getByRole('heading', { name: 'Preflight and start' }) });
  const status = startSection.locator('.callout').filter({ hasText: 'START command' });
  await expect(prompt.or(status)).toBeVisible();
  let steppedUp = false;
  if (await prompt.isVisible()) {
    steppedUp = true;
    await prompt.getByLabel('Passphrase').fill(PASSPHRASE);
    await prompt.getByRole('button', { name: 'Confirm' }).click();
    await expect(prompt).toBeHidden();
  }
  await expect(status.getByText(/^applied/)).toBeVisible({ timeout: 20_000 });
  const commandId = (await status.locator('.cmd-id').innerText()).trim();
  return { commandId, steppedUp };
}

/** Adds two non-negative decimal strings exactly (test-side price arithmetic; never floats). */
export function addDecimal(a: string, b: string): string {
  const scale = Math.max(a.split('.')[1]?.length ?? 0, b.split('.')[1]?.length ?? 0);
  const toInt = (s: string) => {
    const [i = '0', f = ''] = s.split('.');
    return BigInt(i + f.padEnd(scale, '0'));
  };
  const sum = (toInt(a) + toInt(b)).toString().padStart(scale + 1, '0');
  return scale ? `${sum.slice(0, -scale)}.${sum.slice(-scale)}` : sum;
}
