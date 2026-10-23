"""Journal hardening: what the review of the broker-event journal (BR-ORD-20, merged) left open.

1. A journal belongs to one core database. The database has a stable identity (``Store.database_id``, written
   once and carried by backups) that the journal records; a journal of another database is set aside unapplied,
   reported, and reconciliation is required. A database restored from before the journal's first applied entry
   gets every marked entry again, in order, relying on the handlers' broker keys.
2. Start-up replay refreshes the writer lease however large the journal is.
4. Applied entries older than a retention are pruned on a schedule; unapplied entries never are.
5. A corrupt journal is set aside with a report and reconciliation is required, never silently skipped.

(3, the journal in backups, restore drills and restores, is in test_ops_backup.py.)

Every number is a TEST FIXTURE. The simulated broker is a deterministic fixture (BRK-05); the "slow disk" of the
heartbeat test is a simulated clock, not a measurement.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_gateway_journal as base  # noqa: E402
from support import ACCOUNT, T0, Harness  # noqa: E402
from test_gateway_journal import (Journaled, aside_file, aside_marks, economic_state, journal_commands,  # noqa: E402
                                  quiet, running_session)

from unknown.broker.port import ConnectivityEvent  # noqa: E402
from unknown.broker.simulated import SimulatedBroker  # noqa: E402
from unknown.core import ledger  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.clock import ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway import journal as journal_module  # noqa: E402
from unknown.gateway import runner as runner_module  # noqa: E402
from unknown.gateway.journal import EventJournal, JournalSetAside, describe_file  # noqa: E402
from unknown.gateway.runner import (JOURNAL_GAP_REASON, JOURNAL_SET_ASIDE_REASON, attach_journal,  # noqa: E402
                                    detach_journal, event_command)
from unknown.ops.backup import backups_for, restore_backup, take_backup  # noqa: E402
from unknown.service.core import CoreService, Intervals  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.store import db as db_module  # noqa: E402
from unknown.store.db import Store, backup_database, database_identity  # noqa: E402

F = Fixed.parse
NS = 1_000_000_000
DAY = 86_400 * NS


def journal_stops(h: Harness, reason: str) -> list[str]:
    """Durable stop records (StopLatched / StopReasserted) whose reason starts with ``reason``."""
    rows = h.desk.store.query("SELECT type, payload FROM events WHERE type IN ('StopLatched','StopReasserted') "
                              "ORDER BY seq")
    return [json.loads(r["payload"])["info"]["reason"] for r in rows
            if json.loads(r["payload"])["info"]["reason"].startswith(reason)]


def bev_commands(h: Harness) -> int:
    return h.desk.store.query("SELECT COUNT(*) AS n FROM commands WHERE command_id LIKE 'sys.bev.%'")[0]["n"]


def executions(h: Harness) -> int:
    return h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"]


class Home:
    """A runtime home (``Paths``) with a core database at ``paths.db(ACCOUNT)`` and its journal path."""

    def __init__(self, test: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = Paths(self.root / "home")
        self.paths.ensure()
        self.journal_path = self.paths.data / f"{ACCOUNT}.events"

    def harness(self, **kw) -> Harness:
        return Harness(db_path=self.paths.db(ACCOUNT), **kw)


def stop_process(h: Harness, journal: EventJournal | None) -> None:
    """A clean core shutdown: the journal is detached and closed, the lease released, the store closed."""
    if journal is not None:
        detach_journal(h.broker)
        journal.close()
    h.desk.store.release_lease()
    h.desk.store.close()


# ======================================================================== 1. identity
class DatabaseIdentityTest(unittest.TestCase):
    def setUp(self):
        quiet(self)

    def test_the_core_database_has_one_identity_written_once_and_carried_by_its_backups(self):
        h = Harness()
        self.addCleanup(h.close)
        first = h.desk.store.database_id
        self.assertRegex(first, r"\A[0-9a-f]{32}\Z")
        h.restart()
        self.assertEqual(h.desk.store.database_id, first, "a restart keeps the identity")
        other = Harness()
        self.addCleanup(other.close)
        self.assertNotEqual(other.desk.store.database_id, first, "a new database gets its own")
        for sql in ("UPDATE database_identity SET database_id='x'", "DELETE FROM database_identity"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    h.desk.store.conn.execute(sql)
        copy = Path(h.tmp.name) / "copy.sqlite"
        meta = backup_database(h.db_path, copy, h.clock.now_ns())
        self.assertEqual(meta["database_id"], first)
        conn = sqlite3.connect(copy)
        try:
            self.assertEqual(database_identity(conn), first, "a backup carries the identity")
        finally:
            conn.close()

    def _old_journal_with_unapplied_entries(self, home: Home) -> tuple[str, str, int]:
        """An earlier core journaled a fill's three events and died before applying them. Returns its database
        identity, its journal id and how many entries it journaled."""
        h = home.harness()
        journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(h.broker, journal)
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.journal_broker_events(), 3)
        self.assertEqual(journal.unapplied_count(), 3)
        out = (h.desk.store.database_id, journal.journal_id, len(list(journal.entries())))
        stop_process(h, journal)
        return out

    @staticmethod
    def _recreate_database(home: Home) -> None:
        """The owner deliberately recreates the core database: the old file and its epoch marker move away."""
        old = home.root / "old-database"
        old.mkdir()
        for f in home.paths.data.iterdir():
            if f.name.startswith(f"{ACCOUNT}.sqlite"):
                shutil.move(str(f), old / f.name)

    def test_a_recreated_database_never_receives_the_old_journals_unapplied_entries(self):
        """The old journal's three unapplied entries belong to the old database. Next to a recreated database
        (another identity) none of them is applied: the journal is set aside with its entries intact and a
        report, the failure is recorded in the new database (stop latched with EVENT_JOURNAL_SET_ASIDE naming
        both identities, INCOMPLETE reconciliation, so needs_reconciliation), and a fresh journal of the new
        database takes the next broker events. Before the hardening the three entries were applied to it."""
        home = Home(self)
        old_db, old_jid, old_entries = self._old_journal_with_unapplied_entries(home)
        self._recreate_database(home)
        h = home.harness()   # a new database (and a fresh simulated broker account: fixture)
        self.addCleanup(h.desk.store.close)
        self.assertNotEqual(h.desk.store.database_id, old_db)
        running_session(h)
        journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(h.broker, journal)
        self.addCleanup(lambda: (detach_journal(h.broker), journal.close()))
        with self.assertRaises(JournalSetAside) as caught:
            h.runner.replay_journal()
        report = caught.exception.report
        self.assertEqual((bev_commands(h), executions(h)), (0, 0), "nothing of the old journal was applied")
        self.assertIn("belongs to core database", report["reason"])
        self.assertEqual((report["journal_database_id"], report["core_database_id"], report["journal_id"]),
                         (old_db, h.desk.store.database_id, old_jid))
        self.assertEqual(json.loads((Path(report["aside_dir"]) / "report.json").read_text())["reason"], report["reason"])
        held = describe_file(aside_file(report))
        self.assertEqual((held["entries"], held["unmarked"], held["database_id"]), (old_entries, 3, old_db))
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        (reason,) = journal_stops(h, JOURNAL_SET_ASIDE_REASON)
        self.assertIn(old_db, reason)
        self.assertEqual(h.desk.store.query("SELECT status FROM reconciliations ORDER BY rowid DESC LIMIT 1")[0]["status"],
                         "INCOMPLETE")
        h.broker.disconnect()   # the next broker event goes to a fresh journal of this database and is applied
        self.assertEqual(h.runner.drain_events(), 1)
        self.assertNotEqual(journal.journal_id, old_jid)
        self.assertEqual(journal.database_id, h.desk.store.database_id)
        self.assertEqual(journal.verify(), [])
        self.assertEqual(h.verify(), [])

    def test_the_core_service_sets_a_foreign_journal_aside_at_start(self):
        """The same through the core service: at start it opens the journal with its database's identity,
        refuses the old journal before applying anything, and reports it in the health query."""
        home = Home(self)
        old_db, old_jid, _ = self._old_journal_with_unapplied_entries(home)
        self._recreate_database(home)
        clock = ManualClock(T0)
        broker = SimulatedBroker(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        h = home.harness(clock=clock, broker=broker)
        running_session(h)
        new_db = h.desk.store.database_id
        stop_process(h, None)
        svc = CoreService(paths=home.paths, account_key=ACCOUNT, deployment_id="mac-dev", broker=broker, clock=clock,
                          intervals=Intervals(loop_s=0.005, valuation_s=3600.0, reconcile_s=3600.0,
                                              reconcile_retry_s=3600.0, heartbeat_s=0.0))
        thread = threading.Thread(target=svc.run, name="core-under-test", daemon=True)
        thread.start()
        self.addCleanup(lambda: (svc.shutdown(), thread.join(10)))
        self.assertTrue(svc.ready.wait(20))
        health = svc._enqueue({"op": "query", "name": "health"}, frozenset({"OWNER"}))["event_journal"]
        self.assertIsNotNone(health["set_aside"])
        self.assertIn("belongs to core database", health["set_aside"]["reason"])
        self.assertEqual((health["database_id"], health["unapplied"]), (new_db, 0))
        self.assertNotEqual(health["journal_id"], old_jid)
        svc.shutdown()
        thread.join(20)
        conn = sqlite3.connect(f"file:{home.paths.db(ACCOUNT)}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM commands WHERE command_id LIKE ?",
                                          (f"sys.bev.{old_jid}.%",)).fetchone()[0], 0)
            ctl = json.loads(conn.execute("SELECT row FROM control WHERE account_key=?", (ACCOUNT,)).fetchone()["row"])
            runs = [r["status"] for r in conn.execute("SELECT status FROM reconciliations ORDER BY rowid")]
        finally:
            conn.close()
        self.assertTrue(ctl["stop_latched"])
        self.assertTrue(ctl["latch_reason"].startswith(JOURNAL_SET_ASIDE_REASON), ctl["latch_reason"])
        self.assertIn("INCOMPLETE", runs, "reconciliation was required (the service then ran one)")

    def _legacy_journal(self, h: Harness, path: Path) -> EventJournal:
        """A journal written before identities existed (no database id): a fill's three entries, the first two
        applied by ``h``'s core and marked, the third not."""
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        journal = EventJournal(path, ACCOUNT, h.clock.now_ns, journal_id="legacy")
        entries = journal.append([(ev, *event_command(ACCOUNT, ev)) for ev in h.broker.drain_events()])
        for e in entries[:2]:
            h.runner.apply_entry(e, journal)
            journal.mark_applied(e, "applied")
        self.assertIsNone(journal.database_id)
        journal.close()
        return EventJournal(path, ACCOUNT, h.clock.now_ns)

    def test_a_journal_written_before_identities_is_bound_when_the_core_holds_its_history(self):
        """The upgrade path: this database holds the journal's applied entries, so the journal is this
        database's history. It is bound to the database (once) and its unapplied entry is applied."""
        h = Harness()
        self.addCleanup(h.close)
        journal = self._legacy_journal(h, Path(h.tmp.name) / f"{ACCOUNT}.events")
        attach_journal(h.broker, journal)
        self.addCleanup(lambda: (detach_journal(h.broker), journal.close()))
        self.assertEqual(h.runner.replay_journal(), 1)
        self.assertEqual(journal.database_id, h.desk.store.database_id)
        self.assertEqual(journal.journal_id, "legacy")
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("2", "1.000000"))
        self.assertFalse(h.control()["stop_latched"])
        self.assertEqual(h.verify(), [])

    def test_a_journal_written_before_identities_is_set_aside_when_the_core_holds_none_of_it(self):
        """The same legacy journal next to a database that holds none of its applied entries: it cannot be
        shown to be this database's history, so nothing of it is applied and it is set aside."""
        writer = Harness()
        self.addCleanup(writer.close)
        path = Path(writer.tmp.name) / f"{ACCOUNT}.events"
        self._legacy_journal(writer, path).close()
        h = Harness()
        self.addCleanup(h.close)
        running_session(h)
        journal = EventJournal(path, ACCOUNT, h.clock.now_ns)
        attach_journal(h.broker, journal)
        self.addCleanup(lambda: (detach_journal(h.broker), journal.close()))
        with self.assertRaises(JournalSetAside) as caught:
            h.runner.replay_journal()
        self.assertIn("records no core database", caught.exception.report["reason"])
        self.assertEqual((bev_commands(h), executions(h)), (0, 0))
        self.assertTrue(h.control()["needs_reconciliation"])
        self.assertEqual(h.verify(), [])


