"""``python -m unknown.strategy``: run one strategy artifact, or print an artifact's ADR-0017 id.

  python -m unknown.strategy id --hash <64-hex artifact hash>
  python -m unknown.strategy run --artifact TREND_DAILY_V1@0123456789abcdef --account SIM-1 --mode shadow --once
  python -m unknown.strategy run --artifact TREND_DAILY_V1@0123456789abcdef --account SIM-1 --mode live --loop

Exit codes (``--once``): 0 decisions handled, 2 refused (artifact, token, calendar), 3 core unreachable,
4 no active session to record into. ``--loop`` runs until SIGTERM/SIGINT."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from ..domain.calendar import WEEKDAYS_ONLY, CalendarHorizonError, load_calendar
from ..service.paths import Paths
from .artifacts import ArtifactRefused

REPO = Path(__file__).resolve().parents[4]


def _calendar(value: str):
    if value == "WEEKDAYS":
        return WEEKDAYS_ONLY
    path = Path(value)
    if not path.suffix:
        path = REPO / "config" / "calendars" / f"{value}.json"
    return load_calendar(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m unknown.strategy", description="ADR-0017 strategy runner")
    ap.add_argument("--home", default=None, help="runtime root (default ~/.unknown or $UNKNOWN_HOME)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("id", help="print the ADR-0017 id of a registered artifact hash")
    i.add_argument("--hash", required=True)
    i.add_argument("--registry", default=None)
    r = sub.add_parser("run", help="run one artifact (shadow records decisions only; live also submits intents)")
    r.add_argument("--artifact", required=True, help="<CLASS>@<16 hex>, as listed in the session mandate")
    r.add_argument("--account", required=True)
    r.add_argument("--mode", required=True, choices=["shadow", "live"])
    r.add_argument("--session", default=None, help="pin a session id (default: the account's single active session)")
    r.add_argument("--calendar", default="WEEKDAYS", help="WEEKDAYS, a calendar id under config/calendars (e.g. XNYS), or a file")
    r.add_argument("--registry", default=None, help="research registry (default <home>/research/experiments.sqlite)")
    r.add_argument("--research-db", default=None, help="point-in-time store (default <home>/research/research.sqlite)")
    when = r.add_mutually_exclusive_group(required=True)
    when.add_argument("--once", action="store_true", help="decide the latest eligible session now, then exit")
    when.add_argument("--loop", action="store_true", help="decide each eligible session on schedule until stopped")
    r.add_argument("--grace-s", type=int, default=600, help="loop: wait this long after the decision instant")
    r.add_argument("--poll-s", type=float, default=60.0, help="loop: seconds between cycles")
    args = ap.parse_args(argv)
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()

    if args.cmd == "id":
        from ..research.registry import ResearchRegistry
        from .artifacts import artifact_id
        reg = ResearchRegistry.open_readonly(Path(args.registry) if args.registry else paths.root / "research" / "experiments.sqlite")
        try:
            row = reg.conn.execute("SELECT strategy_id FROM artifact WHERE artifact_hash=?", (args.hash,)).fetchone()
        finally:
            reg.close()
        if row is None:
            print(f"artifact {args.hash} is not registered", file=sys.stderr)
            return 2
        print(artifact_id(row["strategy_id"], args.hash))
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .runner import CoreUnreachable, RunnerConfig, RunnerError, StrategyRunner
    try:
        cfg = RunnerConfig(artifact_id=args.artifact, account_key=args.account, mode=args.mode, paths=paths,
                           session_id=args.session, calendar=_calendar(args.calendar),
                           registry_path=Path(args.registry) if args.registry else None,
                           research_path=Path(args.research_db) if args.research_db else None)
        runner = StrategyRunner(cfg)
    except (ArtifactRefused, RunnerError, ValueError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        if args.loop:
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            runner.run_loop(stop, grace_s=args.grace_s, poll_s=args.poll_s)
            return 0
        try:
            summary = runner.run_once()
        except CoreUnreachable as exc:
            print(f"core unreachable: {exc}", file=sys.stderr)
            return 3
        except (ArtifactRefused, RunnerError, CalendarHorizonError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(summary, indent=2, default=str))
        return {"OK": 0, "CORE_UNREACHABLE": 3, "NO_SESSION": 4}[summary["result"]]
    finally:
        runner.close()


if __name__ == "__main__":
    sys.exit(main())
