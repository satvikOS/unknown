"""Read-only IBKR PAPER connectivity check (owner command, run before ``bootstrap_paper --dry-run``).

    <ibkr venv>/bin/python -m unknown.broker.ibkr.check --account IBKR-PAPER-1 [--client-id 90] [--symbol AAPL:NASDAQ]

It opens ONE TWS API connection with a dedicated client id (never the order adapter's or the market-data
connection's, so it can run while ``serve`` is up), and reports:

1. the PAPER identity: configured paper port and ``managedAccounts`` == the expected paper account (R2), the same
   check the order adapter and the market-data session make; a mismatch latches unsafe and nothing else is sent;
2. the host's server version (``EClient.serverVersion``);
3. clock skew from ``reqCurrentTimeInMillis`` (server version >= 197), taken at the midpoint of the round trip,
   against the paper policy's ``max_clock_skew_ms``;
4. the market-data type IBKR *delivered* for one streaming AAPL request (``EWrapper.marketDataType``) and whether
   real-time data is entitled (type 1 with no delayed tick ids); an entitlement error is reported by code;

then cancels the request and disconnects. It places, modifies and cancels no order, sends nothing to the core,
writes no status file and stores no price (quotes go to a private in-memory book that is discarded).

SDK names used beyond those of ``marketdata.IbkrMarketDataSession``: ``EClient.serverVersion``,
``EClient.reqCurrentTimeInMillis`` and ``EWrapper.currentTimeInMillis`` (TWS API 10.51.01, the same calls
``adapter.IbkrAdapter`` makes for its preflight clock check).

Status (ADR-0001): implemented and tested offline against the fake IBKR host; not yet run against IB Gateway."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from ...market.quotes import QuoteBook
from .adapter import MIN_SERVER_VER_CURRENT_TIME_IN_MILLIS
from .config import IbkrConfigError, load_config
from .marketdata import (DELAYED_TYPES, MARKET_DATA_TYPES, ContractSpec, IbkrMarketDataSession, MarketDataConfigError,
                         load_market_data_config)

REPO = Path(__file__).resolve().parents[5]
PAPER_POLICY = REPO / "config" / "policies" / "paper-default.v1.json"
DEFAULT_CHECK_CLIENT_ID = 90
CHECK_INSTRUMENT = "CHECK:{symbol}"
MAX_RTT_MS = 1000                     # a slower round trip is not a usable clock sample


class CheckSession(IbkrMarketDataSession):
    """The read-only market-data session plus one clock query. Adds no order or account-data call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        base = type(self._wrapper)

        class CheckWrapper(base):
            def currentTimeInMillis(self, timeInMillis: int):
                self._session._dispatch("currentTimeInMillis", timeInMillis)

        self._wrapper = CheckWrapper(self)
        self._time_sent: tuple[int, int] | None = None
        self._time_sample: dict | None = None

    def server_version(self) -> int | None:
        with self.cv:
            client = self._client
        try:
            v = client.serverVersion() if client is not None else None
            return int(v) if v is not None else None
        except Exception:
            return None

    def _on_currentTimeInMillis(self, time_in_millis) -> None:
        with self.cv:
            mono, wall = self._clock.monotonic_ns(), self._clock.now_ns()
            sent, self._time_sent = self._time_sent, None
            if sent is None:
                self._anomaly("SERVER_TIME_UNSOLICITED", "currentTimeInMillis without a pending query; ignored")
                return
            rtt = mono - sent[1]
            local_mid = sent[0] + rtt // 2              # the host read its clock inside the round trip
            self._time_sample = {"server_ms": int(time_in_millis), "rtt_ms": rtt // 1_000_000,
                                 "skew_ms": (int(time_in_millis) * 1_000_000 - local_mid) // 1_000_000,
                                 "local_received_ns": wall}
            self.cv.notify_all()

    def measure_clock(self, timeout_s: float) -> dict:
        version = self.server_version()
        if version is None or version < MIN_SERVER_VER_CURRENT_TIME_IN_MILLIS:
            return {"ok": False, "skew_ms": None,
                    "reason": f"server version {version} < {MIN_SERVER_VER_CURRENT_TIME_IN_MILLIS}: reqCurrentTimeInMillis "
                              "is unavailable, so clock skew is unknown (preflight fails closed)"}
        with self.cv:
            self._time_sample, self._time_sent = None, (self._clock.now_ns(), self._clock.monotonic_ns())
        err = self.request("query", "reqCurrentTimeInMillis")
        if err:
            return {"ok": False, "skew_ms": None, "reason": err}
        if not self.wait(lambda: self._time_sample is not None, timeout_s):
            return {"ok": False, "skew_ms": None, "reason": f"no currentTimeInMillis within {timeout_s}s"}
        sample = dict(self._time_sample)
        if sample["rtt_ms"] > MAX_RTT_MS:
            return {"ok": False, **sample, "skew_ms": None, "reason": f"round trip {sample['rtt_ms']} ms is too slow"}
        return {"ok": True, **sample, "reason": None}