# ======================================================================== 1. restore
class RestoreOlderThanTheJournalTest(unittest.TestCase):
    def setUp(self):
        quiet(self)

    def test_a_database_restored_from_before_the_journals_first_applied_entry_gets_every_marked_entry_again(self):
        """RB-10 with a backup older than every entry of the journal. The first fill (E1) reached the core
        before the journal existed, under fresh command ids, and is in the backup. Then the journal took IBKR's
        re-delivery of E1's callbacks (duplicates by broker key) and the second fill (E2). After the restore the
        core holds none of the journal's commands -- no anchor -- but the journal records this database's
        identity, so all six marked entries are applied again in journal order: E1's again as duplicates by
        their broker keys (no second fill, fee or lot), E2's for real. Before the hardening nothing was
        re-applied and E2 was lost beyond the broker's history window."""
        home = Home(self)
        h = home.harness()
        self.addCleanup(lambda: h.desk.store.close())
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        redelivered = list(h.broker._events)
        h.runner.drain_events()   # no journal yet: applied under fresh command ids
        take_backup(home.paths, ACCOUNT, now_ns=h.clock.now_ns())
        (backup,) = backups_for(home.paths, ACCOUNT)
        journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(h.broker, journal)
        h.broker._events.extend(redelivered)   # the first fill's callbacks delivered again (fixture)
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.drain_events(), 6)
        jid = journal.journal_id
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        # The core stops cleanly; the owner restores the backup; a new process starts on the restored file.
        stop_process(h, journal)
        restore_backup(home.paths, ACCOUNT, backup, now_ns=h.clock.now_ns())
        h.desk = h._open()
        self.assertEqual(h.control()["latch_reason"], "RESTORED_FROM_BACKUP")
        self.assertEqual(executions(h), 1)
        journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(h.broker, journal)
        self.addCleanup(lambda: (detach_journal(h.broker), journal.close()))
        self.assertEqual(journal_commands(h, jid), [], "the restored core holds none of the journal's entries")
        self.assertEqual(h.runner.replay_journal(), 6)
        self.assertEqual(journal_commands(h, jid), [1, 2, 3, 4, 5, 6], "each entry once, in journal order")
        self.assertEqual(executions(h), 2)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        with h.desk.store.read() as tx:
            self.assertEqual(ledger.code_total(tx, ACCOUNT, ledger.COMMISSION, "USD"), F("2.00"), "each fee once")
        self.assertEqual(h.verify(), [])
        # The handlers' broker keys are what makes this safe: every entry applied once more, under fresh ids,
        # changes no money state.
        money = economic_state(h)
        for e in journal.entries():
            self.assertTrue(h.desk.run_system(e.command_kind, e.command_payload).accepted)
        self.assertEqual(economic_state(h), money)
        self.assertEqual(h.runner.replay_journal(), 0, "a second start re-applies nothing")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.verify(), [])


