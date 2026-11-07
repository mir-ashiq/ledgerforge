/**
 * Browser-level E2E (v0.16): the full keyboard flow in real Chromium —
 * the jsdom suite proves components, this proves the assembled product
 * (vite proxy → FastAPI engine → PostgreSQL) with zero mouse input.
 *
 * Prerequisites (see the CI `e2e` job / `make e2e`):
 *   1. a fresh lf_e2e database  — python scripts/e2e_setup_db.py
 *   2. the engine + web servers are started for you by `webServer` below
 *      (overridable: E2E_API_COMMAND, E2E_DATABASE_URL, E2E_API_ORIGIN,
 *      E2E_WEB_COMMAND; the vite proxy follows E2E_API_ORIGIN).
 */

import { defineConfig } from '@playwright/test';

const API_PORT = process.env.E2E_API_PORT ?? '8000';
const API_ORIGIN = process.env.E2E_API_ORIGIN ?? `http://localhost:${API_PORT}`;
const DATABASE_URL =
  process.env.E2E_DATABASE_URL ?? 'postgresql+psycopg://postgres:postgres@localhost:5432/lf_e2e';

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false, // one book, one voucher sequence — order is the story
  workers: 1,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: [
    {
      command: process.env.E2E_API_COMMAND ?? 'uvicorn ledgerforge.main:app --port 8000',
      url: `${API_ORIGIN}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        LF_DATABASE_URL: DATABASE_URL,
        LF_AUTH_MODE: 'disabled', // the E2E book is local-only scratch data
      },
    },
    {
      command: process.env.E2E_WEB_COMMAND ?? 'npm run dev',
      url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: { E2E_API_ORIGIN: API_ORIGIN },
    },
  ],
});
