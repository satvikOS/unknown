"""Adversarial review of the core-lifecycle fixes: one regression per defect found, each failing before its fix.

* BR-SES-13 memory latch: a stop from a role that may not stop (AGENT, STRATEGY) or for another account engaged the
  memory latch while the database failed, sent cancels to the broker and then wedged the service for good (its stop
  is recorded REJECTED, so the latch never released and even the owner's green start was refused). A stop the core
  itself refuses once writes work wedged it the same way. A latch file was executed whatever commands it held.
* BR-LED-03 / BR-SES-02: a bust, a correction or a fee adjustment of an owner DISPOSAL's fill after the session
  ended was exempt from the POST_COMPLETION_ADJUSTMENT case, so a busted disposal silently put shares back into
  protected:<id> with a CLEAN reconciliation and nothing open.
* BR-ORD-03: close_unknown_order accepted a reconciliation that predated a later broker report on the order (held,
  Inactive) because only state, filled quantity and broker id counted as a change; an identical report after the run
  left no trace at all.
* BR-ORD-07 (Inactive decision): an order the owner closed (or any locally terminal order) that the broker then
  lists as INACTIVE reconciled CLEAN with no case and no cancel, although an inactive buy "must not come back to
  life unattended".

Every number is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, DEPLOYMENT, Harness  # noqa: E402
import test_core_lifecycle_post_completion as post  # noqa: E402
import test_core_lifecycle_stop as stop  # noqa: E402
import test_core_lifecycle_unknown_orders as unknown  # noqa: E402

from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.service.core import CoreService  # noqa: E402
from unknown.service.ipc import Grant  # noqa: E402

F = Fixed.parse
ARTIFACT = "TREND_DAILY_V1@0123456789abcdef"


def open_cases(h: Harness, kind: str | None = None) -> list[dict]:
    rows = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM exceptions WHERE status='OPEN' ORDER BY rowid")]
    return [c for c in rows if kind is None or c["kind"] == kind]


def sound(test: unittest.TestCase, h: Harness) -> None:
    test.assertEqual(h.verify(), [])
    with tempfile.TemporaryDirectory() as d:
        test.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])


# ------------------------------------------------------------------ BR-SES-13
class MemoryLatchAuthorityTests(stop._Base):
    def service(self, h: Harness) -> CoreService:
        return stop.MemoryLatchTests.service(self, h)

    def request(self, h: Harness, command_id: str, role: str, principal_id: str, account_key: str = ACCOUNT) -> dict:
        return {"op": "command", "command_id": command_id, "kind": "stop", "payload": {"account_key": account_key},
                "principal": {"id": principal_id, "role": role, "authenticated_at_ns": h.clock.now_ns()}}

    def test_a_role_that_may_not_stop_never_latches_in_memory(self):
        """An AGENT token and an artifact-bound STRATEGY token send a stop while every write raises. Neither role may
        stop (ADR-0007), so the failure is reported like any other failed command: no memory latch, no latch file,
        no cancel sent to the broker (B1 still works there), and once writes work the owner can still trade."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        for command_id, role, pid, grant in (("agent.stop.1", "AGENT", "agent.research", frozenset({"AGENT"})),
                                             ("strat.stop.1", "STRATEGY", ARTIFACT, Grant({"STRATEGY"}, ARTIFACT))):
            with self.subTest(role=role):
                with stop.unwritable(h.desk.store), self.assertRaises(Exception):
                    svc._handle(self.request(h, command_id, role, pid), grant)
                self.assertIsNone(svc.memory_latch)
                self.assertFalse(svc.latch_path.exists())
                self.assertEqual(h.broker.lookup("B1").status.value, "ACKNOWLEDGED", "no cancel reached the broker")
        reply = svc._handle(stop.MemoryLatchTests.intent_request(self, h, "S9", "SELL", "9.00"), stop.OWNER_ROLES)
        self.assertTrue(reply["accepted"], reply)
        self.assertFalse(h.control()["stop_latched"])
        sound(self, h)

    def test_a_stop_for_another_account_never_latches_in_memory(self):
        """A stop naming an account this core does not run is not this core's stop: with writes failing it is a
        failed command, not a memory latch for this account."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        with stop.unwritable(h.desk.store), self.assertRaises(Exception):
            svc._handle(self.request(h, "ui.stop.other", "OWNER", "owner", account_key="OTHER-1"), stop.OWNER_ROLES)
        self.assertIsNone(svc.memory_latch)
        self.assertEqual(h.broker.lookup("B1").status.value, "ACKNOWLEDGED")
        sound(self, h)

    def test_a_stop_the_core_refuses_once_writes_work_does_not_wedge_the_latch(self):
        """The owner's stop is latched in memory while writes fail, but once they work the core refuses it (here
        the account is not registered yet, UNKNOWN_ACCOUNT). The refusal is the recorded outcome: the memory latch
        is released (it used to stay forever, refusing every green start), and each stop is stored REJECTED."""
        h = self.harness()   # nothing registered
        svc = self.service(h)
        with stop.unwritable(h.desk.store), self.assertLogs("unknown.core", "CRITICAL"):
            reply = svc._handle(self.request(h, "ui.stop.1", "OWNER", "owner"), stop.OWNER_ROLES)
        self.assertFalse(reply["persisted"])
        with self.assertLogs("unknown.core", "CRITICAL") as logs:
            self.assertTrue(svc._persist_memory_latch())
        self.assertTrue(any("REJECTED" in m for m in logs.output), logs.output)
        self.assertIsNone(svc.memory_latch)
        self.assertFalse(svc.latch_path.exists())
        self.assertEqual(stop.stored(h, "ui.stop.1")["status"], "REJECTED")
        self.assertEqual(stop.stored(h, "ui.stop.1")["result"]["code"], "UNKNOWN_ACCOUNT")

    def test_a_latch_file_holding_anything_but_a_stop_of_this_account_fails_closed(self):
        """The latch file is replayed at start. A file whose pending commands are not stops of this account (here an
        owner SELL intent) is not executed: the service starts latched with its own SYSTEM stop
        (UNREADABLE_STOP_LATCH_FILE), records it, and no order is created from the file."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        forged = stop.MemoryLatchTests.intent_request(self, h, "S9", "SELL", "9.00")
        svc.latch_path.write_text(json.dumps({
            "account_key": ACCOUNT, "reason": "OWNER_STOP", "engaged_at_ns": h.clock.now_ns(), "error": "x",
            "stands_in_for": [forged["command_id"]], "cancels": [],
            "pending": [{"command_id": forged["command_id"], "kind": "submit_intent", "payload": forged["payload"],
                         "principal": forged["principal"]}]}))
        again = CoreService(paths=svc.paths, account_key=ACCOUNT, deployment_id=DEPLOYMENT, broker=h.broker, clock=h.clock)
        again.desk = h.desk
        with self.assertLogs("unknown.core", "CRITICAL"):
            again._load_latch_file()
        self.assertEqual(again.memory_latch.reason, "UNREADABLE_STOP_LATCH_FILE")
        self.assertTrue(again._persist_memory_latch())
        self.assertIsNone(h.row("orders", "order_id", "S9"), "nothing from the file but a stop is executed")
        ctl = h.control()
        self.assertEqual((ctl["stop_latched"], ctl["latch_reason"]), (True, "UNREADABLE_STOP_LATCH_FILE"))
        sound(self, h)


