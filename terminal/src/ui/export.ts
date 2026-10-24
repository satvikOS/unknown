/**
 * Export any data grid to CSV or XLSX (CAP-UX-07). One primitive for every workspace:
 *
 * - Values come from the API's stored decimals. A decimal string is written as it is in CSV. In XLSX a decimal is a
 *   number cell only when it round-trips exactly through an IEEE-754 double at its stored scale and has at most 15
 *   significant digits; otherwise it is a text cell, and the Provenance sheet says so (no silent rounding).
 * - Formula injection (OWASP CSV injection): a TEXT cell that starts with = + - @, tab, CR, LF or a full-width
 *   = + - @ is prefixed with an apostrophe. Typed numeric cells (a negative number) are not altered.
 * - A missing value is exported empty, never 0, with a "<column> status" column carrying the reason code.
 * - Date-times: ISO 8601 with a UTC offset in CSV; an Excel date cell plus a "<column> zone" column (UTC) in XLSX.
 * - Provenance: CSV starts with key/value rows (source, as-of, filters, rights) and a blank line; XLSX has a
 *   Provenance sheet. Both list excluded columns and the licence notes of every licensed source used.
 * - Rights gating (fail closed): owner-generated columns are always exported; a licensed column only when every
 *   source it draws on has a rights record allowing 'storage' (and 'redistribution' for real-time exchange data),
 *   from GET /api/export/rights. Without the registry every licensed column is excluded.
 *
 * XLSX is written with no library: a minimal SpreadsheetML package (content types, workbook, two sheets, styles) in a
 * ZIP archive with stored (uncompressed) entries and CRC-32.
 */

export type ExportCell = string | number | null | undefined;
export type ExportKind = 'text' | 'decimal' | 'number' | 'datetime';
export type ExportSource = { kind: 'owner' } | { kind: 'licensed'; source: string; realtime?: boolean };

export const OWNER: ExportSource = { kind: 'owner' };

/** A value from the market-data bus source `source` (IBKR, ALPACA, ...); `realtime` for exchange real-time data. */
export function marketSource(source: string | null | undefined, realtime: boolean): ExportSource {
  return { kind: 'licensed', source: `marketdata:${(source ?? 'UNKNOWN').toUpperCase()}`, realtime };
}

/** Epoch nanoseconds (number or string) to ISO 8601, for a provenance as-of; null when absent. */
export function nsToIso(ns: unknown): string | null {
  if (typeof ns !== 'number' && typeof ns !== 'string') return null;
  return isoOf(ns);
}

export interface ExportColumn<T> {
  id: string;
  header: string;
  /** 'decimal': exact decimal strings; 'number': display floats (statistics); 'datetime': ISO 8601 or epoch ns. */
  kind?: ExportKind;
  value: (row: T) => ExportCell;
  /** The reason code written to the status column when the value is missing (default NO_DATA). */
  reason?: (row: T) => string | null | undefined;
  /** Where the column's values come from; a function when it differs per row (default: owner-generated). */
  source?: ExportSource | ((row: T) => ExportSource);
}

export interface Provenance {
  /** What the grid shows and where it came from, e.g. "Positions: GET /api/portfolio/holdings". */
  source: string;
  /** ISO 8601 as-of of the data, or null with a reason. */
  asOf: string | null;
  asOfReason?: string;
  filters?: Record<string, string>;
  notes?: readonly string[];
}

export interface ExportTable<T> {
  title: string;
  columns: readonly ExportColumn<T>[];
  rows: readonly T[];
  provenance: Provenance;
}

export interface RightsDataset {
  allowed_uses: string[];
  rights_source: string;
  rights_verified_on: string;
}
export interface RightsRecord {
  name: string;
  verified_on: string;
  terms: string[];
  datasets: Record<string, RightsDataset>;
}
export interface RightsRegistry {
  registry: 'AVAILABLE' | 'UNAVAILABLE';
  records: Record<string, RightsRecord>;
  aliases: Record<string, { record: string; dataset: string }>;
  rule: string;
  detail?: string;
}

export interface Decision {
  allowed: boolean;
  reason: string;
  licence: string | null;
}

