"""Alpaca real-time stock data as a market-data bus source (market data only; no trading API is used).

Wire format, verified 2026-10-23 on docs.alpaca.markets (docs/integrations/alpaca.md):

* URL ``wss://stream.data.alpaca.markets/v2/{feed}`` with feed ``iex``, ``sip`` or ``delayed_sip``
  (https://docs.alpaca.markets/docs/real-time-stock-pricing-data).
* Every server message is a JSON array; control messages have one element: ``[{"T":"success","msg":"connected"}]``,
  ``[{"T":"success","msg":"authenticated"}]``, ``[{"T":"subscription","trades":[...],"quotes":[...],...}]``,
  ``[{"T":"error","code":406,"msg":"connection limit exceeded"}]``
  (https://docs.alpaca.markets/docs/streaming-market-data).
* Client messages: ``{"action":"auth","key":...,"secret":...}``, ``{"action":"subscribe","trades":[...],
  "quotes":[...]}`` and ``unsubscribe`` with the same shape.
* Quote ``T=q``: S, bx, bp, bs, ax, ap, as, c, t (RFC-3339, nanosecond precision), z. Trade ``T=t``: S, i, x, p, s,
  c, t, z. Quote sizes are documented as round lots; events carry ``size_unit = "round_lots"``.
* Error codes 400-410 and 500 (table in streaming-market-data); 405 "symbol limit exceeded" leaves existing
  subscriptions unchanged.
* Plans (https://docs.alpaca.markets/docs/about-market-data-api): Basic covers IEX in real time with "30 symbols"
  of websocket subscriptions; Algo Trader Plus covers all US exchanges with unlimited symbols. That is this source's
  line budget (``symbol_limit`` in the configuration overrides it).

Keys come only from ``<UNKNOWN_HOME>/secrets/alpaca.json`` (``{"key_id": ..., "secret_key": ...}``) when present and
are never logged. Without them the source stays configured and refuses keys with that reason, so the bus falls back.
Decimals are kept as the exact JSON text (``parse_float=str``); nothing becomes a float.
"""
from __future__ import annotations

import calendar
import json
import logging
import threading
import time
from pathlib import Path

from ...marketdata.events import InvalidKey, parse_key
from ...marketdata.source import Capabilities, MarketDataSource, Outcome
from . import ws

log = logging.getLogger("unknown.broker.alpaca")

STREAM = "wss://stream.data.alpaca.markets/v2/"
FEEDS = {  # feed: (data_type, Basic-plan symbol limit or None, consolidation label for CAP-MKT-02)
    "iex": ("REALTIME", 30, "single_venue:IEX"),
    "sip": ("REALTIME", None, "consolidated:SIP"),
    "delayed_sip": ("DELAYED", 30, "consolidated:SIP:15min_delayed"),
}
ERRORS = {
    400: ("UNAVAILABLE", "invalid syntax"), 401: ("UNAVAILABLE", "not authenticated"),
    402: ("UNAVAILABLE", "auth failed"), 403: ("UNAVAILABLE", "already authenticated"),
    404: ("UNAVAILABLE", "auth timeout"), 405: ("LINE_BUDGET", "symbol limit exceeded"),
    406: ("UNAVAILABLE", "connection limit exceeded"), 407: ("STALE", "slow client"),
    409: ("NOT_ENTITLED", "insufficient subscription"), 410: ("NOT_ENTITLED", "invalid subscribe action for this feed"),
    500: ("UNAVAILABLE", "internal error"),
}
SECRETS_FILE = "alpaca.json"


def rfc3339_ns(text: str | None) -> int | None:
    """'2021-02-22T15:51:45.335689322Z' -> integer ns since the epoch (Z or +HH:MM offsets)."""
    if not text:
        return None
    t = text.strip()
    offset = 0
    if t.endswith("Z") or t.endswith("z"):
        t = t[:-1]
    elif len(t) > 6 and t[-6] in "+-" and t[-3] == ":":
        sign = 1 if t[-6] == "+" else -1
        offset = sign * (int(t[-5:-3]) * 3600 + int(t[-2:]) * 60)
        t = t[:-6]
    date_part, _, time_part = t.replace("t", "T").partition("T")
    hms, _, frac = time_part.partition(".")
    y, mo, d = (int(x) for x in date_part.split("-"))
    h, mi, s = (int(x) for x in hms.split(":"))
    if not frac.isdigit() and frac:
        raise ValueError(f"bad fraction in {text!r}")
    nanos = int((frac + "000000000")[:9]) if frac else 0
    return (calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - offset) * 1_000_000_000 + nanos


