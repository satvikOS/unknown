"""Order lifecycle, reservations and broker-outcome handling (ADR-0009, ADR-0012).

Requirements exercised: LED-05 (reservations before submission), ORD-02
(reservation + outbox atomic before transmission), ORD-03 (timeout -> UNKNOWN,
reconcile before retry), ORD-04 (cancel request never releases), ORD-05
(partial fills, cancel/fill races, duplicate and out-of-order callbacks, late
fills), ORD-06 (execution dedup, linked corrections), ORD-07 (state,
transmission state and economic quantities kept separately), ORD-08 (decision
record with the first decisive reason) and RSK-01/RSK-02 (eligibility and
pretrade limits).

Every number is a TEST FIXTURE. Fee arithmetic used throughout (policy
simulated-default.v1: 0.005/share, 1.00 minimum, 100 bps cap, 2000 bps
envelope margin; simulated broker commission max(1.00, 0.005/share)):

* envelope for any qty <= 200 = 1.00 * 1.20 = 1.200000 (uncapped estimate + 20%)
* per-execution estimate = min(1.00, ceil(notional * 1%)) at ledger scale 6
"""
from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, FIXTURE_INSTRUMENT, Harness, load_policy  # noqa: E402

from unknown.broker.simulated import CancelBehavior, SubmitBehavior  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.instruments import TickRule  # noqa: E402
from unknown.market.quotes import Quote  # noqa: E402

F = Fixed.parse
SEC = 1_000_000_000
INST = "FIX-EQ-1"
SESSION = "A"
OWNER = "session:A"


# --------------------------------------------------------------------- helpers
def make_harness(*, seed: str = "150.00", target: str = "157.50", **policy_overrides) -> Harness:
    """Bootstrapped desk with one RUNNING session A seeded with ``seed`` (fixture, default 150.00)."""
    h = Harness(policy=load_policy(**policy_overrides) if policy_overrides else None)
    h.bootstrap()
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=SESSION, seed=seed,
                    target_terminal_equity=target)
    assert r.accepted and r.body["state"] == "RUNNING", r.body
    return h


def submit(h: Harness, order_id: str, side: str, qty: str, limit: str, *, instrument_id: str = INST,
           deadline_ns: int | None = None):
    """submit_intent with an explicit instrument and deadline (support.intent fixes both)."""
    sess = h.row("sessions", "session_id", SESSION)
    mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?",
                                            (sess["mandate_hash"],))[0]["body"])
    return h.cmd("submit_intent", account_key=ACCOUNT, session_id=SESSION, order_id=order_id,
                 instrument_id=instrument_id, side=side, quantity=qty, limit_price=limit,
                 deadline_ns=deadline_ns if deadline_ns is not None else h.clock.now_ns() + 60 * SEC,
                 policy_hash=mandate["policy_hash"])


def order(h: Harness, order_id: str) -> dict:
    return h.row("orders", "order_id", order_id)


def hold(h: Harness, order_id: str, kind: str = "cash") -> tuple[Fixed, str]:
    rows = h.desk.store.query("SELECT amount, status FROM holds WHERE hold_id=?", (f"{order_id}:{kind}",))
    assert len(rows) == 1, f"expected exactly one {kind} hold for {order_id}, got {len(rows)}"
    return F(rows[0]["amount"]), rows[0]["status"]


