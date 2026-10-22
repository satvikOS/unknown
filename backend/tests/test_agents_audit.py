"""Append-only, hash-chained agent audit trail (CAP-AGT-09): chain verification and tamper detection."""
from __future__ import annotations

import enum
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import TempDirCase  # noqa: E402
from unknown.agents.audit import GENESIS, AuditError, AuditTrail, sanitize  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.money import Money  # noqa: E402


class Colour(enum.Enum):
    RED = "red"


class AuditTrailTest(TempDirCase, unittest.TestCase):
    def trail(self) -> AuditTrail:
        t = AuditTrail(self.tmp / "agents" / "audit.sqlite", clock_ns=self.clock.now_ns)
        self.addCleanup(t.close)
        return t

    def fill(self, t: AuditTrail) -> None:
        t.append("job-1", {"kind": "job_started", "attempt": 1}, actor="worker")
        t.append("job-2", {"kind": "job_started", "attempt": 1}, actor="worker")
        t.append("job-1", {"kind": "reserved", "amount": "0.01 USD", "at_ns": 123}, actor="runner")
        t.append("job-1", {"kind": "settled", "actual": "0.0076 USD"}, actor="runner")

    def raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.tmp / "agents" / "audit.sqlite", isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def test_records_are_chained_and_queryable_by_job(self):
        t = self.trail()
        self.fill(t)
        self.assertEqual(t.verify(), [])
        recs = t.records("job-1")
        self.assertEqual([r["kind"] for r in recs], ["job_started", "reserved", "settled"])
        self.assertEqual([r["seq"] for r in recs], [1, 3, 4])
        self.assertEqual(recs[1]["at_ns"], 123)
        self.assertEqual(recs[1]["actor"], "runner")
        self.assertEqual(recs[1]["record"], {"kind": "reserved", "amount": "0.01 USD", "at_ns": 123})
        self.assertEqual(t.head(), (4, recs[-1]["chain_hash"]))

    def test_chain_continues_across_reopen(self):
        t = self.trail()
        self.fill(t)
        t.close()
        again = AuditTrail(self.tmp / "agents" / "audit.sqlite", clock_ns=self.clock.now_ns)
        self.addCleanup(again.close)
        self.assertEqual(again.append("job-1", {"kind": "job_finished"}, actor="worker"), 5)
        self.assertEqual(again.verify(), [])

    def test_update_and_delete_are_refused(self):
        self.fill(self.trail())
        conn = self.raw()
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE audit SET body = '{}' WHERE seq = 2")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM audit WHERE seq = 2")

    def test_edits_deletions_and_reattribution_are_detected(self):
        cases = {
            "body": "UPDATE audit SET body = replace(body, '0.01 USD', '0.00 USD') WHERE seq = 3",
            "rehashed body": "UPDATE audit SET body = '{}', body_hash = 'x' WHERE seq = 3",
            "re-attributed": "UPDATE audit SET job_id = 'job-2' WHERE seq = 3",
            "deleted": "DELETE FROM audit WHERE seq = 2",
        }
        for name, sql in cases.items():
            with self.subTest(name):
                path = self.tmp / "agents" / "audit.sqlite"
                if path.exists():
                    for suffix in ("", "-wal", "-shm"):
                        Path(str(path) + suffix).unlink(missing_ok=True)
                t = AuditTrail(path, clock_ns=self.clock.now_ns)
                self.fill(t)
                self.assertEqual(t.verify(), [])
                conn = sqlite3.connect(path, isolation_level=None)
                conn.execute("DROP TRIGGER audit_no_update")
                conn.execute("DROP TRIGGER audit_no_delete")
                conn.execute(sql)
                conn.close()
                problems = t.verify()
                t.close()
                self.assertNotEqual(problems, [], name)

    def test_sanitize_keeps_exact_numbers_and_never_emits_floats(self):
        out = sanitize({"f": 0.1, "fx": Fixed.parse("1.50"), "m": Money("USD", Fixed.parse("2.00")), "e": Colour.RED,
                        "t": (1, "a"), "s": {"b", "a"}, 3: None, "o": Path("/x/y")})
        self.assertEqual(out["o"], "/x/y")
        self.assertEqual(out["f"], "0.1")
        self.assertEqual(out["fx"], "1.50")
        self.assertEqual(out["e"], "red")
        self.assertEqual(out["t"], [1, "a"])
        self.assertEqual(out["s"], ["a", "b"])
        self.assertIn("3", out)
        t = self.trail()
        t.append("job-1", {"kind": "generated", "latency_s": 1.25, "money": Money("USD", Fixed.parse("0.01"))}, actor="runner")
        self.assertEqual(t.records("job-1")[0]["record"]["latency_s"], "1.25")
        self.assertEqual(t.verify(), [])

    def test_malformed_records_are_refused(self):
        t = self.trail()
        for job, rec, actor in (("job-1", {"no": "kind"}, "w"), ("job-1", ["kind"], "w"), ("", {"kind": "x"}, "w"),
                                ("job-1", {"kind": "x"}, ""), ("job-1", {"kind": "k" * 65}, "w")):
            with self.assertRaises(AuditError):
                t.append(job, rec, actor=actor)
        self.assertEqual(t.head(), (0, GENESIS))

    def test_read_only_handle_on_a_missing_file_is_empty_and_creates_nothing(self):
        path = self.tmp / "nowhere" / "audit.sqlite"
        ro = AuditTrail(path, clock_ns=self.clock.now_ns, readonly=True)
        self.assertEqual((ro.records("job-1"), ro.verify(), ro.head()), ([], [], (0, GENESIS)))
        with self.assertRaises(AuditError):
            ro.append("job-1", {"kind": "x"}, actor="api")
        self.assertFalse(path.exists())
        rw = self.trail()
        self.fill(rw)
        ro2 = AuditTrail(rw.path, clock_ns=self.clock.now_ns, readonly=True)
        self.addCleanup(ro2.close)
        self.assertEqual(len(ro2.records("job-1")), 3)
        self.assertEqual(ro2.verify(), [])


if __name__ == "__main__":
    unittest.main()
