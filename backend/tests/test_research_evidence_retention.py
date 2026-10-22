"""BR-DAT-16: an artifact's expiry is bounded by the retention of the data its evidence rests on, and an artifact
whose evidence data was deleted is EVIDENCE_EXPIRED: it may run until its expiry but can never be promoted again.
Manifests, datasets and dates are TEST FIXTURES."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_research_promotion import AGENT, AUTHOR, DEV, EVAL, HOLD, PASSING, Base  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402
from unknown.research.common import AuthorityError, EvidenceError, RegistryError  # noqa: E402
from unknown.research.engine import BacktestConfig  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.research.manifest import build_manifest  # noqa: E402
from unknown.strategy import artifacts  # noqa: E402
from unknown.strategy.artifacts import ArtifactRefused  # noqa: E402

SYSTEM = Principal("data.retention", Role.SYSTEM)
TODAY = datetime.now(timezone.utc).date()


class RetentionBase(Base):
    def retained_manifest(self, spec, until, retention: str | None, name: str = "LICENSED.daily_bars.v1") -> dict:
        """A manifest whose dataset carries the licence's deletion deadline (``retention_until``)."""
        cfg = BacktestConfig.from_spec(spec, Fixed(100000))
        entry = {"fingerprint": "e" * 64, "until": until.isoformat(), "retention_until": retention}
        return build_manifest(run_kind="DEVELOPMENT", strategy_id=spec.strategy_id, spec_hash=spec.spec_hash,
                              parameters={k: v.to_str() for k, v in spec.parameters.items()}, config=cfg.to_json(),
                              datasets={name: entry}, seeds={}, include_environment=False)

    def store_manifest(self, m: dict) -> str:
        """Record a manifest through a trial (the registry stores manifests with the trials that use them)."""
        tid = self.reg.start_trial("EXP-P", spec=self.spec, segment="WALK_FORWARD", manifest=m, principal=AUTHOR)
        self.reg.finish_trial(tid, status="SUCCEEDED", metrics={})
        return tid

    def register(self, manifest_hash: str, expires_on: date, fast_span: str = "10") -> str:
        return self.promo.register_artifact(self.spec.with_parameters(fast_span=fast_span), AUTHOR, code_hash="c" * 64,
                                            training_interval=DEV, evaluation_manifest_hash=manifest_hash,
                                            allowed_markets=["FIXTURE"], horizon="daily", capacity_estimate="n/a",
                                            expires_on=expires_on)