def outbox(h: Harness, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM outbox WHERE order_id=? ORDER BY created_seq, outbox_id", (order_id,))]


def executions(h: Harness, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM executions WHERE order_id=? ORDER BY rowid", (order_id,))]


def lots(h: Harness, instrument_id: str = INST) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM lots WHERE instrument_id=? ORDER BY rowid", (instrument_id,))]


def position(h: Harness, instrument_id: str = INST) -> Fixed:
    total = Fixed(0)
    for lot in lots(h, instrument_id):
        total = total + F(lot["open_qty"])
    return total


def exceptions_of(h: Harness, kind: str, *, status: str = "OPEN") -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM exceptions WHERE kind=? AND status=? ORDER BY rowid", (kind, status))]


def apply_next(h: Harness, n: int = 1) -> None:
    """Deliver exactly ``n`` queued broker events to the core, in queue order."""
    runner = h.runner
    for ev in h.broker.drain_events(n):
        runner.apply_event(ev)


def economic_state(h: Harness) -> dict:
    """Everything with economic meaning, free of generated ids and timestamps."""
    q = h.desk.store.query
    out = {
        "balances": sorted((r["account_code"], r["owner"], r["currency"], r["instrument_id"], F(r["amount"]))
                           for r in q("SELECT * FROM balances") if not F(r["amount"]).is_zero()),
        "holds": sorted((r["hold_id"], r["kind"], F(r["amount"]), r["status"]) for r in q("SELECT * FROM holds")),
        "lots": sorted((l["lot_id"], l["owner"], F(l["open_qty"]), F(l["open_cost"]))
                       for l in (json.loads(r["row"]) for r in q("SELECT row FROM lots"))),
        "executions": sorted((e["exec_key"], F(e["quantity"]), F(e["price"]), e["status"], e["fee_status"],
                              e["fee_final"]) for e in (json.loads(r["row"]) for r in q("SELECT row FROM executions"))),
        "settlements": sorted((s["settlement_id"], s["direction"], F(s["amount"]), s["status"])
                              for s in (json.loads(r["row"]) for r in q("SELECT row FROM settlements"))),
        "pending_commissions": q("SELECT COUNT(*) AS n FROM pending_commissions")[0]["n"],
        "open_exceptions": sorted(e["kind"] for e in h.open_exceptions()),
        "orders": {},
    }
    for r in q("SELECT row FROM orders"):
        o = json.loads(r["row"])
        out["orders"][o["order_id"]] = (o["state"], o["cancel_state"], F(o["cum_qty"]), F(o["leaves_qty"]),
                                        F(o["avg_price"]) if o["avg_price"] else None, F(o["notional_filled"]),
                                        F(o["fees_estimated"]), F(o["fees_final"]), o["broker_terminal_pending"])
    return out


class FixedAsserts:
    def assertFixed(self, actual, expected: str, msg: str | None = None):
        actual_f = actual if isinstance(actual, Fixed) else F(actual)
        self.assertEqual(actual_f, F(expected), msg or f"{actual_f.to_str()} != {expected}")


class _Base(FixedAsserts, unittest.TestCase):
    policy_overrides: dict = {}

    def setUp(self):
        self.h = make_harness(**self.policy_overrides)
        self.addCleanup(self.h.close)

    def bal(self, code: str, owner: str = OWNER, instrument_id: str = "") -> Fixed:
        return self.h.balance(code, owner, instrument_id=instrument_id)

    def assertClean(self):
        self.assertEqual(self.h.verify(), [])

    def acknowledged_buy(self, order_id: str = "B1", qty: str = "10", limit: str = "10.00") -> None:
        r = submit(self.h, order_id, "BUY", qty, limit)
        self.assertTrue(r.accepted, r.body)
        self.h.pump()
        self.assertEqual(order(self.h, order_id)["state"], "ACKNOWLEDGED")


# ============================================================ reservations
class ReservationTests(_Base):
    def test_buy_cash_hold_is_limit_notional_plus_fee_envelope(self):
        """LED-05, ADR-0009: an accepted BUY holds leaves*limit (ceiling) + the unused fee envelope."""
        h = self.h
        self.assertFixed(h.available(OWNER), "150.00")
        r = submit(h, "B1", "BUY", "10", "10.00")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body["state"], "RESERVED")
        amount, status = hold(h, "B1")
        # 10 * 10.00 = 100.000000 + envelope max(0.05, 1.00) * 1.20 = 1.200000
        self.assertFixed(amount, "101.200000")
        self.assertEqual(status, "ACTIVE")
        self.assertFixed(order(h, "B1")["fee_envelope"], "1.200000")
        self.assertFixed(h.available(OWNER), "48.800000")
        accepted = h.desk.store.query("SELECT payload FROM events WHERE type='OrderAccepted' AND order_id='B1'")
        self.assertFixed(json.loads(accepted[0]["payload"])["info"]["required_cash"], "101.200000")
        self.assertClean()

    def test_hold_recomputes_after_partial_fill_from_leaves_and_unused_envelope(self):
        """LED-05, ORD-05: after a partial fill the hold is ceil(leaves*limit) + (envelope - fees estimated)."""
        h = self.h
        self.acknowledged_buy()
        h.broker.fill("B1", F("4"), F("9.99"))
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "PARTIALLY_FILLED")
        self.assertFixed(o["cum_qty"], "4")
        self.assertFixed(o["leaves_qty"], "6")
        # estimate for 4 @ 9.99: min(1.00, ceil(39.96 * 1%)) = 0.399600
        self.assertFixed(o["fees_estimated"], "0.399600")
        amount, status = hold(h, "B1")
        self.assertFixed(amount, "60.800400")  # 6 * 10.00 + (1.200000 - 0.399600)
        self.assertEqual(status, "ACTIVE")
        self.assertFixed(self.bal("INVENTORY", instrument_id=INST), "39.960000")
        self.assertFixed(self.bal("COMMISSION"), "1.000000")
        self.assertFixed(self.bal("PAYABLE"), "-40.960000")
        # 150 - 40.96 payable - 60.8004 hold
        self.assertFixed(h.available(OWNER), "48.239600")
        self.assertClean()

    def test_cash_hold_rounds_sub_cent_notional_up_to_ledger_scale(self):
        """LED-05, ADR-0004/0006: leaves*limit beyond ledger scale is rounded with CEILING, never down."""
        h = self.h
        inst = dataclasses.replace(FIXTURE_INSTRUMENT, instrument_id="FIX-EQ-2", symbol="FIXB",
                                   name="Fixture Equity B (sub-penny ticks, test only)",
                                   tick_rules=(TickRule(Fixed(0), F("0.0000001")),))
        r = h.owner_cmd("register_instrument", instrument=inst.to_json(),
                        mappings=[{"provider": "simulated", "external_id": "FIX-EQ-2"}])
        self.assertTrue(r.accepted, r.body)
        h.quote("0.1234", "0.1236", instrument_id="FIX-EQ-2")
        r = submit(h, "SUB1", "BUY", "3", "0.1234567", instrument_id="FIX-EQ-2")
        self.assertTrue(r.accepted, r.body)
        # 3 * 0.1234567 = 0.3703701 -> ceil(6dp) 0.370371; + envelope 1.200000
        amount, _ = hold(h, "SUB1")
        self.assertFixed(amount, "1.570371")
        h.pump()
        h.broker.fill("SUB1", F("1"), F("0.1234567"), delay_commission=True)
        h.pump()
        # leaves 2 * 0.1234567 = 0.2469134 -> 0.246914; estimate ceil(0.123457 * 1%) = 0.001235
        self.assertFixed(order(h, "SUB1")["fees_estimated"], "0.001235")
        amount, _ = hold(h, "SUB1")
        self.assertFixed(amount, "1.445679")  # 0.246914 + (1.200000 - 0.001235)
        self.assertClean()

    def test_sell_reserves_owned_quantity_and_unowned_sell_is_rejected(self):
        """LED-05, RSK-01, ADR-0009: a sell holds owned quantity; selling unowned or reserved shares is refused."""
        h = self.h
        r = submit(h, "S0", "SELL", "1", "10.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "OWNED_QUANTITY")
        self.assertIsNone(order(h, "S0"))
        self.acknowledged_buy()
        h.broker.fill("B1", F("10"), F("10.00"))
        h.pump()
        self.assertEqual(position(h), Fixed(10))
        r = submit(h, "S1", "SELL", "6", "10.00")
        self.assertTrue(r.accepted, r.body)
        amount, status = hold(h, "S1", "qty")
        self.assertFixed(amount, "6")
        self.assertEqual(status, "ACTIVE")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM holds WHERE order_id='S1' AND kind='CASH'")[0]["n"], 0)
        r = submit(h, "S2", "SELL", "5", "10.00")  # only 4 unreserved
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "OWNED_QUANTITY")
        r = submit(h, "S3", "SELL", "4", "10.00")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        h.broker.fill("S1", F("2"), F("10.00"))
        h.pump()
        amount, status = hold(h, "S1", "qty")
        self.assertFixed(amount, "4")  # leaves of S1
        self.assertEqual(status, "ACTIVE")
        self.assertEqual(position(h), Fixed(8))
        self.assertClean()

    def test_reservation_and_outbox_commit_atomically_before_transmission(self):
        """ORD-02, LED-05: holds, order and SUBMIT outbox land in one event; nothing is sent at intent time."""
        h = self.h
        r = submit(h, "B1", "BUY", "10", "10.00")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(len(r.events), 1)
        payload = json.loads(h.desk.store.query("SELECT payload FROM events WHERE seq=?", (r.events[0],))[0]["payload"])
        changes = payload["changes"]
        self.assertEqual([o["state"] for o in changes["orders"]], ["RESERVED"])
        self.assertEqual([(x["kind"], x["status"]) for x in changes["holds"]], [("CASH", "ACTIVE")])
        self.assertEqual([(x["action"], x["state"]) for x in changes["outbox"]], [("SUBMIT", "PENDING")])
        self.assertFalse(h.broker.lookup("B1").found)
        self.assertEqual(h.broker.pending_event_count(), 0)
        self.assertClean()

    def test_submit_pending_is_committed_before_the_broker_is_called(self):
        """ORD-02, ADR-0009 Submission 1: the sender commits SUBMIT_PENDING (holds intact) before calling the broker."""
        h = self.h
        seen = []
        real_submit = h.broker.submit

        def spy(broker_order):
            o = order(h, broker_order.client_order_id)
            seen.append((o["state"], hold(h, broker_order.client_order_id),
                         [b["state"] for b in outbox(h, broker_order.client_order_id)]))
            return real_submit(broker_order)

        h.broker.submit = spy
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        h.pump()
        self.assertEqual(seen, [("SUBMIT_PENDING", (F("101.200000"), "ACTIVE"), ["SENDING"])])
        self.assertEqual(order(h, "B1")["state"], "ACKNOWLEDGED")
        self.assertEqual(outbox(h, "B1")[0]["state"], "DONE")
        self.assertClean()

    def test_intent_deadline_expiry_before_transmission_releases_and_never_sends(self):
        """ORD-02, ADR-0009: an intent whose deadline passes before transmission expires locally and is never sent."""
        h = self.h
        r = submit(h, "B1", "BUY", "10", "10.00", deadline_ns=h.clock.now_ns() + 1 * SEC)
        self.assertTrue(r.accepted, r.body)
        self.assertFixed(h.available(OWNER), "48.800000")
        h.clock.advance(2 * SEC)
        stats = h.pump()
        self.assertEqual(stats.sent, 0)
        o = order(h, "B1")
        self.assertEqual(o["state"], "EXPIRED")
        self.assertFixed(o["leaves_qty"], "0")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertEqual([b["state"] for b in outbox(h, "B1")], ["SKIPPED"])
        self.assertFalse(h.broker.lookup("B1").found)
        self.assertFixed(h.available(OWNER), "150.000000")
        self.assertClean()

    def test_intent_already_past_deadline_is_rejected_by_pretrade(self):
        """ORD-08, ADR-0009: an intent evaluated after its deadline is refused with DEADLINE and reserves nothing."""
        h = self.h
        r = submit(h, "B1", "BUY", "1", "10.00", deadline_ns=h.clock.now_ns() - 1)
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "DEADLINE")
        self.assertIsNone(order(h, "B1"))
        self.assertFixed(h.available(OWNER), "150.000000")
        self.assertClean()


