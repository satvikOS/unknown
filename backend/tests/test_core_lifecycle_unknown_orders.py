"""BR-ORD-03: an audited owner resolution that closes a stuck UNKNOWN order on attached broker evidence and never
releases a hold the broker may still be working.

``resolve_exception`` with action ``close_unknown_order`` (on the order's UNKNOWN_ORDER case, or on the
reconciliation's ORDER_UNRESOLVED / ORDER_MISSING_AT_BROKER difference for it) requires: fresh owner authentication;
evidence ``{kind: BROKER_STATEMENT | BROKER_CONFIRM | SUPPORT_REFERENCE, reference}``; and the account's latest
reconciliation to be complete, in this process, younger than ``max_reconciliation_age_s``, taken after the order's
last change, without the order in the broker's open orders, with a lookup that did not find it, and with no blocking
difference other than unresolved UNKNOWN orders themselves (an UNKNOWN order the broker cannot find always leaves
a run DIFFERENCES, so a literally clean run never contains one). Anything else is refused and nothing moves.

Every number is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, T0, Harness  # noqa: E402

from unknown.broker.port import BrokerOrderStatus, LookupResult, OpenOrderRow  # noqa: E402
from unknown.broker.simulated import SimulatedBroker, SubmitBehavior  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.clock import ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402

F = Fixed.parse
SEC = 1_000_000_000
EVIDENCE = {"kind": "SUPPORT_REFERENCE", "reference": "IBKR support ticket 0000-FIXTURE"}


class NoTraceBroker(SimulatedBroker):
    """The simulated broker, plus what the IBKR adapter does for a transmitted order it can no longer trace: its
    lookup answers "cannot determine" (found=None), not "not found", while every other read stays complete.
    ``held_open`` orders are listed among the open orders with status INACTIVE, and ``inactive_lookup`` orders are
    found by lookup with status INACTIVE. TEST FIXTURE only."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.untraceable: set[str] = set()
        self.held_open: set[str] = set()
        self.inactive_lookup: set[str] = set()

    def lookup(self, client_order_id: str) -> LookupResult:
        if client_order_id in self.inactive_lookup:
            return LookupResult(found=True, status=BrokerOrderStatus.INACTIVE, broker_order_id=f"HELD-{client_order_id}",
                                filled_quantity=Fixed(0), remaining_quantity=Fixed(3))
        if client_order_id in self.untraceable:
            return LookupResult(found=None, reason="transmitted, but in no open, completed or execution report")
        return super().lookup(client_order_id)

    def open_orders(self):
        rows, complete = super().open_orders()
        for cid in sorted(self.held_open):
            rows.append(OpenOrderRow(cid, None, f"HELD-{cid}", "FIX-EQ-1", "BUY", BrokerOrderStatus.INACTIVE,
                                     Fixed(0), Fixed(3)))
        return rows, complete


def cases(h: Harness, kind: str, status: str = "OPEN") -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM exceptions WHERE kind=? AND status=? ORDER BY rowid", (kind, status))]


def events(h: Harness, event_type: str) -> list[dict]:
    return [json.loads(r["payload"]) for r in h.desk.store.query(
        "SELECT payload FROM events WHERE type=? ORDER BY seq", (event_type,))]


def hold(h: Harness, oid: str) -> dict:
    (r,) = h.desk.store.query("SELECT * FROM holds WHERE order_id=?", (oid,))
    return dict(r)


class _Base(unittest.TestCase):
    def harness(self) -> Harness:
        clock = ManualClock(T0)
        broker = NoTraceBroker(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        h = Harness(broker=broker, clock=clock)
        self.addCleanup(h.close)
        return h

    def sound(self, h: Harness) -> None:
        self.assertEqual(h.verify(), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])

    def stuck(self, h: Harness, behavior: SubmitBehavior = SubmitBehavior.TIMEOUT_BEFORE_ACCEPT) -> dict:
        """B1 (buy 3 @ 9.00) transmitted with no answer; the broker cannot trace it; a reconciliation leaves it
        UNKNOWN (ORDER_UNRESOLVED), with its hold, and latches the stop. Returns the UNKNOWN_ORDER case."""
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        h.broker.script_submit(behavior)
        self.assertTrue(h.intent("B1", "BUY", "3", "9.00", "A").accepted)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "UNKNOWN")
        h.broker.untraceable.add("B1")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        self.assertEqual([d["kind"] for d in rec.body["differences"] if d["blocking"]], ["ORDER_UNRESOLVED"])
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "UNKNOWN")
        self.assertEqual(hold(h, "B1")["status"], "ACTIVE")
        (case,) = cases(h, "UNKNOWN_ORDER")
        return case

    def close(self, h: Harness, case: dict, evidence: dict | None = EVIDENCE, note: str = "closed on broker evidence"):
        payload = {"case_id": case["case_id"], "action": "close_unknown_order", "note": note}
        if evidence is not None:
            payload["evidence"] = evidence
        return h.owner_cmd("resolve_exception", **payload)


