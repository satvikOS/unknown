#!/usr/bin/env python3
"""Render launchd agents for the core service, the terminal API, backups, the news poller and the AI workers.

    python3 scripts/deploy/macos/render_launchd.py --account SIM-1 --deployment mac-dev --broker simulated \
        --python "$(command -v python3)" --out ~/Library/LaunchAgents

Writes one .plist per process with plistlib (always well-formed) and prints the launchctl commands; it does not load
them (docs/deploy/macos.md):

* com.unknown.core     the single-writer core (``unknown.service.core``, or ``unknown.broker.ibkr.serve`` with
                       --broker ibkr; pass the IBKR venv interpreter as --python, it imports the GPL TWS API client);
* com.unknown.api      the terminal API. It runs under the IBKR venv (--ibkr-python) whenever
                       ``<home>/config/marketdata.json`` enables the IBKR market-data source, because the API's
                       market-data bus imports that source; a missing IBKR venv is then an error, not a silent
                       UNAVAILABLE source;
* com.unknown.backup   periodic backups;
* com.unknown.news     the news poller (``unknown.news.poller run``, standard library only);
* com.unknown.analyst  the AI analyst worker (``unknown.agents.analyst``) and
* com.unknown.ai       the AI process worker (``unknown.agents.worker``: research jobs and the AI processes), both under
                       the agents venv (--agents-python). They are skipped, with the reason printed, when that
                       interpreter does not exist yet (docs/agents/README.md creates it).

IB Gateway itself is not a launchd job here: the owner starts it and logs in (2FA); nothing automates a broker
login. The core, its market-data session and the API's IBKR source reconnect with backoff when the Gateway returns."""
from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CORE_LABELS = ("com.unknown.core", "com.unknown.api", "com.unknown.backup", "com.unknown.news")
AI_LABELS = ("com.unknown.analyst", "com.unknown.ai")


def ibkr_market_data_configured(home: Path) -> bool:
    """True when <home>/config/marketdata.json enables the IBKR source (parsed by the product's own loader)."""
    sys.path.insert(0, str(REPO / "backend" / "src"))
    try:
        from unknown.marketdata.config import load_config
    finally:
        sys.path.pop(0)
    return "IBKR" in load_config(home).sources


def interpreters(args) -> dict[str, str | None]:
    """Interpreter per role. Raises SystemExit(2) with the reason when a required venv is missing."""
    home = Path(args.home).expanduser()
    ibkr_py = Path(args.ibkr_python or home / "venvs" / "ibkr" / "bin" / "python").expanduser()
    agents_py = Path(args.agents_python or home / "venvs" / "agents" / "bin" / "python").expanduser()
    api = args.python
    if args.broker == "ibkr" or ibkr_market_data_configured(home):
        if args.broker == "ibkr" and args.ibkr_python is None:
            api = args.python                 # --python is already the IBKR venv for the IBKR core
        elif not ibkr_py.exists():
            raise SystemExit(f"error: {home / 'config' / 'marketdata.json'} enables the IBKR market-data source, so "
                             f"the API must run under the IBKR venv, but {ibkr_py} does not exist (run "
                             "scripts/setup/ibkr_venv.sh or pass --ibkr-python)")
        else:
            api = str(ibkr_py)
    return {"core": args.python, "api": api, "tools": args.python,
            "agents": str(agents_py) if agents_py.exists() else None, "agents_path": str(agents_py)}