# ============================================================ cancellation
class CancelTests(_Base):
    def test_cancel_request_releases_nothing_until_broker_confirms(self):
        """ORD-04, ORD-07: CANCEL_PENDING keeps the full hold; only the broker CANCELED confirmation releases it."""
        h = self.h
        self.acknowledged_buy()
        h.broker.defer_cancel_confirmation(True)
        r = h.cmd("request_cancel", order_id="B1", reason="test")
        self.assertTrue(r.accepted, r.body)
        o = order(h, "B1")
        self.assertEqual((o["state"], o["prev_state"], o["cancel_state"]), ("CANCEL_PENDING", "ACKNOWLEDGED", "REQUESTED"))
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))
        self.assertFixed(h.available(OWNER), "48.800000")
        self.assertEqual([(b["action"], b["state"]) for b in outbox(h, "B1")], [("SUBMIT", "DONE"), ("CANCEL", "PENDING")])
        h.pump()  # cancel transmitted, broker reports PENDING_CANCEL
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCEL_PENDING", "ACKED"))
        self.assertFixed(o["leaves_qty"], "10")
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))
        self.assertFixed(h.available(OWNER), "48.800000")
        self.assertTrue(h.broker.confirm_cancel("B1"))
        h.pump()
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertFixed(o["leaves_qty"], "0")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertFixed(h.available(OWNER), "150.000000")
        self.assertClean()

    def test_cancel_rejected_too_late_restores_prior_state_and_keeps_hold(self):
        """ORD-04, ADR-0009: a cancel rejection restores the prior state and still releases nothing."""
        h = self.h
        self.acknowledged_buy()
        h.broker.fill("B1", F("4"), F("10.00"))
        h.pump()
        held = hold(h, "B1")
        h.broker.script_cancel(CancelBehavior.REJECT_TOO_LATE)
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        h.pump()
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("PARTIALLY_FILLED", "REJECTED"))
        self.assertEqual(hold(h, "B1"), held)
        self.assertEqual(held[1], "ACTIVE")
        self.assertClean()

    def test_cancel_rejection_after_a_race_fill_restores_partially_filled(self):
        """ORD-04, ORD-05, ORD-07: a fill applied "normally" during CANCEL_PENDING advances the state the
        cancel rejection restores; the label must agree with cum_qty (PARTIALLY_FILLED, not ACKNOWLEDGED)."""
        h = self.h
        self.acknowledged_buy()
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        self.assertEqual(order(h, "B1")["state"], "CANCEL_PENDING")
        h.broker.fill("B1", F("4"), F("10.00"))
        h.runner.drain_events()  # the fill lands before the cancel is transmitted
        o = order(h, "B1")
        self.assertEqual(o["state"], "CANCEL_PENDING")
        self.assertFixed(o["cum_qty"], "4")
        h.broker.script_cancel(CancelBehavior.REJECT_TOO_LATE)
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["cancel_state"], "REJECTED")
        self.assertFixed(o["cum_qty"], "4")
        self.assertFixed(o["leaves_qty"], "6")
        self.assertEqual(hold(h, "B1"), (F("60.800000"), "ACTIVE"))  # 6 * 10.00 + (1.20 - 0.40); not released
        self.assertEqual(o["state"], "PARTIALLY_FILLED")
        self.assertClean()

    def test_cancel_before_transmission_releases_immediately_and_never_sends(self):
        """ORD-02, ORD-04: an order still RESERVED with a PENDING submit is canceled locally; nothing reaches the broker."""
        h = self.h
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertEqual(h.pump().sent, 0)
        self.assertFalse(h.broker.lookup("B1").found)
        self.assertFixed(h.available(OWNER), "150.000000")
        self.assertClean()

    def test_fill_during_cancel_pending_consumes_its_share_of_the_hold(self):
        """ORD-04, ORD-05: a fill racing a cancel is applied normally; confirmation releases only the remainder."""
        h = self.h
        self.acknowledged_buy()
        h.broker.defer_cancel_confirmation(True)
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        h.pump()
        h.broker.fill("B1", F("3"), F("10.00"))
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "CANCEL_PENDING")
        self.assertFixed(o["cum_qty"], "3")
        self.assertFixed(o["leaves_qty"], "7")
        self.assertFixed(o["fees_estimated"], "0.300000")  # ceil(30.00 * 1%)
        self.assertEqual(hold(h, "B1"), (F("70.900000"), "ACTIVE"))  # 7 * 10.00 + (1.20 - 0.30)
        self.assertFixed(h.available(OWNER), "48.100000")  # 150 - 31.00 payable - 70.90
        self.assertTrue(h.broker.confirm_cancel("B1"))
        h.pump()
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertFixed(o["cum_qty"], "3")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertEqual(position(h), Fixed(3))
        self.assertFixed(h.available(OWNER), "119.000000")  # 150 - 30.00 - 1.00 commission
        self.assertClean()

    def test_full_fill_wins_the_race_against_a_pending_cancel(self):
        """ORD-05: when the remainder fills before the cancel is confirmed the order ends FILLED, not CANCELED."""
        h = self.h
        self.acknowledged_buy()
        h.broker.defer_cancel_confirmation(True)
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        h.pump()
        h.broker.fill("B1", F("10"), F("10.00"))
        self.assertFalse(h.broker.confirm_cancel("B1"))
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "FILLED")
        self.assertFixed(o["cum_qty"], "10")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertEqual(position(h), Fixed(10))
        self.assertFixed(h.available(OWNER), "49.000000")  # 150 - 100.00 - 1.00
        self.assertEqual(h.open_exceptions(), [])
        self.assertClean()


