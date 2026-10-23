/**
 * Function registry and command line grammar: `<SECURITY> <FUNCTION> <GO>`.
 *
 * A function is a mnemonic (GP, DES, MON, ...) that opens a workspace, optionally for a security. Other branches
 * register their own with registerFunction(); every registered workspace is also reachable by its id (ORDERS, RISK).
 * The command line never executes trading actions: a function only opens a view; commands with effects run from
 * their own authenticated forms. Nothing here knows about a broker or a data source.
 */
import { parseSecurityKey } from './link';

export interface TerminalFunction {
  /** Upper-case letters and digits, 1-12 characters. */
  mnemonic: string;
  /** Workspace id the function opens (it may not be installed in this build; the command line says so). */
  workspace: string;
  /** True when the function needs a security (typed before it, or the panel's linked security). */
  needsSecurity: boolean;
  /** One line shown in autocomplete and HELP. */
  help: string;
  /** Route segments under the workspace, e.g. ['ts'] for a time-and-sales view. */
  rest?: readonly string[];
}

const MNEMONIC_RE = /^[A-Z][A-Z0-9]{0,11}$/;

/** Built-in mnemonics. Their workspaces are registered by the branches that build them ('chart', 'monitor'). */
export const BUILTIN_FUNCTIONS: readonly TerminalFunction[] = [
  { mnemonic: 'GP', workspace: 'chart', needsSecurity: true, help: 'Price chart for the security' },
  { mnemonic: 'DES', workspace: 'company', needsSecurity: true, help: 'Company description, filings and fundamentals' },
  { mnemonic: 'MON', workspace: 'monitor', needsSecurity: false, help: 'Quote monitor (watchlist of live quotes)' },
  { mnemonic: 'TS', workspace: 'monitor', needsSecurity: true, help: 'Time and sales (trade prints) for the security', rest: ['ts'] },
  { mnemonic: 'DEPTH', workspace: 'monitor', needsSecurity: true, help: 'Market depth (order book) for the security', rest: ['depth'] },
  { mnemonic: 'WS', workspace: 'workstation', needsSecurity: false, help: 'Multi-panel workstation' },
];

const registered = new Map<string, TerminalFunction>();
const listeners = new Set<() => void>();
let version = 0;

function changed(): void {
  version += 1;
  for (const l of [...listeners]) l();
}

/** Register (or replace) a function; returns a function that removes it again. Throws on a malformed mnemonic. */
export function registerFunction(fn: TerminalFunction): () => void {
  const mnemonic = fn.mnemonic.toUpperCase();
  if (!MNEMONIC_RE.test(mnemonic)) throw new Error(`function mnemonic ${JSON.stringify(fn.mnemonic)} must be 1-12 letters or digits`);
  const entry = { ...fn, mnemonic };
  registered.set(mnemonic, entry);
  changed();
  return () => {
    if (registered.get(mnemonic) === entry) {
      registered.delete(mnemonic);
      changed();
    }
  };
}