# ------------------------------------------------------------ BR-LED-03, BR-SES-02
class DisposalAdjustmentTests(post._Base):
    def disposed(self, h: Harness) -> dict:
        """A completed; B2's late share disposed by D1 at 9.70; every case resolved. Returns D1's execution."""
        self.completed(h, resting_buy=True)
        h.broker.fill("B2", F("1"), F("7.00"), allow_terminal=True)
        h.pump()
        for c in open_cases(h, "EXECUTION_ANOMALY"):
            self.assertTrue(h.owner_cmd("resolve_exception", case_id=c["case_id"], note="late fill confirmed").accepted)
        self.assertTrue(post.disposal(h, "D1", "1", "9.70").accepted)
        h.pump()
        h.broker.fill("D1", F("1"), F("9.70"))
        h.pump()
        for c in open_cases(h):
            self.assertTrue(h.owner_cmd("resolve_exception", case_id=c["case_id"], note="late fill disposed").accepted)
        self.assertEqual(open_cases(h), [])
        self.assertEqual(post.lots(h, "protected:A"), [])
        (sale,) = post.executions(h, "D1")
        return sale

    def test_a_bust_of_a_disposal_fill_opens_a_post_completion_case(self):
        """The broker busts D1's fill: the share comes back to protected:A (the reversal is right), and because a
        bust is never the disposal's expected activity a blocking POST_COMPLETION_ADJUSTMENT naming protected:A
        opens. Before the fix nothing opened and reconciliation was CLEAN, so the share sat in protected:A unseen."""
        h = self.harness(post.BustingBroker)
        sale = self.disposed(h)
        bust = h.broker.bust_execution(sale["exec_key"])
        h.pump()
        (lot,) = post.lots(h, "protected:A")
        self.assertEqual(F(lot["open_qty"]), F("1"))
        (case,) = open_cases(h, "POST_COMPLETION_ADJUSTMENT")
        self.assertEqual((case["blocking"], case["owner_hint"], case["key"]), (True, "protected:A", f"post-completion:A:{bust.exec_key}"))
        self.assert_session_stands(h)
        self.sound(h)

    def test_a_correction_and_a_fee_adjustment_of_a_disposal_fill_open_cases(self):
        """A price correction of D1's fill inside its limit (9.70 -> 9.80, no price anomaly) and a later change of
        its final commission (1.00 -> 1.50) are each booked on protected:A and each opens a POST_COMPLETION_ADJUSTMENT;
        the disposal's own first fill and commission still open none."""
        h = self.harness()
        sale = self.disposed(h)
        self.assertEqual(sale["fee_status"], "FINAL")
        before = post.liquid(h, "protected:A")
        corrected = h.broker.correct_execution(sale["exec_key"], F("1"), F("9.80"))
        h.pump()
        self.assertEqual(post.liquid(h, "protected:A"), before + F("0.10"))
        (case,) = open_cases(h)
        self.assertEqual((case["kind"], case["key"], case["blocking"]),
                         ("POST_COMPLETION_ADJUSTMENT", f"post-completion:A:{corrected.exec_key}", True))
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=case["case_id"], note="correction reviewed").accepted)
        r = h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": corrected.exec_key,
                                                    "amount": "1.50", "currency": "USD"})
        self.assertEqual(r.body["status"], "FINAL", r.body)
        self.assertEqual(post.liquid(h, "protected:A"), before + F("0.10") - F("0.50"))
        (fee_case,) = open_cases(h)
        self.assertEqual((fee_case["kind"], fee_case["owner_hint"]), ("POST_COMPLETION_ADJUSTMENT", "protected:A"))
        self.assertIn("CommissionAdjusted", fee_case["detail"]["what"])
        self.assert_session_stands(h)
        self.sound(h)


