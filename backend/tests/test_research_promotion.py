"""Artifact registry, status lifecycle, owner approvals and the AUT-03 selection policy (CAP-RES-12; AUT-01..04;
BR-DAT-01, BR-DAT-10, BR-OPS-10). Evidence payloads are TEST FIXTURES."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from unknown.domain.fixed import Fixed
from unknown.domain.principal import Principal, Role
from unknown.research.common import AuthorityError, EvidenceError, RegistryError
from unknown.research.engine import BacktestConfig
from unknown.research.manifest import build_manifest
from unknown.research.promotion import STATUSES, PromotionService
from unknown.research.registry import ResearchRegistry
from unknown.research.strategies import trend_spec
from unknown.research.study import FIXTURE_PROTOCOL

A = "FIX-PRO-1"
DEV, HOLD = (date(2021, 1, 4), date(2022, 12, 30)), (date(2023, 1, 2), date(2023, 12, 29))
AUTHOR = Principal("research.author", Role.SYSTEM)
EVAL = Principal("research.evaluator", Role.SYSTEM)
AGENT = Principal("agent.research", Role.AGENT)

PASSING = {
    "REPLAY_EXACT": {"replay_exact": 1},
    "WALK_FORWARD_OOS": {"n": 300, "sharpe_per_period": 0.06},
    "PAIRED_BOOTSTRAP": {"bootstrap": {"computable": True, "confidence": 0.9, "replications": 1000, "statistic": "mean",
                                       "lower": 0.0002, "point": 0.0005}},
    # HOLDOUT payloads are built per artifact from a real grant (Base.holdout_evidence); see HoldoutEvidenceTest.
    "HOLDOUT": {"bootstrap": {"computable": True, "confidence": 0.9, "replications": 1000, "statistic": "mean",
                              "lower": -0.0001, "point": 0.0004}},
    "COST_SENSITIVITY": {"by_multiplier": {k: {"net_mean": 0.0004} for k in ("0.5", "1", "2", "3")}},
    # every stress case trend_spec declares
    "STRESS": {"cases": {sid: {"max_drawdown": 0.12} for sid in ("costs_x3", "participation_quarter", "impact_y_max",
                                                                  "limit_tight")}},
    "DSR": {"dsr": 0.97, "M": 1}, "PBO": {"pbo": 0.2}, "N_EFF": {"gate": 400.0},   # M = registry count of EXP-P
    "CORE_REPLAY_MATCH": {"mismatches": [], "ledger_problems": [], "fills_compared": 12},
    "PAPER_RECONCILIATION": {"reconciliation_breaks": 0, "paper_days": 15},
    "MONITORING_INCIDENT": {"category": "FEATURE_DRIFT", "detail": "fixture"},
}
GATES = ("WALK_FORWARD_OOS", "PAIRED_BOOTSTRAP", "HOLDOUT", "COST_SENSITIVITY", "STRESS", "DSR", "PBO", "N_EFF")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ResearchRegistry(Path(self.tmp.name) / "experiments.sqlite")
        self.promo = PromotionService(self.reg)
        self.protocol = self.promo.approve_protocol(FIXTURE_PROTOCOL, self.owner())
        self.started = self.reg.now() + 1
        time.sleep(0.001)
        self.spec = trend_spec([A], DEV, HOLD)
        self.reg.preregister("EXP-P", self.spec, search_space={"fast_span": ["5", "10", "20"]}, budget=5,
                             rejection_criteria=["x"], benchmark="BUY_AND_HOLD", principal=AUTHOR, fixture=True)
        time.sleep(0.001)
        self.manifest = self.make_manifest(self.spec, DEV[1])
        self.trial = self.reg.start_trial("EXP-P", spec=self.spec, segment="DEVELOPMENT", manifest=self.manifest, principal=AUTHOR)
        self.reg.finish_trial(self.trial, status="SUCCEEDED", metrics={})
        self.n_holdouts = 0

    @staticmethod
    def make_manifest(spec, until, extra=None) -> dict:
        cfg = BacktestConfig.from_spec(spec, Fixed(100000))
        return build_manifest(run_kind="DEVELOPMENT", strategy_id=spec.strategy_id, spec_hash=spec.spec_hash,
                              parameters={k: v.to_str() for k, v in spec.parameters.items()}, config=cfg.to_json(),
                              datasets={"FIXTURE": {"fingerprint": "f" * 64, "until": until.isoformat()}}, seeds={},
                              extra=extra, include_environment=False)

    def holdout_evidence(self, art: str) -> tuple[dict, list[dict]]:
        """A real single holdout evaluation for ``art``: its own experiment, a GRANTED access, the HOLDOUT trial."""
        self.n_holdouts += 1
        exp = f"EXP-H{self.n_holdouts}"
        spec = self.reg.spec(self.promo.artifact(art)["spec_hash"])
        self.reg.preregister(exp, self.spec, search_space={"fast_span": ["5", "10", "20"]}, budget=5, rejection_criteria=["x"],
                             benchmark="BUY_AND_HOLD", principal=AUTHOR, fixture=True)
        time.sleep(0.001)
        grant, tid = self.reg.consume_holdout(exp, spec=spec, artifact_hash=art, manifest=self.make_manifest(spec, HOLD[1]),
                                              principal=EVAL)
        self.reg.finish_trial(tid, status="SUCCEEDED", metrics={})
        return ({**PASSING["HOLDOUT"], "holdout_access_id": grant.access_id},
                [{"type": "trial", "id": tid}, {"type": "holdout_access", "id": grant.access_id}])

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def owner(self) -> Principal:
        return Principal("owner", Role.OWNER, authenticated_at_ns=self.reg.now())

    def artifact(self, spec=None, author=AUTHOR) -> str:
        spec = spec or self.spec
        return self.promo.register_artifact(spec, author, code_hash="c" * 64, training_interval=DEV,
                                            evaluation_manifest_hash=self.manifest["manifest_hash"], allowed_markets=["FIXTURE"],
                                            horizon="daily", capacity_estimate="n/a", expires_on=date(2099, 1, 1))

    def ev(self, art: str, kind: str, payload=None, principal=EVAL, started=None) -> str:
        refs = [{"type": "trial", "id": self.trial}]
        if kind == "HOLDOUT" and payload is None:
            payload, refs = self.holdout_evidence(art)
        r = self.promo.add_evidence(art, kind, payload if payload is not None else PASSING[kind],
                                    refs=refs, principal=principal,
                                    evaluation_started_at_ns=started or self.started, protocol_hash=self.protocol)
        return r["evidence_id"]

    def to_validated(self, art: str) -> None:
        self.promo.transition(art, "reproducible", EVAL, evidence_ids=[self.ev(art, "REPLAY_EXACT")], reason="replayed")
        self.promo.transition(art, "validated", EVAL, evidence_ids=[self.ev(art, k) for k in GATES], reason="gates passed")

    def policy_body(self, **over) -> dict:
        body = {"policy_id": "SEL-1", "slot": "TREND/FIXTURE", "strategy_id": "TREND_DAILY_V1",
                "parameter_domain": {"fast_span": ["5", "10"], "slow_span": ["20", "80"], "vol_half_life": ["20", "20"],
                                     "horizon_h": ["10", "10"], "k": ["0.5", "0.5"], "w_max": ["0.45", "0.45"],
                                     "epsilon": ["0.0001", "0.0001"]},
                "instrument_set": [A], "risk_budget": {"max_gross_exposure": "0.95"}, "protocol_hash": self.protocol,
                "max_activations_per_period": 2, "period_days": 30, "ranking": "PAIRED_BOOTSTRAP.lower"}
        body.update(over)
        return body

    def to_eligible(self, art: str) -> None:
        self.to_validated(art)
        self.promo.transition(art, "paper", self.owner(), evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="owner: paper")
        self.promo.transition(art, "eligible", self.owner(), evidence_ids=[self.ev(art, "PAPER_RECONCILIATION")],
                              reason="owner: eligible")


class LifecycleTest(Base):
    def test_full_ladder_with_evidence_and_owner_approvals(self):
        art = self.artifact()
        self.assertEqual(self.promo.status(art), "proposed")
        self.to_eligible(art)
        rec = self.promo.transition(art, "active", self.owner(), reason="owner activates", slot="TREND/FIXTURE")
        self.assertEqual(self.promo.status(art), "active")
        self.assertEqual(rec["approval"]["kind"], "OWNER")
        self.assertEqual(rec["approval"]["by"], "owner")
        self.assertIsInstance(rec["approval"]["at_ns"], int)
        hist = self.promo.history(art)
        self.assertEqual([h["to_status"] for h in hist], ["proposed", "reproducible", "validated", "paper", "eligible", "active"])
        for h in hist:
            if h["to_status"] in ("paper", "eligible", "active"):
                self.assertEqual(h["approval"]["by"], "owner")
        self.assertEqual(self.promo.verify_chain(), [])
        self.assertEqual(set(STATUSES), {"proposed", "reproducible", "validated", "paper", "eligible", "active", "quarantined", "retired"})

    def test_failed_gate_refuses_validation_and_leaves_status_unchanged(self):
        art = self.artifact()
        self.promo.transition(art, "reproducible", EVAL, evidence_ids=[self.ev(art, "REPLAY_EXACT")], reason="replayed")
        bad = {"bootstrap": {**PASSING["PAIRED_BOOTSTRAP"]["bootstrap"], "lower": -0.0001, "passes": True}}
        ids = [self.ev(art, k) for k in GATES if k != "PAIRED_BOOTSTRAP"] + [self.ev(art, "PAIRED_BOOTSTRAP", bad)]
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "validated", EVAL, evidence_ids=ids, reason="try")
        self.assertEqual(self.promo.status(art), "reproducible")
        failing = [e for e in self.promo.evidence(art) if not e["passed"]]
        self.assertEqual([e["kind"] for e in failing], ["PAIRED_BOOTSTRAP"])   # the submitter's 'passes' was ignored

    def test_missing_evidence_refuses(self):
        art = self.artifact()
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "reproducible", EVAL, evidence_ids=[], reason="no replay")

    def test_skipping_rungs_is_refused(self):
        art = self.artifact()
        for to in ("validated", "paper", "eligible", "active"):
            with self.assertRaises(RegistryError):
                self.promo.transition(art, to, self.owner(), reason="skip", slot="S")

    def test_quarantine_empties_the_slot_and_retire_needs_the_owner(self):
        art = self.artifact()
        self.to_eligible(art)
        self.promo.transition(art, "active", self.owner(), reason="go", slot="S1")
        self.promo.transition(art, "quarantined", EVAL, evidence_ids=[self.ev(art, "MONITORING_INCIDENT")], reason="drift")
        self.assertIsNone(self.promo.active_in_slot("S1"))
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "retired", EVAL, reason="system cannot retire")
        self.promo.transition(art, "retired", self.owner(), reason="owner retires")
        self.assertEqual(self.promo.status(art), "retired")


class AuthorityTest(Base):
    def test_agents_can_propose_but_never_approve_or_transition(self):
        art = self.artifact(author=AGENT)
        self.assertEqual(self.promo.artifact(art)["author_role"], "AGENT")
        for to in ("reproducible", "quarantined", "retired"):
            with self.assertRaises(AuthorityError):
                self.promo.transition(art, to, AGENT, evidence_ids=[], reason="agent tries")
        self.assertEqual(self.promo.status(art), "proposed")
        with self.assertRaises(AuthorityError):
            self.promo.approve_protocol({**FIXTURE_PROTOCOL, "min_dsr": "0.10"}, AGENT)
        with self.assertRaises(AuthorityError):
            self.promo.approve_selection_policy(self.policy_body(), AGENT)

    def test_agent_filed_evidence_never_satisfies_a_gate(self):
        art = self.artifact()
        eid = self.ev(art, "REPLAY_EXACT", principal=AGENT)
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "reproducible", EVAL, evidence_ids=[eid], reason="agent evidence")

    def test_the_evaluator_must_not_be_the_author(self):
        art = self.artifact(author=EVAL)
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "reproducible", EVAL, evidence_ids=[self.ev(art, "REPLAY_EXACT")], reason="self")

    def test_eligible_and_active_need_the_owner(self):
        art = self.artifact()
        self.to_validated(art)
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "paper", EVAL, evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="system")
        self.promo.transition(art, "paper", self.owner(), evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="owner")
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "eligible", EVAL, evidence_ids=[self.ev(art, "PAPER_RECONCILIATION")], reason="system")
        self.promo.transition(art, "eligible", self.owner(), evidence_ids=[self.ev(art, "PAPER_RECONCILIATION")], reason="owner")
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "active", EVAL, reason="system without policy", slot="S")

    def test_stale_owner_authentication_is_refused(self):
        art = self.artifact()
        self.to_validated(art)
        stale = Principal("owner", Role.OWNER, authenticated_at_ns=self.reg.now() - 20 * 60 * 10**9)
        with self.assertRaises(AuthorityError):
            self.promo.transition(art, "paper", stale, evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="stale")

    def test_protocol_approved_after_the_evaluation_started_fails_the_evidence(self):
        art = self.artifact()
        early = self.reg.now() - 10**12
        r = self.promo.add_evidence(art, "DSR", PASSING["DSR"], refs=[], principal=EVAL, evaluation_started_at_ns=early,
                                    protocol_hash=self.protocol)
        self.assertFalse(r["passed"])
        self.assertIn("policy precedence", " ".join(r["reasons"]))

    def test_evidence_references_must_exist(self):
        art = self.artifact()
        with self.assertRaises(EvidenceError):
            self.promo.add_evidence(art, "DSR", PASSING["DSR"], refs=[{"type": "trial", "id": "EXP-P:T99999"}], principal=EVAL,
                                    evaluation_started_at_ns=self.started, protocol_hash=self.protocol)

    def test_status_cannot_be_edited_outside_the_promotion_service(self):
        art = self.artifact()
        self.ev(art, "DSR")  # row triggers fire per row: make sure every table below has one
        for sql in ("UPDATE transition SET to_status='active'", "DELETE FROM transition", "UPDATE evidence SET passed=1",
                    "UPDATE artifact SET strategy_id='X'", "UPDATE protocol SET body='{}'"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.reg.conn.execute(sql)
        self.assertEqual(self.promo.status(art), "proposed")
        self.assertEqual(self.promo.verify_chain(), [])

class SelectionPolicyTest(Base):
    def test_automatic_selection_only_inside_the_owner_approved_policy(self):
        inside = self.artifact(self.spec.with_parameters(fast_span="5"))
        better = self.artifact(self.spec.with_parameters(fast_span="10"))
        outside = self.artifact(self.spec.with_parameters(fast_span="20"))
        for art in (inside, better, outside):
            self.to_eligible(art)
        lift = {"bootstrap": {**PASSING["PAIRED_BOOTSTRAP"]["bootstrap"], "lower": 0.0009}}
        self.promo.transition(better, "quarantined", EVAL, evidence_ids=[self.ev(better, "MONITORING_INCIDENT")], reason="q")
        self.ev(outside, "PAIRED_BOOTSTRAP", lift)
        self.promo.approve_selection_policy(self.policy_body(), self.owner())
        system = Principal("promotion.service", Role.SYSTEM)
        out = self.promo.auto_select("SEL-1", system)
        self.assertEqual(out["selected"], inside)          # 'outside' ranks higher but is outside the domain
        self.assertIn(outside, out["refused"])
        self.assertEqual(self.promo.status(inside), "active")
        rec = out["transition"]
        self.assertEqual(rec["approval"]["kind"], "SELECTION_POLICY")
        self.assertEqual(rec["approval"]["approved_by"], "owner")
        with self.assertRaises(AuthorityError):
            self.promo.auto_select("SEL-1", system, artifact_hash=outside)
        with self.assertRaises(AuthorityError):
            self.promo.auto_select("SEL-1", AGENT)
        with self.assertRaises(AuthorityError):
            self.promo.auto_select("SEL-1", self.owner())    # selection runs as the SYSTEM service

    def test_moving_the_active_alias_records_candidate_and_incumbent(self):
        a1 = self.artifact(self.spec.with_parameters(fast_span="5"))
        a2 = self.artifact(self.spec.with_parameters(fast_span="10"))
        for art in (a1, a2):
            self.to_eligible(art)
        self.promo.transition(a1, "active", self.owner(), reason="first", slot="S")
        rec = self.promo.transition(a2, "active", self.owner(), reason="replace", slot="S")
        self.assertEqual(rec["incumbent_hash"], a1)
        self.assertEqual(rec["artifact_hash"], a2)
        self.assertEqual(self.promo.status(a1), "eligible")
        self.assertEqual(self.promo.active_in_slot("S"), a2)
        self.assertEqual(self.promo.history(a1)[-1]["incumbent_hash"], a2)
        self.assertEqual(self.promo.verify_chain(), [])

    def test_policy_requires_the_owner_and_is_immutable(self):
        with self.assertRaises(AuthorityError):
            self.promo.approve_selection_policy(self.policy_body(), Principal("promotion.service", Role.SYSTEM))
        self.promo.approve_selection_policy(self.policy_body(), self.owner())
        with self.assertRaises(RegistryError):
            self.promo.approve_selection_policy(self.policy_body(max_activations_per_period=50), self.owner())

    def test_expired_artifacts_cannot_become_eligible(self):
        art = self.promo.register_artifact(self.spec.with_parameters(fast_span="5"), AUTHOR, code_hash="d" * 64,
                                           training_interval=DEV, evaluation_manifest_hash=self.manifest["manifest_hash"],
                                           allowed_markets=["FIXTURE"], horizon="daily", capacity_estimate="n/a",
                                           expires_on=date.today() - timedelta(days=1))
        self.to_validated(art)
        self.promo.transition(art, "paper", self.owner(), evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="owner")
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "eligible", self.owner(), evidence_ids=[self.ev(art, "PAPER_RECONCILIATION")], reason="x")


class EvidenceBindingReviewTest(Base):
    """Regression tests for the 2026-10-22 adversarial review: evidence is bound to registry facts."""

    def test_holdout_evidence_needs_a_real_grant_for_this_artifact(self):
        art = self.artifact()
        other = self.artifact(self.spec.with_parameters(fast_span="5"))
        fake = {**PASSING["HOLDOUT"], "holdout_access_id": "holdout:1"}           # no such grant exists
        r = self.promo.add_evidence(art, "HOLDOUT", fake, refs=[{"type": "trial", "id": self.trial}], principal=EVAL,
                                    evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertFalse(r["passed"])
        self.assertTrue(any("not a GRANTED" in x for x in r["reasons"]), r["reasons"])
        payload, refs = self.holdout_evidence(other)                              # a grant consumed for ANOTHER artifact
        r = self.promo.add_evidence(art, "HOLDOUT", payload, refs=refs, principal=EVAL, evaluation_started_at_ns=self.started,
                                    protocol_hash=self.protocol)
        self.assertFalse(r["passed"])
        self.assertIn("the holdout was consumed for a different artifact", r["reasons"])
        payload, refs = self.holdout_evidence(art)
        r = self.promo.add_evidence(art, "HOLDOUT", payload, refs=[x for x in refs if x["type"] != "trial"], principal=EVAL,
                                    evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertFalse(r["passed"])                                             # the holdout trial is not cited
        r = self.promo.add_evidence(art, "HOLDOUT", payload, refs=refs, principal=EVAL, evaluation_started_at_ns=self.started,
                                    protocol_hash=self.protocol)
        self.assertTrue(r["passed"], r["reasons"])

    def test_dsr_must_use_the_registry_trial_count(self):
        art = self.artifact()
        tid = self.reg.start_trial("EXP-P", spec=self.spec.with_parameters(fast_span="5"), segment="DEVELOPMENT",
                                   manifest=self.make_manifest(self.spec.with_parameters(fast_span="5"), DEV[1]), principal=AUTHOR)
        self.reg.finish_trial(tid, status="FAILED", error="fixture failure")    # a failed trial still counts: M = 2
        for payload, ok in (({"dsr": 0.97, "M": 1}, False), ({"dsr": 0.97}, False), ({"dsr": 0.97, "M": 2}, True)):
            r = self.promo.add_evidence(art, "DSR", payload, refs=[{"type": "trial", "id": self.trial}], principal=EVAL,
                                        evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
            self.assertEqual(r["passed"], ok, (payload, r["reasons"]))
        r = self.promo.add_evidence(art, "DSR", {"dsr": 0.97, "M": 2}, refs=[], principal=EVAL,
                                    evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertFalse(r["passed"])                                             # no family cited

    def test_stress_evidence_must_cover_every_declared_case(self):
        art = self.artifact()
        r = self.promo.add_evidence(art, "STRESS", {"cases": {"costs_x3": {"max_drawdown": 0.1}}},
                                    refs=[{"type": "trial", "id": self.trial}], principal=EVAL,
                                    evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertFalse(r["passed"])
        self.assertTrue(any("impact_y_max" in x and "limit_tight" in x for x in r["reasons"]), r["reasons"])

    def test_a_late_claimed_start_cannot_dodge_policy_precedence(self):
        """The protocol is approved after the cited trial ran; claiming the evaluation started 'now' does not help."""
        art = self.artifact()
        time.sleep(0.001)
        late = self.promo.approve_protocol({**FIXTURE_PROTOCOL, "min_dsr": "0.10"}, self.owner())
        time.sleep(0.001)
        r = self.promo.add_evidence(art, "DSR", {"dsr": 0.5, "M": 1}, refs=[{"type": "trial", "id": self.trial}],
                                    principal=EVAL, evaluation_started_at_ns=self.reg.now(), protocol_hash=late)
        self.assertFalse(r["passed"])
        self.assertTrue(any("policy precedence" in x for x in r["reasons"]), r["reasons"])
        row = self.reg.conn.execute("SELECT evaluation_started_at_ns FROM evidence WHERE evidence_id=?",
                                    (r["evidence_id"],)).fetchone()
        self.assertEqual(row[0], self.reg.trial(self.trial)["started_at_ns"])   # the effective start is recorded
        r = self.promo.add_evidence(art, "DSR", {"dsr": 0.5, "M": 1}, refs=[], principal=EVAL,
                                    evaluation_started_at_ns=self.reg.now() + 10**12, protocol_hash=self.protocol)
        self.assertIn("evaluation start lies in the future", r["reasons"])


class ActivationReviewTest(Base):
    """Regression tests for activation paths (review 2026-10-22)."""

    def test_automatic_selection_refuses_an_artifact_that_expired_after_becoming_eligible(self):
        art = self.promo.register_artifact(self.spec.with_parameters(fast_span="5"), AUTHOR, code_hash="e" * 64,
                                           training_interval=DEV, evaluation_manifest_hash=self.manifest["manifest_hash"],
                                           allowed_markets=["FIXTURE"], horizon="daily", capacity_estimate="n/a",
                                           expires_on=date.today() + timedelta(days=1))
        self.to_eligible(art)
        self.promo.approve_selection_policy(self.policy_body(), self.owner())
        self.promo._today = lambda: date.today() + timedelta(days=2)             # the artifact has now expired
        system = Principal("promotion.service", Role.SYSTEM)
        out = self.promo.auto_select("SEL-1", system)
        self.assertIsNone(out["selected"])
        self.assertEqual(out["refused"][art], ["artifact expired"])
        with self.assertRaises(AuthorityError):
            self.promo.auto_select("SEL-1", system, artifact_hash=art)
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "active", self.owner(), reason="owner", slot="TREND/FIXTURE")
        self.assertEqual(self.promo.status(art), "eligible")

    def test_an_artifact_moved_to_another_slot_is_not_active_in_its_old_slot(self):
        a1 = self.artifact(self.spec.with_parameters(fast_span="5"))
        a2 = self.artifact(self.spec.with_parameters(fast_span="10"))
        for art in (a1, a2):
            self.to_eligible(art)
        self.promo.transition(a1, "active", self.owner(), reason="first", slot="S1")
        self.promo.transition(a1, "eligible", self.owner(), reason="deactivate")
        self.promo.transition(a1, "active", self.owner(), reason="moved", slot="S2")
        self.assertIsNone(self.promo.active_in_slot("S1"))
        self.assertEqual(self.promo.active_in_slot("S2"), a1)
        rec = self.promo.transition(a2, "active", self.owner(), reason="fill S1", slot="S1")
        self.assertIsNone(rec["incumbent_hash"])                                  # a1 (in S2) is not demoted
        self.assertEqual(self.promo.status(a1), "active")
        self.assertEqual(self.promo.verify_chain(), [])

    def test_automatic_selection_ranks_by_the_conservative_bound_the_gate_used(self):
        loose = self.artifact(self.spec.with_parameters(fast_span="5"))
        tight = self.artifact(self.spec.with_parameters(fast_span="10"))
        for art in (loose, tight):
            self.to_eligible(art)
        base = PASSING["PAIRED_BOOTSTRAP"]["bootstrap"]
        # 'loose' has the higher percentile bound but the lower conservative bound (the one the gate passed on).
        self.ev(loose, "PAIRED_BOOTSTRAP", {"bootstrap": {**base, "lower": 0.0030, "conservative_lower": 0.0001}})
        self.ev(tight, "PAIRED_BOOTSTRAP", {"bootstrap": {**base, "lower": 0.0010, "conservative_lower": 0.0008}})
        self.promo.approve_selection_policy(self.policy_body(), self.owner())
        out = self.promo.auto_select("SEL-1", Principal("promotion.service", Role.SYSTEM))
        self.assertEqual(out["selected"], tight)


class ProtocolTest(Base):
    def test_protocol_is_strict(self):
        for bad in ({**FIXTURE_PROTOCOL, "extra": 1}, {k: v for k, v in FIXTURE_PROTOCOL.items() if k != "hurdle_per_period"},
                    {**FIXTURE_PROTOCOL, "confidence": "0.4"}, {**FIXTURE_PROTOCOL, "replications": 10},
                    {**FIXTURE_PROTOCOL, "max_pbo": "1.5"}):
            with self.assertRaises(EvidenceError):
                self.promo.approve_protocol(bad, self.owner())


if __name__ == "__main__":
    unittest.main()
