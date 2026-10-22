# Project Unknown — implementation status

Last updated: 2026-10-22, 09:47 local. Branch: `impl/foundation` (from `docs/technical-book-v1` @ 83df445). Nothing is merged to `main` and nothing has been pushed. No live trading, paid services, data purchases or cloud resources have been used or enabled.

Status words follow ADR-0001. No capability is `paper-verified` or `live-verified`.

## Repository housekeeping (done)

- **`feat/paper-desk` deleted** on 2026-10-22 after verifying that its tip was `83df44519bdb9107f3276d4e83ffa906ad9779e2`, that it had 0 commits not on `docs/technical-book-v1`, that it had no PR, and that it was created by the owner. The book branch and PR #1 are untouched.
- **`impl/foundation` has no upstream.** Push it only with `git push -u origin impl/foundation` (not yet done).

## What exists

| Increment | Area | Status | Where / evidence |
|---|---|---|---|
| — | Architecture decisions | ADR-0001..0016 accepted | `docs/adr/` |
| — | Requirement register, dependency graph | written | `docs/requirements/` |
| — | Capability register | **217 capabilities, 16 areas**, primary-source and independently verified (929 URLs re-fetched); 19 capabilities above `proposed` | `docs/capabilities/register.json`, `README.md` |
| — | Book review | **134 verified findings**; per-item dispositions pending | `docs/design/book-resolutions.md` |
| I1 | Core: exact arithmetic, event store, journal with owner dimension, holds, lots, policy and mandates, session machine, orders and outbox, pretrade, stop/start/restart/restore, reconciliation, valuation, replay | implemented; fault-matrix suite in progress (291 tests, 43 failing at 09:44 and being triaged) | `backend/src/unknown/{domain,store,core,risk,policy}` |
| I1 | Simulated broker with fault injection | tested (20 tests) | `broker/simulated.py` |
| I3 | Core service process, authenticated command socket, terminal API (scrypt login, CSRF, step-up, SSE), owner CLI, FIXTURE market | implemented; exercised end to end over HTTP | `service/`, `api/`, `cli.py` |
| I3 | Browser terminal | in progress (workflow `terminal-build`) | `terminal/` |
| I2 | IBKR paper adapter (TWS API 10.51.01) | in progress (agent); never connected to IBKR | `broker/ibkr/`, ADR-0014 |
| I4 | Point-in-time research store, provider registry, polite fetcher, SEC EDGAR companyfacts ingestion | tested with fixtures (16 tests, py3.12 and 3.14); live SEC fetch not yet run | `backend/src/unknown/data/` |
| I4 | XNYS trading calendar 2026–2028 (NYSE page, retrieved 2026-10-22) | implemented | `config/calendars/XNYS.json` |
| I6 | Bounded agents: envelopes, spend ledger, tariffs, provider adapters, data-use gate | in progress (agent); paid calls disabled | `backend/src/unknown/agents/`, ADR-0016 |
| ops | Runbooks RB-01..RB-11 | written | `docs/runbooks/README.md` |
| perf | Core latency benchmark | v0 measured under heavy machine load. Profile shows per-command full ledger verification and Python-side filtering dominate; optimization pending | `backend/benchmarks/bench_core.py` |

## Known defects being fixed next (from the in-progress fault-matrix suite)

1. **Risk-reducing sells are blocked after a reconciliation with differences.** The recovery gate should block only buys when positions are confirmed (REC-03).
2. **Red-stop cancels are held while reconciliation is required.** Cancels must always be transmitted.
3. **Target observed while SUSPENDED** must start wind-down (ADR-0008).
4. **A stop or broker observation must commit even if a ledger invariant fails.** It should open an exception and latch, instead of aborting.
5. **Self-cross prevention is missing in pretrade.**
6. **Performance:** move to incremental verification, filter orders and lots in SQL, add indexes.

## Owner decisions recorded (2026-10-22)

- **IBKR SDK licence:** proceed with the official GPL-3.0 TWS API client. The repository will be made private when the work is complete, and the software is for personal use (ADR-0014).
- **SEC contact:** the owner authorized their email for the SEC-required User-Agent. It is stored only in `~/.unknown/config/data.json`. Live ingestion is verified: Apple and Microsoft, 57,846 observations.
- **Claude API:** the owner authorized **USD 200 total** Anthropic spend. Proposed caps are 15.00/day and 2.00/job. The default model is `claude-opus-5-5`; other providers stay disabled. The API key is not yet on this machine. It goes in `~/.unknown/secrets/anthropic_api_key` (mode 600) or `ANTHROPIC_API_KEY`.
- **IBKR:** the owner has an IBKR account. The paper account still needs to be identified locally and IB Gateway installed and logged in.

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
```
