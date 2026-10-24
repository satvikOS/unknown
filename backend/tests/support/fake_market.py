"""TEST-ONLY quote feed for running the simulated desk as a live process (terminal e2e and service tests).

``QuoteFeedDouble`` is a deterministic test double of a real-time quote source: each step it moves a mid price per
instrument and publishes a quote with source ``TEST`` into the desk's quote book. It never fills anything itself:
fills come from the product's ``unknown.service.sim_execution.SimulatedExecution`` configured with source ``TEST``,
exactly as it would be with a real configured source. Product code cannot reach this module (ADR-0020).

``TestFeedExecution`` steps the feed and then the product execution on each core cycle; pass it to ``CoreService``
as ``sim_execution``.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from unknown.broker.simulated import SimulatedBroker
from unknown.domain.clock import Clock
from unknown.domain.fixed import Fixed, Rounding
from unknown.market.quotes import Quote, QuoteBook
from unknown.service.sim_execution import SimulatedExecution

SOURCE_ID = "TEST"


@dataclass
class FeedInstrument:
    instrument_id: str
    mid: Fixed
    spread: Fixed
    tick: Fixed


# The instruments the terminal end-to-end suite registers (backend/tests/support/e2e_bootstrap.py).
E2E_INSTRUMENTS = (("FIX-EQ-1", "10.00", "0.02", "0.01"), ("FIX-EQ-2", "42.00", "0.04", "0.01"))


def e2e_instruments() -> list[FeedInstrument]:
    return [FeedInstrument(i, Fixed.parse(m), Fixed.parse(s), Fixed.parse(t)) for i, m, s, t in E2E_INSTRUMENTS]


class QuoteFeedDouble:
    def __init__(self, clock: Clock, instruments: list[FeedInstrument], *, quotes: QuoteBook | None = None,
                 seed: int = 7, step_bps: int = 15):
        self.clock = clock
        self.quotes = quotes
        self.instruments = instruments
        self.rng = random.Random(seed)   # deterministic test path, not a model of any market
        self.step_bps = step_bps

    def step(self) -> None:
        now = self.clock.now_ns()
        for inst in self.instruments:
            move = self.rng.randint(-self.step_bps, self.step_bps)
            mid = (inst.mid * (10_000 + move)).div(10_000, inst.tick.scale + 2, Rounding.HALF_EVEN)
            inst.mid = mid.to_increment(inst.tick, Rounding.HALF_EVEN)
            half = inst.spread.div(2, inst.tick.scale + 2, Rounding.HALF_EVEN)
            bid = (inst.mid - half).to_increment(inst.tick, Rounding.FLOOR)
            ask = (inst.mid + half).to_increment(inst.tick, Rounding.CEILING)
            self.quotes.put(Quote(inst.instrument_id, bid, ask, inst.mid, "USD", SOURCE_ID, now, now,
                                  market_data_type="live"))


class TestFeedExecution:
    """CoreService ``sim_execution`` for tests: the TEST feed publishes, then the product execution prices from it."""
    __test__ = False

    def __init__(self, clock: Clock, broker: SimulatedBroker, instruments: list[FeedInstrument]):
        self.feed = QuoteFeedDouble(clock, instruments)
        self.execution = SimulatedExecution(broker, clock, source=SOURCE_ID)

    @property
    def quotes(self) -> QuoteBook | None:
        return self.execution.quotes

    @quotes.setter
    def quotes(self, book: QuoteBook) -> None:
        self.feed.quotes = book
        self.execution.quotes = book

    def step(self) -> int:
        self.feed.step()
        return self.execution.step()

    def status(self) -> dict:
        return self.execution.status()
