"""Cost model known answers (CAP-RES-05 acceptance criteria; book ch. 7 impact). Values are TEST FIXTURES."""
from __future__ import annotations

import math
import unittest

from unknown.domain.fixed import Fixed
from unknown.research.costs import CostModel, IbkrFixedUS, IbkrTieredUS

F = Fixed.parse


class IbkrFixedKnownAnswers(unittest.TestCase):
    def test_page_examples(self):
        s = IbkrFixedUS()
        # CAP-RES-05: "100 shares @ USD 25 = USD 1.00; 1,000 shares @ USD 25 = USD 5.00; 1,000 shares @ USD 0.25 = USD 2.50"
        self.assertEqual(s.commission(F("100"), F("25")), F("1.00"))
        self.assertEqual(s.commission(F("1000"), F("25")), F("5.00"))
        self.assertEqual(s.commission(F("1000"), F("0.25")), F("2.50"))

    def test_one_share_minimum_unless_the_percentage_cap_is_lower(self):
        s = IbkrFixedUS()
        self.assertEqual(s.commission(F("1"), F("150")), F("1.00"))
        self.assertEqual(s.commission(F("1"), F("50")), F("0.50"))   # 1% of 50.00

    def test_results_are_exact_at_ledger_scale(self):
        c = IbkrFixedUS().commission(F("3"), F("12.3457"))
        self.assertEqual(c, F("0.370371"))  # 1% of 37.0371 = 0.370371 exactly
        self.assertEqual(c.scale, 6)

    def test_envelope_never_below_charged_commission(self):
        s = IbkrFixedUS()
        for q, p in (("1", "50"), ("100", "25"), ("1000", "0.25"), ("37", "3.17")):
            self.assertGreaterEqual(s.envelope(F(q)), s.commission(F(q), F(p)))


class IbkrTieredKnownAnswers(unittest.TestCase):
    def test_page_example_500k_shares_in_a_month(self):
        # 300,000 at 0.0035 and 200,000 at 0.002 = 1,050 + 400 = 1,450 (before third-party fees)
        self.assertEqual(IbkrTieredUS.base_commission(F("500000"), 0), F("1450"))
        self.assertEqual(IbkrTieredUS().commission(F("500000"), F("25")), F("1450"))

    def test_tiers_apply_marginally_across_orders_in_the_month(self):
        first = IbkrTieredUS.base_commission(F("250000"), 0)
        second = IbkrTieredUS.base_commission(F("250000"), 250_000)
        self.assertEqual(first + second, F("1450"))
        self.assertEqual(second, F("50000") * F("0.0035") + F("200000") * F("0.0020"))

    def test_per_order_minimum_and_cap(self):
        t = IbkrTieredUS()
        self.assertEqual(t.commission(F("10"), F("100")), F("0.35"))       # 0.035 -> minimum 0.35
        self.assertEqual(t.commission(F("10"), F("2")), F("0.20"))         # 1% of 20.00 caps below the minimum


class PriceCostTest(unittest.TestCase):
    def model(self, **kw) -> CostModel:
        base = dict(schedule=IbkrFixedUS(), spread_bps=F("10"), slippage_bps=F("5"), impact_y=F("1.0"),
                    third_party_sell_bps=F("1"))
        base.update(kw)
        return CostModel(**base)

    def test_square_root_impact_formula(self):
        m = self.model(spread_bps=F("0"), slippage_bps=F("0"), impact_y=F("2.0"))
        pc = m.price_cost(0.02, F("10000"), F("1000000"))
        expected = 2.0 * 0.02 * math.sqrt(10000 / 1000000)   # I = Y sigma sqrt(Q/V) = 0.004
        self.assertEqual(pc.impact, F("0.004"))
        self.assertAlmostEqual(float(pc.fraction.to_str()), expected, places=12)

    def test_half_spread_plus_slippage(self):
        pc = self.model(impact_y=F("0")).price_cost(0.02, F("1"), F("1000"))
        self.assertEqual(pc.fraction, F("0.001"))  # 10 bps / 2 + 5 bps = 10 bps

    def test_several_y_values_scale_impact(self):
        values = [self.model(spread_bps=F("0"), slippage_bps=F("0"), impact_y=F(y)).price_cost(0.01, F("400"), F("10000")).impact
                  for y in ("0.5", "1.0", "2.0")]
        self.assertEqual(values, [F("0.001"), F("0.002"), F("0.004")])

    def test_multiplier_scales_every_component(self):
        m1, m3 = self.model(), self.model(multiplier=F("3"))
        self.assertEqual(m3.fees("SELL", F("100"), F("50")).total, m1.fees("SELL", F("100"), F("50")).total * 3)
        a, b = m1.price_cost(0.01, F("100"), F("1000000")), m3.price_cost(0.01, F("100"), F("1000000"))
        self.assertGreaterEqual(b.fraction, a.fraction * 3 - F("0.00000003"))

    def test_third_party_fee_only_on_sells_and_flagged_unverified(self):
        m = self.model()
        self.assertEqual(m.fees("BUY", F("100"), F("50")).third_party, F("0"))
        self.assertEqual(m.fees("SELL", F("100"), F("50")).third_party, F("0.5"))   # 1 bp of 5,000
        self.assertIn("UNVERIFIED", m.describe()["third_party_status"])

    def test_zero_volume_has_no_impact_estimate(self):
        with self.assertRaises(ValueError):
            self.model().price_cost(0.01, F("1"), F("0"))


if __name__ == "__main__":
    unittest.main()
