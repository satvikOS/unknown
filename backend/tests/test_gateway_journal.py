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
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402
from test_replay_randomized import Scenario  # noqa: E402

from unknown.core.replay import replay_and_compare, table_dump  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway import runner as runner_module  # noqa: E402
from unknown.gateway.journal import EventJournal, JournalCorrupt, JournalUnavailable, event_json  # noqa: E402
from unknown.gateway.runner import JOURNAL_STOP_REASON, attach_journal, detach_journal, event_backlog  # noqa: E402
from unknown.store.schema import PROJECTION_TABLES  # noqa: E402

F = Fixed.parse
NS = 1_000_000_000
JID = "j0"


class Crash(BaseException):
    """The process dies here: nothing after it runs, and no ``except Exception`` catches it."""


def quiet(test: unittest.TestCase) -> None:
    """The journal failures below are logged CRITICAL by design."""
    for logger in (runner_module.log,):
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


if __name__ == "__main__":
    unittest.main()