class CloseUnknownOrderTests(_Base):
    def test_owner_closes_a_stuck_unknown_order_on_evidence_and_the_account_can_restart(self):
        """BR-ORD-03: the stuck B1 is closed by the owner with a support reference. B1 becomes REJECTED (no fills),
        its hold is released, its UNKNOWN_ORDER case and the reconciliation's ORDER_UNRESOLVED case are resolved
        with the evidence, its outbox row is done, and the account needs a fresh reconciliation. The record names
        the evidence, the reconciliation run and the lookup it relied on. The next reconciliation is CLEAN and a
        green start succeeds."""
        h = self.harness()
        case = self.stuck(h)
        run = json.loads(h.desk.store.query("SELECT row FROM reconciliations ORDER BY rowid DESC LIMIT 1")[0]["row"])
        r = self.close(h, case)
        self.assertTrue(r.accepted, r.body)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["state"], o["leaves_qty"]), ("REJECTED", "0"))
        self.assertIn(EVIDENCE["reference"], o["reason"])
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.assertEqual(cases(h, "UNKNOWN_ORDER"), [])
        self.assertEqual([c for c in cases(h, "RECONCILIATION_DIFFERENCE") if c["detail"].get("order_id") == "B1"], [])
        (resolved,) = cases(h, "UNKNOWN_ORDER", status="RESOLVED")
        self.assertEqual(resolved["resolution"]["action"], "close_unknown_order")
        self.assertEqual(resolved["resolution"]["evidence"], EVIDENCE)
        (ev,) = events(h, "UnknownOrderClosedByOwner")
        self.assertEqual((ev["info"]["order_id"], ev["info"]["state"], ev["info"]["reconciliation"]), ("B1", "REJECTED", run["run_id"]))
        self.assertEqual(ev["info"]["evidence"], EVIDENCE)
        self.assertIsNone(ev["info"]["lookup"]["found"])
        outbox = [json.loads(x["row"]) for x in h.desk.store.query("SELECT row FROM outbox WHERE order_id='B1'")]
        self.assertTrue(all(b["state"] in ("DONE", "SKIPPED") for b in outbox), outbox)
        self.assertTrue(h.control()["needs_reconciliation"])
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        h.quote("8.99", "9.01")
        self.assertTrue(h.owner_cmd("start", account_key=ACCOUNT).accepted)
        self.sound(h)

    def _assert_nothing_moved(self, h: Harness, r, code: str, *problems: str) -> None:
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, code, r.body)
        for p in problems:
            self.assertTrue(any(x.startswith(p) for x in r.body["details"].get("problems", [p])), r.body)
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "UNKNOWN")
        self.assertEqual(hold(h, "B1")["status"], "ACTIVE")
        self.assertEqual(len(cases(h, "UNKNOWN_ORDER")), 1)

    def test_evidence_note_and_fresh_authentication_are_required(self):
        """BR-ORD-03: no evidence, an unknown evidence kind, an empty reference, a missing note, a strategy and a
        stale owner authentication are all refused; nothing is released."""
        h = self.harness()
        case = self.stuck(h)
        self._assert_nothing_moved(h, self.close(h, case, evidence=None), "EVIDENCE_REQUIRED")
        self._assert_nothing_moved(h, self.close(h, case, evidence={"kind": "HUNCH", "reference": "x"}), "EVIDENCE_REQUIRED")
        self._assert_nothing_moved(h, self.close(h, case, evidence={"kind": "BROKER_STATEMENT", "reference": " "}),
                                   "EVIDENCE_REQUIRED")
        self._assert_nothing_moved(h, self.close(h, case, note=""), "NOTE_REQUIRED")
        stale = Principal("owner", Role.OWNER, authenticated_at_ns=h.clock.now_ns() - 3600 * SEC)
        r = h.cmd("resolve_exception", stale, case_id=case["case_id"], action="close_unknown_order", note="n",
                  evidence=EVIDENCE)
        self._assert_nothing_moved(h, r, "OWNER_REAUTHENTICATION_REQUIRED")
        self.sound(h)

    def test_refused_while_the_broker_may_still_work_the_order(self):
        """BR-ORD-03 'never releases a hold the broker still shows working': refused when the broker reported on the
        order after the latest reconciliation, when that reconciliation lists the order among the broker's open orders (held,
        INACTIVE), when its lookup finds the order, when it has another blocking difference, is incomplete or is
        stale. The order stays UNKNOWN with its hold every time; a fresh, otherwise clean run then lets the same
        evidence close it."""
        h = self.harness()
        case = self.stuck(h)
        # Cancel bookkeeping is not a change the broker could report: a cancel request and its refusal by the
        # broker (which has no such order) leave the run current.
        self.assertTrue(h.cmd("request_cancel", order_id="B1").accepted)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["cancel_state"], "REJECTED")
        # The broker reports on the order after the run (held, with a broker id): the run no longer describes it.
        h.desk.run_system("broker_order_status", {"account_key": ACCOUNT, "client_order_id": "B1", "order_ref": None,
                                                  "broker_order_id": "LATE-1", "status": "INACTIVE", "filled_quantity": "0",
                                                  "remaining_quantity": "3", "reason": "late report"})
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "RECONCILIATION_PREDATES_THE_ORDER")
        h.pump()
        # The broker lists B1 among its open orders (held, Inactive).
        h.broker.held_open.add("B1")
        h.reconcile()
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "BROKER_LISTS_THE_ORDER_AS_OPEN")
        h.broker.held_open.clear()
        h.pump()
        # The lookup finds it (Inactive at the broker).
        h.broker.inactive_lookup.add("B1")
        h.reconcile()
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "BROKER_LOOKUP_FINDS_THE_ORDER:INACTIVE")
        h.broker.inactive_lookup.clear()
        h.pump()
        # Another blocking difference (an unexplained cash movement) in the run.
        h.broker.deposit(F("1.00"))
        h.reconcile()
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "RECONCILIATION_DIFFERENCES:CASH_MISMATCH")
        h.broker.withdraw(F("1.00"))
        # An incomplete run proves nothing.
        h.broker.set_snapshot_incomplete(True)
        h.reconcile()
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "RECONCILIATION_INCOMPLETE")
        h.broker.set_snapshot_incomplete(False)
        # A run older than max_reconciliation_age_s (300 s).
        h.reconcile()
        h.clock.advance(301 * SEC)
        h.quote("8.99", "9.01")
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "RECONCILIATION_STALE")
        # After a fresh, otherwise clean run the same evidence closes it.
        rec = h.reconcile()
        self.assertEqual([d["kind"] for d in rec.body["differences"] if d["blocking"]], ["ORDER_UNRESOLVED"])
        r = self.close(h, case)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "REJECTED")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.sound(h)

    def test_only_an_unknown_order_and_an_order_case_can_be_closed_this_way(self):
        """BR-ORD-03: an order the broker answered for (acknowledged after a timeout) is not UNKNOWN any more and its
        case is resolved, so nothing is closed; a case that names no order (a cash mismatch) is refused."""
        h = self.harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        h.broker.script_submit(SubmitBehavior.TIMEOUT_AFTER_ACCEPT)
        self.assertTrue(h.intent("B1", "BUY", "3", "9.00", "A").accepted)
        h.pump()   # outcome UNKNOWN, then the broker's ACKNOWLEDGED status arrives and resolves the case
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "ACKNOWLEDGED")
        (resolved,) = cases(h, "UNKNOWN_ORDER", status="RESOLVED")
        r = self.close(h, resolved)
        self.assertTrue(r.body.get("noop"), "a resolved case is not reopened")
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "ACKNOWLEDGED")
        h.broker.deposit(F("1.00"))
        h.reconcile()
        (cash,) = cases(h, "CASH_MISMATCH")
        self.assertEqual(self.close(h, cash).rejected_code, "INVALID_ACTION")
        self.assertEqual(hold(h, "B1")["status"], "ACTIVE")
        self.sound(h)

if __name__ == "__main__":
    unittest.main()
