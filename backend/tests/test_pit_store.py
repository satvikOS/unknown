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
