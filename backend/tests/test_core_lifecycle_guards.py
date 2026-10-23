"""Guards the Wave D records describe, each pinned on its own (records review after Wave D).

A mutation run against 8b98d00 deleted each check below with every test green, although the book review, the ADR
amendments and the runbooks state it:

* BR-ORD-03 (ADR-0009 amendment, RB-13): close_unknown_order allows only "blocking differences of unresolved
  UNKNOWN orders" in the reconciliation it relies on. A blocking ORDER_MISSING_AT_BROKER of an order that is still
  working could be excused like an UNKNOWN order's own difference. And "only an UNKNOWN order is closed on
  evidence" (ORDER_NOT_UNKNOWN) was never reached: the other conditions happened to refuse first.
* BR-SES-13 (ADR-0010 amendment, RB-12): while a stop is latched only in memory, "every open buy, from the ledger
  and from the broker's open orders carrying our client order id, gets a best-effort cancel". Either source could
  be dropped, because the tests always had the other one covering it, so a ledger that cannot be read and a broker
  whose open orders cannot be read were both untested; so was "manual and foreign orders are not touched". Only a
  buy intent was refused at the socket in the tests, not the other exposure-increasing kinds; and the SYSTEM stop
  that carries the evidence could lose its reason MEMORY_LATCH_PERSISTED.

Every number is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, T0, Harness  # noqa: E402
import test_core_lifecycle_stop as stop  # noqa: E402
import test_core_lifecycle_unknown_orders as unknown  # noqa: E402

from unknown.broker.port import LookupResult  # noqa: E402
from unknown.broker.simulated import SubmitBehavior  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.clock import ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.service.core import CoreService  # noqa: E402

F = Fixed.parse
EXPOSURE_KINDS = ("start", "start_session", "resume_session", "create_session", "amend_session_request",
                  "authorize_capital")


class LostOrderBroker(unknown.NoTraceBroker):
    """NoTraceBroker, plus ``lost`` orders: missing from the broker's open orders and conclusively not found by
    lookup, as if the broker had dropped an order the core still holds as working. TEST FIXTURE only."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lost: set[str] = set()

    def lookup(self, client_order_id: str) -> LookupResult:
        if client_order_id in self.lost:
            return LookupResult(found=False, reason="no order with this client id (fixture)")
        return super().lookup(client_order_id)

    def open_orders(self):
        rows, complete = super().open_orders()
        return [r for r in rows if r.client_order_id not in self.lost], complete


def blocking_kinds(rec) -> list[str]:
    return sorted(d["kind"] for d in rec.body["differences"] if d["blocking"])


