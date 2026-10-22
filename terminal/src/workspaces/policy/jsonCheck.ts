/**
 * Client-side JSON validation for the policy editor. This checks JSON only; the core validates the
 * policy schema and its message is shown when it rejects a body.
 *
 * Beyond syntax (with line and column), it refuses two things JSON.parse accepts silently but which
 * would send the core something other than what the owner typed:
 *   - a duplicate key in one object (JSON.parse keeps the last one and drops the rest);
 *   - a number literal that does not survive the round trip to the wire unchanged ("10.0" would be
 *     sent as 10, "300.10" as 300.1, and long digit strings lose precision). Money must be a string.
 */

export interface JsonIssue {
  message: string;
  /** 1-based. */
  line: number;
  /** 1-based, in UTF-16 code units. */
  column: number;
  offset: number;
}

export interface JsonCheck {
  ok: boolean;
  /** The parsed value when there are no issues. */
  value?: unknown;
  issues: JsonIssue[];
}

export function lineColumn(text: string, offset: number): { line: number; column: number } {
  let line = 1;
  let lastBreak = -1;
  for (let i = 0; i < offset && i < text.length; i++) {
    if (text.charCodeAt(i) === 10) {
      line += 1;
      lastBreak = i;
    }
  }
  return { line, column: offset - lastBreak };
}

class Stop extends Error {}

const NUMBER_RE = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;

/** Strict JSON parser that records issues with positions. */
class Parser {
  private i = 0;
  readonly issues: JsonIssue[] = [];

  constructor(private readonly text: string) {}

  private issue(message: string, offset = this.i): void {
    this.issues.push({ message, offset, ...lineColumn(this.text, offset) });
  }

  private fail(message: string, offset = this.i): never {
    this.issue(message, offset);
    throw new Stop(message);
  }

  private ws(): void {
    while (this.i < this.text.length && ' \t\n\r'.includes(this.text[this.i] as string)) this.i++;
  }

  private describe(): string {
    if (this.i >= this.text.length) return 'end of input';
    return `“${this.text[this.i]}”`;
  }

  parseDocument(): unknown {
    this.ws();
    if (this.i >= this.text.length) this.fail('The document is empty; a policy is a JSON object.');
    const start = this.i;
    const v = this.value();
    this.ws();
    if (this.i < this.text.length) this.fail(`Unexpected ${this.describe()} after the end of the JSON value.`);
    if (typeof v !== 'object' || v === null || Array.isArray(v)) this.fail('A policy must be a JSON object ({ … }).', start);
    return v;
  }

  private value(): unknown {
    this.ws();
    const c = this.text[this.i];
    if (c === '{') return this.object();
    if (c === '[') return this.array();
    if (c === '"') return this.string();
    if (c === '-' || (c !== undefined && c >= '0' && c <= '9')) return this.number();
    for (const [word, v] of [
      ['true', true],
      ['false', false],
      ['null', null],
    ] as const) {
      if (this.text.startsWith(word, this.i)) {
        this.i += word.length;
        return v;
      }
    }
    return this.fail(`Unexpected ${this.describe()}; expected a value.`);
  }

  private object(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    const seen = new Set<string>();
    this.i++; // {
    this.ws();
    if (this.text[this.i] === '}') {
      this.i++;
      return out;
    }
    for (;;) {
      this.ws();
      if (this.text[this.i] !== '"') this.fail(`Unexpected ${this.describe()}; expected a quoted key.`);
      const keyAt = this.i;
      const key = this.string();
      if (seen.has(key)) this.issue(`Duplicate key “${key}”: JSON keeps only the last value, so the earlier one would be lost.`, keyAt);
      seen.add(key);
      this.ws();
      if (this.text[this.i] !== ':') this.fail(`Unexpected ${this.describe()}; expected “:” after the key.`);
      this.i++;
      Object.defineProperty(out, key, { value: this.value(), enumerable: true, writable: true, configurable: true });
      this.ws();
      const c = this.text[this.i];
      if (c === ',') {
        this.i++;
        this.ws();
        if (this.text[this.i] === '}') this.fail('Trailing comma before “}”.');
        continue;
      }
      if (c === '}') {
        this.i++;
        return out;
      }
      this.fail(`Unexpected ${this.describe()}; expected “,” or “}”.`);
    }
  }

  private array(): unknown[] {
    const out: unknown[] = [];
    this.i++; // [
    this.ws();
    if (this.text[this.i] === ']') {
      this.i++;
      return out;
    }
    for (;;) {
      out.push(this.value());
      this.ws();
      const c = this.text[this.i];
      if (c === ',') {
        this.i++;
        this.ws();
        if (this.text[this.i] === ']') this.fail('Trailing comma before “]”.');
        continue;
      }
      if (c === ']') {
        this.i++;
        return out;
      }
      this.fail(`Unexpected ${this.describe()}; expected “,” or “]”.`);
    }
  }

  private string(): string {
    const start = this.i;
    this.i++; // opening quote
    let out = '';
    for (;;) {
      if (this.i >= this.text.length) this.fail('Unterminated string.', start);
      const c = this.text[this.i] as string;
      const code = c.charCodeAt(0);
      if (c === '"') {
        this.i++;
        return out;
      }
      if (code < 0x20) this.fail('Control character in a string; use an escape such as \\n.');
      if (c === '\\') {
        const e = this.text[this.i + 1];
        const simple: Record<string, string> = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t' };
        if (e !== undefined && e in simple) {
          out += simple[e];
          this.i += 2;
          continue;
        }
        if (e === 'u' && /^[0-9a-fA-F]{4}$/.test(this.text.slice(this.i + 2, this.i + 6))) {
          out += String.fromCharCode(parseInt(this.text.slice(this.i + 2, this.i + 6), 16));
          this.i += 6;
          continue;
        }
        this.fail('Invalid escape sequence in a string.');
      }
      out += c;
      this.i++;
    }
  }

  private number(): number {
    NUMBER_RE.lastIndex = this.i;
    const m = NUMBER_RE.exec(this.text);
    if (!m) return this.fail(`Invalid number starting at ${this.describe()}.`);
    const literal = m[0];
    const at = this.i;
    this.i += literal.length;
    const value = Number(literal);
    const wire = JSON.stringify(value);
    if (wire !== literal) {
      this.issue(`The number ${literal} would be sent as ${wire}. Write it as ${wire} if that is meant` + '; amounts must be decimal strings in quotes.', at);
    }
    return value;
  }
}

/** Validate editor text as a JSON object, reporting every problem found with its position. */
export function checkPolicyJson(text: string): JsonCheck {
  const p = new Parser(text);
  let value: unknown;
  try {
    value = p.parseDocument();
  } catch (err) {
    if (!(err instanceof Stop)) throw err;
    return { ok: false, issues: p.issues };
  }
  if (p.issues.length) return { ok: false, issues: p.issues };
  // Derive twice: the platform parser must agree with this one.
  try {
    JSON.parse(text);
  } catch (err) {
    return { ok: false, issues: [{ message: `JSON.parse refused the text: ${String(err)}`, line: 1, column: 1, offset: 0 }] };
  }
  return { ok: true, value, issues: [] };
}

/** Two-space pretty print, as the editor seeds it. */
export function prettyJson(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}
