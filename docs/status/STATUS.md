# Project Unknown — implementation status

Last updated: 2026-10-22, 13:30 local. Branch: `impl/foundation` (from `docs/technical-book-v1` @ 83df445), pushed to origin; `main` is fast-forwarded to it at checkpoints after CI is green (owner instruction 2026-10-22). No live trading, data purchases or cloud resources have been used. Paid inference so far: one CI smoke call of `claude-opus-5-5`, **USD 0.008224**, under the owner's USD 200 Anthropic authorization.

Status words follow ADR-0001. No capability is `paper-verified` or `live-verified`.

## Repository housekeeping (done)

- **`feat/paper-desk` deleted** on 2026-10-22 after verifying that its tip was `83df44519bdb9107f3276d4e83ffa906ad9779e2`, that it had 0 commits not on `docs/technical-book-v1`, that it had no PR, and that it was created by the owner. The book branch and PR #1 are untouched.
- **CI** (`.github/workflows/ci.yml`) runs:
  - the book companion and the native example;
  - the backend suites on Python 3.12 and 3.14;
  - the terminal's typecheck, unit tests and build;
  - an identifier guard.
- **`live-anthropic.yml`** sends one paid call, only on manual dispatch or a commit message containing the trigger tag. A skipped test fails the job.

## What exists

| Increment | Area | Status | Where / evidence |
|---|---|---|---|
| — | Architecture decisions | ADR-0001..0016 accepted; ADR-0017 (strategy artifacts, promotion, runtime) proposed | `docs/adr/` |
| — | Requirement register, dependency graph | written | `docs/requirements/` |
| — | Capability register | **217 capabilities, 16 areas**, primary-source and independently verified (929 URLs re-fetched). Statuses are being updated as increments land. | `docs/capabilities/register.json`, `README.md` |
| — | Book review | **134 findings, each with a checked disposition**: 77 resolved in code (each with a test), 6 by ADR, 4 accepted limitations, 47 open with owners. `scripts/book_review/build_resolutions.py --check` validates every citation. | `docs/design/book-resolutions.md` |
| — | Threat model | 15 threats mapped to enforcing code and tests; accepted risks listed | `docs/security/threat-model.md` |
| I1 | Core: exact arithmetic, event store, journal with owner dimension, holds, lots, policy and mandates, session machine, orders and outbox, pretrade, stop/start/restart/restore, reconciliation, valuation, replay | Implemented and tested offline. All confirmed review findings are fixed, each with regression tests: 58 from the first review, 19 from the second, and 8 residuals found by an adversarial verifier. Per-command ledger verification is now incremental, with full checks at start, at every reconciliation and at green start. | `backend/src/unknown/{domain,store,core,risk,policy}`; `tests/test_review_fixes.py`, `test_sessions_review.py`, `test_durability_review.py` |
| I1 | Simulated broker with fault injection | tested | `broker/simulated.py` |
| I3 | Core service, authenticated command socket (per-role tokens; strategy tokens bound to one artifact), terminal API (scrypt login, CSRF, step-up, SSE, plugin routes), owner CLI, FIXTURE market | implemented; exercised end to end over HTTP (route smoke test 2026-10-22) | `service/`, `api/`, `cli.py` |
| I3 | Browser terminal: STOP/START control plane, Sessions, Orders (ticket, blotter, recorded pretrade decisions), Risk, Policy, Operations (exceptions, cover_shortfall, reconciliation), Audit (event log, journal, verify), Instruments, Capabilities | implemented and tested: 259 unit tests, plus 31 Playwright e2e tests against a real core and API (fixture market) | `terminal/` |
| I2 | IBKR paper adapter (TWS API 10.51.01, GPL, ADR-0014): orders, executions, commissions, snapshots, host clock, late-handshake verification; service entry point `python -m unknown.broker.ibkr.serve` with reconnect supervision; read-only market-data session (L1, historical bars, contract details) wired to the core's quote book | implemented and tested offline with a fake host and the real SDK objects. **Never connected to IBKR** | `broker/ibkr/` |
| I4 | Point-in-time research store, provider registry, SEC EDGAR companyfacts; Treasury, ECB, OpenFIGI, SEC tickers; company statements, metrics, DCF, peers; `/api/company`, `/api/macro` | tested with fixtures. Live SEC run done (Apple, Microsoft: 57,846 observations, 0 conflicts). Each new source's live smoke test ran once. | `data/`, `data/fundamentals/`, `data/sources/` |
| I4 | Markets: US (XNYS), India (XNSE, XBOM, NCL-CM, ICCL-EQ) and EU (XETR, XPAR, XAMS, XBRU, XLIS, XMIL, XMAD, T2S-EUR) calendars with explicit Muhurat sessions and horizons; venue registry; settlement cycles by trade date (US T+1, India T+1, EU T+2 then T+1 from 2027-10-11) | Implemented. Every date was independently re-verified against official sources; the 3 corrections found are applied. | `config/calendars/`, `config/markets/venues.json`, `domain/markets.py`, `docs/markets/README.md` |
| I5 | Research engine: causal daily-bar backtester, IBKR-like costs, dependence-aware statistics (bootstrap, DSR/PSR, PBO), walk-forward/CPCV, pre-registered trial registry with holdout grants, TREND_DAILY_V1 and baselines, artifact registry and promotion lifecycle, replay through the real core; `/api/research` | tested on synthetic fixtures (175 tests). No market history has been evaluated yet. | `research/`, `docs/research/README.md` |
| I5 | Portfolio analytics: holdings, P&L, TWR/IRR, exposure, VaR with backtests, stress, what-if sandbox, allocation proposals, snapshots; `/api/portfolio` | tested (116 tests). Benchmarks, attribution and factor models are not built (they need licensed data). | `analytics/`, `docs/research/portfolio.md` |
| I5 | Strategy runtime (ADR-0017): decision records (`record_strategy_decision`), per-artifact strategy tokens | core part implemented and tested; the strategy runner process is next | `core/handlers/strategy.py` |
| I6 | Bounded agents: envelopes, spend ledger, tariffs, provider adapters, data-use gate; model worker, durable job store, hash-chained audit trail, PIT evidence, `/api/research/jobs` | Implemented and tested offline. One live Anthropic call ran in CI (USD 0.008224). No live job has run on this Mac: the model is not approved in `approved.json` and no key is on the Mac. | `agents/`, `docs/agents/README.md` |
| ops | Online backups, restore drill and restore (`unknown.cli backup`, `restore-drill`, `restore`); launchd renderer; Mac and AWS deployment docs; runbooks RB-01..RB-11 | Implemented and tested. The AWS path is specified only; nothing is provisioned. | `ops/backup.py`, `scripts/deploy/macos/`, `docs/deploy/`, `docs/runbooks/` |
| perf | Core latency benchmark | Incremental verification: fill CPU per command 6.1 → 2.9 ms (p50), outbox claim 4.2 → 0.6 ms. Wall time is dominated by fsync on a loaded machine. | `backend/benchmarks/bench_core.py` |

