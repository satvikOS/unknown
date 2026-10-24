"""BR-DAT-02: which order states count as pending exposure, the core's CONCENTRATION and GROSS_EXPOSURE checks with
pending orders, and the strategy runner's use of the same definition against a real core.

Pending = the leaves of every order in a core open state (``core.state.OPEN_ORDER_STATES``): RESERVED,
SUBMIT_PENDING, ACKNOWLEDGED, PARTIALLY_FILLED, CANCEL_PENDING, UNKNOWN. Terminal orders (FILLED, CANCELED,
REJECTED, EXPIRED) count nothing. Every number is a TEST FIXTURE; every money-changing test checks the ledger
invariants."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT as H_ACCOUNT  # noqa: E402
from test_orders_lifecycle import SEC, make_harness, order, submit  # noqa: E402
from test_strategy_runner import (ACCOUNT, CLOCK_UTC, DATASET, RealCoreCase, runtime_spec, trend_bars,  # noqa: E402
                                  wait_for)
from unknown.broker.simulated import SubmitBehavior  # noqa: E402
from unknown.core.state import OPEN_ORDER_STATES, TERMINAL_ORDER_STATES  # noqa: E402
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.domain.clock import NS_PER_S, ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.money import Money  # noqa: E402
from unknown.policy.model import FeeModel  # noqa: E402
from unknown.research.bars import record_bars  # noqa: E402
from support.research_fixtures import fixture_instrument  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.store.db import connect  # noqa: E402
from unknown.strategy import artifacts  # noqa: E402
from unknown.strategy.coreview import PENDING_STATES, UNRESOLVED_STATES, CoreSnapshot, CoreView  # noqa: E402
from unknown.strategy.journal import decision_ids  # noqa: E402
from unknown.strategy.runner import RunnerConfig, StrategyRunner, decision_instant  # noqa: E402
from unknown.strategy.sizing import Holding  # noqa: E402

F = Fixed.parse
INST = "FIX-EQ-1"


def pending(h) -> tuple[Holding, tuple[dict, ...]]:
    """What the strategy runner reads (``CoreView``, read-only) for session A of a Harness core."""
    snap = CoreView(h.db_path).snapshot(H_ACCOUNT, "A", [INST])
    return snap.holdings[INST], snap.open_orders[INST]


class PendingStatesTest(unittest.TestCase):
    """The runner's pending exposure is the leaves of exactly the core's open states."""

    def setUp(self):
        self.h = make_harness()
        self.addCleanup(self.h.close)

    def assert_pending(self, state: str, pending_buy: str, owned: str = "0", pending_sell: str = "0"):
        hold, open_ = pending(self.h)
        self.assertEqual(order(self.h, "B1")["state"], state)
        self.assertEqual((hold.pending_buy, hold.owned, hold.pending_sell), (F(pending_buy), F(owned), F(pending_sell)), state)
        self.assertEqual([o["state"] for o in open_], [state] if state in OPEN_ORDER_STATES else [])
        self.assertEqual(self.h.verify(), [])

    def test_the_definition_is_the_cores_open_states(self):
        self.assertEqual(PENDING_STATES, ("RESERVED", "SUBMIT_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED",
                                          "CANCEL_PENDING", "UNKNOWN"))
        self.assertEqual(set(PENDING_STATES) & set(TERMINAL_ORDER_STATES), set())
        self.assertEqual(UNRESOLVED_STATES, ("UNKNOWN", "CANCEL_PENDING"))

    def test_reserved(self):
        self.assertTrue(submit(self.h, "B1", "BUY", "10", "10.00").accepted)
        self.assert_pending("RESERVED", "10")

    def test_submit_pending(self):
        seen = []
        real = self.h.broker.submit

        def spy(broker_order):
            seen.append(pending(self.h))
            return real(broker_order)
        self.h.broker.submit = spy
        self.assertTrue(submit(self.h, "B1", "BUY", "10", "10.00").accepted)
        self.h.pump()
        (hold, open_), = seen
        self.assertEqual((hold.pending_buy, [o["state"] for o in open_]), (F("10"), ["SUBMIT_PENDING"]))

    def test_acknowledged_partially_filled_and_filled(self):
        self.assertTrue(submit(self.h, "B1", "BUY", "10", "10.00").accepted)
        self.h.pump()
        self.assert_pending("ACKNOWLEDGED", "10")
        self.h.broker.fill("B1", F("4"), F("10.00"))
        self.h.pump()
        self.assert_pending("PARTIALLY_FILLED", "6", owned="4")       # leaves only: the filled 4 are owned
        self.h.broker.fill("B1", F("6"), F("10.00"))
        self.h.pump()
        self.assert_pending("FILLED", "0", owned="10")
        # a pending sell counts as sold
        self.assertTrue(submit(self.h, "S1", "SELL", "4", "10.50").accepted)
        hold, _ = pending(self.h)
        self.assertEqual((hold.pending_sell, hold.unreserved, hold.current), (F("4"), F("6"), F("6")))

    def test_cancel_pending_and_unknown_count_until_resolved(self):
        self.assertTrue(submit(self.h, "B1", "BUY", "10", "10.00").accepted)
        self.h.pump()
        self.h.broker.defer_cancel_confirmation(True)
        self.assertTrue(self.h.cmd("request_cancel", order_id="B1", reason="test").accepted)
        self.h.pump()
        self.assert_pending("CANCEL_PENDING", "10")
        self.assertTrue(self.h.broker.confirm_cancel("B1"))
        self.h.pump()
        self.assert_pending("CANCELED", "0")

    def test_unknown(self):
        self.h.broker.script_submit(SubmitBehavior.TIMEOUT_AFTER_ACCEPT)
        self.assertTrue(submit(self.h, "B1", "BUY", "10", "10.00").accepted)
        self.h.runner.pump_outbox()
        self.assert_pending("UNKNOWN", "10")

    def test_rejected_and_expired_count_nothing(self):
        self.h.broker.script_submit(SubmitBehavior.REJECT)
        self.assertTrue(submit(self.h, "B1", "BUY", "10", "10.00").accepted)
        self.h.pump()
        self.assert_pending("REJECTED", "0")
        h = make_harness()
        self.addCleanup(h.close)
        self.h = h
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00", deadline_ns=h.clock.now_ns() + SEC).accepted)
        h.clock.advance(2 * SEC)
        h.pump()
        self.assert_pending("EXPIRED", "0")


class PretradeWithPendingTest(unittest.TestCase):
    """The core's CONCENTRATION and GROSS_EXPOSURE checks reject an order because of pending exposure, and accept the
    same order without it (session A seeded with 150.00; fee envelope 1.20)."""

    def rejected(self, h, order_id: str, qty: str) -> dict:
        r = submit(h, order_id, "BUY", qty, "10.00")
        self.assertFalse(r.accepted, r.body)
        return {c["check"]: c for c in r.body["details"]["decision"]["checks"]} | {"_code": r.rejected_code}

    def test_concentration_counts_pending_buys_of_every_open_state(self):
        over = {"account_limits__max_position_bps_of_session_equity": 5000}     # 50% of 150.00 = 75.00
        control = make_harness(**over)
        self.addCleanup(control.close)
        self.assertTrue(submit(control, "B2", "BUY", "3", "10.00").accepted)   # 30.00 alone fits
        self.assertEqual(control.verify(), [])
        for state in ("ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING"):
            h = make_harness(**over)
            self.addCleanup(h.close)
            self.assertTrue(submit(h, "B1", "BUY", "5", "10.00").accepted)
            h.pump()
            if state == "PARTIALLY_FILLED":
                h.broker.fill("B1", F("2"), F("10.00"))                        # 2 owned + 3 leaves
                h.pump()
            if state == "CANCEL_PENDING":
                h.broker.defer_cancel_confirmation(True)
                self.assertTrue(h.cmd("request_cancel", order_id="B1", reason="test").accepted)
                h.pump()
            self.assertEqual(order(h, "B1")["state"], state)
            checks = self.rejected(h, "B2", "3")
            self.assertEqual(checks["_code"], "CONCENTRATION", state)
            self.assertFalse(checks["CONCENTRATION"]["passed"])
            # (owned + pending leaves + 3) x 10.00 = 80.00 in every state; the limit is 50% of the session equity
            # estimate (75.00, or a little less once a partial fill's lot is valued at the bid)
            self.assertTrue(checks["CONCENTRATION"]["detail"].startswith("position after 80.000000 limit 7"),
                            (state, checks["CONCENTRATION"]["detail"]))
            self.assertEqual(h.verify(), [])
            if state == "CANCEL_PENDING":                                     # once the cancel is confirmed it is gone
                self.assertTrue(h.broker.confirm_cancel("B1"))
                h.pump()
                self.assertTrue(submit(h, "B3", "BUY", "3", "10.00").accepted)
                self.assertEqual(h.verify(), [])

    def test_gross_exposure_counts_every_open_buy(self):
        over = {"account_limits__max_gross_exposure": "100.00"}
        control = make_harness(**over)
        self.addCleanup(control.close)
        self.assertTrue(submit(control, "B2", "BUY", "5", "10.00").accepted)   # 50.00 alone fits
        self.assertEqual(control.verify(), [])
        for state in ("RESERVED", "ACKNOWLEDGED"):
            h = make_harness(**over)
            self.addCleanup(h.close)
            self.assertTrue(submit(h, "B1", "BUY", "6", "10.00").accepted)
            if state == "ACKNOWLEDGED":
                h.pump()
            self.assertEqual(order(h, "B1")["state"], state)
            checks = self.rejected(h, "B2", "5")
            self.assertEqual(checks["_code"], "GROSS_EXPOSURE", state)
            self.assertEqual(checks["GROSS_EXPOSURE"]["detail"], "110.000000 > 100.00")
            self.assertTrue(checks["CONCENTRATION"]["passed"])
            self.assertEqual(h.verify(), [])


class OpenIntentGateTest(unittest.TestCase):
    """At most one live intent per session, artifact and instrument; no new intent beside an UNKNOWN or
    CANCEL_PENDING order (whoever placed it). The pending leaves still count as current exposure."""
    INSTS = ("FIX-EQ-1", "FIX-EQ-2")
    DAY = date(2026, 10, 21)
    AID = "TREND_DAILY_V1@0123456789abcdef"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.paths = Paths(root)
        self.paths.ensure()
        (root / "research").mkdir()
        self.t = decision_instant(self.DAY, CLOCK_UTC)
        store = ResearchStore(root / "research" / "research.sqlite")
        for i in self.INSTS:
            record_bars(store, trend_bars(i, self.DAY, 60, 800, 1000), DATASET, self.t)
        store.close()
        self.art = artifacts.LoadedArtifact(self.AID, "0" * 64, "paper", runtime_spec(self.INSTS), {}, date(2099, 1, 1),
                                            DATASET, True)

    def decide(self, open_orders: dict) -> dict:
        fees = FeeModel(F("0.005"), Money("USD", F("1.00")), 100, 2000, "TEST FIXTURE", "test")
        holdings = {i: Holding(pending_buy=sum((F(o["leaves_qty"]) for o in open_orders.get(i, ()) if o["side"] == "BUY"),
                                               Fixed(0))) for i in self.INSTS}
        snap = CoreSnapshot(ACCOUNT, "SIMULATED", "S1", "RUNNING", "p" * 64, "USD", 6,
                            {i: fixture_instrument(i) for i in self.INSTS}, holdings, F("10000"), F("10000"), fees,
                            open_orders=open_orders)
        with StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", self.paths), clock=ManualClock(self.t)) as r:
            rows = r._decide(self.art, self.DAY, self.t, self.t + 86_400 * NS_PER_S, self.t, snap, list(self.INSTS), {})
        return {x["instrument_id"]: x["decision_payload"] for x in rows}

    def test_gates(self):
        own = {"order_id": "sr-20261020-0123456789abcdef", "state": "ACKNOWLEDGED", "side": "BUY", "leaves_qty": "4",
               "principal_id": self.AID, "principal_role": "STRATEGY"}
        d = self.decide({"FIX-EQ-1": (own,)})
        self.assertEqual(d["FIX-EQ-1"]["outcome"], "BLOCKED")
        self.assertTrue(d["FIX-EQ-1"]["reason"].startswith("OPEN_INTENT: sr-20261020-0123456789abcdef (ACKNOWLEDGED, leaves 4)"))
        self.assertEqual(d["FIX-EQ-1"]["current_quantity"], "4")              # its leaves count as current exposure
        self.assertIsNone(d["FIX-EQ-2"]["outcome"])                           # an intent for the other name
        for state in UNRESOLVED_STATES:
            owner = {**own, "state": state, "principal_id": "owner", "principal_role": "OWNER"}
            d = self.decide({"FIX-EQ-1": (owner,)})
            self.assertEqual(d["FIX-EQ-1"]["outcome"], "BLOCKED", state)
            self.assertTrue(d["FIX-EQ-1"]["reason"].startswith(f"PENDING_UNRESOLVED: {own['order_id']} is {state}"), state)
        owner = {**own, "principal_id": "owner", "principal_role": "OWNER"}    # another principal's working order
        d = self.decide({"FIX-EQ-1": (owner,)})
        self.assertNotEqual(d["FIX-EQ-1"]["outcome"], "BLOCKED")
        self.assertEqual(d["FIX-EQ-1"]["current_quantity"], "4")


# ===================================================================== end to end
class OwnerOrderCase(RealCoreCase):
    LISTED = ("A",)

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def owner_buy(self, order_id: str, instrument: str, qty: str, limit: str) -> None:
        conn = connect(self.paths.db(ACCOUNT), readonly=True)
        try:
            sess = json.loads(conn.execute("SELECT row FROM sessions WHERE session_id='S1'").fetchone()["row"])
            mandate = json.loads(conn.execute("SELECT body FROM mandates WHERE mandate_hash=?",
                                              (sess["mandate_hash"],)).fetchone()["body"])
        finally:
            conn.close()
        self.cmd("submit_intent", account_key=ACCOUNT, session_id="S1", order_id=order_id, instrument_id=instrument,
                 side="BUY", quantity=qty, limit_price=limit, deadline_ns=time.time_ns() + 3600 * NS_PER_S,
                 policy_hash=mandate["policy_hash"])
        wait_for(lambda: self.state(order_id) == "ACKNOWLEDGED", what=f"{order_id} acknowledged")

    def state(self, order_id: str) -> str | None:
        rows = [o for o in self.orders() if o["order_id"] == order_id]
        return rows[0]["state"] if rows else None


class RunnerConcentrationWithPendingTest(OwnerOrderCase):
    """The owner's resting buy of 3 FIX-EQ-1 at 9.00 is pending exposure: the runner counts it as current (buys 1,
    not 4), and the core's CONCENTRATION check counts it too: (3 + 1) x 10.05 = 40.20 > 35% of the 100.00 session."""
    POLICY = {"account_limits__max_position_bps_of_session_equity": 3500}

    def test_pending_counts_in_the_runners_exposure_and_the_cores_concentration_check(self):
        a = self.ids["A"]
        self.owner_buy("owner-b1", "FIX-EQ-1", "3", "9.00")
        with self.runner("A", "live") as r:
            s = r.run_once()
            row = r.journal.get(self.day, "FIX-EQ-1")
        self.assertEqual((row["submit_payload"]["quantity"], row["decision_payload"]["current_quantity"]), ("1", "3"))
        one = s["instruments"]["FIX-EQ-1"]
        self.assertEqual((one["outcome"], one["submit_rejection"]), ("BLOCKED", "CONCENTRATION"), one)
        self.assertIn("CONCENTRATION (position after 40.200000 limit 35.000000)", one["reason"])   # 1 share alone: 10.05
        self.assertEqual([o["order_id"] for o in self.orders(a)], [])
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class RunnerGrossExposureWithPendingTest(OwnerOrderCase):
    """The owner's resting buy of 1 FIX-EQ-2 at 41.00 (pending) plus the runner's 4 FIX-EQ-1 at 10.05 is 81.20 of
    gross exposure, above the 60.00 limit: the core rejects the runner's intent (alone, 40.20 would fit)."""
    POLICY = {"account_limits__max_gross_exposure": "60.00"}

    def test_pending_buys_of_the_account_count_in_gross_exposure(self):
        a = self.ids["A"]
        self.owner_buy("owner-b2", "FIX-EQ-2", "1", "41.00")
        with self.runner("A", "live") as r:
            s = r.run_once()
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertEqual((one["outcome"], one["submit_rejection"]), ("BLOCKED", "GROSS_EXPOSURE"), one)
        self.assertIn("GROSS_EXPOSURE (81.200000 > 60.00)", one["reason"])
        self.assertEqual(two["outcome"], "NO_TRADE", two)                  # the owner's pending 1 is the target
        recs = {d["instrument_id"]: d for d in self.decisions(a)}
        self.assertEqual(recs["FIX-EQ-2"]["current_quantity"], "1")
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class RunnerUnresolvedPendingTest(OwnerOrderCase):
    """An owner order whose cancellation the broker has not confirmed (CANCEL_PENDING) may still fill: the runner
    emits no new intent for that instrument, and trades the other one."""

    def test_no_new_intent_beside_a_cancel_pending_order(self):
        a = self.ids["A"]
        self.owner_buy("owner-b3", "FIX-EQ-1", "3", "9.00")
        self.svc.broker.defer_cancel_confirmation(True)
        self.cmd("request_cancel", order_id="owner-b3", reason="test")
        wait_for(lambda: self.state("owner-b3") == "CANCEL_PENDING", what="CANCEL_PENDING")
        with self.runner("A", "live") as r:
            s = r.run_once()
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertEqual((one["outcome"], one["submitted"]), ("BLOCKED", False), one)
        self.assertTrue(one["reason"].startswith("PENDING_UNRESOLVED: owner-b3 is CANCEL_PENDING with leaves 3"), one["reason"])
        self.assertEqual(two["outcome"], "INTENT_SUBMITTED", two)
        self.assertEqual([o["instrument_id"] for o in self.orders(a)], ["FIX-EQ-2"])
        self.assertTrue(self.svc.broker.confirm_cancel("owner-b3"))
        wait_for(lambda: self.state("owner-b3") == "CANCELED", what="cancel confirmed")
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


del RealCoreCase, OwnerOrderCase     # bases only

if __name__ == "__main__":
    unittest.main()