class ExpiryBoundedByRetentionTest(RetentionBase):
    def test_registration_refuses_an_expiry_after_the_evaluation_datas_retention(self):
        m = self.retained_manifest(self.spec, DEV[1], "2031-06-30")
        self.store_manifest(m)
        with self.assertRaises(EvidenceError) as cm:
            self.register(m["manifest_hash"], date(2031, 12, 31))
        self.assertIn("BR-DAT-16", str(cm.exception))
        art = self.register(m["manifest_hash"], date(2031, 6, 30))
        st = self.promo.evidence_status(art)
        self.assertEqual((st["status"], st["retention_until"], st["effective_expiry"]), ("CURRENT", "2031-06-30", "2031-06-30"))
        # a manifest without a retention date bounds nothing: the registered expiry stands
        self.assertEqual(self.promo.effective_expiry(self.artifact(self.spec.with_parameters(fast_span="5"))), date(2099, 1, 1))

    def test_passed_evidence_on_shorter_retained_data_shortens_the_effective_expiry(self):
        art = self.artifact()                                              # expires 2099-01-01
        short = self.retained_manifest(self.spec, DEV[1], (TODAY + timedelta(days=10)).isoformat(), name="VENDOR.bars")
        tid = self.store_manifest(short)
        # failed evidence supports nothing, so it bounds nothing
        self.promo.add_evidence(art, "WALK_FORWARD_OOS", {"n": 1, "sharpe_per_period": -1.0}, refs=[{"type": "trial", "id": tid}],
                                principal=EVAL, evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertEqual(self.promo.effective_expiry(art), date(2099, 1, 1))
        r = self.promo.add_evidence(art, "WALK_FORWARD_OOS", PASSING["WALK_FORWARD_OOS"], refs=[{"type": "trial", "id": tid}],
                                    principal=EVAL, evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertTrue(r["passed"], r["reasons"])
        self.assertEqual(self.promo.effective_expiry(art), TODAY + timedelta(days=10))
        self.to_eligible(art)
        self.promo._today = lambda: TODAY + timedelta(days=11)            # past the data's retention
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "active", self.owner(), reason="owner", slot="S")
        self.promo.approve_selection_policy(self.policy_body(), self.owner())
        out = self.promo.auto_select("SEL-1", Principal("promotion.service", Role.SYSTEM))
        self.assertEqual(out["refused"][art], ["artifact expired"])
        with self.assertRaises(ArtifactRefused) as cm:                      # and the runner stops running it
            artifacts.load(self.reg, artifacts.artifact_id("TREND_DAILY_V1", art), mode="live",
                           today=TODAY + timedelta(days=11), code_hash="c" * 64)
        self.assertEqual(cm.exception.code, "ARTIFACT_EXPIRED")


class EvidenceExpiredTest(RetentionBase):
    def setUp(self):
        super().setUp()
        self.m = self.retained_manifest(self.spec, DEV[1], None, name="VENDOR.bars")
        self.store_manifest(self.m)

    def test_only_the_owner_or_system_records_a_deletion_and_the_record_is_append_only(self):
        for who in (AGENT, Principal("strategy.x", Role.STRATEGY)):
            with self.assertRaises(AuthorityError):
                self.promo.record_evidence_deletion("VENDOR.bars", principal=who, reason="licence ended")
        with self.assertRaises(RegistryError):
            self.promo.record_evidence_deletion("VENDOR.bars", principal=SYSTEM, reason=" ")
        first = self.promo.record_evidence_deletion("VENDOR.bars", principal=SYSTEM, reason="licence retention ended")
        again = self.promo.record_evidence_deletion("VENDOR.bars", principal=self.owner(), reason="again")
        self.assertEqual(first, again)                                     # recorded once
        for sql in ("UPDATE evidence_deletion SET reason='x'", "DELETE FROM evidence_deletion"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.reg.conn.execute(sql)

    def test_an_active_artifact_on_deleted_data_keeps_running_but_is_never_promoted_again(self):
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        other = self.artifact(self.spec.with_parameters(fast_span="5"))   # rests on the FIXTURE manifest only
        for h in (art, other):
            self.to_eligible(h)
        self.promo.transition(art, "active", self.owner(), reason="owner", slot="S")
        self.assertEqual(self.promo.evidence_status(art)["status"], "CURRENT")
        self.promo.record_evidence_deletion("VENDOR.bars", principal=SYSTEM, reason="licence retention ended")
        st = self.promo.evidence_status(art)
        self.assertEqual((st["status"], [d["dataset"] for d in st["deleted"]]), ("EVIDENCE_EXPIRED", ["VENDOR.bars"]))
        self.assertEqual(self.promo.evidence_status(other)["status"], "CURRENT")   # unrelated data
        self.assertEqual(self.promo.status(art), "active")                 # it may run until its expiry
        loaded = artifacts.load(self.reg, artifacts.artifact_id("TREND_DAILY_V1", art), mode="live", today=TODAY,
                                code_hash="c" * 64)
        self.assertEqual((loaded.status, loaded.evidence_status), ("active", "EVIDENCE_EXPIRED"))
        # stepping down is allowed; every step back up is refused
        self.promo.transition(art, "eligible", self.owner(), reason="owner deactivates")
        with self.assertRaises(EvidenceError) as cm:
            self.promo.transition(art, "active", self.owner(), reason="re-activate", slot="S")
        self.assertIn("EVIDENCE_EXPIRED", str(cm.exception))
        self.promo.approve_selection_policy(self.policy_body(), self.owner())
        system = Principal("promotion.service", Role.SYSTEM)
        out = self.promo.auto_select("SEL-1", system)
        self.assertEqual(out["selected"], other)                            # never the expired-evidence artifact
        self.assertEqual(out["refused"][art], ["EVIDENCE_EXPIRED: the evidence rests on deleted data"])
        with self.assertRaises(AuthorityError):
            self.promo.auto_select("SEL-1", system, artifact_hash=art)
        # quarantine still applies, and the re-validation path out of quarantine is refused
        self.promo.transition(art, "quarantined", EVAL, evidence_ids=[self.ev(art, "MONITORING_INCIDENT")], reason="incident")
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "reproducible", self.owner(), evidence_ids=[self.ev(art, "REPLAY_EXACT")],
                                  reason="re-validate on deleted data")
        self.promo.transition(art, "retired", self.owner(), reason="owner retires")
        self.assertEqual(self.promo.status(art), "retired")
        self.assertEqual(self.promo.verify_chain(), [])

    def test_a_validated_artifact_on_deleted_data_cannot_reach_shadow(self):
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        self.to_validated(art)
        self.promo.record_evidence_deletion("VENDOR.bars", principal=self.owner(), reason="owner deleted the raw data")
        gates = self.promo.history(art)[-1]["evidence_ids"]
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "shadow", self.owner(), evidence_ids=gates, reason="owner")
        self.promo.approve_shadow_envelope({
            "envelope_id": "SH-R", "strategy_id": "TREND_DAILY_V1",
            "parameter_domain": {k: [v, v] for k, v in self.promo.artifact(art)["body"]["bundle"]["parameters"].items()},
            "instrument_set": list(self.promo.artifact(art)["body"]["instruments"]),
            "risk_budget": {"max_gross_exposure": "0.95", "max_single_name_weight": "0.95", "max_drawdown_stop": "0.35"},
            "protocol_hash": self.protocol, "max_promotions_per_period": 5, "period_days": 30}, self.owner())
        with self.assertRaises(EvidenceError):
            self.promo.auto_shadow("SH-R", art, Principal("promotion.service", Role.SYSTEM), evidence_ids=gates)
        self.assertEqual(self.promo.status(art), "validated")


if __name__ == "__main__":
    unittest.main()
