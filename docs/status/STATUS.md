# Project Unknown — implementation status

Last updated: 2026-10-22, 11:10 local. Branch: `impl/foundation` (from `docs/technical-book-v1` @ 83df445), pushed to origin; `main` is fast-forwarded to it at checkpoints after CI is green (owner instruction 2026-10-22). No live trading, data purchases or cloud resources have been used. Paid inference: one CI smoke run of the agent layer (USD 0.50 per-run cap) under the owner's USD 200 Anthropic authorization.

Status words follow ADR-0001. No capability is `paper-verified` or `live-verified`.

## Repository housekeeping (done)

- **`feat/paper-desk` deleted** on 2026-10-22 after verifying that its tip was `83df44519bdb9107f3276d4e83ffa906ad9779e2`, that it had 0 commits not on `docs/technical-book-v1`, that it had no PR, and that it was created by the owner. The book branch and PR #1 are untouched.
- CI (`.github/workflows/ci.yml`) runs the book companion, the native example and the backend suites on Python 3.12 and 3.14, plus an identifier guard. `live-anthropic.yml` sends one paid call only on manual dispatch or a commit message containing the trigger tag; it fails when the test is skipped.

## What exists

| Increment | Area | Status | Where / evidence |
|---|---|---|---|
| — | Architecture decisions | ADR-0001..0016 accepted | `docs/adr/` |
| — | Requirement register, dependency graph | written | `docs/requirements/` |
| — | Capability register | **217 capabilities, 16 areas**, primary-source and independently verified (929 URLs re-fetched); 19 capabilities above `proposed` | `docs/capabilities/register.json`, `README.md` |
| — | Book review | **134 verified findings**; per-item dispositions pending | `docs/design/book-resolutions.md` |
| I1 | Core: exact arithmetic, event store, journal with owner dimension, holds, lots, policy and mandates, session machine, orders and outbox, pretrade, stop/start/restart/restore, reconciliation, valuation, replay | implemented and tested offline: 258-test fault-matrix suite plus earlier suites, all passing on 3.12 and 3.14; 61 adversarial-review findings, 58 confirmed, about 40 fixed in `c41a505`; the rest in progress (session machine, incremental verification, heartbeat, drain) | `backend/src/unknown/{domain,store,core,risk,policy}` |
| I1 | Simulated broker with fault injection | tested | `broker/simulated.py` |
| I3 | Core service process, authenticated command socket, terminal API (scrypt login, CSRF, step-up, SSE), owner CLI, FIXTURE market | implemented; exercised end to end over HTTP | `service/`, `api/`, `cli.py` |
| I3 | Browser terminal | in progress (workflow `terminal-build`) | `terminal/` |
| I2 | IBKR paper adapter (TWS API 10.51.01, GPL, ADR-0014) | implemented and tested offline (82 tests with a fake host); host clock measurement; service entry point `python -m unknown.broker.ibkr.serve` with reconnect supervision. **Never connected to IBKR** | `broker/ibkr/` |
| I4 | Point-in-time research store, provider registry, polite fetcher, SEC EDGAR companyfacts ingestion | tested with fixtures; **live SEC run done** (Apple, Microsoft: 57,846 observations, 0 conflicts) | `backend/src/unknown/data/` |
| I4 | XNYS trading calendar 2026–2028 (NYSE page, retrieved 2026-10-22) | implemented | `config/calendars/XNYS.json` |
| I5 | Research engine, company research, portfolio analytics, IBKR market data | in progress (workflow wave B) | `research/`, `analytics/`, `data/fundamentals/`, `broker/ibkr/marketdata.py` |
| I6 | Bounded agents: envelopes, spend ledger, tariffs, provider adapters, data-use gate | implemented and tested offline (127 tests). Anthropic via the official SDK (`anthropic==1.12.1`); `claude-opus-5-5` tariff approved under the owner's authorization; other providers build only verified request fragments | `backend/src/unknown/agents/`, `docs/agents/README.md`, ADR-0016 |
| ops | Runbooks RB-01..RB-11 | written | `docs/runbooks/README.md` |
| perf | Core latency benchmark | v0 measured under heavy machine load; incremental verification in progress | `backend/benchmarks/bench_core.py` |

## Fixed in `c41a505` (adversarial review and fault matrix)

Authorization runs before command-id dedup. Every system latch goes through the cancel-all-buys path. Cancels are always transmitted and stay re-issuable after a crash or timeout. Restore detection uses an in-database backup marker and fails closed. Each settlement row records the owner and the account code it relieves. Overcommitment moves to `exception:<case>`, with an owner `cover_shortfall`. Corrections, pre-import executions and external commissions are booked, never parked. Preflight fails closed on an unknown broker account mode or clock. The hash chain covers every event column. Details are in the commit message.

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
  - Engineering follow-up: tag quotes `consolidated`/`non_consolidated`. Allow non-consolidated real-time quotes for PAPER validation only, labelled in the terminal. LIVE requires consolidated data unless the owner decides otherwise.

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
# IBKR PAPER (owner logged in to IB Gateway on 4002; config in ~/.unknown/ibkr.json):
bash ../scripts/setup/ibkr_venv.sh
PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.serve --account IBKR-PAPER-1 --deployment mac-dev
```
