"""Pre-registered experiments, the complete trial registry and reproducibility manifests (CAP-RES-10, CAP-RES-11;
RES-04; AUT-04; BR-DAT-07). All data are TEST FIXTURES; registries live in temporary directories."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from unknown.domain.fixed import Fixed
from unknown.domain.principal import Principal, Role
from unknown.research.bars import BarSeries, DatasetView, HoldoutGrant, fingerprint_until
from unknown.research.common import AuthorityError, HoldoutError, RegistryError
from unknown.research.engine import BacktestConfig, run_backtest
from support.research_fixtures import fixture_instrument, recipe, synthetic_bars
from unknown.research.manifest import build_manifest, compare_replay, diff_manifests, verify_manifest
from unknown.research.registry import ResearchRegistry, default_path
from unknown.research.strategies import build, buy_and_hold_spec, trend_spec

F = Fixed.parse
A = "FIX-REG-1"
OWNER = Principal("owner", Role.OWNER, authenticated_at_ns=1)
SYSTEM = Principal("research.evaluator", Role.SYSTEM)
AGENT = Principal("agent.research", Role.AGENT)
STRATEGY = Principal("strategy.x", Role.STRATEGY)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ResearchRegistry(Path(self.tmp.name) / "research" / "experiments.sqlite")
        self.bars = synthetic_bars("trend", A, start=date(2022, 1, 3), sessions=200, seed=1)
        days = [b.session_date for b in self.bars]
        self.dev, self.holdout = (days[0], days[139]), (days[140], days[-1])
        self.spec = trend_spec([A], self.dev, self.holdout, parameters={"fast_span": "5", "slow_span": "20"},
                               grid={"fast_span": ["5", "10"], "slow_span": ["20"]})
        self.series = {A: BarSeries(A, self.bars)}

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def prereg(self, exp="EXP-1", budget=3, principal=SYSTEM):
        return self.reg.preregister(exp, self.spec, search_space={"fast_span": ["5", "10"], "slow_span": ["20"]}, budget=budget,
                                    rejection_criteria=["paired lower bound <= 0"], benchmark="BUY_AND_HOLD",
                                    principal=principal, fixture=True)

    def manifest(self, spec, until=None, extra=None):
        cfg = BacktestConfig.from_spec(spec, F("100000"))
        return build_manifest(run_kind="DEVELOPMENT", strategy_id=spec.strategy_id, spec_hash=spec.spec_hash,
                              parameters={k: v.to_str() for k, v in spec.parameters.items()}, config=cfg.to_json(),
                              datasets={"FIXTURE": {"fingerprint": fingerprint_until(self.series, until or self.dev[1]),
                                                    "until": (until or self.dev[1]).isoformat(),
                                                    "recipe": recipe("trend", A, start=date(2022, 1, 3), sessions=200, seed=1)}},
                              seeds={"engine": 0, "fixture": 1}, extra=extra)

    def backtest(self, spec):
        view = DatasetView(self.series, until=self.dev[1], label="dev", holdout=self.holdout)
        return run_backtest(build(spec), view, {A: fixture_instrument(A)}, BacktestConfig.from_spec(spec, F("100000")))


class PreregistrationTest(Base):
    def test_trials_need_a_preregistration(self):
        with self.assertRaises(RegistryError) as cm:
            self.reg.start_trial("NOPE", spec=self.spec, segment="DEVELOPMENT", manifest=self.manifest(self.spec), principal=SYSTEM)
        self.assertEqual(cm.exception.code, "NO_PREREGISTRATION")

    def test_preregistration_is_hashed_with_its_time_and_immutable(self):
        p = self.prereg()
        self.assertEqual(len(p["prereg_hash"]), 64)
        for key in ("hypothesis", "information_clock", "execution_rule", "cost_model", "benchmark", "search_space", "budget",
                    "rejection_criteria", "holdout_plan", "registered_at_ns"):
            self.assertIn(key, p)
        with self.assertRaises(sqlite3.DatabaseError):
            self.reg.conn.execute("UPDATE experiment SET budget=100 WHERE experiment_id='EXP-1'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.reg.conn.execute("DELETE FROM experiment WHERE experiment_id='EXP-1'")
        self.assertEqual(self.reg.experiment("EXP-1")["prereg_hash"], p["prereg_hash"])

    def test_search_space_values_must_lie_in_the_spec_domain(self):
        with self.assertRaises(RegistryError):
            self.reg.preregister("EXP-X", self.spec, search_space={"fast_span": ["500"]}, budget=2, rejection_criteria=["x"],
                                 benchmark="b", principal=SYSTEM, fixture=True)

    def test_strategy_principals_cannot_preregister(self):
        with self.assertRaises(AuthorityError):
            self.prereg(principal=STRATEGY)

    def test_agents_can_preregister(self):
        self.assertEqual(self.prereg(principal=AGENT)["registered_by"], "agent.research")


class TrialRegistryTest(Base):
    def test_every_run_is_a_trial_and_failures_count(self):
        self.prereg()
        s = self.spec
        with self.reg.run_trial("EXP-1", spec=s, segment="DEVELOPMENT", manifest=self.manifest(s), principal=SYSTEM) as t:
            res = self.backtest(s)
            t.succeed(metrics=res.summary(), returns=list(zip(res.return_dates(), res.returns)), results_hash=res.event_log_hash)
        s2 = s.with_parameters(fast_span="10")
        with self.assertRaises(ZeroDivisionError):
            with self.reg.run_trial("EXP-1", spec=s2, segment="DEVELOPMENT", manifest=self.manifest(s2), principal=SYSTEM):
                1 / 0
        trials = self.reg.trials("EXP-1")
        self.assertEqual([t["status"] for t in trials], ["SUCCEEDED", "FAILED"])
        self.assertIn("ZeroDivisionError", trials[1]["error"])
        self.assertEqual(self.reg.trial_count("EXP-1"), 2)            # M counts the failure
        fam = self.reg.family_matrix("EXP-1")
        self.assertEqual((len(fam["columns"]), fam["trial_count"]), (1, 2))

    def test_interrupted_and_crashed_runs_are_aborted_trials(self):
        self.prereg()
        with self.assertRaises(KeyboardInterrupt):
            with self.reg.run_trial("EXP-1", spec=self.spec, segment="DEVELOPMENT", manifest=self.manifest(self.spec),
                                    principal=SYSTEM):
                raise KeyboardInterrupt
        tid = self.reg.start_trial("EXP-1", spec=self.spec.with_parameters(fast_span="10"), segment="DEVELOPMENT",
                                   manifest=self.manifest(self.spec.with_parameters(fast_span="10")), principal=SYSTEM)
        self.assertEqual(self.reg.reap_running(0), [tid])   # the process "died" with the trial RUNNING
        self.assertEqual([t["status"] for t in self.reg.trials("EXP-1")], ["ABORTED", "ABORTED"])
        self.assertEqual(self.reg.trial_count("EXP-1"), 2)

    def test_trials_are_append_only_and_finish_once(self):
        self.prereg()
        tid = self.reg.start_trial("EXP-1", spec=self.spec, segment="DEVELOPMENT", manifest=self.manifest(self.spec), principal=SYSTEM)
        self.reg.finish_trial(tid, status="SUCCEEDED", metrics={"x": 1})
        with self.assertRaises(RegistryError):
            self.reg.finish_trial(tid, status="FAILED")
        for sql in ("DELETE FROM trial", "UPDATE trial SET params='{}'", "UPDATE trial SET status='RUNNING'"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.reg.conn.execute(sql)
        self.assertEqual(self.reg.trial(tid)["status"], "SUCCEEDED")

    def test_parameters_outside_the_search_space_need_a_visible_amendment(self):
        self.prereg()
        s = self.spec.with_parameters(fast_span="15")
        with self.assertRaises(RegistryError) as cm:
            self.reg.start_trial("EXP-1", spec=s, segment="DEVELOPMENT", manifest=self.manifest(s), principal=SYSTEM)
        self.assertEqual(cm.exception.code, "OUTSIDE_SEARCH_SPACE")
        self.reg.amend("EXP-1", principal=AGENT, reason="neighbouring value", extend_search_space={"fast_span": ["15"]})
        self.reg.start_trial("EXP-1", spec=s, segment="DEVELOPMENT", manifest=self.manifest(s), principal=SYSTEM)
        self.assertEqual(self.reg.search_space("EXP-1")["fast_span"], ["5", "10", "15"])
        self.assertEqual(len(self.reg.amendments("EXP-1")), 1)

    def test_only_parameters_may_differ_from_the_preregistered_spec(self):
        self.prereg()
        body = dict(self.spec.body)
        body["costs"] = {**body["costs"], "spread_bps": "1"}
        from unknown.research.spec import parse_spec
        s = parse_spec(body)
        with self.assertRaises(RegistryError) as cm:
            self.reg.start_trial("EXP-1", spec=s, segment="DEVELOPMENT", manifest=self.manifest(s), principal=SYSTEM)
        self.assertEqual(cm.exception.code, "SPEC_CHANGED")

    def test_budget_is_enforced_and_only_the_owner_can_raise_it(self):
        self.prereg(budget=1)
        self.reg.start_trial("EXP-1", spec=self.spec, segment="DEVELOPMENT", manifest=self.manifest(self.spec), principal=SYSTEM)
        s2 = self.spec.with_parameters(fast_span="10")
        with self.assertRaises(RegistryError) as cm:
            self.reg.start_trial("EXP-1", spec=s2, segment="DEVELOPMENT", manifest=self.manifest(s2), principal=SYSTEM)
        self.assertEqual(cm.exception.code, "BUDGET_EXHAUSTED")
        with self.assertRaises(AuthorityError):
            self.reg.amend("EXP-1", principal=AGENT, reason="more please", budget=5)
        with self.assertRaises(AuthorityError):
            self.reg.amend("EXP-1", principal=SYSTEM, reason="more please", budget=5)
        self.reg.amend("EXP-1", principal=OWNER, reason="owner raised the budget", budget=2)
        self.reg.start_trial("EXP-1", spec=s2, segment="DEVELOPMENT", manifest=self.manifest(s2), principal=SYSTEM)
        self.assertEqual(self.reg.budget_consumption("EXP-1"), 1.0)

    def test_baselines_must_be_declared(self):
        self.prereg()
        bh = buy_and_hold_spec([A], self.dev, self.holdout)
        self.reg.start_trial("EXP-1", spec=bh, segment="BASELINE", manifest=self.manifest(bh), principal=SYSTEM)
        with self.assertRaises(RegistryError):
            self.reg.start_trial("EXP-1", spec=bh, segment="DEVELOPMENT", manifest=self.manifest(bh), principal=SYSTEM)

    def test_manifest_must_name_the_spec(self):
        self.prereg()
        with self.assertRaises(RegistryError):
            self.reg.start_trial("EXP-1", spec=self.spec.with_parameters(fast_span="10"), segment="DEVELOPMENT",
                                 manifest=self.manifest(self.spec), principal=SYSTEM)


class HoldoutTest(Base):
    def test_holdout_is_consumed_once_and_every_attempt_is_logged(self):
        self.prereg()
        m = self.manifest(self.spec, until=self.holdout[1])
        with self.reg.run_holdout("EXP-1", spec=self.spec, artifact_hash="a" * 64, manifest=m, principal=SYSTEM) as t:
            view = DatasetView(self.series, until=self.holdout[1], label="holdout", holdout=self.holdout, grant=t.grant)
            res = run_backtest(build(self.spec), view, {A: fixture_instrument(A)}, BacktestConfig.from_spec(self.spec, F("100000")))
            t.succeed(metrics={"n": len(res.returns)})
        with self.assertRaises(HoldoutError) as cm:
            self.reg.consume_holdout("EXP-1", spec=self.spec, artifact_hash="b" * 64, manifest=m, principal=OWNER)
        self.assertEqual(cm.exception.code, "HOLDOUT_CONSUMED")
        log = self.reg.holdout_log("EXP-1")
        self.assertEqual([a["outcome"] for a in log], ["GRANTED", "REFUSED"])
        self.assertEqual(self.reg.trial_count("EXP-1", "HOLDOUT"), 1)
        with self.assertRaises(sqlite3.DatabaseError):
            self.reg.conn.execute("DELETE FROM holdout_access")

    def test_agents_never_read_the_holdout(self):
        self.prereg()
        m = self.manifest(self.spec, until=self.holdout[1])
        with self.assertRaises(HoldoutError) as cm:
            self.reg.consume_holdout("EXP-1", spec=self.spec, artifact_hash="a" * 64, manifest=m, principal=AGENT)
        self.assertEqual(cm.exception.code, "HOLDOUT_ROLE")
        self.assertEqual(self.reg.holdout_log("EXP-1")[0]["outcome"], "REFUSED")
        # The refusal did not consume the holdout.
        self.reg.consume_holdout("EXP-1", spec=self.spec, artifact_hash="a" * 64, manifest=m, principal=SYSTEM)

    def test_holdout_trials_only_through_a_grant(self):
        self.prereg()
        with self.assertRaises(HoldoutError):
            self.reg.start_trial("EXP-1", spec=self.spec, segment="HOLDOUT", manifest=self.manifest(self.spec), principal=SYSTEM)


class HoldoutLockReviewTest(Base):
    """Regression tests for the 2026-10-22 adversarial review of the holdout lock (BR-DAT-07, CAP-RES-07/-10)."""

    def test_a_hand_made_grant_does_not_open_the_holdout(self):
        forged = HoldoutGrant("holdout:1", "EXP-1", "a" * 64, self.holdout[0], self.holdout[1])
        with self.assertRaises(HoldoutError):
            DatasetView(self.series, until=self.holdout[1], label="forged", holdout=self.holdout, grant=forged)
        self.prereg()
        m = self.manifest(self.spec, until=self.holdout[1])
        grant, _ = self.reg.consume_holdout("EXP-1", spec=self.spec, artifact_hash="a" * 64, manifest=m, principal=SYSTEM)
        copied = HoldoutGrant(grant.access_id, grant.experiment_id, grant.artifact_hash, grant.holdout_start, grant.holdout_end,
                              "0" * 32)   # every visible field copied, wrong nonce
        with self.assertRaises(HoldoutError):
            DatasetView(self.series, until=self.holdout[1], label="copied", holdout=self.holdout, grant=copied)
        DatasetView(self.series, until=self.holdout[1], label="real", holdout=self.holdout, grant=grant)

    def test_a_non_holdout_trial_whose_manifest_reaches_the_holdout_is_refused(self):
        self.prereg()
        with self.assertRaises(HoldoutError) as cm:
            self.reg.start_trial("EXP-1", spec=self.spec, segment="DEVELOPMENT",
                                 manifest=self.manifest(self.spec, until=self.holdout[0]), principal=SYSTEM)
        self.assertEqual(cm.exception.code, "HOLDOUT_REQUIRES_GRANT")
        self.assertEqual(self.reg.trials("EXP-1"), [])

    def test_a_manifest_must_state_what_the_run_may_read(self):
        self.prereg()
        m = self.manifest(self.spec)
        for datasets in ({}, {"FIXTURE": {"fingerprint": "x"}}):
            bad = build_manifest(run_kind="DEVELOPMENT", strategy_id=self.spec.strategy_id, spec_hash=self.spec.spec_hash,
                                 parameters={}, config=m["config"], datasets=datasets, seeds={}, include_environment=False)
            with self.assertRaises(RegistryError) as cm:
                self.reg.start_trial("EXP-1", spec=self.spec, segment="DEVELOPMENT", manifest=bad, principal=SYSTEM)
            self.assertEqual(cm.exception.code, "MANIFEST_INCOMPLETE")

    def test_holdout_returns_from_a_development_trial_are_refused_and_the_trial_still_counts(self):
        """The manifest claims the development window, but the run read a view without the holdout guard."""
        self.prereg()
        with self.assertRaises(HoldoutError) as cm:
            with self.reg.run_trial("EXP-1", spec=self.spec, segment="DEVELOPMENT", manifest=self.manifest(self.spec),
                                    principal=SYSTEM) as t:
                peek = DatasetView(self.series, until=self.holdout[1], label="unguarded")      # holdout=None: no guard
                res = run_backtest(build(self.spec), peek, {A: fixture_instrument(A)},
                                   BacktestConfig.from_spec(self.spec, F("100000")))
                t.succeed(metrics={}, returns=list(zip(res.return_dates(), res.returns)))
        self.assertEqual(cm.exception.code, "HOLDOUT_LEAK")
        (trial,) = self.reg.trials("EXP-1")
        self.assertEqual(trial["status"], "FAILED")
        self.assertIn("HOLDOUT_LEAK", trial["error"])
        self.assertIsNone(self.reg.trial(trial["trial_id"])["returns"])     # nothing from the holdout was stored
        self.assertEqual(self.reg.trial_count("EXP-1"), 1)

    def test_declared_baselines_may_use_the_granted_holdout_and_nothing_else_may(self):
        self.prereg()
        bh = buy_and_hold_spec([A], self.dev, self.holdout)
        with self.assertRaises(HoldoutError):     # no grant yet
            self.reg.start_trial("EXP-1", spec=bh, segment="BASELINE", manifest=self.manifest(bh, until=self.holdout[1]),
                                 principal=SYSTEM)
        grant, _ = self.reg.consume_holdout("EXP-1", spec=self.spec, artifact_hash="a" * 64,
                                            manifest=self.manifest(self.spec, until=self.holdout[1]), principal=SYSTEM)
        under = self.manifest(bh, until=self.holdout[1], extra={"holdout_access_id": grant.access_id})
        tid = self.reg.start_trial("EXP-1", spec=bh, segment="BASELINE", manifest=under, principal=SYSTEM)
        self.reg.finish_trial(tid, status="SUCCEEDED", metrics={}, returns=[(self.holdout[0], 0.001)])
        dev_under_grant = self.manifest(self.spec, until=self.holdout[1], extra={"holdout_access_id": grant.access_id})
        with self.assertRaises(HoldoutError):     # the grant does not open the holdout to development trials
            self.reg.start_trial("EXP-1", spec=self.spec, segment="DEVELOPMENT", manifest=dev_under_grant, principal=SYSTEM)

    def test_follow_up_segments_cannot_evaluate_uncounted_parameters(self):
        """Review fix: a grid run as SENSITIVITY trials used to leave M = 0 and the budget untouched."""
        self.prereg()
        s10 = self.spec.with_parameters(fast_span="10")
        for segment in ("SENSITIVITY", "STRESS", "REPLAY", "WALK_FORWARD"):
            with self.assertRaises(RegistryError) as cm:
                self.reg.start_trial("EXP-1", spec=s10, segment=segment, manifest=self.manifest(s10), principal=AGENT)
            self.assertEqual(cm.exception.code, "UNCOUNTED_PARAMETERS")
        self.reg.start_trial("EXP-1", spec=s10, segment="DEVELOPMENT", manifest=self.manifest(s10), principal=AGENT)
        self.reg.start_trial("EXP-1", spec=s10, segment="SENSITIVITY", manifest=self.manifest(s10), principal=AGENT)
        self.assertEqual(self.reg.trial_count("EXP-1"), 1)

    def test_every_holdout_attempt_is_logged_even_when_the_spec_is_invalid(self):
        self.prereg()
        outside = self.spec.with_parameters(fast_span="15")       # not in the pre-registered search space
        with self.assertRaises(RegistryError):
            self.reg.consume_holdout("EXP-1", spec=outside, artifact_hash="a" * 64,
                                     manifest=self.manifest(outside, until=self.holdout[1]), principal=SYSTEM)
        log = self.reg.holdout_log("EXP-1")
        self.assertEqual([a["outcome"] for a in log], ["REFUSED"])
        self.assertIn("OUTSIDE_SEARCH_SPACE", log[0]["reason"])


class ManifestTest(Base):
    def test_manifest_fields_and_hash(self):
        m = self.manifest(self.spec)
        for key in ("git", "code", "datasets", "config_hash", "seeds", "spec_hash", "parameters", "simulator_version", "python"):
            self.assertIn(key, m)
        self.assertIn("commit", m["git"])
        self.assertEqual(len(m["code"]["sha256"]), 64)
        self.assertTrue(verify_manifest(m))
        m2 = dict(m)
        m2["seeds"] = {"engine": 1, "fixture": 1}
        self.assertFalse(verify_manifest(m2))

    def test_replay_is_exact_and_differences_are_named(self):
        r1, r2 = self.backtest(self.spec), self.backtest(self.spec)
        m = self.manifest(self.spec)
        out = compare_replay(m, r1.event_log_hash, self.manifest(self.spec), r2.event_log_hash)
        self.assertEqual((out["replay_exact"], out["manifest_differences"]), (1, []))
        other = self.manifest(self.spec.with_parameters(fast_span="10"))
        self.assertIn("parameters", diff_manifests(m, other))
        changed = self.manifest(self.spec, until=self.dev[1].replace(day=1))
        self.assertIn("datasets.FIXTURE", diff_manifests(m, changed))

    def test_replay_diff_names_a_changed_interpreter_or_commit(self):
        """CAP-RES-11: changing the (only) dependency's version is named by the replay diff (review fix)."""
        m = self.manifest(self.spec)
        other = {**m, "python": {**m["python"], "version": "3.0.0"}, "git": {**m["git"], "commit": "0" * 40}}
        self.assertEqual(diff_manifests(m, other), ["python.version", "git.commit"])
        self.assertEqual(diff_manifests(m, self.manifest(self.spec)), [])

    def test_default_path_honours_unknown_home(self):
        with mock.patch.dict(os.environ, {"UNKNOWN_HOME": "/tmp/unknown-home-test"}):
            self.assertEqual(default_path(), Path("/tmp/unknown-home-test/research/experiments.sqlite"))


class ReportTest(Base):
    def test_reports_are_append_only(self):
        h = self.reg.add_report("R1", {"label": "FIXTURE", "x": 1.5}, kind="TEST", experiment_id=None, fixture=True)
        self.assertEqual(self.reg.report("R1")["body_hash"], h)
        with self.assertRaises(sqlite3.DatabaseError):
            self.reg.conn.execute("UPDATE report SET body='{}'")


if __name__ == "__main__":
    unittest.main()
