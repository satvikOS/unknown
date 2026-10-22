"""Daily P&L decomposition from the journal (CAP-PORT-02): realized + income - fees + change in unrealized equals the
NAV change minus external flows EXACTLY, in ledger units, after every event of a deterministic replay (buys, a delayed
final commission, partial sells, settlement, a capital authorization). Every value is a TEST FIXTURE."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import DAY, F, SEC, booted, eod, ro, snapshot_store  # noqa: E402
from support import ACCOUNT  # noqa: E402

from unknown.analytics.common import ACCOUNT as ACCOUNT_SCOPE, SYSTEM, parse_scope  # noqa: E402
from unknown.analytics.pnl import pnl_report  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402


def _intent(h, oid: str, side: str, qty: str, limit: str) -> None:
    sess = h.row("sessions", "session_id", "S1")
    ph = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])["policy_hash"]
    r = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id="S1", order_id=oid, instrument_id="FIX-EQ-1", side=side,
              quantity=qty, limit_price=limit, deadline_ns=h.clock.now_ns() + 60 * SEC, policy_hash=ph)
    assert r.accepted, r.body
    h.pump()


class JournalPnlTest(unittest.TestCase):
    def test_identity_holds_exactly_after_every_event(self):
        h = booted(self, authorize="250.00")
        conn = ro(self, h)
        store = snapshot_store(self)

        def day(bid: str):
            h.clock.advance(DAY)
            h.quote(bid, str(F(bid) + F("0.02")))

        eod(self, h, store, conn, {"FIX-EQ-1": "9.00"})
        day("9.00")                                              # buy 5 @ 9.00, commission report delayed
        _intent(h, "B1", "BUY", "5", "9.00")
        h.broker.fill("B1", F("5"), F("9.00"), delay_commission=True)
        h.pump()
        eod(self, h, store, conn, {"FIX-EQ-1": "9.10"})
        day("9.10")                                              # the final commission arrives later
        h.broker.release_commissions()
        h.pump()
        eod(self, h, store, conn, {"FIX-EQ-1": "9.20"})
        day("9.30")                                              # partial exit: sell 2 @ 9.30
        _intent(h, "S1a", "SELL", "2", "9.30")
        h.broker.fill("S1a", F("2"), F("9.30"))
        h.pump()
        eod(self, h, store, conn, {"FIX-EQ-1": "9.25"})
        day("9.25")                                              # settlement: availability changes, value does not
        h.broker.advance_settlement()
        self.assertTrue(h.reconcile().accepted)
        eod(self, h, store, conn, {"FIX-EQ-1": "9.25"})
        day("9.25")                                              # external capital flow into the system
        self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="50.00").accepted)
        eod(self, h, store, conn, {"FIX-EQ-1": "9.25"})
        day("9.40")                                              # close the position: sell 3 @ 9.40
        _intent(h, "S1b", "SELL", "3", "9.40")
        h.broker.fill("S1b", F("3"), F("9.40"))
        h.pump()
        eod(self, h, store, conn, {"FIX-EQ-1": "9.40"})
        self.assertEqual(h.verify(), [])

        for scope in (parse_scope("session:S1"), SYSTEM, ACCOUNT_SCOPE):
            r = pnl_report(store, conn, ACCOUNT, scope, "USD", as_of_ns=h.clock.now_ns())
            self.assertTrue(r["identity_holds"], (scope.label, r["unexplained_days"]))
            self.assertEqual(len(r["days"]), 6, scope.label)
            for d in r["days"]:
                self.assertEqual(d["residual"], "0.000000", (scope.label, d))
            self.assertEqual(r["totals"]["realized"], "1.800000")    # (9.30 - 9.00) x 2 + (9.40 - 9.00) x 3
            self.assertEqual(r["totals"]["unrealized_change"], "0.000000")  # flat at both ends
            self.assertEqual(r["totals"]["fees_estimated"], "0.000000")     # estimate reversed by the final report
            self.assertTrue(r["fixture_data"])
        session = pnl_report(store, conn, ACCOUNT, parse_scope("session:S1"), "USD", as_of_ns=h.clock.now_ns())
        by_day = {d["end"]: d for d in session["days"]}
        dates = sorted(by_day)
        self.assertEqual(by_day[dates[0]]["components"]["fees_estimated"], "-0.450000")  # estimate at the fill (1% cap)
        self.assertEqual(by_day[dates[1]]["components"]["fees_final"], "-1.000000")      # final report on its own day
        self.assertEqual(by_day[dates[1]]["components"]["fees_estimated"], "0.450000")   # estimate reversed
        settle_day = by_day[dates[3]]
        self.assertEqual((settle_day["pnl"], settle_day["external_flows"]), ("0.000000", "0.000000"))
        system = pnl_report(store, conn, ACCOUNT, SYSTEM, "USD", as_of_ns=h.clock.now_ns())
        flow_day = {d["end"]: d for d in system["days"]}[dates[4]]
        self.assertEqual((flow_day["external_flows"], flow_day["pnl"]), ("50.000000", "0.000000"))  # a flow is not P&L
        total = Fixed.parse(system["totals"]["pnl"])
        self.assertEqual(total, Fixed.parse("1.80") - Fixed.parse("3.00"))  # realized minus three 1.00 commissions

    def test_point_in_time(self):
        h = booted(self)
        conn = ro(self, h)
        store = snapshot_store(self)
        eod(self, h, store, conn, {"FIX-EQ-1": "9.00"})
        t0 = h.clock.now_ns()
        h.clock.advance(DAY)
        eod(self, h, store, conn, {"FIX-EQ-1": "9.00"})
        self.assertEqual(pnl_report(store, conn, ACCOUNT, SYSTEM, "USD", as_of_ns=t0)["days"], [])
        self.assertEqual(len(pnl_report(store, conn, ACCOUNT, SYSTEM, "USD", as_of_ns=h.clock.now_ns())["days"]), 1)


if __name__ == "__main__":
    unittest.main()
