"""BR-DAT-16 wired end to end: manifest retention read from the point-in-time store and the provider registry, the
artifact's effective expiry, the store's deletions (a licence retention that elapses, a rights withdrawal), the pull
sweep that makes the artifacts resting on them EVIDENCE_EXPIRED with the deletion as evidence, and a real strategy-runner
cycle refusing them.

Every provider, dataset, bar, instrument and date here is a TEST FIXTURE (synthetic bars; no licensed data)."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_research_promotion import A, AGENT, AUTHOR, DEV, EVAL, PASSING, Base  # noqa: E402
from unknown.data.pit import NS_PER_DAY, ResearchStore  # noqa: E402
from unknown.data.registry import RegistryError as DataRegistryError, parse_provider  # noqa: E402
from unknown.domain.clock import ManualClock, utc_ns  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402
from unknown.research.bars import load_series, pit_manifest_entry, record_bars  # noqa: E402
from unknown.research.common import AuthorityError, EvidenceError  # noqa: E402
from unknown.research.engine import BacktestConfig  # noqa: E402
from support.research_fixtures import synthetic_bars  # noqa: E402
from unknown.research.manifest import UNBOUNDED, build_manifest, retention_date  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.strategy import artifacts  # noqa: E402
from unknown.strategy.artifacts import ArtifactRefused  # noqa: E402
from unknown.strategy.runner import RunnerConfig, StrategyRunner  # noqa: E402

LICENSED = "FIXTURE.vendor_daily_bars.v1"      # the vendor's raw files carry the licence's deletion deadline
ROLLING = "FIXTURE.rolling_daily_bars.v1"      # the provider registry sets a retention period from receipt
PARTNER = "FIXTURE.partner_daily_bars.v1"      # no retention; the partner later withdraws the rights
B = "FIX-PRO-2"
SESSIONS = 30
CUTOFF = utc_ns(2022, 12, 31)                  # the runs read everything available by the end of DEV
SYSTEM = Principal("data.retention", Role.SYSTEM)
SLOT = "TREND/FIXTURE"


def fixture_providers() -> dict:
    def ds(dataset_id: str, retention_days: int | None) -> dict:
        return {"dataset_id": dataset_id, "description": "synthetic daily bars (TEST FIXTURE)", "endpoint": "local",
                "coverage": "fixture instruments", "history_depth": "fixture", "update_frequency": "daily",
                "availability_rule": "bar end plus the fixture lag", "allowed_uses": ["display", "storage"],
                "retention_days": retention_days, "vintage_support": True,
                "rights_source": "TEST FIXTURE: no third-party rights", "rights_verified_on": "2026-10-22"}
    p = parse_provider({
        "provider_id": "fixture-vendor", "name": "Fixture vendor (TEST FIXTURE)", "access": "local", "cost_model": "free",
        "budget": "none", "rate_limit": {"max_requests": 1, "per_seconds": 1.0, "source": "fixture"},
        "failure_behavior": "fixture", "sources": ["tests"], "verified_on": "2026-10-22",
        "datasets": [ds(LICENSED, None), ds(ROLLING, 30), ds(PARTNER, None)]})
    return {p.provider_id: p}


class WiringBase(Base):
    def setUp(self):
        super().setUp()
        self.providers = fixture_providers()
        self.store = ResearchStore(Path(self.tmp.name) / "research.sqlite")
        self.raw_dir = self.store.raw_dir
        self.now_ns = time.time_ns()
        self.received = self.now_ns - 2 * NS_PER_DAY
        self.today = datetime.now(timezone.utc).date()
        self.shas: dict[tuple[str, str], str] = {}

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def ingest(self, dataset: str, instruments: list[str], retention_until_ns: int | None = None) -> None:
        """One raw vendor file per instrument (carrying the licence deadline, if any) and its bars, which cite it."""
        for i, inst in enumerate(instruments):
            bars = synthetic_bars("trend", inst, start=DEV[0], sessions=SESSIONS, seed=11 + i)
            payload = json.dumps({"dataset": dataset, "bars": [b.to_json() for b in bars]}, sort_keys=True).encode()
            sha = self.store.put_raw(payload, provider="fixture-vendor", dataset=dataset, request={"instrument": inst},
                                     receipt_time_ns=self.received, media_type="application/json",
                                     retention_until_ns=retention_until_ns)
            record_bars(self.store, [dataclasses.replace(b, source=f"raw:{sha}") for b in bars], dataset, self.received)
            self.shas[(dataset, inst)] = sha

    def pit_manifest(self, dataset: str, instruments: list[str], tag: str) -> tuple[dict, dict]:
        """A run's manifest built from the store (what the run read) and stored in the registry with its trial."""
        series = load_series(self.store, dataset, instruments, CUTOFF)
        entry = pit_manifest_entry(self.store, dataset, series, DEV[1], CUTOFF, providers=self.providers)
        cfg = BacktestConfig.from_spec(self.spec, Fixed(100000))
        m = build_manifest(run_kind="WALK_FORWARD", strategy_id=self.spec.strategy_id, spec_hash=self.spec.spec_hash,
                           parameters={k: v.to_str() for k, v in self.spec.parameters.items()}, config=cfg.to_json(),
                           datasets={dataset: entry}, seeds={}, extra={"tag": tag}, include_environment=False)
        tid = self.reg.start_trial("EXP-P", spec=self.spec, segment="WALK_FORWARD", manifest=m, principal=AUTHOR)
        self.reg.finish_trial(tid, status="SUCCEEDED", metrics={})
        return m, entry

    def register(self, manifest_hash: str, expires_on: date, fast_span: str) -> str:
        return self.promo.register_artifact(self.spec.with_parameters(fast_span=fast_span), AUTHOR, code_hash="c" * 64,
                                            training_interval=DEV, evaluation_manifest_hash=manifest_hash,
                                            allowed_markets=["FIXTURE"], horizon="daily", capacity_estimate="n/a",
                                            expires_on=expires_on)

    def runner_cycle_refusal(self, art: str, mode: str = "live", at_ns: int | None = None) -> str:
        """Run one real strategy-runner cycle (at ``at_ns``, default now) and return the refusal code. NO_STRATEGY_TOKEN
        means the artifact itself was accepted (the runner got past ``artifacts.load`` to asking for its token);
        anything else is the runner refusing the artifact."""
        aid = artifacts.artifact_id("TREND_DAILY_V1", art)
        cfg = RunnerConfig(aid, "SIM-1", mode, Paths(Path(self.tmp.name) / f"home-{mode}"), registry_path=self.reg.path)
        runner = StrategyRunner(cfg, code_hash="c" * 64, clock=ManualClock(at_ns if at_ns is not None else time.time_ns()))
        try:
            with self.assertRaises(ArtifactRefused) as cm:
                runner.run_once()
            return cm.exception.code
        finally:
            runner.close()

    def activate(self, art: str) -> None:
        self.to_eligible(art)
        self.promo.transition(art, "active", self.owner(), reason="owner activates", slot=SLOT)


