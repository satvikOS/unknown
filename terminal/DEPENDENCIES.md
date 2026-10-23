# Terminal dependencies

Every dependency is pinned to an exact version in `package.json` (`save-exact=true` in `.npmrc`), and `package-lock.json` is committed. Direct dependencies were chosen to keep the set small: React, ReactDOM, Vite, TypeScript, Vitest, React's type declarations, and for the end-to-end suite only `@playwright/test` and `@types/node`. There is no state, routing, table, UI-kit or CSS library.

Rules (terminal contract):
- Each package version was published at least 14 days before it was adopted. The lockfile was resolved on 2026-10-22 with `npm install --before=2026-10-07T00:00:00Z`, so every transitive package is also at least 15 days old. The end-to-end packages were added the same day with `--before=2026-10-08`; the newest of them (`@types/node` 26.6.4) was published on 2026-10-01.
- Licences are limited to MIT, Apache-2.0, BSD and ISC. The resolved tree was checked from each installed `package.json`; it contains no other licence.
- Only `react`, `react-dom`, `scheduler`, and in the chart workspace's own chunk `lightweight-charts` and `fancy-canvas`, ship in the browser bundle. Everything else is build or test tooling.
- `lightweight-charts` 5.2.1 (published 2026-08-12) and its one dependency `fancy-canvas` 2.1.0 (2022-11-16) were added on 2026-10-23 for the chart workspace (CAP-MKT-07). Its Apache-2.0 licence requires the NOTICE attribution and a link to https://www.tradingview.com/ on the page that shows charts: the chart workspace prints "TradingView Lightweight Charts™ Copyright (c) 2026 TradingView, Inc." with that link and keeps the library's `attributionLogo` on (checked by `src/workspaces/chart/model.test.ts`).

Vite 7.3.7 was chosen over Vite 8.3.x: Vite 8 depends on `lightningcss` (MPL-2.0), which is outside the permitted licences.

`axe-core` (and so `@axe-core/playwright`) is MPL-2.0, so the end-to-end accessibility checks are written in `e2e/support/a11y.ts` instead, with a self-test (`e2e/00-audit-selftest.spec.ts`) proving each check fires.

Playwright drives Chromium, which is downloaded by `npx playwright install chromium` into the user's cache (`~/Library/Caches/ms-playwright` on macOS); the browser binary is not an npm package and is not in the lockfile. `@playwright/test` 1.63.0 uses Chromium build 1243.

To upgrade, choose versions released at least 14 days earlier, check the licence (`npm view <pkg>@<ver> license`), reinstall with `npm install --before=<date 14+ days ago>`, update this file, and run `npm run typecheck && npm test && npm run build && npm run test:e2e`.

## Direct dependencies

| Package | Version | Licence | Released (npm `time`) | Role |
|---|---|---|---|---|
| react | 19.3.0 | MIT | 2026-09-09 | runtime: UI library |
| react-dom | 19.3.0 | MIT | 2026-09-09 | runtime: DOM renderer |
| vite | 7.3.7 | MIT | 2026-10-06 | dev: dev server with the `/api` proxy, production build |
| vitest | 5.0.3 | MIT | 2026-09-30 | dev: unit tests |
| typescript | 6.0.3 | Apache-2.0 | 2026-04-16 | dev: strict type checking (`tsc --noEmit`) |
| @types/react | 19.3.0 | MIT | 2026-09-09 | dev: type declarations |
| @types/react-dom | 19.3.0 | MIT | 2026-09-09 | dev: type declarations |
| @playwright/test | 1.63.0 | Apache-2.0 | 2026-09-04 | dev: end-to-end test runner (`npm run test:e2e`) |
| @types/node | 26.6.4 | MIT | 2026-10-01 | dev: type declarations for the end-to-end support code (Node 26) |
| lightweight-charts | 5.2.1 | Apache-2.0 | 2026-08-12 | runtime (chart workspace chunk): TradingView Lightweight Charts renderer |

JSX is compiled by Vite's built-in esbuild transform (`"jsx": "react-jsx"` in `tsconfig.json`), so `@vitejs/plugin-react` is not needed. The trade-off is that dev reloads are full page reloads rather than Fast Refresh.

