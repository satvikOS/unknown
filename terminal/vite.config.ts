import { defineConfig } from 'vitest/config';

// Node's `process` without pulling @types/node into the dependency set.
declare const process: { env: Record<string, string | undefined> };

/** The unknown-api process to proxy to in development (ADR-0013). Override with UNKNOWN_API. */
const apiTarget = process.env.UNKNOWN_API ?? 'http://127.0.0.1:8765';
const devPort = Number(process.env.TERMINAL_PORT ?? '5173');

const proxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: false,
    // Server-Sent Events: never buffer or time out the long-lived /api/stream response.
    timeout: 0,
    proxyTimeout: 0,
  },
};

export default defineConfig({
  server: { host: '127.0.0.1', port: devPort, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: devPort + 1, strictPort: true, proxy },
  build: {
    target: 'es2022',
    // The API's Content-Security-Policy forbids inline scripts.
    modulePreload: { polyfill: false },
    sourcemap: true,
    outDir: 'dist',
    emptyOutDir: true,
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