# ====================================================== book Ch.2 worked case
class WorkedLifecycleTests(FixedAsserts, unittest.TestCase):
    """Book Ch.2: BUY 10 @ 10.00 limit; 4 fill @ 9.99; cancel; 2 more fill; final position 6."""

    def run_lifecycle(self, *, shuffle: bool) -> tuple[Harness, dict]:
        h = make_harness()
        self.addCleanup(h.close)
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        h.pump()
        h.broker.fill("B1", F("4"), F("9.99"))
        h.pump()
        self.assertEqual(hold(h, "B1"), (F("60.800400"), "ACTIVE"))
        h.broker.defer_cancel_confirmation(True)
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "CANCEL_PENDING")
        h.broker.fill("B1", F("2"), F("10.00"))       # queues: execution, commission, PENDING_CANCEL(6)
        self.assertTrue(h.broker.confirm_cancel("B1"))  # queues: CANCELED(6)
        self.assertEqual(h.broker.pending_event_count(), 4)
        if shuffle:
            h.broker.shuffle_pending_events([3, 0, 1, 2])  # confirmation first, then the last fill
            apply_next(h, 1)
            o = order(h, "B1")
            # A terminal status ahead of its executions keeps the reservation.
            self.assertEqual(o["state"], "CANCEL_PENDING")
            self.assertEqual(o["broker_terminal_pending"]["status"], "CANCELED")
            self.assertFixed(o["broker_terminal_pending"]["filled"], "6")
            self.assertEqual(hold(h, "B1"), (F("60.800400"), "ACTIVE"))
        h.pump()
        return h, economic_state(h)

    def assert_final(self, h: Harness):
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertFixed(o["cum_qty"], "6")
        self.assertFixed(o["leaves_qty"], "0")
        self.assertFixed(o["avg_price"], "9.99333333")    # (39.96 + 20.00) / 6, HALF_EVEN at 8dp
        self.assertFixed(o["fees_estimated"], "0.599600")  # 0.399600 + 0.200000
        self.assertFixed(o["fees_final"], "2.000000")
        self.assertIsNone(o["broker_terminal_pending"])
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertEqual(position(h), Fixed(6))
        self.assertFixed(h.balance("INVENTORY", OWNER, instrument_id=INST), "59.960000")
        self.assertFixed(h.balance("PAYABLE", OWNER), "-61.960000")
        self.assertFixed(h.balance("COMMISSION", OWNER), "2.000000")
        self.assertFixed(h.balance("FEE_ESTIMATE", OWNER), "0")
        self.assertFixed(h.balance("FEES_ACCRUED", OWNER), "0")
        self.assertFixed(h.available(OWNER), "88.040000")
        self.assertEqual(h.open_exceptions(), [])
        self.assertEqual(h.verify(), [])
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)

    def test_worked_lifecycle_in_delivery_order(self):
        """ORD-04, ORD-05 (book Ch.2): partial fill, cancel, fill in the race window, confirmation -> position 6."""
        h, _ = self.run_lifecycle(shuffle=False)
        self.assert_final(h)

    def test_cancel_confirmation_before_last_fill_reaches_the_same_state(self):
        """ORD-05 (book Ch.2): confirmation delivered before the last fill yields the identical economic state."""
        h1, in_order = self.run_lifecycle(shuffle=False)
        h2, shuffled = self.run_lifecycle(shuffle=True)
        self.assert_final(h2)
        self.assertEqual(shuffled, in_order)


