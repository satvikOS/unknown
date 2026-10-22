"""The red stop when the ledger or the database is damaged.

BR-REF-19: a corrupt projection never blocks the red stop. The stop is in RECORD_DESPITE_INVARIANTS: if its own
(incremental) ledger check finds the damage, the stop is still recorded and a blocking LEDGER_INVARIANT case opens;
damage the stop does not touch is found by the next whole-ledger check (reconciliation, green start).

BR-SES-13: when the database cannot record the stop at all (disk full, I/O error, a locked or corrupt store), the core
service latches the stop in memory: it refuses every exposure-increasing command, its gateway runner never transmits
an entry, it sends best-effort cancels for every open buy straight to the broker, it logs CRITICAL and reports the
latch in its health, it mirrors the latch in an fsync'd file next to the database, and it records the stop (under the
owner's own command id) plus the evidence of what it did as soon as the database accepts writes again. The tests make
the store's write raise.

Every number is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, DEPLOYMENT, Harness  # noqa: E402

from unknown.core.desk import Command  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway.runner import GatewayRunner  # noqa: E402
from unknown.service.core import CoreService  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402

F = Fixed.parse
SEC = 1_000_000_000
OWNER_ROLES = frozenset({"OWNER"})


def cases(h: Harness, kind: str, status: str = "OPEN") -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM exceptions WHERE kind=? AND status=? ORDER BY rowid", (kind, status))]


def events(h: Harness, event_type: str) -> list[dict]:
    return [json.loads(r["payload"]) for r in h.desk.store.query(
        "SELECT payload FROM events WHERE type=? ORDER BY seq", (event_type,))]


def stored(h: Harness, command_id: str) -> dict | None:
    rows = h.desk.store.query("SELECT status, result FROM commands WHERE command_id=?", (command_id,))
    return {"status": rows[0]["status"], "result": json.loads(rows[0]["result"])} if rows else None


def order(h: Harness, oid: str) -> dict:
    return h.row("orders", "order_id", oid)


def hold(h: Harness, oid: str) -> dict:
    (r,) = h.desk.store.query("SELECT * FROM holds WHERE order_id=?", (oid,))
    return dict(r)


def corrupt(h: Harness, sql: str, params: tuple = ()) -> None:
    """Damage a projection outside the core (as a bad restore or a manual edit would)."""
    h.desk.store.conn.execute(sql, params)


@contextmanager
def unwritable(store):
    """Every write to the store raises, as a full disk or an I/O error would; reads still work."""
    @contextmanager
    def write():
        raise sqlite3.OperationalError("disk I/O error (fixture)")
        yield  # pragma: no cover
    store.write = write
    try:
        yield
    finally:
        del store.write


class _Base(unittest.TestCase):
    def harness(self) -> Harness:
        h = Harness()
        self.addCleanup(h.close)
        return h

    def sound(self, h: Harness) -> None:
        self.assertEqual(h.verify(), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])

    def trading(self, h: Harness) -> None:
        """Session A with 2 shares owned (B0), a working buy B1 at the broker, and two intents not yet sent: a buy
        B2 and a sell S1."""
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("B0", "BUY", "2", "9.00", "A").accepted)
        h.pump()
        h.broker.fill("B0", F("2"), F("9.00"))
        h.pump()
        self.assertTrue(h.intent("B1", "BUY", "3", "8.50", "A").accepted)
        h.pump()
        self.assertEqual(order(h, "B1")["state"], "ACKNOWLEDGED")
        self.assertTrue(h.intent("B2", "BUY", "1", "8.60", "A").accepted)
        self.assertTrue(h.intent("S1", "SELL", "1", "9.00", "A").accepted)
        self.assertEqual((order(h, "B2")["state"], order(h, "S1")["state"]), ("RESERVED", "RESERVED"))


class CorruptProjectionStopTests(_Base):
    def test_red_stop_over_a_corrupt_balance_it_touches_is_recorded_and_ledger_invariant_latches(self):
        """BR-REF-19: session:A's CASH projection is damaged outside the core. The owner's red stop cancels the
        unsent buy B2, which releases session:A's cash hold, so the stop's own ledger check meets the damage. The
        stop is recorded anyway (stored ACCEPTED, latch held with its command id, B2 canceled); a blocking critical
        LEDGER_INVARIANT case opens in the same command; green start is refused while it is open."""
        h = self.harness()
        self.trading(h)
        corrupt(h, "UPDATE balances SET amount='-500.00' WHERE account_key=? AND owner='session:A' AND account_code='CASH'",
                (ACCOUNT,))
        r = h.cmd("stop", h.owner, command_id="red.1", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(stored(h, "red.1")["status"], "ACCEPTED")
        ctl = h.control()
        self.assertEqual((ctl["stop_latched"], ctl["latch_command_id"], ctl["latch_reason"]), (True, "red.1", "OWNER_STOP"))
        self.assertEqual(order(h, "B2")["state"], "CANCELED")
        (alarm,) = events(h, "LedgerInvariantViolation")
        self.assertEqual(alarm["info"]["command_kind"], "stop")
        self.assertTrue(any("available cash negative" in p and "session:A" in p for p in alarm["info"]["problems"]))
        (case,) = cases(h, "LEDGER_INVARIANT")
        self.assertEqual((case["blocking"], case["severity"]), (True, "critical"))
        h.quote("8.99", "9.01")
        start = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertEqual(start.rejected_code, "PREFLIGHT_FAILED")
        self.assertIn("BLOCKING_EXCEPTIONS:LEDGER_INVARIANT", start.body["details"]["blockers"])
        self.assertTrue(h.verify(), "the damage stays visible to the full check")

    def test_damage_the_stop_does_not_touch_is_found_by_the_next_full_check(self):
        """BR-REF-19: unallocated's CASH projection is damaged; the red stop does not touch it, so the stop is
        recorded with no alarm (the per-command check is incremental). The next reconciliation runs the whole-ledger
        check, finds it and opens LEDGER_INVARIANT; the stop stays latched throughout."""
        h = self.harness()
        self.trading(h)
        corrupt(h, "UPDATE balances SET amount='201.00' WHERE account_key=? AND owner='unallocated' AND account_code='CASH'",
                (ACCOUNT,))
        r = h.cmd("stop", h.owner, command_id="red.2", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(events(h, "LedgerInvariantViolation"), [])
        self.assertTrue(h.control()["stop_latched"])
        h.reconcile()
        (alarm,) = events(h, "LedgerInvariantViolation")
        self.assertEqual(alarm["info"]["command_kind"], "reconciliation_result")
        self.assertEqual(len(cases(h, "LEDGER_INVARIANT")), 1)
        self.assertEqual(h.control()["latch_command_id"], "red.2")


class MemoryLatchTests(_Base):
    def service(self, h: Harness) -> CoreService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = CoreService(paths=Paths(Path(tmp.name) / "home"), account_key=ACCOUNT, deployment_id=DEPLOYMENT,
                          broker=h.broker, clock=h.clock)
        svc.paths.ensure()
        svc.desk = h.desk
        return svc

    def stop_request(self, h: Harness, command_id: str = "ui.stop.1") -> dict:
        return {"op": "command", "command_id": command_id, "kind": "stop", "payload": {"account_key": ACCOUNT},
                "principal": {"id": "owner", "role": "OWNER", "authenticated_at_ns": h.clock.now_ns()}}

    def intent_request(self, h: Harness, oid: str, side: str, limit: str) -> dict:
        sess = h.row("sessions", "session_id", "A")
        mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])
        return {"op": "command", "command_id": f"ui.intent.{oid}", "kind": "submit_intent",
                "principal": {"id": "owner", "role": "OWNER", "authenticated_at_ns": h.clock.now_ns()},
                "payload": {"account_key": ACCOUNT, "session_id": "A", "order_id": oid, "instrument_id": "FIX-EQ-1",
                            "side": side, "quantity": "1", "limit_price": limit, "policy_hash": mandate["policy_hash"],
                            "deadline_ns": h.clock.now_ns() + 60 * SEC}}

    def latch_in_memory(self, h: Harness, svc: CoreService) -> dict:
        with unwritable(h.desk.store), self.assertLogs("unknown.core", "CRITICAL") as logs:
            reply = svc._handle(self.stop_request(h), OWNER_ROLES)
        self.assertTrue(any("RED STOP NOT RECORDED" in m for m in logs.output), logs.output)
        return reply

    def test_stop_with_an_unwritable_database_latches_in_memory_cancels_buys_and_is_recorded_later(self):
        """BR-SES-13: every write raises. The owner's stop is not recorded, but the service latches it in memory:
        the reply says so (persisted false, the error, the cancels), B1's cancel goes straight to the broker (which
        cancels it), the health query reports the latch, the latch file names the unrecorded command, and a buy is
        refused while a retry to record keeps failing loudly. Once writes work, the stop is recorded under the
        owner's own command id, followed by a SYSTEM stop that records the outage evidence (error and cancels); the
        memory latch and its file are gone, and the ordinary stop path finishes (B2 never sent, B1 CANCELED, S1 sent)."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        reply = self.latch_in_memory(h, svc)
        self.assertTrue(reply["accepted"])
        self.assertFalse(reply["persisted"])
        body = reply["body"]
        self.assertEqual((body["stop_latched"], body["persisted"]), (True, False))
        self.assertIn("disk I/O error", body["error"])
        self.assertEqual([(c["client_order_id"], c["status"]) for c in body["cancels"]], [("B1", "ACCEPTED")])
        self.assertEqual(h.broker.lookup("B1").status.value, "CANCELED", "the cancel reached the broker")
        self.assertIsNone(stored(h, "ui.stop.1"), "nothing was recorded")
        self.assertFalse(h.control()["stop_latched"])
        health = svc._handle({"op": "query", "name": "health"}, OWNER_ROLES)
        pending = health["memory_latch"]["pending"]
        self.assertEqual(pending[0], "ui.stop.1")
        self.assertTrue(pending[1].startswith("sys.memory-latch."), pending)
        preflight = svc._handle({"op": "query", "name": "preflight", "args": {}}, OWNER_ROLES)
        self.assertIn("STOP_LATCHED_IN_MEMORY", preflight["blockers"])
        latch_file = json.loads(svc.latch_path.read_text())
        self.assertEqual([c["command_id"] for c in latch_file["pending"]], pending)
        with unwritable(h.desk.store), self.assertLogs("unknown.core", "CRITICAL") as logs:
            svc._last_latch_alarm = float("-inf")
            # Each command first tries to record the stop (still failing, loudly), then a buy is refused.
            with self.assertRaises(PermissionError):
                svc._handle(self.intent_request(h, "B9", "BUY", "8.50"), OWNER_ROLES)
            self.assertFalse(svc._persist_memory_latch())
        self.assertTrue(any("RED STOP STILL NOT RECORDED" in m for m in logs.output), logs.output)
        self.assertIsNotNone(svc.memory_latch)
        self.assertIsNone(h.row("orders", "order_id", "B9"))

        self.assertTrue(svc._persist_memory_latch())
        self.assertIsNone(svc.memory_latch)
        self.assertFalse(svc.latch_path.exists())
        self.assertEqual(stored(h, "ui.stop.1")["status"], "ACCEPTED")
        ctl = h.control()
        self.assertEqual((ctl["stop_latched"], ctl["latch_command_id"]), (True, "ui.stop.1"))
        (evidence,) = [e["info"]["memory_latch"] for e in events(h, "StopReasserted") if "memory_latch" in e["info"]]
        self.assertEqual(evidence["stands_in_for"], ["ui.stop.1"])
        self.assertIn("disk I/O error", evidence["error"])
        self.assertEqual([c["client_order_id"] for c in evidence["cancels"]], ["B1"])
        self.assertIsNone(svc._handle({"op": "query", "name": "health"}, OWNER_ROLES)["memory_latch"])
        again = svc._handle(self.stop_request(h), OWNER_ROLES)
        self.assertTrue(again["duplicate"], "the owner's retry with the same id returns the recorded result")

        svc.make_runner().pump()
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.assertEqual(order(h, "B2")["state"], "CANCELED")
        self.assertFalse(h.broker.lookup("B2").found, "the entry was never transmitted")
        self.assertTrue(h.broker.lookup("S1").found, "a sell (reduction) still goes out under a stop")
        self.assertEqual(hold(h, "B1")["status"], "RELEASED")
        self.sound(h)

    def test_the_gateway_never_transmits_an_entry_while_the_memory_latch_is_engaged(self):
        """BR-SES-13: the database accepts writes again but the latch is not recorded yet (the runner pumps before
        the persist step). The runner claims B2's outbox row but does not send it: B2 is recorded REJECTED as
        NOT_TRANSMITTED with its hold released, and the broker never sees it; the sell S1 is transmitted. Then the
        stop is recorded."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        self.latch_in_memory(h, svc)
        svc.make_runner().pump_outbox()
        b2 = order(h, "B2")
        self.assertEqual(b2["state"], "REJECTED")
        self.assertIn("NOT_TRANSMITTED", b2["reason"])
        self.assertIn("STOP_LATCHED_IN_MEMORY", b2["reason"])
        self.assertEqual(hold(h, "B2")["status"], "RELEASED")
        self.assertFalse(h.broker.lookup("B2").found)
        self.assertTrue(h.broker.lookup("S1").found)
        self.assertTrue(svc._persist_memory_latch())
        self.assertTrue(h.control()["stop_latched"])
        svc.make_runner().pump()
        self.sound(h)

    def test_the_next_command_records_the_latch_before_it_runs(self):
        """BR-SES-13 'persists the latch as soon as the database is writable again': once writes work, the next
        command the service handles (here the owner canceling S1) first records the stop, then runs."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        self.latch_in_memory(h, svc)
        reply = svc._handle({"op": "command", "command_id": "ui.cancel.S1", "kind": "request_cancel",
                             "payload": {"order_id": "S1"},
                             "principal": {"id": "owner", "role": "OWNER", "authenticated_at_ns": h.clock.now_ns()}},
                            OWNER_ROLES)
        self.assertTrue(reply["accepted"], reply)
        self.assertIsNone(svc.memory_latch)
        stop_seq = h.desk.store.query("SELECT MIN(seq) AS s FROM events WHERE command_id='ui.stop.1'")[0]["s"]
        cancel_seq = h.desk.store.query("SELECT MIN(seq) AS s FROM events WHERE command_id='ui.cancel.S1'")[0]["s"]
        self.assertLess(stop_seq, cancel_seq)
        self.assertEqual(order(h, "S1")["state"], "CANCELED")
        self.sound(h)

    def test_runner_entry_gate_holds_back_buys_only(self):
        """BR-SES-13 (gateway level): a runner whose entry gate is closed sends sells and cancels, never a buy."""
        h = self.harness()
        self.trading(h)
        runner = GatewayRunner(h.desk, h.broker, ACCOUNT, entry_gate=lambda: "STOP_LATCHED_IN_MEMORY:test")
        stats = runner.pump_outbox()
        self.assertEqual((stats.sent, stats.skipped), (1, 1))
        self.assertEqual(order(h, "B2")["state"], "REJECTED")
        self.assertEqual(order(h, "S1")["state"], "ACKNOWLEDGED")
        self.sound(h)

    def test_a_system_stop_after_repeated_cycle_failures_also_latches_in_memory(self):
        """BR-SES-13: the stop the loop raises after repeated cycle failures takes the same path."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        with unwritable(h.desk.store), self.assertLogs("unknown.core", "CRITICAL"):
            svc._system_stop("CORE_CYCLE_FAILURES:3")
        self.assertIsNotNone(svc.memory_latch)
        self.assertEqual(h.broker.lookup("B1").status.value, "CANCELED")
        self.assertTrue(svc._persist_memory_latch())
        ctl = h.control()
        self.assertEqual((ctl["stop_latched"], ctl["latch_reason"]), (True, "CORE_CYCLE_FAILURES:3"))
        self.sound(h)

    def test_the_latch_file_carries_an_unrecorded_stop_across_a_restart(self):
        """BR-SES-13: the process dies before the database recovers. A new service on the same runtime root reads
        the fsync'd latch file, starts latched in memory, and records the owner's stop under its own id."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        self.latch_in_memory(h, svc)
        again = CoreService(paths=svc.paths, account_key=ACCOUNT, deployment_id=DEPLOYMENT, broker=h.broker, clock=h.clock)
        again.desk = h.desk
        with self.assertLogs("unknown.core", "CRITICAL"):
            again._load_latch_file()
        self.assertIsNotNone(again.memory_latch)
        self.assertIsNotNone(again._entry_gate())
        self.assertTrue(again._persist_memory_latch())
        self.assertEqual(h.control()["latch_command_id"], "ui.stop.1")
        self.assertFalse(again.latch_path.exists())
        self.sound(h)

    def test_a_corrupt_control_row_cannot_block_the_red_stop(self):
        """BR-REF-19 with BR-SES-13: the control projection itself is unreadable, so the desk cannot run the stop
        (it rolls back, nothing recorded). The service still latches in memory and cancels the working buy; the stop
        is recorded once the row is repaired."""
        h = self.harness()
        self.trading(h)
        svc = self.service(h)
        good = h.desk.store.query("SELECT row FROM control WHERE account_key=?", (ACCOUNT,))[0]["row"]
        corrupt(h, "UPDATE control SET row='{damaged' WHERE account_key=?", (ACCOUNT,))
        with self.assertRaises(Exception):
            h.desk.execute(Command("desk.stop", "stop", h.owner, {"account_key": ACCOUNT}))
        with self.assertLogs("unknown.core", "CRITICAL"):
            reply = svc._handle(self.stop_request(h), OWNER_ROLES)
        self.assertFalse(reply["persisted"])
        self.assertEqual(h.broker.lookup("B1").status.value, "CANCELED")
        with self.assertLogs("unknown.core", "CRITICAL"):
            svc._last_latch_alarm = float("-inf")
            self.assertFalse(svc._persist_memory_latch(), "still unreadable: stays latched in memory")
        corrupt(h, "UPDATE control SET row=? WHERE account_key=?", (good, ACCOUNT))
        self.assertTrue(svc._persist_memory_latch())
        self.assertEqual(h.control()["latch_command_id"], "ui.stop.1")
        svc.make_runner().pump()
        self.assertEqual(order(h, "B1")["state"], "CANCELED")
        self.sound(h)


if __name__ == "__main__":
    unittest.main()
