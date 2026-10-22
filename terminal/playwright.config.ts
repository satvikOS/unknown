/**
 * End-to-end suite: the built terminal against a real core (simulated broker, FIXTURE market) and
 * API started fresh for the run. `npm run test:e2e`. See e2e/README.md.
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  // One backend, one account: scenarios build on each other, so they run in order in one worker.
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['json', { outputFile: 'e2e/.results/results.json' }]],
  outputDir: 'e2e/.results/artifacts',
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',
  use: {
    ...devices['Desktop Chrome'],
    viewport: { width: 1440, height: 900 },
    colorScheme: 'dark',
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: { args: ['--disable-audio-output', '--mute-audio'] },
  },
});