# -------------------------------------------------------------------- BR-ORD-03
def report(h: Harness, status: str, broker_order_id: str, reason: str):
    return h.desk.run_system("broker_order_status", {
        "account_key": ACCOUNT, "client_order_id": "B1", "order_ref": None, "broker_order_id": broker_order_id,
        "status": status, "filled_quantity": "0", "remaining_quantity": "3", "reason": reason})


class CloseEvidenceTests(unknown._Base):
    _assert_nothing_moved = unknown.CloseUnknownOrderTests._assert_nothing_moved

    def held_then_reconciled(self, h: Harness) -> dict:
        """B1 stuck UNKNOWN; the broker once reported it held (Inactive, broker id LATE-1); a later reconciliation
        neither lists nor finds it (ORDER_UNRESOLVED only). Returns the UNKNOWN_ORDER case."""
        case = self.stuck(h)
        report(h, "INACTIVE", "LATE-1", "first report")
        h.pump()
        rec = h.reconcile()
        self.assertEqual([d["kind"] for d in rec.body["differences"] if d["blocking"]], ["ORDER_UNRESOLVED"])
        return case

    def test_a_held_report_after_the_run_with_the_same_broker_id_blocks_the_close(self):
        """After the run the broker reports B1 held again, with the same broker id but a new reason: the order
        exists at the broker, so the run no longer proves anything about it. The close is refused
        (RECONCILIATION_PREDATES_THE_ORDERS_LAST_CHANGE) and B1 keeps its hold; it used to be closed REJECTED."""
        h = self.harness()
        case = self.held_then_reconciled(h)
        self.assertEqual(report(h, "INACTIVE", "LATE-1", "held: precaution").body["state"], "UNKNOWN")
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "RECONCILIATION_PREDATES_THE_ORDER")
        rec = h.reconcile()
        self.assertEqual([d["kind"] for d in rec.body["differences"] if d["blocking"]], ["ORDER_UNRESOLVED"])
        self.assertTrue(self.close(h, case).accepted, "a run after the report decides again")
        self.sound(h)

    def test_an_identical_held_report_after_the_run_also_blocks_the_close(self):
        """The same held report again right away is a no-op, but once a reconciliation has run since it was
        recorded, the same report is recorded again (the broker still has the order), so a close cannot rely on
        that earlier run."""
        h = self.harness()
        case = self.stuck(h)
        report(h, "INACTIVE", "LATE-1", "held")
        self.assertTrue(report(h, "INACTIVE", "LATE-1", "held").body.get("noop"), "no run since: a duplicate")
        h.pump()
        h.reconcile()
        self.assertFalse(report(h, "INACTIVE", "LATE-1", "held").body.get("noop"), "recorded again after a run")
        self._assert_nothing_moved(h, self.close(h, case), "CLOSE_NOT_PROVEN", "RECONCILIATION_PREDATES_THE_ORDER")
        self.sound(h)


