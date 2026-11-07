/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { configDefaults } from 'vitest/config';
import { defineConfig } from 'vite';

// The dev server proxies /api to the FastAPI engine (see `make dev`), so the
// browser origin never needs CORS in development. The E2E run may point the
// proxy at a different engine port (E2E_API_ORIGIN, playwright.config.ts).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The sandbox preview gateway fronts the dev server with an ephemeral
    // hostname (e.g. *.fcapp.run), so allow any host in development.
    // Production is served by nginx (frontend/web/nginx.conf), unaffected.
    allowedHosts: true,
    proxy: {
      '/api': {
        target: process.env.E2E_API_ORIGIN ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    // the browser-level specs belong to Playwright (npm run test:e2e),
    // not to the jsdom component suite.
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
});
