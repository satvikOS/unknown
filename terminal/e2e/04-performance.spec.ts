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
 * 4. Research data is not refetched per core seq: while the Company tearsheet is open, the core writes
 *    events and the core-driven overview refetches, but the tearsheet does not; Refresh fetches it.
 * 5. Nanosecond timestamps are shown from the API's exact *_ns_str: the audit event detail and its payload JSON,
 *    and a typed record's time (an order's accepted time: its millisecond and the nanoseconds in its tooltip).
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

const CIK = 9000001; // seeded by 03-research (SYNTHETIC SEC fixtures)
const AS_OF = '2026-10-22T12:00:00Z';

/** Make the core write an event, as the owner CLI or another terminal would: a STOP (StopReasserted once latched). */
async function coreWritesAnEvent(page: Page): Promise<void> {
  const session = await page.request.get(`${baseUrl()}/api/auth/session`);
  const { csrf_token } = (await session.json()) as { csrf_token: string };
  const id = `e2e.stop.${Date.now().toString(16)}`;
  const r = await page.request.post(`${baseUrl()}/api/commands`, {
    headers: { 'X-CSRF-Token': csrf_token },
    data: { kind: 'stop', command_id: id, payload: { account_key: 'SIM-1', reason: 'E2E_CORE_EVENT' } },
  });
  expect(r.status()).toBe(200);
  expect(((await r.json()) as { events?: number[] }).events?.length).toBeGreaterThan(0);
}

test('the Company tearsheet is not refetched when the core writes events; Refresh fetches it', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  const gets: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'GET') gets.push(new URL(r.url()).pathname);
  });
  const count = (path: string) => gets.filter((p) => p === path).length;
  const tearsheet = `/api/company/${CIK}`;
  await page.goto(`${baseUrl()}/#/company/${CIK}/tearsheet/as_of=${AS_OF}`);
  await expect(page.locator('#main').getByText(/Block status words:/)).toBeVisible({ timeout: 20_000 });
  expect(count(tearsheet)).toBe(1);
  // Research data reflects no event-log seq, so the header does not label it with one.
  await expect(page.locator('.ws-head .provenance')).not.toContainText('as of seq');

  for (let i = 0; i < 2; i++) {
    const overview = count('/api/overview');
    await coreWritesAnEvent(page);
    // The stream reports the new seq: the header's core-driven overview refetches...
    await expect.poll(() => count('/api/overview'), { timeout: 15_000 }).toBeGreaterThan(overview);
  }
  // ...and the tearsheet (about 640 kB for a real issuer) does not.
  await page.waitForTimeout(1_000);
  expect(count(tearsheet)).toBe(1);

  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect.poll(() => count(tearsheet)).toBe(2);
  await expect(page.locator('#main').getByText(/Block status words:/)).toBeVisible();
  expect(problems).toEqual([]);
});

/**
 * The first `*_ns` key in a recorded payload that is beyond 2^53 and carries the API's exact `*_ns_str`. (This
 * Mac's clock has microsecond resolution, so these values end in 000 and a float may print the same digits; the
 * unit tests cover values a float does round. Here the check is that the page prints the API's exact string.)
 */
function nsField(v: unknown): { key: string; exact: string } | null {
  if (Array.isArray(v)) {
    for (const x of v) {
      const hit = nsField(x);
      if (hit) return hit;
    }
    return null;
  }
  if (v === null || typeof v !== 'object') return null;
  const r = v as Record<string, unknown>;
  for (const [k, x] of Object.entries(r)) {
    const key = k.slice(0, -'_str'.length);
    if (k.endsWith('_ns_str') && typeof x === 'string' && /^\d+$/.test(x) && typeof r[key] === 'number' && BigInt(x) > 2n ** 53n) return { key, exact: x };
  }
  for (const x of Object.values(r)) {
    const hit = nsField(x);
    if (hit) return hit;
  }
  return null;
}

/** "YYYY-MM-DD HH:MM:SS.nnnnnnnnn UTC" from an exact ns string, computed here independently of the terminal. */
function utcNs(ns: string): string {
  const v = BigInt(ns);
  const iso = new Date(Number(v / 1_000_000_000n) * 1000).toISOString(); // 2026-10-22T13:30:49.000Z
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)}.${(v % 1_000_000_000n).toString().padStart(9, '0')} UTC`;
}

test('the audit event log shows times and recorded payload timestamps to the nanosecond, from the API strings', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  type Ev = { seq: number; recorded_time_ns: number; recorded_time_ns_str: string; receipt_time_ns_str: string; info: unknown };
  const events = (await (await page.request.get(`${baseUrl()}/api/events?after=0&limit=2000`)).json()) as Ev[];
  // An event whose payload holds a nanosecond timestamp (a StartGranted's broker_server_time_ns, for one).
  const target = events.find((e) => nsField(e.info) !== null);
  expect(target, 'an event with a nanosecond timestamp in its payload').toBeTruthy();
  const hit = nsField(target!.info)!;
  await page.goto(`${baseUrl()}/#/audit/events/after=${target!.seq - 1}`);
  await page.getByRole('button', { name: `Expand event ${target!.seq}`, exact: true }).click();
  const detail = page.locator('dl', { has: page.locator('dt', { hasText: 'Writer epoch' }) });
  const dd = (label: string) => detail.locator('dt', { hasText: new RegExp(`^${label}$`) }).locator('xpath=following-sibling::dd[1]');
  await expect(dd('Recorded')).toHaveText(utcNs(target!.recorded_time_ns_str));
  await expect(dd('Received')).toHaveText(utcNs(target!.receipt_time_ns_str));
  const payload = page.locator('pre.ops-json').first();
  await expect(payload).toContainText(`"${hit.key}": ${hit.exact}`);
  await expect(payload).not.toContainText('_ns_str');
  expect(problems).toEqual([]);
});

test('a typed record\'s time comes from the API string: an order\'s accepted time and its tooltip', async ({ page }) => {
  const problems = watchProblems(page);
  await login(page);
  type O = { order_id: string; accepted_at_ns: number; accepted_at_ns_str?: string };
  const orders = (await (await page.request.get(`${baseUrl()}/api/orders?limit=50`)).json()) as O[];
  const order = orders.find((o) => typeof o.accepted_at_ns_str === 'string' && BigInt(o.accepted_at_ns_str) > 2n ** 53n);
  expect(order, 'an order from 01-flow, with its exact accepted_at_ns_str').toBeTruthy();
  const exact = order!.accepted_at_ns_str!;
  await page.goto(`${baseUrl()}/#/orders/${order!.order_id}`);
  const at = page.locator('#main dt', { hasText: /^Accepted$/ }).locator('xpath=following-sibling::dd[1]').locator('time');
  // The text is UTC to the millisecond, the tooltip local time with all nine fraction digits.
  await expect(at).toHaveText(utcNs(exact).replace(/(\.\d{3})\d{6} UTC$/, '$1 UTC'));
  await expect(at).toHaveAttribute('title', new RegExp(`\\.${exact.slice(-9)}$`));
  expect(problems).toEqual([]);
});
