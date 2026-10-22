#!/usr/bin/env python3
"""Render launchd agents for the core service, the terminal API and periodic backups (docs/deploy/macos.md).

    python3 scripts/deploy/macos/render_launchd.py --account SIM-1 --deployment mac-dev --broker simulated \
        --python "$(command -v python3)" --out ~/Library/LaunchAgents

Writes com.unknown.core.plist, com.unknown.api.plist and com.unknown.backup.plist with plistlib (always
well-formed) and prints the launchctl commands; it does not load them. For --broker ibkr pass the research
venv interpreter (~/.unknown/venvs/ibkr/bin/python): the IBKR service imports the GPL TWS API client."""
from __future__ import annotations

import argparse
import plistlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def agents(args) -> dict[str, dict]:
    home = Path(args.home).expanduser()
    env = {"PYTHONPATH": str(REPO / "backend" / "src"), "UNKNOWN_HOME": str(home)}
    logs = home / "logs"
    core_module = "unknown.broker.ibkr.serve" if args.broker == "ibkr" else "unknown.service.core"
    core_args = [args.python, "-m", core_module, "--account", args.account, "--deployment", args.deployment]
    if args.broker == "simulated" and args.fixture_market:
        core_args.append("--fixture-market")
    common = {"WorkingDirectory": str(REPO / "backend"), "EnvironmentVariables": env}
    return {
        "com.unknown.core": {
            "Label": "com.unknown.core", "ProgramArguments": core_args, **common,
            # Restart after a crash; a restart begins reconciliation-required with the stop kept (ADR-0010).
            "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "ProcessType": "Interactive",
            "StandardOutPath": str(logs / "core.out.log"), "StandardErrorPath": str(logs / "core.err.log")},
        "com.unknown.api": {
            "Label": "com.unknown.api",
            "ProgramArguments": [args.python, "-m", "unknown.api.server", "--account", args.account, "--port", str(args.port)],
            **common, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
            "StandardOutPath": str(logs / "api.out.log"), "StandardErrorPath": str(logs / "api.err.log")},
        "com.unknown.backup": {
            "Label": "com.unknown.backup",
            "ProgramArguments": [args.python, "-m", "unknown.cli", "backup", "--account", args.account, "--keep", str(args.keep)],
            **common, "StartInterval": args.backup_interval_s,
            "StandardOutPath": str(logs / "backup.out.log"), "StandardErrorPath": str(logs / "backup.err.log")},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", required=True)
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--broker", choices=["simulated", "ibkr"], default="simulated")
    ap.add_argument("--fixture-market", action="store_true")
    ap.add_argument("--python", required=True, help="absolute interpreter path")
    ap.add_argument("--home", default="~/.unknown")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--keep", type=int, default=96)
    ap.add_argument("--backup-interval-s", type=int, default=900)
    ap.add_argument("--out", required=True, help="directory for the .plist files (e.g. ~/Library/LaunchAgents)")
    args = ap.parse_args(argv)
    if not Path(args.python).is_absolute():
        ap.error("--python must be an absolute path (launchd has no shell PATH)")
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (Path(args.home).expanduser() / "logs").mkdir(parents=True, exist_ok=True)
    for label, body in agents(args).items():
        path = out / f"{label}.plist"
        with open(path, "wb") as f:
            plistlib.dump(body, f)
        print(f"wrote {path}")
    print("\nload with:")
    for label in ("com.unknown.core", "com.unknown.api", "com.unknown.backup"):
        print(f"  launchctl bootstrap gui/$(id -u) {out / (label + '.plist')}")
    print("unload with:  launchctl bootout gui/$(id -u)/com.unknown.core   (and .api, .backup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
