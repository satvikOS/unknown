/**
 * Starts and stops the real backend for the end-to-end suite: a fresh core (simulated broker, FIXTURE
 * market) and API on their own port and UNKNOWN_HOME, the simulated account bootstrapped through the
 * owner CLI, and the terminal built into the run directory (never into terminal/dist, which other
 * developers' API processes may be serving).
 *
 * Every process is recorded by PID in <home>/e2e-pids.json and stopped by that PID only; nothing
 * here kills by name or pattern.
 */
import { execFileSync, spawn } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const TERMINAL_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
export const REPO_DIR = resolve(TERMINAL_DIR, '..');
export const BACKEND_SRC = join(REPO_DIR, 'backend', 'src');
const HARNESS = join(TERMINAL_DIR, 'e2e', 'support', 'harness.py');

export const ACCOUNT = 'SIM-1';
export const DEPLOYMENT = 'mac-dev';
export const PASSPHRASE = 'correct horse battery staple';
/** The API's step-up window for this run (the production API uses 15 minutes; see harness.py). */
export const STEP_UP_WINDOW_S = 12;

export interface RunState {
  home: string;
  port: number;
  baseUrl: string;
  python: string;
  dist: string;
  pids: { core: number | null; api: number | null };
}

const STATE_ENV = 'UNKNOWN_E2E_STATE';

function python(): string {
  return process.env.UNKNOWN_PYTHON ?? 'python3';
}

function env(home: string): NodeJS.ProcessEnv {
  return { ...process.env, UNKNOWN_HOME: home, PYTHONPATH: BACKEND_SRC, PYTHONDONTWRITEBYTECODE: '1' };
}

export function stateFile(home: string): string {
  return join(home, 'e2e-pids.json');
}

export function readState(): RunState {
  const file = process.env[STATE_ENV];
  if (!file || !existsSync(file)) throw new Error('e2e run state not found: global setup did not run');
  return JSON.parse(readFileSync(file, 'utf8')) as RunState;
}

function writeState(s: RunState): void {
  writeFileSync(stateFile(s.home), JSON.stringify(s, null, 2));
  process.env[STATE_ENV] = stateFile(s.home);
}

function portFree(port: number): Promise<boolean> {
  return new Promise((res) => {
    const srv = createServer();
    srv.once('error', () => res(false));
    srv.listen(port, '127.0.0.1', () => srv.close(() => res(true)));
  });
}

