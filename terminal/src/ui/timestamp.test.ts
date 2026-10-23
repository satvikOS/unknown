/** <Timestamp>: the API's exact *_ns_str wins over the float JSON.parse made of the same instant. */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Timestamp } from './primitives';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..');

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((d) => {
    const path = join(dir, d.name);
    if (d.isDirectory()) return sources(path);
    return d.name.endsWith('.tsx') && !d.name.includes('.test.') ? [path] : [];
  });
}

const text = (html: string) => html.replace(/<[^>]+>/g, '');

describe('Timestamp', () => {
  const exact = '1792675849903999999'; // a float makes it ...904000000: one millisecond late
  const float = Number(exact);

  it('shows the right millisecond from the exact string, and every nanosecond in its tooltip', () => {
    const html = renderToStaticMarkup(createElement(Timestamp, { ns: float, nsStr: exact, withMs: true }));
    expect(text(html)).toBe('2026-10-22 13:30:49.903 UTC');
    expect(html).toMatch(/title="[^"]*:49\.903999999"/); // local time: the zone offset is whole minutes
  });

  it('shows all nine fraction digits with withNs', () => {
    expect(text(renderToStaticMarkup(createElement(Timestamp, { ns: float, nsStr: exact, withNs: true })))).toBe('2026-10-22 13:30:49.903999999 UTC');
  });

  it('without the string it falls back to the float, at millisecond precision', () => {
    expect(text(renderToStaticMarkup(createElement(Timestamp, { ns: float, withMs: true })))).toBe('2026-10-22 13:30:49.904 UTC');
    expect(text(renderToStaticMarkup(createElement(Timestamp, { ns: float, nsStr: 'garbage', withMs: true })))).toBe('2026-10-22 13:30:49.904 UTC');
    expect(text(renderToStaticMarkup(createElement(Timestamp, { ns: null, nsStr: null, never: 'not yet' })))).toBe('not yet');
  });

  it('every <Timestamp ns=…> (and research <NsTime ns=…>) in the terminal also passes the API\'s exact string (nsStr)', () => {
    const missing: string[] = [];
    let checked = 0;
    for (const file of sources(SRC)) {
      const source = readFileSync(file, 'utf8');
      for (const m of source.matchAll(/<(?:Timestamp|NsTime)\b[\s\S]*?\/>/g)) {
        if (!/\sns=\{/.test(m[0])) continue; // ms={...}: a browser time, not the API's
        checked += 1;
        if (!/\snsStr=\{/.test(m[0])) missing.push(`${relative(SRC, file)}: ${m[0].replace(/\s+/g, ' ')}`);
      }
    }
    expect(checked).toBeGreaterThan(80);
    expect(missing).toEqual([]);
  });
});
