/**
 * Loading and freshness against the real API. Runs last, after 03-research has seeded the research store.
 *
 * 1. Code splitting: a first visit loads the shell and the opened workspace's chunk only; another workspace's
 *    chunk loads when it is opened, and focus then moves to its title.
 */
import { expect, test, type Page } from '@playwright/test';
import { baseUrl, goKeys, login, watchProblems } from './support/app';

test.describe.configure({ mode: 'serial' });

/** Script chunks the page requested, by file name under /assets/. */
function watchScripts(page: Page): string[] {
  const scripts: string[] = [];
  page.on('request', (r) => {
    const path = new URL(r.url()).pathname;
    if (path.startsWith('/assets/') && path.endsWith('.js')) scripts.push(path.slice('/assets/'.length));
  });
  return scripts;
}

const chunksOf = (scripts: readonly string[], name: string) => scripts.filter((s) => s.startsWith(`${name}-`));

test('a first visit loads the shell and the opened workspace only; others load when opened', async ({ page }) => {
  const problems = watchProblems(page);
  const scripts = watchScripts(page);
  await login(page);
  await expect(page.locator('h1#ws-title')).toHaveText('Overview');

  const entries = scripts.filter((s) => /^index-[\w-]+\.js$/.test(s));
  expect(entries).toHaveLength(1);
  expect(chunksOf(scripts, 'OverviewWorkspace')).toHaveLength(1);
  for (const other of ['PortfolioWorkspace', 'CompanyWorkspace', 'SessionsWorkspace', 'AuditWorkspace', 'ResearchWorkspace']) {
    expect(chunksOf(scripts, other), `${other} loaded before it was opened`).toEqual([]);
  }
  // The entry chunk is the shell only: well under Vite's 500 kB chunk warning (the build enforces 400 kB).
  const entry = await page.request.get(`${baseUrl()}/assets/${entries[0]}`);
  expect((await entry.body()).length).toBeLessThan(400_000);

  await goKeys(page, 'f');
  await expect(page.locator('h1#ws-title')).toHaveText('Portfolio');
  await expect(page.locator('h1#ws-title')).toBeFocused();
  expect(chunksOf(scripts, 'PortfolioWorkspace')).toHaveLength(1);
  expect(problems).toEqual([]);
});
