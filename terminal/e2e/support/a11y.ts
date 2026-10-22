/**
 * Accessibility audit for the end-to-end suite (WCAG 2.2 AA checks that can be decided mechanically).
 *
 * axe-core is MPL-2.0, outside this project's licence allowance (MIT/Apache/BSD/ISC), so the checks are
 * written here. Accessible names come from Playwright's own accessibility tree (`ariaSnapshot`); the rest
 * is computed in the page from rendered styles and geometry:
 *
 *   name        every button, link, form field, tab, option, switch and image has an accessible name (4.1.2, 1.1.1)
 *   contrast    visible text against its composited background: 4.5:1, or 3:1 for large text (1.4.3)
 *   field-edge  a form field's border or fill against what surrounds it: 3:1 (1.4.11)
 *   table       a name (caption/aria-label), header cells, header cells with text, valid aria-sort (1.3.1)
 *   ids         unique ids, and every aria-labelledby/-describedby/-controls/-activedescendant id exists (4.1.2)
 *   page        <html lang>, a document title, one visible h1, banner/navigation/main landmarks (3.1.1, 2.4.2, 1.3.1)
 *   target      pointer targets at least 24x24 CSS px, or spaced so a 24 px circle touches no other target (2.5.8)
 *   focus       (separate, by keyboard) each focused control shows an indicator with 3:1 contrast (2.4.7, 1.4.11)
 *
 * Disabled controls are exempt from contrast (WCAG 1.4.3 "inactive user interface components").
 */
import type { Page } from '@playwright/test';

export interface Violation {
  rule: string;
  where: string;
  target: string;
  detail: string;
}

interface InPageViolation {
  rule: string;
  target: string;
  detail: string;
}

/** What the audit looked at, so a silent pass can be told apart from an empty check. */
export interface AuditStats {
  textChecked: number;
  minTextRatio: number;
  fieldsChecked: number;
  tablesChecked: number;
  targetsChecked: number;
  unparsedColours: number;
}