# ============================================== callbacks and commissions
class CallbackTests(_Base):
    def test_duplicate_execution_callbacks_have_one_economic_effect(self):
        """ORD-05, ORD-06: three deliveries of one execution (and a reconciliation re-read) book once."""
        h = self.h
        self.acknowledged_buy()
        h.broker.fill("B1", F("4"), F("9.99"), duplicate_callbacks=2)
        self.assertEqual(h.broker.pending_event_count(), 5)  # 3 x execution, commission, status
        h.pump()
        self.assertEqual(len(executions(h, "B1")), 1)
        o = order(h, "B1")
        self.assertFixed(o["cum_qty"], "4")
        self.assertFixed(o["leaves_qty"], "6")
        before = economic_state(h)
        rec = h.reconcile()  # executions_since re-delivers the same execution and commission
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(economic_state(h), before)
        self.assertEqual(position(h), Fixed(4))
        self.assertFixed(self.bal("INVENTORY", instrument_id=INST), "39.960000")
        self.assertFixed(self.bal("COMMISSION"), "1.000000")
        self.assertFixed(self.bal("PAYABLE"), "-40.960000")
        self.assertEqual(hold(h, "B1"), (F("60.800400"), "ACTIVE"))
        self.assertClean()

    def test_delayed_commission_keeps_estimate_until_released(self):
        """ORD-05, LED-07, ADR-0012: a delayed commission leaves the estimate accrued; its release finalizes exactly."""
        h = self.h
        self.acknowledged_buy()
        h.broker.fill("B1", F("4"), F("9.99"), delay_commission=True)
        h.pump()
        (ex,) = executions(h, "B1")
        self.assertEqual(ex["fee_status"], "ESTIMATED")
        self.assertFixed(ex["fee_estimate"], "0.399600")
        self.assertFixed(self.bal("FEE_ESTIMATE"), "0.399600")
        self.assertFixed(self.bal("FEES_ACCRUED"), "-0.399600")
        self.assertFixed(self.bal("COMMISSION"), "0")
        self.assertFixed(self.bal("PAYABLE"), "-39.960000")
        stl = h.row("settlements", "settlement_id", "stl:" + ex["exec_key"])
        self.assertFixed(stl["amount"], "39.960000")
        self.assertFixed(order(h, "B1")["fees_final"], "0")
        self.assertFixed(h.available(OWNER), "48.840000")  # 150 - 39.96 - 0.3996 - 60.8004
        self.assertEqual(h.broker.release_commissions(), 1)
        h.pump()
        (ex,) = executions(h, "B1")
        self.assertEqual(ex["fee_status"], "FINAL")
        self.assertFixed(ex["fee_final"], "1.000000")
        self.assertFixed(self.bal("FEE_ESTIMATE"), "0")
        self.assertFixed(self.bal("FEES_ACCRUED"), "0")
        self.assertFixed(self.bal("COMMISSION"), "1.000000")
        self.assertFixed(self.bal("PAYABLE"), "-40.960000")
        stl = h.row("settlements", "settlement_id", "stl:" + ex["exec_key"])
        self.assertFixed(stl["amount"], "40.960000")
        self.assertFixed(order(h, "B1")["fees_final"], "1.000000")
        self.assertEqual(hold(h, "B1"), (F("60.800400"), "ACTIVE"))
        self.assertFixed(h.available(OWNER), "48.239600")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertClean()

    def test_commission_before_its_execution_is_deferred_then_applied(self):
        """ORD-05, ADR-0012: a commission report that overtakes its execution is parked, then applied once."""
        reference = make_harness()
        self.addCleanup(reference.close)
        for hh in (reference, self.h):
            self.assertTrue(submit(hh, "B1", "BUY", "10", "10.00").accepted)
            hh.pump()
            hh.broker.fill("B1", F("4"), F("9.99"))
        reference.pump()
        h = self.h
        self.assertEqual(h.broker.pending_event_count(), 3)  # execution, commission, status
        h.broker.shuffle_pending_events([1, 0, 2])
        apply_next(h, 1)
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM pending_commissions")[0]["n"], 1)
        self.assertEqual(executions(h, "B1"), [])
        self.assertFixed(self.bal("COMMISSION"), "0")
        h.pump()
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM pending_commissions")[0]["n"], 0)
        (ex,) = executions(h, "B1")
        self.assertEqual(ex["fee_status"], "FINAL")
        self.assertFixed(self.bal("COMMISSION"), "1.000000")
        self.assertFixed(self.bal("FEES_ACCRUED"), "0")
        self.assertFixed(self.bal("PAYABLE"), "-40.960000")
        self.assertEqual(economic_state(h), economic_state(reference))
        self.assertClean()

    def test_terminal_status_before_executions_keeps_reservation(self):
        """ORD-05, ADR-0009: FILLED reported ahead of its execution keeps the hold until the execution is ingested."""
        h = self.h
        self.acknowledged_buy()
        h.broker.fill("B1", F("10"), F("9.99"))
        h.broker.shuffle_pending_events([2, 0, 1])  # status FILLED(10) first
        apply_next(h, 1)
        o = order(h, "B1")
        self.assertEqual(o["state"], "ACKNOWLEDGED")
        self.assertFixed(o["cum_qty"], "0")
        self.assertEqual(o["broker_terminal_pending"]["status"], "FILLED")
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))
        self.assertFixed(h.available(OWNER), "48.800000")
        apply_next(h, 1)  # the execution
        o = order(h, "B1")
        self.assertEqual(o["state"], "FILLED")
        self.assertFixed(o["cum_qty"], "10")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        h.pump()  # the commission
        # 150 - 99.90 - 1.00 commission
        self.assertFixed(h.available(OWNER), "49.100000")
        self.assertEqual(h.open_exceptions(), [])
        self.assertClean()

    def test_execution_correction_reverses_and_rebooks_exactly(self):
        """ORD-06, ADR-0009: a corrected execution is linked to its original, fully reversed and rebooked."""
        h = self.h
        self.acknowledged_buy()
        original = h.broker.fill("B1", F("4"), F("9.99"))
        h.pump()
        orig_entries = executions(h, "B1")[0]["effects"]["entries"] + executions(h, "B1")[0]["fee_entries"]
        self.assertEqual(len(orig_entries), 3)  # trade, fee estimate, commission
        corrected = h.broker.correct_execution(original.exec_key, F("4"), F("9.98"))
        self.assertEqual(corrected.correction_of, original.exec_key)
        h.pump()
        old = h.row("executions", "exec_key", original.exec_key)
        new = h.row("executions", "exec_key", corrected.exec_key)
        self.assertEqual((old["status"], old["corrected_by"]), ("CORRECTED", corrected.exec_key))
        self.assertEqual((new["status"], new["correction_of"]), ("ACTIVE", original.exec_key))
        self.assertFixed(new["notional"], "39.920000")
        self.assertFixed(new["fee_final"], "1.000000")
        reversed_ids = sorted(r["reverses"] for r in h.desk.store.query(
            "SELECT reverses FROM journal_entries WHERE kind='REVERSAL'"))
        self.assertEqual(reversed_ids, sorted(orig_entries))
        self.assertEqual(h.row("lots", "lot_id", "lot:" + original.exec_key)["open_qty"], "0")
        new_lot = h.row("lots", "lot_id", "lot:" + corrected.exec_key)
        self.assertFixed(new_lot["open_qty"], "4")
        self.assertFixed(new_lot["open_cost"], "39.920000")
        self.assertEqual(position(h), Fixed(4))
        self.assertFixed(self.bal("INVENTORY", instrument_id=INST), "39.920000")
        self.assertFixed(self.bal("COMMISSION"), "1.000000")
        self.assertFixed(self.bal("FEE_ESTIMATE"), "0")
        self.assertFixed(self.bal("FEES_ACCRUED"), "0")
        self.assertFixed(self.bal("PAYABLE"), "-40.920000")
        self.assertEqual(h.row("settlements", "settlement_id", "stl:" + original.exec_key)["status"], "CANCELLED")
        stl = h.row("settlements", "settlement_id", "stl:" + corrected.exec_key)
        self.assertEqual(stl["status"], "EXPECTED")
        self.assertFixed(stl["amount"], "40.920000")
        o = order(h, "B1")
        self.assertFixed(o["cum_qty"], "4")
        self.assertFixed(o["leaves_qty"], "6")
        self.assertFixed(o["avg_price"], "9.98")
        self.assertFixed(o["notional_filled"], "39.92")
        self.assertFixed(o["fees_estimated"], "0.399200")
        self.assertFixed(o["fees_final"], "1.000000")
        self.assertEqual(hold(h, "B1"), (F("60.800800"), "ACTIVE"))  # 60 + (1.20 - 0.3992)
        self.assertEqual(h.open_exceptions(), [])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertClean()


    def test_quantity_correction_restores_leaves_and_hold(self):
        """ORD-06, LED-05: a correction to fewer shares reverses the original, rebooks 3 and re-reserves the freed share."""
        h = self.h
        self.acknowledged_buy()
        original = h.broker.fill("B1", F("4"), F("9.99"))
        h.pump()
        corrected = h.broker.correct_execution(original.exec_key, F("3"), F("9.99"))
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "PARTIALLY_FILLED")
        self.assertFixed(o["cum_qty"], "3")
        self.assertFixed(o["leaves_qty"], "7")
        self.assertFixed(o["fees_estimated"], "0.299700")  # ceil(29.97 * 1%)
        self.assertEqual(hold(h, "B1"), (F("70.900300"), "ACTIVE"))  # 7 * 10.00 + (1.20 - 0.2997)
        self.assertEqual(position(h), Fixed(3))
        self.assertFixed(self.bal("INVENTORY", instrument_id=INST), "29.970000")
        self.assertFixed(self.bal("COMMISSION"), "1.000000")
        self.assertFixed(self.bal("PAYABLE"), "-30.970000")
        self.assertEqual(h.row("executions", "exec_key", original.exec_key)["corrected_by"], corrected.exec_key)
        self.assertEqual(h.open_exceptions(), [])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertClean()

    def test_correction_of_a_completed_order_rebooks_without_anomaly(self):
        """ORD-06: a price correction on a FILLED order is not a late fill: no anomaly, hold stays released."""
        h = self.h
        self.acknowledged_buy()
        original = h.broker.fill("B1", F("10"), F("10.00"))
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "FILLED")
        h.broker.correct_execution(original.exec_key, F("10"), F("9.98"))
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "FILLED")
        self.assertFixed(o["cum_qty"], "10")
        self.assertFixed(o["avg_price"], "9.98")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertFixed(self.bal("INVENTORY", instrument_id=INST), "99.800000")
        self.assertFixed(self.bal("PAYABLE"), "-100.800000")
        self.assertEqual(h.open_exceptions(), [])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertClean()