# ------------------------------------------------------------------ BR-ORD-03
class CloseGuardTests(unknown._Base):
    def harness(self) -> Harness:
        clock = ManualClock(T0)
        broker = LostOrderBroker(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        h = Harness(broker=broker, clock=clock)
        self.addCleanup(h.close)
        return h

    def test_a_working_orders_difference_blocks_the_close_and_a_working_order_is_never_closed_on_evidence(self):
        """W1 is working (ACKNOWLEDGED) and B1 is a stuck UNKNOWN order the broker cannot trace. The broker then
        loses W1, so the reconciliation holds two blocking differences: B1's ORDER_UNRESOLVED, which the close may
        excuse, and W1's ORDER_MISSING_AT_BROKER, which it may not (W1 is not UNKNOWN). Closing B1 is refused
        RECONCILIATION_DIFFERENCES:ORDER_MISSING_AT_BROKER, and closing W1 through its own reconciliation case is
        refused ORDER_NOT_UNKNOWN; nothing moves either time. Control: once the broker lists W1 again, a fresh run
        holds only B1's difference and the same evidence closes B1. W1 keeps its state and its hold throughout (the
        failed run latched the stop, so W1 waits CANCEL_PENDING with its reservation)."""
        h = self.harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("W1", "BUY", "1", "9.00", "A").accepted)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "W1")["state"], "ACKNOWLEDGED")
        h.broker.script_submit(SubmitBehavior.TIMEOUT_BEFORE_ACCEPT)
        self.assertTrue(h.intent("B1", "BUY", "3", "9.00", "A").accepted)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "UNKNOWN")
        h.broker.untraceable.add("B1")
        h.broker.lost.add("W1")
        rec = h.reconcile()
        self.assertEqual(blocking_kinds(rec), ["ORDER_MISSING_AT_BROKER", "ORDER_UNRESOLVED"], rec.body)
        (case,) = unknown.cases(h, "UNKNOWN_ORDER")
        w1 = h.row("orders", "order_id", "W1")   # the failed run latched the stop, so W1's cancel is pending
        self.assertNotIn(w1["state"], ("UNKNOWN", "CANCELED", "REJECTED"))

        r = self.close(h, case)
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "CLOSE_NOT_PROVEN", r.body)
        self.assertEqual(r.body["details"]["problems"], ["RECONCILIATION_DIFFERENCES:ORDER_MISSING_AT_BROKER"], r.body)
        self.assertEqual((h.row("orders", "order_id", "B1")["state"], unknown.hold(h, "B1")["status"]), ("UNKNOWN", "ACTIVE"))

        (missing,) = [c for c in unknown.cases(h, "RECONCILIATION_DIFFERENCE") if c["detail"].get("order_id") == "W1"]
        r = self.close(h, missing)
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "ORDER_NOT_UNKNOWN", r.body)
        self.assertEqual(h.row("orders", "order_id", "W1"), w1)
        self.assertEqual(unknown.hold(h, "W1")["status"], "ACTIVE")
        self.assertEqual(unknown.events(h, "UnknownOrderClosedByOwner"), [])

        h.broker.lost.clear()
        rec = h.reconcile()
        self.assertEqual(blocking_kinds(rec), ["ORDER_UNRESOLVED"], rec.body)
        r = self.close(h, case)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual((h.row("orders", "order_id", "B1")["state"], unknown.hold(h, "B1")["status"]), ("REJECTED", "RELEASED"))
        self.assertEqual((h.row("orders", "order_id", "W1")["state"], unknown.hold(h, "W1")["status"]),
                         (w1["state"], "ACTIVE"), "W1 is not touched by B1's close")
        self.sound(h)