def parse_messages(text: str) -> list[dict]:
    data = json.loads(text, parse_float=str)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Alpaca stream message is not a JSON array")
    return [m for m in data if isinstance(m, dict)]


def _num(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError("boolean where a number was expected")
    return str(v)


def load_credentials(home: Path) -> dict | None:
    path = Path(home) / "secrets" / SECRETS_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("key_id") or not data.get("secret_key"):
        raise ValueError(f"{path.name} must hold key_id and secret_key")
    return {"key_id": str(data["key_id"]), "secret_key": str(data["secret_key"])}


class AlpacaSource(MarketDataSource):
    source_id = "ALPACA"

    def __init__(self, *, feed: str = "iex", credentials: dict | None, venue_lookup, url: str | None = None,
                 symbol_limit: int | None | str = "plan", connect=ws.connect, backoff_max_s: float = 30.0,
                 run_thread: bool = True, time_ns=time.time_ns):
        if feed not in FEEDS:
            raise ValueError(f"Alpaca feed must be one of {sorted(FEEDS)}")
        self.feed = feed
        self.data_type, plan_limit, self.feed_label = FEEDS[feed]
        self.symbol_limit = plan_limit if symbol_limit == "plan" else symbol_limit
        self.url = url or STREAM + feed
        self.capabilities = Capabilities(quotes=True, trades=True, depth=False, tick_by_tick=True,
                                         delayed=self.data_type == "DELAYED")
        self._credentials = credentials
        self._venue = venue_lookup
        self._connect = connect
        self._backoff_max = backoff_max_s
        self._run_thread = run_thread
        self._time_ns = time_ns
        self._lock = threading.Lock()
        self._wanted: dict[str, str] = {}          # symbol -> key
        self._last_request: list[str] = []
        self._ws = None
        self._authenticated = False
        self._stop = threading.Event()
        self.state = "idle"
        self.last_error: str | None = None
        self.sink = None

    def line_budget(self) -> int | None:
        return self.symbol_limit

    # ------------------------------------------------------------------ subscriptions
    def subscribe(self, key: str) -> Outcome:
        try:
            symbol, mic = parse_key(key)
        except InvalidKey as exc:
            return Outcome(False, "UNAVAILABLE", str(exc))
        v = self._venue(mic) if mic else None
        if v is None or v.country != "US":
            return Outcome(False, "UNAVAILABLE", f"Alpaca streams US stocks and ETFs only; {key} is not a US venue key")
        if self._credentials is None:
            return Outcome(False, "UNAVAILABLE", f"no Alpaca API keys at <UNKNOWN_HOME>/secrets/{SECRETS_FILE}")
        with self._lock:
            other = self._wanted.get(symbol)
            if other is not None and other != key:
                return Outcome(False, "UNAVAILABLE", f"{symbol} already streams as {other}")
            self._wanted[symbol] = key
            live = self._authenticated
        if live:
            self._request("subscribe", [symbol])
        return Outcome(True, "SUBSCRIBED", f"Alpaca {self.feed} feed ({self.feed_label})"
                                           + ("" if live else "; connecting"))

    def unsubscribe(self, key: str) -> None:
        symbol = key.rsplit(".", 1)[0]
        with self._lock:
            if self._wanted.get(symbol) != key:
                return
            del self._wanted[symbol]
            live = self._authenticated
        if live:
            self._request("unsubscribe", [symbol])

    def _request(self, action: str, symbols: list[str]) -> None:
        with self._lock:
            conn = self._ws
            if action == "subscribe":
                self._last_request = list(symbols)
        if conn is None:
            return
        try:
            conn.send_text(json.dumps({"action": action, "trades": symbols, "quotes": symbols}))
        except OSError as exc:
            self.last_error = f"send failed: {type(exc).__name__}"

    # ------------------------------------------------------------------ connection
    def start(self, sink) -> None:
        self.sink = sink
        if self._run_thread and self._credentials is not None:
            threading.Thread(target=self._run, name="md-alpaca", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            conn = self._ws
        if conn is not None:
            conn.close()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.run_session()
                backoff = 1.0
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Alpaca stream: %s", self.last_error)
            self._down(self.last_error or "disconnected")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    def _down(self, why: str) -> None:
        with self._lock:
            self._ws, self._authenticated, self.state = None, False, "disconnected"
            keys = list(self._wanted.values())
        for key in keys:
            self.sink.status(self.source_id, key, "STALE", f"stream disconnected ({why}); reconnecting")

    def run_session(self) -> None:
        """One connection: connect, authenticate, subscribe every wanted symbol, then dispatch until it closes."""
        self.state = "connecting"
        conn = self._connect(self.url, timeout=10.0)
        with self._lock:
            self._ws = conn
        try:
            self._expect(conn, "connected")
            conn.send_text(json.dumps({"action": "auth", "key": self._credentials["key_id"],
                                       "secret": self._credentials["secret_key"]}))
            self._expect(conn, "authenticated")
            with self._lock:
                self._authenticated, self.state = True, "authenticated"
                symbols = sorted(self._wanted)
            if symbols:
                self._request("subscribe", symbols)
            while not self._stop.is_set():
                text = conn.recv()
                if text is None:
                    return
                self.handle(text)
        finally:
            conn.close()

    def _expect(self, conn, msg: str) -> None:
        text = conn.recv()
        if text is None:
            raise ws.WebSocketError(f"closed before '{msg}'")
        for m in parse_messages(text):
            if m.get("T") == "success" and m.get("msg") == msg:
                return
            if m.get("T") == "error":
                code = int(m.get("code", 0))
                raise ws.WebSocketError(f"Alpaca error {code} {m.get('msg') or ERRORS.get(code, ('', '?'))[1]}")
        raise ws.WebSocketError(f"expected '{msg}', got {text[:120]!r}")

    # ------------------------------------------------------------------ messages
    def handle(self, text: str) -> None:
        now = self._time_ns()
        for m in parse_messages(text):
            kind = m.get("T")
            if kind == "q":
                key = self._key(m.get("S"))
                if key:
                    self.sink.quote(self.source_id, key, {"bid": _num(m.get("bp")), "ask": _num(m.get("ap")),
                                                          "bid_size": _num(m.get("bs")), "ask_size": _num(m.get("as"))},
                                    data_type=self.data_type, ts_recv_ns=now, ts_source_ns=rfc3339_ns(m.get("t")),
                                    currency="USD", feed=f"alpaca:{self.feed}", size_unit="round_lots")
            elif kind == "t":
                key = self._key(m.get("S"))
                if key:
                    ts = rfc3339_ns(m.get("t"))
                    exch = m.get("x")
                    self.sink.trade(self.source_id, key, price=_num(m.get("p")), size=_num(m.get("s")),
                                    exchange=exch, conditions=list(m.get("c") or []), data_type=self.data_type,
                                    ts_recv_ns=now, ts_source_ns=ts, feed=f"alpaca:{self.feed}")
                    self.sink.quote(self.source_id, key, {"last": _num(m.get("p")), "last_size": _num(m.get("s"))},
                                    data_type=self.data_type, ts_recv_ns=now, ts_source_ns=ts, currency="USD",
                                    feed=f"alpaca:{self.feed}", size_unit="round_lots")
            elif kind == "subscription":
                confirmed = set(m.get("quotes") or []) | set(m.get("trades") or [])
                for sym in confirmed:
                    key = self._key(sym)
                    if key:
                        self.sink.status(self.source_id, key, "SUBSCRIBED", f"confirmed on the {self.feed} feed")
            elif kind == "error":
                self._error(int(m.get("code", 0)), str(m.get("msg", "")))

    def _key(self, symbol) -> str | None:
        with self._lock:
            return self._wanted.get(symbol)

    def _error(self, code: int, msg: str) -> None:
        state, default = ERRORS.get(code, ("UNAVAILABLE", "error"))
        self.last_error = f"{code} {msg or default}"
        if code in (405, 409, 410):            # refers to the last subscribe request; others stay as they were
            with self._lock:
                keys = [self._wanted.pop(s) for s in self._last_request if s in self._wanted]
            for key in keys:
                self.sink.status(self.source_id, key, state, f"Alpaca {code} {msg or default}")
            return
        with self._lock:
            keys = list(self._wanted.values())
        for key in keys:
            self.sink.status(self.source_id, key, "STALE", f"Alpaca {code} {msg or default}")

    def describe(self) -> dict:
        with self._lock:
            n = len(self._wanted)
        return {**super().describe(), "state": self.state if self._credentials else "no_keys", "feed": self.feed,
                "feed_label": self.feed_label, "keys": n, "last_error": self.last_error}


def build(opts: dict, home: Path) -> AlpacaSource:
    """Factory for config.FACTORIES['ALPACA']; options: feed (iex|sip|delayed_sip), symbol_limit, url."""
    from ...marketdata.config import venue
    limit = opts.get("symbol_limit", "plan")
    return AlpacaSource(feed=opts.get("feed", "iex"), credentials=load_credentials(home), venue_lookup=venue,
                        url=opts.get("url"), symbol_limit=limit if limit in ("plan", None) else int(limit))
