"""DCF (book eq. 17.1), WACC, sensitivity surface, scenario range and model guards (CAP-CO-11). FIXTURE data."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import CIK, fixture_home, ns  # noqa: E402

from unknown.data.fundamentals import dcf  # noqa: E402
from unknown.data.fundamentals.metrics import PriceInput  # noqa: E402
from unknown.data.fundamentals.statements import Fundamentals  # noqa: E402
from unknown.data.fundamentals.valuation import ModelRefused, dcf_model  # noqa: E402
from unknown.data.pit import Observation  # noqa: E402
from unknown.data.sources import treasury  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

E = f"SEC-CIK-{CIK:010d}"
T = ns("2026-08-15T00:00:00Z")


class DcfMathTests(unittest.TestCase):
    def test_equation_17_1_hand_computed(self):
        r = dcf.enterprise_value(["100", "110", "121"], "0.10", "0.02")
        w, g = Fraction(1, 10), Fraction(1, 50)
        flows = [Fraction(100), Fraction(110), Fraction(121)]
        pv = sum(f / (1 + w) ** t for t, f in enumerate(flows, start=1))
        tv = flows[-1] * (1 + g) / (w - g)
        ev = pv + tv / (1 + w) ** 3
        self.assertEqual(Decimal(r["enterprise_value"]), round(Decimal(ev.numerator) / Decimal(ev.denominator), 2))
        self.assertEqual(r["terminal_value"], "1542.75")
        self.assertEqual(r["fcff_next"], "123.42")
        self.assertEqual(r["formula"], "dcf_fcff_gordon@1")
        self.assertEqual(dcf.enterprise_value(["100", "110", "121"], "0.10", "0.02"), r)    # exactly reproducible

    def test_wacc_must_exceed_g(self):
        for g in ("0.10", "0.11"):
            with self.assertRaises(dcf.DcfInputError):
                dcf.enterprise_value(["1"], "0.10", g)
        with self.assertRaises(dcf.DcfInputError):
            dcf.enterprise_value([1.5], "0.1", "0.02")      # floats refused
        with self.assertRaises(dcf.DcfInputError):
            dcf.enterprise_value([], "0.1", "0.02")

    def test_wacc_build_up(self):
        w = dcf.wacc("0.04", "1.2", "0.05", "0.05", "0.21", "0.8", "0.2")
        self.assertEqual(Decimal(w["cost_of_equity"]), Decimal("0.10"))
        self.assertEqual(Decimal(w["wacc"]), Decimal("0.0879"))
        with self.assertRaises(dcf.DcfInputError):
            dcf.wacc("0.04", "1", "0.05", "0.05", "0.21", "0.8", "0.3")

    def test_sensitivity_marks_invalid_cells(self):
        s = dcf.sensitivity(["100", "100"], "0.03", "0.02")
        self.assertEqual(s["wacc_axis"], ["0.01", "0.02", "0.03", "0.04", "0.05"])
        self.assertEqual(s["g_axis"], ["0.010", "0.015", "0.020", "0.025", "0.030"])
        self.assertEqual(s["invalid_cells"], 9)
        self.assertFalse(s["grid"][0][0]["valid"])
        self.assertEqual(s["grid"][0][0]["reason"], "WACC <= g")
        self.assertTrue(s["grid"][4][0]["valid"])
        center = dcf.enterprise_value(["100", "100"], "0.03", "0.02")["enterprise_value"]
        self.assertEqual(s["grid"][2][2]["enterprise_value"], center)

    def test_scenarios_and_equity_bridge(self):
        bridge = [{"component": "total_debt", "sign": -1, "value": "50"}, {"component": "preferred_stock", "sign": -1, "value": None},
                  {"component": "cash_and_equivalents", "sign": 1, "value": "20"}]
        e = dcf.equity_value(Decimal("1000"), bridge, "10")
        self.assertEqual((e["equity_value"], e["per_share"], e["status"]), ("970.00", "97.0000", "INCOMPLETE"))
        self.assertEqual(e["missing_components"], ["preferred_stock"])
        sc = dcf.scenarios(["100", "105"], "0.09", "0.02", bridge=bridge, shares="10")
        names = [r["name"] for r in sc["scenarios"]]
        self.assertEqual(names, ["low", "base", "high"])
        vals = [Decimal(r["per_share"]) for r in sc["scenarios"]]
        self.assertTrue(vals[0] < vals[1] < vals[2])
        self.assertEqual(Decimal(sc["range"]["min"]), vals[0])


class DcfModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.store = fixture_home()
        t = ns("2026-08-14T22:30:00Z")
        cls.store.record(Observation(dataset=treasury.DATASET, entity_id=treasury.ENTITY, field="par_yield:10Y", unit="percent",
                                     value=Fixed.parse("4.25"), period_end="2026-08-14", period_kind="DAY", receipt_time_ns=t,
                                     available_time_ns=t, publication_id="ust-par:2026-08-14", source_ref="FIXTURE"), t)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def f(self):
        return Fundamentals(self.store, E, T)

    def test_model_with_treasury_rf_and_market_weights(self):
        rf = treasury.point(self.store, "10Y", T)
        self.assertEqual(rf["value_pct"], "4.25")
        price = PriceInput(Fixed.parse("50.00"), "USD", "FIXTURE price (test only)", ns("2026-08-14T20:00:00Z"))
        m = dcf_model(self.f(), sic="3571", g="0.02", fcff_growth="0.03", years=5, rf_point=rf, beta="1.1", erp="0.05", kd="0.05",
                      price=price)
        self.assertEqual(m["inputs"]["rf"]["value"], "0.0425")
        self.assertIn("US Treasury par yield 10Y", m["inputs"]["rf"]["source"])
        self.assertEqual(m["inputs"]["weights"]["source"], "market values: market cap / (market cap + total debt)")
        self.assertIn("effective tax rate TTM", m["wacc_build_up"]["sources"]["tax"])
        # Regression (review fix): an unsupplied CRP is a labelled default, not an "owner input"
        self.assertEqual(m["wacc_build_up"]["sources"]["crp"], "default 0 (no country risk premium supplied)")
        self.assertEqual(m["wacc_build_up"]["inputs"]["crp"], "0")
        with_crp = dcf_model(self.f(), sic="3571", g="0.02", fcff=["10"], rf="0.04", beta="1", erp="0.05", crp="0.01", kd="0.05",
                             we="0.8", wd="0.2", tax="0.21")
        self.assertEqual(with_crp["wacc_build_up"]["sources"]["crp"], "owner input")
        self.assertEqual(Decimal(with_crp["wacc_build_up"]["cost_of_equity"]), Decimal("0.10"))
        self.assertEqual(len(m["inputs"]["fcff"]["values"]), 5)
        self.assertEqual(m["equity_bridge"]["status"], "INCOMPLETE")       # preferred / NCI not reported by the fixture filer
        self.assertIn("per_share", m["equity_bridge"])
        center = m["sensitivity"]["grid"][2][2]
        self.assertEqual(center["per_share"], m["equity_bridge"]["per_share"])
        self.assertEqual(Decimal(m["sensitivity"]["wacc_axis"][2]), Decimal(m["wacc_build_up"]["wacc"]))
        self.assertEqual(m["football_field"]["52_week_range"]["status"], "NOT_AVAILABLE")
        again = dcf_model(self.f(), sic="3571", g="0.02", fcff_growth="0.03", years=5, rf_point=rf, beta="1.1", erp="0.05", kd="0.05",
                          price=price)
        self.assertEqual(m, again)                                         # deterministic re-run

    def test_guards(self):
        with self.assertRaises(ModelRefused):                              # financial sector blocked by default
            dcf_model(self.f(), sic="6021", g="0.02", fcff=["10", "11"], wacc="0.09")
        ok = dcf_model(self.f(), sic="6021", g="0.02", fcff=["10", "11"], wacc="0.09", allow_financial=True)
        self.assertTrue(ok["overrides"])
        with self.assertRaises(ModelRefused):                              # currency-inconsistent inputs
            dcf_model(self.f(), sic="3571", g="0.02", fcff=["10"], rf="0.03", beta="1", erp="0.05", kd="0.04", we="0.7", wd="0.3",
                      rate_currency="EUR")
        with self.assertRaises(ModelRefused):                              # WACC <= g
            dcf_model(self.f(), sic="3571", g="0.09", fcff=["10"], wacc="0.09")
        with self.assertRaises(ModelRefused):                              # no forecast given
            dcf_model(self.f(), sic="3571", g="0.02", wacc="0.09")


if __name__ == "__main__":
    unittest.main()
