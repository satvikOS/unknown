"""TEST-ONLY market-data source (source id ``TEST``): a deterministic test double of a real-time provider.

It lives under backend/tests and is never importable from a product entry point: product code has no factory,
configuration key, environment variable or flag that reaches it (ADR-0020). Tests and the terminal end-to-end
harness inject it programmatically (``MarketDataBus(config, {"TEST": FeedDouble(...)})``).

Determinism: the values at step ``n`` of a key depend only on (seed, key, n) and the steps before it, so two sources
with the same seed produce identical sequences (tests compare them). Prices are integer cents rendered as decimal text;
nothing here is a float.
"""
from __future__ import annotations

import hashlib
import threading
import time
import zlib
from dataclasses import dataclass, field

from unknown.marketdata.config import BusConfig, Route
from unknown.marketdata.source import Capabilities, MarketDataSource, Outcome

SEED = "unknown-test-feed-v1"
SOURCE_ID = "TEST"
SLOTS = 25          # keys are spread over 25 phase slots per period so a large list does not tick in one burst


def _h(seed: str, key: str, n: int, salt: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}|{key}|{n}|{salt}".encode()).digest()[:8], "big")


def _cents(c: int) -> str:
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"


@dataclass
class _Sim:
    key: str
    slot: int
    n: int = 0
    volume: int = 0
    open: int | None = None
    high: int | None = None
    low: int | None = None


@dataclass
class Step:
    quote: dict
    trade: dict | None
    depth: dict | None = field(default=None)


class FeedDouble(MarketDataSource):
    source_id = SOURCE_ID
    data_type = "REALTIME"          # it imitates a real-time provider; the source id says TEST
    capabilities = Capabilities(quotes=True, trades=True, depth=True, tick_by_tick=True, delayed=False)

    def __init__(self, *, hz: float = 4.0, depth_levels: int = 5, seed: str = SEED, currency_for=None,
                 time_ns=time.time_ns, run_thread: bool = True):
        if not 0 < hz <= 50:
            raise ValueError("test feed hz must be in (0, 50]")
        self.hz = hz
        self.depth_levels = depth_levels
        self.seed = seed
        self._currency_for = currency_for or (lambda key: None)
        self._time_ns = time_ns
        self._run_thread = run_thread
        self._keys: dict[str, _Sim] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sink = None

    # ------------------------------------------------------------------ deterministic values
    def base_cents(self, key: str) -> int:
        return 2_000 + _h(self.seed, key, 0, "base") % 48_000          # 20.00 .. 499.99

    def step(self, sim: _Sim) -> Step:
        """Advance one key by one step and return its quote fields, an optional trade and an optional depth book."""
        sim.n += 1
        n, key = sim.n, sim.key
        base = self.base_cents(key)
        phase = n % 400
        wave = (phase if phase < 200 else 400 - phase) - 100          # triangle, -100 .. +100
        mid = base + base * wave // 10_000 + (_h(self.seed, key, n, "jit") % 21 - 10)
        spread = 1 + _h(self.seed, key, n, "spr") % 3
        bid, ask = mid - spread // 2 - spread % 2, mid + spread // 2
        bid_size = 100 * (1 + _h(self.seed, key, n, "bs") % 20)
        ask_size = 100 * (1 + _h(self.seed, key, n, "as") % 20)
        trade = None
        if n % 2 == 1:
            price = ask if _h(self.seed, key, n, "side") % 2 else bid
            size = 100 * (1 + _h(self.seed, key, n, "ts") % 9)
            sim.volume += size
            sim.open = price if sim.open is None else sim.open
            sim.high = price if sim.high is None else max(sim.high, price)
            sim.low = price if sim.low is None else min(sim.low, price)
            trade = {"price": _cents(price), "size": str(size), "exchange": "TEST", "conditions": []}
        last = trade or None
        quote = {"bid": _cents(bid), "ask": _cents(ask), "bid_size": str(bid_size), "ask_size": str(ask_size),
                 "last": last["price"] if last else None, "last_size": last["size"] if last else None,
                 "volume": str(sim.volume) if sim.volume else None,
                 "open": _cents(sim.open) if sim.open is not None else None,
                 "high": _cents(sim.high) if sim.high is not None else None,
                 "low": _cents(sim.low) if sim.low is not None else None,
                 "prev_close": _cents(base)}
        if last is None and sim.n > 1:
            quote.pop("last")
            quote.pop("last_size")                 # keep the previous print (merged by the bus)
        depth = None
        if n % 4 == 1:
            depth = {side: [{"price": _cents(top - i if side == "bid" else top + i),
                             "size": str(100 * (1 + _h(self.seed, key, n, f"{side}{i}") % 20)), "mm": None}
                            for i in range(self.depth_levels)]
                     for side, top in (("bid", bid), ("ask", ask))}
        return Step(quote, trade, depth)

    # ------------------------------------------------------------------ MarketDataSource
    def start(self, sink) -> None:
        self.sink = sink
        if self._run_thread and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="md-test-feed", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self, key: str) -> Outcome:
        with self._lock:
            if key not in self._keys:
                self._keys[key] = _Sim(key, zlib.crc32(key.encode()) % SLOTS)
        return Outcome(True, "SUBSCRIBED", "TEST source (test double; never in product)")

    def unsubscribe(self, key: str) -> None:
        with self._lock:
            self._keys.pop(key, None)

    def emit(self, sim: _Sim) -> None:
        st = self.step(sim)
        now = self._time_ns()
        sink, key = self.sink, sim.key
        if sink is None:
            return
        if st.trade is not None:
            sink.trade(self.source_id, key, price=st.trade["price"], size=st.trade["size"],
                       exchange=st.trade["exchange"], conditions=st.trade["conditions"], data_type=self.data_type,
                       ts_recv_ns=now, feed="test")
        sink.quote(self.source_id, key, st.quote, data_type=self.data_type, ts_recv_ns=now,
                   currency=self._currency_for(key), feed="test", size_unit="shares")
        if st.depth is not None:
            for side, levels in st.depth.items():
                sink.depth(self.source_id, key, side=side, levels=levels, data_type=self.data_type, ts_recv_ns=now)

    def _run(self) -> None:
        period = 1.0 / self.hz
        slot_dt = period / SLOTS
        slot = 0
        next_t = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                due = [s for s in self._keys.values() if s.slot == slot]
            for sim in due:
                self.emit(sim)
            slot = (slot + 1) % SLOTS
            next_t += slot_dt
            delay = next_t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -period:          # fell a whole period behind: skip ahead instead of bursting
                next_t = time.monotonic()

    def describe(self) -> dict:
        with self._lock:
            n = len(self._keys)
        return {**super().describe(), "state": "running" if self._thread else "configured", "hz": self.hz,
                "keys": n, "label": "TEST source (test double)"}


def bus_config(**opts) -> BusConfig:
    """A bus configuration with the TEST source only (built in code; product configuration cannot name it)."""
    return BusConfig({SOURCE_ID: dict(opts)}, (Route("*", (SOURCE_ID,)),), origin="test")
