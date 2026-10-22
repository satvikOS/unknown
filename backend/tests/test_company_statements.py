"""Standardized statements as of a decision time (CAP-CO-04, CAP-CO-05, CAP-CO-06). All data is FIXTURE.

Covers: concept precedence across an ASC 606 concept switch, 52-53 week fiscal years, quarters
derived from year-to-date values (Q4 = FY - 9M; cash-flow Q2/Q3 from YTD), TTM, restatements that
never leak backwards, audit-mode knowledge cutoff, a late filer whose TTM is null with a reason, a
fiscal-year transition excluded from TTM, and per-share items never summed.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import CIK, INGESTED_AT, LATE_FILER, accepted, fixture_home, ns  # noqa: E402

from unknown.data.fundamentals.periods import FiscalCalendar, duration_class  # noqa: E402
from unknown.data.fundamentals.statements import Fundamentals  # noqa: E402
from unknown.data.pit import Observation, ResearchStore  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

E = f"SEC-CIK-{CIK:010d}"


def fx(text: str) -> Fixed:
    return Fixed.parse(text)


class PeriodTests(unittest.TestCase):
    def test_duration_classes_cover_52_53_week_periods(self):
        self.assertEqual(duration_class(date(2025, 9, 28), date(2026, 10, 3)), "FY")      # 371 days
        self.assertEqual(duration_class(date(2024, 9, 29), date(2025, 9, 27)), "FY")      # 364 days
        self.assertEqual(duration_class(date(2026, 6, 28), date(2026, 10, 3)), "Q")       # 98 days
        self.assertEqual(duration_class(date(2025, 9, 28), date(2026, 3, 28)), "H1")
        self.assertEqual(duration_class(date(2025, 9, 28), date(2026, 6, 27)), "9M")
        self.assertEqual(duration_class(date(2024, 1, 1), date(2024, 1, 31)), "OTHER")

    def test_calendar_infers_quarters_from_ytd_boundaries(self):
        fy = (date(2024, 9, 29), date(2025, 9, 27))
        spans = [fy, (date(2024, 9, 29), date(2024, 12, 28)), (date(2024, 9, 29), date(2025, 3, 29)),
                 (date(2024, 9, 29), date(2025, 6, 28))]
        cal = FiscalCalendar(spans)
        self.assertEqual([q.label for q in cal.quarters], ["FY2025 Q1", "FY2025 Q2", "FY2025 Q3", "FY2025 Q4"])
        self.assertEqual(cal.quarters[-1].start, date(2025, 6, 29))


class StatementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.store = fixture_home()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def f(self, t_ns, **kw) -> Fundamentals:
        return Fundamentals(self.store, E, t_ns, **kw)

    def test_53_week_year_and_derived_fourth_quarter(self):
        f = self.f(ns("2026-12-01T00:00:00Z"))
        self.assertEqual([p.label for p in f.fiscal_years], ["FY2024", "FY2025", "FY2026"])
        fy26 = f.fiscal_years[-1]
        self.assertEqual(fy26.to_json()["days"], 371)
        q4 = f.quarters[-1]
        self.assertEqual((q4.label, q4.to_json()["days"]), ("FY2026 Q4", 98))
        c = f.cell("revenue", q4)
        self.assertEqual(c.value, fx("145000"))
        self.assertEqual(c.method, "DERIVED_QUARTER")
        self.assertIn("Q4 = FY - 9M YTD", c.formula)
        self.assertIn("DERIVED", c.all_flags())
        self.assertEqual(len(c.obs_ids()), 2)
        # per-share values are never derived by difference
        eps = f.cell("eps_diluted", q4)
        self.assertIsNone(eps.value)
        self.assertEqual(eps.missing_reason, "NOT_DERIVABLE")

    def test_cash_flow_quarters_derived_from_ytd(self):
        f = self.f(ns("2026-08-15T00:00:00Z"))
        q1, q2, q3 = [q for q in f.quarters if q.fiscal_year == 2026]
        self.assertEqual(f.cell("cfo", q1).method, "REPORTED")                  # Q1 is its own YTD
        c2, c3 = f.cell("cfo", q2), f.cell("cfo", q3)
        self.assertEqual((c2.value, c2.method), (fx("37500"), "DERIVED_QUARTER"))   # 30% of 125,000
        self.assertIn("Q2 = 6M YTD - Q1", c2.formula)
        self.assertEqual(c3.value, fx("40500"))
        self.assertEqual(f.cell("free_cash_flow", q2).value, fx("37500") - fx("10000"))

    def test_concept_precedence_across_concept_switch(self):
        before = self.f(ns("2024-12-01T00:00:00Z"))
        fy24 = before.fiscal_years[-1]
        c = before.cell("revenue", fy24)
        self.assertEqual((c.concept, c.value), ("us-gaap:SalesRevenueNet", fx("400000")))
        after = self.f(ns("2025-11-15T00:00:00Z"))
        c2 = after.cell("revenue", next(p for p in after.fiscal_years if p.fiscal_year == 2024))
        self.assertEqual((c2.concept, c2.value), ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", fx("400000")))

    def test_restatement_never_leaks_backwards(self):
        """CAP-CO-06: the 10-K/A is invisible before its acceptance; afterwards it replaces the original."""
        restate = accepted(CIK, "10-K/A", 2025, None)
        before, after = self.f(restate - 1), self.f(restate)
        fy_b = next(p for p in before.fiscal_years if p.fiscal_year == 2025)
        fy_a = next(p for p in after.fiscal_years if p.fiscal_year == 2025)
        self.assertEqual(before.cell("revenue", fy_b).value, fx("460000"))
        self.assertEqual(after.cell("revenue", fy_a).value, fx("450000"))
        q4_b = next(q for q in before.quarters if q.label == "FY2025 Q4")
        q4_a = next(q for q in after.quarters if q.label == "FY2025 Q4")
        self.assertEqual(before.cell("revenue", q4_b).value, fx("130000"))
        self.assertEqual(after.cell("revenue", q4_a).value, fx("120000"))
        # review fix: the derived Q4 says it absorbs an amended FY (restated FY - as-filed 9M YTD)
        self.assertNotIn("derived_from_amended_filing", before.cell("revenue", q4_b).all_flags())
        self.assertIn("derived_from_amended_filing", after.cell("revenue", q4_a).all_flags())
        self.assertNotIn("derived_from_amended_filing", after.cell("cfo", q4_a).all_flags())   # CFO was not amended
        hist = after.with_history(after.cell("revenue", fy_a))
        js = hist.to_json(CIK)
        self.assertTrue(js["restated"])
        self.assertEqual([h["value"] for h in js["history"]], ["460000"])
        self.assertEqual(js["sources"][0]["form"], "10-K/A")
        self.assertTrue(js["sources"][0]["sec_filing_index"].startswith("https://www.sec.gov/Archives/edgar/data/9000001/"))
        # no fact used anywhere is available after the decision time (no look-ahead)
        for key, per in before.index.items():
            for concept in per.values():
                for fact in concept.values():
                    self.assertLessEqual(fact.available_ns, restate - 1)

    def test_audit_mode_knowledge_cutoff(self):
        """Before ingestion this system knew nothing, whatever the publication times."""
        audit = self.f(ns("2026-11-30T00:00:00Z"), knowledge_cutoff_ns=INGESTED_AT - 1)
        self.assertEqual(audit.fact_count, 0)
        self.assertEqual(self.f(ns("2026-11-30T00:00:00Z"), knowledge_cutoff_ns=INGESTED_AT).fact_count,
                         self.f(ns("2026-11-30T00:00:00Z")).fact_count)

    def test_coverage_respects_the_knowledge_cutoff(self):
        """Regression (review fix): the coverage report uses the same PIT filter as the cells; in audit mode before
        ingestion it reports nothing, not concepts this system had not yet ingested."""
        t = ns("2026-11-30T00:00:00Z")
        self.assertEqual(self.f(t, knowledge_cutoff_ns=INGESTED_AT - 1).coverage()["reported_concepts"], 0)
        self.assertGreater(self.f(t).coverage()["reported_concepts"], 10)
        early = self.f(accepted(CIK, "10-K", 2024, None) - 1).coverage()
        self.assertEqual(early["reported_concepts"], 0)                      # nothing accepted yet

    def test_ttm_cell_is_anchored_on_its_own_quarter(self):
        """Regression (review fix): a TTM period whose end is not a known quarter end is missing, never silently the
        TTM of the latest quarter."""
        from unknown.data.fundamentals.periods import Period
        f = self.f(ns("2026-08-15T00:00:00Z"))
        odd = f.cell("revenue", Period("TTM", date(2026, 5, 1)))
        self.assertIsNone(odd.value)
        self.assertEqual(odd.missing_reason, "NOT_REPORTED")
        self.assertIn("no fiscal quarter ends on 2026-05-01", odd.detail)
        q2 = next(q for q in f.quarters if q.label == "FY2026 Q2")
        self.assertEqual(f.cell("revenue", Period("TTM", q2.end)).value, f.ttm("revenue", q2).value)
        self.assertNotEqual(f.ttm("revenue", q2).value, f.ttm("revenue").value)   # Q2 window differs from the latest

    def test_ttm_sum_uses_four_contiguous_quarters(self):
        f = self.f(ns("2026-08-15T00:00:00Z"))
        self.assertEqual(f.latest_quarter().label, "FY2026 Q3")
        t = f.ttm("revenue")
        self.assertEqual((t.method, t.value), ("TTM_SUM", fx("495000")))           # 120,000 restated Q4 + 115 + 125 + 135
        self.assertEqual([c.period.label for c in t.components], ["FY2025 Q4", "FY2026 Q1", "FY2026 Q2", "FY2026 Q3"])
        self.assertEqual(f.ttm("total_assets").method, "LATEST_INSTANT")
        eps = f.ttm("eps_diluted")
        self.assertEqual((eps.value, eps.missing_reason), (None, "NOT_APPLICABLE"))
        year_end = self.f(ns("2026-12-01T00:00:00Z"))
        self.assertEqual(year_end.ttm("revenue").value, fx("520000"))             # window = the 53-week FY2026

    def test_late_filer_ttm_is_null_with_reason(self):
        """CAP-CO-05: a quarter missing at the as-of time makes TTM null, never a shorter or stale window."""
        f = Fundamentals(self.store, f"SEC-CIK-{LATE_FILER:010d}", ns("2026-02-01T00:00:00Z"))
        self.assertEqual(f.latest_quarter().label, "FY2026 Q1")
        t = f.ttm("revenue")
        self.assertIsNone(t.value)
        self.assertEqual(t.missing_reason, "NON_CONTIGUOUS")
        self.assertIn("not available at the as-of time", t.detail)
        later = Fundamentals(self.store, f"SEC-CIK-{LATE_FILER:010d}", ns("2026-02-16T00:00:00Z"))
        self.assertEqual(later.ttm("revenue").value, fx("100000") + fx("110000") + fx("120000") + fx("130000")
                         - fx("100000") + fx("115000"))

    def test_statement_output_carries_lineage(self):
        f = self.f(ns("2026-08-15T00:00:00Z"))
        s = f.statement("Q", 4)
        self.assertEqual(s["mapping"], "std-map@1")
        self.assertEqual([c["label"] for c in s["columns"]], ["FY2026 Q3", "FY2026 Q2", "FY2026 Q1", "FY2025 Q4"])
        rev = next(r for r in s["statements"]["income"] if r["item"] == "revenue")
        cell = rev["cells"][0]
        self.assertEqual(cell["value"], "135000")
        self.assertTrue(cell["obs_ids"])
        self.assertEqual(cell["rule"], "std-map@1:revenue")
        self.assertIsNotNone(cell["latest_input_available_time"])
        missing = next(r for r in s["statements"]["balance"] if r["item"] == "preferred_stock")["cells"][0]
        self.assertIsNone(missing["value"])
        self.assertEqual(missing["missing_reason"], "NOT_REPORTED")
        ttm = f.statement("TTM", 1)
        self.assertEqual(ttm["columns"][0]["start"], "2025-06-29")
        self.assertGreater(s["coverage"]["reported_concepts"], 10)
        with self.assertRaises(ValueError):
            f.statement("XX", 4)


class TransitionPeriodTests(unittest.TestCase):
    """A fiscal-year change (Dec -> Jun year end) with a six-month transition period (FIXTURE)."""

    def setUp(self):
        self.s = ResearchStore(":memory:")
        t = ns("2025-09-01T00:00:00Z")
        periods = [("2023-01-01", "2023-03-31", 10), ("2023-04-01", "2023-06-30", 10), ("2023-07-01", "2023-09-30", 10),
                   ("2023-01-01", "2023-12-31", 40), ("2024-01-01", "2024-03-31", 11), ("2024-04-01", "2024-06-30", 11),
                   ("2024-01-01", "2024-06-30", 22), ("2024-07-01", "2024-09-30", 12), ("2024-10-01", "2024-12-31", 12),
                   ("2025-01-01", "2025-03-31", 12), ("2024-07-01", "2025-06-30", 48), ("2024-07-01", "2024-12-31", 24),
                   ("2024-07-01", "2025-03-31", 36)]
        for i, (a, b, v) in enumerate(periods):
            self.s.record(Observation(dataset="sec.companyfacts", entity_id="SEC-CIK-0009000099", field="us-gaap:Revenues",
                                      unit="USD", currency="USD", period_start=a, period_end=b, period_kind="FY",
                                      value=Fixed(v), receipt_time_ns=t, available_time_ns=t - 1, publication_id=f"fixture-{i}",
                                      source_ref="FIXTURE"), t)

    def tearDown(self):
        self.s.close()

    def test_transition_quarters_excluded_from_ttm(self):
        f = Fundamentals(self.s, "SEC-CIK-0009000099", ns("2025-09-01T00:00:00Z"))
        self.assertTrue(any(x.startswith("fiscal_calendar_break") for x in f.calendar.flags))
        trans = [q for q in f.quarters if "transition_period" in q.flags]
        self.assertEqual([(q.start.isoformat(), q.end.isoformat()) for q in trans], [("2024-01-01", "2024-03-31"), ("2024-04-01", "2024-06-30")])
        mid = next(q for q in f.quarters if q.end == date(2025, 3, 31))
        self.assertEqual(f.ttm("revenue", mid).missing_reason, "FISCAL_TRANSITION")
        last = f.quarters[-1]
        self.assertEqual(last.end, date(2025, 6, 30))
        self.assertEqual(f.ttm("revenue", last).value, Fixed(48))
        from unknown.data.fundamentals.metrics import MetricEngine
        g = MetricEngine(f).compute("revenue_growth_fy")
        self.assertEqual(g.status, "MISSING")
        self.assertIn("transition", g.reason)


class TtmAlternativeFormulaTests(unittest.TestCase):
    """CAP-CO-05 alternative TTM = FY_last + YTD_current - YTD_prior, used only when discrete quarters are unavailable
    (here: operating cash flow reported only as half-year YTD and full year; FIXTURE values)."""

    def test_ttm_alt_is_flagged(self):
        s = ResearchStore(":memory:")
        t = ns("2026-09-01T00:00:00Z")
        facts = [("Revenues", "2025-01-01", "2025-12-31", 400), ("Revenues", "2025-01-01", "2025-03-31", 100),
                 ("Revenues", "2025-04-01", "2025-06-30", 100), ("Revenues", "2025-07-01", "2025-09-30", 100),
                 ("Revenues", "2025-01-01", "2025-06-30", 200), ("Revenues", "2026-01-01", "2026-03-31", 110),
                 ("Revenues", "2026-04-01", "2026-06-30", 110), ("Revenues", "2026-01-01", "2026-06-30", 220),
                 ("NetCashProvidedByUsedInOperatingActivities", "2025-01-01", "2025-12-31", 400),
                 ("NetCashProvidedByUsedInOperatingActivities", "2025-01-01", "2025-06-30", 190),
                 ("NetCashProvidedByUsedInOperatingActivities", "2026-01-01", "2026-06-30", 220)]
        for i, (concept, a, b, v) in enumerate(facts):
            s.record(Observation(dataset="sec.companyfacts", entity_id="SEC-CIK-0009000098", field=f"us-gaap:{concept}", unit="USD",
                                 currency="USD", period_start=a, period_end=b, period_kind="FY", value=Fixed(v), receipt_time_ns=t,
                                 available_time_ns=t - 1, publication_id=f"fixture-{i}", source_ref="FIXTURE"), t)
        f = Fundamentals(s, "SEC-CIK-0009000098", t)
        self.assertEqual(f.latest_quarter().end, date(2026, 6, 30))
        cfo = f.ttm("cfo")
        self.assertEqual((cfo.method, cfo.value), ("TTM_ALT", Fixed(430)))       # 400 + 220 - 190
        self.assertIn("TTM_ALT_FORMULA", cfo.flags)
        self.assertEqual(f.ttm("revenue").method, "TTM_SUM")
        s.close()


if __name__ == "__main__":
    unittest.main()
