"""Book-review finding BR-ORD-20: the durable raw broker-event journal (gateway/journal.py, gateway/runner.py).

Every broker event the gateway takes from an adapter is appended to ``<UNKNOWN_HOME>/data/<account>.events``
(SQLite, fsync'd, append-only, hash-chained) BEFORE it is applied; each applied entry is marked; a new process
applies the unapplied entries in journal order before any new event; and a re-application is a duplicate, never
a second economic effect. If the journal cannot be written, the gateway stops taking events, latches the stop
through the system ``stop`` command and records an INCOMPLETE reconciliation (``needs_reconciliation``).

A "crash" here is ``Crash``, a BaseException raised at the named point: nothing after it runs. The restart then
drops what a process death drops (the adapter's queued callbacks, the runner's in-memory backlog, the journal
connection) and opens a new core process on the same files.

Every number is a TEST FIXTURE. The simulated broker is a deterministic fixture (BRK-05): these tests say
nothing about IBKR's callback behaviour or about a real disk filling up (the disk-full case is SQLite's own
SQLITE_FULL, produced with ``PRAGMA max_page_count``).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import dataclass as _dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, T0, Harness  # noqa: E402
from test_replay_randomized import Scenario  # noqa: E402

from unknown.broker.simulated import SimulatedBroker  # noqa: E402
from unknown.core.replay import replay_and_compare, table_dump  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.clock import ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway import runner as runner_module  # noqa: E402
from unknown.gateway.journal import EventJournal, JournalCorrupt, JournalUnavailable, event_json  # noqa: E402
from unknown.gateway.runner import (JOURNAL_STOP_REASON, attach_journal, detach_journal, event_backlog,  # noqa: E402
                                    journal_of)
from unknown.service import core as core_module  # noqa: E402
from unknown.service.core import CoreService, Intervals  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.store.schema import PROJECTION_TABLES  # noqa: E402

F = Fixed.parse
NS = 1_000_000_000
JID = "j0"


def dataclass_frozen(cls):
    return _dataclass(frozen=True)(cls)


class Crash(BaseException):
    """The process dies here: nothing after it runs, and no ``except Exception`` catches it."""


def quiet(test: unittest.TestCase) -> None:
    """The journal failures below are logged CRITICAL by design."""
    for logger in (runner_module.log, core_module.log):
        patcher = mock.patch.object(logger, "disabled", True)
        patcher.start()
        test.addCleanup(patcher.stop)


class Journaled:
    """A Harness whose broker events go through an event journal next to its core database."""

    def __init__(self, **harness_kw):
        self.h = Harness(**harness_kw)
        self.path = Path(self.h.tmp.name) / f"{ACCOUNT}.events"
        self.journal = self._open()

    def _open(self) -> EventJournal:
        journal = EventJournal(self.path, ACCOUNT, self.h.clock.now_ns, journal_id=JID)
        attach_journal(self.h.broker, journal)
        return journal

    def crash_and_restart(self) -> int:
        """Drop the adapter's queued callbacks, the runner's backlog and the journal connection; open a new core
        process on the same files. The broker's own state (orders, executions) is at the broker and survives.
        Returns how many events were lost from memory."""
        h = self.h
        lost = h.broker.pending_event_count() + len(event_backlog(h.broker))
        h.broker._events.clear()
        event_backlog(h.broker).clear()
        detach_journal(h.broker)
        self.journal.close()
        h.restart()
        self.journal = self._open()
        return lost

    def close(self) -> None:
        detach_journal(self.h.broker)
        self.journal.close()
        self.h.close()


def running_session(h: Harness, session_id: str = "A") -> None:
    h.bootstrap()
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=session_id, seed="150.00",
                    target_terminal_equity="157.50")
    assert r.accepted and r.body["state"] == "RUNNING", r.body


def economic_state(h: Harness) -> dict:
    """What the money path says, keyed by deterministic ids (journal-entry and case ids are not compared)."""
    q = h.desk.store.query

    def rows(table: str, key: str) -> dict:
        return {r[key]: json.loads(r["row"]) for r in q(f"SELECT {key}, row FROM {table}")}

    return {
        "balances": sorted((r["account_code"], r["owner"], r["currency"], r["instrument_id"], r["amount"])
                           for r in q("SELECT * FROM balances") if not F(r["amount"]).is_zero()),
        "orders": {k: (o["state"], o["cum_qty"], o["leaves_qty"], o["fees_estimated"], o["fees_final"], o.get("avg_price"))
                   for k, o in rows("orders", "order_id").items()},
        "executions": {k: (e["status"], e["fee_status"], e["fee_final"], e["quantity"], e["price"])
                       for k, e in rows("executions", "exec_key").items()},
        "lots": {k: (lot["owner"], lot["open_qty"], lot["open_cost"]) for k, lot in rows("lots", "lot_id").items()},
        "holds": {r["hold_id"]: (r["status"], r["amount"]) for r in q("SELECT hold_id, status, amount FROM holds")},
        "settlements": {k: (s["owner"], s["direction"], s["amount"], s["status"])
                        for k, s in rows("settlements", "settlement_id").items()},
        "pending_commissions": sorted(r["exec_key"] for r in q("SELECT exec_key FROM pending_commissions")),
    }


def projections(h: Harness) -> list:
    return [table_dump(h.desk.store.conn, t) for t in PROJECTION_TABLES]


def event_count(h: Harness) -> int:
    return h.desk.store.query("SELECT COUNT(*) AS n FROM events")[0]["n"]


def journal_commands(h: Harness, journal_id: str = JID) -> list[int]:
    """Journal seqs of the entries the core executed, in the order it executed them (commands rowid)."""
    rows = h.desk.store.query("SELECT command_id FROM commands WHERE command_id LIKE ? ORDER BY rowid",
                              (f"sys.bev.{journal_id}.%",))
    return [int(r["command_id"].rsplit(".", 1)[1]) for r in rows]


def fill_page_cap(journal: EventJournal) -> int:
    """Cap the journal file at its current size: the next append that needs a page fails with SQLITE_FULL.
    Returns the previous cap so the test can 'free the disk'."""
    conn = journal._connection()
    previous = conn.execute("PRAGMA max_page_count").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count={pages}")
    return previous


def free_page_cap(journal: EventJournal, previous: int) -> None:
    journal._connection().execute(f"PRAGMA max_page_count={previous}")


def control_run() -> dict:
    """The fill sequence used by the crash tests, applied without any crash (the reference outcome)."""
    j = Journaled()
    try:
        running_session(j.h)
        assert j.h.intent("B1", "BUY", "5", "10.00", "A").accepted
        j.h.pump()
        j.h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        j.h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        j.h.runner.drain_events()
        assert j.h.verify() == []
        return economic_state(j.h)
    finally:
        j.close()


class CrashWindowTest(unittest.TestCase):
    """BR-ORD-20: a crash between taking events from the broker and applying them loses nothing."""

    @classmethod
    def setUpClass(cls):
        cls.reference = control_run()

    def setUp(self):
        self.j = Journaled()
        self.addCleanup(self.j.close)
        h = self.j.h
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        self.before = len(list(self.j.journal.entries()))   # the acknowledgement, applied and marked
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        self.emitted = [event_json(e) for e in h.broker._events]
        self.assertEqual(len(self.emitted), 6)   # execution, commission, status, twice

    def assert_recovered(self) -> None:
        h, journal = self.j.h, self.j.journal
        self.assertEqual(journal.unapplied_count(), 0)
        self.assertEqual(journal.verify(), [])
        self.assertEqual([e.event for e in journal.entries()][self.before:], self.emitted)
        self.assertEqual(journal_commands(h), list(range(1, self.before + 7)))   # each entry once, in order
        self.assertEqual(economic_state(h), self.reference)
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "FILLED")
        self.assertEqual(h.verify(), [])
        self.assertEqual(replay_and_compare(h.db_path, Path(h.tmp.name) / "replay.sqlite"), [])

    def test_crash_between_journal_append_and_apply_loses_nothing(self):
        """The six events are journaled (fsync'd) and taken from the broker; the process dies before the first
        is applied. Nothing was in memory only; the new process applies all six in broker order and the money
        state equals a run without the crash."""
        h = self.j.h
        real = h.desk.run_system

        def dies_on_first_broker_command(kind, payload, *a, **kw):
            if kind.startswith("broker_"):
                raise Crash()
            return real(kind, payload, *a, **kw)

        with mock.patch.object(h.desk, "run_system", dies_on_first_broker_command):
            with self.assertRaises(Crash):
                h.runner.drain_events()
        self.assertEqual(self.j.journal.unapplied_count(), 6)
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 0)
        self.assertEqual(self.j.crash_and_restart(), 0, "events were held only in memory at the crash")
        self.assertEqual(h.runner.replay_journal(), 6)
        self.assert_recovered()

    def test_crash_after_apply_before_mark_is_a_duplicate_for_each_event_kind(self):
        """For an execution, a commission and an order status in turn: the entry's command commits, then the
        process dies before the entry is marked. The new process re-applies it under the same command id; the
        core returns the stored result (a duplicate, no new event), the entry is marked 'duplicate', and the
        money state equals a run without the crash."""
        h = self.j.h
        journal = self.j.journal
        real_mark = journal.mark_applied
        marked = []
        victims = [self.before + 2]   # the commission's entry

        def dies_after_second_apply(entry, outcome):
            if len(marked) == 1:
                raise Crash()   # the second new entry (the commission) is applied but never marked
            marked.append(entry.seq)
            return real_mark(entry, outcome)

        with mock.patch.object(journal, "mark_applied", dies_after_second_apply):
            with self.assertRaises(Crash):
                h.runner.drain_events()
        commission_seq = self.before + 2
        self.assertEqual(journal.unapplied()[0].seq, commission_seq)
        self.assertEqual(h.row("executions", "exec_key", "sim:SIM:E1")["fee_status"], "FINAL")   # applied...
        finalized = h.desk.store.query("SELECT COUNT(*) AS n FROM events WHERE type='CommissionFinalized'")[0]["n"]
        self.j.crash_and_restart()
        h = self.j.h
        # Crash after the execution's and the status's commands in later rounds of the same drain.
        for victim in ("ExecutionEvent", "OrderStatusEvent"):
            with self.subTest(victim=victim):
                pending = self.j.journal.unapplied()
                target = next(e for e in pending if e.event_type == victim)
                real = self.j.journal.mark_applied

                def dies_on(entry, outcome, target=target, real=real):
                    if entry.seq == target.seq:
                        raise Crash()
                    return real(entry, outcome)

                victims.append(target.seq)

                with mock.patch.object(self.j.journal, "mark_applied", dies_on):
                    with self.assertRaises(Crash):
                        h.runner.replay_journal()
                self.j.crash_and_restart()
                h = self.j.h
                stored = h.desk.store.query("SELECT 1 FROM commands WHERE command_id=?", (target.command_id,))
                self.assertTrue(stored, "the entry's command committed before the crash")
                before = event_count(h)
                again = h.runner.apply_entry(target)
                self.assertTrue(again.duplicate)
                self.assertEqual(event_count(h), before, "a re-application records no new event")
        left = len(self.j.journal.unapplied())   # the status entry: applied, not marked
        self.assertEqual(left, 1)
        self.assertEqual(h.runner.replay_journal(), left)
        marks = self.j.journal.marks()
        self.assertEqual([marks[seq]["outcome"] for seq in victims], ["duplicate"] * 3)
        self.assertEqual(sum(1 for m in marks.values() if m["outcome"] == "duplicate"), 3)
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM events WHERE type='CommissionFinalized'")[0]["n"],
                         finalized + 1, "the second fill's commission is finalized once; the first is not again")
        self.assert_recovered()

    def test_crash_in_the_middle_of_a_core_command_reapplies_it_once(self):
        """The process dies inside the core transaction of the third entry (rolled back): the first two are
        applied and marked, the third and later are applied by the next process, each once."""
        h = self.j.h
        real_new, real_write = h.desk._execute_new, h.desk.store.write
        seen = {"n": 0}

        @contextmanager
        def write_then_die():
            with real_write() as tx:
                yield tx            # the handler runs and the command row is inserted...
                raise Crash()       # ...and the process dies before COMMIT (Store.write rolls back)

        def third_dies(command, payload_hash):
            if command.kind.startswith("broker_"):
                seen["n"] += 1
                if seen["n"] == 3:
                    with mock.patch.object(h.desk.store, "write", write_then_die):
                        return real_new(command, payload_hash)
            return real_new(command, payload_hash)

        with mock.patch.object(h.desk, "_execute_new", third_dies):
            with self.assertRaises(Crash):
                h.runner.drain_events()
        third = self.j.journal.unapplied()[0]
        self.assertFalse(h.desk.store.query("SELECT 1 FROM commands WHERE command_id=?", (third.command_id,)))
        self.assertEqual(len(self.j.journal.marks()), self.before + 2)
        self.j.crash_and_restart()
        self.assertEqual(self.j.h.runner.replay_journal(), 4)
        self.assert_recovered()


class ReplayIdempotencyTest(unittest.TestCase):
    """BR-ORD-20, ADR-0005 #5: a journal entry applied twice is a duplicate, never a second economic effect --
    under its own command id (the journal path) and under a fresh command id (the handlers' scoped broker
    keys alone), for executions, commissions and order statuses."""

    def setUp(self):
        self.j = Journaled()
        self.addCleanup(self.j.close)
        running_session(self.j.h)

    def reapply_everything(self, label: str) -> None:
        h, journal = self.j.h, self.j.journal
        entries = list(journal.entries())
        self.assertEqual(journal.unapplied_count(), 0)
        money, snap, events = economic_state(h), projections(h), event_count(h)
        for e in entries:   # 1. under the entry's own command id: the stored result, no new event
            r = h.runner.apply_entry(e)
            self.assertTrue(r.duplicate, f"{label}: entry {e.seq} ({e.event_type})")
        self.assertEqual(event_count(h), events, f"{label}: journal re-application recorded events")
        self.assertEqual(projections(h), snap, f"{label}: journal re-application changed a projection")
        for e in entries:   # 2. under fresh command ids: the handlers' broker keys make it a duplicate
            r = h.desk.run_system(e.command_kind, e.command_payload)
            self.assertTrue(r.accepted, f"{label}: entry {e.seq} {r.body}")
        self.assertEqual(economic_state(h), money, f"{label}: a fresh-id re-application changed money state")
        self.assertEqual(projections(h), snap, f"{label}: a fresh-id re-application changed a projection")
        self.assertEqual(h.verify(), [], label)

    def test_executions_commissions_and_statuses_reapplied_have_no_second_effect(self):
        """Through an acknowledgement, a partial fill, a fill whose commission is delayed (then released), a
        duplicated execution callback, a deferred cancel and its confirmation, and a broker correction, every
        journal entry so far is re-applied after each stage: the books, holds, orders, executions and
        settlements do not move. Fees are charged once: COMMISSION over all owners equals what the broker
        reported."""
        h = self.j.h
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        self.reapply_everything("acknowledged")
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"), duplicate_callbacks=1)
        h.pump()
        self.reapply_everything("partially filled, duplicated callback")
        h.broker.fill("B1", F("3"), F("9.99"), commission=F("1.20"), delay_commission=True)
        h.pump()
        self.reapply_everything("filled, commission delayed")
        h.broker.release_commissions()
        h.pump()
        self.reapply_everything("commission released")
        h.broker.correct_execution("sim:SIM:E2", F("3"), F("9.98"))
        h.pump()
        self.reapply_everything("execution corrected")
        self.assertTrue(h.intent("B2", "BUY", "4", "10.00", "A").accepted)
        h.pump()
        h.broker.defer_cancel_confirmation(True)
        self.assertTrue(h.owner_cmd("request_cancel", order_id="B2", reason="test").accepted)
        h.pump()
        self.reapply_everything("cancel pending")
        h.broker.confirm_cancel("B2")
        h.pump()
        self.reapply_everything("canceled")
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "FILLED")
        self.assertEqual(h.row("orders", "order_id", "B2")["state"], "CANCELED")
        from unknown.core import ledger
        with h.desk.store.read() as tx:
            commission = ledger.code_total(tx, ACCOUNT, ledger.COMMISSION, "USD")
        self.assertEqual(commission, F("2.20"), "1.00 + 1.20, each once (the correction is not charged again)")
        kinds = {e.event_type for e in self.j.journal.entries()}
        self.assertTrue({"ExecutionEvent", "CommissionEvent", "OrderStatusEvent"} <= kinds, kinds)

    def test_early_status_and_commission_reapplied_before_their_execution(self):
        """A FILLED status and a commission that overtake their execution (out-of-order callbacks) are
        re-applied while the core still waits for the execution: under their own ids nothing is recorded;
        under fresh ids the core records its waiting state again (TerminalStatusAwaitingExecutions,
        CommissionDeferred) without changing any projection. The execution then books the fill and its fee
        once."""
        h = self.j.h
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("5"), F("10.00"), commission=F("1.00"))
        kinds = [type(e).__name__ for e in h.broker._events]
        self.assertEqual(kinds, ["ExecutionEvent", "CommissionEvent", "OrderStatusEvent"])
        h.broker.shuffle_pending_events([2, 1, 0])   # status, commission, then the execution
        h.runner.journal_broker_events()
        status, commission, execution = self.j.journal.unapplied()
        for early in (status, commission):
            h.runner.apply_entry(early)
            self.j.journal.mark_applied(early, "applied")
            snap, events = projections(h), event_count(h)
            self.assertTrue(h.runner.apply_entry(early).duplicate)
            self.assertEqual(event_count(h), events)
            h.desk.run_system(early.command_kind, early.command_payload)
            self.assertEqual(projections(h), snap, f"{early.event_type} re-applied under a fresh id")
            self.assertEqual(h.verify(), [])
        self.assertEqual(h.runner.drain_events(), 1)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["state"], o["cum_qty"], o["fees_final"]), ("FILLED", "5", "1.000000"))
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM pending_commissions")[0]["n"], 0)
        self.assertEqual(h.verify(), [])


class DiskFullTest(unittest.TestCase):
    """BR-ORD-20 bound: when the journal cannot be written, stop taking events, report, require reconciliation
    and latch the stop through the system stop command; recover in order once it can be written again."""

    def setUp(self):
        quiet(self)
        self.j = Journaled()
        self.addCleanup(self.j.close)
        running_session(self.j.h)

    def count(self, sql: str) -> int:
        return self.j.h.desk.store.query(sql)[0]["n"]

    def test_disk_full_on_append_fails_closed_and_recovers_in_order(self):
        """SQLITE_FULL on the append: drain raises JournalUnavailable; the eleven events stay in the in-memory
        backlog in broker order (none applied, none lost); the stop is latched with EVENT_JOURNAL_UNWRITABLE
        (B1's cancel is queued by the stop path); an INCOMPLETE reconciliation names the error and sets
        needs_reconciliation; a second failing drain records nothing new; a reconciliation while the journal
        is full is INCOMPLETE, not CLEAN; the owner cannot start. After space returns, one drain journals the
        eleven events in order and applies them; the stop stays latched until a clean reconciliation and an owner
        start."""
        h, journal = self.j.h, self.j.journal
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        applied_before = len(journal.marks())
        previous = fill_page_cap(journal)
        # Eight duplicate execution callbacks make the batch larger than one page, so it cannot fit.
        h.broker.fill("B1", F("5"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        emitted = [event_json(e) for e in h.broker._events]
        self.assertEqual(len(emitted), 11)
        with self.assertRaises(JournalUnavailable) as caught:
            h.runner.drain_events()
        self.assertIn("database or disk is full", str(caught.exception))
        self.assertEqual([event_json(e) for e in event_backlog(h.broker)], emitted)   # kept, in order
        self.assertEqual(h.broker.pending_event_count(), 0)
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM executions"), 0)
        self.assertEqual(len(list(journal.entries())), applied_before)
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"])
        self.assertTrue(ctl["latch_reason"].startswith(JOURNAL_STOP_REASON), ctl["latch_reason"])
        self.assertIn("database or disk is full", ctl["latch_reason"])
        self.assertTrue(ctl["needs_reconciliation"])
        self.assertEqual(h.row("orders", "order_id", "B1")["cancel_state"], "REQUESTED")   # the stop path ran
        (latched,) = h.desk.store.query("SELECT payload FROM events WHERE type='StopLatched' ORDER BY seq DESC LIMIT 1")
        self.assertIn("database or disk is full", json.loads(latched["payload"])["info"]["reason"])
        runs = h.desk.store.query("SELECT status FROM reconciliations ORDER BY rowid")
        self.assertEqual([r["status"] for r in runs], ["CLEAN", "INCOMPLETE"])
        self.assertTrue([e for e in h.open_exceptions() if e["kind"] == "RECONCILIATION_INCOMPLETE"])
        events, runs = event_count(h), self.count("SELECT COUNT(*) AS n FROM reconciliations")
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        self.assertEqual((event_count(h), self.count("SELECT COUNT(*) AS n FROM reconciliations")), (events, runs),
                         "the same failure is recorded once")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "INCOMPLETE", rec.body)
        h.quote("9.99", "10.01")
        start = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertFalse(start.accepted)
        self.assertIn("RECONCILIATION_REQUIRED", start.body["details"]["blockers"])
        self.assertEqual(h.verify(), [])

        free_page_cap(journal, previous)
        h.pump()   # the queued cancel reaches the broker ("too late": B1 is filled there), then the events
        self.assertIsNone(journal.failure)
        self.assertEqual(len(event_backlog(h.broker)), 0)
        self.assertEqual([e.event for e in journal.entries()][applied_before:applied_before + 11], emitted)
        self.assertEqual(journal.unapplied_count(), 0)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["state"], o["cum_qty"], o["fees_final"]), ("FILLED", "5", "1.000000"))
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM executions"), 1, "nine callbacks, one execution")
        self.assertTrue(h.control()["stop_latched"])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.quote("9.99", "10.01")
        start = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(start.accepted, start.body)
        self.assertEqual(journal.verify(), [])
        self.assertEqual(journal_commands(h), [e.seq for e in journal.entries()])
        self.assertEqual(h.verify(), [])
        self.assertEqual(replay_and_compare(h.db_path, Path(h.tmp.name) / "replay.sqlite"), [])

    def test_a_failure_after_the_owner_restarted_latches_again(self):
        """The journal is full while nothing is pending: drains succeed and a clean reconciliation lets the
        owner start. The next event cannot be journaled: the stop is latched again and reconciliation is
        required again, even though the cause is the same as before."""
        h, journal = self.j.h, self.j.journal
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        previous = fill_page_cap(journal)
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        free_page_cap(journal, previous)
        h.pump()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.quote("9.99", "10.01")
        self.assertTrue(h.owner_cmd("start", account_key=ACCOUNT).accepted)
        self.assertTrue(h.intent("B2", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        fill_page_cap(journal)
        h.broker.fill("B2", F("3"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        with self.assertRaises(JournalUnavailable):
            h.pump()
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        self.assertTrue(ctl["latch_reason"].startswith(JOURNAL_STOP_REASON), ctl["latch_reason"])
        self.assertEqual(len(event_backlog(h.broker)), 11)
        free_page_cap(journal, previous)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B2")["cum_qty"], "3")
        self.assertEqual(h.verify(), [])

    def test_a_corrupt_entry_is_never_applied(self):
        """An unapplied entry whose stored payload no longer matches its checksum (edited outside the journal):
        the reopened journal refuses to apply it or anything after it, the drain fails closed (stop latched,
        reconciliation required), and the core never sees the altered quantity."""
        h, journal = self.j.h, self.j.journal
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("5"), F("10.00"), commission=F("1.00"))
        h.runner.journal_broker_events()
        target = journal.unapplied()[0]
        self.assertEqual(target.event_type, "ExecutionEvent")
        journal.close()
        conn = sqlite3.connect(journal.path)
        conn.execute("DROP TRIGGER entries_no_update")   # what an outside edit or bit rot does not need to ask
        conn.execute("UPDATE entries SET command_payload=replace(command_payload, '\"quantity\":\"5\"', "
                     "'\"quantity\":\"50\"') WHERE seq=?", (target.seq,))
        conn.commit()
        conn.close()
        with self.assertRaises(JournalCorrupt):
            journal.unapplied()
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 0)
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        self.assertIn("fails its checksum", journal.failure)
        self.assertEqual(h.verify(), [])

    def test_entries_and_marks_are_append_only(self):
        """The journal's own triggers refuse to rewrite or delete an entry or a mark."""
        h, journal = self.j.h, self.j.journal
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        conn = journal._connection()
        for sql in ("UPDATE entries SET event='{}'", "DELETE FROM entries", "UPDATE applied SET outcome='x'",
                    "DELETE FROM applied"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
        self.assertEqual(journal.verify(), [])

    def test_a_journal_of_another_account_is_refused(self):
        """A journal file belongs to one account: opening it for another is unavailable, never applied."""
        other = EventJournal(self.j.path, "SIM-2", self.j.h.clock.now_ns)
        self.j.journal.unapplied()   # make sure the file exists
        with self.assertRaises(JournalUnavailable):
            other.unapplied()


class OrderingTest(unittest.TestCase):
    """BR-ORD-20: broker order survives apply failures, a full journal and crashes."""

    def test_events_are_applied_once_each_in_broker_order(self):
        """Two orders, partial and duplicated fills, a delayed commission, shuffled callbacks and the stop
        path's cancels, interleaved with: an execution command that raises twice (head-of-line: nothing after it is
        applied meanwhile), a full journal (events wait in memory, in order), and crashes before an apply and
        before a mark. The journal holds exactly the events the broker handed over, in that order; the core
        executed every entry exactly once, in journal order; and the books match the broker."""
        quiet(self)
        j = Journaled()
        self.addCleanup(lambda: j.close())
        h = j.h
        running_session(h)
        handed: list[str] = []
        real_drain = h.broker.drain_events

        def recording_drain(limit: int = 1000):
            out = real_drain(limit)
            handed.extend(event_json(e) for e in out)
            return out

        h.broker.drain_events = recording_drain
        first = len(list(j.journal.entries()))
        self.assertTrue(h.intent("B1", "BUY", "6", "10.00", "A").accepted)
        self.assertTrue(h.intent("B2", "BUY", "4", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"), duplicate_callbacks=2)
        h.broker.fill("B2", F("1"), F("9.99"), commission=F("1.00"), delay_commission=True)
        n = h.broker.pending_event_count()
        h.broker.shuffle_pending_events(list(reversed(range(n))))
        # 1. An execution command fails twice: it and everything after it wait at the head of the journal.
        real = h.desk.run_system
        failures = {"left": 2}

        def flaky(kind, payload, *a, **kw):
            if kind == "broker_execution" and failures["left"]:
                failures["left"] -= 1
                raise RuntimeError("transient failure (fixture)")
            return real(kind, payload, *a, **kw)

        with mock.patch.object(h.desk, "run_system", flaky):
            with self.assertRaises(RuntimeError):
                h.runner.drain_events()
            head, marked = j.journal.unapplied()[0], len(j.journal.marks())
            self.assertEqual(head.event_type, "ExecutionEvent")
            with self.assertRaises(RuntimeError):
                h.runner.drain_events()
            self.assertEqual((j.journal.unapplied()[0].seq, len(j.journal.marks())), (head.seq, marked),
                             "nothing behind a failing entry is applied")
            h.runner.drain_events()
        # 2. A full journal: new events wait in memory, in order, and are journaled after the space returns.
        previous = fill_page_cap(j.journal)
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        h.broker.release_commissions()
        with self.assertRaises(JournalUnavailable):
            h.pump()
        free_page_cap(j.journal, previous)
        h.runner.journal_broker_events()
        # 3. A crash before an apply, then a crash after an apply and before its mark.
        pending = j.journal.unapplied_count()
        self.assertGreater(pending, 4)
        applied = {"n": 0}
        real_now = h.desk.run_system

        def dies_on_third(kind, payload, *a, **kw):
            if kind.startswith("broker_"):
                applied["n"] += 1
                if applied["n"] == 3:
                    raise Crash()
            return real_now(kind, payload, *a, **kw)

        with mock.patch.object(h.desk, "run_system", dies_on_third):
            with self.assertRaises(Crash):
                h.runner.drain_events()
        self.assertEqual(j.crash_and_restart(), 0)
        h = j.h
        h.broker.drain_events = recording_drain
        marks = {"n": 0}
        real_mark = j.journal.mark_applied

        def dies_before_second_mark(entry, outcome):
            marks["n"] += 1
            if marks["n"] == 2:
                raise Crash()
            return real_mark(entry, outcome)

        with mock.patch.object(j.journal, "mark_applied", dies_before_second_mark):
            with self.assertRaises(Crash):
                h.runner.replay_journal()
        self.assertEqual(j.crash_and_restart(), 0)
        h = j.h
        h.broker.drain_events = recording_drain
        h.runner.replay_journal()
        h.pump()   # the stop path's cancels of B1 and B2 (queued while the journal was full) reach the broker

        entries = list(j.journal.entries())
        self.assertEqual([e.event for e in entries][first:], handed, "the journal is the broker's hand-over, in order")
        self.assertEqual(journal_commands(h), [e.seq for e in entries], "each entry executed once, in order")
        self.assertEqual(j.journal.unapplied_count(), 0)
        self.assertEqual(j.journal.verify(), [])
        self.assertEqual(sum(1 for m in j.journal.marks().values() if m["outcome"] == "duplicate"), 1)
        b1, b2 = h.row("orders", "order_id", "B1"), h.row("orders", "order_id", "B2")
        self.assertEqual((b1["state"], b1["cum_qty"]), ("CANCELED", "5"))
        self.assertEqual((b2["state"], b2["cum_qty"]), ("CANCELED", "1"))
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.verify(), [])
        self.assertEqual(replay_and_compare(h.db_path, Path(h.tmp.name) / "replay.sqlite"), [])


class JournalScenario(Scenario):
    """The seeded randomized desk scenario (test_replay_randomized) with every broker event going through the
    journal, plus three steps: a crash after events were journaled and before the k-th is applied, a crash
    after the k-th is applied and before it is marked, and a full journal while the broker keeps sending.
    Each crash opens a new core process and a new journal connection and replays before anything else."""

    WEIGHTS = Scenario.WEIGHTS + (("crash_before_apply", 4), ("crash_before_mark", 4), ("journal_full", 3))

    def __init__(self, test: unittest.TestCase, seed: int, steps: int):
        super().__init__(test, seed, steps)
        self.journal_id = f"rs{seed}"
        self.path = Path(self.h.tmp.name) / f"{ACCOUNT}.events"
        self.journal = self._open_journal()

    def _open_journal(self) -> EventJournal:
        journal = EventJournal(self.path, ACCOUNT, self.h.clock.now_ns, journal_id=self.journal_id)
        attach_journal(self.h.broker, journal)
        return journal

    def run(self) -> None:
        try:
            super().run()
        finally:
            detach_journal(self.h.broker)
            self.journal.close()

    def new_process(self, why: str) -> None:
        h = self.h
        in_memory = h.broker.pending_event_count() + len(event_backlog(h.broker))
        if why != "restart" and in_memory:
            self.fail(f"crash {why} with {in_memory} events not journaled (this test crashes only after intake)")
        detach_journal(h.broker)
        self.journal.close()
        h.restart()
        self.journal = self._open_journal()
        replayed = h.runner.replay_journal()
        self.report_connectivity()
        if self.rng.random() < 0.85:
            self.requote()
        self.note(f"{why} -> epoch {h.desk.store.epoch}, replayed {replayed} journal entries")
        self.count("restart" if why == "restart" else "crash")
        if replayed:
            self.count("crash_replayed")
        self.check_invariants(why)
        self.check_replay(why)
        if self.rng.random() < 0.75:
            self.step_start(procedure=True)

    def step_restart(self) -> None:
        self.new_process("restart")

    def _crash_in_drain(self, where: str) -> None:
        h = self.h
        if h.broker.pending_event_count() == 0:
            self.step_fill(pump=False)
        h.runner.journal_broker_events()
        pending = self.journal.unapplied_count()
        if pending == 0:
            return self.step_pump()
        k = self.rng.randint(1, pending)
        calls = {"n": 0}
        if where == "before_apply":
            real = h.desk.run_system

            def dying(kind, payload, *a, **kw):
                if kind.startswith("broker_"):
                    calls["n"] += 1
                    if calls["n"] == k:
                        raise Crash()
                return real(kind, payload, *a, **kw)
            target, attr = h.desk, "run_system"
        else:
            real_mark = self.journal.mark_applied

            def dying(entry, outcome):
                calls["n"] += 1
                if calls["n"] == k:
                    raise Crash()
                return real_mark(entry, outcome)
            target, attr = self.journal, "mark_applied"
        with mock.patch.object(target, attr, dying):
            try:
                h.runner.drain_events()
            except Crash:
                pass
            else:
                self.fail(f"no crash at entry {k} of {pending}")
        self.note(f"crash {where} at entry {k} of {pending}")
        self.count(f"crashed_{where}")
        self.new_process(f"crash {where}")

    def step_crash_before_apply(self) -> None:
        self._crash_in_drain("before_apply")

    def step_crash_before_mark(self) -> None:
        self._crash_in_drain("before_mark")

    def step_journal_full(self) -> None:
        h = self.h
        self.step_pump()
        working = self.broker_working()
        if not working:
            self.step_buy()
            self.step_pump()
            working = self.broker_working()
        previous = fill_page_cap(self.journal)
        try:
            if working:
                row = self.rng.choice(working)
                order = h.row("orders", "order_id", row.client_order_id)
                price = F(order["limit_price"])
                fee = self.commission(Fixed(1))
                h.broker.fill(row.client_order_id, Fixed(1), price, commission=fee, duplicate_callbacks=8)
                self.shadow.record(row.side, Fixed(1), price, fee)
                self.note(f"journal full: fill {row.client_order_id} {row.side} 1 @{price} fee={fee} dup=8")
            try:
                h.pump()
                self.count("journal_full_absorbed")
            except JournalUnavailable:
                self.count("journal_full_failed")
                ctl = h.control()
                if not (ctl["stop_latched"] and ctl["needs_reconciliation"]):
                    self.fail(f"journal full but the desk did not fail closed: {ctl}")
                if self.rng.random() < 0.5:
                    r = self.reconcile("journal full")
                    if r.body.get("status") != "INCOMPLETE":
                        self.fail(f"reconciliation with unjournaled events was {r.body.get('status')}")
        finally:
            free_page_cap(self.journal, previous)
        self.step_pump()

    def quiesce(self) -> None:
        super().quiesce()
        t, journal = self.t, self.journal
        entries = list(journal.entries())
        t.assertEqual(journal.verify(), [], f"seed {self.seed}")
        t.assertEqual(journal.unapplied_count(), 0, f"seed {self.seed}")
        t.assertEqual(sorted(journal.marks()), [e.seq for e in entries], f"seed {self.seed}: every entry marked")
        t.assertEqual(journal_commands(self.h, self.journal_id), [e.seq for e in entries],
                      f"seed {self.seed}: every entry executed exactly once, in journal order")
        duplicates = sum(1 for mark in journal.marks().values() if mark["outcome"] == "duplicate")
        # Each crash before a mark leaves one applied, unmarked entry: its re-application is a duplicate.
        t.assertGreaterEqual(duplicates, self.stats.get("crashed_before_mark", 0), f"seed {self.seed}")


class RandomizedJournalTest(unittest.TestCase):
    def test_seeded_scenarios_with_crashes_and_a_full_journal(self):
        """BR-ORD-20 with LED-01..LED-08, ORD-01..ORD-06, REC-01: 6 seeded 60-step desk scenarios against the
        simulated broker, crashing at random journal boundaries and filling the journal. After every step the
        ledger verifies and holds match their orders; at every crash a replay of the event log reproduces every
        projection; each scenario ends with CLEAN reconciliations, ledger cash and lots equal to the broker's,
        an independent shadow tally equal to ledger and broker to the cent, a sound journal whose every entry
        was executed by the core exactly once in journal order, and a final replay_and_compare."""
        quiet(self)
        totals: dict[str, int] = {}
        for seed in range(101, 107):
            with self.subTest(seed=seed):
                sc = JournalScenario(self, seed, 60)
                sc.run()
                for k, v in sc.stats.items():
                    totals[k] = totals.get(k, 0) + v
        for key in ("crashed_before_apply", "crashed_before_mark", "crash_replayed", "journal_full_failed",
                    "fill_full", "recon_CLEAN", "recon_INCOMPLETE", "restart", "start_granted"):
            self.assertGreater(totals.get(key, 0), 0, f"randomized runs never exercised {key}: {totals}")


class ServiceWiringTest(unittest.TestCase):
    """The core service journals at <UNKNOWN_HOME>/data/<account>.events and, at start, applies what an earlier
    process journaled but did not apply before any new event, request or connectivity sync."""

    def wait_for(self, predicate, what: str, timeout_s: float = 20.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    def test_service_replays_the_journal_before_new_events(self):
        quiet(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = Paths(Path(tmp.name) / "home")
        paths.ensure()
        clock = ManualClock(T0)
        broker = SimulatedBroker(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        path = paths.data / f"{ACCOUNT}.events"
        # An earlier process: it journaled the first fill's events and died before applying them.
        h = Harness(db_path=paths.db(ACCOUNT), clock=clock, broker=broker)
        journal = EventJournal(path, ACCOUNT, clock.now_ns)
        attach_journal(broker, journal)
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.journal_broker_events(), 3)
        journal_id = journal.journal_id
        detach_journal(broker)
        journal.close()
        h.desk.store.release_lease()
        h.close()
        # Events that reach the new process's adapter after it starts.
        broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        svc = CoreService(paths=paths, account_key=ACCOUNT, deployment_id="mac-dev", broker=broker, clock=clock,
                          intervals=Intervals(loop_s=0.005, valuation_s=3600.0, reconcile_s=3600.0,
                                              reconcile_retry_s=3600.0, heartbeat_s=0.0))
        thread = threading.Thread(target=svc.run, name="core-under-test", daemon=True)
        thread.start()
        self.addCleanup(lambda: (svc.shutdown(), thread.join(10)))
        self.assertTrue(svc.ready.wait(20))
        self.assertIs(journal_of(broker), svc.journal)
        db = paths.db(ACCOUNT)

        def read(sql: str, args: tuple = ()) -> list:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(sql, args).fetchall()
            finally:
                conn.close()

        self.wait_for(lambda: read("SELECT COUNT(*) AS n FROM executions")[0]["n"] == 2, "both fills applied")
        health = svc._enqueue({"op": "query", "name": "health"}, frozenset({"OWNER"}))
        self.assertEqual((health["event_journal"]["unapplied"], health["event_journal"]["failure"]), (0, None))
        svc.shutdown()
        thread.join(20)
        self.assertFalse(thread.is_alive())
        commands = [(r["command_id"], r["kind"]) for r in read("SELECT command_id, kind FROM commands ORDER BY rowid")]
        start = max(i for i, (cid, _) in enumerate(commands) if cid.startswith("process."))
        after_start = commands[start + 1:]
        seqs = [int(cid.rsplit(".", 1)[1]) for cid, _ in after_start if cid.startswith(f"sys.bev.{journal_id}.")]
        first_other = next(i for i, (cid, _) in enumerate(after_start) if not cid.startswith("sys.bev."))
        replayed = [cid for cid, _ in after_start[:first_other]]
        self.assertEqual(len(replayed), 3, f"the earlier process's 3 entries come first after the start: {after_start}")
        self.assertEqual(seqs, sorted(seqs))
        reopened = EventJournal(path, ACCOUNT, clock.now_ns)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.unapplied_count(), 0)
        self.assertEqual(reopened.verify(), [])
        self.assertEqual(reopened.journal_id, journal_id)
        self.assertEqual(len(list(reopened.entries())), 1 + 3 + 3)   # acknowledgement, first fill, second fill
        self.assertEqual(replay_and_compare(db, Path(tmp.name) / "replay.sqlite"), [])



class ReviewRegressionTest(unittest.TestCase):
    """Adversarial review of BR-ORD-20 (feat/gateway-event-journal): each test failed on the branch as built."""

    def setUp(self):
        quiet(self)

    # ------------------------------------------------------------------ restore
    def restored(self):
        """A core whose second fill was applied and marked after a backup, restored (RB-10) from that backup.
        Returns the harness on the restored database, a journal reopened on the same file, and the entry count."""
        from unknown.ops.backup import backups_for, restore_backup, take_backup
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = Paths(Path(tmp.name) / "home")
        paths.ensure()
        h = Harness(db_path=paths.db(ACCOUNT))
        self.addCleanup(lambda: h.desk.store.close())
        path = paths.data / f"{ACCOUNT}.events"
        journal = EventJournal(path, ACCOUNT, h.clock.now_ns, journal_id=JID)
        attach_journal(h.broker, journal)
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.runner.drain_events()
        take_backup(paths, ACCOUNT, now_ns=h.clock.now_ns())
        (backup,) = backups_for(paths, ACCOUNT)
        at_backup = len(list(journal.entries()))
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        h.runner.drain_events()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "FILLED")
        after = len(list(journal.entries()))
        self.assertEqual(after - at_backup, 3)
        self.assertEqual(journal.unapplied_count(), 0)
        # The core stops cleanly; the owner restores the backup; a new process starts on the restored file.
        h.desk.store.release_lease()
        h.desk.store.close()
        restore_backup(paths, ACCOUNT, backup, now_ns=h.clock.now_ns())
        detach_journal(h.broker)
        journal.close()
        h.desk = h._open()
        self.assertEqual(h.control()["latch_reason"], "RESTORED_FROM_BACKUP")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 1)
        journal = EventJournal(path, ACCOUNT, h.clock.now_ns, journal_id=JID)
        attach_journal(h.broker, journal)
        self.addCleanup(lambda: (detach_journal(h.broker), journal.close()))
        return h, journal, after

    def assert_restored_core_complete(self, h: Harness, journal: EventJournal, entries: int) -> None:
        self.assertEqual(journal_commands(h), list(range(1, entries + 1)), "each entry executed once in this core")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 2)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        self.assertEqual(journal.unapplied_count(), 0)
        self.assertEqual(h.runner.replay_journal(), 0, "a second start re-applies nothing")
        self.assertEqual(h.verify(), [])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.verify(), [])

    def test_entries_marked_applied_after_a_restored_backup_point_are_applied_to_the_restored_core(self):
        """RB-10 restore (ops.backup.restore_backup) puts an older core database in place and leaves the event
        journal where it is. Entries the old core applied and marked after the backup point are not in the
        restored core: the next process applies them again (in journal order, under their own command ids)
        instead of trusting the marks, so the journaled fills are not lost beyond the broker's history window.
        Entries the restored core already has are not applied again. On the branch nothing was applied."""
        h, journal, entries = self.restored()
        self.assertEqual(h.runner.replay_journal(), 3, "the three entries applied after the backup point")
        self.assert_restored_core_complete(h, journal, entries)

    def test_a_dropped_journal_connection_does_not_cancel_the_reapplication(self):
        """The re-application decided at start survives the journal reopening its connection (an I/O error)
        before the entries are applied: the reopened journal does not take its marks for the truth again."""
        h, journal, entries = self.restored()
        self.assertEqual(h.runner.rewind_to_core(journal), 3)
        journal.close()   # the connection is dropped; the object (and the service's reference) stays
        self.assertEqual(journal.unapplied_count(), 3)
        self.assertEqual(h.runner.drain_events(), 3)
        self.assert_restored_core_complete(h, journal, entries)

    def test_a_fresh_core_never_receives_an_old_journals_history(self):
        """A core database that never applied anything from this journal (a new database next to an old
        journal) shares no history with it: the marks are kept, nothing old is applied."""
        j = Journaled()
        self.addCleanup(j.close)
        running_session(j.h)
        self.assertTrue(j.h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        j.h.pump()
        j.h.broker.fill("B1", F("5"), F("10.00"), commission=F("1.00"))
        j.h.runner.drain_events()
        entries = len(list(j.journal.entries()))
        fresh = Harness()
        self.addCleanup(fresh.close)
        journal = EventJournal(j.path, ACCOUNT, fresh.clock.now_ns, journal_id=JID)
        self.addCleanup(journal.close)
        attach_journal(fresh.broker, journal)
        self.addCleanup(lambda: detach_journal(fresh.broker))
        running_session(fresh)
        self.assertEqual(fresh.runner.replay_journal(), 0)
        self.assertEqual(journal_commands(fresh), [])
        self.assertEqual(len(list(journal.entries())), entries)
        self.assertEqual(fresh.verify(), [])

    # ------------------------------------------------------------------ id conflicts
    def test_a_reapplication_after_a_deployment_id_change_is_a_duplicate_not_a_conflict(self):
        """An entry applied by a core running as one deployment, unmarked at the crash, is re-applied by the next
        process running as another (``--deployment`` names the system principal). Same command id, same
        payload, both SYSTEM: a duplicate. On the branch the core raised CommandConflict (a different principal)
        on every drain, recorded a CommandIdConflict event each time, and nothing behind it was ever applied."""
        from unknown.core.desk import open_desk
        j = Journaled()
        self.addCleanup(j.close)
        h = j.h
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("5"), F("10.00"), commission=F("1.00"))
        real_mark = j.journal.mark_applied

        def dies_before_first_mark(entry, outcome):
            raise Crash()

        with mock.patch.object(j.journal, "mark_applied", dies_before_first_mark):
            with self.assertRaises(Crash):
                h.runner.drain_events()
        del real_mark
        h.broker._events.clear()
        detach_journal(h.broker)
        j.journal.close()
        h.desk.store.close()
        h.desk = open_desk(h.db_path, h.clock, h.ids, deployment_id="renamed-host", takeover=True, quotes=None,
                           health=h.health, calendars={"WEEKDAYS": WEEKDAYS_ONLY})
        j.journal = j._open()
        conflicts = h.desk.store.query("SELECT COUNT(*) AS n FROM events WHERE type='CommandIdConflict'")[0]["n"]
        self.assertEqual(h.runner.replay_journal(), 3)
        self.assertEqual(j.journal.unapplied_count(), 0)
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM events WHERE type='CommandIdConflict'")[0]["n"],
                         conflicts)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["state"], o["cum_qty"], o["fees_final"]), ("FILLED", "5", "1.000000"))
        self.assertEqual(sorted(m["outcome"] for m in j.journal.marks().values()).count("duplicate"), 1)
        self.assertEqual(h.verify(), [])

    def test_a_journal_older_than_the_core_fails_closed_without_flooding_the_event_log(self):
        """The journal file is rolled back to an older copy (a file-level restore of the home directory) while
        the core database keeps later commands of the same journal: the next entry's command id names a
        different broker event in the core. The gateway must not apply it; it fails closed (stop latched,
        reconciliation required) and does not record a CommandIdConflict event on every cycle."""
        j = Journaled()
        self.addCleanup(j.close)
        h = j.h
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        old_copy = Path(h.tmp.name) / "old.events"
        dst = sqlite3.connect(old_copy)
        j.journal._connection().backup(dst)
        dst.close()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.runner.drain_events()
        detach_journal(h.broker)
        j.journal.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{j.path}{suffix}").unlink(missing_ok=True)
        old_copy.replace(j.path)
        h.restart()
        j.journal = j._open()
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        conflicts = h.desk.store.query("SELECT COUNT(*) AS n FROM events WHERE type='CommandIdConflict'")[0]["n"]
        for _ in range(3):
            with self.assertRaises(JournalUnavailable):
                h.runner.drain_events()
        self.assertLessEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM events WHERE type='CommandIdConflict'")[0]["n"],
                             conflicts + 1)
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        self.assertTrue(ctl["latch_reason"].startswith(JOURNAL_STOP_REASON), ctl["latch_reason"])
        self.assertEqual(h.row("orders", "order_id", "B1")["cum_qty"], "2", "the colliding entry was not applied")
        self.assertEqual(h.verify(), [])

    # ------------------------------------------------------------------ corruption
    def test_valid_entries_before_a_corrupt_one_are_applied_and_nothing_is_appended_behind_it(self):
        """The last of three unapplied entries fails its checksum: the two before it are verified against the
        chain and applied (``nothing from this entry on is applied`` -- not nothing at all); the drain fails
        closed; and new broker events are not appended behind the broken entry, where they could never be
        applied, but wait in memory (bounded) for the owner to move the journal aside."""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("5"), F("10.00"), commission=F("1.00"))
        h.runner.journal_broker_events()
        execution, commission, status = journal.unapplied()
        journal.close()
        conn = sqlite3.connect(journal.path)
        conn.execute("DROP TRIGGER entries_no_update")
        conn.execute("UPDATE entries SET command_payload=replace(command_payload, 'FILLED', 'CANCELLED') WHERE seq=?",
                     (status.seq,))
        conn.commit()
        conn.close()
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 1)
        self.assertEqual(h.row("executions", "exec_key", "sim:SIM:E1")["fee_status"], "FINAL")
        self.assertEqual(sorted(journal.marks())[-2:], [execution.seq, commission.seq])
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        entries = len(list(journal.entries()))
        h.broker.disconnect()
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        self.assertEqual(len(list(journal.entries())), entries, "nothing appended behind a broken entry")
        self.assertEqual(len(event_backlog(h.broker)), 1)
        self.assertEqual(h.verify(), [])

    # ------------------------------------------------------------------ intake
    def test_an_unknown_event_type_blocks_only_itself_and_what_follows(self):
        """An event the gateway cannot map to a command is at the head-of-line, as before the journal: but the
        events the broker handed over before it are journaled and applied, and so are entries already in the
        journal. On the branch nothing of the batch was journaled and no journaled entry was applied."""

        @dataclass_frozen
        class StrangeEvent:
            what: str

        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.runner.journal_broker_events()                  # three entries journaled, not applied
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        h.broker._events.insert(2, StrangeEvent("x"))    # execution, commission, ?, status
        with self.assertRaises(TypeError):
            h.runner.drain_events()
        self.assertEqual(journal.unapplied_count(), 0)
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 2)
        self.assertEqual([type(e).__name__ for e in event_backlog(h.broker)], ["StrangeEvent", "OrderStatusEvent"])
        self.assertEqual(h.verify(), [])

    # ------------------------------------------------------------------ episodes
    def test_a_second_failure_with_the_same_cause_is_reported_again_while_the_stop_is_still_latched(self):
        """Two failure episodes with the same cause, separated by drains that succeed and a CLEAN reconciliation,
        while the owner has not cleared the stop: the second is recorded durably too (a StopReasserted naming
        the journal error -- the INCOMPLETE run itself keeps no error text). No test covered the episode reset:
        removing it left every test green."""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()

        def journal_stops() -> list[str]:
            rows = h.desk.store.query("SELECT type, payload FROM events WHERE type IN ('StopLatched','StopReasserted') "
                                      "ORDER BY seq")
            return [r["type"] for r in rows if json.loads(r["payload"])["info"]["reason"].startswith(JOURNAL_STOP_REASON)]

        previous = fill_page_cap(journal)
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        self.assertEqual(journal_stops(), ["StopLatched"])
        free_page_cap(journal, previous)
        h.runner.drain_events()   # the stop's cancel of B1 is still in the outbox: B1 keeps working at the broker
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertTrue(h.control()["stop_latched"])
        self.assertFalse(h.control()["needs_reconciliation"])
        fill_page_cap(journal)
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        self.assertEqual(journal_stops(), ["StopLatched", "StopReasserted"])
        self.assertTrue(h.control()["needs_reconciliation"])
        free_page_cap(journal, previous)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["cum_qty"], "5")
        self.assertEqual(h.verify(), [])

    # ------------------------------------------------------------------ the bound
    def test_while_the_journal_fails_no_further_events_are_taken_from_the_broker(self):
        """The claimed bound: while appends fail, the in-memory backlog does not grow; later broker events stay
        in the adapter. (No test exercised it: removing the condition left every test green.)"""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        previous = fill_page_cap(journal)
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        held = len(event_backlog(h.broker))
        self.assertEqual(held, 11)
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        later = h.broker.pending_event_count()
        self.assertEqual(later, 3)
        with self.assertRaises(JournalUnavailable):
            h.runner.drain_events()
        self.assertEqual((len(event_backlog(h.broker)), h.broker.pending_event_count()), (held, later))
        free_page_cap(journal, previous)
        h.runner.drain_events()
        h.runner.drain_events()
        self.assertEqual(h.row("orders", "order_id", "B1")["cum_qty"], "5")
        self.assertEqual(journal.unapplied_count(), 0)
        self.assertEqual(h.verify(), [])


