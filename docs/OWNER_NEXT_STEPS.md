# Owner next steps

Only the owner can do the actions below. Each step names the command and the document behind it. Nothing in the system takes these steps itself.

## 1. IBKR paper (unblocks paper verification, BRK-02)

1. Wait for IBKR to finish processing the paper account. It was requested on 2026-10-22 and is processed by the next business day. Its id is already in `~/.unknown/ibkr.json` (mode 600, outside git).
2. Install **IB Gateway** from IBKR's website and log in with the **paper** username and 2FA. Then set:
   - Configuration → API → Settings:
     - enable ActiveX and Socket Clients;
     - socket port **4002**;
     - **Read-Only API off**;
     - trusted IP 127.0.0.1.
   - Time zone: America/New_York.
3. In Client Portal, share market data with the paper account. Also review the subscriber status shown there ("Non-Commercial Professional" was observed; see STATUS.md).
4. Set up the IBKR environment once: `bash scripts/setup/ibkr_venv.sh`.
5. Start the core with IBKR, then bootstrap the account. `--dry-run` first resolves the listings without sending anything:
   ```bash
   cd backend
   PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.serve --account IBKR-PAPER-1 --deployment mac-dev &
   PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.bootstrap_paper --account IBKR-PAPER-1 --deployment mac-dev --universe AAPL:NASDAQ,MSFT:NASDAQ --dry-run
   PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.bootstrap_paper --account IBKR-PAPER-1 --deployment mac-dev --universe AAPL:NASDAQ,MSFT:NASDAQ
   ```
6. Open the terminal and review preflight. Press **green start** only when every blocker is clear.
7. Work through the evidence checklist in `docs/integrations/ibkr.md` section 5. A capability moves to `paper-verified` only when its item passes. Item 4 now matters more: the core holds an IBKR **Inactive** order as UNKNOWN with its reservation (a held buy is canceled automatically). Record which real sequences produce Inactive.

## 2. Terminal on this Mac

```bash
cd terminal && npm ci --ignore-scripts && npm run build && cd ..
python3 scripts/deploy/macos/render_launchd.py --account IBKR-PAPER-1 --deployment mac-dev --broker ibkr \
    --python ~/.unknown/venvs/ibkr/bin/python --out ~/Library/LaunchAgents
```

Then load the three agents as `docs/deploy/macos.md` describes, and open http://127.0.0.1:8765. Backups run every 15 minutes. Run `python -m unknown.cli restore-drill --account IBKR-PAPER-1` at least once a month.

## 3. Claude research jobs on this Mac (USD 200 authorized; USD 0.008224 spent so far)

1. Put the key at `~/.unknown/secrets/anthropic_api_key` (mode 600). The GitHub secret is only visible to CI.
2. Create the agents environment: `python3 -m venv ~/.unknown/venvs/agents && ~/.unknown/venvs/agents/bin/pip install -r backend/requirements-agents.txt`.
3. Approve the model for the research role in `config/models/approved.json`. The exact entry and the command that prints your policy hash are in `docs/agents/README.md` ("Running research jobs on this Mac").
4. Run `--check`. It writes nothing; then start the worker. The research store already holds live SEC data for AAPL, MSFT, NVDA, AMZN, GOOGL, META and JPM, plus Treasury curves and ECB rates. A job costs roughly USD 0.05–0.25.

## 4. Decisions only the owner can make

The list is in `docs/status/STATUS.md` → "Owner decisions needed":
- the research-gate confidence level for autocorrelated candidates;
- the enterprise-value policy;
- the daily loss window;
- stale quotes on sells;
- unverified fee and data terms;
- EU trading permissions on the IBKR account;
- a review of the Wave D trading-behaviour decisions (Inactive orders, same-id execution amendments, late effects after completion, the memory stop latch), recorded as ADR amendments.

The book-review findings that are still open are tracked with an owner and a missing item for each in `docs/design/book-resolutions.md`.

## 5. Running a strategy (offline data; never IBKR yet)

The promotion ladder changed: `validated -> paper` no longer exists. An artifact reaches `shadow` first, by your approval or by the SYSTEM inside a shadow envelope you approved. Only then can you promote it to `paper`, and live mode needs `paper`. Run one artifact per session, because exposure is counted per session. The steps are in `docs/strategy/README.md`, "Running it (owner)".

## 6. When something goes wrong (Wave D runbooks)

These owner actions exist only for the situations below. Two of them have no terminal form yet: send them through `POST /api/commands` as the runbook shows.

- **RB-12: a stop the database could not record.** STOP answers `persisted: false`, and preflight shows `STOP_LATCHED_IN_MEMORY`. Fix the disk or the database. Never delete `<account>.stop-latch.json`. Green start after the stop is recorded.
- **RB-13: an UNKNOWN order that the broker cannot report.** Get a statement, confirm or support reference, wait for a complete and fresh reconciliation, then send `resolve_exception` with `close_unknown_order` and the evidence (API only). Acknowledging the case in the terminal does not close the order.
- **RB-14: inventory an ended session left behind** (a `POST_COMPLETION_ADJUSTMENT` case). Confirm it with the broker, resolve any `EXECUTION_ANOMALY`, then send a `submit_intent` SELL with purpose `DISPOSAL` naming the ended session (API only).
- **RB-15: a full or corrupt broker-event journal** (stop reason `EVENT_JOURNAL_UNWRITABLE`). Free the disk; or stop the core, move the journal aside and reconcile.
