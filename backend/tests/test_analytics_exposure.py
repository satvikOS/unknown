"""Exposure and concentration by instrument, owner, sector and currency (CAP-PORT-03).

The issuer and SIC code used for the sector test live in a temporary point-in-time research
store created by the test: a FIXTURE issuer, not EDGAR data."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import F, SECOND_INSTRUMENT, booted, buy, quotes, register_second, ro  # noqa: E402
from support import ACCOUNT, FIXTURE_INSTRUMENT, T0  # noqa: E402

from unknown.analytics.common import SYSTEM  # noqa: E402
from unknown.analytics.exposure import SicClassifier, exposure_report, sic_division  # noqa: E402
from unknown.analytics.holdings import holdings  # noqa: E402
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.domain.fixed import Fixed, Rounding  # noqa: E402
from unknown.domain.instruments import Instrument  # noqa: E402

ISSUER_INSTRUMENT = Instrument.from_json({**FIXTURE_INSTRUMENT.to_json(), "instrument_id": "FIX-EQ-3", "symbol": "TSTC",
                                          "name": "Test issuer common (fixture)", "attributes": {"sec.cik": 999001}})


def research_store(test: unittest.TestCase, *, alias_from_ns: int) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "research.sqlite"
    rs = ResearchStore(path)
    rs.upsert_entity("SEC-CIK-0000999001", "issuer", "FIXTURE ISSUER (test only)", {"cik": 999001, "sic": "3571"})
    rs.add_alias("ticker:Nasdaq", "TSTC", "SEC-CIK-0000999001", alias_from_ns, "fixture")
    rs.close()
    return path


class SicTest(unittest.TestCase):
    def test_division_ranges(self):
        self.assertEqual(sic_division("3571"), "D Manufacturing")
        self.assertEqual(sic_division("6022"), "H Finance, Insurance and Real Estate")
        self.assertEqual(sic_division("7372"), "I Services")
        self.assertIsNone(sic_division("abc"))
        self.assertIsNone(sic_division("1800"))  # major group 18 is unassigned

    def test_alias_recorded_after_the_decision_time_cannot_classify(self):
        path = research_store(self, alias_from_ns=T0 + 1_000)
        clf = SicClassifier(path)
        self.addCleanup(clf.close)
        early = clf.classify("X", "TSTC", {}, T0)
        later = clf.classify("X", "TSTC", {}, T0 + 1_000)
        self.assertEqual((early["sector"], early["status"]), ("unknown", "unclassified"))
        self.assertIn("valid at the decision time", early["reason"])
        self.assertEqual((later["sector"], later["sic"]), ("D Manufacturing", "3571"))

    def test_missing_store_is_explicit(self):
        r = SicClassifier("/nonexistent/research.sqlite").classify("X", "TSTC", {}, T0)
        self.assertEqual(r["sector"], "unknown")
        self.assertIn("not found", r["reason"])


class ExposureReportTest(unittest.TestCase):
    def setUp(self):
        self.h = booted(self)
        h = self.h
        register_second(h)
        assert h.owner_cmd("register_instrument", instrument=ISSUER_INSTRUMENT.to_json(),
                           mappings=[{"provider": "simulated", "external_id": "FIX-EQ-3"}]).accepted
        buy(h, "S1", "B1", "4", "10.00", bid="10.00", ask="10.02")
        buy(h, "S1", "B2", "3", "8.00", instrument_id="FIX-EQ-2", bid="8.00", ask="8.02")
        buy(h, "S1", "B3", "2", "5.00", instrument_id="FIX-EQ-3", bid="5.00", ask="5.02")
        self.assertEqual(h.verify(), [])
        self.conn = ro(self, h)
        self.rep = holdings(self.conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)

    def test_groupings_sum_to_totals_and_unclassified_bucket_is_explicit(self):
        clf = SicClassifier(research_store(self, alias_from_ns=T0))
        self.addCleanup(clf.close)
        r = exposure_report(self.rep, classifier=clf)
        usd = r["currencies"]["USD"]
        self.assertEqual((usd["long"], usd["short"], usd["gross"], usd["net"]),
                         ("74.000000", "0.000000", "74.000000", "74.000000"))   # 40 + 24 + 10
        for grouping in ("by_instrument", "by_owner", "by_sector"):
            total = sum((Fixed.parse(g["gross"]) for g in usd[grouping]), Fixed(0))
            self.assertEqual(total, Fixed.parse(usd["gross"]), grouping)
        sectors = {g["sector"]: g["gross"] for g in usd["by_sector"]}
        self.assertEqual(sectors, {"unknown": "64.000000", "D Manufacturing": "10.000000"})
        self.assertEqual(usd["coverage"]["sector_classified_share_of_gross"], "0.13513514")
        reasons = {c["instrument_id"]: c["reason"] for c in r["classification"]["instruments"]}
        self.assertEqual(reasons["FIX-EQ-1"], "fixture instrument: no issuer")
        self.assertTrue(r["fixture_data"])

    def test_concentration_metrics(self):
        r = exposure_report(self.rep)
        c = r["currencies"]["USD"]["concentration"]
        shares = [40 / 74, 24 / 74, 10 / 74]
        hhi = sum(s * s for s in shares)
        self.assertAlmostEqual(c["hhi"], hhi, places=12)
        self.assertAlmostEqual(c["effective_n"], 1 / hhi, places=12)
        self.assertAlmostEqual(c["top_5_share"], 1.0, places=12)
        self.assertEqual(r["currencies"]["USD"]["by_instrument"][0]["instrument_id"], "FIX-EQ-1")  # largest first

    def test_both_percentage_forms_are_labelled(self):
        usd = exposure_report(self.rep)["currencies"]["USD"]
        self.assertEqual(usd["ibkr_form"]["long_pct"], "1.00000000")
        self.assertIn("IBKR white-paper form", usd["ibkr_form"]["note"])
        self.assertEqual(usd["denominator"]["name"], "scope NAV (nav_marked)")
        self.assertEqual(usd["gross_of_denominator"],
                         F("74").div(Fixed.parse(usd["denominator"]["value"]), 8, Rounding.HALF_EVEN).to_str())

    def test_unpriced_positions_lower_coverage_instead_of_counting_zero(self):
        q = quotes(self.h)
        del q[SECOND_INSTRUMENT.instrument_id]
        rep = holdings(self.conn, ACCOUNT, q, now_ns=self.h.clock.now_ns(), scope=SYSTEM)
        usd = exposure_report(rep)["currencies"]["USD"]
        self.assertEqual(usd["gross"], "50.000000")
        self.assertEqual(usd["unpriced"][0]["instrument_id"], "FIX-EQ-2")
        self.assertIsNone(usd["denominator"]["value"])  # scope NAV needs every position priced
        self.assertEqual(usd["coverage"]["unpriced_positions"], 1)


if __name__ == "__main__":
    unittest.main()
