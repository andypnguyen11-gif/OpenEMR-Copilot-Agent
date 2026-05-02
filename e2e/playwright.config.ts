import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  // Each chat turn waits on a real LLM call (Sonnet via Anthropic) plus
  // an optional schema-retry. 25-30s per turn is normal; tests with
  // multiple turns need headroom.
  timeout: 120_000,
  expect: { timeout: 30_000 },
  // OpenEMR session state is single-user (admin). Parallel tabs would
  // race on the CSRF token rotation. Keep workers=1 until we wire
  // per-worker auth.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://localhost:8300',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'setup',
      testMatch: /.*\.setup\.ts/,
    },
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        storageState: '.auth/admin.json',
      },
      dependencies: ['setup'],
    },
  ],
});
