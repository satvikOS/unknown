"""Adversarial verification of commit c41a505 ("Fix core defects found by the fault-matrix suite and
adversarial review").

Each test reproduces the original failure scenario of one confirmed review finding through the public
Harness/Desk interfaces and asserts the corrected behaviour, then probes edge cases the fix might have
missed. Tests named ``test_fNN_...`` refer to review finding NN. Every money-changing test asserts the
ledger invariants and that a replay of the event log reproduces every projection (ADR-0005 #3).

Tests decorated with ``@unittest.expectedFailure`` document behaviour that is STILL WRONG at c41a505:
their docstrings name the finding, the exact wrong behaviour and where it comes from. They keep the
suite green while the defect stands; when the source is fixed they become unexpected successes and the
decorator must be removed.

Every number is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, DEPLOYMENT, Harness, load_policy  # noqa: E402

from unknown.broker.port import BrokerOrderStatus  # noqa: E402
from unknown.broker.simulated import CancelBehavior  # noqa: E402
from unknown.core.desk import HealthSnapshot  # noqa: E402
from unknown.core.replay import replay_and_compare, verify_chain  # noqa: E402
from unknown.domain.errors import CommandConflict  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway.runner import execution_payload  # noqa: E402

F = Fixed.parse
SEC = 1_000_000_000
DAY = 86_400 * SEC
INST = "FIX-EQ-1"
LIQUID = ("CASH", "RECEIVABLE", "PAYABLE", "FEES_ACCRUED")


# --------------------------------------------------------------------- helpers
def running(h: Harness, sid: str = "A") -> None:
    """Bootstrapped account with one RUNNING session (template seed 100.00, target 105.00)."""
    h.bootstrap()
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=sid)
    assert r.accepted and r.body["state"] == "RUNNING", r.body


def order(h: Harness, order_id: str) -> dict:
    return h.row("orders", "order_id", order_id)


def outbox(h: Harness, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM outbox WHERE order_id=? ORDER BY created_seq, outbox_id", (order_id,))]


def cancel_rows(h: Harness, order_id: str) -> list[dict]:
    return [b for b in outbox(h, order_id) if b["action"] == "CANCEL"]


def hold(h: Harness, order_id: str) -> dict:
    (r,) = h.desk.store.query("SELECT * FROM holds WHERE order_id=?", (order_id,))
    return dict(r)


def executions(h: Harness, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM executions WHERE order_id=? ORDER BY rowid", (order_id,))]


def all_lots(h: Harness) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM lots ORDER BY rowid")]


def open_lots(h: Harness, owner_prefix: str = "") -> list[dict]:
    return [l for l in all_lots(h) if l["owner"].startswith(owner_prefix) and not F(l["open_qty"]).is_zero()]


def ledger_position(h: Harness, instrument_id: str = INST) -> Fixed:
    total = Fixed(0)
    for lot in all_lots(h):
        if lot["instrument_id"] == instrument_id:
            total = total + F(lot["open_qty"])
    return total


def events(h: Harness, event_type: str) -> list[dict]:
    return [json.loads(r["payload"]) for r in h.desk.store.query(
        "SELECT payload FROM events WHERE type=? ORDER BY seq", (event_type,))]


def cases(h: Harness, kind: str, status: str = "OPEN") -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM exceptions WHERE kind=? AND status=? ORDER BY rowid", (kind, status))]


def liquid(h: Harness, owner: str) -> Fixed:
    total = Fixed(0)
    for code in LIQUID:
        total = total + h.balance(code, owner)
    return total


def code_total(h: Harness, code: str) -> Fixed:
    total = Fixed(0)
    for r in h.desk.store.query("SELECT amount FROM balances WHERE account_key=? AND account_code=?", (ACCOUNT, code)):
        total = total + F(r["amount"])
    return total


def broker_status(h: Harness, order_id: str) -> str | None:
    lk = h.broker.lookup(order_id)
    return lk.status.value if lk.found else None


def buy_acked(h: Harness, oid: str, qty: str, limit: str, sid: str = "A") -> None:
    r = h.intent(oid, "BUY", qty, limit, sid)
    assert r.accepted, r.body
    h.pump()
    assert order(h, oid)["state"] == "ACKNOWLEDGED", order(h, oid)


def bought(h: Harness, oid: str, qty: str, price: str, sid: str = "A", **fill) -> dict:
    buy_acked(h, oid, qty, price, sid)
    h.broker.fill(oid, F(qty), F(price), **fill)
    h.pump()
    assert order(h, oid)["state"] == "FILLED", order(h, oid)
    return executions(h, oid)[-1]


def settle_next_day(h: Harness):
    h.clock.advance(DAY)
    h.quote("9.99", "10.01")
    h.broker.advance_settlement()
    return h.reconcile()


def restore_in_place(h: Harness, copy: Path) -> None:
    """Operator restore: stop the process, put ``copy`` at the live path (beside the live <db>.epoch), reopen."""
    h.desk.store.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{h.db_path}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(copy, h.db_path)
    h.desk = h._open()


def sqlite_online_backup(source: Path, target: Path) -> None:
    """A backup made with the SQLite online backup API by any tool other than Store.backup_to
    (for example the sqlite3 shell's .backup command), as ADR-0003 #6 describes backups."""
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


class _Base(unittest.TestCase):
    def harness(self, **kw) -> Harness:
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h

    def scratch(self) -> Path:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def assertSound(self, h: Harness) -> None:
        """Ledger invariants hold and a replay of the event log reproduces every projection."""
        self.assertEqual(h.verify(), [])
        self.assertEqual(replay_and_compare(h.db_path, self.scratch() / "replay.sqlite"), [])

    def assertFixed(self, actual, expected: str, msg: str | None = None) -> None:
        actual = actual if isinstance(actual, Fixed) else F(actual)
        self.assertEqual(actual, F(expected), msg or f"{actual.to_str()} != {expected}")


# ============================================================ cancels (2, 12, 14, 18, 35)
class CancelTransmissionTests(_Base):
    def test_f12_f01_cancel_is_transmitted_while_reconciliation_is_required_after_disconnect(self):
        """Finding 12 (and 1): a red-stop CANCEL goes out although a disconnect set needs_reconciliation;
        the reservation is kept until the broker confirms."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.broker.disconnect()
        h.broker.reconnect()
        h.pump()
        self.assertTrue(h.control()["needs_reconciliation"])
        r = h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(hold(h, "B1")["status"], "ACTIVE", "a cancel request never releases the reservation")
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.assertSound(h)

    def test_f12_cancel_is_transmitted_after_restart_before_any_reconciliation(self):
        """Finding 12: after a restart (reconciled_epoch is stale) a stop's CANCEL still goes out."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.restart()
        self.assertNotEqual(h.control()["reconciled_epoch"], h.desk.store.epoch)
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertSound(h)

    def test_f12_buy_submit_is_still_held_while_reconciliation_is_required(self):
        """Finding 12 (guard): only CANCEL rows bypass the gate; an exposure-increasing SUBMIT does not."""
        h = self.harness()
        running(h)
        self.assertTrue(h.intent("B1", "BUY", "2", "10.00", "A").accepted)
        h.broker.disconnect()
        h.broker.reconnect()
        h.runner.drain_events()   # the core learns of the disconnect before the next outbox pass
        self.assertTrue(h.control()["needs_reconciliation"])
        h.pump()
        self.assertIsNone(broker_status(h, "B1"))
        self.assertEqual(order(h, "B1")["state"], "RESERVED")
        self.assertSound(h)

    def _cancel_lost_in_crash(self, h: Harness) -> None:
        """Stop, claim the CANCEL row (SENDING), then crash before broker.cancel and outbox_result."""
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        (row,) = cancel_rows(h, "B1")
        begin = h.desk.run_system("outbox_begin", {"outbox_id": row["outbox_id"]})
        self.assertTrue(begin.accepted and begin.body["send"], begin.body)
        self.assertEqual(order(h, "B1")["cancel_state"], "SENT")
        h.restart()   # the process died between outbox_begin and outbox_result
        self.assertEqual(broker_status(h, "B1"), "ACKNOWLEDGED", "the cancel never reached the broker")
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCEL_PENDING", "UNKNOWN"))
        self.assertEqual([b["state"] for b in cancel_rows(h, "B1")], ["NEEDS_LOOKUP"])

    def test_f02_f14_f18_cancel_lost_in_crash_is_reissued_by_a_second_stop(self):
        """Findings 2, 14, 18: a crash while a CANCEL is SENDING leaves the order cancelable: the next
        stop enqueues a fresh CANCEL, it is transmitted and the broker cancels the buy."""
        h = self.harness()
        self._cancel_lost_in_crash(h)
        r = h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body["entries_canceled"], [{"order_id": "B1", "state": "CANCEL_PENDING"}])
        self.assertEqual([b["state"] for b in cancel_rows(h, "B1")], ["NEEDS_LOOKUP", "PENDING"])
        self.assertEqual(order(h, "B1")["prev_state"], "ACKNOWLEDGED", "a re-issued cancel keeps the pre-cancel state")
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.assertSound(h)

    def test_f14_f18_cancel_lost_in_crash_is_resent_by_reconciliation(self):
        """Findings 14, 18: without a second stop, reconciliation sees the order still working without
        PENDING_CANCEL, records CANCEL_NOT_EFFECTIVE and sends the cancel again."""
        h = self.harness()
        self._cancel_lost_in_crash(h)
        h.connect()
        rec = h.reconcile()
        kinds = [d["kind"] for d in rec.body["differences"]]
        self.assertIn("CANCEL_NOT_EFFECTIVE", kinds)
        self.assertEqual(cancel_rows(h, "B1")[-1]["state"], "PENDING")
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertSound(h)

    def test_f02_cancel_requested_twice_before_transmission_queues_one_row(self):
        """Finding 2 (guard): re-issuing is idempotent while a CANCEL row is queued or in flight."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        self.assertTrue(h.owner_cmd("request_cancel", order_id="B1").accepted)
        self.assertEqual(len(cancel_rows(h, "B1")), 1)
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertSound(h)

    def test_f35_cancel_timeout_blocks_entries_and_stays_reissuable(self):
        """Finding 35: a cancel whose outcome is UNKNOWN opens a blocking UNKNOWN_ORDER case, green start is
        refused, and a later stop re-sends the cancel; the broker's terminal report clears the case."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.broker.script_cancel(CancelBehavior.TIMEOUT)
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        h.pump()
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCEL_PENDING", "UNKNOWN"))
        (case,) = cases(h, "UNKNOWN_ORDER")
        self.assertTrue(case["blocking"])
        self.assertEqual(broker_status(h, "B1"), "ACKNOWLEDGED")
        h.quote("9.99", "10.01")
        refused = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertFalse(refused.accepted)
        self.assertIn("BLOCKING_EXCEPTIONS:UNKNOWN_ORDER", refused.body["details"]["blockers"])
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertEqual(cases(h, "UNKNOWN_ORDER"), [])
        self.assertSound(h)

    def test_f35_cancel_timeout_then_reconciliation_resends_the_cancel(self):
        """Finding 35: reconciliation after a timed-out cancel finds the buy still working, reports
        CANCEL_NOT_EFFECTIVE and re-sends the cancel instead of reporting the order as handled."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.broker.script_cancel(CancelBehavior.TIMEOUT)
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        h.pump()
        rec = h.reconcile()
        self.assertIn("CANCEL_NOT_EFFECTIVE", [d["kind"] for d in rec.body["differences"]])
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertSound(h)


    def test_f02_cancel_that_reached_the_broker_before_the_crash_is_not_sent_twice(self):
        """Finding 2 (guard): the crash came after the broker canceled B1; after restart the broker's CANCELED
        report finalizes the order and no second cancel is queued."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        (row,) = cancel_rows(h, "B1")
        self.assertTrue(h.desk.run_system("outbox_begin", {"outbox_id": row["outbox_id"]}).body["send"])
        h.broker.cancel("B1", order(h, "B1")["broker_order_id"])
        h.restart()
        h.pump()
        o = order(h, "B1")
        self.assertEqual((o["state"], o["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertEqual([b["state"] for b in cancel_rows(h, "B1")], ["NEEDS_LOOKUP"])
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).body["entries_canceled"] == [])
        self.assertSound(h)


# ============================================================ system latches (3, 13, 36, 42)
class SystemLatchTests(_Base):
    def test_f03_f13_foreign_open_order_latch_pulls_our_open_buy(self):
        """Findings 3, 13: a FOREIGN_ACTIVITY latch from reconciliation cancels our working buy, and the
        CANCEL is transmitted although the failed reconciliation left needs_reconciliation set."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.broker.foreign_order(INST, "BUY", F("1"), F("9.00"))
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"]), (True, "FOREIGN_ACTIVITY"))
        self.assertTrue(c["needs_reconciliation"])
        self.assertEqual(len(cancel_rows(h, "B1")), 1)
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.assertSound(h)

    def test_f03_f13_unmapped_execution_latch_pulls_our_open_buy(self):
        """Findings 3, 13: an execution in an instrument outside the master latches the stop through
        latch_stop, which also cancels open buys."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.broker.external_trade("NOT-IN-MASTER", "BUY", F("1"), F("5.00"))
        h.pump()
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"]), (True, "UNMAPPED_EXECUTION"))
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertSound(h)

    def test_f03_foreign_execution_latch_pulls_our_open_buy(self):
        """Findings 3, 13: an execution tagged by another deployment latches and cancels open buys."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.desk.run_system("broker_execution", {
            "account_key": ACCOUNT, "exec_key": "sim:SIM:FOREIGN-1", "provider_exec_id": "FOREIGN-1",
            "client_order_id": None, "order_ref": "aws-prod:3:x", "broker_order_id": "FOREIGN-9",
            "instrument_id": INST, "provider_ref": INST, "side": "BUY", "quantity": "1", "price": "10.00",
            "currency": "USD", "exec_time_ns": h.clock.now_ns(), "correction_of": None})
        self.assertEqual(h.control()["latch_reason"], "FOREIGN_ACTIVITY")
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual(h.verify(), [])

    def test_f36_broker_working_order_that_is_terminal_locally_is_flagged_and_pulled(self):
        """Finding 36: a stale CANCELED status released B1's reservation while the broker still works it;
        reconciliation records a blocking difference, opens a case, latches and sends a forced cancel."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.desk.run_system("broker_order_status", {"account_key": ACCOUNT, "client_order_id": "B1", "order_ref": None,
                                                  "broker_order_id": order(h, "B1")["broker_order_id"],
                                                  "status": "CANCELED", "filled_quantity": "0",
                                                  "remaining_quantity": "0", "reason": "stale"})
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        self.assertIn("TERMINAL_ORDER_WORKING", [d["kind"] for d in rec.body["differences"]])
        self.assertTrue(any(c["detail"].get("issue") == "broker lists a locally terminal order as working"
                            for c in cases(h, "EXECUTION_ANOMALY")))
        self.assertTrue(h.control()["stop_latched"])
        self.assertTrue(cancel_rows(h, "B1")[-1]["force"])
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertSound(h)

    def test_f36_locally_terminal_sell_still_working_blocks_a_second_sale(self):
        """Finding 36 (probe, sell side): a stale CANCELED released S1's quantity hold while the broker still
        works it. Reconciliation flags it; the forced cancel is only for buys, so the freed shares must not
        be sold twice: the open EXECUTION_ANOMALY blocks further reductions until the owner resolves it."""
        h = self.harness()
        running(h)
        bought(h, "B1", "2", "10.00")
        h.quote("9.99", "10.01")
        self.assertTrue(h.intent("S1", "SELL", "2", "10.00", "A").accepted)
        h.pump()
        h.desk.run_system("broker_order_status", {"account_key": ACCOUNT, "client_order_id": "S1", "order_ref": None,
                                                  "broker_order_id": order(h, "S1")["broker_order_id"],
                                                  "status": "CANCELED", "filled_quantity": "0",
                                                  "remaining_quantity": "0", "reason": "stale"})
        rec = h.reconcile()
        self.assertIn("TERMINAL_ORDER_WORKING", [d["kind"] for d in rec.body["differences"]])
        self.assertEqual(cancel_rows(h, "S1"), [])
        h.quote("9.99", "10.01")
        second = h.intent("S2", "SELL", "2", "10.00", "A")
        self.assertFalse(second.accepted, "the shares S1 still offers at the broker cannot be offered again")
        self.assertSound(h)

    def test_f42_failed_reconciliation_latches_and_needs_a_green_start(self):
        """Finding 42: a reconciliation with blocking differences latches the stop (RECONCILIATION_FAILURE);
        resolving the difference and a later CLEAN run do not resume entries without an owner start."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        h.broker.deposit(F("10.00"))  # unexplained cash at the broker
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"]), (True, "RECONCILIATION_FAILURE"))
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED", "the failure latch pulls open buys")
        (mismatch,) = cases(h, "CASH_MISMATCH")
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=mismatch["case_id"], action="record_external_cash_flow",
                                    currency="USD", amount="10.00", note="owner deposit").accepted)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertTrue(h.control()["stop_latched"])
        self.assertEqual(h.intent("B2", "BUY", "1", "10.00", "A").rejected_code, "STOP_LATCH")
        h.quote("9.99", "10.01")
        ok = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(ok.accepted, ok.body)
        self.assertTrue(h.intent("B3", "BUY", "1", "10.00", "A").accepted)
        self.assertSound(h)


# ============================================================ restore (4, 20)
class RestoreDetectionTests(_Base):
    def _trade_then_stop(self, h: Harness, backup) -> None:
        """B1 is reserved, a backup is taken, then the owner's stop cancels B1 before transmission."""
        running(h)
        self.assertTrue(h.intent("B1", "BUY", "2", "10.00", "A").accepted)
        backup(h)
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        self.assertEqual(order(h, "B1")["state"], "CANCELED")

    def _reopen_elsewhere(self, h: Harness, copy: Path) -> Harness:
        h.desk.store.close()
        h2 = Harness(db_path=copy, broker=h.broker, clock=h.clock, seed=7 * 10**12)  # ids disjoint from h's
        h2.n = 1000   # command ids disjoint from the ones stored in the copy
        self.addCleanup(h2.desk.store.close)
        return h2

    def _assert_restore_detected(self, h: Harness) -> None:
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"]), (True, "RESTORED_FROM_BACKUP"))
        self.assertEqual(len(cases(h, "RESTORED_FROM_BACKUP")), 1)
        self.assertEqual(order(h, "B1")["state"], "UNKNOWN")
        h.connect()
        h.pump()
        self.assertIsNone(broker_status(h, "B1"), "an order the stop canceled is never sent again")
        submit = next(b for b in outbox(h, "B1") if b["action"] == "SUBMIT")
        self.assertEqual(submit["state"], "NEEDS_LOOKUP")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(order(h, "B1")["state"], "REJECTED", "conclusively never received by the broker")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.assertSound(h)

    def test_f04_restore_in_place_of_a_backup_taken_in_the_current_epoch(self):
        """Finding 4: the most common restore (a Store.backup_to copy from the current epoch put back at the
        live path beside the live <db>.epoch) is detected: the stop is latched and B1 is never re-sent."""
        h = self.harness()
        backup = self.scratch() / "backup.sqlite"
        self._trade_then_stop(h, lambda hh: hh.desk.store.backup_to(backup))
        restore_in_place(h, backup)
        self._assert_restore_detected(h)

    def test_f20_backup_copied_to_another_directory_without_its_epoch_file(self):
        """Finding 20: a Store.backup_to copy opened in a different directory (no <db>.epoch) is a restore."""
        h = self.harness()
        backup = self.scratch() / "backup.sqlite"
        self._trade_then_stop(h, lambda hh: hh.desk.store.backup_to(backup))
        self.assertFalse(Path(f"{backup}.epoch").exists())
        h2 = self._reopen_elsewhere(h, backup)
        (started,) = [e for e in events(h2, "ProcessStartedForAccount") if e["info"]["restored"]]
        self._assert_restore_detected(h2)

    def test_f20_unmarked_copy_in_another_directory_is_detected_by_the_missing_marker(self):
        """Finding 20: even a copy without the backup marker (plain SQLite online backup API) opened where
        no <db>.epoch exists is treated as restored, because the database is not empty."""
        h = self.harness()
        backup = self.scratch() / "core.sqlite"
        self._trade_then_stop(h, lambda hh: sqlite_online_backup(hh.db_path, backup))
        h2 = self._reopen_elsewhere(h, backup)
        evidence = [e["info"] for e in events(h2, "ProcessStarted") + events(h2, "ProcessStartedForAccount")]
        self.assertTrue(any(i.get("restored") for i in evidence))
        self._assert_restore_detected(h2)

    def test_f20_restart_after_a_detected_restore_is_an_ordinary_restart(self):
        """Finding 20: the backup marker is consumed by the restore; the next restart is not another restore."""
        h = self.harness()
        backup = self.scratch() / "backup.sqlite"
        self._trade_then_stop(h, lambda hh: hh.desk.store.backup_to(backup))
        restore_in_place(h, backup)
        self.assertIsNone(h.desk.store.backup_origin())
        h.restart()
        last = events(h, "ProcessStartedForAccount")[-1]
        self.assertFalse(last["info"]["restored"])
        self.assertEqual(len(cases(h, "RESTORED_FROM_BACKUP")), 1)
        self.assertEqual(h.desk.verify(), [])

    def test_f04_restored_buy_that_was_transmitted_after_the_backup_is_pulled(self):
        """Findings 3, 4: B1 was sent after the backup; the restored copy thinks it is RESERVED. The restore
        latch marks it UNKNOWN, never re-submits it, and the stop's cancel reaches the broker."""
        h = self.harness()
        running(h)
        self.assertTrue(h.intent("B1", "BUY", "2", "10.00", "A").accepted)
        backup = self.scratch() / "backup.sqlite"
        h.desk.store.backup_to(backup)
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "ACKNOWLEDGED")
        restore_in_place(h, backup)
        self.assertEqual(order(h, "B1")["state"], "UNKNOWN")
        h.pump()
        self.assertEqual(broker_status(h, "B1"), "CANCELED")
        self.assertEqual([b["state"] for b in outbox(h, "B1") if b["action"] == "SUBMIT"], ["NEEDS_LOOKUP"])
        rec = h.reconcile()
        self.assertEqual(order(h, "B1")["state"], "CANCELED", rec.body)
        self.assertSound(h)


