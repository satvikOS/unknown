/**
 * Loading and freshness against the real API. Runs last, after 03-research has seeded the research store.
 *
 * 1. Code splitting: a first visit loads the shell and the opened workspace's chunk only; another workspace's
 *    chunk loads when it is opened, and focus then moves to its title.
 * 2. Unknown outcomes and conflicts: a STOP whose response is lost on the way back is recovered from
 *    GET /api/commands/<id> (never sent twice); a command id that already names another command is refused
 *    with 409 COMMAND_ID_CONFLICT and shown as that refusal.
 * 3. The capability register is revalidated with its ETag: a refresh is answered 304 by the API (its own
 *    request log says so) and the page keeps showing the register.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { baseUrl, drawer, goKeys, header, login, watchProblems } from './support/app';
import { readState } from './support/backend';

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

/** GET /api/commands/<id> requests the page made (the tracker's stored-result lookups), by command id. */
function watchLookups(page: Page): string[] {
  const ids: string[] = [];
  page.on('request', (r) => {
    const path = new URL(r.url()).pathname;
    if (r.method() === 'GET' && path.startsWith('/api/commands/')) ids.push(decodeURIComponent(path.slice('/api/commands/'.length)));
  });
  return ids;
}

/** The expected network noise of these scenarios: the reset connection and the 409 answer. */
const expectedNoise = (p: string) => /Failed to load resource: (net::ERR_CONNECTION_RESET|the server responded with a status of 409)/.test(p);

test('a STOP whose response is lost is recovered from its stored result, not sent twice', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  const lookups = watchLookups(page);
  const posted: string[] = [];
  await page.route(
    '**/api/commands',
    async (route) => {
      posted.push((route.request().postDataJSON() as { command_id: string }).command_id);
      const answered = await route.fetch(); // the API and the core execute the command...
      expect(answered.status()).toBe(200);
      await route.abort('connectionreset'); // ...and the browser never sees the answer
    },
    { times: 1 },
  );
  await header(page).getByRole('button', { name: 'STOP', exact: true }).click();
  const status = drawer(page).locator('.callout').filter({ hasText: 'STOP command' });
  await expect(status.getByText(/^applied/)).toBeVisible({ timeout: 20_000 });
  await expect(status.locator('.cmd-lookup')).toHaveText(
    'Outcome recovered from the stored result (GET /api/commands/<id>, answered by the core); nothing was resent.',
  );
  await expect(status.getByRole('button', { name: 'Retry with the same id' })).toHaveCount(0);
  const id = (await status.locator('.cmd-id').innerText()).trim();
  expect(posted).toEqual([id]);
  expect(lookups).toEqual([id]);
  const stored = await page.request.get(`${baseUrl()}/api/commands/${encodeURIComponent(id)}`);
  expect(await stored.json()).toMatchObject({ command_id: id, kind: 'stop', accepted: true });
  expect(problems.filter((p) => !expectedNoise(p))).toEqual([]);
});

test('a command id that already names another command is refused with 409 COMMAND_ID_CONFLICT, shown as that refusal', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  const lookups = watchLookups(page);
  const answers: number[] = [];
  page.on('response', (r) => {
    if (new URL(r.url()).pathname === '/api/commands') answers.push(r.status());
  });
  let earlier = '';
  await page.route(
    '**/api/commands',
    async (route) => {
      const body = route.request().postDataJSON() as { kind: string; command_id: string; payload: Record<string, unknown> };
      // Another command takes this id first: the same kind with a different payload.
      const first = await route.fetch({ postData: JSON.stringify({ ...body, payload: { ...body.payload, reason: 'E2E_EARLIER_COMMAND' } }) });
      expect(first.status()).toBe(200);
      earlier = body.command_id;
      await route.continue(); // the terminal's own request now reuses that id with its own payload
    },
    { times: 1 },
  );
  await header(page).getByRole('button', { name: 'STOP', exact: true }).click();
  const status = drawer(page).locator('.callout').filter({ hasText: 'STOP command' });
  await expect(status.getByText('rejected: COMMAND_ID_CONFLICT')).toBeVisible({ timeout: 20_000 });
  await expect(status).toContainText('the original command keeps its result and this request was not executed');
  await expect(status.getByRole('button', { name: /Retry|Check the stored result/ })).toHaveCount(0);
  expect((await status.locator('.cmd-id').innerText()).trim()).toBe(earlier);
  expect(answers).toEqual([409]);
  expect(lookups).toEqual([]); // a definitive refusal is not looked up
  await expect(page.locator('.notices .notice', { hasText: 'STOP: rejected' })).toBeVisible();
  const stored = await page.request.get(`${baseUrl()}/api/commands/${encodeURIComponent(earlier)}`);
  expect(await stored.json()).toMatchObject({ command_id: earlier, kind: 'stop', accepted: true });
  expect(problems.filter((p) => !expectedNoise(p))).toEqual([]);
});

/** How many times the API answered GET /api/capabilities with this status, from its own request log. */
function capabilityAnswers(status: number): number {
  const log = readFileSync(join(readState().home, 'api.log'), 'utf8');
  return log.split('\n').filter((line) => line.includes(`"GET /api/capabilities HTTP/1.1" ${status} `)).length;
}

test('the capability register revalidates with its ETag: a refresh is a 304 and the page keeps the register', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  const full = capabilityAnswers(200);
  await page.goto(`${baseUrl()}/#/capabilities`);
  const table = page.getByRole('table', { name: /^Capability register: \d+ of \d+ entries\b/ });
  await expect(table).toBeVisible();
  const caption = (await table.locator('caption').textContent()) ?? '';
  // Nothing is kept before the first load after sign-in: it is a full 200.
  await expect.poll(() => capabilityAnswers(200)).toBeGreaterThan(full);
  const fullAfterFirst = capabilityAnswers(200);
  const notModified = capabilityAnswers(304);
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect.poll(() => capabilityAnswers(304)).toBeGreaterThan(notModified);
  expect(capabilityAnswers(200)).toBe(fullAfterFirst); // the register was not sent again
  await expect(table.locator('caption')).toHaveText(caption);
  expect(problems).toEqual([]);
});
