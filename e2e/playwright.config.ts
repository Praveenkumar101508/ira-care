import { defineConfig } from '@playwright/test';

/**
 * End-to-end browser tests of the whole IRA CARE journey against a running staging stack
 * (scripts/staging/clean_clone_proof.sh starts one and sets these variables).
 */
export default defineConfig({
  testDir: '.',
  timeout: 180_000,
  expect: { timeout: 30_000 },
  workers: 1,
  retries: 0,
  reporter: [['list'], ['json', { outputFile: 'results.json' }]],
  use: {
    trace: 'off',
    video: 'off',
    viewport: { width: 1280, height: 800 },
    actionTimeout: 20_000,
  },
});