## Transitive packages (from package-lock.json)

| Package | Version | Licence | Scope |
|---|---|---|---|
| @esbuild/aix-ppc64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/android-arm | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/android-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/android-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/darwin-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/darwin-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/freebsd-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/freebsd-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-arm | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-ia32 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-loong64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-mips64el | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-ppc64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-riscv64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-s390x | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/linux-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/netbsd-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/netbsd-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/openbsd-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/openbsd-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/openharmony-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/sunos-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/win32-arm64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/win32-ia32 | 0.28.2 | MIT | dev, optional (platform binary) |
| @esbuild/win32-x64 | 0.28.2 | MIT | dev, optional (platform binary) |
| @jridgewell/resolve-uri | 3.1.2 | MIT | dev |
| @jridgewell/sourcemap-codec | 1.6.0 | MIT | dev |
| @jridgewell/trace-mapping | 0.3.31 | MIT | dev |
| @napi-rs/lzma-linux-x64-gnu | 1.5.1 | MIT | dev, optional (platform binary) |
| @rollup/rollup-android-arm-eabi | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-android-arm64 | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-darwin-arm64 | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-darwin-x64 | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-freebsd-arm64 | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-freebsd-x64 | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-arm-gnueabihf | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-arm-musleabihf | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-arm64-gnu | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-arm64-musl | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-loong64-gnu | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-loong64-musl | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-ppc64-gnu | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-ppc64-musl | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-riscv64-gnu | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-riscv64-musl | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-s390x-gnu | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-x64-gnu | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-linux-x64-musl | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-openbsd-x64 | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-openharmony-arm64 | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-win32-arm64-msvc | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-win32-ia32-msvc | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-win32-x64-gnu | 4.64.0 | MIT | dev, optional (platform binary) |
| @rollup/rollup-win32-x64-msvc | 4.64.0 | MIT | dev, optional (platform binary) |
| @types/chai | 5.2.3 | MIT | dev |
| @types/deep-eql | 4.0.2 | MIT | dev |
| @types/estree | 1.0.9 | MIT | dev |
| undici-types | 8.9.0 | MIT | dev (via @types/node; released 2026-07-24) |
| @vitest/mocker | 5.0.3 | MIT | dev |
| @vitest/spy | 5.0.3 | MIT | dev |
| assertion-error | 2.0.1 | MIT | dev |
| chai | 6.3.0 | MIT | dev |
| csstype | 3.2.3 | MIT | dev |
| es-module-lexer | 2.3.2 | MIT | dev |
| esbuild | 0.28.2 | MIT | dev |
| estree-walker | 3.0.3 | MIT | dev |
| expect-type | 1.4.0 | Apache-2.0 | dev |
| fancy-canvas | 2.1.0 | MIT | runtime (chart workspace chunk; dependency of lightweight-charts) |
| fdir | 6.5.0 | MIT | dev |
| fsevents | 2.3.3 | MIT | dev, optional (platform binary; also optional for playwright) |
| magic-string | 1.4.3 | MIT | dev |
| nanoid | 3.3.20 | MIT | dev |
| obug | 2.2.1 | MIT | dev |
| picocolors | 1.1.1 | ISC | dev |
| picomatch | 4.0.7 | MIT | dev |
| playwright | 1.63.0 | Apache-2.0 | dev (via @playwright/test; released 2026-09-04) |
| playwright-core | 1.63.0 | Apache-2.0 | dev (via playwright; released 2026-09-04) |
| postcss | 8.5.29 | MIT | dev |
| rollup | 4.64.0 | MIT | dev |
| scheduler | 0.28.0 | MIT | runtime |
| source-map-js | 1.2.2 | BSD-3-Clause | dev |
| std-env | 4.3.0 | MIT | dev |
| tinybench | 6.2.1 | MIT | dev |
| tinyexec | 1.3.1 | MIT | dev |
| tinyglobby | 0.2.17 | MIT | dev |
| why-is-node-running | 3.2.1 | MIT | dev |