/** Runs in the browser. Must be self-contained (it is serialized by page.evaluate). */
function inPageAudit(opts: { appPage: boolean }): { violations: InPageViolation[]; stats: AuditStats } {
  const out: InPageViolation[] = [];
  const stats: AuditStats = { textChecked: 0, minTextRatio: Infinity, fieldsChecked: 0, tablesChecked: 0, targetsChecked: 0, unparsedColours: 0 };
  const canvas = document.createElement('canvas');
  canvas.width = 1;
  canvas.height = 1;
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
  type RGBA = [number, number, number, number];
  const cache = new Map<string, RGBA>();
  const parse = (css: string): RGBA => {
    const hit = cache.get(css);
    if (hit) return hit;
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = '#010203';
    ctx.fillStyle = css;
    if (ctx.fillStyle === '#010203' && css.replace(/\s/g, '') !== 'rgb(1,2,3)') {
      stats.unparsedColours++;
      out.push({ rule: 'checker', target: css, detail: 'colour value could not be parsed; contrast not decided' });
    }
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    const v: RGBA = [d[0]!, d[1]!, d[2]!, d[3]! / 255];
    cache.set(css, v);
    return v;
  };
  const over = (top: RGBA, under: RGBA): RGBA => {
    const a = top[3] + under[3] * (1 - top[3]);
    if (a === 0) return [0, 0, 0, 0];
    const ch = (i: number) => (top[i]! * top[3] + under[i]! * under[3] * (1 - top[3])) / a;
    return [ch(0), ch(1), ch(2), a];
  };
  const lum = (c: RGBA) => {
    const f = (v: number) => {
      const s = v / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  };
  const ratio = (a: RGBA, b: RGBA) => {
    const [x, y] = [lum(a), lum(b)];
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  };
  const describe = (el: Element): string => {
    const id = el.id ? `#${el.id}` : '';
    const cls = typeof el.className === 'string' && el.className.trim() ? `.${el.className.trim().split(/\s+/).slice(0, 2).join('.')}` : '';
    const text = (el.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 40);
    return `${el.tagName.toLowerCase()}${id}${cls}${text ? ` "${text}"` : ''}`;
  };
  const shown = (el: Element): boolean => {
    if (!(el instanceof HTMLElement || el instanceof SVGElement)) return false;
    if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const srOnly = (el: Element): boolean => {
    for (let e: Element | null = el; e; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.clipPath === 'inset(50%)' || cs.clip === 'rect(0px, 0px, 0px, 0px)') return true;
      const r = e.getBoundingClientRect();
      if (cs.position === 'absolute' && r.width <= 1 && r.height <= 1) return true;
    }
    return false;
  };
  const disabled = (el: Element): boolean => !!el.closest(':disabled, [aria-disabled="true"], [inert]');
  /** Composited background behind `el`, or null when an image/gradient makes it undecidable. */
  const background = (el: Element): RGBA | null => {
    const layers: RGBA[] = [];
    for (let e: Element | null = el; e; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage !== 'none') return null;
      const c = parse(cs.backgroundColor);
      if (c[3] > 0) layers.push(c);
      if (c[3] >= 1) break;
    }
    let acc: RGBA = [255, 255, 255, 1];
    for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i]!, acc);
    return acc;
  };
  const opacityChain = (el: Element): number => {
    let o = 1;
    for (let e: Element | null = el; e; e = e.parentElement) o *= Number(getComputedStyle(e).opacity);
    return o;
  };

  // ---- contrast (1.4.3)
  const seen = new Set<Element>();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.nodeValue || !n.nodeValue.trim()) continue;
    const el = n.parentElement;
    if (!el || seen.has(el)) continue;
    seen.add(el);
    if (!shown(el) || srOnly(el) || disabled(el)) continue;
    if (el.closest('svg, [aria-hidden="true"]')) continue;
    const cs = getComputedStyle(el);
    const bg = background(el);
    if (!bg) continue;
    const fg0 = parse(cs.color);
    const fg: RGBA = [fg0[0], fg0[1], fg0[2], fg0[3] * opacityChain(el)];
    const shownFg = over(fg, bg);
    const r = ratio(shownFg, bg);
    stats.textChecked++;
    stats.minTextRatio = Math.min(stats.minTextRatio, r);
    const size = parseFloat(cs.fontSize);
    const bold = Number(cs.fontWeight) >= 700;
    const large = size >= 24 || (bold && size >= 18.66);
    const need = large ? 3 : 4.5;
    if (r + 1e-6 < need) {
      out.push({ rule: 'contrast', target: describe(el), detail: `${r.toFixed(2)}:1 < ${need}:1 (text ${cs.color} on ${`rgb(${bg.slice(0, 3).map(Math.round).join(',')})`}, ${size}px)` });
    }
  }

  // ---- field edges (1.4.11)
  for (const el of document.querySelectorAll('input:not([type=hidden]):not([type=radio]):not([type=checkbox]), select, textarea')) {
    if (!shown(el) || disabled(el)) continue;
    const parentBg = background(el.parentElement ?? document.body);
    const ownBg = background(el);
    if (!parentBg || !ownBg) continue;
    const cs = getComputedStyle(el);
    stats.fieldsChecked++;
    const sides = ['Top', 'Right', 'Bottom', 'Left'] as const;
    const edge = Math.max(
      ...sides.map((s) => (parseFloat(cs.getPropertyValue(`border-${s.toLowerCase()}-width`)) > 0 ? ratio(over(parse(cs.getPropertyValue(`border-${s.toLowerCase()}-color`)), parentBg), parentBg) : 1)),
    );
    const fill = ratio(ownBg, parentBg);
    if (edge < 3 && fill < 3) out.push({ rule: 'field-edge', target: describe(el), detail: `border ${edge.toFixed(2)}:1, fill ${fill.toFixed(2)}:1 against the surrounding background (need 3:1)` });
  }

  // ---- tables (1.3.1)
  for (const t of document.querySelectorAll('table')) {
    if (!shown(t)) continue;
    stats.tablesChecked++;
    const named = (t.querySelector(':scope > caption')?.textContent ?? '').trim() || t.getAttribute('aria-label') || t.getAttribute('aria-labelledby');
    if (!named) out.push({ rule: 'table', target: describe(t), detail: 'table has no caption or aria-label' });
    const ths = t.querySelectorAll('th');
    if (!ths.length && t.getAttribute('role') !== 'presentation') out.push({ rule: 'table', target: describe(t), detail: 'table has no header cells' });
    for (const th of ths) {
      const text = (th.textContent ?? '').trim() || th.getAttribute('aria-label');
      if (!text) out.push({ rule: 'table', target: describe(t), detail: `empty header cell ${describe(th)}` });
    }
    const sorted = [...t.querySelectorAll('[aria-sort]')];
    for (const s of sorted) {
      if (s.tagName !== 'TH' && s.getAttribute('role') !== 'columnheader') out.push({ rule: 'table', target: describe(s), detail: 'aria-sort on a non-header cell' });
      if (!['ascending', 'descending', 'none', 'other'].includes(s.getAttribute('aria-sort') ?? '')) out.push({ rule: 'table', target: describe(s), detail: `invalid aria-sort ${s.getAttribute('aria-sort')}` });
    }
    if (sorted.filter((s) => s.getAttribute('aria-sort') !== 'none').length > 1) out.push({ rule: 'table', target: describe(t), detail: 'more than one column marked sorted' });
  }

  // ---- ids and references (4.1.2)
  const ids = new Map<string, number>();
  for (const el of document.querySelectorAll('[id]')) ids.set(el.id, (ids.get(el.id) ?? 0) + 1);
  for (const [id, n] of ids) if (n > 1) out.push({ rule: 'ids', target: `#${id}`, detail: `id used ${n} times` });
  for (const attr of ['aria-labelledby', 'aria-describedby', 'aria-controls', 'aria-activedescendant', 'aria-errormessage', 'aria-owns', 'for']) {
    for (const el of document.querySelectorAll(`[${attr}]`)) {
      for (const ref of (el.getAttribute(attr) ?? '').split(/\s+/).filter(Boolean)) {
        if (!document.getElementById(ref)) out.push({ rule: 'ids', target: describe(el), detail: `${attr} refers to missing id "${ref}"` });
      }
    }
  }

  // ---- page (3.1.1, 2.4.2, 1.3.1)
  if (!document.documentElement.lang) out.push({ rule: 'page', target: 'html', detail: 'no lang attribute' });
  if (!document.title.trim()) out.push({ rule: 'page', target: 'title', detail: 'empty document title' });
  const h1s = [...document.querySelectorAll('h1')].filter(shown);
  if (h1s.length !== 1) out.push({ rule: 'page', target: 'h1', detail: `${h1s.length} visible h1 elements (expected 1)` });
  if (opts.appPage) {
    for (const [sel, what] of [['header, [role=banner]', 'banner'], ['nav, [role=navigation]', 'navigation'], ['main, [role=main]', 'main']] as const) {
      if (!document.querySelector(sel)) out.push({ rule: 'page', target: what, detail: `no ${what} landmark` });
    }
  }

  // ---- target size (2.5.8)
  const targets = [...document.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab], [role=option], [role=checkbox], [role=radio], [role=switch], [tabindex]:not([tabindex="-1"])')]
    .filter((el) => shown(el) && !srOnly(el))
    .filter((el) => !el.closest('[role=listbox]') || el.getAttribute('role') === 'option');
  const rects = targets.map((el) => el.getBoundingClientRect());
  stats.targetsChecked = targets.length;
  const undersized = (r: DOMRect) => r.width < 24 - 0.01 || r.height < 24 - 0.01;
  const inlineInText = (el: Element) => {
    if (el.tagName !== 'A') return false;
    const p = el.parentElement;
    if (!p) return false;
    const text = (p.textContent ?? '').replace(/\s+/g, ' ').trim();
    const own = (el.textContent ?? '').replace(/\s+/g, ' ').trim();
    return text.length > own.length + 3;
  };
  /** A target covered at its centre by something else (e.g. under a modal scrim) cannot be pointed at. */
  const covered = (r: DOMRect, el: Element) => {
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return false;
    const hit = document.elementFromPoint(cx, cy);
    return !!hit && hit !== el && !el.contains(hit) && !hit.contains(el);
  };
  // While a modal dialog is open, everything outside it and the header lies under its scrim.
  const modal = document.querySelector('[aria-modal="true"]');
  const reachable = (el: Element) => !modal || modal.contains(el) || !!el.closest('header, [role=banner]');
  targets.forEach((el, i) => {
    const r = rects[i]!;
    if (!undersized(r)) return;
    if (!reachable(el) || covered(r, el)) return;
    if (inlineInText(el)) return; // inline exception
    if ((el instanceof HTMLInputElement && (el.type === 'radio' || el.type === 'checkbox')) && el.closest('label')) return; // the label is the target
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') return; // full-width fields; height checked by layout
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const clash = targets.some((other, j) => {
      if (j === i || other.contains(el) || el.contains(other) || !reachable(other)) return false;
      const o = rects[j]!;
      const dx = Math.max(o.left - cx, 0, cx - o.right);
      const dy = Math.max(o.top - cy, 0, cy - o.bottom);
      if (Math.hypot(dx, dy) < 12) return true;
      if (undersized(o)) return Math.hypot(o.left + o.width / 2 - cx, o.top + o.height / 2 - cy) < 24;
      return false;
    });
    if (clash) out.push({ rule: 'target', target: describe(el), detail: `${r.width.toFixed(1)}x${r.height.toFixed(1)} px and closer than 24 px to another target` });
  });
  return { violations: out, stats };
}

