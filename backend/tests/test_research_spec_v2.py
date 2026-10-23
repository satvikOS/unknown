"""Strategy spec schema v2 (BR-DAT-04 risk budget, BR-DAT-18 feature validity window, BR-DAT-19 submission window)
and the readability of schema v1 documents. Every spec here is a TEST FIXTURE."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_research_risk_sizing import v1_body  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.research.common import SpecError  # noqa: E402
from unknown.research.engine import BacktestConfig  # noqa: E402
from unknown.research.spec import SPEC_SCHEMA_VERSION, parse_spec  # noqa: E402
from unknown.research.strategies import buy_and_hold_spec, trend_spec, vol_capped_spec  # noqa: E402

DEV, HOLD = (date(2021, 1, 4), date(2022, 12, 30)), (date(2023, 1, 2), date(2023, 12, 29))


def v2_body() -> dict:
    return copy.deepcopy(trend_spec(["FIX-RS-1"], DEV, HOLD).body)


class SchemaV2Test(unittest.TestCase):
    def test_the_builders_write_v2_with_every_contract(self):
        self.assertEqual(SPEC_SCHEMA_VERSION, 2)
        for builder in (trend_spec, buy_and_hold_spec, vol_capped_spec):
            s = builder(["FIX-RS-1"], DEV, HOLD)
            self.assertEqual(s.schema_version, 2)
            self.assertIsNotNone(s.submission_window)
            self.assertEqual(set(s.risk_budget), {"loss_fraction_of_equity", "adverse_move_fraction"})
            self.assertTrue(s.features and all(f["validity_window_s"] == 4 * 86400 for f in s.features))
        t = trend_spec(["FIX-RS-1"], DEV, HOLD)
        self.assertEqual(t.risk_budget, {"loss_fraction_of_equity": Fixed.parse("0.03"),
                                         "adverse_move_fraction": Fixed.parse("0.05")})
        self.assertEqual([(f["id"], f["version"], f["warm_up_observations"]) for f in t.features],
                         [("ema_spread_log_price", 1, 250), ("ewma_vol", 1, 2)])

    def test_v2_contracts_are_required_and_bounded(self):
        cases = []
        b = v2_body()
        del b["rules"]["sizing"]["risk_budget"]
        cases.append((b, "risk_budget"))
        for key, bad in (("loss_fraction_of_equity", "0"), ("loss_fraction_of_equity", "1.5"),
                         ("adverse_move_fraction", "0"), ("adverse_move_fraction", 0.05)):
            b = v2_body()
            b["rules"]["sizing"]["risk_budget"][key] = bad
            cases.append((b, key))
        b = v2_body()
        b["rules"]["sizing"]["risk_budget"]["stop_loss"] = "0.1"
        cases.append((b, "unknown fields"))
        b = v2_body()
        del b["features"][0]["validity_window_s"]
        cases.append((b, "validity_window_s"))
        b = v2_body()
        b["features"][1]["validity_window_s"] = 0
        cases.append((b, "validity_window_s"))
        b = v2_body()
        b["features"][1]["id"] = b["features"][0]["id"]
        cases.append((b, "unique"))
        for body, fragment in cases:
            with self.assertRaises(SpecError) as cm:
                parse_spec(body)
            self.assertIn(fragment, str(cm.exception))


class SchemaV1ReadableTest(unittest.TestCase):
    """Registries hold v1 specs written before v2: they stay parseable, and the backtester runs them with the legacy
    rules (no window, no risk term), labelled so in the run config; the strategy runner refuses them."""

    def test_a_v1_document_parses_and_runs_with_the_legacy_rules(self):
        s = parse_spec(v1_body())
        self.assertEqual((s.schema_version, s.submission_window, s.risk_budget), (1, None, None))
        self.assertTrue(all(f["validity_window_s"] is None for f in s.features))
        cfg = BacktestConfig.from_spec(s, Fixed.parse("100000"))
        self.assertIsNone(cfg.execution.window)
        self.assertIsNone(cfg.risk)
        j = cfg.to_json()
        self.assertNotIn("risk_term", j)
        self.assertNotIn("submission_window", j["execution"])
        self.assertEqual(j["execution"]["latency_s"], 60)
        self.assertNotEqual(s.spec_hash, trend_spec(["FIX-RS-1"], DEV, HOLD).spec_hash)
        for bad in (2, 3, 0):
            b = v1_body()
            b["schema_version"] = bad
            if bad == 2:
                with self.assertRaises(SpecError):          # a v2 document needs the v2 contracts
                    parse_spec(b)
            else:
                with self.assertRaises(SpecError):
                    parse_spec(b)


if __name__ == "__main__":
    unittest.main()