# ============================================================ authority (5, 19, 21, 22, 23, 40, 41, 52, 54)
class AuthorityTests(_Base):
    def test_f05_f19_agent_cannot_preclaim_a_process_start_id(self):
        """Findings 5, 19: an AGENT using a reserved system id is refused, nothing is stored under the id,
        and the next process start keeps full restart semantics."""
        h = self.harness()
        h.bootstrap()
        guess = f"process.{DEPLOYMENT}.{h.desk.store.epoch + 1}"
        r = h.cmd("process_started", h.agent, command_id=guess, epoch=h.desk.store.epoch + 1, restored_from_backup=False)
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM commands WHERE command_id=?", (guess,))[0]["n"], 0)
        r = h.cmd("submit_research_proposal", h.agent, command_id=f"sys.{DEPLOYMENT}.x", proposal={})
        self.assertEqual(r.rejected_code, "UNAUTHORIZED", "reserved prefixes are refused even for permitted kinds")
        r = h.cmd("stop", h.owner, command_id="process.owner-picked", account_key=ACCOUNT)
        self.assertEqual(r.rejected_code, "UNAUTHORIZED", "only SYSTEM may use reserved prefixes")
        h.restart()
        c = h.control()
        self.assertTrue(c["needs_reconciliation"])
        self.assertTrue(c["resume_requires_owner"])
        ids = [r["command_id"] for r in h.desk.store.query("SELECT command_id FROM commands WHERE kind='process_started'")]
        self.assertEqual(len(ids), 2)
        for epoch, cid in zip((1, 2), ids):
            self.assertNotEqual(cid, f"process.{DEPLOYMENT}.{epoch}", "process start ids are not predictable")
            self.assertTrue(cid.startswith(f"process.{DEPLOYMENT}.{epoch}."))

    def test_f05_f19_unauthorized_attempt_never_claims_the_owner_id(self):
        """Findings 5, 19: an AGENT issuing an owner command under id X is refused without storing X, so the
        owner's later command X executes (not a duplicate of the refusal)."""
        h = self.harness()
        running(h)
        r = h.cmd("stop", h.agent, command_id="ui.stop.0001", account_key=ACCOUNT)
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        self.assertFalse(h.control()["stop_latched"])
        r = h.cmd("stop", h.owner, command_id="ui.stop.0001", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertFalse(r.duplicate)
        self.assertTrue(h.control()["stop_latched"])
        self.assertEqual(len(events(h, "CommandRejected")), 1, "the refusal is still recorded as an event")
        self.assertSound(h)

    def test_f05_f52_same_id_from_another_principal_is_a_recorded_conflict(self):
        """Findings 5, 52: an id already used by one principal, presented by another with the same payload,
        is a conflict: it raises, never returns the stored result, and is recorded durably."""
        h = self.harness()
        h.bootstrap()
        proposal = {"schema_version": 1, "job_id": "job-1", "hypothesis": "h", "evidence": [], "horizon_seconds": 60,
                    "assumptions": [], "abstain": False, "reason": "r"}
        first = h.cmd("submit_research_proposal", h.agent, command_id="shared.1", proposal=proposal)
        self.assertTrue(first.accepted, first.body)
        with self.assertRaises(CommandConflict):
            h.cmd("submit_research_proposal", h.owner, command_id="shared.1", proposal=proposal)
        (conflict,) = events(h, "CommandIdConflict")
        self.assertEqual((conflict["info"]["difference"], conflict["info"]["principal_id"]), ("principal", "owner"))
        again = h.cmd("submit_research_proposal", h.agent, command_id="shared.1", proposal=proposal)
        self.assertTrue(again.duplicate)
        self.assertSound(h)

    def test_f52_reused_id_with_different_payload_records_an_event_and_keeps_the_original(self):
        """Finding 52: a reused command id with a different payload leaves a CommandIdConflict event (it
        survives the refusal) and the original command's result is unchanged."""
        h = self.harness()
        running(h)
        r = h.cmd("stop", h.owner, command_id="ui.stop.0002", account_key=ACCOUNT)
        self.assertTrue(r.accepted)
        with self.assertRaises(CommandConflict):
            h.cmd("stop", h.owner, command_id="ui.stop.0002", account_key=ACCOUNT, reason="different")
        (conflict,) = events(h, "CommandIdConflict")
        self.assertEqual(conflict["info"]["difference"], "payload")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM commands WHERE command_id='ui.stop.0002'")[0]["n"], 1)
        self.assertEqual(h.control()["latch_reason"], "OWNER_STOP")
        self.assertSound(h)

    def test_f54_chain_detects_reattribution_of_every_column(self):
        """Finding 54: re-attributing an event (principal, account, command, epoch, times, seq order) breaks
        the hash chain, not only editing its payload."""
        h = self.harness()
        running(h)
        bought(h, "B1", "2", "10.00")
        h.desk.store.close()
        conn = sqlite3.connect(str(h.db_path))
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(verify_chain(conn), [])
            target = conn.execute("SELECT seq FROM events WHERE type='ExecutionRecorded'").fetchone()["seq"]
            for column, value in (("principal_id", "agent.research"), ("account_key", "OTHER"), ("command_id", "forged"),
                                  ("writer_epoch", 99), ("recorded_time_ns", 1), ("session_id", "Z"),
                                  ("correlation_id", "c"), ("order_id", "B9"), ("event_time_ns", 7)):
                original = conn.execute(f"SELECT {column} FROM events WHERE seq=?", (target,)).fetchone()[0]
                conn.execute(f"UPDATE events SET {column}=? WHERE seq=?", (value, target))
                problems = verify_chain(conn)
                self.assertIn(f"event {target} chain hash mismatch", problems, column)
                conn.execute(f"UPDATE events SET {column}=? WHERE seq=?", (original, target))
            self.assertEqual(verify_chain(conn), [])
            conn.commit()
        finally:
            conn.close()
        h.desk = h._open()

    def _health(self, h: Harness, **overrides):
        base = h.health()
        fields = {"broker_connected": base.broker_connected, "broker_authenticated": base.broker_authenticated,
                  "broker_server_time_ns": base.broker_server_time_ns, "disk_free_mb": base.disk_free_mb,
                  "detail": base.detail, "broker_account_mode": base.broker_account_mode, **overrides}
        h.desk.health = lambda: HealthSnapshot(**fields)

    def _ready_to_start(self) -> Harness:
        h = self.harness()
        h.bootstrap(start=False)
        h.quote("9.99", "10.01")
        return h

    def test_f21_preflight_fails_closed_on_unknown_broker_account_mode(self):
        """Finding 21: missing broker-reported mode blocks green start."""
        h = self._ready_to_start()
        self._health(h, broker_account_mode=None)
        r = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertIn("BROKER_ACCOUNT_MODE_UNKNOWN", r.body["details"]["blockers"])

    def test_f21_preflight_refuses_a_broker_reporting_a_live_account(self):
        """Finding 21: a SIMULATED registration does not authorize a connection the broker reports as LIVE."""
        h = self._ready_to_start()
        self._health(h, broker_account_mode="LIVE")
        blockers = h.owner_cmd("start", account_key=ACCOUNT).body["details"]["blockers"]
        self.assertIn("BROKER_REPORTS_LIVE_ACCOUNT", blockers)
        self.assertIn("BROKER_ACCOUNT_MODE_MISMATCH:LIVE", blockers)

    def test_f41_preflight_fails_closed_without_broker_server_time(self):
        """Finding 41: missing broker server time is CLOCK_SKEW_UNKNOWN, never a pass."""
        h = self._ready_to_start()
        self._health(h, broker_server_time_ns=None)
        self.assertIn("CLOCK_SKEW_UNKNOWN", h.owner_cmd("start", account_key=ACCOUNT).body["details"]["blockers"])

    def test_f22_paper_account_ownership_needs_certificate_or_attestation(self):
        """Finding 22: outside SIMULATED, ownership is not granted without a release certificate or an
        explicit owner attestation, and the grant forces a reconciliation before any entry."""
        h = self.harness()
        self.assertTrue(h.owner_cmd("register_account", account_key="PAPER-1", provider="simulated", mode="PAPER",
                                    base_currency="USD").accepted)
        r = h.owner_cmd("acquire_execution_ownership", account_key="PAPER-1", deployment_id=DEPLOYMENT)
        self.assertEqual(r.rejected_code, "RELEASE_CERTIFICATE_REQUIRED")
        r = h.owner_cmd("acquire_execution_ownership", account_key="PAPER-1", deployment_id=DEPLOYMENT,
                        attest_no_other_deployment=True)
        self.assertTrue(r.accepted, r.body)
        self.assertTrue(h.row("control", "account_key", "PAPER-1")["needs_reconciliation"])

    def test_f23_strategy_cannot_borrow_an_eligible_strategy_id(self):
        """Finding 23: a STRATEGY principal naming another (eligible) strategy id is refused; its own id is
        checked against the mandate's eligibility list."""
        h = self.harness()
        running(h)
        r = h.intent("S1", "BUY", "1", "10.00", "A", principal=h.strategy, strategy_id="owner-manual")
        self.assertEqual(r.rejected_code, "STRATEGY_IDENTITY_MISMATCH")
        r = h.intent("S2", "BUY", "1", "10.00", "A", principal=h.strategy)
        self.assertEqual(r.rejected_code, "INVALID_FIELD")
        r = h.intent("S3", "BUY", "1", "10.00", "A", principal=h.strategy, strategy_id=h.strategy.id)
        self.assertFalse(r.accepted)
        self.assertNotEqual(r.rejected_code, "STRATEGY_IDENTITY_MISMATCH")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)

    def test_f23_strategy_named_in_the_mandate_can_trade_and_cancel_only_its_own_orders(self):
        """Finding 23 (guard): the bound identity works when the mandate names it; it cannot cancel others'."""
        h = self.harness(policy=load_policy(session_template__strategy_eligibility=["owner-manual", "strategy.fixture"]))
        running(h)
        r = h.intent("S1", "BUY", "1", "10.00", "A", principal=h.strategy, strategy_id=h.strategy.id)
        self.assertTrue(r.accepted, r.body)
        self.assertTrue(h.intent("O1", "BUY", "1", "10.00", "A").accepted)
        r = h.cmd("request_cancel", h.strategy, order_id="O1")
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        self.assertTrue(h.cmd("request_cancel", h.strategy, order_id="S1").accepted)
        self.assertSound(h)

    def test_f40_system_cannot_submit_intents(self):
        """Finding 40: SYSTEM may not issue submit_intent (its wind-down sells use create_order directly)."""
        h = self.harness()
        running(h)
        sess = h.row("sessions", "session_id", "A")
        mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])
        r = h.desk.run_system("submit_intent", {
            "account_key": ACCOUNT, "session_id": "A", "order_id": "SYS1", "instrument_id": INST, "side": "BUY",
            "quantity": "1", "limit_price": "10.00", "deadline_ns": h.clock.now_ns() + 60 * SEC,
            "policy_hash": mandate["policy_hash"]})
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        self.assertIsNone(order(h, "SYS1"))


