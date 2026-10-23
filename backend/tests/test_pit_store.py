"""Point-in-time store semantics (ADR-0015; DAT-02, DAT-04, DAT-06). All values are fixtures."""
import unittest

from unknown.data.pit import OPEN_END, Observation, ResearchStore
from unknown.domain.clock import utc_ns
from unknown.domain.fixed import Fixed

D = "sec.companyfacts"
Q_END = "2025-12-31"
FILED_10K = utc_ns(2026, 2, 20, 21, 5)      # original 10-K acceptance (fixture)
FILED_10KA = utc_ns(2026, 5, 10, 20, 0)     # amended 10-K/A restating the same period (fixture)
INGESTED = utc_ns(2026, 10, 22)


def obs(value: str | None, available: int, **kw) -> Observation:
    base = dict(dataset=D, entity_id="ISSUER-1", field="Revenues", unit="USD", currency="USD", period_start="2025-01-01",
                period_end=Q_END, period_kind="FY", receipt_time_ns=INGESTED, available_time_ns=available,
                source_ref="raw:fixture")
    base.update(kw)
    if value is not None:
        base["value"] = Fixed.parse(value)
    return Observation(**base)


class PointInTimeTests(unittest.TestCase):
    def setUp(self):
        self.s = ResearchStore(":memory:")

    def tearDown(self):
        self.s.close()

    def test_restatement_never_leaks_backwards(self):
        """DAT-04: a later publication for the same period is invisible before its own available time."""
        self.s.record(obs("1000", FILED_10K), INGESTED)
        self.s.record(obs("900", FILED_10KA), INGESTED)
        before_any = self.s.latest_as_of("ISSUER-1", "Revenues", FILED_10K - 1)
        self.assertIsNone(before_any)
        between = self.s.latest_as_of("ISSUER-1", "Revenues", FILED_10KA - 1)
        self.assertEqual(between["value"], "1000")
        after = self.s.latest_as_of("ISSUER-1", "Revenues", FILED_10KA)
        self.assertEqual(after["value"], "900")
        self.assertEqual(len(self.s.history("ISSUER-1", "Revenues", Q_END)), 2)

    def test_identical_reingestion_is_unchanged(self):
        a = self.s.record(obs("1000", FILED_10K), INGESTED)
        b = self.s.record(obs("1000", FILED_10K), INGESTED + 1)
        self.assertEqual(b, (a[0], "UNCHANGED"))

    def test_silent_correction_of_same_publication(self):
        """A provider changing an already-published value is a knowledge-time revision, visible in audit mode."""
        first, _ = self.s.record(obs("1000", FILED_10K), INGESTED)
        later = INGESTED + 86_400 * 10**9
        second, outcome = self.s.record(obs("1001", FILED_10K), later)
        self.assertEqual(outcome, "CORRECTED")
        research = self.s.latest_as_of("ISSUER-1", "Revenues", later)
        self.assertEqual((research["value"], research["supersedes"]), ("1001", first))
        self.assertIn("silent_correction", research["quality"])
        audit = self.s.latest_as_of("ISSUER-1", "Revenues", later, knowledge_cutoff_ns=INGESTED)
        self.assertEqual(audit["value"], "1000")
        old = [r for r in self.s.history("ISSUER-1", "Revenues", Q_END) if r["obs_id"] == first][0]
        self.assertEqual(old["known_to_ns"], later)

    def test_missing_is_not_zero(self):
        """DAT-06: a missing value is recorded with its reason and never as zero."""
        self.s.record(obs(None, FILED_10K, missing_reason="NOT_REPORTED"), INGESTED)
        r = self.s.latest_as_of("ISSUER-1", "Revenues", FILED_10K)
        self.assertIsNone(r["value"])
        self.assertEqual(r["missing_reason"], "NOT_REPORTED")
        with self.assertRaises(ValueError):
            obs(None, FILED_10K, missing_reason="ZERO")
        with self.assertRaises(ValueError):
            Observation(dataset=D, entity_id="X", field="f", unit="USD", receipt_time_ns=0, available_time_ns=0, source_ref="r")

    def test_floats_rejected(self):
        with self.assertRaises(TypeError):
            Observation(dataset=D, entity_id="X", field="f", unit="USD", receipt_time_ns=0, available_time_ns=0,
                        source_ref="r", value=1.5)

    def test_symbology_history_and_ambiguity(self):
        """DOM-03/DAT-04: a reused ticker resolves by date; overlapping claims are refused, not guessed."""
        self.s.upsert_entity("ISSUER-OLD", "issuer", "Old Co (fixture)")
        self.s.upsert_entity("ISSUER-NEW", "issuer", "New Co (fixture)")
        t1, t2 = utc_ns(2010, 1, 1), utc_ns(2018, 6, 1)
        self.s.add_alias("ticker:XNYS", "ABC", "ISSUER-OLD", t1, "fixture", valid_to_ns=t2)
        self.s.add_alias("ticker:XNYS", "ABC", "ISSUER-NEW", t2, "fixture")
        self.assertEqual(self.s.resolve("ticker:XNYS", "ABC", utc_ns(2015, 1, 1)), "ISSUER-OLD")
        self.assertEqual(self.s.resolve("ticker:XNYS", "ABC", utc_ns(2020, 1, 1)), "ISSUER-NEW")
        self.assertIsNone(self.s.resolve("ticker:XNYS", "ABC", utc_ns(2005, 1, 1)))
        self.s.add_alias("ticker:XNYS", "ABC", "ISSUER-OLD", utc_ns(2019, 1, 1), "fixture-conflict")
        with self.assertRaises(LookupError):
            self.s.resolve("ticker:XNYS", "ABC", utc_ns(2020, 1, 1))

    def test_derived_metric_lineage(self):
        oid, _ = self.s.record(obs("1000", FILED_10K), INGESTED)
        mid = self.s.record_metric("ISSUER-1", "revenue_fy", "identity@1", FILED_10K, Fixed.parse("1000"), "USD", [oid], INGESTED)
        row = self.s.conn.execute("SELECT * FROM derived_metric WHERE metric_id=?", (mid,)).fetchone()
        self.assertEqual((row["formula"], row["inputs"]), ("identity@1", f"[{oid}]"))
        with self.assertRaises(ValueError):
            self.s.record_metric("ISSUER-1", "x", "f@1", 0, None, "USD", [], 0)

    def test_open_end_sentinel(self):
        self.s.record(obs("1000", FILED_10K), INGESTED)
        self.assertEqual(self.s.history("ISSUER-1", "Revenues", Q_END)[0]["known_to_ns"], OPEN_END)


