# End-to-end tests

`npm run test:e2e` drives the built terminal in headless Chromium against a real backend started for the run. Nothing is mocked: the core (simulated broker, FIXTURE market), the API, the Unix socket, the SQLite database and the hash-chained event log are the production code paths.

```bash
npx playwright install chromium     # once per machine (Chromium build 1243 for @playwright/test 1.63.0)
UNKNOWN_E2E_ROOT=/some/private/dir npm run test:e2e
```

## What global setup does

`global-setup.ts` → `support/backend.ts`:

1. Typechecks and builds the terminal into `<run>/dist`. It never writes `terminal/dist`, which other API processes may be serving.
2. Creates a fresh `UNKNOWN_HOME` at `$UNKNOWN_E2E_ROOT/run-<timestamp>-<pid>` (default root: `<os tmpdir>/unknown-term-e2e`). It runs `unknown.cli init` and writes the owner passphrase record.
3. Starts the core with `--fixture-market` and waits for its socket to answer a health query.
4. Runs `unknown.cli bootstrap-sim`. This registers SIM-1, publishes and activates the simulated policy, registers the fixture instruments, acquires ownership, waits for the opening reconciliation and authorizes 300.00 USD.
5. Starts the API on the first free port in 8800-8899, starting at 8853 (or `E2E_API_PORT`), serving `<run>/dist`.

Every PID goes into `<run>/e2e-pids.json`. `global-teardown.ts` sends SIGTERM to those PIDs only, then SIGKILL after 15 s; nothing is killed by name. Logs stay in the run directory: `core.log`, `api.log` and `bootstrap.log`.

Environment: `UNKNOWN_E2E_ROOT`, `E2E_API_PORT`, `UNKNOWN_PYTHON` (default `python3`), and `E2E_SCREENSHOTS=<dir>` to save a full-page screenshot of every audited view in both themes.

## Test-only seams (`support/harness.py`)

The harness runs the real `unknown.service.core.main` and `unknown.api.server.main`, with one change each:

- **Core: the simulated broker survives a restart.** The simulator lives inside the core process, so a plain restart makes it forget every fill and cash movement while the ledger remembers them. Reconciliation then reports `POSITION_MISMATCH`, and start stays blocked: an acknowledgement cannot clear it, because the next run re-opens it. A real broker keeps the account when our process restarts. To model that, the harness pickles the simulator after a clean SIGTERM shutdown and restores it on the next start.
- **API: a 12 s step-up window instead of the policy's `reauth_max_age_s` (900 s in the default simulated policy).** The harness sets a cap that can only shorten the window. This lets the suite exercise HTTP 428, the passphrase prompt and the retry with the same command id. The core's own `reauth_max_age_s` check is untouched.

## Scenarios

`01-flow.spec.ts` runs in order, in one browser session, on one account:

1. **Login:** a wrong passphrase is refused, then the right one is accepted.
2. **Overview:** SIMULATED appears in text and in the mode colour (badge border and band). The FIXTURE label shows, the account is STOPPED after bootstrap, and the bucket shows 300.00 USD.
3. **Green START with step-up:** after the step-up window has passed, START answers 428. A wrong passphrase is refused; the right one sends START again with the same command id (asserted on the wire: 428 then 200, one id). The command is applied and entries are permitted.
4. **Create a session (100.00 → 105.00, 5.00%):** with entries permitted it is funded and RUNNING.
5. **Order ticket:** a BUY of 1 FIX-EQ-1 at the live ask + 0.02 (exact decimal arithmetic) is ACCEPTED. It shows FILLED in the blotter, and the overview position carries a FIXTURE mark.
6. **Red STOP:** the command id shows and reaches applied, and the header shows STOPPED. Five progress steps appear, with the residual position and "Liquidation on stop: NONE". A new BUY is rejected with STOP_LATCH as the only failed pretrade check.
7. **Core restart:** the core is restarted by PID. The process epoch increments, the header still shows STOPPED, and the latch is the same command id as before.
8. **START after reconciliation:** preflight blockers clear only after a new reconciliation id, then START is applied.
9. **Audit verify:** no chain or ledger problems.
10. **Palette:** Cmd/Ctrl+K (both modifiers) navigates to Sessions and to the session record. Focus moves to the workspace title.
11. **Console:** no page errors, and no console errors except the expected 401, 428 and 503 responses.