/** Whether one source may be exported, and the licence note to print. */
export function rightsDecision(src: ExportSource, reg: RightsRegistry | null): Decision {
  if (src.kind === 'owner') return { allowed: true, reason: 'owner-generated data', licence: null };
  if (!reg || reg.registry !== 'AVAILABLE') {
    return { allowed: false, reason: `rights registry unavailable${reg?.detail ? ` (${reg.detail})` : ''}: licensed columns are excluded`, licence: null };
  }
  const alias = reg.aliases[src.source];
  const [recId, dsId] = alias ? [alias.record, alias.dataset] : (src.source.split('/', 2) as [string, string | undefined]);
  const rec = recId ? reg.records[recId] : undefined;
  if (!rec) return { allowed: false, reason: `no rights record for source ${src.source}`, licence: null };
  const datasets = dsId ? (rec.datasets[dsId] ? [[dsId, rec.datasets[dsId]] as const] : []) : Object.entries(rec.datasets);
  if (datasets.length === 0) return { allowed: false, reason: `no rights record for dataset ${dsId ?? src.source}`, licence: null };
  const need = src.realtime ? ['storage', 'redistribution'] : ['storage'];
  const notes = datasets.map(([id, d]) => `${rec.name} / ${id}: allowed uses ${d.allowed_uses.join(', ') || 'none'} (rights read from ${d.rights_source}, ${d.rights_verified_on})`);
  for (const [id, d] of datasets) {
    const missing = need.filter((u) => !d.allowed_uses.includes(u));
    if (missing.length) {
      return {
        allowed: false,
        reason: `${rec.name} / ${id} does not allow ${missing.join(' and ')}${src.realtime ? ' (real-time exchange data)' : ''}`,
        licence: notes.join('; '),
      };
    }
  }
  return { allowed: true, reason: 'allowed by the rights record', licence: notes.join('; ') };
}

// ------------------------------------------------------------------ cell rules

const DECIMAL = /^-?\d+(\.\d+)?$/;
const FORMULA_START = /^[=+\-@\t\r\n＝＋－＠]/;

/** OWASP: neutralise a text cell a spreadsheet would read as a formula. */
export function neutralize(text: string): string {
  return FORMULA_START.test(text) ? `'${text}` : text;
}

/** Whether an exact decimal string survives a trip through an IEEE-754 double at its own scale (and Excel's 15 digits). */
export function roundTripsAsDouble(s: string): boolean {
  if (!DECIMAL.test(s)) return false;
  const scale = s.includes('.') ? s.length - s.indexOf('.') - 1 : 0;
  const digits = s.replace(/^-/, '').replace('.', '').replace(/^0+/, '');
  if (digits.length > 15) return false;
  const n = Number(s);
  if (!Number.isFinite(n) || scale > 100) return false;
  const back = n.toFixed(scale);
  const norm = (x: string) => (/^-0(\.0*)?$/.test(x) ? x.slice(1) : x);
  return norm(back) === norm(s);
}

/** Epoch nanoseconds (number or digit string) or an ISO string -> ISO 8601 UTC with milliseconds, or null. */
export function isoOf(v: ExportCell): string | null {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number' || /^\d{10,}$/.test(String(v))) {
    const ms = typeof v === 'number' ? v / 1e6 : Number(BigInt(String(v)) / 1_000_000n);
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  }
  const d = new Date(String(v));
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

interface Cell {
  /** Text for CSV (already neutralised for text cells). */
  text: string;
  /** XLSX typing. */
  type: 'string' | 'number' | 'date' | 'empty';
  /** For number/date cells, the value written in <v>. */
  v?: string;
}

export interface PreparedExport {
  title: string;
  headers: string[];
  rows: Cell[][];
  provenance: [string, string][];
  excluded: { column: string; reason: string }[];
  licences: string[];
  textDecimals: string[];
}

function sourcesOf<T>(c: ExportColumn<T>, rows: readonly T[]): ExportSource[] {
  if (!c.source) return [OWNER];
  if (typeof c.source !== 'function') return [c.source];
  const seen = new Map<string, ExportSource>();
  for (const r of rows) {
    const s = c.source(r);
    seen.set(s.kind === 'owner' ? 'owner' : `${s.source}|${s.realtime ? 1 : 0}`, s);
  }
  return seen.size ? [...seen.values()] : [OWNER];
}