/** E2E_API_PORT if set (must be free), else the first free port in 8800-8899 starting at 8853. */
async function choosePort(): Promise<number> {
  const fixed = process.env.E2E_API_PORT;
  if (fixed) {
    const p = Number(fixed);
    if (!(await portFree(p))) throw new Error(`E2E_API_PORT ${p} is in use`);
    return p;
  }
  for (let i = 0; i < 100; i++) {
    const p = 8800 + ((53 + i) % 100);
    if (await portFree(p)) return p;
  }
  throw new Error('no free port in 8800-8899');
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export function alive(pid: number | null): boolean {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function spawnLogged(home: string, logName: string, args: string[]): number {
  const fd = openSync(join(home, logName), 'a');
  try {
    const child = spawn(python(), args, { cwd: join(REPO_DIR, 'backend'), env: env(home), detached: true, stdio: ['ignore', fd, fd] });
    child.unref();
    if (!child.pid) throw new Error(`could not start ${args.join(' ')}`);
    return child.pid;
  } finally {
    closeSync(fd);
  }
}

function cli(home: string, ...args: string[]): string {
  return execFileSync(python(), ['-m', 'unknown.cli', ...args], { cwd: join(REPO_DIR, 'backend'), env: env(home), encoding: 'utf8' });
}

async function waitFor(what: string, check: () => Promise<boolean> | boolean, timeoutMs: number): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await check()) return;
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${what}`);
}

function coreArgs(home: string): string[] {
  return [HARNESS, 'core', '--broker-state', join(home, 'sim-broker.pickle'), '--',
    '--account', ACCOUNT, '--deployment', DEPLOYMENT, '--fixture-market'];
}

/** True once the core's socket answers a health query with the API token. */
async function coreAnswers(home: string): Promise<boolean> {
  try {
    const out = execFileSync(
      python(),
      ['-c', 'from unknown.service.paths import Paths; from unknown.service.ipc import CoreClient; p = Paths.default(); '
        + 'print("ok" if "broker_connected" in CoreClient(p.socket, p.token("api")).query("health") else "no")'],
      { cwd: join(REPO_DIR, 'backend'), env: env(home), encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
    );
    return out.trim() === 'ok';
  } catch {
    return false;
  }
}

async function startCore(s: RunState): Promise<void> {
  s.pids.core = spawnLogged(s.home, 'core.log', coreArgs(s.home));
  writeState(s);
  await waitFor('the core socket', async () => {
    if (!alive(s.pids.core)) throw new Error(`core exited; see ${join(s.home, 'core.log')}`);
    return coreAnswers(s.home);
  }, 30_000);
}

/** SIGTERM by recorded PID (a clean shutdown releases the writer lease), SIGKILL after 15 s. */
export async function stopPid(pid: number | null): Promise<void> {
  if (!alive(pid)) return;
  process.kill(pid!, 'SIGTERM');
  const until = Date.now() + 15_000;
  while (alive(pid) && Date.now() < until) await sleep(100);
  if (alive(pid)) process.kill(pid!, 'SIGKILL');
}

/** Restart the core process the suite started (by its recorded PID). */
export async function restartCore(): Promise<{ oldPid: number | null; newPid: number | null }> {
  const s = readState();
  const oldPid = s.pids.core;
  await stopPid(oldPid);
  s.pids.core = null;
  writeState(s);
  await startCore(s);
  return { oldPid, newPid: s.pids.core };
}

export async function setUp(): Promise<RunState> {
  const root = process.env.UNKNOWN_E2E_ROOT ?? join(tmpdir(), 'unknown-term-e2e');
  const home = join(root, `run-${new Date().toISOString().replace(/[:.]/g, '-')}-${process.pid}`);
  mkdirSync(home, { recursive: true });
  const port = await choosePort();
  const s: RunState = {
    home,
    port,
    baseUrl: `http://127.0.0.1:${port}`,
    python: python(),
    dist: join(home, 'dist'),
    pids: { core: null, api: null },
  };
  writeState(s);

  // Build the terminal into the run directory (typecheck included).
  execFileSync('npx', ['tsc', '--noEmit', '-p', 'tsconfig.json'], { cwd: TERMINAL_DIR, stdio: 'inherit' });
  execFileSync('npx', ['vite', 'build', '--outDir', s.dist, '--emptyOutDir', '--logLevel', 'warn'], { cwd: TERMINAL_DIR, stdio: 'inherit' });

  cli(home, 'init');
  execFileSync(
    python(),
    ['-c', 'import os,sys; from pathlib import Path; from unknown.api.auth import write_owner_record; '
      + "write_owner_record(Path(os.environ['UNKNOWN_HOME'])/'secrets'/'owner.json', sys.argv[1])", PASSPHRASE],
    { cwd: join(REPO_DIR, 'backend'), env: env(home) },
  );
  await startCore(s);
  writeFileSync(join(home, 'bootstrap.log'), cli(home, 'bootstrap-sim', '--account', ACCOUNT, '--deployment', DEPLOYMENT));

  s.pids.api = spawnLogged(home, 'api.log', [HARNESS, 'api', '--step-up-window-s', String(STEP_UP_WINDOW_S), '--',
    '--account', ACCOUNT, '--port', String(port), '--static', s.dist]);
  writeState(s);
  await waitFor('the API', async () => {
    if (!alive(s.pids.api)) throw new Error(`API exited; see ${join(home, 'api.log')}`);
    try {
      const r = await fetch(`${s.baseUrl}/api/auth/session`);
      return r.ok;
    } catch {
      return false;
    }
  }, 30_000);
  return s;
}

export async function tearDown(): Promise<void> {
  let s: RunState;
  try {
    s = readState();
  } catch {
    return;
  }
  await stopPid(s.pids.api);
  await stopPid(s.pids.core);
  s.pids = { core: null, api: null };
  writeState(s);
}
