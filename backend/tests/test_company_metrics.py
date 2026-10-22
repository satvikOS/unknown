"""Ratios, EV bridge and multiples with lineage (CAP-CO-07; DAT-05). All data and prices are FIXTURE values."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import CIK, fixture_home, ns  # noqa: E402

from unknown.data.fundamentals.metrics import (FORMULAS, MetricEngine, PriceInput, explain_record, persist,  # noqa: E402
                                               record_price_input)
from unknown.data.fundamentals.statements import Fundamentals  # noqa: E402
from unknown.domain.fixed import Fixed, Rounding  # noqa: E402

E = f"SEC-CIK-{CIK:010d}"
T = ns("2026-08-15T00:00:00Z")
PRICE_TIME = ns("2026-08-14T20:00:00Z")
QUOTIENTS = {"gross_margin", "operating_margin", "ebitda_margin", "net_margin", "fcf_margin", "effective_tax_rate",
             "debt_to_equity", "net_debt_to_ebitda", "interest_coverage", "current_ratio"}


def fixture_price(value="50.00", t=PRICE_TIME, ccy="USD", obs_id=None) -> PriceInput:
    return PriceInput(Fixed.parse(value), ccy, "FIXTURE price (test only, not market data)", t, obs_id)


class MetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.store = fixture_home()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def engine(self, price=None, entity=E, sic="3571", t=T) -> MetricEngine:
        return MetricEngine(Fundamentals(self.store, entity, t), price, sic=sic)

    def test_library_has_at_least_fifteen_documented_ratios(self):
        self.assertGreaterEqual(len([f for f in FORMULAS.values() if f.unit in ("ratio", "x")]), 15)
        self.assertTrue(all(f.ref.endswith("@1") and f.expression for f in FORMULAS.values()))

    def test_ratios_recompute_from_displayed_inputs(self):
        """CAP-CO-07: recomputing any ratio from its displayed inputs reproduces the stored value."""
        eng = self.engine(fixture_price())
        checked = 0
        for name in QUOTIENTS:
            r = eng.compute(name)
            self.assertEqual(r.status, "OK", f"{name}: {r.reason}")
            num, den = (Fixed.parse(i["value"]) for i in r.to_json()["inputs"][:2])
            self.assertEqual(r.value, num.div(den, 10, Rounding.HALF_EVEN), name)
            checked += 1
        self.assertEqual(checked, len(QUOTIENTS))
        gm = eng.compute("gross_margin")
        self.assertEqual(gm.value, Fixed.parse("0.3878787879"))      # (42,000 + 150,000) / 495,000, hand-computed
        self.assertEqual(eng.compute("revenue_ttm").value, Fixed(495000))

    def test_returns_use_average_balances(self):
        eng = self.engine()
        roe = eng.compute("roe")
        self.assertEqual(roe.status, "OK")
        js = roe.to_json(CIK, detail=True)
        roles = [i["role"] for i in js["inputs"]]
        self.assertEqual(roles, ["net_income_TTM", "equity_parent (begin)", "equity_parent (end)"])
        ni, b, e = (Fixed.parse(i["cell"]["value"]) for i in js["inputs"])
        self.assertEqual(roe.value, ni.div((b + e).div(2, 1, Rounding.UNNECESSARY), 10, Rounding.HALF_EVEN))
        self.assertEqual(eng.compute("roic").status, "OK")
        self.assertEqual(eng.compute("fcff_ttm").status, "OK")

    def test_negative_earnings_pe_is_not_meaningful(self):
        loss = self.engine(fixture_price("5.00"), entity="SEC-CIK-0009000004")
        pe = loss.compute("pe_ttm")
        self.assertEqual((pe.status, pe.value), ("NM", None))
        self.assertIn("net income TTM <= 0", pe.reason)
        self.assertEqual(loss.compute("pe_fy").status, "NM")
        self.assertEqual(loss.compute("net_margin").status, "OK")              # a negative margin is meaningful
        self.assertTrue(loss.compute("net_margin").value.is_negative())

    def test_ev_bridge_lists_missing_components_and_is_incomplete(self):
        eng = self.engine(fixture_price())
        mc = eng.compute("market_cap")
        self.assertEqual(mc.value, Fixed.parse("50.00") * Fixed(10000))
        ev = eng.compute("ev_standard")
        self.assertEqual(ev.status, "INCOMPLETE")
        comps = {b["component"]: b for b in ev.bridge}
        self.assertEqual(comps["preferred_stock"]["status"], "MISSING")
        self.assertIsNone(comps["preferred_stock"]["value"])
        self.assertEqual(comps["noncontrolling_interest"]["status"], "MISSING")
        total = sum((Fixed.parse(b["contribution"]) for b in ev.bridge if b["status"] == "OK"), Fixed(0))
        self.assertEqual(ev.value, total)
        self.assertEqual(ev.value, mc.value + Fixed(100000) - Fixed.parse(comps["cash_and_equivalents"]["value"]) - Fixed(20000))
        self.assertEqual(eng.compute("ev_to_ebitda").status, "INCOMPLETE")      # inherits the flag
        self.assertEqual(eng.compute("pe_ttm").status, "OK")

    def test_price_rules(self):
        with self.assertRaises(ValueError):                                  # look-ahead price
            self.engine(fixture_price(t=T + 1))
        no_price = self.engine().compute("pe_ttm")
        self.assertEqual(no_price.status, "NO_PRICE")
        eur = self.engine(fixture_price(ccy="EUR")).compute("market_cap")
        self.assertEqual(eur.status, "MISSING")
        self.assertIn("FX conversion is not applied", eur.reason)
        stale = self.engine(fixture_price(t=T - 10 * 86_400 * 10**9)).compute("pe_ttm")
        self.assertIn("stale_price", stale.flags)
        with self.assertRaises(ValueError):
            PriceInput(Fixed.parse("-1"), "USD", "x", T)

    def test_financial_sector_suppresses_ev_metrics(self):
        eng = self.engine(fixture_price(), sic="6021")
        self.assertEqual(eng.compute("ev_standard").status, "SUPPRESSED")
        self.assertEqual(eng.compute("ev_to_ebitda").status, "SUPPRESSED")
        self.assertEqual(eng.compute("pe_ttm").status, "OK")

    def test_show_calculation(self):
        d = self.engine(fixture_price()).explain("net_margin")
        self.assertEqual(d["formula"], "net_margin@1")
        self.assertEqual(d["expression"], "net_income_TTM / revenue_TTM")
        num = d["inputs"][0]["cell"]
        self.assertEqual(num["method"], "TTM_SUM")
        self.assertEqual(len(num["components"]), 4)
        q4 = num["components"][0]
        self.assertEqual(q4["method"], "DERIVED_QUARTER")
        self.assertTrue(any(s["form"] == "10-K/A" for s in q4["sources"]))    # restated FY feeds the derived Q4
        self.assertTrue(d["source_dates"])
        self.assertTrue(d["input_obs_ids"])

    def test_persist_with_record_metric_and_explain_record(self):
        price = record_price_input(self.store, E, Fixed.parse("50.00"), "USD", "FIXTURE price (test only)", PRICE_TIME, T)
        eng = self.engine(price)
        results = [eng.compute(n) for n in ("gross_margin", "pe_ttm", "ev_standard", "market_cap")]
        out = persist(self.store, results, E, T, T + 5)
        self.assertTrue(all(o["persisted"] for o in out))
        pe_id = next(o["metric_id"] for o in out if o["metric"] == "pe_ttm")
        rec = explain_record(self.store, pe_id)
        self.assertEqual(rec["formula"], "pe_ttm@1")
        self.assertEqual(rec["value"], eng.compute("pe_ttm").value.to_str())
        datasets = {i["dataset"] for i in rec["inputs"]}
        self.assertEqual(datasets, {"sec.companyfacts", "owner.price_input"})
        self.assertEqual(rec["missing_input_ids"], [])
        ev_id = next(o["metric_id"] for o in out if o["metric"] == "ev_standard")
        ev_rec = explain_record(self.store, ev_id)
        self.assertIsNone(ev_rec["value"])                                   # INCOMPLETE is never stored as a value
        self.assertEqual(ev_rec["missing_reason"], "NOT_REPORTED")
        unpersisted = persist(self.store, [self.engine(fixture_price()).compute("pe_ttm")], E, T, T)
        self.assertEqual(unpersisted[0]["persisted"], False)
        with self.assertRaises(LookupError):
            explain_record(self.store, 10**9)


class YearAgoComparisonTests(unittest.TestCase):
    """Regression (review fix): "four quarters earlier" is found by date, not by list position. FIXTURE values.

    The issuer never filed its FY2025 Q3 10-Q (no 9M YTD, no Q3 3-month value), so FY2025 Q3 and Q4 are absent from the
    fiscal calendar. As of 2026-06-01 the latest quarter is FY2026 Q1 (to 2026-03-31). The year-ago quarter is FY2025
    Q1 (to 2025-03-31); list position 4 back would be FY2024 Q3 (to 2024-09-30), an 18-month comparison."""

    E = "SEC-CIK-0009000097"

    def setUp(self):
        from unknown.data.pit import Observation, ResearchStore
        self.s = ResearchStore(":memory:")
        t = ns("2026-06-01T00:00:00Z")
        revenue = [("2023-01-01", "2023-12-31", 400), ("2023-01-01", "2023-09-30", 300),
                   ("2024-01-01", "2024-12-31", 440), ("2024-01-01", "2024-03-31", 100), ("2024-04-01", "2024-06-30", 110),
                   ("2024-01-01", "2024-06-30", 210), ("2024-07-01", "2024-09-30", 110), ("2024-01-01", "2024-09-30", 320),
                   ("2025-01-01", "2025-03-31", 120), ("2025-04-01", "2025-06-30", 125), ("2025-01-01", "2025-06-30", 245),
                   ("2025-01-01", "2025-12-31", 520), ("2026-01-01", "2026-03-31", 150)]
        equity = [("2024-09-30", 1000), ("2024-12-31", 1500), ("2025-03-31", 2000), ("2025-12-31", 2500), ("2026-03-31", 3000)]
        rows = [("us-gaap:Revenues", a, b, v) for a, b, v in revenue] + [("us-gaap:StockholdersEquity", None, b, v) for b, v in equity]
        for i, (field, a, b, v) in enumerate(rows):
            self.s.record(Observation(dataset="sec.companyfacts", entity_id=self.E, field=field, unit="USD", currency="USD",
                                      period_start=a, period_end=b, period_kind="FIXTURE" if a else "INSTANT", value=Fixed(v),
                                      receipt_time_ns=t, available_time_ns=t - 1, publication_id=f"fixture-{i}",
                                      source_ref="FIXTURE"), t)
        self.f = Fundamentals(self.s, self.E, t)

    def tearDown(self):
        self.s.close()

    def test_yoy_growth_compares_with_the_quarter_one_year_earlier(self):
        q0 = self.f.latest_quarter()
        self.assertEqual(q0.end.isoformat(), "2026-03-31")
        self.assertEqual(self.f.quarters_back(q0, 4).end.isoformat(), "2024-09-30")      # list position: the old defect
        self.assertEqual(self.f.year_ago_quarter(q0).end.isoformat(), "2025-03-31")
        g = MetricEngine(self.f).compute("revenue_growth_yoy")
        self.assertEqual(g.status, "OK")
        cur, prior = (i.cell for i in g.inputs)
        self.assertEqual((cur.method, cur.value), ("TTM_ALT", Fixed(550)))           # 520 + 150 - 120
        self.assertEqual((prior.method, prior.period.end.isoformat(), prior.value), ("TTM_SUM", "2025-03-31", Fixed(460)))
        self.assertEqual(g.value, Fixed(550).div(Fixed(460), 10, Rounding.HALF_EVEN) - 1)   # 0.1956521739, not 550/420 - 1

    def test_average_balance_begins_one_year_earlier(self):
        avg, inputs, flags = MetricEngine(self.f).avg_bal("equity_parent")
        self.assertEqual([i.cell.period.end.isoformat() for i in inputs], ["2025-03-31", "2026-03-31"])
        self.assertEqual((avg, flags), (Fixed(2500), []))

    def test_no_year_ago_quarter_means_missing_not_older(self):
        q = next(q for q in self.f.quarters if q.end.isoformat() == "2024-03-31")   # nothing ends ~2023-03-31
        self.assertIsNone(self.f.year_ago_quarter(q))
        eng = MetricEngine(self.f)
        eng.q0 = q
        g = eng.compute("revenue_growth_yoy")
        self.assertEqual(g.status, "MISSING")
        self.assertIn("no fiscal quarter ends one year before 2024-03-31", g.reason)


if __name__ == "__main__":
    unittest.main()