/** Excel serial date (days since 1899-12-30) from an ISO string. */
function excelSerial(iso: string): string {
  return String((Date.parse(iso) - Date.UTC(1899, 11, 30)) / 86_400_000);
}

export function prepareExport<T>(table: ExportTable<T>, reg: RightsRegistry | null, generatedAt: Date, delimiter = ','): PreparedExport {
  const excluded: { column: string; reason: string }[] = [];
  const licences = new Set<string>();
  const cols: ExportColumn<T>[] = [];
  for (const c of table.columns) {
    const decisions = sourcesOf(c, table.rows).map((s) => rightsDecision(s, reg));
    for (const d of decisions) if (d.licence) licences.add(d.licence);
    const refused = decisions.find((d) => !d.allowed);
    if (refused) excluded.push({ column: c.header, reason: refused.reason });
    else cols.push(c);
  }
  const headers: string[] = [];
  const plan: { col: ExportColumn<T>; status: boolean; zone: boolean }[] = [];
  for (const c of cols) {
    const status = table.rows.some((r) => {
      const v = c.value(r);
      return v === null || v === undefined || v === '' || (c.kind === 'datetime' && isoOf(v) === null);
    });
    const zone = c.kind === 'datetime';
    headers.push(c.header);
    if (zone) headers.push(`${c.header} zone`);
    if (status) headers.push(`${c.header} status`);
    plan.push({ col: c, status, zone });
  }
  const textDecimals = new Set<string>();
  const rows = table.rows.map((r) => {
    const out: Cell[] = [];
    for (const { col, status, zone } of plan) {
      const raw = col.value(r);
      let cell: Cell;
      let missing = raw === null || raw === undefined || raw === '';
      if (missing) cell = { text: '', type: 'empty' };
      else if (col.kind === 'datetime') {
        const iso = isoOf(raw);
        if (iso === null) {
          missing = true;
          cell = { text: '', type: 'empty' };
        } else cell = { text: iso, type: 'date', v: excelSerial(iso) };
      } else if ((col.kind === 'decimal' || col.kind === 'number') && typeof raw === 'number') {
        cell = Number.isFinite(raw) ? { text: String(raw), type: 'number', v: String(raw) } : { text: '', type: 'empty' };
        if (!Number.isFinite(raw)) missing = true;
      } else if (col.kind === 'decimal' && DECIMAL.test(String(raw))) {
        const s = String(raw);
        if (roundTripsAsDouble(s)) cell = { text: s, type: 'number', v: s };
        else {
          cell = { text: s, type: 'string' };
          textDecimals.add(col.header);
        }
      } else {
        const s = neutralize(String(raw));
        cell = { text: s, type: 'string' };
      }
      out.push(cell);
      if (zone) out.push({ text: 'UTC', type: 'string' });
      if (status) {
        const why = missing ? (col.reason?.(r) ?? 'NO_DATA') : '';
        out.push({ text: why ? neutralize(why) : '', type: why ? 'string' : 'empty' });
      }
    }
    return out;
  });
  const provenance: [string, string][] = [
    ['Export', table.title],
    ['Source', table.provenance.source],
    ['As of', table.provenance.asOf ?? `unavailable (${table.provenance.asOfReason ?? 'NO_DATA'})`],
    ['Generated', generatedAt.toISOString()],
    ['Rows', String(table.rows.length)],
    ['Delimiter (CSV)', delimiter === ';' ? 'semicolon' : 'comma'],
  ];
  for (const [k, v] of Object.entries(table.provenance.filters ?? {})) provenance.push([`Filter: ${k}`, v]);
  for (const n of table.provenance.notes ?? []) provenance.push(['Note', n]);
  provenance.push(['Rights rule', reg?.rule ?? 'rights registry unavailable: only owner-generated columns are exported']);
  for (const l of licences) provenance.push(['Licence', l]);
  for (const e of excluded) provenance.push(['Excluded column', `${e.column}: ${e.reason}`]);
  for (const t of textDecimals) provenance.push(['Text decimals', `${t}: values that do not round-trip through a double are text cells (exact)`]);
  provenance.push(['Missing values', 'empty, never 0; the adjacent status column carries the reason code']);
  return {
    title: table.title,
    headers,
    rows,
    provenance: provenance.map(([k, v]) => [neutralize(k), neutralize(v)]),
    excluded,
    licences: [...licences],
    textDecimals: [...textDecimals],
  };
}