# ======================================================================== 2. heartbeat
class StartupHeartbeatTest(unittest.TestCase):
    ROW_NS = NS // 5        # simulated cost of reading one journal row (a very slow disk)
    QUERY_NS = NS // 5      # of one core query
    COMMAND_NS = NS // 5    # of one core command

    def test_start_up_replay_of_a_large_journal_never_lets_the_lease_go_stale(self):
        """A heartbeat-counting store under a simulated slow disk: every journal row read, core query and core
        command advances the clock. The start-up replay of a 300-entry journal -- 100 entries marked applied
        that the database lacks (restored), 200 never applied -- takes far longer than the 30 s stale bound,
        but the lease is refreshed during every long pass: the open verification, the rewind walk, the batch
        verification and the applies. The cadences are scaled down (20 rows) so the fixture stays small.
        Before the hardening, the open verification and the rewind walk ran without a heartbeat."""
        quiet(self)
        home = Home(self)
        clock = ManualClock(T0)
        broker = SimulatedBroker(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        h = home.harness(clock=clock, broker=broker)
        running_session(h)
        journal = EventJournal(home.journal_path, ACCOUNT, clock.now_ns, database_id=h.desk.store.database_id)
        events = [ConnectivityEvent(True, True, f"fixture event {i}") for i in range(300)]
        entries = journal.append([(ev, *event_command(ACCOUNT, ev)) for ev in events])
        for e in entries[:100]:
            journal.mark_applied(e, "applied")   # marked by a core whose database was then restored from before
        journal.close()
        stop_process(h, None)

        svc = CoreService(paths=home.paths, account_key=ACCOUNT, deployment_id="mac-dev", broker=broker, clock=clock,
                          intervals=Intervals(heartbeat_s=0.0))
        svc.desk = svc.open_desk()
        self.addCleanup(svc.desk.store.close)
        store = svc.desk.store
        store.conn.execute("PRAGMA synchronous=OFF")   # test speed only; durability is not under test here
        state = {"last": clock.now_ns(), "worst": 0, "beats": 0}

        def charge(ns: int) -> None:
            clock.advance(ns)
            state["worst"] = max(state["worst"], clock.now_ns() - state["last"])

        real_heartbeat, real_query, real_run = store.heartbeat, store.query, svc.desk.run_system

        def counting_heartbeat() -> None:
            real_heartbeat()
            state["beats"] += 1
            state["last"] = clock.now_ns()

        def slow_query(*a, **kw):
            charge(self.QUERY_NS)
            return real_query(*a, **kw)

        def slow_run(*a, **kw):
            charge(self.COMMAND_NS)
            return real_run(*a, **kw)

        real_connect = db_module.connect

        def journal_connect(path, **kw):
            conn = real_connect(path, **kw)
            if Path(path) == home.journal_path:
                conn.row_factory = lambda cursor, row: (charge(self.ROW_NS), sqlite3.Row(cursor, row))[1]
            return conn

        with mock.patch.object(store, "heartbeat", counting_heartbeat), \
                mock.patch.object(store, "query", slow_query), \
                mock.patch.object(svc.desk, "run_system", slow_run), \
                mock.patch.object(db_module, "connect", journal_connect), \
                mock.patch.object(journal_module, "HEARTBEAT_EVERY", 20), \
                mock.patch.object(runner_module, "REWIND_BEAT_EVERY", 20):
            started = clock.now_ns()
            svc._open_journal()
            svc._replay_journal(svc.make_runner())
            elapsed = clock.now_ns() - started
            unapplied = svc.journal.unapplied_count()
        detach_journal(broker)
        svc.journal.close()
        self.assertEqual(unapplied, 0)
        self.assertEqual(len(journal_commands_of(store, svc.journal.journal_id)), 300)
        self.assertGreater(elapsed, 5 * Store.LEASE_STALE_AFTER_NS, "the replay is far longer than the stale bound")
        self.assertLess(state["worst"], Store.LEASE_STALE_AFTER_NS,
                        f"the lease went {state['worst'] / NS:.1f} s without a heartbeat ({state['beats']} beats)")
        lease = store.conn.execute("SELECT heartbeat_at_ns FROM writer_lease WHERE id=1").fetchone()[0]
        self.assertLess(clock.now_ns() - lease, Store.LEASE_STALE_AFTER_NS)


def journal_commands_of(store: Store, journal_id: str) -> list[str]:
    return [r["command_id"] for r in store.conn.execute("SELECT command_id FROM commands WHERE command_id LIKE ? "
                                                        "ORDER BY rowid", (f"sys.bev.{journal_id}.%",))]


# ======================================================================== 4. retention
class RetentionTest(unittest.TestCase):
    def setUp(self):
        quiet(self)

    def test_prune_deletes_only_applied_entries_older_than_the_retention(self):
        """Entries applied 31 days ago go; three unapplied entries just as old stay (and anything after them),
        and go only once applied; young entries stay. The chain still verifies from the recorded floor, seqs
        continue after it (no command id is reused), the append-only guards are back after each prune, and a
        new process re-applies nothing."""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.runner.drain_events()
        applied = [e.seq for e in journal.entries()]
        k = applied[-1]
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.journal_broker_events(), 3)   # journaled, not applied
        h.clock.advance(31 * DAY)
        self.assertEqual(journal.prune(30 * DAY), k)
        self.assertEqual(journal.pruned_through, k)
        self.assertEqual([e.seq for e in journal.entries()], [k + 1, k + 2, k + 3], "old but unapplied: kept")
        self.assertEqual(journal.verify(), [])
        self.assertEqual(sorted(journal.marks()), [])
        self.assertEqual(h.runner.drain_events(), 3)
        self.assertEqual(journal.prune(30 * DAY), 3)
        self.assertEqual(list(journal.entries()), [])
        h.broker.disconnect()
        self.assertEqual(h.runner.drain_events(), 1)
        (young,) = journal.entries()
        self.assertEqual(young.seq, k + 4, "seqs continue after the pruned ones")
        self.assertEqual(journal.prune(30 * DAY), 0, "a young entry stays")
        conn = journal._connection()
        for sql in ("DELETE FROM entries", "DELETE FROM applied", "DELETE FROM prunes"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
        j.crash_and_restart()
        self.assertEqual(j.h.runner.replay_journal(), 0)
        self.assertEqual(j.journal.pruned_through, k + 3)
        self.assertEqual(j.journal.verify(), [])
        self.assertEqual(journal_commands(j.h), list(range(1, k + 5)), "each entry executed once, in order")
        o = j.h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        self.assertEqual(j.h.verify(), [])

    def test_prune_keeps_an_unmarked_entry_even_when_the_applied_prefix_claims_it(self):
        """Review regression (isolation): prune has two guards for unapplied entries, the in-memory applied
        prefix and the check that every deleted entry is marked. With the second deleted, every other test
        stayed green, because the first alone carried them. Here the prefix is forced past three unmarked
        entries: prune still stops at the first of them."""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.runner.drain_events()
        k = max(journal.marks())
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.journal_broker_events(), 3)
        h.clock.advance(31 * DAY)
        journal._applied_through = k + 3   # a wrong prefix (fixture): it must not be enough to delete anything
        self.assertEqual(journal.prune(30 * DAY), k)
        self.assertEqual([e.seq for e in journal.entries()], [k + 1, k + 2, k + 3])

    def test_prune_never_deletes_what_a_rewind_will_apply_again(self):
        """A restored database lacks three marked entries: they are to be applied again. A prune with no
        retention at all, before they are, keeps them (and prunes only up to the newest entry the core holds)."""
        h, journal, entries = base.ReviewRegressionTest.restored(self)
        self.assertEqual(h.runner.rewind_to_core(journal), 3)
        h.clock.advance(31 * DAY)
        journal.prune(0)
        self.assertEqual(journal.pruned_through, entries - 3)
        self.assertEqual(journal.unapplied_count(), 3)
        self.assertEqual(h.runner.replay_journal(), 3)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        self.assertEqual(h.verify(), [])

    def test_prune_waits_for_the_gateway_to_admit_the_journal(self):
        """A journal object not yet admitted (its rewind not yet run) prunes nothing: until then the core has
        not decided which marked entries it lacks."""
        h, journal, entries = base.ReviewRegressionTest.restored(self)
        h.clock.advance(31 * DAY)
        self.assertEqual(journal.prune(0), 0)
        self.assertEqual(h.runner.replay_journal(), 3)
        self.assertEqual(journal.prune(0), entries)
        self.assertEqual(h.verify(), [])

    def test_the_core_service_prunes_on_its_schedule(self):
        """The service loop prunes applied entries older than its retention (Intervals.journal_retention_s,
        default 30 days; ``--journal-retention-days``) every ``journal_prune_s``, and reports the floor in the
        health query."""
        self.assertEqual(Intervals().journal_retention_s, 30 * 86400)
        home = Home(self)
        clock = ManualClock(T0)
        broker = SimulatedBroker(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        h = home.harness(clock=clock, broker=broker)
        journal = EventJournal(home.journal_path, ACCOUNT, clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(broker, journal)
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.runner.drain_events()
        old = len(list(journal.entries()))
        self.assertEqual(journal.unapplied_count(), 0)
        stop_process(h, journal)
        clock.advance(31 * DAY)
        svc = CoreService(paths=home.paths, account_key=ACCOUNT, deployment_id="mac-dev", broker=broker, clock=clock,
                          intervals=Intervals(loop_s=0.005, valuation_s=3600.0, reconcile_s=3600.0,
                                              reconcile_retry_s=3600.0, heartbeat_s=0.0, journal_prune_s=0.0))
        thread = threading.Thread(target=svc.run, name="core-under-test", daemon=True)
        thread.start()
        self.addCleanup(lambda: (svc.shutdown(), thread.join(10)))
        self.assertTrue(svc.ready.wait(20))
        deadline = time.monotonic() + 20
        health = {}
        while time.monotonic() < deadline:
            health = svc._enqueue({"op": "query", "name": "health"}, frozenset({"OWNER"}))["event_journal"]
            if health["pruned_through"]:
                break
            time.sleep(0.01)
        self.assertGreaterEqual(health["pruned_through"], old, health)
        self.assertEqual(health["unapplied"], 0)
        svc.shutdown()
        thread.join(20)
        held = describe_file(home.journal_path)
        self.assertGreaterEqual(held["pruned_through"], old)
        self.assertEqual(held["unmarked"], 0)

    def test_both_entry_points_take_the_retention_from_their_command_line(self):
        """Review regression: ``--journal-retention-days`` reaches the service from the simulator entry point
        and from the IBKR one (which had no such flag), and a value that would make every prune fail unseen
        (NaN, infinity) or prune nothing sensible (negative) is refused at the command line."""
        from unknown.broker.ibkr import serve as ibkr_serve
        from unknown.service import core as core_module
        home = Home(self)
        common = ["--account", ACCOUNT, "--deployment", "mac-dev", "--home", str(home.paths.root)]
        with mock.patch.object(core_module.logging, "basicConfig"):
            with mock.patch.object(core_module, "serve") as served:
                core_module.main(common + ["--journal-retention-days", "7"])
            self.assertEqual(served.call_args.args[0].intervals.journal_retention_s, 7 * 86400)
            with mock.patch.object(core_module, "serve") as served:
                core_module.main(common)
            self.assertEqual(served.call_args.args[0].intervals.journal_retention_s, 30 * 86400)
            with mock.patch.object(ibkr_serve, "build_service", return_value=(object(), None)) as built, \
                    mock.patch.object(ibkr_serve, "serve"):
                ibkr_serve.main(common + ["--journal-retention-days", "0.5"])
            self.assertEqual(built.call_args.kwargs["intervals"].journal_retention_s, 0.5 * 86400)
            for main in (core_module.main, ibkr_serve.main):
                for bad in ("-1", "nan", "inf", "x"):
                    with self.subTest(main=main.__module__, value=bad), \
                            mock.patch("sys.stderr"), mock.patch.object(core_module, "serve"), \
                            mock.patch.object(ibkr_serve, "build_service") as built:
                        with self.assertRaises(SystemExit):
                            main(common + ["--journal-retention-days", bad])
                        built.assert_not_called()

    def test_the_pruning_gap_is_recorded_once_per_restored_database_not_on_every_start(self):
        """Review regression: every journaled entry was pruned and nothing was journaled since, so a restored
        database has no anchor at any start. The gap is recorded at the first start only (its stop carries
        ``sys.bevgap.<journal id>.<floor>``); before the fix every start latched the stop again and recorded a
        new INCOMPLETE reconciliation, until a new broker event was applied -- even after a green start."""
        home = Home(self)
        h = home.harness()
        self.addCleanup(lambda: h.desk.store.close())
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()   # no journal yet
        take_backup(home.paths, ACCOUNT, now_ns=h.clock.now_ns())
        (backup,) = backups_for(home.paths, ACCOUNT)
        journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(h.broker, journal)
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.drain_events(), 3)
        h.clock.advance(31 * DAY)
        self.assertEqual(journal.prune(30 * DAY), 3)
        jid = journal.journal_id
        stop_process(h, journal)
        restore_backup(home.paths, ACCOUNT, backup, now_ns=h.clock.now_ns())

        def incomplete() -> int:
            return h.desk.store.query("SELECT COUNT(*) AS n FROM reconciliations WHERE status='INCOMPLETE'")[0]["n"]

        for start in range(3):
            h.desk = h._open()
            before = incomplete()
            journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
            attach_journal(h.broker, journal)
            self.assertEqual(h.runner.replay_journal(), 0)
            with self.subTest(start=start):
                self.assertEqual(len(journal_stops(h, JOURNAL_GAP_REASON)), 1)
                self.assertEqual(incomplete() - before, 1 if start == 0 else 0, "a reconciliation required once")
            stop_process(h, journal)
        h.desk = h._open()
        self.assertEqual(h.desk.store.query("SELECT kind FROM commands WHERE command_id=?",
                                            (f"sys.bevgap.{jid}.3",))[0]["kind"], "stop")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.row("orders", "order_id", "B1")["cum_qty"], "2")
        self.assertEqual(h.verify(), [])

    def test_a_database_restored_from_before_pruned_entries_records_the_gap(self):
        """The backup predates entries that were since pruned: they cannot be applied again. The newer marked
        entries are re-applied in order, the gap is recorded (stop with EVENT_JOURNAL_GAP, INCOMPLETE
        reconciliation), and the reconciliation then recovers the pruned fill from the broker."""
        home = Home(self)
        h = home.harness()
        self.addCleanup(lambda: h.desk.store.close())
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()   # no journal yet
        take_backup(home.paths, ACCOUNT, now_ns=h.clock.now_ns())
        (backup,) = backups_for(home.paths, ACCOUNT)
        journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(h.broker, journal)
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.drain_events(), 3)
        h.clock.advance(31 * DAY)
        self.assertEqual(journal.prune(30 * DAY), 3)
        h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        self.assertEqual(h.runner.drain_events(), 3)
        stop_process(h, journal)
        restore_backup(home.paths, ACCOUNT, backup, now_ns=h.clock.now_ns())
        h.desk = h._open()
        journal = EventJournal(home.journal_path, ACCOUNT, h.clock.now_ns, database_id=h.desk.store.database_id)
        attach_journal(h.broker, journal)
        self.addCleanup(lambda: (detach_journal(h.broker), journal.close()))
        self.assertEqual(h.runner.replay_journal(), 3, "the entries after the prune floor are applied again")
        (gap,) = journal_stops(h, JOURNAL_GAP_REASON)
        self.assertIn("pruned", gap)
        self.assertTrue(h.control()["needs_reconciliation"])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        self.assertEqual(h.verify(), [])


