/** Stops the core and API this run started, by recorded PID only. */
import { readState, tearDown } from './support/backend';

export default async function globalTeardown(): Promise<void> {
  let home = '';
  try {
    home = readState().home;
  } catch {
    // setup never recorded a run
  }
  await tearDown();
  if (home) console.log(`[e2e] stopped the run's processes; logs kept in ${home}`);
}
