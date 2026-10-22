"""Core service entry point for the IBKR PAPER adapter (ADR-0012, ADR-0014).

    <ibkr venv>/bin/python -m unknown.broker.ibkr.serve --account IBKR-PAPER-1 --deployment mac-dev

The dependency points from the adapter to the core: this module builds the
GPL TWS API adapter and hands it to the generic ``CoreService``; the core never
imports the adapter. The configuration (with the paper account id) is read from
outside the repository (default ``~/.unknown/ibkr.json``). The service
reconnects with backoff but never logs in for the owner: the owner logs in to
IB Gateway/TWS, and an identity mismatch latches DISCONNECTED_UNSAFE."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ...domain.clock import SystemClock
from ...service.core import CoreService, serve
from ...service.paths import Paths
from .adapter import PROVIDER, IbkrAdapter
from .config import load_config


def build_service(*, account: str, deployment: str, config_path: str | None = None, home: str | None = None) -> CoreService:
    clock = SystemClock()
    cfg = load_config(config_path)
    holder: list[CoreService] = []

    def resolve(con_id: int) -> str | None:
        # Called on the adapter's message thread: read the service's copied map, never SQLite.
        return holder[0].instrument_map.get(str(con_id)) if holder else None

    adapter = IbkrAdapter(cfg, instrument_resolver=resolve, clock=clock)
    paths = Paths(Path(home).expanduser()) if home else Paths.default()
    svc = CoreService(paths=paths, account_key=account, deployment_id=deployment, broker=adapter, clock=clock)
    svc.broker_mapping_provider = PROVIDER
    holder.append(svc)
    return svc


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Project Unknown core service with the IBKR PAPER adapter")
    ap.add_argument("--account", required=True, help="internal account key, e.g. IBKR-PAPER-1")
    ap.add_argument("--deployment", required=True, help="deployment id, e.g. mac-dev")
    ap.add_argument("--config", default=None, help="IBKR config (default ~/.unknown/ibkr.json; must be outside git)")
    ap.add_argument("--home", default=None, help="runtime root (default ~/.unknown or $UNKNOWN_HOME)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    serve(build_service(account=args.account, deployment=args.deployment, config_path=args.config, home=args.home))


if __name__ == "__main__":
    main()
