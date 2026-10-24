import { describe, expect, it } from 'vitest';
import {
  OWNER,
  crc32,
  neutralize,
  prepareExport,
  rightsDecision,
  roundTripsAsDouble,
  runExport,
  resetRightsCache,
  toCsv,
  toXlsx,
  type ExportSource,
  type ExportTable,
  type RightsRegistry,
} from './export';

const REG: RightsRegistry = {
  registry: 'AVAILABLE',
  rule: 'test rule',
  records: {
    'sec-edgar': { name: 'SEC EDGAR', verified_on: '2026-10-22', terms: [], datasets: { 'sec.submissions': { allowed_uses: ['display', 'storage'], rights_source: 'https://www.sec.gov/privacy', rights_verified_on: '2026-10-22' } } },
    alpaca: { name: 'Alpaca', verified_on: '2026-10-22', terms: [], datasets: { 'alpaca.stock_stream': { allowed_uses: ['display', 'storage'], rights_source: 'terms', rights_verified_on: '2026-10-22' } } },
    blocked: { name: 'Blocked', verified_on: '2026-10-22', terms: [], datasets: { 'b.site': { allowed_uses: [], rights_source: 'terms', rights_verified_on: '2026-10-22' } } },
  },
  aliases: { 'news:sec-filings': { record: 'sec-edgar', dataset: 'sec.submissions' }, 'marketdata:ALPACA': { record: 'alpaca', dataset: 'alpaca.stock_stream' } },
};

interface Row {
  name: string;
  qty: string | null;
  px: number | null;
  at: string | null;
}

function table(rows: Row[], extra: Partial<ExportTable<Row>> = {}): ExportTable<Row> {
  return {
    title: 'Test grid',
    rows,
    provenance: { source: 'test', asOf: '2026-10-24T00:00:00.000Z', filters: { scope: 'system' } },
    columns: [
      { id: 'name', header: 'Name', value: (r) => r.name },
      { id: 'qty', header: 'Quantity', kind: 'decimal', value: (r) => r.qty, reason: () => 'NOT_REPORTED' },
      { id: 'px', header: 'Beta', kind: 'number', value: (r) => r.px },
      { id: 'at', header: 'Time', kind: 'datetime', value: (r) => r.at },
    ],
    ...extra,
  };
}

/** RFC 4180 parser (test oracle). */
function parseCsv(text: string, d = ','): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let f = '';
  let q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!;
    if (q) {
      if (c === '"' && text[i + 1] === '"') {
        f += '"';
        i++;
      } else if (c === '"') q = false;
      else f += c;
    } else if (c === '"') q = true;
    else if (c === d) {
      row.push(f);
      f = '';
    } else if (c === '\r' && text[i + 1] === '\n') {
      row.push(f);
      rows.push(row);
      row = [];
      f = '';
      i++;
    } else f += c;
  }
  return rows;
}

function unzip(bytes: Uint8Array): Map<string, string> {
  const out = new Map<string, string>();
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let at = 0;
  while (dv.getUint32(at, true) === 0x04034b50) {
    const crc = dv.getUint32(at + 14, true);
    const size = dv.getUint32(at + 18, true);
    const nlen = dv.getUint16(at + 26, true);
    const name = new TextDecoder().decode(bytes.subarray(at + 30, at + 30 + nlen));
    const data = bytes.subarray(at + 30 + nlen, at + 30 + nlen + size);
    expect(crc32(data)).toBe(crc);
    out.set(name, new TextDecoder().decode(data));
    at += 30 + nlen + size;
  }
  return out;
}

const AT = new Date('2026-10-24T12:00:00Z');

