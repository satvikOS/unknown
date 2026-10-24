/**
 * Workstation shell in the real browser: the command line, linked panels, layout persistence and recovery.
 * Functions whose workspaces this build does not install must say so instead of opening nothing.
 */
import { expect, test } from '@playwright/test';
import { login, watchProblems } from './support/app';

test('command line, linked panels, saved layouts, lock and recovery from a corrupt layout', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  const cmd = page.getByRole('combobox', { name: /^Command/ });
  const msg = page.locator('.cmdbar-msg');

  // Unknown and uninstalled functions are reported plainly; a known installed one opens on its own.
  await cmd.fill('XYZZY');
  await cmd.press('Enter');
  await expect(msg).toHaveText(/^Unknown function XYZZY/);
  await cmd.fill('RISK');
  await cmd.press('Enter');
  await expect(page).toHaveURL(/#\/risk$/);
  await expect(page.locator('h1#ws-title')).toHaveText('Risk');

  // F2 focuses the command line; Tab accepts the autocomplete suggestion.
  await page.locator('h1#ws-title').focus();
  await page.keyboard.press('F2');
  await expect(cmd).toBeFocused();
  await cmd.fill('ws');
  await expect(page.getByRole('option', { name: /^WS/ })).toBeVisible();
  await cmd.press('Tab');
  await expect(cmd).toHaveValue('WS ');
  await cmd.press('Enter');
  await expect(page).toHaveURL(/#\/workstation$/);

  // Trader template (monitor and chart are installed): three panels on channel 1, each workspace an h2 in its panel.
  const panels = page.locator('section.ws-panel');
  await expect(panels).toHaveCount(3);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Workstation');
  await expect(page.getByRole('heading', { level: 2, name: 'Monitor' })).toBeVisible();

  // A security typed on its own lands on the active panel's channel; linked panels follow, an unlinked one does not.
  await page.getByLabel('Panel 3 link channel').selectOption('none');
  await panels.first().click({ position: { x: 5, y: 40 } });
  await cmd.fill('SAP.XETR');
  await cmd.press('Enter');
  await expect(msg).toHaveText('SAP.XETR loaded on panel 1.');
  await expect(page.locator('.ws-panel-sec', { hasText: 'SAP.XETR' })).toHaveCount(2);
  await expect(panels.nth(2).locator('.ws-panel-sec')).toHaveText('no security');

  // Split, save as a named layout, lock; the lock refuses changing what a panel shows from the command line.
  await page.getByRole('button', { name: 'Split panel 1 side by side' }).click();
  await expect(panels).toHaveCount(4);
  await page.getByLabel('Layout name').fill('E2E desk');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Saved "E2E desk" as version 1.')).toBeVisible();
  await page.getByRole('button', { name: 'Lock layout' }).click();
  await expect(page.getByRole('button', { name: 'Split panel 1 side by side' })).toBeDisabled();
  await cmd.fill('AUDIT');
  await cmd.press('Enter');
  await expect(msg).toHaveText(/^The layout is locked/);

  // The draft survives a reload with its lock; a corrupt store opens a template and says why.
  await page.reload();
  await expect(panels).toHaveCount(4);
  await expect(page.getByRole('button', { name: 'Locked' })).toHaveAttribute('aria-pressed', 'true');
  await page.evaluate(() => window.localStorage.setItem('unknown.terminal.workstation.v1', '{"schema":1,"draft":'));
  await page.reload();
  await expect(page.getByRole('status').filter({ hasText: 'could not be read' })).toBeVisible();
  await expect(panels).toHaveCount(3);
  expect(await page.evaluate(() => window.localStorage.getItem('unknown.terminal.workstation.corrupt'))).toBe('{"schema":1,"draft":');

  // Status bar: the market-data client is registered and names the stream state and each source with its data type
  // (the suite's TEST feed, REALTIME). Only real data types exist: nothing is labelled SYNTHETIC or FIXTURE.
  const bar = page.getByRole('contentinfo', { name: 'Status bar' });
  await expect(bar).toContainText(/MD (STREAMING|CONNECTING|STALE)/);
  await expect(bar.locator('.src-badge', { hasText: 'TEST · REALTIME' })).toBeVisible();
  await expect(bar).not.toContainText(/SYNTHETIC|FIXTURE/);
  expect(problems).toEqual([]);
});
