"""BR-DAT-16: an artifact's expiry is bounded by the retention of the data its evidence rests on, and an artifact
whose evidence data was deleted is EVIDENCE_EXPIRED: the deletion is filed as its evidence, an active one leaves its
slot, its expiry ends the day before the deletion so it never runs again, and it can never be promoted again.
Manifests, datasets and dates are TEST FIXTURES. The wiring to the point-in-time store is in
test_research_evidence_expiry_wiring."""
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
from unknown.research.manifest import UNBOUNDED, build_manifest  # noqa: E402
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

    def test_the_owner_may_step_down_an_active_artifact_whose_evidence_retention_has_passed(self):
        # Review fix 2026-10-22: the expiry check guards promotions, not the step down out of a slot.
        m = self.retained_manifest(self.spec, DEV[1], (TODAY + timedelta(days=10)).isoformat(), name="VENDOR.short")
        self.store_manifest(m)
        art = self.register(m["manifest_hash"], TODAY + timedelta(days=10))
        self.to_eligible(art)
        self.promo.transition(art, "active", self.owner(), reason="owner", slot="S")
        self.promo._today = lambda: TODAY + timedelta(days=11)            # past the data's retention
        self.promo.transition(art, "eligible", self.owner(), reason="owner steps the expired artifact down")
        self.assertEqual((self.promo.status(art), self.promo.active_in_slot("S")), ("eligible", None))
        with self.assertRaises(EvidenceError):                              # and it cannot go back up
            self.promo.transition(art, "active", self.owner(), reason="owner", slot="S")
        self.assertEqual(self.promo.verify_chain(), [])