## Owner decisions needed (raised by today's work)

- **Research gate:** on highly autocorrelated candidates, the paired-bootstrap gate interval covers 92.7% instead of the 93–97% target. Choose a higher confidence level, or a minimum effective sample size, for such candidates.
- **Enterprise value:** filers usually omit preferred stock and minority interest when they have none, so EV is INCOMPLETE for most filers. Keep that, or treat an omission as none, with a flag.
- **Daily loss window:** it starts at the first valuation of each risk day, so an overnight gap counts only against the cumulative allowance. Confirm or change.
- **Stale quotes on sells:** a stale quote also refuses risk-reducing exits (QUOTE_FRESH applies to sells). Confirm against ADR-0010.
- **Unverified data:**
  - third-party sell fees are assumed at 1 bp;
  - the reuse terms of the Treasury feed are unreviewed;
  - whether the EU T+1 switch applies to trades or to settlements dated from 2027-10-11 is unconfirmed.
- **IBKR eligibility:** whether the account has EU trading permissions. NSE needs an IB India account (Indian residents or NRIs); BSE is not offered by IBKR.

## Owner decisions recorded (2026-10-22)

- **IBKR SDK licence:** proceed with the official GPL-3.0 TWS API client. The repository will be made private when the work is complete, and the software is for personal use (ADR-0014).
- **SEC contact:** the owner authorized their email for the SEC-required User-Agent. It is stored only in `~/.unknown/config/data.json`. Live ingestion is verified: Apple and Microsoft, 57,846 observations.
- **Claude API:** the owner authorized **USD 200 total** Anthropic spend. The caps are in `~/.unknown/config/agents.json`: 200.00 lifetime, 15.00/day, 2.00/job. The default model is `claude-opus-5-5`, whose tariff row is approved; other providers stay disabled. The key is the GitHub Actions secret `API_KEY_ANTHROPIC`, used only by `live-anthropic.yml`. For agent runs on this Mac, the key must also go in `~/.unknown/secrets/anthropic_api_key` (mode 600) or `ANTHROPIC_API_KEY`.
- **IBKR:** the owner has an IBKR account. A paper account was requested on 2026-10-22 and is processed by the next business day. Its id is stored only in `~/.unknown/ibkr.json` (mode 600). IB Gateway must be installed and logged in by the owner: port 4002, Read-Only API off, time zone America/New_York, market-data sharing with the paper account on.
- **Market data:** the owner chose a **real-time US equity subscription** in IBKR Client Portal, shared with the paper account. Delayed data stays forbidden for order validation in every mode.
- **Market data observed on the owner's account (screenshot, 2026-10-22).** No account identifiers are recorded here.
  - The Market Data API Acknowledgement is signed (2026-10-22) and API access is enabled.
  - US equities come from **"US Real-Time Non Consolidated Streaming Quotes"** (fee waived). IBKR's pricing page says this free feed is Cboe One + IEX and **non-consolidated** (record CAP-DATA-10): it is real time, but it is not the national best bid/offer. No Network A/B/C subscription is present.
  - The subscriber status shows **"Non-Commercial Professional"**. IBKR classifies users as Professional by default and tells individuals to update their status if they qualify as Non-Professional (CAP-IBKR-12). The owner should review this, because it changes exchange fees, for example under the CTA schedule in CAP-MKT-11.
  - Open question: whether exchange *non-display* (automated-use) fees apply to this account's algorithmic consumption of consolidated data (CAP-MKT-11 lists CTA non-display fees). Confirm with IBKR before subscribing for automated use.
  - Done: quotes carry `consolidation` and `market_data_type`. Non-consolidated real-time quotes validate SIMULATED and PAPER orders; LIVE requires consolidated (pretrade `QUOTE_CONSOLIDATED`). `~/.unknown/config/ibkr_marketdata.json` labels the STK:USD feed non-consolidated.