class FeeOverrunTests(FixedAsserts, unittest.TestCase):
    def test_commission_above_envelope_is_recorded_and_opens_blocking_overcommitment(self):
        """LED-05, ADR-0006 (observation path): a fee beyond the reserved envelope is booked in full; the owner's
        negative availability opens a blocking OVERCOMMITTED case attributed to it, and the ledger stays verified."""
        h = make_harness(seed="101.20", target="106.26")
        self.addCleanup(h.close)
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)  # requires exactly 101.200000
        self.assertFixed(h.available(OWNER), "0.000000")
        h.pump()
        h.broker.fill("B1", F("10"), F("10.00"), commission=F("2.50"))
        h.pump()
        self.assertFixed(h.balance("COMMISSION", OWNER), "2.500000")
        self.assertFixed(h.balance("PAYABLE", OWNER), "-102.500000")
        # ADR-0006: the shortfall is attributed to exception:<case>, so the session stays non-negative.
        self.assertFixed(h.available(OWNER), "0.000000")
        (case,) = exceptions_of(h, "OVERCOMMITTED")
        self.assertTrue(case["blocking"])
        self.assertEqual(case["owner_hint"], OWNER)
        self.assertFixed(h.available(f"exception:{case['case_id']}"), "-1.300000")
        self.assertEqual(F(case["detail"]["shortfall"]), F("1.30"))
        r = submit(h, "B2", "BUY", "1", "1.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "OPEN_EXCEPTIONS")
        # Acknowledging is not enough: the owner funds the shortfall from cash they choose.
        ack = h.owner_cmd("resolve_exception", case_id=case["case_id"], note="seen")
        self.assertEqual(ack.rejected_code, "SHORTFALL_UNCOVERED")
        self.assertEqual(h.verify(), [])


# ============================================ submission outcomes (ORD-03)
class SubmissionOutcomeTests(_Base):
    def test_timeout_after_accept_is_unknown_blocks_entries_and_reconcile_resolves(self):
        """ORD-03, ADR-0009: a timeout is UNKNOWN with holds kept, a blocking UNKNOWN_ORDER, no retry; reconciliation resolves."""
        h = self.h
        h.broker.script_submit(SubmitBehavior.TIMEOUT_AFTER_ACCEPT)
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        stats = h.runner.pump_outbox()  # do not drain the broker's queued acknowledgment yet
        self.assertEqual(stats.sent, 1)
        o = order(h, "B1")
        self.assertEqual(o["state"], "UNKNOWN")
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))
        self.assertEqual([b["state"] for b in outbox(h, "B1")], ["UNKNOWN"])
        (case,) = exceptions_of(h, "UNKNOWN_ORDER")
        self.assertTrue(case["blocking"])
        self.assertEqual(case["order_id"], "B1")
        # No automatic retry while the outcome is unknown.
        self.assertEqual(h.runner.pump_outbox().sent, 0)
        # New exposure is blocked account-wide.
        r = submit(h, "B2", "BUY", "1", "10.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "OPEN_EXCEPTIONS")
        self.assertIn("UNKNOWN_ORDER", json.dumps(r.body["details"]["decision"]))
        self.assertClean()
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        o = order(h, "B1")
        self.assertEqual(o["state"], "ACKNOWLEDGED")
        self.assertEqual(o["broker_order_id"], h.broker.lookup("B1").broker_order_id)
        self.assertEqual(exceptions_of(h, "UNKNOWN_ORDER"), [])
        self.assertEqual(len(exceptions_of(h, "UNKNOWN_ORDER", status="RESOLVED")), 1)
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))  # still working at the broker
        r = submit(h, "B3", "BUY", "1", "10.00")
        self.assertTrue(r.accepted, r.body)
        self.assertClean()

    def test_timeout_before_accept_reconciles_to_rejected_and_releases(self):
        """ORD-03, ADR-0009: UNKNOWN + broker conclusively not found -> REJECTED and holds released."""
        h = self.h
        h.broker.script_submit(SubmitBehavior.TIMEOUT_BEFORE_ACCEPT)
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "UNKNOWN")
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))
        self.assertFixed(h.available(OWNER), "48.800000")
        self.assertEqual(len(exceptions_of(h, "UNKNOWN_ORDER")), 1)
        self.assertFalse(h.broker.lookup("B1").found)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        o = order(h, "B1")
        self.assertEqual(o["state"], "REJECTED")
        self.assertFixed(o["leaves_qty"], "0")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertFixed(h.available(OWNER), "150.000000")
        self.assertEqual(exceptions_of(h, "UNKNOWN_ORDER"), [])
        self.assertEqual([b["state"] for b in outbox(h, "B1")], ["DONE"])
        self.assertEqual(outbox(h, "B1")[0]["result"]["status"], "NOT_RECEIVED")
        self.assertTrue(submit(h, "B2", "BUY", "1", "10.00").accepted)
        self.assertClean()

    def test_transport_error_on_submit_is_unknown(self):
        """ORD-03, ADR-0012: an exception from the transport is UNKNOWN (never assumed rejected); holds stay."""
        h = self.h
        h.broker.script_submit(SubmitBehavior.RAISE)
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "UNKNOWN")
        self.assertIn("ConnectionError", o["reason"])
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))
        self.assertEqual(len(exceptions_of(h, "UNKNOWN_ORDER")), 1)
        self.assertClean()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(order(h, "B1")["state"], "REJECTED")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertClean()

    def test_unknown_stays_unknown_and_reserved_while_broker_unreachable(self):
        """ORD-03: without a conclusive lookup the order stays UNKNOWN, reserved and blocking; it never resends."""
        h = self.h
        h.broker.script_submit(SubmitBehavior.TIMEOUT_BEFORE_ACCEPT)
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        h.pump()
        h.broker.disconnect()
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "INCOMPLETE")
        o = order(h, "B1")
        self.assertEqual(o["state"], "UNKNOWN")
        self.assertEqual(hold(h, "B1"), (F("101.200000"), "ACTIVE"))
        self.assertEqual(len(exceptions_of(h, "UNKNOWN_ORDER")), 1)
        self.assertEqual(h.pump().sent, 0)
        h.broker.reconnect()
        h.connect()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(order(h, "B1")["state"], "REJECTED")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertEqual(exceptions_of(h, "UNKNOWN_ORDER"), [])
        self.assertClean()

    def test_unambiguous_reject_releases_holds(self):
        """ORD-03, ADR-0009: an unambiguous broker rejection is REJECTED with holds released and no exception."""
        h = self.h
        h.broker.script_submit(SubmitBehavior.REJECT)
        self.assertTrue(submit(h, "B1", "BUY", "10", "10.00").accepted)
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "REJECTED")
        self.assertEqual(o["reason"], "scripted rejection")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertFixed(h.available(OWNER), "150.000000")
        self.assertEqual(h.open_exceptions(), [])
        self.assertEqual([b["state"] for b in outbox(h, "B1")], ["DONE"])
        self.assertClean()