# ============================================================ executions and commissions (6, 7, 15, 17, 26, 28, 38)
class ExecutionLedgerTests(_Base):
    def _overfilled_sell(self, h: Harness) -> dict:
        """Session A buys 2 @ 10.00, then its sell of 2 is overfilled by the broker to 3 (one share uncovered)."""
        running(h)
        bought(h, "B1", "2", "10.00")
        h.quote("9.99", "10.01")
        r = h.intent("S1", "SELL", "2", "10.00", "A")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        h.broker.fill("S1", F("3"), F("10.00"), allow_overfill=True)
        h.pump()
        (ex,) = executions(h, "S1")
        return ex

    def test_f06_uncovered_sell_settles_excess_to_the_exception_owner(self):
        """Finding 6: the selling session's settlement covers only its own shares (20.00 - 1.00 commission);
        the uncovered 10.00 settles to exception:<case>, so settlement never credits the session the excess."""
        h = self.harness()
        ex = self._overfilled_sell(h)
        (case,) = [c for c in cases(h, "EXECUTION_ANOMALY") if c["detail"].get("issue") == "sell exceeds owned quantity"]
        quarantine = f"exception:{case['case_id']}"
        rows = {s["settlement_id"]: s for s in (json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM settlements"))}
        main, excess = rows[f"stl:{ex['exec_key']}"], rows[f"stl:{ex['exec_key']}:excess"]
        self.assertEqual((main["owner"], main["direction"], main["relieves"]), ("session:A", "RECEIVE", "RECEIVABLE"))
        self.assertFixed(main["amount"], "19.00")
        self.assertEqual((excess["owner"], excess["direction"]), (quarantine, "RECEIVE"))
        self.assertFixed(excess["amount"], "10.00")
        self.assertEqual(ledger_position(h), F("-1"))
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "session:A"), "98.00")   # 100 - 21.00 + 19.00
        self.assertFixed(h.balance("RECEIVABLE", "session:A"), "0")
        self.assertFixed(h.balance("CASH", quarantine), "10.00")
        self.assertFixed(h.balance("RECEIVABLE", quarantine), "0")
        self.assertSound(h)

    def test_f28_correcting_an_uncovered_sell_removes_the_quarantine_short(self):
        """Finding 28: the broker corrects the overfilled sell to 2 shares; the quarantine short lot is
        closed with the other lot effects, the ledger position is flat like the broker's, and settlement
        reconciles."""
        h = self.harness()
        ex = self._overfilled_sell(h)
        h.broker.correct_execution(ex["exec_key"], F("2"), F("10.00"))
        h.pump()
        self.assertEqual(open_lots(h, "exception:"), [])
        self.assertEqual(ledger_position(h), F("0"))
        self.assertEqual(open_lots(h), [])
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "session:A"), "98.00")
        for owner in {l["owner"] for l in all_lots(h) if l["owner"].startswith("exception:")}:
            self.assertFixed(liquid(h, owner), "0", owner)
        self.assertSound(h)

    def test_f28_correcting_a_covered_sell_into_an_uncovered_one_quarantines_once(self):
        """Finding 28 (probe): the reverse direction. A covered sell of 2 is corrected to 3 shares; exactly one
        share is quarantined, and correcting it back to 2 removes the quarantine again."""
        h = self.harness()
        running(h)
        bought(h, "B1", "2", "10.00")
        h.quote("9.99", "10.01")
        self.assertTrue(h.intent("S1", "SELL", "2", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("S1", F("2"), F("10.00"))
        h.pump()
        (ex,) = executions(h, "S1")
        r1 = h.broker.correct_execution(ex["exec_key"], F("3"), F("10.00"))
        h.pump()
        self.assertEqual(ledger_position(h), F("-1"))
        self.assertEqual(len(open_lots(h, "exception:")), 1)
        h.broker.correct_execution(r1.exec_key, F("2"), F("10.00"))
        h.pump()
        self.assertEqual(open_lots(h, "exception:"), [])
        self.assertEqual(ledger_position(h), F("0"))
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "session:A"), "98.00")
        self.assertSound(h)

    def test_f07_f17_deferred_commission_in_another_currency_never_discards_the_fill(self):
        """Findings 7, 17: a commission in EUR arrives before its USD execution; the execution is still booked,
        a blocking anomaly records the commission, and the commission stays pending (never applied)."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        exec_key = "sim:SIM:E1"   # the simulated broker's next execution id
        r = h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": exec_key, "amount": "1.00",
                                                    "currency": "EUR"})
        self.assertTrue(r.accepted and r.body.get("deferred"), r.body)
        row = h.broker.fill("B1", F("2"), F("10.00"))
        self.assertEqual(row.exec_key, exec_key)
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "FILLED")
        self.assertEqual(len(executions(h, "B1")), 1)
        self.assertTrue(any("currency differs" in c["detail"].get("issue", "") for c in cases(h, "EXECUTION_ANOMALY")))
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM pending_commissions")[0]["n"], 1)
        self.assertFixed(h.balance("COMMISSION", "session:A"), "1.00", "the broker's USD report is booked once")
        self.assertSound(h)

    def test_f17_malformed_commission_is_refused_at_deferral(self):
        """Finding 17: a malformed commission is an anomaly when it arrives; nothing is deferred, and the later
        execution books normally."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        r = h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": "sim:SIM:E1", "amount": "1,00",
                                                    "currency": "USD"})
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body.get("anomaly"), "malformed commission report")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM pending_commissions")[0]["n"], 0)
        h.broker.fill("B1", F("2"), F("10.00"))
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "FILLED")
        self.assertSound(h)

    def test_f38_redelivery_of_an_execution_with_an_unmapped_instrument_is_a_duplicate(self):
        """Finding 38: raw_economic is captured as received, so an identical redelivery whose instrument is
        unmapped (instrument_id None) is a duplicate, not a critical content conflict."""
        h = self.harness()
        running(h)
        buy_acked(h, "B1", "2", "10.00")
        row = h.broker.fill("B1", F("2"), F("10.00"))
        h.broker.drain_events()   # deliver our own crafted copies instead
        payload = {**execution_payload(ACCOUNT, row), "instrument_id": None}
        first = h.desk.run_system("broker_execution", payload)
        second = h.desk.run_system("broker_execution", payload)
        self.assertTrue(first.accepted and not first.body.get("duplicate"), first.body)
        self.assertTrue(second.body.get("duplicate"), second.body)
        mapped = h.desk.run_system("broker_execution", execution_payload(ACCOUNT, row))
        self.assertTrue(mapped.body.get("duplicate"), mapped.body)
        self.assertFalse([c for c in cases(h, "EXECUTION_ANOMALY") if "changed content" in c["detail"].get("issue", "")])
        self.assertEqual(len(executions(h, "B1")), 1)
        self.assertSound(h)

    def test_f26_external_buy_and_its_commission_are_covered_from_outside(self):
        """Finding 26: a manual buy and its commission are funded from 'outside' (unauthorized cash), so no
        system owner claims cash the broker does not hold."""
        h = self.harness(initial_cash="400.00")
        running(h)
        h.broker.external_trade(INST, "BUY", F("2"), F("10.00"), commission=F("1.00"))
        h.pump()
        self.assertFixed(h.available("external"), "0")
        self.assertFixed(h.available("outside"), "79.00")   # 100 - 20.00 - 1.00
        self.assertFixed(h.balance("COMMISSION", "external"), "1.00")
        self.assertEqual(cases(h, "OVERCOMMITTED"), [])
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertSound(h)

    def test_f15_late_commission_for_a_corrected_execution_is_booked_once(self):
        """Finding 15: with commissions delayed, the broker corrects B1's price; the original's late report is
        redirected to the replacement once, the replacement's own report is a duplicate, the estimate is
        reversed once and settlement reconciles."""
        h = self.harness()
        running(h)
        ex = bought(h, "B1", "2", "10.00", delay_commission=True)
        self.assertEqual(ex["fee_status"], "ESTIMATED")
        h.broker.correct_execution(ex["exec_key"], F("2"), F("9.90"))
        h.pump()
        self.assertEqual(h.broker.release_commissions(), 2)
        h.pump()
        self.assertFixed(code_total(h, "COMMISSION"), "1.00")
        self.assertFixed(code_total(h, "FEE_ESTIMATE"), "0")
        self.assertFixed(code_total(h, "FEES_ACCRUED"), "0")
        self.assertFixed(h.available("session:A"), "79.20")   # 100 - 19.80 - 1.00
        self.assertEqual(cases(h, "EXECUTION_ANOMALY"), [])
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "session:A"), "79.20")
        self.assertSound(h)

    def test_f15_every_arrival_order_of_correction_and_commissions_books_one_commission(self):
        """Finding 15 (probe): the replacement execution, the original's late commission and the replacement's
        commission arrive in all six orders (including a commission for the corrected execution before the
        replacement itself). Each order books exactly one commission, reverses the estimate once, raises no
        anomaly, reconciles CLEAN and replays exactly."""
        import itertools
        for perm in itertools.permutations(range(3)):
            with self.subTest(arrival=perm):
                h = Harness()
                try:
                    running(h)
                    ex = bought(h, "B1", "2", "10.00", delay_commission=True)
                    h.broker.correct_execution(ex["exec_key"], F("2"), F("9.90"))
                    self.assertEqual(h.broker.release_commissions(), 2)
                    # queue: [replacement execution, original's commission, replacement's commission]
                    self.assertEqual(h.broker.pending_event_count(), 3)
                    h.broker.shuffle_pending_events(list(perm))
                    h.pump()
                    self.assertFixed(code_total(h, "COMMISSION"), "1.00")
                    self.assertFixed(code_total(h, "FEE_ESTIMATE"), "0")
                    self.assertFixed(code_total(h, "FEES_ACCRUED"), "0")
                    self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM pending_commissions")[0]["n"], 0)
                    self.assertEqual(cases(h, "EXECUTION_ANOMALY"), [])
                    self.assertFixed(h.available("session:A"), "79.20")
                    rec = settle_next_day(h)
                    self.assertEqual(rec.body["status"], "CLEAN", rec.body)
                    self.assertSound(h)
                finally:
                    h.close()

    def test_f15_commission_of_the_original_after_the_replacement_is_final(self):
        """Finding 15 (probe): the replacement's report (1.00) is booked first; the original's late report for
        the same amount is a duplicate, and one with a different amount is an anomaly, never a second charge."""
        h = self.harness()
        running(h)
        ex = bought(h, "B1", "2", "10.00", delay_commission=True)
        new = h.broker.correct_execution(ex["exec_key"], F("2"), F("9.90"))
        h.pump()
        r = h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": new.exec_key, "amount": "1.00",
                                                    "currency": "USD"})
        self.assertEqual(r.body.get("status"), "FINAL", r.body)
        r = h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": ex["exec_key"], "amount": "1.00",
                                                    "currency": "USD"})
        self.assertTrue(r.body.get("duplicate"), r.body)
        r = h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": ex["exec_key"], "amount": "1.40",
                                                    "currency": "USD"})
        self.assertIn("anomaly", r.body)
        self.assertFixed(code_total(h, "COMMISSION"), "1.00")
        self.assertFixed(code_total(h, "FEE_ESTIMATE"), "0")
        self.assertSound(h)