describe('cell rules', () => {
  it('neutralises formula starters including full-width forms and leaves numbers alone', () => {
    for (const s of ['=HYPERLINK("http://x")', '＝HYPERLINK("x")', '+1', '-cmd', '@SUM(A1)', '\tx', '\rx', '\nx']) expect(neutralize(s)).toBe(`'${s}`);
    expect(neutralize('plain')).toBe('plain');
    const p = prepareExport(table([{ name: '=HYPERLINK("x")', qty: '-5', px: -0.5, at: null }]), REG, AT);
    expect(p.rows[0]![0]!.text).toBe(`'=HYPERLINK("x")`);
    expect(p.rows[0]![1]).toEqual({ text: '-5', type: 'number', v: '-5' });
    expect(p.rows[0]![2]!.text).toBe('-0.5');
  });

  it('writes a decimal as a number only when it round-trips through a double', () => {
    expect(roundTripsAsDouble('0.1')).toBe(true);
    expect(roundTripsAsDouble('-5.000000')).toBe(true);
    expect(roundTripsAsDouble('123456789012.345')).toBe(true);
    expect(roundTripsAsDouble('12345678901234567.89')).toBe(false);
    expect(roundTripsAsDouble('0.1234567890123456789')).toBe(false);
    const p = prepareExport(table([{ name: 'a', qty: '12345678901234567.89', px: null, at: null }]), REG, AT);
    expect(p.rows[0]![1]!.type).toBe('string');
    expect(p.textDecimals).toEqual(['Quantity']);
    expect(p.provenance.some(([k]) => k === 'Text decimals')).toBe(true);
  });

  it('exports a missing value as empty with a status column, never 0', () => {
    const p = prepareExport(table([{ name: 'a', qty: null, px: null, at: null }, { name: 'b', qty: '1', px: 2, at: '2026-10-24T13:30:00Z' }]), REG, AT);
    expect(p.headers).toEqual(['Name', 'Quantity', 'Quantity status', 'Beta', 'Beta status', 'Time', 'Time zone', 'Time status']);
    const csv = parseCsv(toCsv(p, { provenance: false }));
    expect(csv[1]).toEqual(['a', '', 'NOT_REPORTED', '', 'NO_DATA', '', 'UTC', 'NO_DATA']);
    expect(csv[2]).toEqual(['b', '1', '', '2', '', '2026-10-24T13:30:00.000Z', 'UTC', '']);
  });
});