const NAMED_ROLES = ['button', 'link', 'textbox', 'combobox', 'checkbox', 'radio', 'tab', 'switch', 'slider', 'spinbutton', 'searchbox', 'menuitem', 'option', 'img', 'listbox', 'dialog'];

/** Unnamed controls according to Playwright's accessibility tree. */
async function unnamed(page: Page): Promise<InPageViolation[]> {
  const yaml = await page.locator('body').ariaSnapshot();
  const out: InPageViolation[] = [];
  const re = new RegExp(`^\\s*- (${NAMED_ROLES.join('|')})(?=$|[\\s:\\[])(?! ")`);
  for (const line of yaml.split('\n')) {
    if (re.test(line)) out.push({ rule: 'name', target: line.trim(), detail: 'no accessible name' });
  }
  return out;
}

export async function auditWithStats(page: Page, where: string, opts: { appPage?: boolean } = {}): Promise<{ violations: Violation[]; stats: AuditStats }> {
  const { violations, stats } = await page.evaluate(inPageAudit, { appPage: opts.appPage ?? true });
  const found = [...violations, ...(await unnamed(page))];
  return { violations: found.map((v) => ({ ...v, where })), stats };
}

export async function audit(page: Page, where: string, opts: { appPage?: boolean } = {}): Promise<Violation[]> {
  return (await auditWithStats(page, where, opts)).violations;
}