# ============================================================ settlement and fee estimates (24, 25, 37)
class SettlementAndFeeTests(_Base):
    def test_f24_commission_refund_after_settlement_relieves_the_payable(self):
        """Finding 24: a BUY's commission is lowered after the trade settled. The adjustment row records that
        it relieves PAYABLE (direction RECEIVE) and its confirmation clears the payable, so available cash
        equals the cash the broker actually holds."""
        h = self.harness()
        running(h)
        ex = bought(h, "B1", "2", "10.00", commission=F("1.00"))
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("PAYABLE", "session:A"), "0")
        r = h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": ex["exec_key"], "amount": "0.50",
                                                    "currency": "USD"})
        self.assertTrue(r.accepted, r.body)
        (adj,) = [json.loads(x["row"]) for x in h.desk.store.query("SELECT row FROM settlements WHERE settlement_id LIKE ?",
                                                                   (f"stl:{ex['exec_key']}:fee:%",))]
        self.assertEqual((adj["direction"], adj["relieves"], adj["status"]), ("RECEIVE", "PAYABLE", "EXPECTED"))
        self.assertFixed(adj["amount"], "0.50")
        h.clock.advance(DAY)
        h.broker.deposit(F("0.50"))   # the broker refunds the commission difference
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("PAYABLE", "session:A"), "0")
        self.assertFixed(h.balance("CASH", "session:A"), "79.50")
        self.assertFixed(h.available("session:A"), "79.50")
        self.assertSound(h)

    def test_f24_sell_commission_increase_after_settlement_relieves_the_receivable(self):
        """Finding 24 (other side): a SELL's commission rises after settlement; the PAY row relieves RECEIVABLE."""
        h = self.harness()
        running(h)
        bought(h, "B1", "2", "10.00", commission=F("1.00"))
        h.quote("9.99", "10.01")
        self.assertTrue(h.intent("S1", "SELL", "2", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("S1", F("2"), F("10.00"), commission=F("1.00"))
        h.pump()
        (sx,) = executions(h, "S1")
        self.assertEqual(settle_next_day(h).body["status"], "CLEAN")
        h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": sx["exec_key"], "amount": "1.25",
                                                "currency": "USD"})
        h.clock.advance(DAY)
        h.broker.withdraw(F("0.25"))
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("RECEIVABLE", "session:A"), "0")
        self.assertFixed(h.balance("CASH", "session:A"), "97.75")   # 100 - 21.00 + 18.75
        self.assertSound(h)

    def test_f25_f37_exit_from_a_fully_invested_session_is_not_overcommitted(self):
        """Findings 25, 37: a session that spent all but 0.90 of its cash sells; the sell's fee estimate accrues
        against the sale's receivable, so available settled cash is unchanged and no OVERCOMMITTED case or
        FAILED_REVIEW follows."""
        h = self.harness()
        running(h)
        h.quote("10.89", "10.91")
        bought(h, "B1", "9", "10.90", commission=F("1.00"))
        self.assertFixed(h.available("session:A"), "0.90")   # 100 - 98.10 - 1.00
        h.quote("10.89", "10.91")
        self.assertTrue(h.intent("S1", "SELL", "9", "10.89", "A").accepted)
        h.pump()
        h.broker.fill("S1", F("9"), F("10.89"), delay_commission=True)
        h.pump()
        (sx,) = executions(h, "S1")
        self.assertEqual(sx["fee_estimate_code"], "RECEIVABLE")
        self.assertFixed(sx["fee_estimate"], "0.9801")   # 1% cap of 98.01 at ledger scale 6
        self.assertFixed(h.available("session:A"), "0.90")
        self.assertEqual(cases(h, "OVERCOMMITTED"), [])
        h.tick()
        self.assertNotEqual(h.session_state("A"), "FAILED_REVIEW")
        h.broker.release_commissions()
        h.pump()
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "session:A"), "97.91")   # 100 - 99.10 + 98.01 - 1.00 (sim default)
        self.assertSound(h)