class EvidenceExpiredTest(RetentionBase):
    def setUp(self):
        super().setUp()
        self.m = self.retained_manifest(self.spec, DEV[1], UNBOUNDED, name="VENDOR.bars")
        self.store_manifest(self.m)

    def load(self, art: str, mode: str = "live"):
        return artifacts.load(self.reg, artifacts.artifact_id("TREND_DAILY_V1", art), mode=mode, today=TODAY,
                              code_hash="c" * 64)

    def deletion_evidence(self, art: str) -> list[dict]:
        return [e for e in self.promo.evidence(art) if e["kind"] == "EVIDENCE_DELETION" and e["passed"]]

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

    def test_an_active_artifact_on_deleted_data_is_evidence_expired_and_never_runs_again(self):
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        other = self.artifact(self.spec.with_parameters(fast_span="5"))   # rests on the FIXTURE manifest only
        for h in (art, other):
            self.to_eligible(h)
        self.promo.transition(art, "active", self.owner(), reason="owner", slot="S")
        self.assertEqual(self.load(art).status, "active")                 # it runs before the deletion
        self.assertEqual(self.promo.evidence_status(art)["status"], "CURRENT")
        self.promo.record_evidence_deletion("VENDOR.bars", principal=SYSTEM, reason="licence retention ended")
        # the same transaction filed the deletion as its evidence and stepped it down out of its slot
        (ev,) = self.deletion_evidence(art)
        self.assertEqual((ev["payload"]["deletion_id"], ev["refs"], ev["created_by"]),
                         ("dataset:VENDOR.bars", [{"type": "deletion", "id": "dataset:VENDOR.bars"}], SYSTEM.id))
        last = self.promo.history(art)[-1]
        self.assertEqual((last["from_status"], last["to_status"], last["slot"], last["principal"], last["evidence_ids"]),
                         ("active", "eligible", "S", SYSTEM.id, [ev["evidence_id"]]))
        self.assertIn("EVIDENCE_EXPIRED", last["reason"])
        self.assertIsNone(self.promo.active_in_slot("S"))
        st = self.promo.evidence_status(art)
        self.assertEqual((st["status"], [(d["dataset"], d["evidence_id"]) for d in st["deleted"]]),
                         ("EVIDENCE_EXPIRED", [("VENDOR.bars", ev["evidence_id"])]))
        self.assertLess(date.fromisoformat(st["effective_expiry"]), TODAY)  # the deletion ends its expiry
        self.assertEqual((self.promo.status(other), self.promo.evidence_status(other)["status"]), ("eligible", "CURRENT"))
        # the strategy runner refuses it in every mode, even at a status that may run
        for mode in artifacts.MODES:
            with self.assertRaises(ArtifactRefused) as cm:
                self.load(art, mode)
            self.assertEqual(cm.exception.code, "ARTIFACT_EXPIRED")
        self.assertEqual(self.load(other).status, "eligible")
        # every way back up is refused; quarantine and retirement still apply
        with self.assertRaises(EvidenceError):
            self.promo.transition(art, "active", self.owner(), reason="re-activate", slot="S")
        self.promo.approve_selection_policy(self.policy_body(), self.owner())
        system = Principal("promotion.service", Role.SYSTEM)
        out = self.promo.auto_select("SEL-1", system)
        self.assertEqual(out["selected"], other)                            # never the evidence-expired artifact
        self.assertEqual(out["refused"][art], ["artifact expired"])
        with self.assertRaises(AuthorityError):
            self.promo.auto_select("SEL-1", system, artifact_hash=art)
        self.promo.transition(art, "quarantined", EVAL, evidence_ids=[self.ev(art, "MONITORING_INCIDENT")], reason="incident")
        with self.assertRaises(EvidenceError) as cm:
            self.promo.transition(art, "reproducible", self.owner(), evidence_ids=[self.ev(art, "REPLAY_EXACT")],
                                  reason="re-validate on deleted data")
        self.assertIn("EVIDENCE_EXPIRED", str(cm.exception))
        self.promo.transition(art, "retired", self.owner(), reason="owner retires")
        self.assertEqual(self.promo.status(art), "retired")
        self.assertEqual(self.promo.verify_chain(), [])

    def test_every_artifact_on_the_data_gets_the_deletion_as_evidence_and_a_retired_one_is_left_alone(self):
        arts = {fast: self.register(self.m["manifest_hash"], date(2099, 1, 1), fast_span=fast) for fast in ("5", "10", "20")}
        self.promo.transition(arts["10"], "quarantined", EVAL, evidence_ids=[self.ev(arts["10"], "MONITORING_INCIDENT")],
                              reason="incident")
        self.promo.transition(arts["20"], "retired", self.owner(), reason="owner retires")
        self.promo.record_evidence_deletion("VENDOR.bars", principal=self.owner(), reason="owner deleted the raw data")
        self.assertEqual([(self.promo.status(arts[f]), len(self.deletion_evidence(arts[f]))) for f in ("5", "10", "20")],
                         [("proposed", 1), ("quarantined", 1), ("retired", 0)])
        self.assertEqual([self.promo.evidence_status(arts[f])["status"] for f in ("5", "10", "20")],
                         ["EVIDENCE_EXPIRED"] * 3)
        with self.assertRaises(EvidenceError):
            self.promo.transition(arts["5"], "reproducible", EVAL, evidence_ids=[self.ev(arts["5"], "REPLAY_EXACT")],
                                  reason="replayed")
        self.assertEqual(self.promo.verify_chain(), [])

    def test_a_validated_artifact_on_deleted_data_cannot_reach_shadow(self):
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        self.to_validated(art)
        self.promo.record_evidence_deletion("VENDOR.bars", principal=self.owner(), reason="owner deleted the raw data")
        gates = [h for h in self.promo.history(art) if h["to_status"] == "validated"][-1]["evidence_ids"]
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

    def test_nothing_new_may_rest_on_deleted_data(self):
        art = self.artifact()                                              # rests on FIXTURE only
        tid = self.reg.trials("EXP-P", "WALK_FORWARD")[-1]["trial_id"]      # the VENDOR.bars trial of setUp
        self.promo.record_evidence_deletion("VENDOR.bars", principal=SYSTEM, reason="licence retention ended")
        with self.assertRaises(EvidenceError) as cm:                       # registration on deleted data
            self.register(self.m["manifest_hash"], date(2099, 1, 1), fast_span="20")
        self.assertIn("EVIDENCE_EXPIRED", str(cm.exception))
        r = self.promo.add_evidence(art, "WALK_FORWARD_OOS", PASSING["WALK_FORWARD_OOS"], refs=[{"type": "trial", "id": tid}],
                                    principal=EVAL, evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertFalse(r["passed"])
        self.assertTrue(any("whose data was deleted" in why for why in r["reasons"]), r["reasons"])
        self.assertEqual((self.promo.status(art), self.promo.evidence_status(art)["status"]), ("proposed", "CURRENT"))

    def test_deletion_evidence_is_bound_to_a_recorded_deletion_that_touches_the_artifact(self):
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        other = self.artifact(self.spec.with_parameters(fast_span="5"))
        with self.assertRaises(EvidenceError):                             # an unrecorded deletion is not a reference
            self.promo.add_evidence(art, "EVIDENCE_DELETION", {"deletion_id": "dataset:VENDOR.bars", "dataset": "VENDOR.bars",
                                                               "deleted_at_ns": 1}, refs=[{"type": "deletion",
                                                                                           "id": "dataset:VENDOR.bars"}],
                                    principal=self.owner(), evaluation_started_at_ns=self.started)
        self.promo.record_evidence_deletion("VENDOR.bars", principal=SYSTEM, reason="licence retention ended")
        (d,) = self.promo.deletions()
        payload = {"deletion_id": d["deletion_id"], "dataset": d["dataset"], "deleted_at_ns": d["deleted_at_ns"]}
        refs = [{"type": "deletion", "id": d["deletion_id"]}]
        # an agent's filing, a payload that differs from the record, or a deletion that missed the artifact never passes
        self.assertFalse(self.promo.add_evidence(art, "EVIDENCE_DELETION", payload, refs=refs, principal=AGENT,
                                                 evaluation_started_at_ns=self.started)["passed"])
        self.assertFalse(self.promo.add_evidence(art, "EVIDENCE_DELETION", {**payload, "deleted_at_ns": 0}, refs=refs,
                                                 principal=self.owner(), evaluation_started_at_ns=self.started)["passed"])
        uncited = self.promo.add_evidence(art, "EVIDENCE_DELETION", payload, refs=[], principal=self.owner(),
                                          evaluation_started_at_ns=self.started)
        self.assertFalse(uncited["passed"])
        self.assertIn("the evidence does not cite the recorded deletion", uncited["reasons"])
        missed = self.promo.add_evidence(other, "EVIDENCE_DELETION", payload, refs=refs, principal=self.owner(),
                                         evaluation_started_at_ns=self.started)
        self.assertFalse(missed["passed"])
        self.assertIn("the deletion did not remove data this artifact's evidence rests on", missed["reasons"])
        self.assertEqual(self.promo.evidence_status(other)["status"], "CURRENT")

    def test_the_sweep_completes_a_deletion_recorded_before_deletions_were_filed_as_evidence(self):
        from unknown.data.pit import ResearchStore
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        self.to_eligible(art)
        self.promo.transition(art, "active", self.owner(), reason="owner", slot="S")
        # a registry written by the earlier code: the deletion row exists, the artifact neither cites it nor stepped down
        self.reg.conn.execute("INSERT INTO evidence_deletion VALUES (?,?,?,?,?,?)",
                              ("VENDOR.bars", self.reg.now(), "licence ended", self.reg.now(), SYSTEM.id, "SYSTEM"))
        st = self.promo.evidence_status(art)
        self.assertEqual((self.promo.status(art), st["status"], st["deleted"][0]["evidence_id"]),
                         ("active", "EVIDENCE_EXPIRED", None))
        with self.assertRaises(ArtifactRefused):                          # the expiry bound alone already stops it
            self.load(art)
        store = ResearchStore(":memory:")
        try:
            out = self.promo.sweep_store_deletions(store, principal=SYSTEM)
            self.assertEqual((out["recorded"], [(x["artifact_hash"], x["deactivated"]) for x in out["expired"]]),
                             ([], [(art, True)]))
            self.assertEqual((self.promo.status(art), len(self.deletion_evidence(art))), ("eligible", 1))
            self.assertEqual(self.promo.sweep_store_deletions(store, principal=SYSTEM)["expired"], [])
        finally:
            store.close()

    # ---- review regressions (2026-10-22)
    def test_a_deletion_dated_after_it_was_recorded_expires_the_artifact_from_the_recording(self):
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        self.to_eligible(art)                                              # eligible may run live
        later = self.reg.now() + 30 * 86_400 * 10**9
        self.promo.record_evidence_deletion("VENDOR.bars", principal=SYSTEM, reason="deletion scheduled", deleted_at_ns=later)
        self.assertEqual(self.promo.evidence_status(art)["status"], "EVIDENCE_EXPIRED")
        self.assertLess(self.promo.effective_expiry(art), TODAY)
        for mode in artifacts.MODES:                                       # it never runs again, not until `later`
            with self.assertRaises(ArtifactRefused) as cm:
                self.load(art, mode)
            self.assertEqual(cm.exception.code, "ARTIFACT_EXPIRED")

    def test_an_active_artifact_that_already_cites_the_deletion_still_leaves_its_slot(self):
        from unknown.data.pit import ResearchStore
        art = self.register(self.m["manifest_hash"], date(2099, 1, 1))
        self.to_eligible(art)
        self.promo.transition(art, "active", self.owner(), reason="owner", slot="S")
        # a deletion recorded by the earlier code, and the owner filing it as evidence before any sweep
        self.reg.conn.execute("INSERT INTO evidence_deletion VALUES (?,?,?,?,?,?)",
                              ("VENDOR.bars", self.reg.now(), "licence ended", self.reg.now(), SYSTEM.id, "SYSTEM"))
        (d,) = self.promo.deletions()
        filed = self.promo.add_evidence(art, "EVIDENCE_DELETION", {"deletion_id": d["deletion_id"], "dataset": d["dataset"],
                                                                   "deleted_at_ns": d["deleted_at_ns"]},
                                        refs=[{"type": "deletion", "id": d["deletion_id"]}], principal=self.owner(),
                                        evaluation_started_at_ns=self.started)
        self.assertTrue(filed["passed"], filed["reasons"])
        store = ResearchStore(":memory:")
        try:
            out = self.promo.sweep_store_deletions(store, principal=SYSTEM)
        finally:
            store.close()
        self.assertEqual([(x["artifact_hash"], x["deactivated"], x["evidence_id"]) for x in out["expired"]],
                         [(art, True, filed["evidence_id"])])
        last = self.promo.history(art)[-1]
        self.assertEqual((last["from_status"], last["to_status"], last["evidence_ids"]),
                         ("active", "eligible", [filed["evidence_id"]]))
        self.assertIsNone(self.promo.active_in_slot("S"))
        self.assertEqual(len(self.deletion_evidence(art)), 1)              # the owner's filing is reused, not repeated
        self.assertEqual(self.promo.verify_chain(), [])

    def test_a_legacy_entry_without_retention_is_reported_unrecorded(self):
        art = self.artifact()                                              # Base's FIXTURE manifest states no retention
        st = self.promo.evidence_status(art)
        self.assertEqual((st["retention_unrecorded"], st["datasets"]["FIXTURE"]["retention"]), (["FIXTURE"], "unrecorded"))
        art2 = self.register(self.m["manifest_hash"], date(2099, 1, 1), fast_span="20")
        self.assertEqual(self.promo.evidence_status(art2)["datasets"]["VENDOR.bars"]["retention"], UNBOUNDED)
        with self.assertRaises(ValueError):
            self.retained_manifest(self.spec, DEV[1], "next year")


if __name__ == "__main__":
    unittest.main()
