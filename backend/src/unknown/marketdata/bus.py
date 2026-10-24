"""The market-data bus (ADR-0018): sources in, normalised contract events out to SSE clients.

* Per key, one source serves the key (chosen by config routes with fallback order); every event names it.
* Quotes are coalesced per key: at most one publish every ``coalesce_ms`` (50 ms), always the latest merged state.
  A quote arriving after a quiet window is published at once; one arriving inside the window marks the key dirty
  and the coalescer publishes the latest state when the window ends.
* Trades are never coalesced. Each client has a bounded queue; when it is full a trade is not queued, the drop is
  counted (``trades_dropped``) and the client receives a ``status`` event naming the count. The trade is still in
  the tick store, so time & sales can be re-read; the per-key trade ``seq`` gap tells the client too.
* Depth events are full side snapshots, so a client's queue keeps only the latest per (key, side).
* Line budgets are enforced before a source is asked: a source at its budget is skipped (fallback) and a key no
  source can take gets a LINE_BUDGET / NOT_ENTITLED / UNAVAILABLE status, never a silent drop.
* Nothing here touches the core's event-sourced database; the core's marks keep their own path.

Locks: ``_sub_lock`` serialises subscription changes and is held while calling a source; ``_lock`` guards state and
is never held while calling a source, so a source may call the sink from a thread holding its own lock.
"""
from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import BusConfig, build_sources, load_config
from .events import (QUOTE_FIELDS, SIZE_UNITS, check_data_type, dec, depth_event, ns, quote_event, status_event,
                     trade_event)
from .latency import LatencyStats
from .source import MarketDataSource, Outcome
from .store import TickStore

log = logging.getLogger("unknown.marketdata")

MAX_KEYS_PER_STREAM = 2000
REFUSAL_STATES = ("NOT_ENTITLED", "UNAVAILABLE")


@dataclass
class _KeyState:
    key: str
    source: str
    data_type: str | None
    refs: int = 0
    reason: str = ""
    fields: dict = field(default_factory=lambda: {f: None for f in QUOTE_FIELDS})
    currency: str | None = None
    feed: str | None = None
    size_unit: str | None = None
    ts_source_ns: int | None = None
    ts_recv_ns: int | None = None
    last_pub: float = -1e9
    dirty: bool = False
    quote_event: dict | None = None
    depth: dict = field(default_factory=dict)
    status: dict | None = None


class ClientQueue:
    """One SSE client's pending events. Quotes and depth sides are slots (latest wins); trades and statuses queue."""

    def __init__(self, max_events: int, on_drop=None):
        self.cv = threading.Condition()
        self.order: deque = deque()
        self.quotes: dict[str, dict] = {}
        self.depth: dict[tuple, dict] = {}
        self.max_events = max_events
        self.queued = 0
        self.overflow: dict[str, int] = {}
        self.dropped = 0
        self.closed = False
        self.keys: set[str] = set()
        self._on_drop = on_drop

    def put_quote(self, ev: dict) -> None:
        with self.cv:
            if ev["key"] not in self.quotes:
                self.order.append(("quote", ev["key"]))
            self.quotes[ev["key"]] = ev
            self.cv.notify()

    def put_depth(self, ev: dict) -> None:
        slot = (ev["key"], ev["side"])
        with self.cv:
            if slot not in self.depth:
                self.order.append(("depth", slot))
            self.depth[slot] = ev
            self.cv.notify()

    def put(self, name: str, ev: dict) -> None:
        with self.cv:
            if self.queued >= self.max_events and name == "trade":
                self.overflow[ev["key"]] = self.overflow.get(ev["key"], 0) + 1
                self.dropped += 1
                if self._on_drop:
                    self._on_drop()
                self.cv.notify()
                return
            self.order.append((name, ev))
            self.queued += 1
            self.cv.notify()

    def drain(self, timeout: float) -> list[tuple[str, dict]]:
        with self.cv:
            if not self.order and not self.overflow and not self.closed:
                self.cv.wait(timeout)
            out = []
            while self.order:
                name, ref = self.order.popleft()
                if name == "quote":
                    out.append(("quote", self.quotes.pop(ref)))
                elif name == "depth":
                    out.append(("depth", self.depth.pop(ref)))
                else:
                    self.queued -= 1
                    out.append((name, ref))
            for key, n in self.overflow.items():
                out.append(("status", status_event(
                    key, "STALE", f"{n} trade(s) not delivered: this client's buffer was full ({self.max_events} "
                                  f"events); the trade seq has a gap; re-read /api/marketdata/trades?key={key}")))
            self.overflow.clear()
            return out


