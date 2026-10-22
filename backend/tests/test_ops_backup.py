"""Online backup and restore drill (ADR-0003 #6, ADR-0010; runbooks): a backup is taken beside a running
core without its lease, is marked inside itself, carries a SHA-256 sidecar, and a drill proves it restores:
chain and replay equivalence hold and opening it is detected as a restore with the stop latched."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402
from unknown.ops.backup import backups_for, restore_backup, restore_drill, take_backup  # noqa: E402
import shutil  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
