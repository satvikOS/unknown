"""IBKR as a market-data bus source (ADR-0018): an adapter behind unknown.marketdata.source.MarketDataSource.

It wraps its own read-only ``IbkrMarketDataSession`` (PAPER identity checks, pacing, line budget, entitlement states
of marketdata.py), with an API client id distinct from the order adapter's and from the core's market-data session.
IBKR market-data lines are shared by every API client and TWS on the username, so the owner gives this source an
explicit ``lines`` share in ``<UNKNOWN_HOME>/config/marketdata.json``; the bus refuses keys beyond it.

* Quotes: Level 1 with the type IBKR *delivered* (CAP-MKT-01): live -> REALTIME, delayed / delayed frozen ->
  DELAYED, frozen -> FROZEN. An unavailable, cancelled or interrupted stream becomes a status event (NOT_ENTITLED for
  entitlement errors, STALE for interruptions), never a quote with old values.
* Trades: only from tick-by-tick "AllLast" (reqTickByTickData), and only when the configuration asserts the
  entitlement (``tick_by_tick: true``). IBKR allows simultaneous tick-by-tick subscriptions up to 5% of the
  username's market-data lines (CAP-MKT-03 reference); the source keeps to that. Level 1 'last' ticks are sampled,
  so they never produce trade events.
* Depth: reqMktDepth when ``depth: true`` with the owner's ``depth_lines`` (no default is assumed). Each update
  emits the full side snapshot; error 317 (book reset) clears the book.

Keys are ``<SYMBOL>.<MIC>``; the IBKR primary exchange and currency come from config/markets/venues.json
(``provider_codes.ibkr``). A MIC without an IBKR code, or a core instrument id, is UNAVAILABLE from this source.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path

from ...marketdata.events import InvalidKey, parse_key
from ...marketdata.source import Capabilities, MarketDataSource, Outcome
from .marketdata import ENTITLEMENT_CODES, ContractSpec
from .reconnect import RECONNECT_MAX_S, ReconnectBackoff

log = logging.getLogger("unknown.broker.ibkr")

DATA_TYPES = {"live": "REALTIME", "delayed": "DELAYED", "delayed_frozen": "DELAYED", "frozen": "FROZEN"}
QUOTE_NAMES = {"close": "prev_close"}
DEPTH_RESET = 317
TBT_SHARE_PERCENT = 5


def contract_for_key(key: str, venue_lookup) -> ContractSpec | str:
    """ContractSpec for ``SYMBOL.MIC`` or a reason string."""
    try:
        symbol, mic = parse_key(key)
    except InvalidKey as exc:
        return str(exc)
    if mic is None:
        return "IBKR source needs <SYMBOL>.<MIC> (core instrument ids stream through the core's own session)"
    v = venue_lookup(mic)
    code = (v.provider_codes or {}).get("ibkr") if v is not None else None
    if not code:
        return f"no IBKR exchange code for {mic} in config/markets/venues.json"
    return ContractSpec(symbol=symbol.replace(".", " "), primary_exchange=code, currency=v.currency)


class _TickByTick:
    def __init__(self, src: "IbkrSource", key: str):
        self.src, self.key = src, key

    def on_tick_by_tick(self, time_s, price, size, attrib, exchange, conditions) -> None:
        now = time.time_ns()
        conds = [c for c in (conditions or "").split(",") if c]
        if getattr(attrib, "pastLimit", False):
            conds.append("PAST_LIMIT")
        if getattr(attrib, "unreported", False):
            conds.append("UNREPORTED")
        self.src.sink.trade(self.src.source_id, self.key, price=_dec(price), size=_dec(size), exchange=exchange or None,
                            conditions=conds, data_type="REALTIME", ts_recv_ns=now,
                            ts_source_ns=int(time_s) * 1_000_000_000 if time_s else None, feed="ibkr:tick_by_tick")

    def on_error(self, code, text) -> None:
        self.src._kind_error(self.key, "tick-by-tick", code, text)


class _Depth:
    def __init__(self, src: "IbkrSource", key: str):
        self.src, self.key = src, key
        self.book = {"bid": [], "ask": []}

    def on_depth(self, position, mm, operation, side, price, size) -> None:
        name = "bid" if side == 1 else "ask"           # ibapi: side 0 = ask, 1 = bid
        rows = self.book[name]
        level = {"price": _dec(price), "size": _dec(size), "mm": mm}
        if operation == 0:                              # insert
            rows.insert(min(position, len(rows)), level)
        elif operation == 1 and position < len(rows):   # update
            rows[position] = level
        elif operation == 2 and position < len(rows):   # delete
            rows.pop(position)
        self.src.sink.depth(self.src.source_id, self.key, side=name, levels=list(rows), data_type="REALTIME",
                            ts_recv_ns=time.time_ns())

    def on_error(self, code, text) -> None:
        if int(code) == DEPTH_RESET:
            self.book = {"bid": [], "ask": []}
            for side in ("bid", "ask"):
                self.src.sink.depth(self.src.source_id, self.key, side=side, levels=[], data_type="REALTIME",
                                    ts_recv_ns=time.time_ns())
            return
        self.src._kind_error(self.key, "depth", code, text)


def _dec(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float):
        return repr(v) if v == v and v not in (float("inf"), float("-inf")) else None
    return str(v)


class IbkrSource(MarketDataSource):
    source_id = "IBKR"
    # IBKR reports the delivered type per subscription (marketDataType callback): live, delayed or frozen. Nothing is
    # called REALTIME before IBKR says so; each quote carries the delivered type (ADR-0020).
    data_type = None

    def __init__(self, session, *, lines: int, venue_lookup, tick_by_tick: bool = False, depth: bool = False,
                 depth_lines: int = 0, depth_rows: int = 10, smart_depth: bool = True, maintain_s: float = 5.0,
                 run_thread: bool = True, backoff: ReconnectBackoff | None = None):
        if lines < 1:
            raise ValueError("IBKR source needs lines >= 1 (its share of the username's market-data lines)")
        if depth and depth_lines < 1:
            raise ValueError("depth: true needs depth_lines >= 1 (the owner's entitled depth subscriptions)")
        self.session = session
        self.lines = lines
        self.venue_lookup = venue_lookup
        self.tbt_budget = max(0, lines * TBT_SHARE_PERCENT // 100) if tick_by_tick else 0
        self.depth_budget = depth_lines if depth else 0
        self.depth_rows, self.smart_depth = depth_rows, smart_depth
        self.capabilities = Capabilities(quotes=True, trades=self.tbt_budget > 0, depth=self.depth_budget > 0,
                                         tick_by_tick=self.tbt_budget > 0, delayed=True)
        self.maintain_s = maintain_s
        self._run_thread = run_thread
        self._lock = threading.Lock()
        self._keys: dict[str, ContractSpec] = {}
        self._extra: dict[str, dict] = {}            # key -> {"tbt": req_id, "depth": req_id}
        self._stop = threading.Event()
        self.sink = None
        self.backoff = backoff or ReconnectBackoff()
        self._epoch: int | None = None              # session.connections when last maintained

    def line_budget(self) -> int | None:
        cap = getattr(getattr(self.session, "lines", None), "capacity", None)
        return min(self.lines, cap) if isinstance(cap, int) else self.lines

    # ------------------------------------------------------------------ lifecycle
    def start(self, sink) -> None:
        self.sink = sink
        self.session.add_quote_listener(self._on_quote)
        if self._run_thread:
            threading.Thread(target=self._run, name="md-ibkr", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            wait = self.maintain_s if self.backoff.up else max(self.maintain_s, self.backoff.wait_s())
            self._stop.wait(min(wait, RECONNECT_MAX_S))

    def step(self) -> dict:
        """One maintenance pass: reconnect with capped exponential backoff when IB Gateway is gone (never logs in),
        re-request time & sales and depth after a reconnect (IBKR drops every request with the socket), resubscribe
        quotes, and report the outage on every key meanwhile."""
        try:
            reason = self.session.ready_reason()
            if reason:
                self.backoff.lost(reason)
                if self.backoff.latched or not self.backoff.due():
                    return {"connected": False, "reason": reason}
                self.session.connect()
                reason = self.session.ready_reason()
                if reason:
                    self.backoff.failed(reason)
                    self._all_stale(f"IB Gateway not connected ({reason}); reconnecting with backoff")
                    return {"connected": False, "reason": reason}
            self.backoff.connected()
            reopened = self._reopen_after_reconnect()
            return {"connected": True, "maintenance": self.session.maintain(), "reopened": reopened}
        except Exception as exc:
            log.warning("IBKR market-data source: %s: %s", type(exc).__name__, exc)
            self.backoff.failed(f"{type(exc).__name__}: {exc}")
            self._all_stale(f"session not connected: {type(exc).__name__}")
            return {"connected": False, "reason": f"{type(exc).__name__}: {exc}"}

    def _all_stale(self, why: str) -> None:
        with self._lock:
            keys = list(self._keys)
        for k in keys:
            self.sink.status(self.source_id, k, "STALE", why)

    def _reopen_after_reconnect(self) -> list[str]:
        """After a new verified connection, the tick-by-tick and depth request ids of the old socket are dead:
        unregister them and request each again (quotes are resubscribed by the session's own maintenance)."""
        epoch = getattr(self.session, "connections", 0)
        with self._lock:
            previous, self._epoch = self._epoch, epoch
            if previous is None or previous == epoch:
                return []
            todo = [(k, dict(e)) for k, e in self._extra.items() if e]
            for k, _ in todo:
                self._extra[k] = {}
        reopened = []
        for key, old in todo:
            for req_id in old.values():
                self.session.unregister(req_id)
            with self._lock:
                if key not in self._keys:
                    continue
                extra = self._extra.setdefault(key, {})
            if "tbt" in old:
                self._open(key, extra, "tbt", _TickByTick(self, key), "reqTickByTickData",
                           lambda c: (c, "AllLast", 0, False))
            if "depth" in old:
                self._open(key, extra, "depth", _Depth(self, key), "reqMktDepth",
                           lambda c: (c, self.depth_rows, self.smart_depth, []))
            reopened.append(key)
        return reopened

    # ------------------------------------------------------------------ subscriptions
    def subscribe(self, key: str) -> Outcome:
        spec = contract_for_key(key, self.venue_lookup)
        if isinstance(spec, str):
            return Outcome(False, "UNAVAILABLE", spec)
        r = self.session.subscribe(key, spec)
        if not r.ok:
            reason = r.reason or ""
            state = "LINE_BUDGET" if reason.startswith("LINE_BUDGET") else "UNAVAILABLE"
            return Outcome(False, state, reason)
        with self._lock:
            self._keys[key] = spec
            extra = self._extra.setdefault(key, {})
            n_tbt = sum(1 for e in self._extra.values() if "tbt" in e)
            n_depth = sum(1 for e in self._extra.values() if "depth" in e)
        notes = [r.reason]
        if self.tbt_budget:
            if n_tbt < self.tbt_budget:
                notes.append(self._open(key, extra, "tbt", _TickByTick(self, key), "reqTickByTickData",
                                        lambda c: (c, "AllLast", 0, False)))
            else:
                notes.append(f"time & sales: tick-by-tick budget {self.tbt_budget} in use")
        if self.depth_budget:
            if n_depth < self.depth_budget:
                notes.append(self._open(key, extra, "depth", _Depth(self, key), "reqMktDepth",
                                        lambda c: (c, self.depth_rows, self.smart_depth, [])))
            else:
                notes.append(f"depth: {self.depth_budget} depth subscriptions in use")
        return Outcome(True, "SUBSCRIBED", "; ".join(n for n in notes if n))

    def _open(self, key, extra, kind, handler, method, args) -> str:
        req_id = self.session.new_req_id()
        self.session.register(req_id, handler)
        sdk_contract = self._keys[key].to_sdk(self.session.sdk.Contract)
        err = self.session.request("query", method, req_id, *args(sdk_contract))
        if err:
            self.session.unregister(req_id)
            return f"{kind}: not requested ({err})"
        with self._lock:
            extra[kind] = req_id
        return f"{kind}: requested"

    def unsubscribe(self, key: str) -> None:
        with self._lock:
            self._keys.pop(key, None)
            extra = self._extra.pop(key, {})
        if "tbt" in extra:
            self.session.request("cancel", "cancelTickByTickData", extra["tbt"])
            self.session.unregister(extra["tbt"])
        if "depth" in extra:
            self.session.request("cancel", "cancelMktDepth", extra["depth"], self.smart_depth)
            self.session.unregister(extra["depth"])
        self.session.unsubscribe(key)

    def _kind_error(self, key: str, kind: str, code, text) -> None:
        code = int(code)
        with self._lock:
            extra = self._extra.get(key, {})
            extra.pop("tbt" if kind == "tick-by-tick" else "depth", None)
        # The key keeps streaming quotes, so this is STALE for the part that stopped (NOT_ENTITLED would make the bus
        # fail the whole key over to another source); the reason names the part and the entitlement.
        entitled = "not entitled; " if code in ENTITLEMENT_CODES else ""
        self.sink.status(self.source_id, key, "STALE", f"{kind} unavailable ({entitled}IBKR {code}: {text}); "
                                                       "quotes continue")

    # ------------------------------------------------------------------ quotes (called under the session's cv)
    def _on_quote(self, info: dict) -> None:
        key = info["instrument_id"]
        with self._lock:
            if key not in self._keys:
                return
        if info["unavailable"]:
            u = info["unavailable"]
            state = "NOT_ENTITLED" if u.get("code") in ENTITLEMENT_CODES else "UNAVAILABLE"
            self.sink.status(self.source_id, key, state, f"IBKR {u.get('code')} {u.get('state')}: {u.get('cause', '')}")
            return
        if info["blank"]:
            if info["active"]:
                self.sink.status(self.source_id, key, "STALE", f"stream interrupted: {info['stale'] or 'resubscribe'}"
                                                                "; values discarded (CAP-MKT-01)")
            return
        data_type = DATA_TYPES.get(info["kind"])
        if data_type is None:
            return                                      # type not yet reported: nothing is published as live
        fields = {QUOTE_NAMES.get(k, k): v for k, v in info["fields"].items()}
        self.sink.quote(self.source_id, key, fields, data_type=data_type, ts_recv_ns=info["receipt_ns"],
                        currency=info["currency"], feed=f"ibkr:{info['kind']}", replace=True)

    def describe(self) -> dict:
        with self._lock:
            n = len(self._keys)
        conn = self.session.connection_state()
        reconnect = self.backoff.to_json()
        if conn.get("connected") and conn.get("verified_paper"):
            state = "upstream_down" if conn.get("upstream_down") is not None else "running"
        else:
            state = reconnect["state"] if reconnect["state"] != "connected" else "reconnecting"
        return {**super().describe(), "state": state, "keys": n, "tick_by_tick_budget": self.tbt_budget,
                "depth_budget": self.depth_budget, "reason": conn.get("reason") if state != "running" else None,
                "upstream_down": conn.get("upstream_down"), "data_farms_down": conn.get("data_farms_down"),
                "reconnect": reconnect}


def build(opts: dict, home: Path) -> IbkrSource:
    """Factory for config.FACTORIES['IBKR']; options: client_id, lines, tick_by_tick, depth, depth_lines, depth_rows."""
    from ...marketdata.config import venue
    from .config import load_config
    from .marketdata import IbkrMarketDataSession, load_market_data_config
    cfg = load_config(opts.get("ibkr_config"))
    md = load_market_data_config(home)
    if "client_id" not in opts:
        raise ValueError("IBKR source needs its own 'client_id' (distinct from the order adapter's and the core's "
                         "market-data session)")
    if int(opts["client_id"]) == md.client_id:
        raise ValueError("IBKR source client_id must differ from the core's market-data session client_id")
    md = dataclasses.replace(md, client_id=int(opts["client_id"]))
    session = IbkrMarketDataSession(cfg, md)
    return IbkrSource(session, lines=int(opts["lines"]), venue_lookup=venue,
                      tick_by_tick=bool(opts.get("tick_by_tick", False)), depth=bool(opts.get("depth", False)),
                      depth_lines=int(opts.get("depth_lines", 0)), depth_rows=int(opts.get("depth_rows", 10)),
                      smart_depth=bool(opts.get("smart_depth", True)))