if __name__ == "__main__":
    unittest.main()


class PublicationIdentityTests(unittest.TestCase):
    def test_same_instant_different_documents_are_not_a_correction(self):
        """Regression (live SEC ingest 2026-10-22): a 10-Q and an 8-K filed the same day reporting different
        values for one period are two publications, not a silent correction."""
        s = ResearchStore(":memory:")
        t = utc_ns(2013, 4, 24, 23, 59, 59)
        a = Observation(dataset=D, entity_id="I", field="UTB", unit="USD", receipt_time_ns=INGESTED, available_time_ns=t,
                        source_ref="r", value=Fixed.parse("2100000000"), period_end="2012-09-29", period_kind="INSTANT",
                        publication_id="0001193125-13-168288")
        b = Observation(dataset=D, entity_id="I", field="UTB", unit="USD", receipt_time_ns=INGESTED, available_time_ns=t,
                        source_ref="r", value=Fixed.parse("2062000000"), period_end="2012-09-29", period_kind="INSTANT",
                        publication_id="0001193125-13-170623")
        self.assertEqual(s.record(a, INGESTED)[1], "NEW")
        self.assertEqual(s.record(b, INGESTED)[1], "NEW")
        self.assertEqual(len(s.conflicts("I", "UTB")), 1)
        self.assertEqual(s.latest_as_of("I", "UTB", t)["publication_id"], "0001193125-13-170623")  # deterministic tie-break
        s.close()