export function subscribeFunctions(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function functionsVersion(): number {
  return version;
}

export interface WorkspaceRef {
  id: string;
  title: string;
  description?: string;
}

/**
 * Every function: built-ins, then registered ones (which replace a built-in of the same mnemonic), then one per
 * installed workspace under its upper-cased id (unless a mnemonic of that name already exists).
 */
export function listFunctions(workspaces: readonly WorkspaceRef[]): TerminalFunction[] {
  const out = new Map<string, TerminalFunction>();
  for (const f of BUILTIN_FUNCTIONS) out.set(f.mnemonic, f);
  for (const f of registered.values()) out.set(f.mnemonic, f);
  for (const w of workspaces) {
    const m = w.id.toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (MNEMONIC_RE.test(m) && !out.has(m)) out.set(m, { mnemonic: m, workspace: w.id, needsSecurity: false, help: w.description ?? w.title });
  }
  return [...out.values()];
}

export type ParsedCommand =
  | { kind: 'empty' }
  | { kind: 'help' }
  | { kind: 'function'; fn: TerminalFunction; security: string | null }
  | { kind: 'security'; security: string }
  | { kind: 'unknown'; text: string; reason: string };

/** A token that names a security: `<SYMBOL>.<MIC>` (MIC of four characters starting with X) or a core id. */
export function looksLikeSecurity(token: string): boolean {
  return parseSecurityKey(token).mic !== null;
}

/** Strip a trailing `<GO>` / `GO` and split into upper-cased tokens. */
export function tokenize(text: string): string[] {
  const t = text.trim().replace(/<\s*GO\s*>$/i, '').trim();
  if (!t) return [];
  const tokens = t.split(/\s+/).map((x) => x.toUpperCase());
  if (tokens.length > 1 && tokens[tokens.length - 1] === 'GO') tokens.pop();
  return tokens;
}

/**
 * Parse a command line. Accepted forms: `FN`, `SECURITY FN`, `SECURITY` (load the security on the channel),
 * `HELP`. A security may be a `<SYMBOL>.<MIC>` key or, when followed by a function, any core instrument id.
 */
export function parseCommand(text: string, functions: readonly TerminalFunction[]): ParsedCommand {
  const tokens = tokenize(text);
  if (tokens.length === 0) return { kind: 'empty' };
  const find = (m: string) => functions.find((f) => f.mnemonic === m);
  if (tokens.length === 1) {
    const [t] = tokens as [string];
    if (t === 'HELP' || t === '?') return { kind: 'help' };
    const fn = find(t);
    if (fn) return { kind: 'function', fn, security: null };
    if (looksLikeSecurity(t)) return { kind: 'security', security: t };
    return { kind: 'unknown', text: t, reason: `Unknown function ${t}. Type HELP for the list of functions.` };
  }
  if (tokens.length === 2) {
    const [sec, m] = tokens as [string, string];
    const fn = find(m);
    if (!fn) {
      if (find(sec)) return { kind: 'unknown', text: m, reason: `Type the security first, then the function: ${m} ${sec}.` };
      return { kind: 'unknown', text: m, reason: `Unknown function ${m}. Type HELP for the list of functions.` };
    }
    return { kind: 'function', fn, security: sec };
  }
  return { kind: 'unknown', text: tokens.join(' '), reason: 'Too many words: type <SECURITY> <FUNCTION>, for example AAPL.XNAS GP.' };
}

/** Autocomplete for the token being typed: functions whose mnemonic starts with it, then those whose help matches. */
export function suggestFunctions(text: string, functions: readonly TerminalFunction[], limit = 8): TerminalFunction[] {
  const tokens = text.trim().toUpperCase().split(/\s+/).filter(Boolean);
  const last = tokens[tokens.length - 1] ?? '';
  if (!last || text.endsWith(' ')) return text.trim() && looksLikeSecurity(last) ? functions.filter((f) => f.needsSecurity).slice(0, limit) : [];
  const prefix = functions.filter((f) => f.mnemonic.startsWith(last));
  const words = last.length >= 2 ? functions.filter((f) => !f.mnemonic.startsWith(last) && f.help.toUpperCase().includes(last)) : [];
  return [...prefix, ...words].slice(0, limit);
}

/** Replace the token being typed with the chosen mnemonic. */
export function applySuggestion(text: string, mnemonic: string): string {
  const trimmed = text.replace(/\s+$/, '');
  if (text.endsWith(' ') || !trimmed) return `${trimmed ? `${trimmed} ` : ''}${mnemonic}`;
  const i = trimmed.lastIndexOf(' ');
  return `${i < 0 ? '' : trimmed.slice(0, i + 1)}${mnemonic}`;
}

/** Command history: newest last, no consecutive duplicates, capped. */
export function pushHistory(history: readonly string[], entry: string, cap = 50): string[] {
  const e = entry.trim();
  if (!e) return [...history];
  const out = history.filter((h) => h !== e);
  out.push(e);
  return out.slice(-cap);
}

const HISTORY_KEY = 'unknown.terminal.cmd-history.v1';

export function loadHistory(storage: Pick<Storage, 'getItem'> | null = safeStorage()): string[] {
  try {
    const raw = storage?.getItem(HISTORY_KEY);
    const v: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string').slice(-50) : [];
  } catch {
    return [];
  }
}

export function saveHistory(history: readonly string[], storage: Pick<Storage, 'setItem'> | null = safeStorage()): void {
  try {
    storage?.setItem(HISTORY_KEY, JSON.stringify(history.slice(-50)));
  } catch {
    // storage unavailable: history lasts for this page only
  }
}

export function safeStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Where a command lands: the active workstation panel, or the single workspace on screen. The shell installs a
 * default target (navigate the hash route on channel 'default'); the workstation replaces it while it is open.
 */
export interface CommandTarget {
  /** Link channel the security is published on. */
  channel: string;
  /** Short description for messages, e.g. "panel 2" or "this window". */
  label: string;
  /** Open the workspace; returns null on success or a plain reason it could not (e.g. the layout is locked). */
  open: (workspace: string, rest: readonly string[]) => string | null;
}

const targets: CommandTarget[] = [];
let fallbackTarget: CommandTarget | null = null;
const targetListeners = new Set<() => void>();

function targetsChanged(): void {
  for (const l of [...targetListeners]) l();
}

/** The shell's own target (single workspace on screen), used when no workstation target is pushed. */
export function setDefaultCommandTarget(t: CommandTarget | null): void {
  fallbackTarget = t;
  targetsChanged();
}

/** Push a target; the most recent one wins until its returned remover runs. */
export function pushCommandTarget(t: CommandTarget): () => void {
  targets.push(t);
  targetsChanged();
  return () => {
    const i = targets.lastIndexOf(t);
    if (i >= 0) targets.splice(i, 1);
    targetsChanged();
  };
}

export function commandTarget(): CommandTarget | null {
  return targets[targets.length - 1] ?? fallbackTarget;
}

export function subscribeCommandTarget(l: () => void): () => void {
  targetListeners.add(l);
  return () => targetListeners.delete(l);
}

export interface CommandOutcome {
  ok: boolean;
  message: string;
  /** Set for HELP: the shell shows the function list. */
  showHelp?: boolean;
}

export interface CommandDeps {
  installed: (workspace: string) => boolean;
  target: CommandTarget | null;
  linked: (channel: string) => string | null;
  setLinked: (key: string, channel: string) => void;
}

/** Run a parsed command: publish the security on the target's channel, then open the function's workspace. */
export function executeCommand(cmd: ParsedCommand, deps: CommandDeps): CommandOutcome {
  const t = deps.target;
  switch (cmd.kind) {
    case 'empty':
      return { ok: false, message: 'Type a function (for example MON), or a security and a function (AAPL.XNAS GP), then Enter.' };
    case 'help':
      return { ok: true, message: 'Function list opened.', showHelp: true };
    case 'unknown':
      return { ok: false, message: cmd.reason };
    case 'security':
      if (!t) return { ok: false, message: 'No panel is ready to receive the security.' };
      deps.setLinked(cmd.security, t.channel);
      return { ok: true, message: `${cmd.security} loaded on ${t.label}.` };
    case 'function': {
      const { fn } = cmd;
      if (!deps.installed(fn.workspace)) {
        return { ok: false, message: `${fn.mnemonic} is a known function, but its workspace (${fn.workspace}) is not installed in this build.` };
      }
      if (!t) return { ok: false, message: 'No panel is ready to open the function.' };
      const security = cmd.security ?? deps.linked(t.channel);
      if (fn.needsSecurity && !security) {
        return { ok: false, message: `${fn.mnemonic} needs a security: type it first, for example AAPL.XNAS ${fn.mnemonic}.` };
      }
      if (cmd.security) deps.setLinked(cmd.security, t.channel);
      const refused = t.open(fn.workspace, fn.rest ?? []);
      if (refused) return { ok: false, message: refused };
      return { ok: true, message: `${fn.mnemonic}${fn.needsSecurity && security ? ` ${security}` : ''} opened on ${t.label}.` };
    }
  }
}
