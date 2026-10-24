"""The core reserves a BUY's fee envelope from the account broker's fee schedule (ADR-0006 amendment 2026-10-24;
CAP-OMS-11). Prices, quantities and cash are TEST FIXTURES; the rates are config/fees/ibkr-pro-tiered-us.json's."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness, load_policy  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.money import Money  # noqa: E402
from unknown.policy.model import Policy  # noqa: E402
from unknown.risk import fees as policy_fees  # noqa: E402
from unknown.risk.fee_schedule import (IBKR_PRO_TIERED_US, FeeScheduleBook, load_schedule,  # noqa: E402
                                       parse_schedule)

F = Fixed.parse
SEC = 1_000_000_000
SESSION = "A"
OWNER = "session:A"
TRADE_DATE = date(2026, 10, 20)     # the harness clock's date (fixture)


def ibkr_harness(fee_schedules: FeeScheduleBook | None = None, seed: int = 1) -> Harness:
    """An account whose broker is ``ibkr`` (orders still go to the simulated broker: nothing leaves the process),
    with limits wide enough for a USD 300 order. Fixture amounts only."""
    policy = load_policy(account_limits__max_order_notional="400.00", account_limits__authorized_capital="600.00",
                         account_limits__max_gross_exposure="600.00")
    h = Harness(initial_cash="1000.00", policy=policy, provider="ibkr", fee_schedules=fee_schedules, seed=seed)
    h.bootstrap(authorize="600.00")
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=SESSION, seed="400.00",
                    target_terminal_equity="420.00")
    assert r.accepted and r.body["state"] == "RUNNING", r.body
    h.quote("0.99", "1.01")
    return h


def buy(h: Harness, order_id: str, qty: str, limit: str):
    sess = h.row("sessions", "session_id", SESSION)
    mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?",
                                            (sess["mandate_hash"],))[0]["body"])
    return h.cmd("submit_intent", account_key=ACCOUNT, session_id=SESSION, order_id=order_id, instrument_id="FIX-EQ-1",
                 side="BUY", quantity=qty, limit_price=limit, deadline_ns=h.clock.now_ns() + 60 * SEC,
                 policy_hash=mandate["policy_hash"])


def cash_hold(h: Harness, order_id: str) -> Fixed:
    return F(h.desk.store.query("SELECT amount FROM holds WHERE hold_id=?", (f"{order_id}:cash",))[0]["amount"])


def accepted_info(h: Harness, order_id: str) -> dict:
    rows = h.desk.store.query("SELECT payload FROM events WHERE type='OrderAccepted' AND order_id=?", (order_id,))
    return json.loads(rows[0]["payload"])["info"]


class ScheduleEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.s = load_schedule()

    def test_a_300_share_buy_at_1_reserves_at_least_the_schedule_bound(self):
        h = ibkr_harness()
        self.addCleanup(h.close)
        before = h.available(OWNER)
        r = buy(h, "B300", "300", "1.00")
        self.assertTrue(r.accepted, r.body)
        bound = self.s.pretrade_bound("BUY", F("300"), F("1.00"), TRADE_DATE)
        self.assertEqual(bound, F("2.011672"))
        order = h.row("orders", "order_id", "B300")
        self.assertGreaterEqual(F(order["fee_envelope"]), bound)
        self.assertEqual(F(order["fee_envelope"]), F("2.011672"))
        # the old fixed policy envelope under-reserved this order: 1.80 < 2.011672
        policy = Policy.parse(load_policy())
        old = policy_fees.envelope(policy.fees, F("300"), Money("USD", F("300.000000"))).amount
        self.assertEqual(old, F("1.80"))
        self.assertLess(old, F(order["fee_envelope"]))
        self.assertEqual(cash_hold(h, "B300"), F("302.011672"))
        self.assertEqual(h.available(OWNER), before - F("302.011672"))
        info = accepted_info(h, "B300")
        self.assertEqual(info["required_cash"], "302.011672")
        self.assertEqual(info["fee_envelope"]["basis"], "BROKER_SCHEDULE")
        self.assertEqual(info["fee_envelope"]["schedule_id"], self.s.schedule_id)
        self.assertEqual(info["fee_envelope"]["content_hash"], self.s.content_hash)
        check = next(c for c in info["decision"]["checks"] if c["check"] == "FEE_SCHEDULE")
        self.assertTrue(check["passed"])
        self.assertIn(self.s.schedule_id, check["detail"])
        self.assertEqual(h.verify(), [])

    def test_the_envelope_follows_the_order_size(self):
        h = ibkr_harness()
        self.addCleanup(h.close)
        h.quote("9.99", "10.01")
        for oid, qty in (("S1", "1"), ("S2", "10"), ("S3", "20")):
            with self.subTest(qty=qty):
                self.assertTrue(buy(h, oid, qty, "10.00").accepted)
                expected = self.s.pretrade_bound("BUY", F(qty), F("10.00"), TRADE_DATE)
                self.assertEqual(F(h.row("orders", "order_id", oid)["fee_envelope"]), expected)
                self.assertEqual(cash_hold(h, oid), F(qty) * F("10.00") + expected)
        # a 1-share order pays the per-order minimum: the envelope is never below it
        self.assertGreaterEqual(F(h.row("orders", "order_id", "S1")["fee_envelope"]), self.s.minimum)
        self.assertEqual(h.verify(), [])

    def test_a_whole_fill_estimates_its_fee_from_the_same_schedule_within_the_envelope(self):
        h = ibkr_harness()
        self.addCleanup(h.close)
        self.assertTrue(buy(h, "B300", "300", "1.00").accepted)
        h.pump()
        h.broker.fill("B300", F("300"), F("1.00"))
        h.pump()
        ex = json.loads(h.desk.store.query("SELECT row FROM executions WHERE order_id='B300'")[0]["row"])
        charge = self.s.charge("BUY", F("300"), F("1.00"), TRADE_DATE).total
        self.assertEqual(F(ex["fee_estimate"]), charge)
        self.assertLessEqual(charge, F(h.row("orders", "order_id", "B300")["fee_envelope"]))
        self.assertEqual([e for e in h.open_exceptions() if e["kind"] == "OVERCOMMITTED"], [])
        self.assertEqual(h.verify(), [])

    def test_replay_reproduces_the_envelope_and_is_deterministic(self):
        runs = []
        for _ in range(2):
            h = ibkr_harness(seed=7)
            self.addCleanup(h.close)
            self.assertTrue(buy(h, "B300", "300", "1.00").accepted)
            h.pump()
            h.broker.fill("B300", F("120"), F("0.99"))
            h.pump()
            self.assertEqual(h.verify(), [])
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(replay_and_compare(h.db_path, Path(tmp) / "replay.sqlite"), [])
            runs.append((accepted_info(h, "B300")["fee_envelope"], h.row("orders", "order_id", "B300")["fee_envelope"],
                         str(cash_hold(h, "B300"))))
        self.assertEqual(runs[0], runs[1])


class MissingScheduleTests(unittest.TestCase):
    def refused(self, book: FeeScheduleBook) -> tuple[Harness, dict]:
        h = ibkr_harness(book)
        self.addCleanup(h.close)
        before = h.available(OWNER)
        r = buy(h, "B1", "10", "1.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.body["code"], "FEE_SCHEDULE")
        self.assertIsNone(h.row("orders", "order_id", "B1"))
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM holds")[0]["n"], 0)
        self.assertEqual(h.available(OWNER), before)
        self.assertEqual(h.verify(), [])
        return h, next(c for c in r.body["details"]["decision"]["checks"] if c["check"] == "FEE_SCHEDULE")

    def test_no_schedule_for_the_broker_refuses_the_buy_with_the_reason(self):
        _, check = self.refused(FeeScheduleBook(()))
        self.assertFalse(check["passed"])
        self.assertIn("FEE_SCHEDULE_MISSING", check["detail"])
        self.assertIn("broker ibkr, venue XNAS", check["detail"])

    def test_a_schedule_that_does_not_cover_the_venue_refuses(self):
        body = json.loads(IBKR_PRO_TIERED_US.read_text())
        body["venues"] = ["XNYS"]
        _, check = self.refused(FeeScheduleBook((parse_schedule(body),)))
        self.assertIn("venue XNAS", check["detail"])

    def test_a_schedule_file_that_fails_to_load_is_named_in_the_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = json.loads(IBKR_PRO_TIERED_US.read_text())
            body["commission"]["status"] = "maybe"
            (Path(tmp) / "broken.json").write_text(json.dumps(body))
            book = FeeScheduleBook.from_dir(tmp)
        self.assertEqual(book.schedules, ())
        _, check = self.refused(book)
        self.assertIn("broken.json", check["detail"])

    def test_two_schedules_covering_one_order_refuse_as_ambiguous(self):
        body = json.loads(IBKR_PRO_TIERED_US.read_text())
        other = copy.deepcopy(body)
        other["schedule_id"] = "fee.ibkr.pro.fixed.us@2026-10-23"
        _, check = self.refused(FeeScheduleBook((parse_schedule(body), parse_schedule(other))))
        self.assertIn("FEE_SCHEDULE_AMBIGUOUS", check["detail"])


class SimulatorTests(unittest.TestCase):
    def test_the_simulated_broker_keeps_the_policy_fee_model_and_says_so(self):
        h = Harness(fee_schedules=FeeScheduleBook(()))      # no schedule at all: the simulator does not need one
        self.addCleanup(h.close)
        h.bootstrap()
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=SESSION, seed="150.00",
                        target_terminal_equity="157.50")
        self.assertTrue(r.accepted, r.body)
        self.assertTrue(buy(h, "B1", "10", "10.00").accepted)
        self.assertEqual(F(h.row("orders", "order_id", "B1")["fee_envelope"]), F("1.20"))
        info = accepted_info(h, "B1")
        self.assertEqual(info["fee_envelope"]["basis"], "POLICY_MODEL")
        self.assertIn("policy fee model", next(c for c in info["decision"]["checks"] if c["check"] == "FEE_SCHEDULE")["detail"])
        self.assertEqual(h.verify(), [])


class ScheduleScopeTests(unittest.TestCase):
    def test_every_schedule_venue_is_a_registered_venue_of_its_market_and_currency(self):
        from unknown.domain.markets import load_registry
        reg = load_registry()
        s = load_schedule()
        self.assertEqual(s.broker, "ibkr")
        known = {"BATS", "XASE"}           # US listing exchanges in the policy eligibility, not yet in the registry
        for mic in s.venues:
            with self.subTest(mic=mic):
                if mic in known:
                    continue
                v = reg.venue(mic)
                self.assertEqual((v.country, v.currency), (s.market, s.currency))

    def test_venues_must_be_mics_and_the_broker_must_be_named(self):
        body = json.loads(IBKR_PRO_TIERED_US.read_text())
        for mutate in (lambda b: b.__setitem__("venues", ["nasdaq"]), lambda b: b.pop("broker")):
            b = copy.deepcopy(body)
            mutate(b)
            with self.assertRaises(ValueError):
                parse_schedule(b)


if __name__ == "__main__":
    unittest.main()
