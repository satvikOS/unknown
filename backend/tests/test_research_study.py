"""End-to-end fixture study (I5 deliverable 6) and the read-only research API routes (deliverable 8).

The study runs TREND_DAILY_V1 and its baselines on SYNTHETIC fixtures through
pre-registration, the trial registry, walk-forward selection, the paired
bootstrap, DSR/PBO/n_eff, cost and stress sensitivity, replay, the single
holdout evaluation, the core replay and the promotion gates. Assertions are
about the machinery and the labelling, never about profitability."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from unknown.api.routes import research as routes
from unknown.domain.principal import Principal, Role
from unknown.research.common import HoldoutError
from unknown.research.registry import ResearchRegistry
from support.research_study import run_fixture_study


_STATE: dict = {}


def setUpModule():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    reg_path = root / "research" / "experiments.sqlite"
    reg = ResearchRegistry(reg_path)
    # T1 is recorded unflagged (as the terminal e2e test registry is); TF keeps the registry's fixture flag, which
    # product routes never serve (ADR-0020).
    report = run_fixture_study(reg, study_id="T1", sessions=420, core_replay_sessions=100, out_dir=root / "reports",
                               kinds=("trend", "random_walk"), fixture_flag=False)
    run_fixture_study(reg, study_id="TF", sessions=420, core_replay_sessions=0, kinds=("trend",))
    _STATE.update(tmp=tmp, root=root, reg_path=reg_path, reg=reg, report=report)


def tearDownModule():
    _STATE["reg"].close()
    _STATE["tmp"].cleanup()


class _Shared(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.reg_path, cls.reg, cls.report = (_STATE[k] for k in ("root", "reg_path", "reg", "report"))


class StudyTest(_Shared):

    def test_report_is_labelled_fixture_everywhere(self):
        r = self.report
        self.assertEqual(r["label"], "FIXTURE")
        self.assertIn("SYNTHETIC FIXTURE", r["warning"])
        for res in r["results"]:
            self.assertEqual(res["label"], "FIXTURE")
            self.assertTrue(all(rc["label"] == "FIXTURE" for rc in res["recipes"]))
            self.assertIn("not evidence of profitability", res["conclusion"])
        stored = json.loads((self.root / "reports" / "T1-report.json").read_text())
        self.assertEqual(stored["report_id"], "T1-report")
        self.assertEqual(self.reg.report("T1-report")["body_hash"], r["report_hash"])

    def test_trial_counts_come_from_the_registry(self):
        for res in self.report["results"]:
            exp = res["experiment_id"]
            self.assertEqual(res["trial_counts"]["DEVELOPMENT"], 6)
            self.assertEqual(res["dsr"]["M"], self.reg.trial_count(exp))
            self.assertEqual(res["dsr"]["M_registry"], 6)
            self.assertEqual(len(self.reg.trials(exp)), sum(res["trial_counts"].values()))
            self.assertTrue(all(t["status"] == "SUCCEEDED" for t in self.reg.trials(exp)))

    def test_holdout_was_consumed_exactly_once_and_cannot_be_reused(self):
        res = self.report["results"][0]
        self.assertEqual(res["holdout"]["consumptions"], 1)
        exp = res["experiment_id"]
        spec = self.reg.spec(res["selected"]["spec_hash"])
        manifest = self.reg.manifest(self.reg.trial(res["holdout"]["trial_id"])["manifest_hash"])
        with self.assertRaises(HoldoutError):
            self.reg.consume_holdout(exp, spec=spec, artifact_hash=res["selected"]["artifact_hash"], manifest=manifest,
                                     principal=Principal("research.evaluator", Role.SYSTEM))

    def test_replay_core_replay_and_gate_verdicts(self):
        for res in self.report["results"]:
            self.assertEqual(res["replay"]["replay_exact"], 1)
            self.assertTrue(res["core_replay"]["comparable"])
            self.assertEqual(res["core_replay"]["mismatches"], [])
            self.assertEqual(res["core_replay"]["ledger_problems"], [])
            kinds = {e["kind"] for e in res["evidence"]}
            self.assertEqual(kinds, {"REPLAY_EXACT", "WALK_FORWARD_OOS", "PAIRED_BOOTSTRAP", "HOLDOUT", "COST_SENSITIVITY",
                                     "STRESS", "DSR", "PBO", "N_EFF", "CORE_REPLAY_MATCH"})
            self.assertEqual(res["transitions"][0]["to_status"], "reproducible")
            self.assertEqual(res["status"], "validated" if not res["failing_gates"] else "reproducible")
            self.assertEqual(set(res["cost_sensitivity"]) >= {"0.5", "1", "2", "3"}, True)
            self.assertEqual(res["pbo"]["combinations"], 12870)
            self.assertNotEqual(res["status"], "eligible")   # no owner approval in a study

    def test_paired_bootstrap_used_identical_dates(self):
        for res in self.report["results"]:
            b = res["paired_bootstrap_oos"]
            self.assertEqual(b["n"], res["walk_forward"]["oos"]["n"])
            self.assertIn("conservative_lower", b)

    def test_walk_forward_uses_the_preregistered_purge(self):
        """Review fix (BR-DAT-14): the gap between train end and test start is the spec's purge_sessions, not 1."""
        for res in self.report["results"]:
            spec = self.reg.spec(res["selected"]["spec_hash"])
            purge = spec.walk_forward["purge_sessions"]
            self.assertGreaterEqual(purge, spec.walk_forward["label_horizon_sessions"] + spec.execution["execution_delay_sessions"])
            self.assertEqual(res["walk_forward"]["purge_horizon_observations"], purge)
            self.assertTrue(res["walk_forward"]["folds"])
            for f in res["walk_forward"]["folds"]:
                self.assertGreaterEqual(f["test_span"][0] - f["train_span"][1] - 1, purge)

    def test_n_eff_gate_is_computed_on_the_paired_oos_differences(self):
        for res in self.report["results"]:
            self.assertEqual(res["n_eff"]["n"], res["paired_bootstrap_oos"]["n"])
            self.assertIn("paired differences", res["n_eff"]["series"])
            self.assertEqual(res["n_eff_development"]["n"], res["selected"]["development"]["n"])
            self.assertEqual(res["dsr"]["M_source"], "registry trial_count (all statuses)")

    def test_protocol_statistic_and_selection_metric_are_honoured(self):
        from unknown.research import stats
        from unknown.research.spec import parse_spec
        from unknown.research.strategies import trend_spec
        from support.research_study import FIXTURE_PROTOCOL, protocol_statistic, selection_score
        r = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.0, 0.004]
        util = {**FIXTURE_PROTOCOL, "statistic": "utility.ch20@1", "utility_c_drawdown": "0.5", "utility_c_es": "0.1"}
        fn, sid = protocol_statistic(util)
        self.assertEqual(sid, "utility.ch20@1")
        self.assertAlmostEqual(fn(r), stats.utility(r, 0.5, 0.1), places=15)
        self.assertEqual(protocol_statistic(FIXTURE_PROTOCOL)[1], "mean")
        for res in self.report["results"]:          # the report's bootstraps used the protocol's statistic
            self.assertEqual(res["paired_bootstrap_oos"]["statistic"], FIXTURE_PROTOCOL["statistic"])
            self.assertEqual(res["holdout"]["paired_bootstrap"]["statistic"], FIXTURE_PROTOCOL["statistic"])
        spec = self.reg.spec(self.report["results"][0]["selected"]["spec_hash"])
        self.assertAlmostEqual(selection_score(spec, util, r), stats.sharpe(r), places=15)
        body = {**spec.body, "validation_plan": {**spec.body["validation_plan"], "selection_metric": "utility.ch20@1"}}
        self.assertAlmostEqual(selection_score(parse_spec(body), util, r), stats.utility(r, 0.5, 0.1), places=15)

    def test_study_is_deterministic(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            reg = ResearchRegistry(Path(tmp.name) / "r.sqlite")
            again = run_fixture_study(reg, study_id="T2", sessions=420, core_replay_sessions=0, kinds=("trend",))
            reg.close()
        finally:
            tmp.cleanup()
        a = [f["event_log_hash"] for f in self.report["results"][0]["family"]]
        b = [f["event_log_hash"] for f in again["results"][0]["family"]]
        self.assertEqual(a, b)


class ResearchRoutesTest(_Shared):
    """Exercise the route module directly (the orchestrator registers it in api/routes/__init__.py)."""

    def ctx(self, path=None):
        return SimpleNamespace(paths=SimpleNamespace(root=self.root), research_registry_path=path)

    def get(self, *parts, **q):
        return routes.handle(self.ctx(), ["research", *parts], q, "SIM-1", None)

    def test_missing_registry_is_an_explicit_unavailable_reason(self):
        out = routes.handle(self.ctx(self.root / "nope.sqlite"), ["research", "experiments"], {}, "SIM-1", None)
        self.assertFalse(out["available"])
        self.assertIn("not found", out["unavailable_reason"])
        self.assertEqual(out["reason"], "NO_DATA")
        self.assertIsNone(out["data"])

    def test_every_endpoint_returns_data_with_provenance(self):
        idx = self.get()
        self.assertTrue(idx["available"])
        self.assertGreater(idx["data"]["counts"]["trial"], 0)
        strategies = self.get("strategies")["data"]
        detail = self.get("strategies", strategies[0]["spec_hash"])
        self.assertTrue(detail["data"]["hash_verified"])
        exps = self.get("experiments")
        self.assertEqual({e["experiment_id"] for e in exps["data"]}, {"T1-TREND", "T1-RWALK"})
        self.assertTrue(all(e["holdout_consumed"] and "fixture" not in e for e in exps["data"]))
        exp = self.get("experiments", "T1-TREND")
        self.assertTrue(exp["data"]["prereg_hash_verified"])
        self.assertNotIn("fixture", exp["provenance"])
        trials = self.get("experiments", "T1-TREND", "trials")["data"]
        self.assertTrue(all("returns" not in t for t in trials))
        one = self.get("trials", trials[0]["trial_id"])["data"]
        self.assertEqual(one["manifest"]["manifest_hash"], one["manifest_hash"])
        self.assertTrue(one["returns"])
        dev = self.get("experiments", "T1-TREND", "trials", segment="DEVELOPMENT")["data"]
        self.assertEqual(len(dev), 6)
        reports = self.get("reports")["data"]
        self.assertEqual([r["report_id"] for r in reports], ["T1-report"])
        rep = self.get("reports", reports[0]["report_id"])
        self.assertNotIn("fixture", rep["data"])
        arts = self.get("artifacts")["data"]
        art = self.get("artifacts", arts[0]["artifact_hash"])["data"]
        self.assertEqual(art["chain_problems"], [])
        self.assertEqual(art["status"], art["history"][-1]["to_status"])
        formulas = self.get("formulas")["data"]
        self.assertIn("eq15.1@1", {f["formula_id"] for f in formulas})
        for out in (idx, exps, exp, rep):
            for key in ("source", "path", "read_at_ns"):
                self.assertIn(key, out["provenance"])

    def test_fixture_flagged_rows_are_never_served_and_segments_validated(self):
        """ADR-0020: rows the registry flags as FIXTURE (test data) are omitted from lists and 404 when read."""
        self.assertNotIn("TF-TREND", {e["experiment_id"] for e in self.get("experiments")["data"]})
        flagged_trial = self.reg.trials("TF-TREND", None)[0]["trial_id"]
        for parts in (("experiments", "TF-TREND"), ("experiments", "TF-TREND", "trials"), ("trials", flagged_trial),
                      ("reports", "TF-report")):
            with self.assertRaisesRegex(LookupError, "never served", msg=str(parts)):
                self.get(*parts)
        counts = self.get()["data"]["counts"]
        self.assertEqual((counts["experiment"], counts["report"]), (2, 1))
        self.assertEqual(counts["trial"], sum(len(self.reg.trials(e, None)) for e in ("T1-TREND", "T1-RWALK")))
        with self.assertRaises(ValueError):
            self.get("experiments", "T1-TREND", "trials", segment="BOGUS")

    def test_unknown_ids_are_404(self):
        for parts in (("trials", "T1-TREND:T99999"), ("experiments", "NOPE"), ("strategies", "0" * 64), ("reports", "x"),
                      ("artifacts", "f" * 64), ("bogus",)):
            with self.assertRaises(LookupError, msg=str(parts)):
                self.get(*parts)

    def test_routes_cannot_write(self):
        reg = ResearchRegistry.open_readonly(self.reg_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reg.conn.execute("INSERT INTO meta VALUES ('x', 'y')")
        finally:
            reg.close()
        self.assertEqual(routes.ROUTES[0][0], ("research",))


if __name__ == "__main__":
    unittest.main()
