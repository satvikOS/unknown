import type { Plugin } from 'vite';
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

/**
 * Budget for the JavaScript a first visit loads: the entry chunk and every chunk it imports statically. Each
 * workspace is a dynamic import (src/workspaces/lazy.tsx), so this is the shell alone: 311 kB on 2026-10-22,
 * against 938 kB as one bundle before the split. The build fails above the budget instead of printing Vite's
 * 500 kB chunk warning; load new code on demand, or raise the budget deliberately.
 */
const INITIAL_JS_BUDGET_KB = 400;

function initialBundleBudget(limitKb: number): Plugin {
  return {
    name: 'unknown-initial-bundle-budget',
    apply: 'build',
    writeBundle(_options, bundle) {
      const chunks = new Map<string, { code: string; imports: string[]; isEntry: boolean }>();
      for (const out of Object.values(bundle)) if (out.type === 'chunk') chunks.set(out.fileName, out);
      const encoder = new TextEncoder();
      for (const [name, entry] of chunks) {
        if (!entry.isEntry) continue;
        const seen = new Set<string>();
        const stack = [name];
        let bytes = 0;
        while (stack.length) {
          const file = stack.pop() as string;
          const chunk = chunks.get(file);
          if (seen.has(file) || !chunk) continue;
          seen.add(file);
          bytes += encoder.encode(chunk.code).length;
          stack.push(...chunk.imports);
        }
        const kb = bytes / 1000; // Vite reports kB as 1000 bytes
        if (kb > limitKb) {
          this.error(
            `initial JavaScript is ${kb.toFixed(2)} kB (${[...seen].join(', ')}), over the ${limitKb} kB budget: ` +
              'load the new code with a dynamic import (see src/workspaces/lazy.tsx) or raise INITIAL_JS_BUDGET_KB deliberately',
          );
        }
      }
    },
  };
}

export default defineConfig({
  plugins: [initialBundleBudget(INITIAL_JS_BUDGET_KB)],
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
