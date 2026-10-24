"""Simulated execution priced only from a real, configured quote source (ADR-0020).

The simulated broker (``unknown.broker.simulated``) is the default safety mode: no order leaves the machine. In a
running core it fills a working limit order only when a quote from the configured real source crosses it:

* BUY fills the remaining quantity at the ask when ``limit >= ask``; SELL at the bid when ``limit <= bid``;
* the quote must come from the configured source, must not be flagged ``fixture``, must be live (not delayed,
  frozen or labelled stale: delayed data is never used as live) and must be within ``max_age_ns`` (default 5 s);
* with no configured source nothing fills, ever, and the status says why (``NO_SOURCE``). A missing quote is
  ``NO_DATA``, an old one ``STALE``, a quote from another source or flagged as test data ``NO_SOURCE``.

There is no generated price path in product code. Tests and the terminal end-to-end harness feed the desk's quote
book from a TEST double under backend/tests, which product code cannot reach.
"""
from __future__ import annotations

from ..broker.simulated import SimulatedBroker
from ..domain.clock import Clock
from ..domain.unavailable import NO_DATA, NO_SOURCE, STALE
from ..market.quotes import QuoteBook

DEFAULT_MAX_AGE_NS = 5_000_000_000


class SimulatedExecution:
    def __init__(self, broker: SimulatedBroker, clock: Clock, *, source: str | None, quotes: QuoteBook | None = None,
                 max_age_ns: int = DEFAULT_MAX_AGE_NS):
        self.broker = broker
        self.clock = clock
        self.source = source                    # the configured real quote source's id, or None
        self.quotes = quotes                    # the desk's quote book (CoreService.run sets it)
        self.max_age_ns = max_age_ns
        self.fills = 0
        self.unpriced: dict[str, str] = {}      # client order id -> reason code (latest step)

    def _source_matches(self, quote_source: str) -> bool:
        head = quote_source.split(":", 1)[0].upper()
        return self.source is not None and head == self.source.upper()

    def step(self) -> int:
        """Cross working orders against current quotes; returns the number of fills."""
        self.unpriced = {}
        if self.source is None or self.quotes is None:
            for o in self.broker.working_orders():
                self.unpriced[o.client_order_id] = NO_SOURCE
            self.broker.advance_settlement()     # settlement follows the calendar, not prices
            return 0
        now = self.clock.now_ns()
        fills = 0
        for o in self.broker.working_orders():
            q = self.quotes.get(o.instrument_id)
            if q is None or q.bid is None or q.ask is None:
                self.unpriced[o.client_order_id] = NO_DATA
                continue
            if q.fixture or not self._source_matches(q.source):
                self.unpriced[o.client_order_id] = NO_SOURCE
                continue
            if q.delayed or ":stale" in q.source or q.age_ns(now) > self.max_age_ns:
                self.unpriced[o.client_order_id] = STALE
                continue
            if o.side == "BUY" and o.limit_price >= q.ask:
                self.broker.fill(o.client_order_id, o.remaining, q.ask)
                fills += 1
            elif o.side == "SELL" and o.limit_price <= q.bid:
                self.broker.fill(o.client_order_id, o.remaining, q.bid)
                fills += 1
        self.broker.advance_settlement()
        self.fills += fills
        return fills

    def status(self) -> dict:
        if self.source is None:
            return {"pricing": "UNAVAILABLE", "source": None, "reason": NO_SOURCE,
                    "detail": "no real quote source is configured for the simulated broker, so no simulated order "
                              "fills (ADR-0020)", "fills": self.fills, "unpriced": dict(self.unpriced)}
        return {"pricing": "REAL_QUOTES", "source": self.source, "reason": None,
                "detail": f"working orders fill only when a {self.source} quote no older than "
                          f"{self.max_age_ns // 1_000_000} ms crosses the limit", "fills": self.fills,
                "unpriced": dict(self.unpriced)}