class SecondReviewTest(unittest.TestCase):
    """Second adversarial review of BR-ORD-20 (feat/gateway-event-journal). Each test failed before its fix, or
    (marked "coverage") was the only test to fail when the code it names was removed."""

    def setUp(self):
        quiet(self)

    def test_a_journal_failure_before_the_outbox_pass_transmits_no_new_buy(self):
        """The pump journals what the broker sent before its outbox pass. When that append fails, the desk fails
        closed BEFORE the pass: a buy queued in the outbox is not transmitted while broker events cannot be made
        durable, and the stop's cancel of the working order goes out in the same pass. Before the fix the failure
        was swallowed until after the pass, so the new buy reached the broker (and was then cancelled)."""
        j = Journaled()
        self.addCleanup(j.close)
        h = j.h
        running_session(h)
        self.assertTrue(h.intent("B0", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B0", F("2"), F("10.00"), commission=F("1.00"), duplicate_callbacks=8)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)   # queued in the outbox, not sent
        previous = fill_page_cap(j.journal)
        with self.assertRaises(JournalUnavailable):
            h.pump()
        self.assertFalse(h.broker.lookup("B1").found, "a new buy reached the broker while the journal was failing")
        self.assertNotIn(h.row("orders", "order_id", "B1")["state"], ("SUBMIT_PENDING", "ACKNOWLEDGED"))
        self.assertEqual(h.broker.lookup("B0").status.value, "CANCELED", "the stop's cancel goes out in the same pass")
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        self.assertTrue(ctl["latch_reason"].startswith(JOURNAL_STOP_REASON), ctl["latch_reason"])
        stops = h.desk.store.query("SELECT COUNT(*) AS n FROM events WHERE type IN ('StopLatched','StopReasserted')")
        self.assertEqual(stops[0]["n"], 1, "the failure is recorded once, not again by the drain after the pass")
        self.assertEqual(h.verify(), [])
        free_page_cap(j.journal, previous)
        h.pump()
        h.pump()
        b0 = h.row("orders", "order_id", "B0")
        self.assertEqual((b0["state"], b0["cum_qty"]), ("CANCELED", "2"))
        self.assertEqual(j.journal.unapplied_count(), 0)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