## External blockers

- **IBKR paper verification (BRK-02):** needs an IBKR Pro paper account identified in local config, TWS or IB Gateway installed and logged in manually with 2FA, and the API enabled. None of this exists on this Mac (checked 2026-10-22).
- **FRED/ALFRED:** needs a free API key registered by the owner.
- **Live trading:** all deployment-record gates are unset; refused in code.

## How to run

```bash
cd backend && PYTHONPATH=src python3 -m unittest discover -s tests -v          # also /opt/homebrew/bin/python3.12
# Simulated desk as processes (FIXTURE market):
export UNKNOWN_HOME=~/.unknown-sim
PYTHONPATH=src python3 -m unknown.cli init && PYTHONPATH=src python3 -m unknown.cli set-passphrase
PYTHONPATH=src python3 -m unknown.service.core --account SIM-1 --deployment mac-dev --fixture-market &
PYTHONPATH=src python3 -m unknown.cli bootstrap-sim --account SIM-1 --deployment mac-dev
PYTHONPATH=src python3 -m unknown.api.server --account SIM-1 --port 8765 &   # then open http://127.0.0.1:8765
# Research jobs (model worker; needs the agents venv, a key in ~/.unknown/secrets/anthropic_api_key mode 600 and an
# owner approval in config/models/approved.json; see docs/agents/README.md "Owner steps for a live job"):
PYTHONPATH=src ~/.unknown/venvs/agents/bin/python -m unknown.agents.worker --check   # local readiness, no network
PYTHONPATH=src ~/.unknown/venvs/agents/bin/python -m unknown.agents.worker &          # then POST /api/research/jobs
# IBKR PAPER (owner logged in to IB Gateway on 4002; config in ~/.unknown/ibkr.json):
bash ../scripts/setup/ibkr_venv.sh
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.serve --account IBKR-PAPER-1 --deployment mac-dev
```