// ------------------------------------------------------------------ CSV (RFC 4180)

function csvField(text: string, delimiter: string): string {
  return text.includes('"') || text.includes(delimiter) || text.includes('\n') || text.includes('\r') ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv(p: PreparedExport, { delimiter = ',', provenance = true }: { delimiter?: ',' | ';'; provenance?: boolean } = {}): string {
  const line = (cells: string[]) => cells.map((c) => csvField(c, delimiter)).join(delimiter);
  const out: string[] = [];
  if (provenance) {
    for (const [k, v] of p.provenance) out.push(line([k, v]));
    out.push('');
  }
  out.push(line(p.headers.map(neutralize)));
  for (const r of p.rows) out.push(line(r.map((c) => c.text)));
  return out.join('\r\n') + '\r\n';
}

// ------------------------------------------------------------------ XLSX (SpreadsheetML in a stored ZIP)

const XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n';
const NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
const NS_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const NS_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships';

export function xmlText(s: string): string {
  // eslint-disable-next-line no-control-regex
  return s.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g, '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function colName(i: number): string {
  let s = '';
  for (let n = i + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}

function sheetXml(rows: Cell[][]): string {
  const body = rows
    .map((r, ri) => {
      const cells = r
        .map((c, ci) => {
          const ref = `${colName(ci)}${ri + 1}`;
          if (c.type === 'empty') return '';
          if (c.type === 'number') return `<c r="${ref}"><v>${c.v}</v></c>`;
          if (c.type === 'date') return `<c r="${ref}" s="1"><v>${c.v}</v></c>`;
          return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xmlText(c.text)}</t></is></c>`;
        })
        .join('');
      return `<row r="${ri + 1}">${cells}</row>`;
    })
    .join('');
  return `${XML_HEAD}<worksheet xmlns="${NS_MAIN}"><sheetData>${body}</sheetData></worksheet>`;
}

const STYLES =
  `${XML_HEAD}<styleSheet xmlns="${NS_MAIN}"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts>` +
  '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>' +
  '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>' +
  '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +
  '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
  '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>' +
  '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>';

export function xlsxEntries(p: PreparedExport): [string, string][] {
  const head: Cell[] = p.headers.map((h) => ({ text: neutralize(h), type: 'string' }));
  const prov: Cell[][] = p.provenance.map(([k, v]) => [
    { text: k, type: 'string' },
    { text: v, type: 'string' },
  ]);
  return [
    [
      '[Content_Types].xml',
      `${XML_HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
        '<Default Extension="xml" ContentType="application/xml"/>' +
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
    ],
    ['_rels/.rels', `${XML_HEAD}<Relationships xmlns="${NS_PKG}"><Relationship Id="rId1" Type="${NS_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>`],
    [
      'xl/workbook.xml',
      `${XML_HEAD}<workbook xmlns="${NS_MAIN}" xmlns:r="${NS_REL}"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/><sheet name="Provenance" sheetId="2" r:id="rId2"/></sheets></workbook>`,
    ],
    [
      'xl/_rels/workbook.xml.rels',
      `${XML_HEAD}<Relationships xmlns="${NS_PKG}"><Relationship Id="rId1" Type="${NS_REL}/worksheet" Target="worksheets/sheet1.xml"/>` +
        `<Relationship Id="rId2" Type="${NS_REL}/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="${NS_REL}/styles" Target="styles.xml"/></Relationships>`,
    ],
    ['xl/styles.xml', STYLES],
    ['xl/worksheets/sheet1.xml', sheetXml([head, ...p.rows])],
    ['xl/worksheets/sheet2.xml', sheetXml(prov)],
  ];
}

let CRC_TABLE: Uint32Array | null = null;
export function crc32(bytes: Uint8Array): number {
  if (!CRC_TABLE) {
    CRC_TABLE = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      CRC_TABLE[n] = c >>> 0;
    }
  }
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]!) & 0xff]! ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/** A ZIP archive with stored entries (method 0) and a fixed 1980-01-01 timestamp (byte-identical output). */
export function zipStored(entries: readonly [string, Uint8Array][]): Uint8Array {
  const enc = new TextEncoder();
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const [name, data] of entries) {
    const nameBytes = enc.encode(name);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true); // UTF-8 names
    local.setUint16(8, 0, true);
    local.setUint16(10, 0, true);
    local.setUint16(12, 0x21, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, data.length, true);
    local.setUint32(22, data.length, true);
    local.setUint16(26, nameBytes.length, true);
    local.setUint16(28, 0, true);
    const cd = new DataView(new ArrayBuffer(46));
    cd.setUint32(0, 0x02014b50, true);
    cd.setUint16(4, 20, true);
    cd.setUint16(6, 20, true);
    cd.setUint16(8, 0x0800, true);
    cd.setUint16(10, 0, true);
    cd.setUint16(12, 0, true);
    cd.setUint16(14, 0x21, true);
    cd.setUint32(16, crc, true);
    cd.setUint32(20, data.length, true);
    cd.setUint32(24, data.length, true);
    cd.setUint16(28, nameBytes.length, true);
    cd.setUint32(42, offset, true);
    parts.push(new Uint8Array(local.buffer), nameBytes, data);
    central.push(new Uint8Array(cd.buffer), nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const cdSize = central.reduce((n, b) => n + b.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, cdSize, true);
  end.setUint32(16, offset, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(all.reduce((n, b) => n + b.length, 0));
  let at = 0;
  for (const b of all) {
    out.set(b, at);
    at += b.length;
  }
  return out;
}

export function toXlsx(p: PreparedExport): Uint8Array {
  const enc = new TextEncoder();
  return zipStored(xlsxEntries(p).map(([n, s]) => [n, enc.encode(s)] as [string, Uint8Array]));
}

// ------------------------------------------------------------------ browser glue

let rightsCache: { at: number; value: Promise<RightsRegistry | null> } | null = null;

/** GET /api/export/rights, cached for ten minutes; null (fail closed) when it cannot be read. */
export function loadRights(fetchImpl: typeof fetch = globalThis.fetch, now = Date.now()): Promise<RightsRegistry | null> {
  if (rightsCache && now - rightsCache.at < 600_000) return rightsCache.value;
  const value = fetchImpl('/api/export/rights', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
    .then((r) => (r.ok ? (r.json() as Promise<RightsRegistry>) : null))
    .catch(() => null);
  rightsCache = { at: now, value };
  return value;
}

export function resetRightsCache(): void {
  rightsCache = null;
}

export function exportFileName(title: string, ext: 'csv' | 'xlsx', at: Date): string {
  const base = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'export';
  return `${base}-${at.toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-')}.${ext}`;
}

export type ExportFormat = 'csv' | 'csv-semicolon' | 'xlsx';

/** Build, gate and download one export. Resolves with a one-line summary for the status line. */
export async function runExport<T>(table: ExportTable<T>, format: ExportFormat, deps: { fetchImpl?: typeof fetch; save?: (name: string, blob: Blob) => void } = {}): Promise<string> {
  const at = new Date();
  const reg = await loadRights(deps.fetchImpl);
  const delimiter = format === 'csv-semicolon' ? ';' : ',';
  const prep = prepareExport(table, reg, at, delimiter);
  const ext = format === 'xlsx' ? 'xlsx' : 'csv';
  const blob =
    format === 'xlsx'
      ? new Blob([toXlsx(prep) as BlobPart], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
      : new Blob([toCsv(prep, { delimiter })], { type: 'text/csv;charset=utf-8' });
  (deps.save ?? saveBlob)(exportFileName(table.title, ext, at), blob);
  const ex = prep.excluded.length ? `; ${prep.excluded.length} column(s) excluded by data rights: ${prep.excluded.map((e) => e.column).join(', ')}` : '';
  return `Exported ${table.rows.length} row(s) to ${ext.toUpperCase()}${ex}.`;
}

function saveBlob(name: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
