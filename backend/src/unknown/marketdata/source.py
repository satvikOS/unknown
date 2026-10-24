"""The MarketDataSource interface: every provider (IBKR, Alpaca, ...) is an adapter behind it.

Nothing in the bus, the API or the terminal knows a provider by anything other than its ``source_id`` and its
declared capabilities. A source:

* declares ``capabilities`` (quotes, trades, depth, tick-by-tick, delayed) and its ``data_type`` (REALTIME,
  DELAYED or FROZEN; a source may send per-event types, e.g. IBKR delivering delayed data);
* declares a ``line_budget()`` (simultaneous keys it may stream; None = no stated limit), which the bus enforces
  before calling ``subscribe``;
* answers ``subscribe(key)`` synchronously with an ``Outcome`` (its local entitlement/coverage check), then delivers
  normalised data and status through the ``Sink`` it was started with, from any thread.

Sources never call back into the bus while holding a lock the bus may wait on; the bus never holds its state lock
while calling a source (see bus.py).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    quotes: bool = True
    trades: bool = False
    depth: bool = False
    tick_by_tick: bool = False
    delayed: bool = False

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    state: str = "SUBSCRIBED"      # a contract status state; refusals use UNAVAILABLE / NOT_ENTITLED / LINE_BUDGET
    reason: str = ""


class Sink(Protocol):
    def quote(self, source_id: str, key: str, fields: dict, *, data_type: str, ts_recv_ns: int,
              ts_source_ns: int | None = None, currency: str | None = None, feed: str | None = None,
              size_unit: str | None = None, replace: bool = False) -> None: ...

    def trade(self, source_id: str, key: str, *, price, size, exchange: str | None, conditions, data_type: str,
              ts_recv_ns: int, ts_source_ns: int | None = None, feed: str | None = None) -> None: ...

    def depth(self, source_id: str, key: str, *, side: str, levels: list, data_type: str, ts_recv_ns: int) -> None: ...

    def status(self, source_id: str, key: str, state: str, reason: str) -> None: ...


class MarketDataSource(ABC):
    source_id: str = ""
    data_type: str | None = "REALTIME"   # None: the provider reports the delivered type per event (e.g. IBKR)
    capabilities: Capabilities = Capabilities()

    def line_budget(self) -> int | None:
        return None

    def start(self, sink: Sink) -> None:
        self.sink = sink

    def stop(self) -> None:
        pass

    @abstractmethod
    def subscribe(self, key: str) -> Outcome: ...

    @abstractmethod
    def unsubscribe(self, key: str) -> None: ...

    def describe(self) -> dict:
        return {"source": self.source_id, "data_type": self.data_type, "capabilities": self.capabilities.to_json(),
                "line_budget": self.line_budget(), "state": "configured"}


class UnavailableSource(MarketDataSource):
    """A configured source that could not be built (missing client library, keys or configuration).

    It stays visible: every subscription is refused with the build error, so a key routed to it falls back to the
    next configured source or shows UNAVAILABLE with the cause, never silently nothing."""

    def __init__(self, source_id: str, reason: str):
        self.source_id = source_id
        self.reason = reason
        self.capabilities = Capabilities(quotes=False)

    def subscribe(self, key: str) -> Outcome:
        return Outcome(False, "UNAVAILABLE", f"{self.source_id} unavailable: {self.reason}")

    def unsubscribe(self, key: str) -> None:
        pass

    def describe(self) -> dict:
        return {**super().describe(), "state": "unavailable", "reason": self.reason}
