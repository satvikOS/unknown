"""Quote snapshots used by risk checks and valuation.

Quotes are market data, not economic events: they are not written to the
event log. Every decision that uses a quote records the quote (source,
timestamps, age) in its own event, so the decision is reconstructible
without replaying the market-data stream (DAT-02, ORD-08)."""
from __future__ import annotations

import threading
from dataclasses import dataclass

from ..domain.fixed import Fixed, Rounding


CONSOLIDATION_VALUES = frozenset({"consolidated", "non_consolidated", "unknown"})
# IBKR market data types 1-4 (live, frozen, delayed, delayed frozen); "unreported" when a source does not say.
MARKET_DATA_TYPES = frozenset({"live", "frozen", "delayed", "delayed_frozen", "unreported"})


@dataclass(frozen=True)
class Quote:
    instrument_id: str
    bid: Fixed | None
    ask: Fixed | None
    last: Fixed | None
    currency: str
    source: str               # provider + feed, e.g. "ibkr:delayed" or "fixture"
    event_time_ns: int | None  # provider time if available
    receipt_time_ns: int       # local first observation
    delayed: bool = False      # delayed data must never be presented or used as live
    fixture: bool = False      # deterministic test data; never product data
    # consolidated (national best bid/offer) | non_consolidated (e.g. IBKR's free Cboe One + IEX feed,
    # CAP-DATA-10) | unknown. Comes only from the owner's feed configuration, never inferred (CAP-MKT-02).
    consolidation: str = "unknown"
    market_data_type: str = "unreported"

    def __post_init__(self) -> None:
        if self.consolidation not in CONSOLIDATION_VALUES:
            raise ValueError(f"consolidation must be one of {sorted(CONSOLIDATION_VALUES)}")
        if self.market_data_type not in MARKET_DATA_TYPES:
            raise ValueError(f"market_data_type must be one of {sorted(MARKET_DATA_TYPES)}")
        if self.market_data_type in ("frozen", "delayed", "delayed_frozen") and not self.delayed:
            raise ValueError("a frozen or delayed quote must be marked delayed")

    def age_ns(self, now_ns: int) -> int:
        reference = self.event_time_ns if self.event_time_ns is not None else self.receipt_time_ns
        return max(0, now_ns - reference)

    def mid(self) -> Fixed | None:
        if self.bid is None or self.ask is None or self.bid > self.ask:
            return None
        return (self.bid + self.ask).div(2, max(self.bid.scale, self.ask.scale) + 1, rounding=Rounding.HALF_EVEN)

    def to_json(self, now_ns: int) -> dict:
        return {
            "instrument_id": self.instrument_id,
            "bid": self.bid.to_str() if self.bid is not None else None,
            "ask": self.ask.to_str() if self.ask is not None else None,
            "last": self.last.to_str() if self.last is not None else None,
            "currency": self.currency, "source": self.source,
            "event_time_ns": self.event_time_ns, "receipt_time_ns": self.receipt_time_ns,
            "age_ms": self.age_ns(now_ns) // 1_000_000, "delayed": self.delayed, "fixture": self.fixture,
            "consolidation": self.consolidation, "market_data_type": self.market_data_type,
        }



class QuoteBook:
    """Latest quote per instrument; thread-safe; fed by market-data adapters."""

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._lock = threading.Lock()

    def put(self, quote: Quote) -> None:
        with self._lock:
            current = self._quotes.get(quote.instrument_id)
            if current is None or quote.receipt_time_ns >= current.receipt_time_ns:
                self._quotes[quote.instrument_id] = quote

    def get(self, instrument_id: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(instrument_id)

    def snapshot(self) -> dict[str, Quote]:
        with self._lock:
            return dict(self._quotes)