# -------------------------------------------------------------------- BR-ORD-07
class TerminalOrderHeldAtBrokerTests(unknown._Base):
    def test_a_closed_order_the_broker_lists_as_inactive_is_flagged_and_canceled(self):
        """The owner closed B1 on evidence (REJECTED, hold released); later the broker lists B1 among its open
        orders as INACTIVE. A held order is not final and may work again, so this is the same as a locally terminal
        order still working: the run reports TERMINAL_ORDER_WORKING (blocking), a high EXECUTION_ANOMALY opens and a
        forced cancel is queued for the buy. It used to reconcile CLEAN with nothing open."""
        h = self.harness()
        case = self.stuck(h)
        self.assertTrue(self.close(h, case).accepted)
        h.broker.held_open.add("B1")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        self.assertIn("TERMINAL_ORDER_WORKING", [d["kind"] for d in rec.body["differences"] if d["blocking"]])
        (anomaly,) = [c for c in open_cases(h, "EXECUTION_ANOMALY") if c["key"] == "working-after-terminal:B1"]
        self.assertEqual(anomaly["detail"]["broker_status"], "INACTIVE")
        cancels = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM outbox WHERE order_id='B1'")]
        self.assertTrue(any(b["action"] == "CANCEL" and b.get("force") and b["state"] == "PENDING" for b in cancels), cancels)
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "REJECTED", "a terminal state is never left")
        self.sound(h)


if __name__ == "__main__":
    unittest.main()
