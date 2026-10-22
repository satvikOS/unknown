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
from unknown.ops.backup import backups_for, restore_drill, take_backup  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
