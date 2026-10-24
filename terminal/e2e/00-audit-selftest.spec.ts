/**
 * The accessibility checker must catch what it claims to catch: each rule is shown a page with one
 * known defect and must report it, and a clean page must report nothing. Without this, a silent
 * checker would make the workspace audit pass vacuously.
 */
import { expect, test } from '@playwright/test';
import { audit, auditFocus, auditWithStats, headerOverlaps } from './support/a11y';

const CLEAN = `<!doctype html><html lang="en"><head><title>clean</title><style>
  body { background: #ffffff; color: #111111; font: 14px sans-serif; margin: 16px; }
  button, input { min-height: 28px; min-width: 28px; margin: 6px; border: 1px solid #555; background: #fff; color: #111; }
  :focus-visible { outline: 2px solid #1a4fd6; outline-offset: 2px; }
</style></head><body>
  <header>Status</header><nav aria-label="Main"><a href="#a">Area</a></nav>
  <main><h1>Title</h1>
    <label for="q">Query</label> <input id="q">
    <button type="button">Run</button>
    <table><caption>Rows</caption><thead><tr><th scope="col" aria-sort="ascending">Name</th><th scope="col">Value</th></tr></thead>
      <tbody><tr><td>a</td><td>1</td></tr></tbody></table>
  </main></body></html>`;

function variant(edit: (html: string) => string): string {
  return edit(CLEAN);
}

const CASES: { rule: string; html: string; expect: RegExp }[] = [
  { rule: 'contrast', html: variant((h) => h.replace('<h1>Title</h1>', '<h1>Title</h1><p style="color:#9a9a9a">faint text</p>')), expect: /faint text/ },
  { rule: 'name', html: variant((h) => h.replace('<button type="button">Run</button>', '<button type="button"><svg width="10" height="10" aria-hidden="true"></svg></button>')), expect: /button/ },
  { rule: 'name', html: variant((h) => h.replace('<label for="q">Query</label> ', '')), expect: /textbox/ },
  { rule: 'table', html: variant((h) => h.replace('<caption>Rows</caption>', '')), expect: /no caption/ },
  { rule: 'table', html: variant((h) => h.replace('<th scope="col">Value</th>', '<th scope="col"></th>')), expect: /empty header cell/ },
  { rule: 'table', html: variant((h) => h.replace('<td>a</td>', '<td aria-sort="ascending">a</td>')), expect: /non-header cell/ },
  { rule: 'ids', html: variant((h) => h.replace('<h1>Title</h1>', '<h1 id="q">Title</h1>')), expect: /id used 2 times/ },
  { rule: 'ids', html: variant((h) => h.replace('<button type="button">', '<button type="button" aria-describedby="nowhere">')), expect: /missing id "nowhere"/ },
  { rule: 'page', html: variant((h) => h.replace('<html lang="en">', '<html>')), expect: /no lang/ },
  { rule: 'page', html: variant((h) => h.replace('<h1>Title</h1>', '<h1>Title</h1><h1>Second</h1>')), expect: /2 visible h1/ },
  {
    rule: 'target',
    html: variant((h) => h.replace('<button type="button">Run</button>', '<button type="button" style="min-height:0;min-width:0;height:14px;width:14px;margin:0;padding:0">a</button><button type="button" style="min-height:0;min-width:0;height:14px;width:14px;margin:0;padding:0">b</button>')),
    expect: /closer than 24 px/,
  },
  { rule: 'field-edge', html: variant((h) => h.replace('<input id="q">', '<input id="q" style="border:1px solid #eeeeee;background:#fff">')), expect: /need 3:1/ },
];

test('the accessibility checker reports nothing on a clean page', async ({ page }) => {
  await page.setContent(CLEAN);
  expect(await audit(page, 'clean')).toEqual([]);
  expect(await auditFocus(page, 'clean', 6)).toEqual([]);
});

for (const c of CASES) {
  test(`the accessibility checker catches a ${c.rule} defect (${c.expect.source})`, async ({ page }) => {
    await page.setContent(c.html);
    const found = await audit(page, c.rule);
    expect(found.filter((v) => v.rule === c.rule).map((v) => `${v.target} ${v.detail}`).join('\n')).toMatch(c.expect);
  });
}

test('the focus check catches a control with its outline removed', async ({ page }) => {
  await page.setContent(CLEAN.replace(':focus-visible { outline: 2px solid #1a4fd6; outline-offset: 2px; }', '* { outline: none !important; }'));
  const found = await auditFocus(page, 'no outline', 6);
  expect(found.map((v) => v.detail).join('\n')).toMatch(/no visible focus indicator/);
});

test('the checker decides contrast on color-mix() and oklch() colours, as the terminal uses them', async ({ page }) => {
  await page.setContent(
    CLEAN.replace(
      '<h1>Title</h1>',
      '<h1>Title</h1><p style="background:color-mix(in srgb, #1a4fd6 14%, #ffffff);color:oklch(0.35 0.02 250)">mixed</p>' +
        '<p style="background:color-mix(in srgb, #000 50%, #fff);color:color-mix(in srgb, #000 55%, #fff)">too close</p>',
    ),
  );
  const { violations, stats } = await auditWithStats(page, 'modern colours');
  expect(violations.filter((v) => v.rule === 'checker')).toEqual([]);
  expect(stats.unparsedColours).toBe(0);
  expect(stats.textChecked).toBeGreaterThan(3);
  expect(violations.filter((v) => v.rule === 'contrast').map((v) => v.target).join('\n')).toMatch(/too close/);
  expect(violations.filter((v) => v.rule === 'contrast').map((v) => v.target).join('\n')).not.toMatch(/mixed/);
});

test('the header overlap check catches status items drawn over each other, and passes when they are apart', async ({ page }) => {
  const header = (groupWidth: string) => `<!doctype html><html lang="en"><body style="margin:0;font:14px sans-serif">
    <header class="topbar" style="display:flex;width:900px">
      <div class="topbar-status" style="display:flex;gap:10px;overflow:hidden;flex:1 1 auto;min-width:0">
        <div class="topbar-group" style="display:flex;gap:8px;min-width:0;width:${groupWidth}">
          <span class="pill" style="flex:none">CORE OK</span><span class="badge" style="flex:none">SIM BROKER</span>
        </div>
        <span class="pill" style="flex:none">STREAMING</span>
      </div>
      <button type="button">STOP</button>
    </header></body></html>`;
  await page.setContent(header('40px'));
  expect((await headerOverlaps(page)).join('\n')).toMatch(/SIM BROKER \/ STREAMING|CORE OK \/ STREAMING/);
  await page.setContent(header('auto'));
  expect(await headerOverlaps(page)).toEqual([]);
});