describe('CSV', () => {
  it('re-imports random Unicode text with commas, quotes and line breaks to identical values', () => {
    let seed = 7;
    const rnd = () => (seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31;
    const alphabet = ['a', 'Z', ',', ';', '"', '\r', '\n', ' ', 'é', '漢', '😀', ' ', 'x', '1'];
    const rows: Row[] = Array.from({ length: 300 }, () => ({
      name: 'k' + Array.from({ length: Math.floor(rnd() * 12) }, () => alphabet[Math.floor(rnd() * alphabet.length)]).join(''),
      qty: String(Math.floor(rnd() * 1e6) / 100),
      px: null,
      at: null,
    }));
    for (const d of [',', ';'] as const) {
      const p = prepareExport(table(rows), REG, AT, d);
      const parsed = parseCsv(toCsv(p, { delimiter: d, provenance: false }), d).slice(1);
      expect(parsed.map((r) => r[0])).toEqual(rows.map((r) => r.name));
      expect(parsed.map((r) => r[1])).toEqual(rows.map((r) => r.qty));
    }
  });

  it('starts with the provenance block and records the delimiter', () => {
    const csv = toCsv(prepareExport(table([{ name: 'a', qty: '1', px: 1, at: null }]), REG, AT, ';'), { delimiter: ';' });
    const parsed = parseCsv(csv, ';');
    expect(parsed[0]).toEqual(['Export', 'Test grid']);
    expect(parsed).toContainEqual(['Delimiter (CSV)', 'semicolon']);
    expect(parsed).toContainEqual(['Filter: scope', 'system']);
  });
});

describe('XLSX', () => {
  it('is a valid stored ZIP with typed cells, a date cell and a Provenance sheet', () => {
    const p = prepareExport(table([{ name: '=1+1', qty: '-5', px: 1.25, at: '2026-10-24T12:00:00Z' }, { name: 'big', qty: '12345678901234567.89', px: null, at: null }]), REG, AT);
    const files = unzip(toXlsx(p));
    expect([...files.keys()]).toEqual(['[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml', 'xl/_rels/workbook.xml.rels', 'xl/styles.xml', 'xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml']);
    const s1 = files.get('xl/worksheets/sheet1.xml')!;
    expect(s1).toContain('<c r="B2"><v>-5</v></c>');
    expect(s1).toContain(`<c r="A2" t="inlineStr"><is><t xml:space="preserve">'=1+1</t></is></c>`);
    expect(s1).toContain('<c r="C2"><v>1.25</v></c>');
    expect(s1).toContain('<c r="E2" s="1"><v>46319.5</v></c>');
    expect(s1).toContain('<t xml:space="preserve">12345678901234567.89</t>');
    expect(files.get('xl/workbook.xml')).toContain('name="Provenance"');
    expect(files.get('xl/worksheets/sheet2.xml')).toContain('Missing values');
    expect(toXlsx(p)).toEqual(toXlsx(p));
  });
});

describe('rights gating', () => {
  const lic = (source: string, realtime = false): ExportSource => ({ kind: 'licensed', source, realtime });

  it('allows owner data, gates licensed data on storage, real-time on redistribution, and fails closed', () => {
    const cases: [ExportSource, RightsRegistry | null, boolean][] = [
      [OWNER, null, true],
      [OWNER, REG, true],
      [lic('news:sec-filings'), REG, true],
      [lic('sec-edgar'), REG, true],
      [lic('sec-edgar/sec.submissions'), REG, true],
      [lic('marketdata:ALPACA'), REG, true],
      [lic('marketdata:ALPACA', true), REG, false],
      [lic('marketdata:IBKR'), REG, false],
      [lic('blocked'), REG, false],
      [lic('sec-edgar/unknown.dataset'), REG, false],
      [lic('news:sec-filings'), null, false],
      [lic('news:sec-filings'), { ...REG, registry: 'UNAVAILABLE' }, false],
    ];
    let falseAllow = 0;
    for (const [src, reg, expected] of cases) {
      const d = rightsDecision(src, reg);
      if (d.allowed && !expected) falseAllow++;
      expect(d.allowed, JSON.stringify(src)).toBe(expected);
    }
    expect(falseAllow).toBe(0);
    expect(rightsDecision(lic('news:sec-filings'), REG).licence).toContain('rights read from https://www.sec.gov/privacy');
  });

  it('excludes a column whose rows draw on a source without rights, and lists it', () => {
    const t = table([{ name: 'a', qty: '1', px: 1, at: null }]);
    const cols = [...t.columns, { id: 'bid', header: 'Bid', kind: 'decimal' as const, value: () => '10.00', source: () => lic('marketdata:IBKR') }];
    const p = prepareExport({ ...t, columns: cols }, REG, AT);
    expect(p.headers).not.toContain('Bid');
    expect(p.excluded).toEqual([{ column: 'Bid', reason: 'no rights record for source marketdata:IBKR' }]);
    expect(p.provenance).toContainEqual(['Excluded column', 'Bid: no rights record for source marketdata:IBKR']);
  });

  it('runs end to end without a registry and still exports owner columns', async () => {
    resetRightsCache();
    const saved: [string, Blob][] = [];
    const msg = await runExport(table([{ name: 'a', qty: '1', px: 1, at: null }]), 'csv', {
      fetchImpl: (() => Promise.reject(new Error('offline'))) as unknown as typeof fetch,
      save: (n, b) => saved.push([n, b]),
    });
    expect(msg).toBe('Exported 1 row(s) to CSV.');
    expect(saved[0]![0]).toMatch(/^test-grid-\d{8}-\d{6}\.csv$/);
    resetRightsCache();
  });
});