# ======================================================================== 5. corruption
class CorruptJournalTest(unittest.TestCase):
    def setUp(self):
        quiet(self)

    def test_a_checksum_failure_sets_the_journal_aside_with_a_report_and_requires_reconciliation(self):
        """The first unapplied entry (a fill's execution) fails its checksum. Nothing from it on is applied,
        and it is not skipped silently: the failure is recorded in the core first (stop latched with
        EVENT_JOURNAL_SET_ASIDE naming the checksum failure, INCOMPLETE reconciliation, RECONCILIATION_INCOMPLETE
        case, no green start), the file moves aside with report.json (the corrupt seq, the unapplied entries
        still inside), the health query shows it, and a fresh journal takes the next events. The clean
        reconciliation the owner then needs recovers the fill from the broker."""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("5"), F("10.00"), commission=F("1.00"))
        h.runner.journal_broker_events()
        execution = journal.unapplied()[0]
        journal.close()
        conn = sqlite3.connect(journal.path)
        conn.execute("DROP TRIGGER entries_no_update")
        conn.execute("UPDATE entries SET event=replace(event, 'E1', 'E9') WHERE seq=?", (execution.seq,))
        conn.commit()
        conn.close()
        with self.assertRaises(JournalSetAside) as caught:
            h.runner.drain_events()
        report = caught.exception.report
        self.assertEqual(executions(h), 0)
        self.assertIn(f"entry {execution.seq} fails its checksum", report["reason"])
        self.assertEqual(report["broken_seq"], execution.seq)
        self.assertEqual(json.loads((Path(report["aside_dir"]) / "report.json").read_text())["broken_seq"],
                         execution.seq)
        self.assertEqual(describe_file(aside_file(report))["unmarked"], 3)
        self.assertEqual((report["contents"]["unmarked"], report["contents"]["journal_id"]), (3, base.JID))
        self.assertNotIn(execution.seq, aside_marks(report))
        (reason,) = journal_stops(h, JOURNAL_SET_ASIDE_REASON)
        self.assertIn("fails its checksum", reason)
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        self.assertTrue([e for e in h.open_exceptions() if e["kind"] == "RECONCILIATION_INCOMPLETE"])
        self.assertEqual(journal.status()["set_aside"]["aside_dir"], report["aside_dir"])
        h.quote("9.99", "10.01")
        start = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertFalse(start.accepted)
        self.assertIn("RECONCILIATION_REQUIRED", start.body["details"]["blockers"])
        h.broker.disconnect()
        h.broker.reconnect()
        self.assertEqual(h.runner.drain_events(), 2, "new events go to a fresh journal and are applied")
        self.assertEqual(journal.verify(), [])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "1.000000"))
        self.assertEqual(h.verify(), [])

    def test_a_file_sqlite_reports_damaged_is_set_aside(self):
        """The journal file is overwritten with bytes SQLite cannot read (not a checksum: the file itself). The
        next drain sets it aside (SQLite reports it damaged) and fails closed; the following drain journals and
        applies the waiting event in a fresh file."""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        running_session(h)
        journal.close()
        size = journal.path.stat().st_size
        journal.path.write_bytes(b"\xde\xad" * (size // 2))
        for suffix in ("-wal", "-shm"):
            Path(f"{journal.path}{suffix}").unlink(missing_ok=True)
        h.broker.disconnect()
        with self.assertRaises(JournalSetAside) as caught:
            h.runner.drain_events()
        self.assertIn("damaged", caught.exception.report["reason"])
        self.assertTrue(aside_file(caught.exception.report).exists())
        ctl = h.control()
        self.assertTrue(ctl["stop_latched"] and ctl["needs_reconciliation"])
        self.assertEqual(h.runner.drain_events(), 1)
        self.assertEqual([e.event_type for e in journal.entries()], ["ConnectivityEvent"])
        self.assertEqual(h.verify(), [])

    @staticmethod
    def _journal_with_unapplied_fill(j: Journaled) -> None:
        h = j.h
        running_session(h)
        assert h.intent("B1", "BUY", "5", "10.00", "A").accepted
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        assert h.runner.journal_broker_events() == 3   # journaled, not applied

    def test_damage_only_the_admission_reads_sets_the_journal_aside_in_a_new_process(self):
        """Review regression: a page only the start-up admission reads is damaged (here the prune records, read
        by the rewind). A new process must set the journal aside, as for any damaged file -- before the fix the
        admission raised JournalCorrupt on every drain and the desk failed closed (EVENT_JOURNAL_UNWRITABLE)
        forever with the file still in place."""
        j = Journaled()
        self.addCleanup(j.close)
        self._journal_with_unapplied_fill(j)
        j.journal.close()
        conn = sqlite3.connect(j.path)
        try:
            page = conn.execute("PRAGMA page_size").fetchone()[0]
            root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='prunes'").fetchone()[0]
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        data = bytearray(j.path.read_bytes())
        data[(root - 1) * page:root * page] = b"\xff" * page
        j.path.write_bytes(bytes(data))
        j.crash_and_restart()
        h = j.h
        with self.assertRaises(JournalSetAside) as caught:
            h.runner.drain_events()
        self.assertIn("damaged", caught.exception.report["reason"])
        self.assertTrue(aside_file(caught.exception.report).exists())
        self.assertTrue(h.control()["latch_reason"].startswith(JOURNAL_SET_ASIDE_REASON), h.control()["latch_reason"])
        self.assertTrue(h.control()["needs_reconciliation"])
        h.broker.disconnect()
        h.broker.reconnect()
        self.assertEqual(h.runner.drain_events(), 2, "a fresh journal takes the next events")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")   # recovers the fill from the broker
        self.assertEqual(h.row("orders", "order_id", "B1")["cum_qty"], "2")
        self.assertEqual(h.verify(), [])

    def test_a_journal_that_cannot_be_moved_aside_is_recorded_once_not_on_every_cycle(self):
        """Review regression: while the file cannot be moved (here os.replace is refused), every cycle tries
        again, but the failure is recorded in the core once -- before the fix each pump wrote four stops and four
        INCOMPLETE reconciliations (two descriptions of one failure alternated, defeating the once-per-episode
        record) and left three empty set-aside directories. When the move works, the journal is set aside."""
        j = Journaled()
        self.addCleanup(j.close)
        h, journal = j.h, j.journal
        self._journal_with_unapplied_fill(j)
        execution = journal.unapplied()[0]
        journal.close()
        conn = sqlite3.connect(journal.path)
        conn.execute("DROP TRIGGER entries_no_update")
        conn.execute("UPDATE entries SET event=replace(event, 'E1', 'E9') WHERE seq=?", (execution.seq,))
        conn.commit()
        conn.close()

        def records() -> tuple[int, int, int]:
            q = h.desk.store.query
            return (q("SELECT COUNT(*) AS n FROM events WHERE type IN ('StopLatched','StopReasserted')")[0]["n"],
                    q("SELECT COUNT(*) AS n FROM reconciliations")[0]["n"],
                    len([p for p in journal.path.parent.iterdir() if ".set-aside-" in p.name]))

        stops, runs, _ = records()

        def refused(*a, **kw):
            raise PermissionError(1, "Operation not permitted")

        local_os = mock.Mock(wraps=journal_module.os)
        local_os.replace = refused
        with mock.patch.object(journal_module, "os", local_os):
            for _ in range(4):
                with self.assertRaises(JournalSetAside):
                    h.runner.pump()   # the outbox pass still runs; its drain raises again
                with self.assertRaises(JournalSetAside) as caught:
                    h.runner.drain_events()
                self.assertIsNone(caught.exception.report)
                self.assertEqual(records(), (stops + 1, runs + 1, 0), "recorded once; no directory left behind")
        self.assertTrue(journal.path.exists(), "the file stays in place while it cannot be moved")
        self.assertIn("Operation not permitted", journal.status()["failure"])
        self.assertTrue(h.control()["latch_reason"].startswith(JOURNAL_SET_ASIDE_REASON))
        with self.assertRaises(JournalSetAside) as caught:
            h.runner.drain_events()
        self.assertEqual(caught.exception.report["broken_seq"], execution.seq)
        self.assertEqual(records()[2], 1)
        self.assertEqual(executions(h), 0)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