def _entitlement(sub: dict | None) -> tuple[bool | None, str]:
    if sub is None:
        return None, "no subscription record"
    t, unavailable = sub.get("delivered_type"), sub.get("unavailable")
    if unavailable:
        return False, f"unavailable: {unavailable}"
    if t == 1:
        if sub.get("live"):
            return True, "IBKR delivered type 1 (live)"
        return False, f"type 1 reported but status {sub.get('status')} (delayed tick ids or a stale connection)"
    if t in DELAYED_TYPES:
        return False, (f"IBKR delivered type {t} ({MARKET_DATA_TYPES[t]}): this account is not entitled to real-time "
                       "data for the listing (share the live account's market data with the paper account)")
    if t == 2:
        return None, "IBKR delivered type 2 (frozen; usually the market is closed): real-time entitlement not proven"
    return None, f"no marketDataType report (status {sub.get('status')})"


def run_check(cfg, md_cfg, *, account_key: str, client_id: int = DEFAULT_CHECK_CLIENT_ID, symbol: str = "AAPL",
              primary_exchange: str = "NASDAQ", wait_s: float = 10.0, max_clock_skew_ms: int | None = None,
              sdk=None, client_factory=None, clock=None, out=print) -> dict:
    """Connect, verify, measure, disconnect. Returns the report; ``report['ok']`` is True only if every check passed."""
    if client_id in (cfg.client_id, md_cfg.client_id):
        raise ValueError(f"client id {client_id} is in use by the order adapter ({cfg.client_id}) or the market-data "
                         f"connection ({md_cfg.client_id}); pass a dedicated --client-id")
    check_md = replace(md_cfg, client_id=client_id)
    kwargs = {"quote_book": QuoteBook(), "status_path": None}
    if sdk is not None:
        kwargs["sdk"] = sdk
    if client_factory is not None:
        kwargs["client_factory"] = client_factory
    if clock is not None:
        kwargs["clock"] = clock
    session = CheckSession(cfg, check_md, **kwargs)
    report: dict = {"account_key": account_key, "account": cfg.masked_account(), "client_id": client_id,
                    "host": f"{cfg.host}:{cfg.port}", "places_orders": False, "writes_to_core": False}
    out("IBKR paper connectivity check (read-only: places nothing, writes nothing to the core)")
    try:
        state = session.connect()
        report["identity"] = {"ok": bool(state.get("verified_paper")), "reason": state.get("reason")}
        out(f"account        {account_key} -> {cfg.masked_account()} at {cfg.host}:{cfg.port}, client id {client_id}: "
            + ("PAPER verified (paper port, managedAccounts match)" if report["identity"]["ok"]
               else f"NOT VERIFIED: {state.get('reason')}"))
        if not report["identity"]["ok"]:
            report["ok"] = False
            out("result         FAIL (nothing else was requested)")
            return report
        version = session.server_version()
        report["server_version"] = version
        out(f"server version {version if version is not None else 'unknown'}")
        clock_r = session.measure_clock(cfg.request_timeout_s)
        if clock_r["ok"] and max_clock_skew_ms is not None and abs(clock_r["skew_ms"]) > max_clock_skew_ms:
            clock_r = {**clock_r, "ok": False, "reason": f"|skew| > {max_clock_skew_ms} ms (paper policy limit)"}
        clock_r["limit_ms"] = max_clock_skew_ms
        report["clock"] = clock_r
        out("clock skew     " + (f"{clock_r['skew_ms']:+d} ms (round trip {clock_r['rtt_ms']} ms; limit "
                                 f"{max_clock_skew_ms} ms): OK" if clock_r["ok"] else f"FAIL: {clock_r['reason']}"))
        instrument = CHECK_INSTRUMENT.format(symbol=symbol)
        sub_r = session.subscribe(instrument, ContractSpec(symbol=symbol, primary_exchange=primary_exchange))
        sub = None
        if sub_r.ok:
            session.wait(lambda: (lambda s: s is not None and (s["delivered_type"] is not None or s["unavailable"]))(
                session.subscription(instrument)), wait_s)
            sub = session.subscription(instrument)
            session.unsubscribe(instrument)
        entitled, why = _entitlement(sub) if sub_r.ok else (None, sub_r.reason)
        report["market_data"] = {"symbol": symbol, "primary_exchange": primary_exchange,
                                 "requested_type": MARKET_DATA_TYPES[md_cfg.requested_market_data_type],
                                 "delivered_type": sub.get("delivered_type") if sub else None,
                                 "delivered_type_name": sub.get("delivered_type_name") if sub else None,
                                 "status": sub.get("status") if sub else None,
                                 "realtime_entitled": entitled, "reason": why}
        out(f"market data    {symbol} ({primary_exchange}): delivered "
            f"{report['market_data']['delivered_type_name'] or 'nothing'} (requested "
            f"{report['market_data']['requested_type']}); real-time entitled: "
            f"{ {True: 'YES', False: 'NO', None: 'UNKNOWN'}[entitled] } - {why}")
        report["ok"] = bool(report["identity"]["ok"] and clock_r["ok"] and entitled is True)
        out("result         " + ("PASS" if report["ok"] else "FAIL"))
        return report
    finally:
        session.disconnect()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m unknown.broker.ibkr.check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--account", required=True, help="internal account key, e.g. IBKR-PAPER-1 (label only)")
    ap.add_argument("--client-id", type=int, default=DEFAULT_CHECK_CLIENT_ID,
                    help=f"dedicated API client id for this check (default {DEFAULT_CHECK_CLIENT_ID})")
    ap.add_argument("--symbol", default="AAPL:NASDAQ", help="SYMBOL:PRIMARY_EXCHANGE for the market-data probe")
    ap.add_argument("--wait-s", type=float, default=10.0, help="seconds to wait for IBKR's marketDataType report")
    ap.add_argument("--config", default=None, help="IBKR config (default ~/.unknown/ibkr.json)")
    ap.add_argument("--home", default=None, help="runtime root holding config/ibkr_marketdata.json")
    ap.add_argument("--json", action="store_true", help="also print the report as JSON")
    args = ap.parse_args(argv)
    symbol, _, primary = args.symbol.partition(":")
    if not symbol or not primary:
        print("--symbol must be SYMBOL:PRIMARY_EXCHANGE, e.g. AAPL:NASDAQ", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
        md_cfg = load_market_data_config(Path(args.home) if args.home else None)
        skew = json.loads(PAPER_POLICY.read_text())["account_limits"]["max_clock_skew_ms"]
        report = run_check(cfg, md_cfg, account_key=args.account, client_id=args.client_id, symbol=symbol.upper(),
                           primary_exchange=primary.upper(), wait_s=args.wait_s, max_clock_skew_ms=skew)
    except (IbkrConfigError, MarketDataConfigError, ValueError, ImportError) as exc:
        print(f"check not run: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
