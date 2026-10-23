import { describe, expect, it } from 'vitest';
import {
  applySuggestion,
  executeCommand,
  listFunctions,
  loadHistory,
  parseCommand,
  pushHistory,
  registerFunction,
  saveHistory,
  suggestFunctions,
  type CommandTarget,
} from './functions';

const WS = [
  { id: 'overview', title: 'Overview' },
  { id: 'company', title: 'Company' },
  { id: 'research-jobs', title: 'Research jobs' },
];

function harness(installed: string[], refuse: string | null = null) {
  const linked = new Map<string, string>();
  const opened: [string, readonly string[]][] = [];
  const target: CommandTarget = {
    channel: 'ch2',
    label: 'panel 2',
    open: (w, r) => {
      if (refuse) return refuse;
      opened.push([w, r]);
      return null;
    },
  };
  const deps = {
    installed: (w: string) => installed.includes(w),
    target,
    linked: (ch: string) => linked.get(ch) ?? null,
    setLinked: (k: string, ch: string) => void linked.set(ch, k),
  };
  return { linked, opened, deps };
}

describe('command line grammar', () => {
  const fns = listFunctions(WS);

  it('parses SECURITY FUNCTION GO, a bare function, a bare security and HELP', () => {
    expect(parseCommand('aapl.xnas gp <GO>', fns)).toMatchObject({ kind: 'function', fn: { mnemonic: 'GP', workspace: 'chart' }, security: 'AAPL.XNAS' });
    expect(parseCommand('MON', fns)).toMatchObject({ kind: 'function', fn: { workspace: 'monitor' }, security: null });
    expect(parseCommand('RELIANCE.XNSE', fns)).toEqual({ kind: 'security', security: 'RELIANCE.XNSE' });
    expect(parseCommand('help', fns)).toEqual({ kind: 'help' });
    expect(parseCommand('  ', fns)).toEqual({ kind: 'empty' });
  });

  it('says plainly when a function is unknown or the words are in the wrong order', () => {
    expect(parseCommand('XYZZY', fns)).toMatchObject({ kind: 'unknown', reason: expect.stringMatching(/^Unknown function XYZZY/) });
    expect(parseCommand('GP AAPL.XNAS', fns)).toMatchObject({ kind: 'unknown', reason: expect.stringMatching(/security first/) });
    expect(parseCommand('A B C', fns)).toMatchObject({ kind: 'unknown' });
  });

  it('derives a function for every installed workspace without shadowing a built-in mnemonic', () => {
    const m = fns.map((f) => f.mnemonic);
    expect(m).toEqual(expect.arrayContaining(['GP', 'DES', 'MON', 'TS', 'DEPTH', 'OVERVIEW', 'RESEARCHJOBS']));
    expect(fns.find((f) => f.mnemonic === 'DES')?.workspace).toBe('company');
  });

  it('lets another branch register or replace a function and remove it again', () => {
    const off = registerFunction({ mnemonic: 'ts', workspace: 'timesales', needsSecurity: true, help: 'Time and sales' });
    expect(listFunctions(WS).find((f) => f.mnemonic === 'TS')?.workspace).toBe('timesales');
    off();
    expect(listFunctions(WS).find((f) => f.mnemonic === 'TS')?.workspace).toBe('monitor');
    expect(() => registerFunction({ mnemonic: 'bad one', workspace: 'x', needsSecurity: false, help: '' })).toThrow(/mnemonic/);
  });

  it('autocompletes by prefix, then by help text, and replaces only the token being typed', () => {
    expect(suggestFunctions('AAPL.XNAS G', fns).map((f) => f.mnemonic)[0]).toBe('GP');
    expect(suggestFunctions('AAPL.XNAS ', fns).every((f) => f.needsSecurity)).toBe(true);
    expect(suggestFunctions('depth', fns).map((f) => f.mnemonic)).toContain('DEPTH');
    expect(applySuggestion('AAPL.XNAS G', 'GP')).toBe('AAPL.XNAS GP');
    expect(applySuggestion('AAPL.XNAS ', 'DES')).toBe('AAPL.XNAS DES');
  });

  it('keeps a capped history without duplicates and survives broken storage', () => {
    expect(pushHistory(['MON', 'GP'], 'MON')).toEqual(['GP', 'MON']);
    expect(pushHistory([], '  ')).toEqual([]);
    expect(pushHistory(Array.from({ length: 60 }, (_, i) => `F${i}`), 'X')).toHaveLength(50);
    const broken = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('quota');
      },
    };
    expect(loadHistory(broken)).toEqual([]);
    expect(() => saveHistory(['MON'], broken)).not.toThrow();
    expect(loadHistory({ getItem: () => '{"not":"a list"}' })).toEqual([]);
  });
});

describe('command execution', () => {
  const fns = listFunctions(WS);

  it('publishes the security on the target channel and opens the function there', () => {
    const h = harness(['chart']);
    const out = executeCommand(parseCommand('AAPL.XNAS GP', fns), h.deps);
    expect(out).toMatchObject({ ok: true, message: 'GP AAPL.XNAS opened on panel 2.' });
    expect(h.linked.get('ch2')).toBe('AAPL.XNAS');
    expect(h.opened).toEqual([['chart', []]]);
  });

  it('uses the channel security when none is typed, and refuses a security function without one', () => {
    const h = harness(['monitor']);
    expect(executeCommand(parseCommand('TS', fns), h.deps)).toMatchObject({ ok: false, message: expect.stringMatching(/TS needs a security/) });
    h.linked.set('ch2', 'SAP.XETR');
    expect(executeCommand(parseCommand('DEPTH', fns), h.deps)).toMatchObject({ ok: true, message: 'DEPTH SAP.XETR opened on panel 2.' });
    expect(h.opened).toEqual([['monitor', ['depth']]]);
  });

  it('says a known function is not installed instead of opening nothing', () => {
    const h = harness([]);
    expect(executeCommand(parseCommand('AAPL.XNAS GP', fns), h.deps)).toMatchObject({ ok: false, message: expect.stringMatching(/not installed in this build/) });
    expect(h.linked.size).toBe(0);
  });

  it('reports the target refusing (a locked layout) and opens HELP on request', () => {
    const h = harness(['monitor'], 'The layout is locked.');
    expect(executeCommand(parseCommand('MON', fns), h.deps)).toEqual({ ok: false, message: 'The layout is locked.' });
    expect(executeCommand(parseCommand('HELP', fns), h.deps)).toMatchObject({ ok: true, showHelp: true });
  });
});
