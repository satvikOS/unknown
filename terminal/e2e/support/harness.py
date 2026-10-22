"""Test-only launchers for the terminal end-to-end suite. Never used outside `npm run test:e2e`.

  python harness.py core --broker-state FILE -- <unknown.service.core arguments>
  python harness.py api --step-up-window-s N -- <unknown.api.server arguments>
  python harness.py md-status [--age-s N]

Both run the real entry points (`unknown.service.core.main`, `unknown.api.server.main`) unchanged
except for one seam each, so the suite exercises the real core, API, socket, database and terminal:

core: the simulated broker lives inside the core process, so restarting the core would make the
    broker forget every order, fill and cash movement, while the ledger remembers them. A real
    broker does not forget the account when our process restarts. To model that, the launcher
    pickles the simulator after a clean SIGTERM shutdown and restores it on the next start (the
    deterministic FIXTURE quote path restarts from its seed; only the broker's book persists).

api: the API's step-up window is the active policy's reauth_max_age_s (900 s in the default
    simulated policy), or 15 minutes without a policy. The suite caps it with
    server.STEP_UP_MAX_AGE_NS, which can only shorten the window, so the HTTP 428 -> passphrase ->
    same-command-id retry path can be exercised in seconds. The core's own policy check
    (reauth_max_age_s) is untouched.

md-status: the IBKR market-data status file (<UNKNOWN_HOME>/run/ibkr-marketdata-status.json) is written
    only by a running IbkrMarketDataSession, and the suite never connects to IBKR. This writes one with the
    real session code driven by the backend's own socket stand-in (tests/test_ibkr_marketdata.py PureHost):
    FIXTURE instruments, a fixture paper account id, and IBKR's documented callbacks for a live
    non-consolidated quote, a delayed quote, entitlement errors 354 and 10197 and warning 10090. ``--age-s``
    back-dates the session clock so the API sees a stale status file. No price value is written (the
    session never writes prices to the file).

Reads the pickle it wrote itself; the file lives in the suite's private runtime directory."""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from pathlib import Path


def run_core(state: Path, argv: list[str]) -> None:
    from unknown.service import core as core_mod

    original_factory = core_mod._simulated_broker
    original_serve = core_mod.serve

    def broker_factory(clock, cash):
        if state.exists():
            broker = pickle.loads(state.read_bytes())
            broker._clock = clock
            print(f"[e2e harness] restored simulated broker book from {state}", flush=True)
            return broker
        return original_factory(clock, cash)

    def serve(svc) -> None:
        try:
            original_serve(svc)
        finally:
            # svc.run() has returned: the loop thread is done with the broker.
            tmp = state.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(svc.broker))
            os.replace(tmp, state)
            print(f"[e2e harness] saved simulated broker book to {state}", flush=True)

    core_mod._simulated_broker = broker_factory
    core_mod.serve = serve
    core_mod.main(argv)


def run_api(window_s: int, argv: list[str]) -> None:
    from unknown.api import server

    server.STEP_UP_MAX_AGE_NS = window_s * 1_000_000_000
    print(f"[e2e harness] API step-up window set to {window_s} s for this test run", flush=True)
    server.main(argv)


def write_md_status(age_s: int) -> None:
    import time
    from decimal import Decimal

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend" / "tests"))
    import test_ibkr_marketdata as fx  # the backend's fixture socket host, checked against the pinned SDK
    from unknown.broker.ibkr import marketdata as MD
    from unknown.broker.ibkr.config import config_from_dict
    from unknown.broker.ibkr.pacing import PacingScheduler
    from unknown.domain.clock import ManualClock
    from unknown.market.quotes import QuoteBook
    from unknown.service.paths import Paths

    paths = Paths.default()
    clock = ManualClock(time.time_ns() - age_s * 1_000_000_000)
    hosts: list = []

    def factory(wrapper):
        hosts.append(fx.PureHost(wrapper))
        return hosts[-1]

    cfg = config_from_dict(fx.base_config(paths.root / "ibkr-state"))
    md = MD.market_data_config_from_dict(fx.md_config())
    pacer = PacingScheduler(1000, 20, monotonic_ns=clock.monotonic_ns, sleep=lambda s: None)
    session = MD.IbkrMarketDataSession(cfg, md, quote_book=QuoteBook(), clock=clock, client_factory=factory,
                                       sdk=fx.PURE_SDK, pacer=pacer, status_path=paths.run / MD.STATUS_FILE_NAME)
    state = session.connect()
    if not state["verified_paper"]:
        raise SystemExit(f"fixture session did not verify: {state}")
    w = hosts[-1].wrapper
    attrib = fx.attrib()

    def sub(instrument: str, con_id: int) -> int:
        r = session.subscribe(instrument, MD.ContractSpec(con_id=con_id))
        if not r.ok:
            raise SystemExit(f"subscribe {instrument}: {r.reason}")
        return r.req_id

    live = sub("FIX-MD-LIVE", 900101)          # live, non-consolidated (the owner's feed label)
    w.marketDataType(live, 1)
    w.tickPrice(live, 1, 10.01, attrib)
    w.tickSize(live, 0, Decimal("300"))
    w.tickPrice(live, 2, 10.03, attrib)
    w.tickSize(live, 3, Decimal("200"))
    delayed = sub("FIX-MD-DELAYED", 900102)    # IBKR delivered delayed data although live was requested
    w.marketDataType(delayed, 3)
    w.tickPrice(delayed, 66, 20.10, attrib)
    w.tickPrice(delayed, 67, 20.14, attrib)
    nosub = sub("FIX-MD-NOSUB", 900103)        # error 354: not subscribed (terminal for the subscription)
    w.error(nosub, 0, 354, "Requested market data is not subscribed. (FIXTURE text)", "")
    compete = sub("FIX-MD-COMPETE", 900104)    # error 10197: competing live session
    w.marketDataType(compete, 1)
    w.tickPrice(compete, 1, 30.00, attrib)
    w.error(compete, 0, 10197, "No market data during competing live session (FIXTURE text)", "")
    partial = sub("FIX-MD-PARTIAL", 900105)    # warning 10090: part of the data is not subscribed
    w.marketDataType(partial, 1)
    w.tickPrice(partial, 1, 40.00, attrib)
    w.tickPrice(partial, 2, 40.02, attrib)
    w.error(partial, 0, 10090, "Part of requested market data is not subscribed (FIXTURE text)", "")
    path = session.maintain() and paths.run / MD.STATUS_FILE_NAME
    print(f"[e2e harness] wrote FIXTURE market-data status to {path} (clock {age_s} s behind)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="which", required=True)
    c = sub.add_parser("core")
    c.add_argument("--broker-state", required=True)
    a = sub.add_parser("api")
    a.add_argument("--step-up-window-s", type=int, required=True)
    m = sub.add_parser("md-status")
    m.add_argument("--age-s", type=int, default=0)
    args, rest = ap.parse_known_args()
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if args.which == "core":
        run_core(Path(args.broker_state), rest)
    elif args.which == "md-status":
        write_md_status(args.age_s)
    else:
        run_api(args.step_up_window_s, rest)


if __name__ == "__main__":
    sys.exit(main())