class ReadOnlyOpenTests(unittest.TestCase):
    """Research consumers (agent worker, terminal API) open the store read-only and never create it."""

    def test_reads_but_never_writes_or_creates(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research.sqlite"
            with self.assertRaises(FileNotFoundError):
                ResearchStore.open_readonly(path)
            self.assertFalse(path.exists())
            rw = ResearchStore(path)
            rw.record(obs("100", FILED_10K), INGESTED)
            rw.close()
            ro = ResearchStore.open_readonly(path)
            self.assertEqual(ro.latest_as_of("ISSUER-1", "Revenues", FILED_10K)["value"], "100")
            with self.assertRaises(sqlite3.OperationalError):
                ro.record(obs("101", FILED_10KA), INGESTED)
            ro.close()


class RetentionAndDeletionTests(unittest.TestCase):
    """BR-DAT-16: retention facts read-only, deletion by retention or rights withdrawal, and the deletion events the
    research registry sweeps. Payloads, deadlines and values are fixtures."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.s = ResearchStore(Path(self.tmp.name) / "research.sqlite")
        self.deadline = utc_ns(2027, 1, 1)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def raw(self, body: bytes, retention_until_ns=None, dataset=D) -> str:
        return self.s.put_raw(body, provider="fixture", dataset=dataset, request={"body": body.decode()},
                              receipt_time_ns=INGESTED, media_type="application/json", retention_until_ns=retention_until_ns)

    def test_retention_facts_cover_exactly_the_current_observations_a_run_reads(self):
        early, late = self.raw(b"early", self.deadline - 10), self.raw(b"late", self.deadline)
        self.s.record(obs("1000", FILED_10K, source_ref=f"raw:{early}#Revenues"), INGESTED)
        self.s.record(obs("900", FILED_10KA, source_ref=f"raw:{late}#Revenues"), INGESTED)
        self.s.record(obs("7", FILED_10K, entity_id="ISSUER-2", source_ref="owner:fixture"), INGESTED)
        facts = self.s.retention_facts(D, ["ISSUER-1", "ISSUER-2"], FILED_10KA)
        self.assertEqual((facts["observations"], facts["raw_retention_until_ns"], facts["bounded_observations"],
                          facts["first_receipt_ns"]), (3, self.deadline - 10, 2, INGESTED))
        # a run whose cutoff precedes the restatement reads only the original
        self.assertEqual(self.s.retention_facts(D, ["ISSUER-1"], FILED_10KA - 1)["observations"], 1)
        # an entity not read, or data with no raw link, bounds nothing
        self.assertIsNone(self.s.retention_facts(D, ["ISSUER-2"], FILED_10KA)["raw_retention_until_ns"])
        self.assertEqual(self.s.retention_facts(D, ["NOBODY"], FILED_10KA)["observations"], 0)

    def test_enforce_retention_deletes_due_payloads_and_their_observations_and_emits_one_event(self):
        early, late = self.raw(b"early", self.deadline), self.raw(b"late", self.deadline + 10)
        self.s.record(obs("1000", FILED_10K, source_ref=f"raw:{early}#a"), INGESTED)
        # a silent correction of the same publication, from the later payload, supersedes the first version
        second, outcome = self.s.record(obs("1001", FILED_10K, source_ref=f"raw:{late}#a"), INGESTED + 10)
        self.assertEqual(outcome, "CORRECTED")
        self.assertTrue((self.s.raw_dir / early).exists())
        self.assertEqual(self.s.enforce_retention(self.deadline - 1), [])
        (ev,) = self.s.enforce_retention(self.deadline)
        self.assertEqual((ev["dataset"], ev["cause"], ev["deleted_at_ns"], ev["observations"], ev["raw_objects"]),
                         (D, "RETENTION_ELAPSED", self.deadline, 1, [early]))
        self.assertEqual(ev["entities"], {"ISSUER-1": {"observations": 1, "first_available_ns": FILED_10K,
                                                       "last_available_ns": FILED_10K}})
        self.assertFalse((self.s.raw_dir / early).exists())
        self.assertTrue((self.s.raw_dir / late).exists())
        rows = {r["obs_id"]: r for r in self.s.history("ISSUER-1", "Revenues", Q_END)}
        self.assertEqual(sorted(rows), [second])
        self.assertIsNone(rows[second]["supersedes"])            # it no longer links to the deleted version
        deleted = self.s.conn.execute("SELECT deleted_at_ns FROM raw_object WHERE sha256=?", (early,)).fetchone()[0]
        self.assertEqual(deleted, self.deadline)                  # the hash and request row is kept
        self.assertEqual(self.s.enforce_retention(self.deadline), [])          # done once
        with self.assertRaises(ValueError):                       # and never stored again
            self.raw(b"early", self.deadline + 100)
        self.assertEqual([e["event_id"] for e in self.s.deletion_events()], [ev["event_id"]])

    def test_a_registry_retention_period_deletes_observations_received_that_long_ago(self):
        from unknown.data.pit import NS_PER_DAY
        self.s.record(obs("1000", FILED_10K), INGESTED)
        self.s.record(obs("5", FILED_10K, dataset="other.dataset"), INGESTED)
        days = {D: 30, "other.dataset": None}
        self.assertEqual(self.s.enforce_retention(INGESTED + 30 * NS_PER_DAY - 1, retention_days=days), [])
        (ev,) = self.s.enforce_retention(INGESTED + 30 * NS_PER_DAY, retention_days=days)
        self.assertEqual((ev["dataset"], ev["observations"], ev["raw_objects"]), (D, 1, []))
        self.assertEqual(len(self.s.as_of("ISSUER-1", "Revenues", FILED_10K, dataset="other.dataset")), 1)
        with self.assertRaises(ValueError):
            self.s.enforce_retention(INGESTED, retention_days={D: -1})

    def test_withdraw_rights_by_entity_or_for_the_whole_dataset(self):
        p1, p2 = self.raw(b"issuer-1"), self.raw(b"issuer-2")
        self.raw(b"never-extracted")
        self.s.record(obs("1000", FILED_10K, source_ref=f"raw:{p1}#a"), INGESTED)
        self.s.record(obs("2000", FILED_10K, entity_id="ISSUER-2", source_ref=f"raw:{p2}#a"), INGESTED)
        with self.assertRaises(ValueError):
            self.s.withdraw_rights(D, reason="x", at_ns=INGESTED, entity_ids=[])
        with self.assertRaises(ValueError):
            self.s.withdraw_rights(D, reason=" ", at_ns=INGESTED)
        one = self.s.withdraw_rights(D, reason="rights to ISSUER-2 withdrawn", at_ns=INGESTED + 1, entity_ids=["ISSUER-2"])
        self.assertEqual((one["cause"], sorted(one["entities"]), one["raw_objects"]), ("RIGHTS_WITHDRAWN", ["ISSUER-2"], [p2]))
        self.assertIsNone(self.s.latest_as_of("ISSUER-2", "Revenues", FILED_10K))
        self.assertEqual(self.s.latest_as_of("ISSUER-1", "Revenues", FILED_10K)["value"], "1000")
        whole = self.s.withdraw_rights(D, reason="licence terminated", at_ns=INGESTED + 2)
        self.assertEqual((sorted(whole["entities"]), len(whole["raw_objects"])), (["ISSUER-1"], 2))  # and the unextracted
        again = self.s.withdraw_rights(D, reason="licence terminated (confirmed)", at_ns=INGESTED + 3)
        self.assertEqual((again["observations"], again["raw_objects"], again["entities"]), (0, [], {}))  # still recorded
        self.assertEqual([e["seq"] for e in self.s.deletion_events(datasets=[D])], [1, 2, 3])
        self.assertEqual([e["seq"] for e in self.s.deletion_events(after_seq=2)], [3])
        self.assertEqual(self.s.deletion_events(datasets=["other.dataset"]), [])

    def test_deletion_events_are_append_only_and_an_older_store_has_none(self):
        import sqlite3
        from pathlib import Path
        self.s.withdraw_rights(D, reason="fixture", at_ns=INGESTED)
        for sql in ("UPDATE deletion_event SET reason='x'", "DELETE FROM deletion_event"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.s.conn.execute(sql)
        old = Path(self.tmp.name) / "old.sqlite"
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE observation (obs_id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        ro = ResearchStore.open_readonly(old)
        self.assertEqual(ro.deletion_events(), [])
        ro.close()
