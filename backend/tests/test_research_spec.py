"""Strategy specification schema (RES-01; BR-DAT-08; CAP-RES-12). Every spec here is a TEST FIXTURE."""
from __future__ import annotations

import copy
import unittest
from datetime import date

from unknown.domain.fixed import Fixed
from unknown.research.common import SpecError
from unknown.research.spec import REQUIRED_EVIDENCE, parse_spec
from unknown.research.strategies import buy_and_hold_spec, trend_spec, vol_capped_spec

DEV = (date(2021, 1, 4), date(2023, 6, 30))
HOLDOUT = (date(2023, 7, 3), date(2024, 6, 28))
INSTS = ["FIX-A-1", "FIX-A-2"]


def body() -> dict:
    return copy.deepcopy(trend_spec(INSTS, DEV, HOLDOUT).body)


class SpecCoverageTest(unittest.TestCase):
    def test_every_res01_field_is_present_and_required(self):
        """RES-01 lists: hypothesis/market conditions; equations, variables, units, assumptions; causal features and data
        requirements; entry/exit/sizing/holding/execution; costs, capacity, liquidity, operational constraints; risk limits
        and failure conditions; train/validation/untouched plan; baselines; sensitivity and stress; promotion evidence."""
        required_paths = [
            ("hypothesis", "statement"), ("hypothesis", "market_conditions"), ("equations",), ("variables",), ("assumptions",),
            ("features",), ("data_requirements",), ("rules", "entry"), ("rules", "exit"), ("rules", "sizing"),
            ("rules", "holding"), ("rules", "execution"), ("costs",), ("capacity",), ("liquidity",),
            ("operational_constraints",), ("risk_limits",), ("failure_conditions",), ("validation_plan", "development"),
            ("validation_plan", "untouched_holdout"), ("validation_plan", "walk_forward"), ("baselines",),
            ("sensitivity_plan",), ("stress_plan",), ("promotion_evidence",), ("parameters",), ("parameter_domain",)]
        for p in required_paths:
            b = body()
            target = b
            for k in p[:-1]:
                target = target[k]
            del target[p[-1]]
            with self.assertRaises(SpecError, msg=f"missing {'.'.join(p)} must be rejected"):
                parse_spec(b)
        units = [v["unit"] for v in body()["variables"]]
        self.assertTrue(all(units))

    def test_builders_produce_valid_specs_with_stable_hashes(self):
        a = trend_spec(INSTS, DEV, HOLDOUT)
        b = trend_spec(INSTS, DEV, HOLDOUT)
        self.assertEqual(a.spec_hash, b.spec_hash)
        self.assertEqual(len(a.spec_hash), 64)
        self.assertNotEqual(a.spec_hash, a.with_parameters(fast_span="5").spec_hash)
        for builder in (buy_and_hold_spec, vol_capped_spec):
            self.assertEqual(builder(INSTS, DEV, HOLDOUT).family, "BASELINE")
        self.assertEqual(a.param_int("slow_span"), 40)
        self.assertEqual(a.cost_multipliers, (Fixed.parse("0.5"), Fixed(1), Fixed(2), Fixed(3)))


class SpecStrictnessTest(unittest.TestCase):
    def assertRejects(self, b: dict, fragment: str) -> None:
        with self.assertRaises(SpecError) as cm:
            parse_spec(b)
        self.assertIn(fragment, str(cm.exception))

    def test_unknown_field_rejected(self):
        b = body()
        b["rules"]["execution"]["market_on_close"] = True
        self.assertRejects(b, "unknown fields")

    def test_numbers_must_be_decimal_strings(self):
        b = body()
        b["costs"]["spread_bps"] = 10.0
        self.assertRejects(b, "decimal string")

    def test_same_bar_fill_is_refused(self):
        b = body()
        b["rules"]["execution"]["same_bar_fill"] = True
        self.assertRejects(b, "same_bar_fill")

    def test_shorting_and_margin_refused(self):
        for key, value in (("allow_short", True), ("allow_margin", True), ("long_only", False)):
            b = body()
            b["risk_limits"][key] = value
            self.assertRejects(b, key)

    def test_holdout_must_follow_development_without_overlap(self):
        b = body()
        b["validation_plan"]["untouched_holdout"]["start"] = "2023-06-30"
        self.assertRejects(b, "chronological")

    def test_purge_must_cover_label_horizon_plus_execution_delay(self):
        b = body()
        b["validation_plan"]["walk_forward"]["purge_sessions"] = 1
        self.assertRejects(b, "BR-DAT-14")

    def test_cost_multipliers_must_cover_half_to_triple(self):
        b = body()
        b["sensitivity_plan"]["cost_multipliers"] = ["1", "2", "3", "4"]
        self.assertRejects(b, "0.5, 1, 2 and 3")

    def test_impact_needs_several_y_values_and_the_default_among_them(self):
        b = body()
        b["costs"]["impact_y_values"] = ["1.0", "1.00"]
        self.assertRejects(b, "two distinct")
        b = body()
        b["costs"]["impact_y_default"] = "3"
        self.assertRejects(b, "impact_y_default")

    def test_required_promotion_evidence(self):
        b = body()
        b["promotion_evidence"] = sorted(REQUIRED_EVIDENCE - {"HOLDOUT"})
        self.assertRejects(b, "HOLDOUT")

    def test_parameters_inside_domain_and_integers_where_declared(self):
        b = body()
        b["parameters"]["fast_span"] = "70"
        self.assertRejects(b, "fast_span")
        b = body()
        b["parameters"]["fast_span"] = "5.5"
        self.assertRejects(b, "integer")
        b = body()
        del b["parameters"]["k"]
        self.assertRejects(b, "missing")
        b = body()
        b["sensitivity_plan"]["parameter_grid"]["slow_span"] = ["300"]
        self.assertRejects(b, "slow_span")

    def test_formula_ids_must_be_registered(self):
        b = body()
        b["equations"][0]["formula_id"] = "eq99.9@1"
        self.assertRejects(b, "formula registry")

    def test_point_in_time_data_and_causal_features_only(self):
        b = body()
        b["data_requirements"][0]["point_in_time"] = False
        self.assertRejects(b, "point_in_time")
        b = body()
        b["features"][0]["causal"] = False
        self.assertRejects(b, "causal")

    def test_sizing_cannot_exceed_risk_limits(self):
        b = body()
        b["risk_limits"]["max_single_name_weight"] = "0.30"
        self.assertRejects(b, "risk_limits")


if __name__ == "__main__":
    unittest.main()
