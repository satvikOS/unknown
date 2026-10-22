"""Operational stress scenarios on actual positions (CAP-PORT-11; book Ch. 6).

Each scenario revalues the real ledger positions exactly and then runs the core's own
reaction on a private copy. Thresholds are reported as crossed, never as caps on the loss.
All values are TEST FIXTURES; scenario results are labelled hypothetical."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import F, SEC, booted, buy, db_hash, quotes, register_second, ro  # noqa: E402
from support import ACCOUNT, DEPLOYMENT  # noqa: E402

from unknown.analytics.common import parse_scope  # noqa: E402
from unknown.analytics.stress import correlated_selloff, fee_revision, overnight_gap, stale_quote  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402


def _policy_hash(h) -> str:
    sess = h.row("sessions", "session_id", "S1")
    return json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])[
        "policy_hash"]


class ShockScenarioTest(unittest.TestCase):
    """Session S1 holds 4 FIXA bought at 10.00 and 3 FIXB at 8.00, now bid 10.50 / 8.40 (NAV 101.20)."""

    def setUp(self):
        h = self.h = booted(self)
        register_second(h)
        buy(h, "S1", "B1", "4", "10.00", bid="10.00", ask="10.02")
        buy(h, "S1", "B2", "3", "8.00", instrument_id="FIX-EQ-2", bid="8.00", ask="8.02")
        h.quote("10.50", "10.52")
        h.quote("8.40", "8.42", "FIX-EQ-2")
        h.tick()  # a core valuation: session high water 101.20, account loss-budget baseline +1.20
        self.assertEqual(h.session_state("S1"), "RUNNING")
        self.assertEqual(h.verify(), [])
        self.conn = ro(self, h)
        self.scope = parse_scope("session:S1")

    def test_uniform_up_move_is_exactly_proportional(self):
        r = overnight_gap(self.conn, ACCOUNT, quotes(self.h), now_ns=self.h.clock.now_ns(), gap="0.05", scope=self.scope,
                          deployment_id=DEPLOYMENT)
        mv = Fixed.parse("67.200000")  # 4 x 10.50 + 3 x 8.40
        self.assertEqual(r["pnl_by_currency"]["USD"], (mv * Fixed.parse("0.05")).rescale(6).to_str())  # 3.360000
        self.assertEqual(r["label"].split(":")[0], "HYPOTHETICAL what-if")
        self.assertEqual(r["unmapped"], [])

    def test_gap_down_crosses_triggers_without_capping_the_loss_and_the_core_reacts(self):
        before = db_hash(self.h.db_path)
        r = overnight_gap(self.conn, ACCOUNT, quotes(self.h), now_ns=self.h.clock.now_ns(), gap="-0.10", scope=self.scope,
                          deployment_id=DEPLOYMENT)
        self.assertEqual(db_hash(self.h.db_path), before)
        self.assertEqual(r["pnl_by_currency"]["USD"], "-6.720000")
        t = {x["threshold"]: x for x in r["thresholds"]}
        dd = t["session_drawdown_trigger"]
        self.assertTrue(dd["crossed"])
        self.assertEqual((dd["level"], dd["equity_after"], dd["loss_beyond_threshold"]), ("99.176000", "94.480000", "4.696000"))
        self.assertIn("not capped", dd["consequence"])
        self.assertTrue(t["session_max_loss"]["crossed"])
        self.assertTrue(t["daily_loss_trigger"]["crossed"])
        self.assertEqual(t["daily_loss_trigger"]["loss_after"], "6.720000")
        self.assertFalse(t["cumulative_loss_allowance"]["crossed"])
        reaction = r["core_reaction"]
        types = [e["type"] for e in reaction["events"]]
        self.assertIn("SessionSuspended", types)
        self.assertTrue(reaction["stop_latched_after"])
        self.assertIn("DAILY_LOSS", reaction["latch_reason_after"])
        self.assertEqual(self.h.session_state("S1"), "RUNNING")  # the real session is untouched

    def test_resting_orders_through_a_gap(self):
        h = self.h
        ph = _policy_hash(h)
        for oid, side, qty, limit in (("RB", "BUY", "1", "10.00"), ("RS", "SELL", "2", "10.45")):
            res = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id="S1", order_id=oid, instrument_id="FIX-EQ-1",
                        side=side, quantity=qty, limit_price=limit, deadline_ns=h.clock.now_ns() + 60 * SEC, policy_hash=ph)
            self.assertTrue(res.accepted, res.body)
        h.pump()
        r = overnight_gap(self.conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), gap="-0.10", scope=self.scope,
                          deployment_id=DEPLOYMENT)
        orders = {o["order_id"]: o for o in r["resting_orders"]}
        self.assertEqual(orders["RB"]["effect"].split(" ")[0], "fills")
        self.assertEqual(orders["RB"]["immediate_marked_pnl"], "-0.550000")  # bought at 10.00, marked at 9.45
        self.assertIn("does not fill", orders["RS"]["effect"])
        self.assertEqual(r["pnl_by_currency"]["USD"], "-7.270000")

    def test_correlated_selloff_with_betas(self):
        r = correlated_selloff(self.conn, ACCOUNT, quotes(self.h), now_ns=self.h.clock.now_ns(), market_shock="-0.20",
                               betas={"FIX-EQ-2": "0.5"}, scope=self.scope, deployment_id=DEPLOYMENT)
        pos = {p["instrument_id"]: p for p in r["positions"]}
        self.assertEqual((pos["FIX-EQ-1"]["price_after"], pos["FIX-EQ-1"]["pnl"]), ("8.40", "-8.400000"))
        self.assertEqual((pos["FIX-EQ-2"]["price_after"], pos["FIX-EQ-2"]["pnl"]), ("7.56", "-2.520000"))
        self.assertEqual(r["pnl_by_currency"]["USD"], "-10.920000")
        self.assertIn("beta 1.0 assumed", r["parameters"]["beta_note"])


class StaleQuoteTest(unittest.TestCase):
    def test_stale_marks_drop_out_of_fresh_nav_and_the_gate_refuses_exits(self):
        h = booted(self)
        buy(h, "S1", "B1", "5", "9.00", bid="9.00", ask="9.02")
        conn = ro(self, h)
        r = stale_quote(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), delay_s=600, scope=parse_scope("session:S1"),
                        deployment_id=DEPLOYMENT)
        self.assertEqual([p["instrument_id"] for p in r["became_stale"]], ["FIX-EQ-1"])
        self.assertIsNotNone(r["fresh_nav_before"]["USD"])
        self.assertIsNone(r["fresh_nav_after"]["USD"])
        self.assertEqual(r["conservative_values"][0]["value_after_haircut"], "42.750000")  # 5 x 9.00 x 0.95
        self.assertFalse(r["core_reaction"]["valuation_ok_after"])
        gate = {g["side"]: g for g in r["order_gate"]}
        self.assertEqual(gate["SELL"]["code"], "QUOTE_FRESH")
        self.assertFalse(gate["BUY"]["approved"])
        self.assertIn("QUOTE_FRESH", {c["check"] for c in gate["BUY"]["failed_checks"]})
        self.assertIn("risk-reducing exits are also refused", r["finding"])


class FeeRevisionTest(unittest.TestCase):
    def test_revised_final_fees_reduce_available_cash_through_the_observation_path(self):
        h = booted(self)
        h.quote("10.00", "10.02")
        res = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id="S1", order_id="B1", instrument_id="FIX-EQ-1",
                    side="BUY", quantity="4", limit_price="10.00", deadline_ns=h.clock.now_ns() + 60 * SEC,
                    policy_hash=_policy_hash(h))
        self.assertTrue(res.accepted, res.body)
        h.pump()
        h.broker.fill("B1", F("4"), F("10.00"), delay_commission=True)
        h.pump()
        self.assertEqual(h.verify(), [])
        conn = ro(self, h)
        before = db_hash(h.db_path)
        r = fee_revision(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), factor="2", scope=parse_scope("session:S1"),
                         deployment_id=DEPLOYMENT)
        self.assertEqual(db_hash(h.db_path), before)
        (p,) = r["pending_estimates"]
        # estimate = min(max(4 x 0.005, 1.00), 1% of 40.00) = 0.40; revised = min(max(0.04, 2.00), 2% of 40.00) = 0.80
        self.assertEqual((p["estimate"], p["revised_final"], p["additional_cost"]), ("0.400000", "0.800000", "0.400000"))
        available_now = h.available("session:S1")
        after = {a["owner"]: a["available"] for a in r["core_reaction"]["available_cash_after"]}
        self.assertEqual(Fixed.parse(after["session:S1"]), available_now - Fixed.parse("0.40"))
        self.assertIn("CommissionFinalized", [e["type"] for e in r["core_reaction"]["events"]])
        self.assertEqual(r["exit_costs"][0]["difference"], "0.400000")


if __name__ == "__main__":
    unittest.main()
