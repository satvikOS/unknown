"""Holdings, cash ladder and NAV read model (CAP-PORT-01; ADR-0006; book Ch. 9).

All numbers are TEST FIXTURES produced through the real core (Harness). Every money-changing
step is followed by the ledger invariant check ``Harness.verify() == []``."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import (DAY, SEC, booted, buy, create_session, quotes, ro)  # noqa: E402
from support import ACCOUNT  # noqa: E402

from unknown.analytics.common import ACCOUNT as ACCOUNT_SCOPE, SYSTEM, parse_scope  # noqa: E402
from unknown.analytics.holdings import base_currency_total, holdings  # noqa: E402
from unknown.domain.fixed import Fixed, Rounding  # noqa: E402


def nav(report: dict, field: str = "nav_marked", ccy: str = "USD") -> str | None:
    return report["totals"][ccy][field]


class BucketPartitionTest(unittest.TestCase):
    def test_allocation_and_reservation_never_change_nav(self):
        """CAP-PORT-01: allocating, reserving or protecting cash leaves NAV unchanged; buckets partition NAV."""
        h = booted(self, session=False)
        conn = ro(self, h)
        before = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)
        self.assertEqual(nav(before), "300.000000")
        create_session(h)  # owner transfer unallocated -> session:S1
        self.assertEqual(h.verify(), [])
        after_alloc = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)
        self.assertEqual(nav(after_alloc), nav(before))
        owners = {(o["owner"]): o["nav_marked"] for o in after_alloc["owners"]}
        self.assertEqual(owners, {"session:S1": "100.000000", "unallocated": "200.000000"})
        # A working buy holds cash but changes no balance: NAV identical, available cash lower by the hold.
        buy(h, "S1", "B1", "5", "9.00", fill=False)
        self.assertEqual(h.verify(), [])
        reserved = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)
        self.assertEqual(nav(reserved), nav(before))
        s1 = next(o for o in reserved["owners"] if o["owner"] == "session:S1")
        self.assertEqual(s1["cash"]["cash_holds"], "46.200000")   # ceil(5 x 9.00) + fee envelope 1.00 + 20% margin
        self.assertEqual(s1["cash"]["available_cash"], "53.800000")
        account = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=ACCOUNT_SCOPE)
        total = sum((Fixed.parse(o["nav_marked"]) for o in account["owners"]), Fixed(0))
        self.assertEqual(total, Fixed.parse(nav(account)))

    def test_position_marked_at_bid_and_unrealized_against_cost(self):
        h = booted(self)
        conn = ro(self, h)
        buy(h, "S1", "B1", "5", "9.00", bid="8.99", ask="9.01")
        self.assertEqual(h.verify(), [])
        r = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=parse_scope("session:S1"))
        (p,) = r["positions"]
        self.assertEqual((p["quantity"], p["cost_basis"], p["market_value"], p["unrealized"]),
                         ("5", "45.000000", "44.950000", "-0.050000"))
        self.assertEqual(p["mark"]["status"], "fresh")
        self.assertTrue(p["fixture"])
        self.assertTrue(r["fixture_data"])
        s1 = r["owners"][0]
        # CASH 100 + PAYABLE -(45 + 1.00 commission) + 44.95 marked = 98.95
        self.assertEqual(s1["nav_fresh"], "98.950000")
        self.assertEqual(r["lineage"]["formulas"][:3], ["port.mark@1", "port.market_value@1", "port.nav@1"])


class MarkQualityTest(unittest.TestCase):
    def setUp(self):
        self.h = booted(self)
        self.conn = ro(self, self.h)
        buy(self.h, "S1", "B1", "5", "9.00", bid="9.10", ask="9.12")

    def test_unpriced_position_is_never_valued_at_zero(self):
        r = holdings(self.conn, ACCOUNT, {}, now_ns=self.h.clock.now_ns(), scope=SYSTEM)
        (p,) = r["positions"]
        self.assertIsNone(p["market_value"])
        self.assertEqual(p["mark"]["status"], "unpriced")
        self.assertIn("never valued at zero", p["valuation_note"])
        self.assertIsNone(nav(r))
        self.assertIsNone(nav(r, "nav_fresh"))
        self.assertEqual(r["totals"]["USD"]["unpriced_instruments"], ["FIX-EQ-1"])
        s1 = next(o for o in r["owners"] if o["owner"] == "session:S1")
        self.assertEqual(s1["unpriced_positions"][0]["cost_basis"], "45.000000")
        self.assertIsNone(p["weights"]["owner_nav"])

    def test_stale_mark_is_flagged_and_excluded_from_fresh_nav(self):
        q = quotes(self.h)
        later = self.h.clock.now_ns() + 10 * SEC  # policy freshness budget is 5,000 ms
        r = holdings(self.conn, ACCOUNT, q, now_ns=later, scope=SYSTEM)
        (p,) = r["positions"]
        self.assertEqual(p["mark"]["status"], "stale")
        self.assertEqual(p["mark"]["age_ms"], 10_000)
        self.assertEqual(p["market_value"], "45.500000")  # last mark, flagged, never zero
        self.assertIsNone(nav(r, "nav_fresh"))
        self.assertIsNotNone(nav(r))
        self.assertEqual(r["totals"]["USD"]["stale_instruments"], ["FIX-EQ-1"])
        self.assertEqual(r["totals"]["USD"]["valuation_status"], "stale_marks")
        relaxed = holdings(self.conn, ACCOUNT, q, now_ns=later, scope=SYSTEM, max_age_ms=60_000)
        self.assertEqual(nav(relaxed, "nav_fresh"), nav(r))

    def test_quote_received_after_valuation_instant_is_ignored(self):
        """Point-in-time guard: a valuation never uses a quote that did not exist yet."""
        earlier = self.h.clock.now_ns() - 1
        r = holdings(self.conn, ACCOUNT, quotes(self.h), now_ns=earlier, scope=SYSTEM)
        (p,) = r["positions"]
        self.assertEqual(p["mark"]["status"], "unpriced")
        self.assertIn("point-in-time", p["mark"]["reason"])

    def test_delayed_quote_is_never_fresh(self):
        q = quotes(self.h)
        q["FIX-EQ-1"] = {**q["FIX-EQ-1"], "delayed": True}
        r = holdings(self.conn, ACCOUNT, q, now_ns=self.h.clock.now_ns(), scope=SYSTEM)
        self.assertEqual(r["positions"][0]["mark"]["status"], "stale")
        self.assertIsNone(nav(r, "nav_fresh"))

    def test_mid_and_last_bases(self):
        q = quotes(self.h)
        mid = holdings(self.conn, ACCOUNT, q, now_ns=self.h.clock.now_ns(), scope=SYSTEM, mark_basis="mid")
        self.assertEqual(mid["positions"][0]["mark"]["price"], "9.110")
        self.assertEqual(mid["positions"][0]["market_value"], "45.550000")
        last = holdings(self.conn, ACCOUNT, q, now_ns=self.h.clock.now_ns(), scope=SYSTEM, mark_basis="last")
        self.assertEqual(last["positions"][0]["mark"]["status"], "unpriced")  # fixture quotes carry no last trade


class WeightsAndScopesTest(unittest.TestCase):
    def test_denominator_changes_only_the_weight(self):
        h = booted(self)
        conn = ro(self, h)
        buy(h, "S1", "B1", "5", "9.00", bid="8.99", ask="9.01")
        now = h.clock.now_ns()
        sys_r = holdings(conn, ACCOUNT, quotes(h), now_ns=now, scope=SYSTEM)
        ses_r = holdings(conn, ACCOUNT, quotes(h), now_ns=now, scope=parse_scope("owner:session:S1"))
        (a,), (b,) = sys_r["positions"], ses_r["positions"]
        for k in ("quantity", "cost_basis", "market_value", "unrealized"):
            self.assertEqual(a[k], b[k])
        self.assertEqual(a["weight_denominators"]["scope_nav"], "298.950000")
        self.assertEqual(b["weight_denominators"]["scope_nav"], "98.950000")
        mv = Fixed.parse(a["market_value"])
        for name, den in a["weight_denominators"].items():
            if den is None:
                continue
            self.assertEqual(a["weights"][name], mv.div(Fixed.parse(den), 8, Rounding.HALF_EVEN).to_str(), name)
        self.assertEqual(a["weights"]["session_seed"], "0.44950000")
        self.assertEqual(a["weights"]["authorized_capital"], "0.14983333")

    def test_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            parse_scope("owner:nobody")


class SettlementAndLadderTest(unittest.TestCase):
    def test_unsettled_quantity_until_broker_confirms_settlement(self):
        h = booted(self)
        conn = ro(self, h)
        buy(h, "S1", "B1", "5", "9.00", bid="8.99", ask="9.01")
        r = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)
        self.assertEqual((r["positions"][0]["settled_quantity"], r["positions"][0]["unsettled_quantity"]), ("0", "5"))
        h.clock.advance(DAY)
        h.broker.advance_settlement()
        rec = h.reconcile()
        self.assertTrue(rec.accepted, rec.body)
        self.assertEqual(h.verify(), [])
        h.quote("8.99", "9.01")
        r2 = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)
        self.assertEqual((r2["positions"][0]["settled_quantity"], r2["positions"][0]["unsettled_quantity"]), ("5", "0"))
        self.assertEqual(nav(r2), nav(r))  # settlement changes availability, never value

    def test_cash_ladder_partitions_ledger_cash(self):
        h = booted(self)
        conn = ro(self, h)
        buy(h, "S1", "B1", "5", "9.00", fill=False)
        r = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=ACCOUNT_SCOPE)
        (ladder,) = r["cash_ladder"]
        self.assertEqual(ladder["ledger_settled_cash"], "300.000000")
        self.assertEqual(ladder["reserved_cash_holds"], "46.200000")
        self.assertEqual(sum((Fixed.parse(v) for v in ladder["liquid_by_bucket"].values()), Fixed(0)), Fixed.parse("300"))
        self.assertIsNone(ladder["broker_settled_cash"])
        self.assertIn("reconciliation", ladder["broker_settled_cash_reason"])


class ReadOnlyTest(unittest.TestCase):
    def test_analytics_connection_cannot_write_the_core(self):
        h = booted(self, session=False)
        conn = ro(self, h)
        holdings(conn, ACCOUNT, {}, now_ns=h.clock.now_ns())
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM balances")


class BaseCurrencyTotalTest(unittest.TestCase):
    TOTALS = {"USD": {"nav_marked": "100.000000", "nav_fresh": "100.000000"},
              "EUR": {"nav_marked": "50.000000", "nav_fresh": None}}

    def test_missing_rate_makes_the_total_unavailable_not_partial(self):
        r = base_currency_total(self.TOTALS, "USD", None)
        self.assertIsNone(r["nav_marked"])
        self.assertIn("EUR", r["reason"])

    def test_reporting_conversion_with_a_supplied_rate(self):
        r = base_currency_total(self.TOTALS, "USD", {"EUR": {"rate": "1.08", "source": "fixture", "as_of_ns": 0}})
        self.assertEqual(r["nav_marked"], "154.000000")
        self.assertIsNone(r["nav_fresh"])  # one currency lacks a fresh NAV
        self.assertIn("never size transactions", r["note"])


if __name__ == "__main__":
    unittest.main()
