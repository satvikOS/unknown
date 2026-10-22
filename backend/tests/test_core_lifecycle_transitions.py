"""BR-ORD-07: one published order transition table, enforced at the single point every handler writes orders
through (``Context.emit``); an undeclared transition is refused and recorded as an anomaly, never written. Also the
decision on IBKR Inactive (held) orders: UNKNOWN with every reservation kept, never released as REJECTED.

Every number is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402

from unknown.broker.port import BrokerOrderStatus  # noqa: E402
from unknown.core import order_machine, state  # noqa: E402
from unknown.core.handlers import HANDLERS  # noqa: E402
from unknown.core.handlers.common import hold_rows, new_exception  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

F = Fixed.parse
INST = "FIX-EQ-1"


def order(h: Harness, oid: str) -> dict:
    return h.row("orders", "order_id", oid)


def hold(h: Harness, oid: str) -> dict:
    (r,) = h.desk.store.query("SELECT * FROM holds WHERE order_id=?", (oid,))
    return dict(r)


def cases(h: Harness, kind: str, status: str = "OPEN") -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM exceptions WHERE kind=? AND status=? ORDER BY rowid", (kind, status))]


def events(h: Harness, event_type: str) -> list[dict]:
    return [json.loads(r["payload"]) for r in h.desk.store.query(
        "SELECT payload FROM events WHERE type=? ORDER BY seq", (event_type,))]


def recorded_transitions(h: Harness) -> list[tuple[str, str | None, str, str]]:
    """(order id, from, to, event type) for every state change the event log records, in order."""
    current: dict[str, str | None] = {}
    out = []
    for r in h.desk.store.query("SELECT type, payload FROM events ORDER BY seq"):
        for row in json.loads(r["payload"])["changes"].get("orders", []):
            old = current.get(row["order_id"])
            if old != row["state"]:
                out.append((row["order_id"], old, row["state"], r["type"]))
            current[row["order_id"]] = row["state"]
    return out


def status(h: Harness, oid: str, st: str, filled: str = "0"):
    return h.desk.run_system("broker_order_status", {"account_key": ACCOUNT, "client_order_id": oid, "order_ref": None,
                                                     "broker_order_id": None, "status": st, "filled_quantity": filled,
                                                     "remaining_quantity": "0", "reason": "fixture"})


class _Base(unittest.TestCase):
    def harness(self) -> Harness:
        h = Harness()
        self.addCleanup(h.close)
        return h

    def running(self, h: Harness) -> None:
        h.bootstrap()
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A")
        assert r.accepted and r.body["state"] == "RUNNING", r.body
        h.quote("8.99", "9.01")

    def sound(self, h: Harness) -> None:
        self.assertEqual(h.verify(), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])
        for oid, old, new, event_type in recorded_transitions(h):
            self.assertTrue(order_machine.allowed(old, new), f"{oid}: {old} -> {new} in {event_type} is undeclared")


class TableTests(_Base):
    def test_table_never_leaves_a_terminal_state_and_names_only_real_states(self):
        """BR-ORD-07: the published table is the whole machine. Every source is an open state (or no row yet),
        every target a real state, no transition leaves FILLED/CANCELED/REJECTED/EXPIRED, and each row says why."""
        self.assertEqual(set(order_machine.STATES), set(state.OPEN_ORDER_STATES) | set(state.TERMINAL_ORDER_STATES))
        for (old, new), why in order_machine.TRANSITIONS.items():
            with self.subTest(old=old, new=new):
                self.assertIn(old, (None,) + order_machine.OPEN_STATES)
                self.assertIn(new, order_machine.STATES)
                self.assertTrue(why)
        for terminal in order_machine.TERMINAL_STATES:
            for target in order_machine.STATES:
                if target != terminal:
                    self.assertFalse(order_machine.allowed(terminal, target), f"{terminal} -> {target}")
        for old, new in (("PARTIALLY_FILLED", "ACKNOWLEDGED"), ("ACKNOWLEDGED", "SUBMIT_PENDING"),
                         ("ACKNOWLEDGED", "RESERVED"), (None, "ACKNOWLEDGED"), (None, "FILLED"), ("UNKNOWN", "RESERVED")):
            self.assertFalse(order_machine.allowed(old, new), f"{old} -> {new} must stay undeclared")
        self.assertTrue(order_machine.allowed("FILLED", "FILLED"), "field updates in a terminal state are allowed")
        rows = order_machine.table()
        self.assertEqual(len(rows), len(order_machine.TRANSITIONS))
        self.assertEqual(rows[0]["from"], None)

    def test_a_full_lifecycle_records_only_declared_transitions(self):
        """BR-ORD-07: buy acknowledged, partly filled, cancel requested and rejected too late, filled; a sell
        canceled before transmission; another buy canceled at the broker. The event log's transitions all appear
        in the table, the ledger holds and a replay reproduces every projection."""
        h = self.harness()
        self.running(h)
        self.assertTrue(h.intent("B1", "BUY", "4", "9.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("1"), F("9.00"))
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "PARTIALLY_FILLED")
        from unknown.broker.simulated import CancelBehavior
        h.broker.script_cancel(CancelBehavior.REJECT_TOO_LATE)
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        self.assertEqual(order(h, "B1")["state"], "CANCEL_PENDING")
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "PARTIALLY_FILLED")
        h.broker.fill("B1", F("3"), F("9.00"))
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "FILLED")
        self.assertTrue(h.intent("S1", "SELL", "2", "9.00", "A").accepted)
        self.assertTrue(h.cmd("request_cancel", order_id="S1").accepted)
        self.assertEqual(order(h, "S1")["state"], "CANCELED")
        self.assertTrue(h.intent("B2", "BUY", "2", "8.50", "A").accepted)
        h.pump()
        self.assertTrue(h.cmd("request_cancel", order_id="B2").accepted)
        h.pump()
        self.assertEqual(order(h, "B2")["state"], "CANCELED")
        seen = {(old, new) for _, old, new, _ in recorded_transitions(h)}
        self.assertLessEqual(seen, set(order_machine.TRANSITIONS))
        self.assertIn(("CANCEL_PENDING", "PARTIALLY_FILLED"), seen)
        self.sound(h)


class EnforcementTests(_Base):
    def _filled(self, h: Harness) -> None:
        self.running(h)
        self.assertTrue(h.intent("B1", "BUY", "2", "9.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("9.00"))
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "FILLED")

    def _patch(self, h: Harness, kind: str, handler) -> None:
        h.desk._handlers = {**HANDLERS, kind: handler}
        self.addCleanup(setattr, h.desk, "_handlers", HANDLERS)

    def test_undeclared_transition_is_refused_recorded_as_an_anomaly_and_latches(self):
        """BR-ORD-07: a (deliberately defective) status handler first records a legitimate case, then tries to
        reopen a FILLED order as ACKNOWLEDGED and re-reserve it. Nothing of the command is written: the order,
        its released hold and the case it opened are as before. The refusal is recorded instead: an
        OrderTransitionRefused event carrying the received payload, a blocking critical ORDER_TRANSITION_REFUSED
        case naming the order, the stop latch, and the command stored REJECTED (UNDECLARED_ORDER_TRANSITION)."""
        h = self.harness()
        self._filled(h)
        before = order(h, "B1")
        hold_before = hold(h, "B1")

        def defective(ctx, p):
            o = state.order(ctx.tx, p["client_order_id"])
            case = new_exception(ctx, ACCOUNT, "EXECUTION_ANOMALY", blocking=True, severity="low", detail={},
                                 key="defective-first-effect")
            ctx.emit("LegitimateFirstEffect", {}, {"exceptions": [case]}, account_key=ACCOUNT)
            reopened = {**o, "state": "ACKNOWLEDGED", "prev_state": o["state"], "leaves_qty": "2"}
            ctx.emit("DefectiveReopen", {}, {"orders": [reopened], "holds": hold_rows(reopened, Fixed(1), release=False)},
                     account_key=ACCOUNT, order_id=o["order_id"])
            return {}

        self._patch(h, "broker_order_status", defective)
        r = status(h, "B1", "ACKNOWLEDGED")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "UNDECLARED_ORDER_TRANSITION")
        self.assertEqual(r.body["details"]["transitions"],
                         [{"order_id": "B1", "account_key": ACCOUNT, "session_id": "A", "from": "FILLED", "to": "ACKNOWLEDGED"}])
        self.assertEqual(order(h, "B1"), before)
        self.assertEqual(hold(h, "B1"), hold_before)
        self.assertEqual(cases(h, "EXECUTION_ANOMALY"), [], "the command's earlier effect is rolled back too")
        self.assertEqual(events(h, "LegitimateFirstEffect"), [])
        self.assertEqual(events(h, "DefectiveReopen"), [])
        (ev,) = events(h, "OrderTransitionRefused")
        self.assertEqual(ev["info"]["event_type"], "DefectiveReopen")
        self.assertEqual(ev["info"]["received"]["status"], "ACKNOWLEDGED", "the broker fact is kept in the record")
        (case,) = cases(h, "ORDER_TRANSITION_REFUSED")
        self.assertEqual((case["blocking"], case["severity"], case["order_id"], case["session_id"]), (True, "critical", "B1", "A"))
        ctl = h.control()
        self.assertEqual((ctl["stop_latched"], ctl["latch_reason"]), (True, "ORDER_TRANSITION_REFUSED"))
        stored = h.desk.store.query("SELECT status, result FROM commands WHERE command_id=?", (r.command_id,))[0]
        self.assertEqual(stored["status"], "REJECTED")
        self.assertEqual(json.loads(stored["result"])["code"], "UNDECLARED_ORDER_TRANSITION")
        # The same refusal again (a redelivery) is one more record but not a second case.
        status(h, "B1", "ACKNOWLEDGED")
        self.assertEqual(len(cases(h, "ORDER_TRANSITION_REFUSED")), 1)
        self.assertEqual(len(events(h, "OrderTransitionRefused")), 2)
        # A new entry is refused while the case is open.
        h.quote("8.99", "9.01")
        self.assertEqual(h.intent("B9", "BUY", "1", "9.00", "A").rejected_code, "STOP_LATCH")
        self.sound(h)

    def test_an_owner_command_with_an_undeclared_transition_is_refused_the_same_way(self):
        """BR-ORD-07: the enforcement does not depend on the principal: a defective owner cancel that would turn a
        FILLED order into CANCEL_PENDING is refused, recorded and latched, and the order stays FILLED."""
        h = self.harness()
        self._filled(h)

        def defective_cancel(ctx, p):
            o = state.order(ctx.tx, p["order_id"])
            ctx.emit("DefectiveCancel", {}, {"orders": [{**o, "state": "CANCEL_PENDING", "prev_state": o["state"]}]},
                     account_key=ACCOUNT, order_id=o["order_id"])
            return {}

        self._patch(h, "request_cancel", defective_cancel)
        r = h.cmd("request_cancel", order_id="B1")
        self.assertEqual(r.rejected_code, "UNDECLARED_ORDER_TRANSITION")
        self.assertEqual(order(h, "B1")["state"], "FILLED")
        (ev,) = events(h, "OrderTransitionRefused")
        self.assertIsNone(ev["info"]["received"], "an owner command's payload is not copied into the record")
        self.assertEqual(len(cases(h, "ORDER_TRANSITION_REFUSED")), 1)
        self.assertTrue(h.control()["stop_latched"])
        self.sound(h)

    def test_submit_rejection_after_a_broker_cancel_keeps_the_terminal_state(self):
        """BR-ORD-07: the broker's CANCELED report ends B1 before the submit result (REJECTED) is recorded. A
        terminal state is never relabelled: B1 stays CANCELED with its hold released, the result is kept on the
        outbox row, and no transition is refused."""
        h = self.harness()
        self.running(h)
        self.assertTrue(h.intent("B1", "BUY", "2", "9.00", "A").accepted)
        self.assertTrue(h.desk.run_system("outbox_begin", {"outbox_id": "B1:submit"}).body["send"])
        self.assertEqual(order(h, "B1")["state"], "SUBMIT_PENDING")
        self.assertEqual(status(h, "B1", "CANCELED").body, {"state": "CANCELED"})
        r = h.desk.run_system("outbox_result", {"outbox_id": "B1:submit", "status": "REJECTED", "reason": "late reject"})
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        row = json.loads(h.desk.store.query("SELECT row FROM outbox WHERE outbox_id='B1:submit'")[0]["row"])
        self.assertEqual((row["state"], row["result"]["status"]), ("DONE", "REJECTED"))
        self.assertEqual(len(events(h, "SubmitResultAfterTerminal")), 1)
        self.assertEqual(cases(h, "ORDER_TRANSITION_REFUSED"), [])
        self.assertFalse(h.control()["stop_latched"])
        self.sound(h)


class InactiveOrderTests(_Base):
    """Decision (order_machine.py): IBKR Inactive is not working and not final, so the order is held as UNKNOWN."""

    def _acked_buy(self, h: Harness) -> dict:
        self.running(h)
        self.assertTrue(h.intent("B1", "BUY", "3", "9.00", "A").accepted)
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "ACKNOWLEDGED")
        return hold(h, "B1")

    def test_inactive_buy_keeps_its_reservation_blocks_entries_and_is_canceled(self):
        """An INACTIVE report for an acknowledged buy no longer releases it as REJECTED: B1 becomes UNKNOWN with
        the same ACTIVE cash hold, a blocking UNKNOWN_ORDER case (cause INACTIVE) and a queued cancel. Entries and
        green start are blocked. The broker's CANCELED confirmation ends it: hold released, case resolved."""
        h = self.harness()
        cash_hold = self._acked_buy(h)
        r = status(h, "B1", "INACTIVE")
        self.assertEqual(r.body, {"state": "UNKNOWN", "held": True})
        o = order(h, "B1")
        self.assertEqual((o["state"], o["prev_state"], o["broker_held"]["status"]), ("UNKNOWN", "ACKNOWLEDGED", "INACTIVE"))
        self.assertEqual(o["cancel_state"], "REQUESTED")
        self.assertEqual(hold(h, "B1"), cash_hold, "the reservation is kept, unchanged")
        (case,) = cases(h, "UNKNOWN_ORDER")
        self.assertEqual((case["blocking"], case["detail"]["cause"], case["order_id"]), (True, "INACTIVE", "B1"))
        h.quote("8.99", "9.01")
        self.assertEqual(h.intent("B2", "BUY", "1", "9.00", "A").rejected_code, "OPEN_EXCEPTIONS")
        self.assertEqual(status(h, "B1", "INACTIVE").body.get("noop"), True, "the same report again changes nothing")
        self.assertEqual(len(events(h, "OrderHeldAtBroker")), 1)
        h.pump()   # the queued cancel reaches the broker, which confirms it
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.assertEqual(cases(h, "UNKNOWN_ORDER"), [])
        self.sound(h)

    def test_inactive_sell_keeps_its_quantity_hold_and_working_again_clears_the_hold_flag(self):
        """A held sell keeps reserving its shares (no automatic cancel: it only reduces exposure). A later working
        report returns it to ACKNOWLEDGED, clears broker_held and resolves the case; nothing was released."""
        h = self.harness()
        self.running(h)
        self.assertTrue(h.intent("B1", "BUY", "3", "9.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("3"), F("9.00"))
        h.pump()
        self.assertTrue(h.intent("S1", "SELL", "3", "9.00", "A").accepted)
        h.pump()
        qty_hold = hold(h, "S1")
        self.assertEqual(status(h, "S1", "INACTIVE").body["state"], "UNKNOWN")
        self.assertEqual(hold(h, "S1"), qty_hold)
        self.assertEqual(order(h, "S1")["cancel_state"], "NONE", "a held sell is not canceled automatically")
        self.assertEqual(status(h, "S1", "ACKNOWLEDGED").body["state"], "ACKNOWLEDGED")
        o = order(h, "S1")
        self.assertIsNone(o["broker_held"])
        self.assertEqual(hold(h, "S1"), qty_hold)
        self.assertEqual(cases(h, "UNKNOWN_ORDER"), [])
        self.sound(h)

    def test_reconciliation_lookup_inactive_holds_until_the_broker_reports_a_terminal_state(self):
        """Through reconciliation: the broker no longer lists B1 as open and its lookup says INACTIVE. B1 is held
        UNKNOWN with its reservation (before: released as REJECTED). Only when the broker later reports it
        CANCELED does a reconciliation release it."""
        h = self.harness()
        cash_hold = self._acked_buy(h)
        h.broker._orders["B1"].status = BrokerOrderStatus.INACTIVE   # fixture: the broker holds the order inactive
        h.reconcile()
        o = order(h, "B1")
        self.assertEqual((o["state"], o["broker_held"]["via"]), ("UNKNOWN", "reconciliation"))
        self.assertEqual(hold(h, "B1"), cash_hold)
        self.assertEqual(len(cases(h, "UNKNOWN_ORDER")), 1)
        h.broker._orders["B1"].status = BrokerOrderStatus.CANCELED   # fixture: the broker ends it
        h.reconcile()
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.assertEqual(cases(h, "UNKNOWN_ORDER"), [])
        self.sound(h)


if __name__ == "__main__":
    unittest.main()