# ------------------------------------------------------------------ BR-SES-13
class MemoryLatchGuardTests(stop._Base):
    def service(self, h: Harness) -> CoreService:
        return stop.MemoryLatchTests.service(self, h)

    def stop_request(self, h: Harness, command_id: str = "ui.stop.1") -> dict:
        return stop.MemoryLatchTests.stop_request(self, h, command_id)

    def latch(self, h: Harness, svc: CoreService) -> dict:
        return stop.MemoryLatchTests.latch_in_memory(self, h, svc)

    def request(self, h: Harness, kind: str, payload: dict) -> dict:
        return {"op": "command", "command_id": f"ui.{kind}.1", "kind": kind, "payload": payload,
                "principal": {"id": "owner", "role": "OWNER", "authenticated_at_ns": h.clock.now_ns()}}

    def test_an_unreadable_ledger_still_cancels_every_buy_the_broker_lists_as_ours(self):
        """Every write raises and the ledger's orders cannot be read either. The broker's open orders are the only
        source left: B1, which carries our client order id, is canceled at the broker. A foreign buy working at the
        broker (no client order id of ours) is not touched."""
        h = self.harness()
        self.trading(h)
        foreign = h.broker.foreign_order("FIX-EQ-1", "BUY", F("1"), F("8.40"))
        svc = self.service(h)
        store, read = h.desk.store, h.desk.store.query

        def query(sql, params=()):
            if "FROM orders" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed (fixture)")
            return read(sql, params)

        store.query = query
        try:
            reply = self.latch(h, svc)
        finally:
            del store.query
        self.assertFalse(reply["persisted"])
        self.assertEqual([(c["client_order_id"], c["status"]) for c in reply["body"]["cancels"]], [("B1", "ACCEPTED")])
        self.assertEqual(h.broker.lookup("B1").status.value, "CANCELED")
        rows, _ = h.broker.open_orders()
        self.assertIn(foreign.broker_order_id, [r.broker_order_id for r in rows], "a foreign order is never canceled")
        self.assertTrue(svc._persist_memory_latch())
        self.assertTrue(h.control()["stop_latched"])
        self.sound(h)

    def test_unreadable_broker_open_orders_still_cancel_every_open_buy_in_the_ledger(self):
        """Every write raises and the broker's open orders cannot be read. The ledger is the only source left: its
        working buy B1 is canceled at the broker."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)

        def unreadable():
            raise ConnectionError("open orders unavailable (fixture)")

        h.broker.open_orders = unreadable
        try:
            reply = self.latch(h, svc)
        finally:
            del h.broker.open_orders
        self.assertEqual([(c["client_order_id"], c["status"]) for c in reply["body"]["cancels"]], [("B1", "ACCEPTED")])
        self.assertEqual(h.broker.lookup("B1").status.value, "CANCELED")
        self.assertTrue(svc._persist_memory_latch())
        self.sound(h)

    def test_every_exposure_increasing_command_waits_while_the_stop_is_latched_only_in_memory(self):
        """While the stop is latched only in memory (writes still fail), start, session creation, start and resume,
        request amendments, capital authorization and a buy intent are each refused at the socket, before the
        desk. A sell intent is not held back there: it reaches the desk, whose write fails."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        self.latch(h, svc)
        with stop.unwritable(h.desk.store), self.assertLogs("unknown.core", "CRITICAL"):
            svc._last_latch_alarm = float("-inf")
            for kind in EXPOSURE_KINDS:
                with self.subTest(kind=kind), self.assertRaisesRegex(PermissionError, "latched in memory"):
                    svc._handle(self.request(h, kind, {"account_key": ACCOUNT, "session_id": "A"}), stop.OWNER_ROLES)
            with self.assertRaisesRegex(PermissionError, "latched in memory"):
                svc._handle(stop.MemoryLatchTests.intent_request(self, h, "B9", "BUY", "8.50"), stop.OWNER_ROLES)
            with self.assertRaises(sqlite3.OperationalError):
                svc._handle(stop.MemoryLatchTests.intent_request(self, h, "S9", "SELL", "9.00"), stop.OWNER_ROLES)
        self.assertIsNotNone(svc.memory_latch)
        self.assertTrue(svc._persist_memory_latch())
        for kind in EXPOSURE_KINDS:
            self.assertIsNone(stop.stored(h, f"ui.{kind}.1"), kind)
        self.assertIsNone(h.row("orders", "order_id", "B9"))
        self.sound(h)

    def test_the_latch_is_recorded_under_the_owners_id_then_as_memory_latch_persisted(self):
        """Once writes work: the owner's stop is recorded first, under its own command id and reason, then the
        service's SYSTEM stop with reason MEMORY_LATCH_PERSISTED, whose StopReasserted event carries the evidence."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        self.latch(h, svc)
        self.assertTrue(svc._persist_memory_latch())
        ctl = h.control()
        self.assertEqual((ctl["latch_command_id"], ctl["latch_reason"]), ("ui.stop.1", "OWNER_STOP"))
        rows = h.desk.store.query("SELECT command_id, principal_id, payload FROM events WHERE type='StopReasserted' "
                                  "ORDER BY seq")
        (evidence,) = [r for r in rows if "memory_latch" in json.loads(r["payload"])["info"]]
        info = json.loads(evidence["payload"])["info"]
        self.assertEqual(info["reason"], "MEMORY_LATCH_PERSISTED")
        self.assertTrue(evidence["command_id"].startswith("sys.memory-latch."), evidence["command_id"])
        self.assertTrue(evidence["principal_id"].startswith("system."), evidence["principal_id"])
        self.assertEqual(info["memory_latch"]["stands_in_for"], ["ui.stop.1"])
        stop_seq = h.desk.store.query("SELECT MIN(seq) AS s FROM events WHERE command_id='ui.stop.1'")[0]["s"]
        evidence_seq = h.desk.store.query("SELECT MIN(seq) AS s FROM events WHERE command_id=?",
                                          (evidence["command_id"],))[0]["s"]
        self.assertLess(stop_seq, evidence_seq)
        self.sound(h)


if __name__ == "__main__":
    unittest.main()
