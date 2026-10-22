"""Core service entry point for the IBKR PAPER adapter (ADR-0012, ADR-0014).

    <ibkr venv>/bin/python -m unknown.broker.ibkr.serve --account IBKR-PAPER-1 --deployment mac-dev

The dependency points from the adapter to the core: this module builds the
GPL TWS API adapter and hands it to the generic ``CoreService``; the core never
imports the adapter. The configuration (with the paper account id) is read from
outside the repository (default ``~/.unknown/ibkr.json``). The service
reconnects with backoff but never logs in for the owner: the owner logs in to
IB Gateway/TWS, and an identity mismatch latches DISCONNECTED_UNSAFE.

Market data (docs/integrations/ibkr-marketdata.md) runs on a second, read-only
connection with its own client id when ``<UNKNOWN_HOME>/config/ibkr_marketdata.json``
exists. The two connections split IBKR's per-username request budget. A
maintenance thread connects that session, subscribes every instrument with an
``ibkr`` provider mapping, and publishes into the core's quote book. Without the
file there are no quotes, and pretrade and preflight fail closed."""
from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path

from ...domain.clock import SystemClock
from ...market.quotes import QuoteBook
from ...service.core import CoreService, serve
from ...service.paths import Paths
from .adapter import PROVIDER, IbkrAdapter
from .config import load_config
from .marketdata import (ContractSpec, IbkrMarketDataSession, MarketDataConfigError, default_status_path,
                         load_market_data_config, split_host_budget)

log = logging.getLogger("unknown.ibkr.serve")
MAINTAIN_INTERVAL_S = 30.0
RECONNECT_MAX_S = 300.0


class MarketDataMaintainer:
    """Keeps the read-only market-data session connected and subscribed. It runs on its own thread, never on the
    core loop or the SDK message thread."""

    def __init__(self, session: IbkrMarketDataSession, svc: CoreService):
        self.session = session
        self.svc = svc
        self.stop = threading.Event()
        self._backoff = 5.0

    def step(self) -> dict:
        state = self.session.connection_state()
        if not (state.get("connected") and state.get("verified_paper")):
            if str(state.get("reason") or "").startswith("DISCONNECTED_UNSAFE"):
                return {"connected": False, "reason": state.get("reason")}   # latched; only the owner clears it
            self.session.connect()
            state = self.session.connection_state()
            if not (state.get("connected") and state.get("verified_paper")):
                return {"connected": False, "reason": state.get("reason")}
        subscribed, refused = [], {}
        for con_id, (instrument_id, currency) in sorted(self.svc.instrument_details.items()):
            if not con_id.isdigit():
                continue
            result = self.session.subscribe(instrument_id, ContractSpec(con_id=int(con_id), currency=currency))
            if result.ok:
                subscribed.append(instrument_id)
            else:
                refused[instrument_id] = result.reason
        return {"connected": True, "subscribed": subscribed, "refused": refused, "maintenance": self.session.maintain()}

    def run(self) -> None:
        self.svc.ready.wait()   # instrument mappings come from the core database, which svc.run opens
        while not self.stop.is_set():
            try:
                out = self.step()
                wait = MAINTAIN_INTERVAL_S if out.get("connected") else self._backoff
                self._backoff = 5.0 if out.get("connected") else min(self._backoff * 2, RECONNECT_MAX_S)
            except Exception:
                log.exception("market-data maintenance failed")
                wait = self._backoff
                self._backoff = min(self._backoff * 2, RECONNECT_MAX_S)
            self.stop.wait(wait)


def build_service(*, account: str, deployment: str, config_path: str | None = None, home: str | None = None
                  ) -> tuple[CoreService, MarketDataMaintainer | None]:
    clock = SystemClock()
    cfg = load_config(config_path)
    paths = Paths(Path(home).expanduser()) if home else Paths.default()
    holder: list[CoreService] = []

    def resolve(con_id: int) -> str | None:
        # Called on the adapter's message thread: read the service's copied map, never SQLite.
        return holder[0].instrument_map.get(str(con_id)) if holder else None

    try:
        md_cfg = load_market_data_config(paths.root)
    except MarketDataConfigError as exc:
        log.warning("no market data: %s (orders validate only against quotes, so entries will fail closed)", exc)
        md_cfg = None
    quotes = QuoteBook()
    order_pacer, md_pacer = split_host_budget(cfg, md_cfg) if md_cfg is not None else (None, None)
    adapter = IbkrAdapter(cfg, instrument_resolver=resolve, clock=clock, pacer=order_pacer)
    svc = CoreService(paths=paths, account_key=account, deployment_id=deployment, broker=adapter, clock=clock,
                      quotes=quotes)
    svc.broker_mapping_provider = PROVIDER
    holder.append(svc)
    maintainer = None
    if md_cfg is not None:
        session = IbkrMarketDataSession(cfg, md_cfg, quote_book=quotes, clock=clock, pacer=md_pacer,
                                        status_path=default_status_path(paths.root))
        maintainer = MarketDataMaintainer(session, svc)
    return svc, maintainer


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Project Unknown core service with the IBKR PAPER adapter")
    ap.add_argument("--account", required=True, help="internal account key, e.g. IBKR-PAPER-1")
    ap.add_argument("--deployment", required=True, help="deployment id, e.g. mac-dev")
    ap.add_argument("--config", default=None, help="IBKR config (default ~/.unknown/ibkr.json; must be outside git)")
    ap.add_argument("--home", default=None, help="runtime root (default ~/.unknown or $UNKNOWN_HOME)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    svc, maintainer = build_service(account=args.account, deployment=args.deployment, config_path=args.config,
                                    home=args.home)
    if maintainer is not None:
        threading.Thread(target=maintainer.run, name="ibkr-marketdata", daemon=True).start()
    serve(svc)


if __name__ == "__main__":
    main()
