"""The ``shadow`` promotion status (ADR-0017 #2; book ch. 20 "Shadow and paper stages", "Selection within an approved
envelope"). Evidence payloads, envelopes and protocols are TEST FIXTURES.

``shadow`` sits between ``validated`` and ``paper``. It is reached in one of two ways, and both are durable,
hash-chained transition records that cite the gate evidence the validation rested on:

* the owner approves it (re-authenticated within 15 minutes);
* the SYSTEM promotion service promotes it automatically inside an owner-approved envelope that fixes the artifact
  class, the parameter domain, the instrument set, the risk budget and the evaluation protocol.

The strategy runner's shadow mode needs ``shadow`` or later; live mode still needs ``paper``."""
from __future__ import annotations

import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_research_promotion import AGENT, AUTHOR, DEV, EVAL, GATES, PASSING, A, Base  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402
from unknown.research.common import AuthorityError, EvidenceError, RegistryError  # noqa: E402
from unknown.research.promotion import LADDER, RULES, STATUSES  # noqa: E402
from unknown.research.study import FIXTURE_PROTOCOL  # noqa: E402
from unknown.strategy import artifacts  # noqa: E402
from unknown.strategy.artifacts import ArtifactRefused  # noqa: E402

SYSTEM = Principal("promotion.service", Role.SYSTEM)


class ShadowLadderTest(Base):
    def test_shadow_sits_between_validated_and_paper(self):
        self.assertEqual(LADDER, ("proposed", "reproducible", "validated", "shadow", "paper", "eligible", "active"))
        self.assertEqual(STATUSES[:7], LADDER)
        self.assertIn(("validated", "shadow"), RULES)
        self.assertIn(("shadow", "paper"), RULES)
        self.assertNotIn(("validated", "paper"), RULES)      # paper is reached only through shadow
        art = self.artifact()
        self.to_validated(art)
        with self.assertRaises(RegistryError) as cm:
            self.promo.transition(art, "paper", self.owner(), evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="skip")
        self.assertEqual(cm.exception.code, "INVALID_TRANSITION")
        self.assertEqual(self.promo.status(art), "validated")

    def test_the_owner_approves_shadow_and_the_record_cites_the_gate_evidence(self):
        art = self.artifact()
        self.to_validated(art)
        gates = self.promo.history(art)[-1]["evidence_ids"]
        with self.assertRaises(EvidenceError):            # recorded with evidence: none cited is refused
            self.promo.transition(art, "shadow", self.owner(), evidence_ids=[], reason="no evidence")
        with self.assertRaises(EvidenceError):            # every gate the spec lists, not a subset
            self.promo.transition(art, "shadow", self.owner(), evidence_ids=gates[:-1], reason="subset")
        with self.assertRaises(AuthorityError):           # the SYSTEM needs an envelope (auto_shadow), not this path
            self.promo.transition(art, "shadow", EVAL, evidence_ids=gates, reason="system")
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "shadow", AGENT, evidence_ids=gates, reason="agent")
        stale = Principal("owner", Role.OWNER, authenticated_at_ns=self.reg.now() - 20 * 60 * 10**9)
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "shadow", stale, evidence_ids=gates, reason="stale")
        self.assertEqual(self.promo.status(art), "validated")
        rec = self.promo.transition(art, "shadow", self.owner(), evidence_ids=gates, reason="owner: shadow")
        self.assertEqual((rec["from_status"], rec["to_status"], rec["approval"]["kind"], rec["approval"]["by"]),
                         ("validated", "shadow", "OWNER", "owner"))
        self.assertEqual(rec["evidence_ids"], sorted(gates))
        self.assertEqual(self.promo.status(art), "shadow")
        # shadow -> paper is an owner decision on CORE_REPLAY_MATCH, as validated -> paper was
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "paper", EVAL, evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="system")
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "paper", self.owner(), evidence_ids=[], reason="no replay")
        self.promo.transition(art, "paper", self.owner(), evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="paper")
        self.assertEqual(self.promo.status(art), "paper")
        self.assertEqual(self.promo.verify_chain(), [])

    def test_a_shadow_artifact_can_be_quarantined_and_retired(self):
        art = self.artifact()
        self.to_shadow(art)
        self.promo.transition(art, "quarantined", EVAL, evidence_ids=[self.ev(art, "MONITORING_INCIDENT")], reason="drift")
        self.assertEqual(self.promo.status(art), "quarantined")
        art2 = self.artifact(self.spec.with_parameters(fast_span="5"))
        self.to_shadow(art2)
        self.promo.transition(art2, "retired", self.owner(), reason="owner retires")
        self.assertEqual(self.promo.status(art2), "retired")


