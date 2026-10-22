/**
 * Backend steps for the portfolio, market-data and research-job scenarios (03-portfolio-agents.spec.ts),
 * run against the suite's own UNKNOWN_HOME. Every process here is short-lived and synchronous (it
 * exits before the call returns, with a timeout), so none outlives the step that started it.
 *
 * No provider key ever reaches these processes: every *API_KEY* variable is removed from their
 * environment, the owner policy written here disables the only provider, and config/models/approved.json
 * approves no model. The model worker therefore refuses jobs before any network call.
 */
import { execFileSync } from 'node:child_process';
import { chmodSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { ACCOUNT, BACKEND_SRC, REPO_DIR, TERMINAL_DIR, readState } from './backend';

const HARNESS = join(TERMINAL_DIR, 'e2e', 'support', 'harness.py');

/** The per-job cap in the owner policy this suite writes (USD). */
export const E2E_PER_JOB_CAP = '0.50';

function backendEnv(home: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env, UNKNOWN_HOME: home, PYTHONPATH: BACKEND_SRC, PYTHONDONTWRITEBYTECODE: '1' };
  for (const k of Object.keys(env)) if (/API_KEY/i.test(k)) delete env[k];
  return env;
}

function py(args: string[], timeoutMs = 60_000): string {
  const s = readState();
  return execFileSync(s.python, args, { cwd: join(REPO_DIR, 'backend'), env: backendEnv(s.home), encoding: 'utf8', timeout: timeoutMs, stdio: ['ignore', 'pipe', 'pipe'] });
}

/** Writes a FIXTURE IBKR market-data status file with the real session code (harness.py md-status). */
export function writeMarketDataStatus(ageS = 0): string {
  return py([HARNESS, 'md-status', '--age-s', String(ageS)]);
}

/** Records one end-of-day valuation snapshot with the owner's CLI (unknown.analytics.snapshots record). */
export function recordSnapshot(): string {
  return py(['-m', 'unknown.analytics.snapshots', 'record', '--account', ACCOUNT]);
}

/**
 * The owner policy (<home>/config/agents.json, mode 600) with positive caps, so the API queues jobs,
 * and the anthropic provider DISABLED, so the worker refuses every job before any network call.
 */
export function writeOwnerPolicyProviderDisabled(): void {
  const dir = join(readState().home, 'config');
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const file = join(dir, 'agents.json');
  const doc = {
    schema_version: 1,
    comment: 'end-to-end suite: provider disabled; no key; no model approved',
    currency: 'USD',
    caps: { per_day: '1.00', per_job: E2E_PER_JOB_CAP, lifetime: '1.00' },
    providers: {
      anthropic: {
        enabled: false,
        api_key_env: 'UNKNOWN_E2E_NO_KEY',
        region: 'global',
        account: { training_permission_granted: false, zdr_enabled: false },
      },
    },
    concurrency: { global: 1, per_role: { research: 1 } },
  };
  writeFileSync(file, JSON.stringify(doc, null, 2), { mode: 0o600 });
  chmodSync(file, 0o600);
}

/** Runs the real model worker until nothing is queued (`--once`). With the policy above it fails each job. */
export function runWorkerOnce(): string {
  return py(['-m', 'unknown.agents.worker', '--once'], 120_000);
}