class MarketDataBus:
    def __init__(self, config: BusConfig, sources: dict[str, MarketDataSource], *, store: TickStore | None = None,
                 time_ns=time.time_ns, monotonic=time.monotonic):
        self.config = config
        self.sources = sources
        self.store = store
        self._time_ns = time_ns
        self._mono = monotonic
        self._window = config.coalesce_ms / 1000.0
        self._lock = threading.RLock()
        self._sub_lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._keys: dict[str, _KeyState] = {}
        self._seq: dict[tuple[str, str], int] = {}
        self._clients: set[ClientQueue] = set()
        self._by_key: dict[str, set[ClientQueue]] = {}
        self._in_use: dict[str, set[str]] = {sid: set() for sid in sources}
        self._heap: list = []
        self._stopped = False
        self.latency = LatencyStats()
        self.counters = {k: 0 for k in ("quotes_in", "quotes_published", "quotes_coalesced", "trades", "trades_dropped",
                                        "depth", "status", "refused", "ignored", "failovers")}
        self._coalescer = threading.Thread(target=self._coalesce_run, name="md-coalescer", daemon=True)
        self._started = False

    @classmethod
    def from_home(cls, home: Path) -> "MarketDataBus":
        config = load_config(home)
        bus = cls(config, build_sources(config, home), store=TickStore(Path(home) / "marketdata" / "ticks.sqlite"))
        bus.start()
        return bus

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._coalescer.start()
        for src in self.sources.values():
            src.start(self)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._cv.notify_all()
        for src in self.sources.values():
            try:
                src.stop()
            except Exception:
                log.exception("stopping source %s failed", src.source_id)
        if self.store is not None:
            self.store.close()

    # ------------------------------------------------------------------ helpers
    def _next_seq(self, key: str, kind: str) -> int:
        n = self._seq.get((key, kind), 0) + 1
        self._seq[(key, kind)] = n
        return n

    def _state(self, source_id: str, key: str) -> _KeyState | None:
        st = self._keys.get(key)
        if st is None or st.source != source_id:
            self.counters["ignored"] += 1          # late event from a source that no longer serves the key
            return None
        return st

    def _fanout(self, key: str):
        return tuple(self._by_key.get(key, ()))

    def _drop_counted(self) -> None:
        with self._lock:
            self.counters["trades_dropped"] += 1

    # ------------------------------------------------------------------ Sink (called by sources, any thread)
    def quote(self, source_id: str, key: str, fields: dict, *, data_type: str, ts_recv_ns: int,
              ts_source_ns: int | None = None, currency: str | None = None, feed: str | None = None,
              size_unit: str | None = None, replace: bool = False) -> None:
        check_data_type(data_type)
        if size_unit is not None and size_unit not in SIZE_UNITS:
            raise ValueError(f"size_unit {size_unit!r}")
        clean = {f: dec(fields.get(f)) for f in QUOTE_FIELDS if f in fields or replace}
        with self._lock:
            st = self._state(source_id, key)
            if st is None:
                return
            st.fields.update(clean)
            st.data_type = data_type
            st.currency = currency if currency is not None else st.currency
            st.feed = feed if feed is not None else st.feed
            st.size_unit = size_unit if size_unit is not None else st.size_unit
            st.ts_source_ns, st.ts_recv_ns = ts_source_ns, ts_recv_ns
            self.counters["quotes_in"] += 1
            now = self._mono()
            if now - st.last_pub >= self._window:
                self._publish_quote(st, now)
            elif st.dirty:
                self.counters["quotes_coalesced"] += 1
            else:
                st.dirty = True
                heapq.heappush(self._heap, (st.last_pub + self._window, key))
                self._cv.notify()

    def _publish_quote(self, st: _KeyState, now: float) -> None:
        st.last_pub, st.dirty = now, False
        pub = self._time_ns()
        ev = quote_event(st.key, self._next_seq(st.key, "quote"), st.fields, currency=st.currency, source=st.source,
                         data_type=st.data_type, feed=st.feed, size_unit=st.size_unit, ts_source_ns=st.ts_source_ns,
                         ts_recv_ns=st.ts_recv_ns, ts_pub_ns=pub)
        st.quote_event = ev
        self.counters["quotes_published"] += 1
        if st.ts_recv_ns is not None:
            self.latency.record("recv_to_pub", pub - st.ts_recv_ns)
            if st.ts_source_ns is not None:
                self.latency.record("source_to_recv", st.ts_recv_ns - st.ts_source_ns)
        for c in self._fanout(st.key):
            c.put_quote(ev)

    def _coalesce_run(self) -> None:
        with self._lock:
            while not self._stopped:
                if not self._heap:
                    self._cv.wait(1.0)
                    continue
                due, key = self._heap[0]
                delay = due - self._mono()
                if delay > 0:
                    self._cv.wait(delay)
                    continue
                heapq.heappop(self._heap)
                st = self._keys.get(key)
                if st is not None and st.dirty:
                    self._publish_quote(st, self._mono())

    def trade(self, source_id: str, key: str, *, price, size, exchange: str | None, conditions, data_type: str,
              ts_recv_ns: int, ts_source_ns: int | None = None, feed: str | None = None) -> None:
        check_data_type(data_type)
        with self._lock:
            st = self._state(source_id, key)
            if st is None:
                return
            pub = self._time_ns()
            ev = trade_event(key, self._next_seq(key, "trade"), price=price, size=size, exchange=exchange,
                             conditions=conditions, source=source_id, data_type=data_type, feed=feed,
                             ts_source_ns=ts_source_ns, ts_recv_ns=ts_recv_ns, ts_pub_ns=pub)
            self.counters["trades"] += 1
            self.latency.record("recv_to_pub", pub - ts_recv_ns)
            if ts_source_ns is not None:
                self.latency.record("source_to_recv", ts_recv_ns - ts_source_ns)
            clients = self._fanout(key)
        if self.store is not None:
            self.store.append(ev)
        for c in clients:
            c.put("trade", ev)

    def depth(self, source_id: str, key: str, *, side: str, levels: list, data_type: str, ts_recv_ns: int) -> None:
        check_data_type(data_type)
        with self._lock:
            st = self._state(source_id, key)
            if st is None:
                return
            ev = depth_event(key, self._next_seq(key, "depth"), side=side, levels=levels, source=source_id,
                             data_type=data_type, ts_recv_ns=ts_recv_ns)
            st.depth[side] = ev
            self.counters["depth"] += 1
            for c in self._fanout(key):
                c.put_depth(ev)

    def status(self, source_id: str, key: str, state: str, reason: str) -> None:
        ev = status_event(key, state, f"{source_id}: {reason}")
        with self._lock:
            st = self._state(source_id, key)
            if st is None:
                return
            st.status = ev
            self.counters["status"] += 1
            for c in self._fanout(key):
                c.put("status", ev)
        if state in REFUSAL_STATES and self._fallbacks_after(key, source_id):
            threading.Thread(target=self._failover, args=(key, source_id, ev["reason"]), name="md-failover",
                             daemon=True).start()

    # ------------------------------------------------------------------ subscriptions
    def _fallbacks_after(self, key: str, source_id: str) -> tuple[str, ...]:
        order = self.config.sources_for(key)
        return order[order.index(source_id) + 1:] if source_id in order else ()

    def open_client(self, keys: list[str]) -> ClientQueue:
        c = ClientQueue(self.config.trade_buffer, on_drop=self._drop_counted)
        with self._lock:
            self._clients.add(c)
        for i, key in enumerate(keys):
            if i >= MAX_KEYS_PER_STREAM:
                c.put("status", status_event(key, "LINE_BUDGET", f"more than {MAX_KEYS_PER_STREAM} keys in one stream"))
                continue
            self._acquire(c, key)
        return c

    def close_client(self, c: ClientQueue) -> None:
        with self._lock:
            self._clients.discard(c)
            c.closed = True
            keys = list(c.keys)
        for key in keys:
            self._release(c, key)

    def _attach(self, c: ClientQueue, st: _KeyState) -> None:
        st.refs += 1
        c.keys.add(st.key)
        self._by_key.setdefault(st.key, set()).add(c)

    def _acquire(self, c: ClientQueue, key: str) -> None:
        with self._sub_lock:
            with self._lock:
                st = self._keys.get(key)
                if st is not None:
                    self._attach(c, st)
                    c.put("status", st.status or status_event(key, "SUBSCRIBED", st.reason))
                    if st.quote_event is not None:
                        c.put_quote(st.quote_event)
                    for ev in st.depth.values():
                        c.put_depth(ev)
                    return
            outcome = self._route(key, self.config.sources_for(key), c)
        if not outcome.ok:
            with self._lock:
                self.counters["refused"] += 1
            c.put("status", status_event(key, outcome.state, outcome.reason))

    def _route(self, key: str, order: tuple[str, ...], c: ClientQueue | None, *, refs: int = 0,
               prior: str = "") -> Outcome:
        """Try ``order`` (called with _sub_lock held). On success the key's state exists and the client is attached."""
        refusals: list[Outcome] = []
        for sid in order:
            src = self.sources[sid]
            budget = src.line_budget()
            with self._lock:
                used = len(self._in_use[sid])
                if budget is not None and used >= budget:
                    refusals.append(Outcome(False, "LINE_BUDGET", f"{sid}: {used} of {budget} lines in use"))
                    continue
                st = _KeyState(key, sid, src.data_type, refs=refs)
                self._keys[key] = st
                self._in_use[sid].add(key)
                if c is not None:
                    self._attach(c, st)
            try:
                out = src.subscribe(key)
            except Exception as exc:
                log.exception("source %s subscribe(%s) failed", sid, key)
                out = Outcome(False, "UNAVAILABLE", f"{sid}: {type(exc).__name__}: {exc}")
            with self._lock:
                if out.ok:
                    # Never claim a data type the provider has not reported (IBKR reports it per subscription).
                    kind = src.data_type or "delivered type not yet reported"
                    st.reason = f"{prior}{sid} ({kind}): {out.reason}".strip()
                    ev = status_event(key, "SUBSCRIBED", st.reason)
                    for client in self._fanout(key):
                        client.put("status", ev)
                    return out
                self._keys.pop(key, None)
                self._in_use[sid].discard(key)
                for client in self._by_key.pop(key, set()):
                    client.keys.discard(key)
            refusals.append(out if out.reason.startswith(sid) else Outcome(False, out.state, f"{sid}: {out.reason}"))
        if not refusals:
            refusals.append(Outcome(False, "UNAVAILABLE", "NO_SOURCE: no configured source serves this key "
                                                          "(<UNKNOWN_HOME>/config/marketdata.json routes)"))
        states = [r.state for r in refusals]
        state = "LINE_BUDGET" if "LINE_BUDGET" in states else refusals[0].state
        return Outcome(False, state, "; ".join(r.reason for r in refusals))

    def _failover(self, key: str, from_source: str, reason: str) -> None:
        with self._sub_lock:
            with self._lock:
                st = self._keys.get(key)
                if st is None or st.source != from_source:
                    return
                clients = self._by_key.pop(key, set())
                refs = st.refs
                del self._keys[key]
                self._in_use[from_source].discard(key)
            try:
                self.sources[from_source].unsubscribe(key)
            except Exception:
                log.exception("unsubscribe after refusal failed")
            order = self._fallbacks_after(key, from_source)
            with self._lock:
                self.counters["failovers"] += 1
            out = Outcome(False, "UNAVAILABLE", "")
            for sid in order:
                out = self._route(key, (sid,), None, refs=refs, prior=f"fell back from {from_source} ({reason}); ")
                if out.ok:
                    with self._lock:
                        self._by_key[key] = set(clients)
                        ev = status_event(key, "SUBSCRIBED", self._keys[key].reason)
                    for c in clients:
                        c.put("status", ev)
                    return
            for c in clients:
                c.keys.discard(key)
                c.put("status", status_event(key, out.state, f"{reason}; {out.reason}"))

    def _release(self, c: ClientQueue, key: str) -> None:
        with self._sub_lock:
            with self._lock:
                st = self._keys.get(key)
                c.keys.discard(key)
                clients = self._by_key.get(key)
                if clients is not None:
                    clients.discard(c)
                if st is None:
                    return
                st.refs -= 1
                if st.refs > 0:
                    return
                del self._keys[key]
                self._by_key.pop(key, None)
                self._in_use[st.source].discard(key)
                src = self.sources[st.source]
            try:
                src.unsubscribe(key)
            except Exception:
                log.exception("source %s unsubscribe(%s) failed", st.source, key)

    # ------------------------------------------------------------------ reads
    def snapshot(self, keys: list[str]) -> dict:
        quotes, status = [], []
        with self._lock:
            for key in keys:
                st = self._keys.get(key)
                if st is not None and st.quote_event is not None:
                    quotes.append(st.quote_event)
                elif st is not None:
                    status.append(st.status or status_event(key, "SUBSCRIBED", f"{st.reason}; no data yet"))
                else:
                    status.append(status_event(key, "UNAVAILABLE", "PENDING: not streaming: open /api/marketdata/stream "
                                                                   "with this key first"))
        return {"quotes": quotes, "status": status, "ts_pub_ns": ns(self._time_ns())}

    def describe(self) -> dict:
        with self._lock:
            in_use = {sid: len(v) for sid, v in self._in_use.items()}
            counters = dict(self.counters)
            clients = len(self._clients)
            keys = len(self._keys)
        sources = []
        for sid, src in self.sources.items():
            d = src.describe()
            d["source"] = sid
            d["lines_in_use"] = in_use.get(sid, 0)
            sources.append(d)
        return {"config": self.config.origin, "coalesce_ms": self.config.coalesce_ms,
                "trade_buffer": self.config.trade_buffer, "sources": sources,
                "routes": [{"match": r.match, "sources": list(r.sources)} for r in self.config.routes],
                "clients": clients, "keys": keys, "counters": counters,
                "store": self.store.stats() if self.store is not None else None}