class ShadowEnvelopeTest(Base):
    def envelope(self, **over) -> dict:
        body = {"envelope_id": "SHADOW-1", "strategy_id": "TREND_DAILY_V1",
                "parameter_domain": {"fast_span": ["5", "10"], "slow_span": ["20", "80"], "vol_half_life": ["20", "20"],
                                     "horizon_h": ["10", "10"], "k": ["0.5", "0.5"], "w_max": ["0.45", "0.45"],
                                     "epsilon": ["0.0001", "0.0001"]},
                "instrument_set": [A],
                "risk_budget": {"max_gross_exposure": "0.95", "max_single_name_weight": "0.95", "max_drawdown_stop": "0.35"},
                "protocol_hash": self.protocol, "max_promotions_per_period": 2, "period_days": 30}
        body.update(over)
        return body

    def validated(self, fast_span: str = "10") -> tuple[str, list[str]]:
        art = self.artifact(self.spec.with_parameters(fast_span=fast_span))
        self.to_validated(art)
        return art, self.promo.history(art)[-1]["evidence_ids"]

    def test_the_owner_approves_an_immutable_strict_envelope(self):
        for bad in (self.envelope(extra=1), {k: v for k, v in self.envelope().items() if k != "risk_budget"},
                    self.envelope(risk_budget={"max_gross_exposure": "0.95"}),
                    self.envelope(risk_budget={"max_gross_exposure": "1.5", "max_single_name_weight": "0.95",
                                               "max_drawdown_stop": "0.35"}),
                    self.envelope(parameter_domain={"fast_span": ["10", "5"]}), self.envelope(instrument_set=[]),
                    self.envelope(protocol_hash="0" * 64), self.envelope(max_promotions_per_period=0)):
            with self.assertRaises((EvidenceError, RegistryError)):
                self.promo.approve_shadow_envelope(bad, self.owner())
        for who in (AGENT, SYSTEM, EVAL):
            with self.assertRaises(AuthorityError):
                self.promo.approve_shadow_envelope(self.envelope(), who)
        h = self.promo.approve_shadow_envelope(self.envelope(), self.owner())
        self.assertEqual(self.promo.shadow_envelope("SHADOW-1")["hash"], h)
        with self.assertRaises(RegistryError) as cm:
            self.promo.approve_shadow_envelope(self.envelope(max_promotions_per_period=50), self.owner())
        self.assertEqual(cm.exception.code, "ENVELOPE_EXISTS")
        import sqlite3
        for sql in ("UPDATE shadow_envelope SET body='{}'", "DELETE FROM shadow_envelope"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.reg.conn.execute(sql)

    def test_automatic_promotion_inside_the_envelope_is_recorded_with_its_evidence_and_envelope(self):
        env_hash = self.promo.approve_shadow_envelope(self.envelope(), self.owner())
        art, gates = self.validated()
        rec = self.promo.auto_shadow("SHADOW-1", art, SYSTEM, evidence_ids=gates)
        self.assertEqual(self.promo.status(art), "shadow")
        self.assertEqual((rec["from_status"], rec["to_status"], rec["principal"], rec["role"]),
                         ("validated", "shadow", "promotion.service", "SYSTEM"))
        self.assertEqual(rec["evidence_ids"], sorted(gates))
        a = rec["approval"]
        self.assertEqual((a["kind"], a["envelope_id"], a["envelope_hash"], a["approved_by"]),
                         ("SHADOW_ENVELOPE", "SHADOW-1", env_hash, "owner"))
        self.assertEqual(self.promo.history(art)[-1]["approval"], a)        # durable, in the hash chain
        self.assertEqual(self.promo.verify_chain(), [])
        # the envelope stops at shadow: paper stays an owner decision
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "paper", SYSTEM, evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="system")

    def test_outside_the_envelope_is_refused_and_the_status_is_unchanged(self):
        # risk budget: the spec's max_drawdown_stop is 0.35; a budget of 0.20 does not cover it
        self.promo.approve_shadow_envelope(self.envelope(envelope_id="TIGHT-RISK", risk_budget={
            "max_gross_exposure": "0.95", "max_single_name_weight": "0.95", "max_drawdown_stop": "0.20"}), self.owner())
        self.promo.approve_shadow_envelope(self.envelope(envelope_id="OTHER-CLASS", strategy_id="BUY_AND_HOLD"), self.owner())
        self.promo.approve_shadow_envelope(self.envelope(envelope_id="OTHER-NAMES", instrument_set=["FIX-OTHER-1"]),
                                           self.owner())
        self.promo.approve_shadow_envelope(self.envelope(), self.owner())
        art, gates = self.validated()
        outside_params, gates2 = self.validated("20")          # fast_span 20 is outside [5, 10]
        cases = [("TIGHT-RISK", art, gates, "max_drawdown_stop"), ("OTHER-CLASS", art, gates, "strategy outside"),
                 ("OTHER-NAMES", art, gates, "instruments outside"), ("SHADOW-1", outside_params, gates2, "fast_span=20")]
        for env, h, ev, fragment in cases:
            with self.assertRaises(AuthorityError) as cm:
                self.promo.auto_shadow(env, h, SYSTEM, evidence_ids=ev)
            self.assertIn(fragment, str(cm.exception), env)
        self.assertEqual((self.promo.status(art), self.promo.status(outside_params)), ("validated", "validated"))

    def test_only_the_system_promotes_and_only_validated_artifacts_with_the_envelopes_protocol(self):
        self.promo.approve_shadow_envelope(self.envelope(), self.owner())
        art, gates = self.validated()
        for who in (self.owner(), AGENT, Principal("strategy.x", Role.STRATEGY)):
            with self.assertRaises(AuthorityError):
                self.promo.auto_shadow("SHADOW-1", art, who, evidence_ids=gates)
        with self.assertRaises(EvidenceError):                  # recorded with evidence: the gates must be cited
            self.promo.auto_shadow("SHADOW-1", art, SYSTEM, evidence_ids=gates[:3])
        # evaluation protocol: gate evidence evaluated under another (owner-approved) protocol is outside the envelope
        other = self.promo.approve_protocol({**FIXTURE_PROTOCOL, "min_dsr": "0.10"}, self.owner())
        time.sleep(0.002)
        self.reg.preregister("EXP-Q", self.spec, search_space={"fast_span": ["5", "10", "20"]}, budget=5,
                             rejection_criteria=["x"], benchmark="BUY_AND_HOLD", principal=AUTHOR, fixture=True)
        time.sleep(0.002)
        tq = self.reg.start_trial("EXP-Q", spec=self.spec, segment="DEVELOPMENT", manifest=self.manifest, principal=AUTHOR)
        self.reg.finish_trial(tq, status="SUCCEEDED", metrics={})
        art2 = self.artifact(self.spec.with_parameters(fast_span="5"))
        self.promo.transition(art2, "reproducible", EVAL, evidence_ids=[self.ev(art2, "REPLAY_EXACT")], reason="replayed")
        ids = []
        for k in GATES:
            refs, payload = [{"type": "trial", "id": tq}], PASSING[k]
            if k == "HOLDOUT":
                payload, refs = self.holdout_evidence(art2)
            r = self.promo.add_evidence(art2, k, payload, refs=refs, principal=EVAL,
                                        evaluation_started_at_ns=self.reg.now(), protocol_hash=other)
            self.assertTrue(r["passed"], (k, r["reasons"]))
            ids.append(r["evidence_id"])
        self.promo.transition(art2, "validated", EVAL, evidence_ids=ids, reason="gates passed under another protocol")
        with self.assertRaises(AuthorityError) as cm:
            self.promo.auto_shadow("SHADOW-1", art2, SYSTEM, evidence_ids=ids)
        self.assertIn("protocol", str(cm.exception))
        self.assertEqual(self.promo.status(art2), "validated")
        # not validated
        reproducible = self.artifact(self.spec.with_parameters(fast_span="5", slow_span="80"))
        self.promo.transition(reproducible, "reproducible", EVAL, evidence_ids=[self.ev(reproducible, "REPLAY_EXACT")],
                              reason="replayed")
        with self.assertRaises(AuthorityError):
            self.promo.auto_shadow("SHADOW-1", reproducible, SYSTEM, evidence_ids=gates)
        self.assertEqual(self.promo.status(art), "validated")
        self.promo.auto_shadow("SHADOW-1", art, SYSTEM, evidence_ids=gates)
        self.assertEqual(self.promo.status(art), "shadow")

    def test_expired_artifacts_and_the_period_limit_are_refused(self):
        self.promo.approve_shadow_envelope(self.envelope(max_promotions_per_period=1), self.owner())
        expired = self.promo.register_artifact(self.spec.with_parameters(fast_span="5"), AUTHOR, code_hash="d" * 64,
                                               training_interval=DEV, evaluation_manifest_hash=self.manifest["manifest_hash"],
                                               allowed_markets=["FIXTURE"], horizon="daily", capacity_estimate="n/a",
                                               expires_on=date.today() + timedelta(days=1))
        self.to_validated(expired)
        today = datetime.now(timezone.utc).date()
        self.promo._today = lambda: today + timedelta(days=2)
        with self.assertRaises(EvidenceError):
            self.promo.auto_shadow("SHADOW-1", expired, SYSTEM, evidence_ids=self.promo.history(expired)[-1]["evidence_ids"])
        self.promo._today = lambda: today
        art, gates = self.validated()
        self.promo.auto_shadow("SHADOW-1", art, SYSTEM, evidence_ids=gates)
        second = self.promo.history(expired)[-1]["evidence_ids"]
        with self.assertRaises(AuthorityError) as cm:            # one automatic promotion per 30 days
            self.promo.auto_shadow("SHADOW-1", expired, SYSTEM, evidence_ids=second)
        self.assertIn("limit", str(cm.exception))
        self.assertEqual(self.promo.status(expired), "validated")


class RunnerModeGateTest(Base):
    """The runner reads the registry's own ladder: shadow mode needs 'shadow', live mode needs 'paper'."""

    def test_shadow_status_permits_shadow_mode_only(self):
        art = self.artifact(self.spec.with_parameters(fast_span="5"))
        self.to_shadow(art)
        reg_today = datetime.now(timezone.utc).date()
        aid = artifacts.artifact_id("TREND_DAILY_V1", art)
        loaded = artifacts.load(self.reg, aid, mode="shadow", today=reg_today, code_hash="c" * 64)
        self.assertEqual(loaded.status, "shadow")
        with self.assertRaises(ArtifactRefused) as cm:
            artifacts.load(self.reg, aid, mode="live", today=reg_today, code_hash="c" * 64)
        self.assertEqual(cm.exception.code, "STATUS_NOT_PERMITTED")
        self.assertEqual(artifacts.STATUS_ORDER, LADDER)


if __name__ == "__main__":
    unittest.main()
