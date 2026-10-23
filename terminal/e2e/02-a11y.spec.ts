/**
 * Accessibility and layout pass over every workspace, its sub-views and the shared overlays, in the
 * dark and light themes, after the flow spec has created sessions, orders and positions to render.
 * Checks are in support/a11y.ts. Also: no page-level sideways scroll and STOP visible at 390 px.
 */
import { expect, test, type Page } from '@playwright/test';
import { audit, auditFocus, auditWithStats, headerOverlaps, type AuditStats, type Violation } from './support/a11y';
import { baseUrl, drawer, header, login, watchProblems } from './support/app';

interface Ids {
  session: string | null;
  order: string | null;
  policy: string | null;
  capability: string | null;
}

async function ids(page: Page): Promise<Ids> {
  const get = async <T>(path: string): Promise<T> => (await (await page.request.get(`${baseUrl()}${path}`)).json()) as T;
  const sessions = await get<{ session_id: string }[]>('/api/sessions');
  const orders = await get<{ order_id: string }[]>('/api/orders');
  const policies = await get<{ policy_hash: string }[] | { policies: { policy_hash: string }[] }>('/api/policies');
  const caps = await get<{ capabilities?: { id?: string; capability_id?: string }[] }>('/api/capabilities');
  const policyList = Array.isArray(policies) ? policies : policies.policies;
  const cap = caps.capabilities?.[0];
  return {
    session: sessions[0]?.session_id ?? null,
    order: orders[0]?.order_id ?? null,
    policy: policyList[0]?.policy_hash ?? null,
    capability: cap ? (cap.id ?? cap.capability_id ?? null) : null,
  };
}

function routes(i: Ids): string[] {
  const r = [
    '#/overview',
    '#/sessions',
    '#/orders',
    '#/orders/~new/FIX-EQ-1',
    '#/orders/~executions',
    '#/risk',
    '#/instruments',
    '#/instruments/FIX-EQ-1',
    '#/operations/status',
    '#/operations/exceptions',
    '#/operations/reconciliation',
    '#/operations/fees',
    '#/audit/events',
    '#/audit/journal',
    '#/audit/verify',
    '#/policy',
    '#/policy/draft',
    '#/capabilities',
    '#/workstation',
  ];
  if (i.session) r.push(`#/sessions/${i.session}`);
  if (i.order) r.push(`#/orders/${i.order}`);
  if (i.policy) r.push(`#/policy/${i.policy}`, `#/policy/compare/${i.policy}/${i.policy}`);
  if (i.capability) r.push(`#/capabilities/${encodeURIComponent(i.capability)}`);
  return r;
}

/** The workspace title is shown and nothing on the page still says it is loading. */
async function settle(page: Page): Promise<void> {
  await expect(page.locator('h1#ws-title')).toBeVisible();
  await page.waitForTimeout(400);
  await expect(page.getByText(/^(Loading|Running preflight)\b/)).toHaveCount(0, { timeout: 15_000 });
}

function report(all: Violation[]): string {
  const byRule = new Map<string, Violation[]>();
  for (const v of all) byRule.set(v.rule, [...(byRule.get(v.rule) ?? []), v]);
  return [...byRule.entries()].map(([rule, vs]) => `${rule} (${vs.length}):\n${vs.map((v) => `  [${v.where}] ${v.target}: ${v.detail}`).join('\n')}`).join('\n');
}

