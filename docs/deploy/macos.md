# Running the desk on the Mac (launchd)

Status (ADR-0001): **implemented, exercised locally with the simulated broker**. The IBKR variant is implemented but **not paper-verified** (no paper session yet).

## Processes

| launchd label | Runs | Restart policy |
|---|---|---|
| `com.unknown.core` | Single-writer core: `unknown.service.core` for the simulator, or `unknown.broker.ibkr.serve` for IBKR PAPER | Restarted after a crash (30 s throttle). A restart always begins reconciliation-required. The stop latch and resume hold persist (ADR-0010). |
| `com.unknown.api` | Terminal API on `127.0.0.1:8765` (owner login, CSRF, step-up). It runs under the IBKR venv (`--ibkr-python`, default `~/.unknown/venvs/ibkr/bin/python`) whenever `~/.unknown/config/marketdata.json` enables the IBKR market-data source, because the API's market-data bus imports that source. If that venv is missing, rendering stops with an error. | Restarted after a crash. |
| `com.unknown.backup` | `unknown.cli backup` every 15 minutes, keeping 96 copies (24 h) | Periodic. |
| `com.unknown.news` | News poller: `unknown.news.poller run` (standard library; one poller per home) | Restarted after a crash (60 s throttle). |
| `com.unknown.analyst` | AI analyst worker: `unknown.agents.analyst`, under the agents venv (`--agents-python`, default `~/.unknown/venvs/agents/bin/python`) | Restarted after a crash (30 s throttle). |
| `com.unknown.ai` | AI process worker: `unknown.agents.worker` (research jobs and the AI processes), under the agents venv | Restarted after a crash (30 s throttle). |

The two AI jobs are rendered only when the agents venv exists. Otherwise the renderer prints why it skipped them;
create the venv as `docs/agents/README.md` describes, then render again. The AI workers make paid model calls only
under the owner's policy in `~/.unknown/config/agents.json`.

**IB Gateway is not a launchd job.** The owner starts IB Gateway and logs in with the paper username and 2FA. Nothing
in this repository logs in, stores the IBKR password or bypasses broker security. When the Gateway drops (IBKR 1100,
the nightly restart, weekend maintenance or a closed socket), no process needs restarting. The core, its market-data
session and the API's IBKR source each reconnect with backoff (5 s, doubling to 300 s) once the Gateway accepts
connections again. Until then they report the outage:

- the core's health (`GET /api/health` → `core.broker_connection`): `state` is `reconnecting` (socket closed),
  `waiting_for_gateway` (socket up while IBKR is down, or not logged in) or `latched` (identity mismatch; only the
  owner clears it). It also shows the reason, failed attempts and the next attempt;
- `core.market_data` for the core's market-data session: `status` is `RECONNECTING`, `UPSTREAM_DOWN` or
  `DATA_FARM_DOWN`, with the reconnect state;
- `GET /api/marketdata/sources` for the bus source: `state`, `reason` and `reconnect`. Its quotes show STALE meanwhile.

A second core process on the same database cannot write: the writer lease fences it (ADR-0011). A crashed core's lease goes stale 30 s after its last heartbeat. The first restart after a crash can therefore find the lease still fresh and exit with `LeaseHeld`. launchd retries after its 30 s throttle, and that attempt takes the stale lease. A clean shutdown releases the lease, so the next start is immediate.

## Install

```bash
cd ~/unknown
python3 -m venv ~/.unknown/venvs/core          # optional: the core needs only the standard library
PYTHONPATH=backend/src python3 -m unknown.cli init
PYTHONPATH=backend/src python3 -m unknown.cli set-passphrase
# simulator (no quote source: simulated orders never fill and every value shows an em dash with NO_SOURCE; ADR-0020):
python3 scripts/deploy/macos/render_launchd.py --account SIM-1 --deployment mac-dev --broker simulated \
    --python "$(command -v python3)" --out ~/Library/LaunchAgents
# or IBKR PAPER (after scripts/setup/ibkr_venv.sh and ~/.unknown/ibkr.json; the owner logs in to IB Gateway):
python3 scripts/deploy/macos/render_launchd.py --account IBKR-PAPER-1 --deployment mac-dev --broker ibkr \
    --python ~/.unknown/venvs/ibkr/bin/python --out ~/Library/LaunchAgents
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.unknown.core.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.unknown.api.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.unknown.backup.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.unknown.news.plist
# only if the renderer wrote them (agents venv present):
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.unknown.analyst.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.unknown.ai.plist
```

The renderer prints the exact `launchctl bootstrap` line for every file it wrote. Load the core before the API. To
replace a loaded job after rendering again, `bootout` it first and then `bootstrap` it.

Logs are written to `~/.unknown/logs/`. To stop a process, use `launchctl bootout gui/$(id -u)/com.unknown.core` (and the same for `.api`, `.backup`, `.news`, `.analyst` and `.ai`).

Stopping the core process does **not** cancel orders resting at the broker. The red stop (RB-01) is the way to pull entries. If the core is down, use the broker's own interface.

## IBKR specifics

- **Login:** the owner logs in to IB Gateway (or TWS) manually with 2FA. Nothing in this repository automates login or 2FA (CLAUDE.md, book Chapter 12). Log in again weekly and after IBKR's resets. The core reconnects with backoff (5 s doubling to 300 s), reconciles and keeps the stop and resume hold. Its market-data session and the API's IBKR source reconnect the same way and resubscribe (see "IB Gateway is not a launchd job" above).
- **Evidence:** `python -m unknown.broker.ibkr.evidence --account IBKR-PAPER-1` (IBKR venv) runs the read-only part of the paper checklist and writes its record to `~/.unknown/evidence/` (`docs/integrations/ibkr.md` section 5).
- **Settings:** socket port 4002 (Gateway paper), Read-Only API off, API time zone America/New_York. Share market data with the paper account in Client Portal.
- **Identity:** the configuration is in `~/.unknown/ibkr.json` (mode 600, outside git). If the account list reported at handshake differs from the configured paper account, the adapter latches `DISCONNECTED_UNSAFE` and the core never retries it.

## Backups

Copy `~/.unknown/backups/` off the Mac encrypted, for example with Time Machine to an encrypted disk. Run `unknown.cli restore-drill` at least monthly and record the result (RB-10).