class ManifestRetentionTest(WiringBase):
    def test_the_manifest_records_the_earliest_retention_of_the_observations_read(self):
        deadline = self.now_ns + 10 * NS_PER_DAY
        self.ingest(LICENSED, [A], retention_until_ns=deadline)
        self.ingest(LICENSED, [B], retention_until_ns=deadline - 3 * NS_PER_DAY)     # read only by a [A, B] run
        _, entry = self.pit_manifest(LICENSED, [A], "a")
        self.assertEqual(entry["retention_until"], retention_date(deadline).isoformat())
        self.assertEqual(retention_date(deadline), datetime.fromtimestamp(deadline // 10**9, tz=timezone.utc).date()
                         - timedelta(days=1))                                       # the day before the deadline
        self.assertEqual(entry["scope"], {"entities": [A], "available_until_ns": str(CUTOFF)})
        self.assertEqual((entry["retention"]["observations"], entry["retention"]["deadline_ns"]),
                         (SESSIONS * 6, str(deadline)))                             # 5 bar fields + interval per bar
        _, both = self.pit_manifest(LICENSED, [A, B], "ab")
        self.assertEqual(both["retention_until"], retention_date(deadline - 3 * NS_PER_DAY).isoformat())

    def test_a_registry_retention_period_runs_from_receipt_and_no_retention_is_unbounded(self):
        self.ingest(ROLLING, [A])
        self.ingest(PARTNER, [A])
        _, rolling = self.pit_manifest(ROLLING, [A], "rolling")
        self.assertEqual(rolling["retention_until"], retention_date(self.received + 30 * NS_PER_DAY).isoformat())
        self.assertEqual(rolling["retention"]["registry_retention_days"], 30)
        _, partner = self.pit_manifest(PARTNER, [A], "partner")
        self.assertEqual((partner["retention_until"], partner["retention"]["deadline_ns"]), (UNBOUNDED, None))

    def test_unregistered_data_has_no_retention_to_state(self):
        self.ingest("FIXTURE.unregistered_bars", [A])
        series = load_series(self.store, "FIXTURE.unregistered_bars", [A], CUTOFF)
        with self.assertRaises(DataRegistryError):
            pit_manifest_entry(self.store, "FIXTURE.unregistered_bars", series, DEV[1], CUTOFF, providers=self.providers)

    def test_the_artifact_expiry_is_the_earlier_of_its_declared_expiry_and_its_evidence_retention(self):
        self.ingest(LICENSED, [A], retention_until_ns=self.now_ns + 10 * NS_PER_DAY)
        m, entry = self.pit_manifest(LICENSED, [A], "a")
        bound = date.fromisoformat(entry["retention_until"])
        with self.assertRaises(EvidenceError):                     # registration may not outlive the data
            self.register(m["manifest_hash"], date(2099, 1, 1), "5")
        early = self.register(m["manifest_hash"], bound - timedelta(days=3), "5")
        self.assertEqual(self.promo.effective_expiry(early), bound - timedelta(days=3))
        # a later trial on data retained for less time, cited by passed evidence, shortens an artifact resting on it
        self.ingest(ROLLING, [A])
        art = self.artifact()                                       # Base FIXTURE evaluation manifest, 2099
        self.assertEqual(self.promo.effective_expiry(art), date(2099, 1, 1))
        rolling_m, rolling = self.pit_manifest(ROLLING, [A], "rolling")
        (trial,) = [t for t in self.reg.trials("EXP-P", "WALK_FORWARD") if t["manifest_hash"] == rolling_m["manifest_hash"]]
        r = self.promo.add_evidence(art, "WALK_FORWARD_OOS", PASSING["WALK_FORWARD_OOS"],
                                    refs=[{"type": "trial", "id": trial["trial_id"]}], principal=EVAL,
                                    evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertTrue(r["passed"], r["reasons"])
        self.assertEqual(self.promo.effective_expiry(art), date.fromisoformat(rolling["retention_until"]))


class RetentionElapsesTest(WiringBase):
    def test_retention_elapses_the_store_deletes_and_the_sweep_expires_the_active_artifact(self):
        deadline = self.now_ns + 10 * NS_PER_DAY
        self.ingest(LICENSED, [A], retention_until_ns=deadline)
        m, entry = self.pit_manifest(LICENSED, [A], "a")
        last_day = date.fromisoformat(entry["retention_until"])
        art = self.register(m["manifest_hash"], last_day, "10")
        other = self.artifact(self.spec.with_parameters(fast_span="5"))        # rests on the FIXTURE manifest only
        self.activate(art)
        self.to_eligible(other)
        self.assertEqual(self.runner_cycle_refusal(art), "NO_STRATEGY_TOKEN")    # accepted: it runs today
        # the expiry alone stops it the day after the last retained day, before anything is deleted
        with self.assertRaises(ArtifactRefused) as cm:
            artifacts.load(self.reg, artifacts.artifact_id("TREND_DAILY_V1", art), mode="live",
                           today=last_day + timedelta(days=1), code_hash="c" * 64)
        self.assertEqual(cm.exception.code, "ARTIFACT_EXPIRED")

        self.assertEqual(self.store.enforce_retention(deadline - 1), [])          # not due yet: nothing deleted
        (event,) = self.store.enforce_retention(deadline)
        sha = self.shas[(LICENSED, A)]
        self.assertEqual((event["dataset"], event["cause"], event["deleted_at_ns"], event["observations"],
                          event["raw_objects"], sorted(event["entities"])),
                         (LICENSED, "RETENTION_ELAPSED", deadline, SESSIONS * 6, [sha], [A]))
        self.assertFalse((self.raw_dir / sha).exists())                          # the vendor file is gone
        self.assertEqual(self.store.conn.execute("SELECT deleted_at_ns FROM raw_object WHERE sha256=?", (sha,))
                         .fetchone()[0], deadline)                               # its hash and request are kept
        self.assertEqual(load_series(self.store, LICENSED, [A], CUTOFF)[A].dates, ())
        self.assertEqual(self.promo.evidence_status(art)["status"], "CURRENT")   # pull-based: not yet known
        self.assertEqual(self.runner_cycle_refusal(art, at_ns=deadline), "ARTIFACT_EXPIRED")   # the date bound holds

        with self.assertRaises(AuthorityError):
            self.promo.sweep_store_deletions(self.store, principal=AGENT)
        out = self.promo.sweep_store_deletions(self.store, principal=SYSTEM)
        self.assertIn(LICENSED, out["datasets"])
        self.assertEqual((out["recorded"], [(x["artifact_hash"], x["deactivated"]) for x in out["expired"]]),
                         ([event["event_id"]], [(art, True)]))
        st = self.promo.evidence_status(art)
        self.assertEqual((st["status"], [d["deletion_id"] for d in st["deleted"]]), ("EVIDENCE_EXPIRED", [event["event_id"]]))
        last = self.promo.history(art)[-1]                                       # it left its slot, citing the deletion
        (ev,) = [e for e in self.promo.evidence(art) if e["evidence_id"] in last["evidence_ids"]]
        self.assertEqual((last["from_status"], last["to_status"], ev["kind"], ev["passed"], ev["payload"]["deletion_id"],
                          ev["payload"]["cause"], ev["payload"]["manifests"]),
                         ("active", "eligible", "EVIDENCE_DELETION", True, event["event_id"], "RETENTION_ELAPSED",
                          [m["manifest_hash"]]))
        self.assertIsNone(self.promo.active_in_slot(SLOT))
        for mode in ("live", "shadow"):
            self.assertEqual(self.runner_cycle_refusal(art, mode, at_ns=deadline), "ARTIFACT_EXPIRED")
        # and it never comes back: the deletion (dated at the licence deadline, recorded today) already ended its expiry
        self.assertLess(self.promo.effective_expiry(art), self.today)
        with self.assertRaises(EvidenceError) as cm:
            self.promo.transition(art, "active", self.owner(), reason="owner", slot=SLOT)
        self.assertIn("artifact expired", str(cm.exception))
        self.assertEqual((self.promo.status(other), self.promo.evidence_status(other)["status"]), ("eligible", "CURRENT"))
        again = self.promo.sweep_store_deletions(self.store, principal=SYSTEM)  # idempotent
        self.assertEqual((again["recorded"], again["expired"]), ([], []))
        self.assertEqual(len([e for e in self.promo.evidence(art) if e["kind"] == "EVIDENCE_DELETION"]), 1)
        with self.assertRaises(EvidenceError):                                   # nothing new rests on it
            self.register(m["manifest_hash"], last_day, "20")
        with self.assertRaises(sqlite3.DatabaseError):                           # recorded once, append-only
            self.reg.conn.execute("DELETE FROM evidence_deletion_event")
        self.assertEqual(self.promo.verify_chain(), [])

    def test_a_registry_retention_period_is_enforced_from_receipt(self):
        self.ingest(ROLLING, [A])
        m, entry = self.pit_manifest(ROLLING, [A], "rolling")
        art = self.register(m["manifest_hash"], date.fromisoformat(entry["retention_until"]), "10")
        days = {ROLLING: 30}
        self.assertEqual(self.store.enforce_retention(self.now_ns, retention_days=days), [])
        (event,) = self.store.enforce_retention(self.received + 30 * NS_PER_DAY, retention_days=days)
        sha = self.shas[(ROLLING, A)]
        self.assertEqual((event["dataset"], event["observations"], event["raw_objects"]), (ROLLING, SESSIONS * 6, [sha]))
        self.assertFalse((self.raw_dir / sha).exists())          # the vendor file goes with what was extracted from it
        out = self.promo.sweep_store_deletions(self.store, principal=SYSTEM)
        self.assertEqual([(x["artifact_hash"], x["deactivated"]) for x in out["expired"]], [(art, False)])
        self.assertEqual((self.promo.status(art), self.promo.evidence_status(art)["status"]), ("proposed", "EVIDENCE_EXPIRED"))


class RightsWithdrawnTest(WiringBase):
    def test_a_rights_withdrawal_expires_exactly_the_artifacts_that_read_the_withdrawn_data(self):
        self.ingest(PARTNER, [A, B])
        m_a, entry_a = self.pit_manifest(PARTNER, [A], "a")
        m_b, _ = self.pit_manifest(PARTNER, [B], "b")
        self.assertEqual(entry_a["retention_until"], UNBOUNDED)
        on_a = self.register(m_a["manifest_hash"], date(2099, 1, 1), "5")       # no retention: the declared expiry
        on_b = self.register(m_b["manifest_hash"], date(2099, 1, 1), "20")
        self.assertEqual(self.promo.effective_expiry(on_a), date(2099, 1, 1))
        self.activate(on_a)
        self.to_eligible(on_b)

        # the partner withdraws the rights to B's data only
        withdrawal = self.store.withdraw_rights(PARTNER, reason="partner withdrew the rights to FIX-PRO-2",
                                                at_ns=self.now_ns, entity_ids=[B])
        self.assertEqual((withdrawal["cause"], sorted(withdrawal["entities"]), withdrawal["raw_objects"]),
                         ("RIGHTS_WITHDRAWN", [B], [self.shas[(PARTNER, B)]]))
        self.assertEqual(load_series(self.store, PARTNER, [B], CUTOFF)[B].dates, ())
        self.assertEqual(len(load_series(self.store, PARTNER, [A], CUTOFF)[A].dates), SESSIONS)
        out = self.promo.sweep_store_deletions(self.store, principal=SYSTEM)
        self.assertEqual([x["artifact_hash"] for x in out["expired"]], [on_b])
        self.assertEqual([self.promo.evidence_status(h)["status"] for h in (on_a, on_b)], ["CURRENT", "EVIDENCE_EXPIRED"])
        self.assertEqual((self.promo.status(on_a), self.promo.active_in_slot(SLOT)), ("active", on_a))
        self.assertEqual(self.runner_cycle_refusal(on_a), "NO_STRATEGY_TOKEN")  # A's data is intact: it still runs
        self.assertEqual(self.runner_cycle_refusal(on_b), "ARTIFACT_EXPIRED")   # eligible, but expired by the deletion

        # then the whole dataset
        whole = self.store.withdraw_rights(PARTNER, reason="partner agreement terminated", at_ns=self.now_ns + 1)
        self.assertEqual((sorted(whole["entities"]), whole["raw_objects"]), ([A], [self.shas[(PARTNER, A)]]))
        out = self.promo.sweep_store_deletions(self.store, principal=self.owner())
        self.assertEqual([(x["artifact_hash"], x["deactivated"]) for x in out["expired"]], [(on_a, True)])
        self.assertEqual((self.promo.status(on_a), self.promo.active_in_slot(SLOT)), ("eligible", None))
        for mode in ("live", "shadow"):
            self.assertEqual(self.runner_cycle_refusal(on_a, mode), "ARTIFACT_EXPIRED")
        st = self.promo.evidence_status(on_a)
        self.assertEqual([(d["cause"], d["deleted_at_ns"]) for d in st["deleted"]], [("RIGHTS_WITHDRAWN", self.now_ns + 1)])
        with self.assertRaises(ValueError):                                      # the payload is not stored again
            self.ingest(PARTNER, [A])
        self.assertEqual(self.promo.verify_chain(), [])

    def test_a_deletion_of_data_the_run_did_not_read_expires_nothing(self):
        self.ingest(PARTNER, [A])
        m, _ = self.pit_manifest(PARTNER, [A], "a")
        art = self.register(m["manifest_hash"], date(2099, 1, 1), "5")
        # A's later sessions arrive in a separate vendor file with a short licence; they became available after the
        # cutoff the run read up to, so their deletion removes nothing the run read
        late = synthetic_bars("trend", A, start=date(2023, 6, 1), sessions=3, seed=5)
        sha = self.store.put_raw(json.dumps([b.to_json() for b in late]).encode(), provider="fixture-vendor",
                                 dataset=PARTNER, request={"instrument": A, "part": "late"}, receipt_time_ns=self.received,
                                 media_type="application/json", retention_until_ns=self.now_ns + NS_PER_DAY)
        record_bars(self.store, [dataclasses.replace(b, source=f"raw:{sha}") for b in late], PARTNER, self.received)
        (expired_late,) = self.store.enforce_retention(self.now_ns + NS_PER_DAY)
        self.assertGreater(expired_late["entities"][A]["first_available_ns"], CUTOFF)
        other = self.store.withdraw_rights(PARTNER, reason="fixture", at_ns=self.now_ns, entity_ids=["FIX-NOT-READ"])
        self.assertEqual((other["observations"], other["entities"]), (0, {}))
        out = self.promo.sweep_store_deletions(self.store, principal=SYSTEM)
        self.assertEqual((sorted(out["recorded"]), out["expired"]), (sorted([expired_late["event_id"], other["event_id"]]), []))
        self.assertEqual((self.promo.status(art), self.promo.evidence_status(art)["status"]), ("proposed", "CURRENT"))
        self.assertEqual(len(load_series(self.store, PARTNER, [A], CUTOFF)[A].dates), SESSIONS)


class ReviewRegressionTest(WiringBase):
    """Adversarial review of the wiring (2026-10-22)."""

    def test_the_sweep_records_deletions_of_data_no_artifact_rests_on_yet(self):
        # A run read the partner's data, but no artifact rests on it when the rights are withdrawn and the sweep runs.
        self.ingest(PARTNER, [A])
        m, _ = self.pit_manifest(PARTNER, [A], "a")
        (trial,) = [t for t in self.reg.trials("EXP-P", "WALK_FORWARD") if t["manifest_hash"] == m["manifest_hash"]]
        art = self.artifact()                                                   # rests on the FIXTURE manifest only
        event = self.store.withdraw_rights(PARTNER, reason="partner withdrew the rights", at_ns=self.now_ns)
        out = self.promo.sweep_store_deletions(self.store, principal=SYSTEM)
        self.assertIn(PARTNER, out["datasets"])
        self.assertEqual((out["recorded"], out["expired"]), ([event["event_id"]], []))
        with self.assertRaises(EvidenceError) as cm:                             # nothing new may rest on it
            self.register(m["manifest_hash"], date(2099, 1, 1), "5")
        self.assertIn("EVIDENCE_EXPIRED", str(cm.exception))
        r = self.promo.add_evidence(art, "WALK_FORWARD_OOS", PASSING["WALK_FORWARD_OOS"],
                                    refs=[{"type": "trial", "id": trial["trial_id"]}], principal=EVAL,
                                    evaluation_started_at_ns=self.started, protocol_hash=self.protocol)
        self.assertFalse(r["passed"])
        self.assertTrue(any("whose data was deleted" in why for why in r["reasons"]), r["reasons"])
        self.assertEqual(self.promo.evidence_status(art)["status"], "CURRENT")

    def test_a_withdrawal_whose_data_spans_the_cutoff_touches_the_run(self):
        self.ingest(PARTNER, [A])
        late = synthetic_bars("trend", A, start=date(2023, 6, 1), sessions=3, seed=5)      # available after the cutoff
        sha = self.store.put_raw(json.dumps([b.to_json() for b in late]).encode(), provider="fixture-vendor",
                                 dataset=PARTNER, request={"instrument": A, "part": "late"}, receipt_time_ns=self.received,
                                 media_type="application/json")
        record_bars(self.store, [dataclasses.replace(b, source=f"raw:{sha}") for b in late], PARTNER, self.received)
        m, _ = self.pit_manifest(PARTNER, [A], "a")
        art = self.register(m["manifest_hash"], date(2099, 1, 1), "5")
        ev = self.store.withdraw_rights(PARTNER, reason="rights to FIX-PRO-1 withdrawn", at_ns=self.now_ns, entity_ids=[A])
        span = ev["entities"][A]
        self.assertLessEqual(int(span["first_available_ns"]), CUTOFF)
        self.assertGreater(int(span["last_available_ns"]), CUTOFF)
        out = self.promo.sweep_store_deletions(self.store, principal=SYSTEM)
        self.assertEqual([x["artifact_hash"] for x in out["expired"]], [art])
        self.assertEqual(self.promo.evidence_status(art)["status"], "EVIDENCE_EXPIRED")


if __name__ == "__main__":
    unittest.main()
