"""A strategy runner in shadow mode for an artifact the research registry holds at ``shadow`` (ADR-0017 #2), promoted
automatically inside an owner-approved envelope, against a real core service. Every number is a TEST FIXTURE."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_strategy_runner import RealCoreCase  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402
from unknown.strategy.artifacts import ArtifactRefused  # noqa: E402

SYSTEM = Principal("promotion.service", Role.SYSTEM)


class ShadowStatusRunnerTest(RealCoreCase):
    LISTED = ("S",)

    def make_artifacts(self) -> None:
        h, aid = self.rf.register("10")
        self.rf.to_validated(h)
        self.rf.promo.approve_shadow_envelope({
            "envelope_id": "SHADOW-E2E", "strategy_id": "TREND_DAILY_V1",
            "parameter_domain": {k: [v.to_str(), v.to_str()] for k, v in self.rf.spec.with_parameters(fast_span="10")
                                 .parameters.items()},
            "instrument_set": ["FIX-EQ-1", "FIX-EQ-2"],
            "risk_budget": {"max_gross_exposure": "0.95", "max_single_name_weight": "0.95", "max_drawdown_stop": "0.35"},
            "protocol_hash": self.rf.protocol, "max_promotions_per_period": 1, "period_days": 30}, self.rf.owner())
        rec = self.rf.promo.auto_shadow("SHADOW-E2E", h, SYSTEM, evidence_ids=self.rf.promo.history(h)[-1]["evidence_ids"])
        assert rec["approval"]["kind"] == "SHADOW_ENVELOPE"
        self.ids["S"], self.hashes["S"] = aid, h

    def test_a_shadow_status_artifact_records_shadow_decisions_and_never_runs_live(self):
        s_id = self.ids["S"]
        self.assertEqual(self.rf.promo.status(self.hashes["S"]), "shadow")
        with self.runner("S", "live") as r:
            with self.assertRaises(ArtifactRefused) as cm:
                r.run_once()
        self.assertEqual(cm.exception.code, "STATUS_NOT_PERMITTED")
        self.assertEqual((self.orders(s_id), self.decisions(s_id)), ([], []))
        with self.runner("S", "shadow") as r:
            s = r.run_once()
        self.assertEqual((s["status"], s["mode"], s["result"]), ("shadow", "shadow", "OK"))
        self.assertEqual(self.orders(), [])
        recs = self.decisions(s_id)
        self.assertEqual(sorted((d["instrument_id"], d["mode"]) for d in recs), [("FIX-EQ-1", "shadow"), ("FIX-EQ-2", "shadow")])
        self.assertTrue(all(d["outcome"] == "NO_TRADE" for d in recs), recs)
        self.assertTrue(any(d["reason"].startswith("SHADOW_WOULD_SUBMIT") for d in recs), recs)
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


del RealCoreCase     # imported as a base only: not collected as a test case of this module

if __name__ == "__main__":
    unittest.main()
