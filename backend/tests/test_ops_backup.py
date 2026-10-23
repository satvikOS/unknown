"""Online backup and restore drill (ADR-0003 #6, ADR-0010; runbooks): a backup is taken beside a running
core without its lease, is marked inside itself, carries a SHA-256 sidecar, and a drill proves it restores:
chain and replay equivalence hold and opening it is detected as a restore with the stop latched."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway import runner as runner_module  # noqa: E402
from unknown.gateway.journal import EventJournal, describe_file  # noqa: E402
from unknown.gateway.runner import attach_journal, detach_journal  # noqa: E402
from unknown.ops import backup as backup_module  # noqa: E402
from unknown.ops.backup import backups_for, journal_file, restore_backup, restore_drill, take_backup  # noqa: E402
import shutil  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402

F = Fixed.parse


class BackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = Paths(Path(tmp.name))
        self.paths.ensure()
        self.h = Harness(db_path=self.paths.db(ACCOUNT))
        self.addCleanup(self.h.close)
        self.h.bootstrap()
        self.assertTrue(self.h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)

    def test_backup_while_the_core_runs_then_drill_proves_it_restores(self):
        meta = take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        self.assertEqual(meta["integrity_check"], "ok")
        self.assertGreater(meta["source_max_seq"], 0)
        (backup,) = backups_for(self.paths, ACCOUNT)
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(backup.with_suffix(".json").read_text())["sha256"], meta["sha256"])
        # the live core keeps writing after the backup
        self.h.owner_cmd("stop", account_key=ACCOUNT)
        report = restore_drill(backup, ACCOUNT)
        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(report["restore_detected"])
        self.assertTrue(report["stop_latched"])
        self.assertEqual(report["latch_reason"], "RESTORED_FROM_BACKUP")
        self.assertEqual(report["replay_problems"], [])
        self.assertTrue(report["sha256_matches_sidecar"])
        self.assertEqual(self.h.verify(), [], "the live database is untouched")

    def test_tampered_backup_fails_the_drill(self):
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        with open(backup, "ab") as f:
            f.write(b"\0")
        report = restore_drill(backup, ACCOUNT)
        self.assertFalse(report["ok"])
        self.assertFalse(report["sha256_matches_sidecar"])

    def test_retention_keeps_the_newest(self):
        t = self.h.clock.now_ns()
        for i in range(5):
            take_backup(self.paths, ACCOUNT, keep=3, now_ns=t + i * 1_000_000)
        kept = backups_for(self.paths, ACCOUNT)
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(list(self.paths.backups.glob("*.json"))), 3)
        self.assertTrue(kept[-1].name.endswith("Z.sqlite"))

    def test_missing_database_is_an_error_not_an_empty_backup(self):
        with self.assertRaises(FileNotFoundError):
            take_backup(self.paths, "NO-SUCH-ACCOUNT")


class BackupReviewTests(BackupTests):
    """Review 2026-10-22 findings on backups and restores."""

    def test_retention_never_touches_an_account_whose_key_extends_this_one(self):
        t = self.h.clock.now_ns()
        other = self.paths.backups / f"{ACCOUNT}-2-20261020T000000000000Z.sqlite"   # account "<ACCOUNT>-2"
        other.write_bytes(b"other account")
        for i in range(3):
            take_backup(self.paths, ACCOUNT, keep=1, now_ns=t + i * 1_000_000)
        self.assertTrue(other.exists(), "another account's backup was deleted")
        self.assertEqual(len(backups_for(self.paths, ACCOUNT)), 1)

    def test_drill_without_a_sidecar_is_a_problem_unless_waived(self):
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        backup.with_suffix(".json").unlink()
        self.assertFalse(restore_drill(backup, ACCOUNT)["ok"])
        self.assertTrue(restore_drill(backup, ACCOUNT, require_sidecar=False)["ok"])

    def test_corrupt_backup_fails_integrity_even_without_sidecar(self):
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        data = bytearray(backup.read_bytes())
        data[4096:4196] = b"\xff" * 100                      # damage a page after the header
        backup.write_bytes(bytes(data))
        report = restore_drill(backup, ACCOUNT, require_sidecar=False)
        self.assertFalse(report["ok"])

    def test_marker_matches_the_snapshot(self):
        meta = take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        last = self.h.desk.store.query("SELECT MAX(seq) AS s FROM events")[0]["s"]
        self.assertEqual(meta["source_max_seq"], last)
        self.assertEqual(meta["source_epoch"], self.h.desk.store.epoch)

    def test_no_partial_file_is_left_behind(self):
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        self.assertEqual(list(self.paths.backups.glob("*.partial")), [])

    def test_restore_refuses_while_the_core_holds_the_lease(self):
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        with self.assertRaises(RuntimeError):
            restore_backup(self.paths, ACCOUNT, backup, now_ns=self.h.clock.now_ns())

    def test_restore_moves_wal_and_shm_aside_and_the_next_start_latches(self):
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        self.h.owner_cmd("stop", account_key=ACCOUNT)
        # simulate a crash: the lease stays held but goes stale; a -wal file stays beside the database
        self.h.desk.store.conn.close()
        wal = Path(f"{self.paths.db(ACCOUNT)}-wal")
        if not wal.exists():
            wal.write_bytes(b"stale wal from a crash")
        stale = self.h.clock.now_ns() + 3600 * 1_000_000_000
        report = restore_backup(self.paths, ACCOUNT, backup, now_ns=stale)
        self.assertIn(f"{ACCOUNT}.sqlite", report["moved_aside"])
        self.assertIn(f"{ACCOUNT}.sqlite-wal", report["moved_aside"])
        self.assertFalse(wal.exists())
        from unknown.core.desk import open_desk
        from unknown.domain.clock import ManualClock
        from unknown.domain.ids import IdGenerator
        clock = ManualClock(stale)
        desk = open_desk(self.paths.db(ACCOUNT), clock, IdGenerator(lambda: clock.now_ns() // 1_000_000),
                         deployment_id="restored", holder="restored", takeover=True)
        self.addCleanup(desk.store.close)
        with desk.store.read() as tx:
            import json as _json
            c = _json.loads(tx.one("SELECT row FROM control WHERE account_key=?", (ACCOUNT,))["row"])
        self.assertEqual((c["stop_latched"], c["latch_reason"]), (True, "RESTORED_FROM_BACKUP"))


class JournalBackupTests(unittest.TestCase):
    """Journal hardening (BR-ORD-20 follow-up): the online backup carries a consistent copy of the broker-event
    journal, the restore drill checks and applies it, and a restore keeps or installs the right journal."""

    def setUp(self):
        for logger in (runner_module.log,):
            patcher = mock.patch.object(logger, "disabled", True)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = Paths(Path(tmp.name))
        self.paths.ensure()
        self.h = Harness(db_path=self.paths.db(ACCOUNT))
        self.addCleanup(lambda: self.h.desk.store.close())
        self.journal = self.open_journal()
        h = self.h
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A", seed="150.00",
                                    target_terminal_equity="157.50").accepted)
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("2"), F("10.00"), commission=F("1.00"))
        h.runner.drain_events()

    def open_journal(self) -> EventJournal:
        journal = EventJournal(journal_file(self.paths, ACCOUNT), ACCOUNT, self.h.clock.now_ns,
                               database_id=self.h.desk.store.database_id)
        attach_journal(self.h.broker, journal)
        self.addCleanup(lambda: (detach_journal(self.h.broker), journal.close()))
        return journal

    def backup_while_a_fill_lands_between_the_copies(self) -> tuple[Path, dict]:
        """``take_backup`` while the core keeps working: the second fill is journaled and applied after the
        database copy and before the journal copy."""
        real = backup_module.backup_database

        def then_a_fill(*a, **kw):
            meta = real(*a, **kw)
            self.h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
            self.assertEqual(self.h.runner.drain_events(), 3)
            return meta

        with mock.patch.object(backup_module, "backup_database", then_a_fill):
            meta = take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        return backup, meta

    def stop_and_restore(self, backup: Path) -> dict:
        h = self.h
        detach_journal(h.broker)
        self.journal.close()
        h.desk.store.release_lease()
        h.desk.store.close()
        return restore_backup(self.paths, ACCOUNT, backup, now_ns=h.clock.now_ns())

    def start(self) -> EventJournal:
        """A new core process on the restored files (restore detected); returns its journal."""
        self.h.desk = self.h._open()
        self.assertEqual(self.h.control()["latch_reason"], "RESTORED_FROM_BACKUP")
        return self.open_journal()

    def test_the_backup_copies_the_journal_after_the_database_and_the_drill_applies_it(self):
        backup, meta = self.backup_while_a_fill_lands_between_the_copies()
        copy = backup.with_suffix(".events")
        info = meta["journal"]
        self.assertEqual(info["file"], copy.name)
        self.assertEqual(oct(copy.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(backup.with_suffix(".json").read_text())["journal"]["sha256"], info["sha256"])
        self.assertEqual((info["journal_id"], info["database_id"]), (self.journal.journal_id, meta["database_id"]))
        self.assertEqual(meta["database_id"], self.h.desk.store.database_id)
        self.assertEqual(info["entries"], len(list(self.journal.entries())), "taken after the second fill")
        self.assertEqual(info["unmarked"], 0)
        report = restore_drill(backup, ACCOUNT)
        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(report["journal"]["sha256_matches_sidecar"])
        self.assertEqual(report["journal"]["applied"], 3, "the fill the database copy lacked is applied")
        self.assertEqual(report["journal"]["unapplied_after"], 0)
        self.assertEqual(report["ledger_problems"], [])
        self.assertEqual(self.h.verify(), [], "the live database is untouched")
        t = self.h.clock.now_ns()
        for i in range(1, 3):
            take_backup(self.paths, ACCOUNT, keep=1, now_ns=t + i * 1_000_000)
        (kept,) = backups_for(self.paths, ACCOUNT)
        self.assertEqual(sorted(p.name for p in self.paths.backups.glob("*.events")), [kept.with_suffix(".events").name])

    def test_the_drill_reports_a_journal_copy_that_is_tampered_missing_or_older_than_the_database(self):
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        copy = backup.with_suffix(".events")
        self.assertTrue(restore_drill(backup, ACCOUNT)["ok"])
        original = copy.read_bytes()
        with self.subTest("tampered"):
            with open(copy, "ab") as f:
                f.write(b"\0")
            report = restore_drill(backup, ACCOUNT)
            self.assertFalse(report["ok"])
            self.assertIn("journal: the copy does not match its recorded SHA-256", report["problems"])
            copy.write_bytes(original)
        with self.subTest("missing"):
            copy.unlink()
            report = restore_drill(backup, ACCOUNT)
            self.assertIn("journal: the sidecar records a journal copy but the file is missing", report["problems"])
            copy.write_bytes(original)
        with self.subTest("older than the database copy"):
            # A journal copy taken BEFORE the database copy misses entries the database already applied.
            early = self.paths.root / "early.events"
            conn = sqlite3.connect(early)
            self.journal._connection().backup(conn)
            conn.close()
            self.h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
            self.h.runner.drain_events()
            take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns() + 1_000_000)
            newest = backups_for(self.paths, ACCOUNT)[-1]
            shutil.copyfile(early, newest.with_suffix(".events"))
            sidecar = json.loads(newest.with_suffix(".json").read_text())
            sidecar["journal"]["sha256"] = backup_module._sha256(newest.with_suffix(".events"))
            newest.with_suffix(".json").write_text(json.dumps(sidecar))
            report = restore_drill(newest, ACCOUNT)
            self.assertFalse(report["ok"])
            self.assertTrue([p for p in report["problems"] if "the journal was copied before the database" in p],
                            report["problems"])

    def test_restore_keeps_the_live_journal_of_the_same_database(self):
        """The live journal belongs to the restored database and is newer than the backup's copy: it stays,
        and the next start applies what the restored database lacks."""
        take_backup(self.paths, ACCOUNT, now_ns=self.h.clock.now_ns())
        (backup,) = backups_for(self.paths, ACCOUNT)
        self.h.broker.fill("B1", F("3"), F("10.00"), commission=F("1.00"))
        self.assertEqual(self.h.runner.drain_events(), 3)
        entries = len(list(self.journal.entries()))
        report = self.stop_and_restore(backup)
        self.assertEqual(report["journal"]["action"], "kept")
        self.assertEqual(report["journal"]["moved_aside"], [])
        journal = self.start()
        self.assertEqual(len(list(journal.entries())), entries)
        self.assertEqual(self.h.runner.replay_journal(), 3)
        o = self.h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        self.assertEqual(self.h.verify(), [])

    def test_restore_installs_the_backups_journal_when_the_live_one_is_another_databases(self):
        """The live journal was replaced by another database's journal: the restore moves it aside with the old
        database and installs the backup's journal copy, whose entries the next start applies."""
        backup, meta = self.backup_while_a_fill_lands_between_the_copies()
        live = journal_file(self.paths, ACCOUNT)
        detach_journal(self.h.broker)
        self.journal.close()
        live.unlink()
        foreign = EventJournal(live, ACCOUNT, self.h.clock.now_ns, database_id="f" * 32)
        foreign.open().close()
        self.journal = foreign
        report = self.stop_and_restore(backup)
        restored = report["journal"]
        self.assertEqual(restored["action"], "installed")
        self.assertEqual(restored["live"]["database_id"], "f" * 32)
        self.assertIn(live.name, restored["moved_aside"])
        self.assertEqual(Path(restored["aside_dir"]), Path(report["aside_dir"]))
        self.assertEqual(describe_file(Path(report["aside_dir"]) / live.name)["database_id"], "f" * 32)
        held = describe_file(live)
        self.assertEqual((held["journal_id"], held["database_id"], held["entries"]),
                         (meta["journal"]["journal_id"], meta["database_id"], meta["journal"]["entries"]))
        self.start()
        self.assertEqual(self.h.runner.replay_journal(), 3, "the fill after the database copy")
        o = self.h.row("orders", "order_id", "B1")
        self.assertEqual((o["cum_qty"], o["fees_final"]), ("5", "2.000000"))
        self.assertFalse([e for e in self.h.open_exceptions() if e["kind"] == "RECONCILIATION_INCOMPLETE"],
                         "the installed journal is this database's: nothing is set aside")
        self.assertEqual(self.h.verify(), [])

    def test_restore_refuses_a_journal_copy_it_cannot_install_before_moving_anything(self):
        """Review regression: the backup's journal copy is damaged (or another account's or database's). The
        restore must refuse before anything moves -- before the fix it replaced the database and moved the live
        journal aside, and only then failed on the copy's integrity check, leaving a half-done restore. Without
        the copy (moved out by the owner) the restore goes ahead."""
        backup, _ = self.backup_while_a_fill_lands_between_the_copies()
        copy = backup.with_suffix(".events")
        original = copy.read_bytes()
        live = journal_file(self.paths, ACCOUNT)
        detach_journal(self.h.broker)
        self.journal.close()
        live.unlink()
        foreign = EventJournal(live, ACCOUNT, self.h.clock.now_ns, database_id="f" * 32)
        foreign.open().close()
        self.journal = foreign
        h = self.h
        detach_journal(h.broker)
        h.desk.store.release_lease()
        h.desk.store.close()
        database = self.paths.db(ACCOUNT)
        before = (database.read_bytes(), live.read_bytes())
        other = self.paths.root / "other.events"
        EventJournal(other, "SIM-2", h.clock.now_ns).open().close()
        damaged = bytearray(original)
        damaged[4096:8192] = b"\xff" * 4096
        # SQLite builds differ in where they notice the damage (macOS: integrity_check; Linux CI: the first read), so
        # either refusal is accepted; what matters is that it is refused before anything moves.
        for name, content, expected in (("damaged", bytes(damaged), ("fails integrity_check", "cannot be read")),
                                        ("another account's", other.read_bytes(), ("belongs to account 'SIM-2'",))):
            with self.subTest(name):
                copy.write_bytes(content)
                with self.assertRaises(RuntimeError) as caught:
                    restore_backup(self.paths, ACCOUNT, backup, now_ns=h.clock.now_ns())
                self.assertTrue(any(e in str(caught.exception) for e in expected), str(caught.exception))
                self.assertIn("Nothing was moved", str(caught.exception))
                self.assertEqual((database.read_bytes(), live.read_bytes()), before, "nothing moved")
                self.assertEqual([p.name for p in self.paths.data.iterdir() if p.name.startswith("replaced-")], [])
        copy.unlink()
        report = restore_backup(self.paths, ACCOUNT, backup, now_ns=h.clock.now_ns())
        self.assertEqual(report["journal"]["action"], "moved_aside")
        self.assertFalse(live.exists())


if __name__ == "__main__":
    unittest.main()