for (const scheme of ['dark', 'light'] as const) {
  test(`every workspace passes the accessibility checks (${scheme} theme)`, async ({ browser }) => {
    test.setTimeout(240_000);
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
    // Dark is the terminal's default whatever the system says; the light pass selects the light theme explicitly.
    if (scheme === 'light') await context.addInitScript(() => window.localStorage.setItem('unknown.terminal.theme', 'light'));
    const page = await context.newPage();
    const problems = watchProblems(page);
    const found: Violation[] = [];

    await page.goto(`${baseUrl()}/`);
    await expect(page.getByLabel('Passphrase')).toBeFocused();
    found.push(...(await audit(page, 'login', { appPage: false })));
    await login(page);
    const i = await ids(page);

    const stats: [string, AuditStats][] = [];
    for (const r of routes(i)) {
      await page.goto(`${baseUrl()}/${r}`);
      await settle(page);
      const a = await auditWithStats(page, r);
      found.push(...a.violations);
      stats.push([r, a.stats]);
      // Optional review screenshots: E2E_SCREENSHOTS=<dir> npm run test:e2e
      const shots = process.env.E2E_SCREENSHOTS;
      if (shots) await page.screenshot({ path: `${shots}/${scheme}-${r.replace(/[^a-z0-9~-]+/gi, '_').replace(/^_+/, '')}.png`, fullPage: true });
    }
    // The checks must have had something to check on every view.
    for (const [r, st] of stats) {
      expect.soft(st.textChecked, `text elements checked on ${r}`).toBeGreaterThan(20);
      expect.soft(st.targetsChecked, `targets checked on ${r}`).toBeGreaterThan(10);
    }
    const totals = stats.reduce(
      (acc, [, st]) => ({
        text: acc.text + st.textChecked,
        fields: acc.fields + st.fieldsChecked,
        tables: acc.tables + st.tablesChecked,
        targets: acc.targets + st.targetsChecked,
        min: Math.min(acc.min, st.minTextRatio),
      }),
      { text: 0, fields: 0, tables: 0, targets: 0, min: Infinity },
    );
    const lowest = stats.reduce((a, b) => (b[1].minTextRatio < a[1].minTextRatio ? b : a));
    console.log(
      `[a11y ${scheme}] checked ${totals.text} text elements (lowest contrast ${totals.min.toFixed(2)}:1 on ${lowest[0]}), ` +
        `${totals.fields} form fields, ${totals.tables} tables, ${totals.targets} pointer targets`,
    );
    // Keyboard focus indicators on a dense page and on the ticket form.
    for (const r of ['#/overview', '#/orders/~new/FIX-EQ-1']) {
      await page.goto(`${baseUrl()}/${r}`);
      await settle(page);
      found.push(...(await auditFocus(page, `${r} (Tab order)`)));
    }

    // Overlays: palette, shortcuts help, control drawer, a form dialog.
    await page.goto(`${baseUrl()}/#/overview`);
    await settle(page);
    await page.keyboard.press('ControlOrMeta+k');
    await expect(page.getByRole('combobox')).toBeFocused();
    found.push(...(await audit(page, 'palette')));
    await page.keyboard.press('Escape');
    await page.locator('#main').focus();
    await page.keyboard.press('Shift+?');
    await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeVisible();
    found.push(...(await audit(page, 'shortcuts help')));
    await page.keyboard.press('Escape');
    await header(page).getByRole('button', { name: 'START', exact: true }).click();
    await expect(drawer(page)).toBeVisible();
    await expect(drawer(page).getByText(/^(Loading|Running preflight)\b/)).toHaveCount(0);
    found.push(...(await audit(page, 'control drawer')));
    await page.keyboard.press('Escape');
    await page.goto(`${baseUrl()}/#/sessions`);
    await settle(page);
    await page.getByRole('button', { name: 'New session request' }).click();
    await expect(page.getByRole('dialog', { name: 'New session request' })).toBeVisible();
    found.push(...(await audit(page, 'new session dialog')));
    await page.keyboard.press('Escape');

    console.log(`[a11y ${scheme}] ${found.length} violation(s) over ${routes(i).length + 6} views`);
    if (found.length) console.log(report(found));
    expect(found, report(found)).toEqual([]);
    expect(problems.filter((p) => !/Failed to load resource/.test(p))).toEqual([]);
    await context.close();
  });
}

test('narrow screens: no sideways page scroll, no overlapping header items, STOP stays visible', async ({ browser }) => {
  test.setTimeout(180_000);
  for (const width of [390, 1100, 1280, 1440, 1600]) {
    const context = await browser.newContext({ viewport: { width, height: 844 }, colorScheme: 'dark' });
    const page = await context.newPage();
    await login(page);
    const i = await ids(page);
    for (const r of routes(i)) {
      await page.goto(`${baseUrl()}/${r}`);
      await settle(page);
      const { sw, cw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
      expect.soft(sw, `${r} at ${width}px scrolls sideways (${sw} > ${cw})`).toBeLessThanOrEqual(cw);
      await expect.soft(header(page).getByRole('button', { name: 'STOP', exact: true }), `STOP visible on ${r} at ${width}px`).toBeInViewport();
      await expect.soft(header(page).locator('.mode-badge'), `mode badge visible on ${r} at ${width}px`).toBeInViewport();
    }
    expect.soft(await headerOverlaps(page), `overlapping header items at ${width}px`).toEqual([]);
    await context.close();
  }
});