# ================================================= anomalous executions
class AnomalyTests(_Base):
    def assertBlocksNewExposure(self):
        r = submit(self.h, "BX", "BUY", "1", "10.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "OPEN_EXCEPTIONS")

    def test_overfill_is_recorded_with_blocking_anomaly(self):
        """ORD-05, ADR-0009: a fill beyond the remaining quantity is booked in full and opens a blocking EXECUTION_ANOMALY."""
        h = self.h
        self.acknowledged_buy()
        h.broker.fill("B1", F("12"), F("10.00"), allow_overfill=True)
        h.pump()
        (ex,) = executions(h, "B1")
        self.assertFixed(ex["quantity"], "12")
        o = order(h, "B1")
        self.assertEqual(o["state"], "FILLED")
        self.assertFixed(o["cum_qty"], "12")
        self.assertFixed(o["leaves_qty"], "0")
        self.assertEqual(position(h), Fixed(12))
        self.assertFixed(self.bal("INVENTORY", instrument_id=INST), "120.000000")
        self.assertFixed(self.bal("PAYABLE"), "-121.000000")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        (case,) = exceptions_of(h, "EXECUTION_ANOMALY")
        self.assertTrue(case["blocking"])
        self.assertIn("fill exceeds remaining quantity", [a["issue"] for a in case["detail"]["anomalies"]])
        self.assertBlocksNewExposure()
        self.assertClean()

    def test_price_outside_limit_is_recorded_with_blocking_anomaly(self):
        """ORD-05, ADR-0009: a buy fill above the limit is booked at the reported price and blocks new exposure."""
        h = self.h
        self.acknowledged_buy()
        h.broker.fill("B1", F("4"), F("10.05"), allow_price_violation=True)
        h.pump()
        (ex,) = executions(h, "B1")
        self.assertFixed(ex["price"], "10.05")
        self.assertFixed(self.bal("INVENTORY", instrument_id=INST), "40.200000")
        o = order(h, "B1")
        self.assertEqual(o["state"], "PARTIALLY_FILLED")
        self.assertFixed(o["cum_qty"], "4")
        (case,) = exceptions_of(h, "EXECUTION_ANOMALY")
        self.assertTrue(case["blocking"])
        self.assertEqual([a["issue"] for a in case["detail"]["anomalies"]], ["price outside limit"])
        self.assertEqual(hold(h, "B1"), (F("60.798000"), "ACTIVE"))  # 60 + (1.20 - 0.402)
        self.assertBlocksNewExposure()
        self.assertClean()

    def test_late_fill_after_canceled_is_recorded_not_discarded(self):
        """ORD-05, ADR-0009: a fill after a confirmed cancel is booked, keeps the hold released and opens an anomaly."""
        h = self.h
        self.acknowledged_buy()
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertFixed(h.available(OWNER), "150.000000")
        h.broker.fill("B1", F("3"), F("10.00"), allow_terminal=True)
        h.pump()
        o = order(h, "B1")
        self.assertEqual(o["state"], "CANCELED")
        self.assertFixed(o["cum_qty"], "3")
        self.assertFixed(o["leaves_qty"], "0")
        (ex,) = executions(h, "B1")
        self.assertFixed(ex["quantity"], "3")
        self.assertEqual(position(h), Fixed(3))
        self.assertFixed(self.bal("PAYABLE"), "-31.000000")
        self.assertEqual(hold(h, "B1"), (F("0"), "RELEASED"))
        self.assertFixed(h.available(OWNER), "119.000000")
        (case,) = exceptions_of(h, "EXECUTION_ANOMALY")
        self.assertTrue(case["blocking"])
        self.assertIn("fill after terminal state", [a["issue"] for a in case["detail"]["anomalies"]])
        self.assertBlocksNewExposure()
        self.assertClean()


# ===================================================== pretrade limits
class PretradeLimitTests(_Base):
    def test_price_collar_bounds_buy_and_sell_limits(self):
        """RSK-02, ORD-08: limits beyond mid +/- 100 bps are refused with PRICE_COLLAR; the boundary is accepted."""
        h = self.h  # quote 9.99 / 10.01 -> mid 10.000, cap 10.10, floor 9.90
        r = submit(h, "B1", "BUY", "1", "10.11")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "PRICE_COLLAR")
        self.assertEqual(r.body["details"]["decision"]["code"], "PRICE_COLLAR")
        self.assertIsNotNone(r.body["details"]["quote"])
        self.assertTrue(submit(h, "B2", "BUY", "1", "10.10").accepted)
        r = submit(h, "S1", "SELL", "1", "9.89")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "PRICE_COLLAR")
        self.assertIsNone(order(h, "B1"))
        self.assertClean()

    def test_stale_missing_or_delayed_quote_rejects(self):
        """RSK-02: a quote older than 5000 ms, a missing quote or a delayed quote cannot validate an order."""
        h = self.h
        h.clock.advance(5000 * 1_000_000)
        self.assertTrue(submit(h, "B1", "BUY", "1", "10.00").accepted)  # exactly at max age
        h.clock.advance(1_000_000)
        r = submit(h, "B2", "BUY", "1", "10.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "QUOTE_FRESH")
        now = h.clock.now_ns()
        h.desk.quotes.put(Quote(INST, F("9.99"), F("10.01"), None, "USD", "fixture:delayed", now, now,
                                delayed=True, fixture=True))
        r = submit(h, "B3", "BUY", "1", "10.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "QUOTE_NOT_DELAYED")
        inst = dataclasses.replace(FIXTURE_INSTRUMENT, instrument_id="FIX-EQ-3", symbol="FIXC",
                                   name="Fixture Equity C (no quote, test only)")
        self.assertTrue(h.owner_cmd("register_instrument", instrument=inst.to_json(),
                                    mappings=[{"provider": "simulated", "external_id": "FIX-EQ-3"}]).accepted)
        r = submit(h, "B4", "BUY", "1", "10.00", instrument_id="FIX-EQ-3")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "QUOTE_FRESH")
        self.assertClean()

    def test_max_order_notional(self):
        """RSK-02: a BUY whose notional exceeds max_order_notional (100.00) is refused; exactly 100.00 is accepted."""
        h = self.h
        r = submit(h, "B1", "BUY", "11", "10.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "MAX_ORDER_NOTIONAL")
        self.assertFixed(h.available(OWNER), "150.000000")
        r = submit(h, "B2", "BUY", "10", "10.00")
        self.assertTrue(r.accepted, r.body)
        self.assertClean()


class OrderRateTests(_Base):
    policy_overrides = {"account_limits__max_orders_per_minute": 3}

    def test_order_rate_limit_per_minute(self):
        """RSK-02: at most max_orders_per_minute accepted orders in a trailing 60 s window; rejections do not count."""
        h = self.h
        for i in range(3):
            self.assertTrue(submit(h, f"B{i}", "BUY", "1", "10.00").accepted)
        r = submit(h, "B3", "BUY", "1", "10.00")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "ORDER_RATE")
        r = submit(h, "B4", "BUY", "1", "10.00")
        self.assertEqual(r.rejected_code, "ORDER_RATE")
        h.clock.advance(60 * SEC + 1)
        h.quote("9.99", "10.01")
        self.assertTrue(submit(h, "B5", "BUY", "1", "10.00").accepted)
        # 4 holds of 1 * 10.00 + 1.20
        self.assertFixed(h.available(OWNER), "105.200000")
        self.assertClean()


if __name__ == "__main__":
    unittest.main()