`02-a11y.spec.ts` covers 29 views (every workspace, its sub-views and records, and the palette, help, control drawer and a form dialog) in the dark and light themes, using the checks in `support/a11y.ts`:

| Check | WCAG | Rule |
|---|---|---|
| Accessible names | 4.1.2, 1.1.1 | From Playwright's accessibility tree |
| Text contrast | 1.4.3 | Composited background; 4.5:1, or 3:1 for large text |
| Form field edges | 1.4.11 | 3:1 |
| Tables | 1.3.1 | Name, header cells, valid `aria-sort` |
| Ids and ARIA references | 4.1.2 | Unique ids; every referenced id exists |
| Page structure | 3.1.1, 2.4.2, 1.3.1 | `lang`, title, one h1, landmarks |
| Target size | 2.5.8 | 24×24, or the spacing exception |
| Focus indicators | 2.4.7 | Visible, with 3:1 contrast |

It also checks five widths, from 390 px to 1600 px: no sideways page scroll, no header items drawn over each other, and STOP and the mode badge in view.

`00-audit-selftest.spec.ts` gives each check a page with one known defect and requires it to be reported. A clean page must report nothing, so a silent check cannot pass the workspace audit vacuously.

`03-research.spec.ts` covers the Company, Markets & macro and Research workspaces. It runs after the other files, on the same backend:

1. **Before any research data:** each workspace shows the API's unavailable reason verbatim and the command that would populate it (`python -m unknown.data.sec ingest --cik <CIK>`, the Treasury and ECB ingest commands, `python -m unknown.research.study --out <DIR>`). An issuer search says that no SEC source was searched. The formula registry, which is served from code, is available.
2. **Seeding:** `support/seed_research.py --home <run home>` writes the run's research store and registry, where the API reads them. All of it is SYNTHETIC and labelled FIXTURE:
   - the backend's fixture SEC issuers (`backend/tests/test_company_fixtures.py`), ingested through the real `unknown.data.sec.ingest` path with a fake opener;
   - a Treasury par curve on 2026-10-20 and 2026-10-21 (the second without the 1.5M tenor) and ECB rates for four currencies, both through the real parsers with a stub fetcher;
   - the fixture study (`unknown.research.study`).
3. **Company** (every check uses a fixed as-of, so results do not depend on the run date):
   - search, an issuer opened at the search's as-of time, and the tearsheet blocks with their reasons;
   - a restated FY2025 revenue whose original value is what an earlier as-of shows;
   - MISSING, NO_PRICE and INCOMPLETE reasons verbatim, and a show-calculation with the EV bridge;
   - the DCF recipe (WACC build-up from the Treasury 10Y), INCOMPLETE equity figures, the price marker on the football field, an UNAVAILABLE risk-free rate with the complete Treasury ingest command, a REFUSED model (WACC <= g);
   - peers (a member opens at the same as-of, without the price input) and filings (acceptance time as published).
4. **Macro:** the curve with its missing tenor, a comparison date (both ways round: a tenor missing on the curve date keeps the comparison's value), a past as-of that hides the later curve, an as-of before any macro observation (each panel shows the API's reason and the dataset's ingest command), ECB rates and USD crosses.
5. **Research:**
   - experiments with FIXTURE labels and the holdout access log;
   - trials filtered by segment, and a trial's return chart, money and manifest;
   - an artifact's history and gate verdicts (PAIRED_BOOTSTRAP fails with its reason);
   - a report, a specification and the formula filter.
6. **Accessibility and layout:**
   - the `support/a11y.ts` checks on 21 of these views and on a computed DCF, in both themes;
   - keyboard focus indicators through the statements and the DCF form;
   - no sideways page scroll at 390 px.

Run it alone with `npx playwright test e2e/03-research.spec.ts`. Global setup still starts a fresh core and API.