# ============================================================ corrections (11, 27, 29)
class CorrectionTests(_Base):
    def _completed(self, h: Harness) -> tuple[dict, dict]:
        """Session A: buy 10 @ 9.00, target observed at 9.70, wind-down sold at 9.70, completed (protected:A)."""
        running(h)
        h.quote("8.99", "9.01")
        buy = bought(h, "B1", "10", "9.00")
        h.quote("9.70", "9.72")
        h.tick()
        self.assertEqual(h.session_state("A"), "WINDING_DOWN")
        (wd,) = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM orders WHERE json_extract(row,'$.purpose')='WIND_DOWN'")]
        h.pump()
        h.broker.fill(wd["order_id"], F(wd["quantity"]), F("9.70"))
        h.pump()
        h.tick()
        h.reconcile()
        h.tick()
        self.assertEqual(h.session_state("A"), "COMPLETED_UNSETTLED")
        (sell,) = executions(h, wd["order_id"])
        return buy, sell

    def _assert_session_owner_empty(self, h: Harness) -> None:
        for r in h.desk.store.query("SELECT account_code, amount FROM balances WHERE owner='session:A'"):
            if r["account_code"] in LIQUID + ("INVENTORY",):
                self.assertFixed(r["amount"], "0", f"session:A {r['account_code']}")
        self.assertEqual(open_lots(h, "session:A"), [])

    def test_f11_f27_buy_correction_after_completion_books_both_legs_on_protected(self):
        """Findings 11, 27: a price correction of the buy after completion reverses and rebooks on protected:A
        (never on session:A), reopens no inventory, and the 1.00 gain is a visible delta on protected:A."""
        h = self.harness()
        buy, _ = self._completed(h)
        before = liquid(h, "protected:A")
        self.assertFixed(before, "105.00")
        h.broker.correct_execution(buy["exec_key"], F("10"), F("8.90"))
        h.pump()
        self._assert_session_owner_empty(h)
        self.assertEqual(open_lots(h), [])
        self.assertFixed(liquid(h, "protected:A"), "106.00")
        self.assertFixed(h.balance("INVENTORY", "protected:A", instrument_id=INST), "0")
        self.assertTrue(cases(h, "POST_COMPLETION_ADJUSTMENT"))
        self.assertEqual(h.session_state("A"), "COMPLETED_UNSETTLED")
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "protected:A"), "106.00")
        self.assertSound(h)

    def test_f11_f27_sell_correction_after_completion_books_both_legs_on_protected(self):
        """Findings 11, 27: a price correction of the wind-down sell after completion stays on protected:A."""
        h = self.harness()
        _, sell = self._completed(h)
        h.broker.correct_execution(sell["exec_key"], F("10"), F("9.60"))
        h.pump()
        self._assert_session_owner_empty(h)
        self.assertEqual(open_lots(h), [])
        self.assertFixed(liquid(h, "protected:A"), "104.00")
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "protected:A"), "104.00")
        self.assertSound(h)

    def test_f27_correction_of_a_correction_after_completion(self):
        """Finding 27 (probe): a correction of the corrected buy (two revisions) after completion still nets on
        protected:A, with the original estimate and commission reversed exactly once."""
        h = self.harness()
        buy, _ = self._completed(h)
        r1 = h.broker.correct_execution(buy["exec_key"], F("10"), F("8.90"))
        h.pump()
        h.broker.correct_execution(r1.exec_key, F("10"), F("8.95"))
        h.pump()
        self._assert_session_owner_empty(h)
        self.assertEqual(open_lots(h), [])
        self.assertFixed(liquid(h, "protected:A"), "105.50")
        self.assertFixed(code_total(h, "COMMISSION"), "2.00")   # one buy, one sell
        self.assertFixed(code_total(h, "FEE_ESTIMATE"), "0")
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertSound(h)

    def test_f27_correction_after_close_books_both_legs_on_unallocated(self):
        """Findings 11, 27 (probe): after an owner-ended session CLOSED, a buy price correction reverses and
        rebooks on 'unallocated' (where the closed session's balances went), never on session:A."""
        h = self.harness()
        running(h)
        h.quote("8.99", "9.01")
        buy = bought(h, "B1", "10", "9.00")
        h.quote("8.99", "9.01")
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        (wd,) = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM orders WHERE json_extract(row,'$.purpose')='WIND_DOWN'")]
        h.pump()
        h.broker.fill(wd["order_id"], F(wd["quantity"]), F(wd["limit_price"]))
        h.pump()
        h.tick()
        h.reconcile()
        h.tick()
        self.assertEqual(h.session_state("A"), "CLOSED")
        before = liquid(h, "unallocated")
        h.broker.correct_execution(buy["exec_key"], F("10"), F("8.90"))
        h.pump()
        self._assert_session_owner_empty(h)
        self.assertEqual(open_lots(h), [])
        self.assertEqual(liquid(h, "unallocated"), before + F("1.00"))
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertSound(h)

    def test_f29_correction_of_a_partly_sold_buy_is_booked(self):
        """Finding 29: B1 (4 @ 10.00) is half sold, then corrected to 9.50; the replacement is booked, the sold
        half's cost is restated (realized +1.00) and the open half carries the corrected cost."""
        h = self.harness()
        running(h)
        buy = bought(h, "B1", "4", "10.00")
        h.quote("9.99", "10.01")
        self.assertTrue(h.intent("S1", "SELL", "2", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("S1", F("2"), F("10.00"))
        h.pump()
        h.broker.correct_execution(buy["exec_key"], F("4"), F("9.50"))
        h.pump()
        (lot,) = open_lots(h)
        self.assertEqual((lot["owner"], F(lot["open_qty"])), ("session:A", F("2")))
        self.assertFixed(lot["open_cost"], "19.00")
        self.assertFixed(h.balance("INVENTORY", "session:A", instrument_id=INST), "19.00")
        self.assertFixed(h.balance("REALIZED", "session:A"), "-1.00")   # credit = gain of 2 x (10.00 - 9.50)
        rec = settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertSound(h)

    def test_f29_correction_after_settlement_is_booked_with_compensating_rows(self):
        """Finding 29: a correction of a buy whose settlement was already confirmed is booked: the settled row
        is compensated, the replacement settles, and the next reconciliation is clean."""
        h = self.harness()
        running(h)
        buy = bought(h, "B1", "4", "10.00")
        self.assertEqual(settle_next_day(h).body["status"], "CLEAN")
        self.assertFixed(h.balance("CASH", "session:A"), "59.00")
        h.broker.correct_execution(buy["exec_key"], F("4"), F("9.50"))
        h.pump()
        h.broker.advance_settlement()
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertFixed(h.balance("CASH", "session:A"), "61.00")
        self.assertFixed(h.balance("PAYABLE", "session:A"), "0")
        (lot,) = open_lots(h)
        self.assertFixed(lot["open_cost"], "38.00")
        self.assertSound(h)


# ============================================================ manual sells (30)
class ManualSellTests(_Base):
    def _with_opening_position(self) -> Harness:
        """The account already holds 5 shares (bought by hand before the system existed)."""
        h = self.harness(initial_cash="400.00")
        h.broker.external_trade(INST, "BUY", F("5"), F("10.00"), commission=F("1.00"))
        h.clock.advance(DAY)
        h.broker.advance_settlement()
        return h

    def test_f30_manual_sell_relieves_outside_opening_lots(self):
        """Finding 30: a manual sale of 3 relieves the owner's own opening lots ('outside'); no quarantine
        short nets out a real long, and positions reconcile."""
        h = self._with_opening_position()
        running(h)
        self.assertEqual(ledger_position(h), F("5"))
        h.broker.external_trade(INST, "SELL", F("3"), F("10.00"), commission=F("1.00"))
        h.pump()
        self.assertEqual(open_lots(h, "exception:"), [])
        (lot,) = open_lots(h)
        self.assertEqual((lot["owner"], F(lot["open_qty"])), ("outside", F("2")))
        self.assertFalse([c for c in cases(h, "EXECUTION_ANOMALY") if "sell exceeds" in c["detail"].get("issue", "")])
        rec = h.reconcile()
        self.assertNotIn("POSITION_MISMATCH", [d["kind"] for d in rec.body["differences"]], rec.body)
        self.assertSound(h)

    def test_f30_manual_sale_of_session_shares_is_flagged_not_netted_silently(self):
        """Finding 30: with no outside shares, a manual sale that can only have used session shares opens a
        blocking case instead of letting a quarantine short silently cancel the session's long."""
        h = self.harness()
        running(h)
        bought(h, "B1", "2", "10.00")
        h.broker.external_trade(INST, "SELL", F("2"), F("10.00"), commission=F("1.00"))
        h.pump()
        flagged = [c for c in cases(h, "EXECUTION_ANOMALY") if c["detail"].get("issue") == "manual sale may have used session-held shares"]
        self.assertEqual(len(flagged), 1)
        self.assertTrue(flagged[0]["blocking"])
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
