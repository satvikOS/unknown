"""FIXTURE market for running the simulated desk as a live process.

It is not market data. It moves a deterministic pseudo-random quote per
fixture instrument and fills simulated orders that cross it, so the terminal
and the service loop can be exercised end to end. Every quote it produces is
labelled ``fixture=True`` and ``source='fixture-market'``; the terminal shows
FIXTURE wherever these values appear (TRM-03)."""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..broker.simulated import SimulatedBroker
from ..domain.clock import Clock
from ..domain.fixed import Fixed, Rounding
from ..market.quotes import Quote, QuoteBook


@dataclass
class FixtureInstrument:
    instrument_id: str
    mid: Fixed
    spread: Fixed
    tick: Fixed


class FixtureMarket:
    def __init__(self, clock: Clock, broker: SimulatedBroker, quotes: QuoteBook, instruments: list[FixtureInstrument],
                 *, seed: int = 7, step_bps: int = 15):
        self.clock = clock
        self.broker = broker
        self.quotes = quotes
        self.instruments = instruments
        self.rng = random.Random(seed)   # deterministic fixture path, not a model of any market
        self.step_bps = step_bps

    def step(self) -> int:
        now = self.clock.now_ns()
        for inst in self.instruments:
            move = self.rng.randint(-self.step_bps, self.step_bps)
            mid = (inst.mid * (10_000 + move)).div(10_000, inst.tick.scale + 2, Rounding.HALF_EVEN)
            inst.mid = mid.to_increment(inst.tick, Rounding.HALF_EVEN)
            half = inst.spread.div(2, inst.tick.scale + 2, Rounding.HALF_EVEN)
            bid = (inst.mid - half).to_increment(inst.tick, Rounding.FLOOR)
            ask = (inst.mid + half).to_increment(inst.tick, Rounding.CEILING)
            self.quotes.put(Quote(inst.instrument_id, bid, ask, inst.mid, "USD", "fixture-market", now, now, fixture=True))
        return self._cross()

    def _cross(self) -> int:
        fills = 0
        for order in list(self.broker._orders.values()):  # fixture-only access to the simulator's book
            if not order.working or not order.remaining.is_positive():
                continue
            q = self.quotes.get(order.instrument_id)
            if q is None or q.bid is None or q.ask is None:
                continue
            if order.side == "BUY" and order.limit_price >= q.ask:
                self.broker.fill(order.client_order_id, order.remaining, q.ask)
                fills += 1
            elif order.side == "SELL" and order.limit_price <= q.bid:
                self.broker.fill(order.client_order_id, order.remaining, q.bid)
                fills += 1
        self.broker.advance_settlement()
        return fills
