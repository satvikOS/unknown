"""Simulated execution (ADR-0020): the simulator prices only from the configured real quote source.

Without a configured source nothing fills and the status says NO_SOURCE; a missing quote is NO_DATA; a delayed, stale
or old quote is STALE; a quote from another source or flagged as test data is NO_SOURCE. Every price below is a test
value."""
from __future__ import annotations

import unittest

from unknown.broker.port import BrokerOrder
from unknown.broker.simulated import SimulatedBroker
from unknown.domain.calendar import WEEKDAYS_ONLY
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed
from unknown.market.quotes import Quote, QuoteBook
from unknown.service.sim_execution import SimulatedExecution

F = Fixed.parse
NOW = utc_ns(2026, 10, 23, 15, 0)


def order(cid: str, side: str, limit: str) -> BrokerOrder:
    return BrokerOrder(client_order_id=cid, order_ref=f"DEP:1:{cid}", instrument_id="INS-A", provider_ref="INS-A",
                       side=side, quantity=F("10"), limit_price=F(limit), currency="USD", time_in_force="DAY")


def quote(source="IBKR:live", bid="9.99", ask="10.01", age_s=0, **kw) -> Quote:
    t = NOW - age_s * 1_000_000_000
    return Quote("INS-A", F(bid), F(ask), None, "USD", source, t, t, **kw)


class SimulatedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(NOW)
        self.broker = SimulatedBroker(self.clock, initial_settled_cash=F("1000.00"), calendar=WEEKDAYS_ONLY)
        self.book = QuoteBook()
        self.broker.submit(order("B1", "BUY", "10.05"))

    def run_with(self, source, q=None):
        if q is not None:
            self.book.put(q)
        ex = SimulatedExecution(self.broker, self.clock, source=source, quotes=self.book)
        return ex, ex.step()

    def test_no_configured_source_fills_nothing_and_says_why(self):
        ex, fills = self.run_with(None, quote())
        self.assertEqual(fills, 0)
        st = ex.status()
        self.assertEqual((st["pricing"], st["reason"], st["unpriced"]), ("UNAVAILABLE", "NO_SOURCE", {"B1": "NO_SOURCE"}))
        self.assertEqual(len(self.broker.working_orders()), 1)

    def test_a_crossing_live_quote_from_the_source_fills_at_the_ask(self):
        ex, fills = self.run_with("IBKR", quote())
        self.assertEqual((fills, ex.status()["reason"], self.broker.working_orders()), (1, None, []))
        ex_rows = self.broker.executions_since(None).executions
        self.assertEqual([(r.side, r.price.to_str()) for r in ex_rows], [("BUY", "10.01")])

    def test_unusable_quotes_never_fill(self):
        cases = {"missing": (None, "NO_DATA"),
                 "other source": (quote(source="ALPACA:iex"), "NO_SOURCE"),
                 "test data": (quote(fixture=True), "NO_SOURCE"),
                 "delayed": (quote(delayed=True, market_data_type="delayed"), "STALE"),
                 "old": (quote(age_s=60), "STALE"),
                 "not crossing": (quote(bid="10.10", ask="10.12"), None)}
        for name, (q, reason) in cases.items():
            with self.subTest(name):
                self.book = QuoteBook()
                ex, fills = self.run_with("IBKR", q)
                self.assertEqual(fills, 0)
                self.assertEqual(ex.unpriced.get("B1"), reason)


if __name__ == "__main__":
    unittest.main()
