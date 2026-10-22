/** Starts a fresh core + API for the run and builds the terminal (see support/backend.ts). */
import { setUp } from './support/backend';

export default async function globalSetup(): Promise<void> {
  const s = await setUp();
  process.env.E2E_BASE_URL = s.baseUrl;
  console.log(`[e2e] core pid ${s.pids.core}, api pid ${s.pids.api} on ${s.baseUrl}; home ${s.home}`);
}