def agents(args, py: dict | None = None) -> dict[str, dict]:
    py = py or interpreters(args)
    home = Path(args.home).expanduser()
    env = {"PYTHONPATH": str(REPO / "backend" / "src"), "UNKNOWN_HOME": str(home)}
    logs = home / "logs"
    core_module = "unknown.broker.ibkr.serve" if args.broker == "ibkr" else "unknown.service.core"
    core_args = [py["core"], "-m", core_module, "--account", args.account, "--deployment", args.deployment]
    common = {"WorkingDirectory": str(REPO / "backend"), "EnvironmentVariables": env}

    def logs_for(name: str) -> dict:
        return {"StandardOutPath": str(logs / f"{name}.out.log"), "StandardErrorPath": str(logs / f"{name}.err.log")}

    out = {
        "com.unknown.core": {
            "Label": "com.unknown.core", "ProgramArguments": core_args, **common,
            # Restart after a crash; a restart begins reconciliation-required with the stop kept (ADR-0010).
            "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "ProcessType": "Interactive",
            **logs_for("core")},
        "com.unknown.api": {
            "Label": "com.unknown.api",
            "ProgramArguments": [py["api"], "-m", "unknown.api.server", "--account", args.account, "--port", str(args.port)],
            **common, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10, **logs_for("api")},
        "com.unknown.backup": {
            "Label": "com.unknown.backup",
            "ProgramArguments": [py["tools"], "-m", "unknown.cli", "backup", "--account", args.account, "--keep", str(args.keep)],
            **common, "StartInterval": args.backup_interval_s, **logs_for("backup")},
        "com.unknown.news": {
            "Label": "com.unknown.news",
            "ProgramArguments": [py["tools"], "-m", "unknown.news.poller", "run", "--home", str(home)],
            # One poller per home (it holds news/poller.lock); a crash is restarted after 60 s.
            **common, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 60, **logs_for("news")},
    }
    if py["agents"] is not None:
        out["com.unknown.analyst"] = {
            "Label": "com.unknown.analyst",
            "ProgramArguments": [py["agents"], "-m", "unknown.agents.analyst", "--home", str(home)],
            **common, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, **logs_for("analyst")}
        out["com.unknown.ai"] = {
            "Label": "com.unknown.ai",
            "ProgramArguments": [py["agents"], "-m", "unknown.agents.worker", "--home", str(home)],
            **common, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, **logs_for("ai")}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", required=True)
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--broker", choices=["simulated", "ibkr"], default="simulated")
    ap.add_argument("--python", required=True, help="absolute interpreter path (the IBKR venv's for --broker ibkr)")
    ap.add_argument("--ibkr-python", default=None,
                    help="IBKR venv interpreter for the API when the IBKR market-data source is configured "
                         "(default <home>/venvs/ibkr/bin/python)")
    ap.add_argument("--agents-python", default=None,
                    help="agents venv interpreter for the AI workers (default <home>/venvs/agents/bin/python)")
    ap.add_argument("--home", default="~/.unknown")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--keep", type=int, default=96)
    ap.add_argument("--backup-interval-s", type=int, default=900)
    ap.add_argument("--out", required=True, help="directory for the .plist files (e.g. ~/Library/LaunchAgents)")
    args = ap.parse_args(argv)
    for name in ("python", "ibkr_python", "agents_python"):
        value = getattr(args, name)
        if value is not None and not Path(value).expanduser().is_absolute():
            ap.error(f"--{name.replace('_', '-')} must be an absolute path (launchd has no shell PATH)")
    try:
        py = interpreters(args)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    except ValueError as exc:                      # an invalid marketdata.json (MarketDataConfigError)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (Path(args.home).expanduser() / "logs").mkdir(parents=True, exist_ok=True)
    rendered = agents(args, py)
    for label, body in rendered.items():
        path = out / f"{label}.plist"
        with open(path, "wb") as f:
            plistlib.dump(body, f)
        print(f"wrote {path}")
    if py["api"] != args.python:
        print(f"API runs under {py['api']}: config/marketdata.json enables the IBKR market-data source")
    if py["agents"] is None:
        print(f"skipped {', '.join(AI_LABELS)}: {py['agents_path']} does not exist (create the agents venv as "
              "docs/agents/README.md describes, then render again)")
    print("\nload with:")
    for label in rendered:
        print(f"  launchctl bootstrap gui/$(id -u) {out / (label + '.plist')}")
    print("unload with:  launchctl bootout gui/$(id -u)/<label>   (for example com.unknown.core)")
    print("IB Gateway is not a launchd job: start it and log in yourself (2FA); nothing here logs in for you.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
