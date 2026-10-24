# Running the desk on the Mac (launchd)

Status (ADR-0001): **implemented, exercised locally with the simulated broker**. The IBKR variant is implemented but **not paper-verified** (no paper session yet).

## Processes

| launchd label | Runs | Restart policy |
|---|---|---|
| `com.unknown.core` | Single-writer core: `unknown.service.core` for the simulator, or `unknown.broker.ibkr.serve` for IBKR PAPER | Restarted after a crash (30 s throttle). A restart always begins reconciliation-required. The stop latch and resume hold persist (ADR-0010). |
| `com.unknown.api` | Terminal API on `127.0.0.1:8765` (owner login, CSRF, step-up) | Restarted after a crash. |
| `com.unknown.backup` | `unknown.cli backup` every 15 minutes, keeping 96 copies (24 h) | Periodic. |

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
```

Logs are written to `~/.unknown/logs/`. To stop a process, use `launchctl bootout gui/$(id -u)/com.unknown.core` (and the same for `.api` and `.backup`).

Stopping the core process does **not** cancel orders resting at the broker. The red stop (RB-01) is the way to pull entries. If the core is down, use the broker's own interface.

## IBKR specifics

- **Login:** the owner logs in to IB Gateway (or TWS) manually with 2FA. Nothing in this repository automates login or 2FA (CLAUDE.md, book Chapter 12). Log in again weekly and after IBKR's resets. The core reconnects with backoff (5 s doubling to 300 s), reconciles and keeps the stop and resume hold.
- **Settings:** socket port 4002 (Gateway paper), Read-Only API off, API time zone America/New_York. Share market data with the paper account in Client Portal.
- **Identity:** the configuration is in `~/.unknown/ibkr.json` (mode 600, outside git). If the account list reported at handshake differs from the configured paper account, the adapter latches `DISCONNECTED_UNSAFE` and the core never retries it.

## Backups

Copy `~/.unknown/backups/` off the Mac encrypted, for example with Time Machine to an encrypted disk. Run `unknown.cli restore-drill` at least monthly and record the result (RB-10).