/** Tabs through the first `count` stops from the top and checks each shows a 3:1 focus indicator. */
export async function auditFocus(page: Page, where: string, count = 30): Promise<Violation[]> {
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur();
    window.scrollTo(0, 0);
  });
  const out: Violation[] = [];
  const seen = new Set<string>();
  for (let i = 0; i < count; i++) {
    await page.keyboard.press('Tab');
    const r = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      const cs = getComputedStyle(el);
      const outline = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 1 ? cs.outlineColor : null;
      const shadow = cs.boxShadow !== 'none' ? cs.boxShadow : null;
      let bg = 'rgb(255,255,255)';
      for (let e: Element | null = el.parentElement; e; e = e.parentElement) {
        const b = getComputedStyle(e).backgroundColor;
        if (b && b !== 'rgba(0, 0, 0, 0)' && b !== 'transparent') {
          bg = b;
          break;
        }
      }
      const key = `${el.tagName}#${el.id}.${typeof el.className === 'string' ? el.className : ''}:${(el.textContent ?? '').trim().slice(0, 30)}`;
      return { key, outline, shadow, bg, desc: key };
    });
    if (!r || seen.has(r.key)) continue;
    seen.add(r.key);
    if (!r.outline && !r.shadow) {
      out.push({ rule: 'focus', where, target: r.desc, detail: 'no visible focus indicator (outline or box-shadow)' });
      continue;
    }
    if (r.outline) {
      const ok = await page.evaluate(({ fg, bg }) => {
        const c = document.createElement('canvas').getContext('2d')!;
        const rgb = (css: string) => {
          c.clearRect(0, 0, 1, 1);
          c.fillStyle = css;
          c.fillRect(0, 0, 1, 1);
          const d = c.getImageData(0, 0, 1, 1).data;
          return [d[0]!, d[1]!, d[2]!];
        };
        const lum = (v: number[]) => v.map((x) => x / 255).map((s) => (s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4)).reduce((a, s, i) => a + s * [0.2126, 0.7152, 0.0722][i]!, 0);
        const [a, b] = [lum(rgb(fg)), lum(rgb(bg))];
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      }, { fg: r.outline, bg: r.bg });
      if (ok < 3) out.push({ rule: 'focus', where, target: r.desc, detail: `focus outline ${r.outline} on ${r.bg} is ${ok.toFixed(2)}:1 (< 3:1)` });
    }
  }
  return out;
}

/** Visible header items whose boxes intersect (an item clipped out of the status row counts as hidden). */
export async function headerOverlaps(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const status = document.querySelector('.topbar-status')?.getBoundingClientRect();
    const items = [...document.querySelectorAll('.topbar .pill, .topbar .badge, .topbar button, .topbar .clock, .topbar .account-id, .topbar .brand')]
      .filter((el) => !el.parentElement?.closest('.pill, .badge, button'))
      .map((el) => ({ el, r: el.getBoundingClientRect() }))
      .filter(({ el, r }) => r.width > 0 && r.height > 0 && el.checkVisibility())
      .filter(({ el, r }) => !status || !el.closest('.topbar-status') || r.left < status.right - 1);
    const out: string[] = [];
    items.forEach((a, i) =>
      items.slice(i + 1).forEach((b) => {
        const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
        const h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
        if (w > 1 && h > 1) out.push(`${(a.el.textContent ?? '').trim()} / ${(b.el.textContent ?? '').trim()}`);
      }),
    );
    return out;
  });
}
