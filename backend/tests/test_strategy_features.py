"""BR-DAT-18: per-feature validity windows and enumerated missingness under the approved feature contract, and a
deterministic re-warm from the point-in-time store when a strategy runner restarts (a restarted runner computes the
same features and targets; a silently corrected history is detected and blocks new entries). Every bar is a TEST
FIXTURE."""
from __future__ import annotations

import copy
import math
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_strategy_runner import (ACCOUNT, CLOCK_UTC, DATASET, DEV, HOLD, RealCoreCase, runtime_spec,  # noqa: E402
                                  trend_bars)
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.clock import NS_PER_S, ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.research.bars import BarSeries, load_series, record_bars  # noqa: E402
from unknown.research.common import SpecError  # noqa: E402
from unknown.research.features import FEATURE_MISSINGNESS, FeatureRow, feature_rows  # noqa: E402
from unknown.research.fixtures import manual_bar  # noqa: E402
from unknown.research.spec import parse_spec  # noqa: E402
from unknown.research.strategies import (build, buy_and_hold_spec, trend_score, trend_spec,  # noqa: E402
                                         vol_capped_spec)
from unknown.service.paths import Paths  # noqa: E402
from unknown.strategy.journal import decision_ids  # noqa: E402
from unknown.strategy.runner import (RunnerConfig, StrategyRunner, _decide_with, decision_instant,  # noqa: E402
                                     latest_eligible_session, strategy_decision, submission_window)

DAY = date(2026, 10, 21)
INST = "FIX-EQ-1"


def decide(bars: list, spec=None, day: date = DAY):
    spec = spec or runtime_spec((INST,))
    t = decision_instant(day, CLOCK_UTC)
    decision, visible, strategy = _decide_with(spec, {INST: BarSeries(INST, bars)}, day, t)
    rows = feature_rows(spec, strategy, INST, visible[INST], decision_session=day, session_close_ns=None,
                        calendar=WEEKDAYS_ONLY)
    return {r.feature_id: r for r in rows}, decision, t


class FeatureRowsTest(unittest.TestCase):
    def test_values_versions_warm_up_and_the_validity_window(self):
        bars = trend_bars(INST, DAY, 60, 800, 1000)
        rows, decision, t = decide(bars)
        spread, vol = rows["ema_spread_log_price"], rows["ewma_vol"]
        sc = trend_score([float(b.close.to_str()) for b in bars], fast_span=10, slow_span=40, vol_half_life=20, horizon_h=10,
                         epsilon=0.0001)
        self.assertEqual((spread.value, vol.value), (repr(sc["ema_f"] - sc["ema_s"]), repr(sc["sigma"])))
        self.assertEqual((spread.version, vol.version), (1, 1))
        self.assertEqual((spread.warm_up_state, spread.observations, spread.min_observations), ("WARM", 60, 40))
        self.assertEqual((vol.warm_up_state, vol.min_observations), ("WARM", 2))
        self.assertEqual(spread.valid_from_ns, bars[-1].available_ns)
        self.assertEqual(spread.valid_until_ns, bars[-1].available_ns + 4 * 86400 * NS_PER_S)
        self.assertTrue(spread.usable_at(t))
        self.assertFalse(spread.usable_at(spread.valid_until_ns))
        self.assertFalse(spread.usable_at(bars[-1].available_ns - 1))
        self.assertIsNone(spread.missing_reason)

    def test_warm_up_is_recorded_explicitly(self):
        rows, decision, _ = decide(trend_bars(INST, DAY, 20, 800, 1000))       # 20 < slow_span 40
        self.assertEqual(rows["ema_spread_log_price"].warm_up_state, "WARMING")
        self.assertEqual(rows["ewma_vol"].warm_up_state, "WARM")
        self.assertEqual(decision.targets[INST], 0.0)                          # the strategy holds while warming
        rows, _, _ = decide([manual_bar(INST, DAY, "10.00", "10.10", "9.90", "10.00")])
        self.assertEqual((rows["ewma_vol"].warm_up_state, rows["ewma_vol"].value, rows["ewma_vol"].missing_reason),
                         ("WARMING", None, None))                               # undefined before a first return

    def test_missingness_is_enumerated(self):
        self.assertEqual(FEATURE_MISSINGNESS, ("NOT_REPORTED", "PROVIDER_DELAY", "SOURCE_GAP", "NOT_APPLICABLE",
                                               "QUARANTINED"))
        bars = trend_bars(INST, DAY, 60, 800, 1000)
        rows, _, _ = decide(bars[:-1])                                          # D's bar has not arrived
        self.assertEqual({r.missing_reason for r in rows.values()}, {"PROVIDER_DELAY"})
        rows, _, _ = decide(bars[:-10] + bars[-9:])            # a hole 10 sessions back: inside the spread's 40-session
        self.assertEqual((rows["ema_spread_log_price"].missing_reason, rows["ewma_vol"].missing_reason),
                         ("SOURCE_GAP", None))                  # warm-up span, outside the volatility's 2 sessions
        rows, _, _ = decide(bars[:10] + bars[11:])                              # 49 sessions back: outside both spans
        self.assertEqual((rows["ema_spread_log_price"].missing_reason, rows["ewma_vol"].missing_reason), (None, None))
        spec = runtime_spec((INST,))
        empty = feature_rows(spec, build(spec), INST, [], decision_session=DAY, session_close_ns=None, calendar=WEEKDAYS_ONLY)
        self.assertEqual({(r.missing_reason, r.warm_up_state) for r in empty}, {("NOT_REPORTED", "COLD")})

        class Broken:
            FEATURES = {"ema_spread_log_price": 1, "ewma_vol": 1}

            def feature_values(self, inst):
                return {"ema_spread_log_price": math.nan}

            def feature_warm_up(self, fid):
                return 2
        rows = {r.feature_id: r for r in feature_rows(spec, Broken(), INST, bars, decision_session=DAY,
                                                     session_close_ns=None, calendar=WEEKDAYS_ONLY)}
        self.assertEqual((rows["ema_spread_log_price"].missing_reason, rows["ewma_vol"].missing_reason),
                         ("QUARANTINED", "NOT_APPLICABLE"))
        with self.assertRaises(ValueError):
            FeatureRow("x", 1, INST, DAY, 0, 1, "WARM", 1, 1, None, "STALE")    # not an enumerated reason
        with self.assertRaises(ValueError):
            FeatureRow("x", 1, INST, DAY, 0, 1, "WARM", 1, 1, "1.0", "SOURCE_GAP")

    def test_the_gap_check_follows_the_venue_calendar_and_stops_at_its_horizon(self):
        from unknown.domain.calendar import load_calendar
        xnys = load_calendar(Path(__file__).resolve().parents[2] / "config" / "calendars" / "XNYS.json")
        days, d = [], date(2025, 11, 3)                         # XNYS data starts 2026-01-01
        while d <= date(2026, 2, 27):
            if d.weekday() < 5 and (d.year < 2026 or xnys.is_business_day(d)):
                days.append(d)
            d += timedelta(days=1)
        bars = [manual_bar(INST, x, "10.00", "10.10", "9.90", f"{10 + k / 1000:.2f}") for k, x in enumerate(days)]
        spec = runtime_spec((INST,)).with_parameters(slow_span="80")    # an 80-session span reaches back into 2025
        t = decision_instant(days[-1], CLOCK_UTC)

        def rows(bs):
            _, visible, strategy = _decide_with(spec, {INST: BarSeries(INST, bs)}, days[-1], t)
            return {r.feature_id: r for r in feature_rows(spec, strategy, INST, visible[INST], decision_session=days[-1],
                                                          session_close_ns=None, calendar=xnys)}
        self.assertIsNone(rows(bars)["ema_spread_log_price"].missing_reason)   # holidays are not gaps
        hole = [b for b in bars if b.session_date != date(2026, 2, 10)]
        self.assertEqual(rows(hole)["ema_spread_log_price"].missing_reason, "SOURCE_GAP")

    def test_the_freshness_budget_bounds_the_validity_window(self):
        bars = trend_bars(INST, DAY, 60, 800, 1000)
        body = copy.deepcopy(runtime_spec((INST,)).body)
        body["features"][0]["freshness_budget_s"] = 3600                        # one hour, inside the 4-day window
        rows, _, t = decide(bars, parse_spec(body))
        spread, vol = rows["ema_spread_log_price"], rows["ewma_vol"]
        self.assertEqual(spread.valid_until_ns, bars[-1].available_ns + 3600 * NS_PER_S)
        self.assertEqual(vol.valid_until_ns, bars[-1].available_ns + 4 * 86400 * NS_PER_S)
        self.assertTrue(spread.usable_at(t))                                    # 15 minutes after availability
        self.assertFalse(spread.usable_at(bars[-1].available_ns + 3600 * NS_PER_S))

    def test_the_implementation_must_compute_the_approved_contract(self):
        body = copy.deepcopy(trend_spec([INST], DEV, HOLD).body)
        body["features"][1]["version"] = 2
        with self.assertRaises(SpecError) as cm:
            build(parse_spec(body))
        self.assertIn("feature contract mismatch", str(cm.exception))
        body = copy.deepcopy(trend_spec([INST], DEV, HOLD).body)
        body["features"][0]["warm_up_observations"] = 30                       # slow_span 40 needs 40
        with self.assertRaises(SpecError) as cm:
            build(parse_spec(body))
        self.assertIn("needs 40 observations", str(cm.exception))

    def test_every_builder_contract_covers_its_parameter_domain(self):
        """The contract's warm-up is an upper bound over the parameter domain: a spec at the domain maximum of the
        warm-up parameter still builds (the volatility baseline's contract said 20 against a half-life domain up to
        120, so a valid parameter set would have been refused)."""
        for spec, param in ((trend_spec([INST], DEV, HOLD), "slow_span"), (vol_capped_spec([INST], DEV, HOLD), "vol_half_life"),
                            (buy_and_hold_spec([INST], DEV, HOLD), None)):
            if param is not None:
                spec = spec.with_parameters(**{param: spec.body["parameter_domain"][param]["max"]})
            strategy = build(spec)
            for f in spec.features:
                need = strategy.feature_warm_up(f["id"]) if hasattr(strategy, "feature_warm_up") else None
                self.assertLessEqual(need or 0, f["warm_up_observations"], (spec.strategy_id, f["id"]))


class RunnerFeatureGateTest(unittest.TestCase):
    AID = "TREND_DAILY_V1@0123456789abcdef"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = Paths(self.root)
        self.paths.ensure()

    def test_a_feature_with_a_missing_input_blocks_the_decision(self):
        from unknown.domain.money import Money
        from unknown.policy.model import FeeModel
        from unknown.research.fixtures import fixture_instrument
        from unknown.strategy import artifacts
        from unknown.strategy.coreview import CoreSnapshot
        (self.root / "research").mkdir()
        t = decision_instant(DAY, CLOCK_UTC)
        store = ResearchStore(self.root / "research" / "research.sqlite")
        bars = trend_bars("FIX-EQ-1", DAY, 60, 800, 1000)
        record_bars(store, bars[:-10] + bars[-9:], DATASET, t)                   # FIX-EQ-1 has a hole in its history
        record_bars(store, trend_bars("FIX-EQ-2", DAY, 60, 800, 1000), DATASET, t)
        store.close()
        insts = ("FIX-EQ-1", "FIX-EQ-2")
        art = artifacts.LoadedArtifact(self.AID, "0" * 64, "paper", runtime_spec(), {}, date(2099, 1, 1), DATASET, True)
        fees = FeeModel(Fixed.parse("0.005"), Money("USD", Fixed.parse("1.00")), 100, 2000, "TEST FIXTURE", "test")
        snap = CoreSnapshot(ACCOUNT, "SIMULATED", "S1", "RUNNING", "p" * 64, "USD", 6, {i: fixture_instrument(i) for i in insts},
                            {}, Fixed.parse("10000"), Fixed.parse("10000"), fees)
        with StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", self.paths), clock=ManualClock(t)) as r:
            rows = r._decide(art, DAY, t, t + 86_400 * NS_PER_S, t, snap, list(insts), {})
        d = {x["instrument_id"]: x for x in rows}
        self.assertEqual(d["FIX-EQ-1"]["decision_payload"]["outcome"], "BLOCKED")
        self.assertEqual(d["FIX-EQ-1"]["decision_payload"]["reason"], "FEATURE_INVALID: ema_spread_log_price v1 SOURCE_GAP")
        self.assertIsNone(d["FIX-EQ-2"]["decision_payload"]["outcome"])         # an intent for the other name
        spread = {f["feature_id"]: f for f in d["FIX-EQ-1"]["detail"]["features"]}["ema_spread_log_price"]
        self.assertEqual((spread["missing_reason"], spread["value"]), ("SOURCE_GAP", None))   # journaled with the decision

    def test_a_feature_outside_its_validity_window_at_the_decision_blocks_it(self):
        """Review fix (an untested gate): the validity window is checked at the decision as well as at transmission. A
        one-minute freshness budget on the spread makes a 15-minute-old close unusable at the 21:30 decision."""
        from unknown.domain.money import Money
        from unknown.policy.model import FeeModel
        from unknown.research.fixtures import fixture_instrument
        from unknown.strategy import artifacts
        from unknown.strategy.coreview import CoreSnapshot
        (self.root / "research").mkdir()
        t = decision_instant(DAY, CLOCK_UTC)
        store = ResearchStore(self.root / "research" / "research.sqlite")
        insts = ("FIX-EQ-1", "FIX-EQ-2")
        for i in insts:
            record_bars(store, trend_bars(i, DAY, 60, 800, 1000), DATASET, t)
        store.close()
        body = copy.deepcopy(runtime_spec().body)
        body["features"][0]["freshness_budget_s"] = 60
        art = artifacts.LoadedArtifact(self.AID, "0" * 64, "paper", parse_spec(body), {}, date(2099, 1, 1), DATASET, True)
        fees = FeeModel(Fixed.parse("0.005"), Money("USD", Fixed.parse("1.00")), 100, 2000, "TEST FIXTURE", "test")
        snap = CoreSnapshot(ACCOUNT, "SIMULATED", "S1", "RUNNING", "p" * 64, "USD", 6, {i: fixture_instrument(i) for i in insts},
                            {}, Fixed.parse("10000"), Fixed.parse("10000"), fees)
        with StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", self.paths), clock=ManualClock(t)) as r:
            rows = r._decide(art, DAY, t, t + 86_400 * NS_PER_S, t, snap, list(insts), {})
        for x in rows:
            self.assertEqual(x["decision_payload"]["outcome"], "BLOCKED", x["instrument_id"])
            self.assertTrue(x["decision_payload"]["reason"].startswith(
                "FEATURE_OUTSIDE_VALIDITY_WINDOW: ema_spread_log_price valid ["), x["decision_payload"]["reason"])
            self.assertIsNone(x["submit_payload"])
        # the same data under the spec's own budget decides an intent
        art = artifacts.LoadedArtifact(self.AID, "0" * 64, "paper", runtime_spec(), {}, date(2099, 1, 1), DATASET, True)
        with StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", self.paths), clock=ManualClock(t)) as r:
            rows = r._decide(art, DAY, t, t + 86_400 * NS_PER_S, t, snap, list(insts), {})
        self.assertTrue(all(x["submit_payload"] is not None for x in rows), [x["decision_payload"] for x in rows])

    def test_an_intent_whose_features_expired_is_never_transmitted(self):
        _, wt = submission_window(runtime_spec(), DAY, WEEKDAYS_ONLY, fixture=True)

        class Client:
            sent: list = []

            def command(self, command_id, kind, payload, principal):
                Client.sent.append(kind)
                assert kind == "record_strategy_decision", kind
                return {"accepted": True, "duplicate": False, "body": {"recorded": True}, "events": []}

        class Core:
            def commands(self, ids):
                return {}
        clock = ManualClock(wt.start_ns + 60 * NS_PER_S)
        r = StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", self.paths), clock=clock, client=Client())
        self.addCleanup(r.close)
        r.core = Core()
        ids = decision_ids(self.AID, DAY, INST)
        submit = {"account_key": ACCOUNT, "session_id": "S1", "order_id": ids["order_id"], "instrument_id": INST,
                  "side": "BUY", "quantity": "4", "limit_price": "10.05", "deadline_ns": wt.end_ns}
        feature = {"feature_id": "ewma_vol", "version": 1, "value": "0.01", "valid_until_ns": wt.start_ns + 30 * NS_PER_S}
        r.journal.plan([{"artifact_id": self.AID, "session_date": DAY, "instrument_id": INST, "mode": "live", **ids,
                         "submit_payload": submit, "decision_payload": {"outcome": None, "reason": "z=1", "order_ids": []},
                         "detail": {"window": wt.to_json(), "model_bound": "10.05", "collar_bps": 50,
                                    "features": [feature]}}], clock.now_ns())
        with self.assertLogs("unknown.strategy", level="WARNING"):
            row = r._execute(r.journal.get(DAY, INST))
        self.assertEqual((row["state"], Client.sent), ("RECORDED", ["record_strategy_decision"]))
        self.assertEqual(row["decision_payload"]["outcome"], "BLOCKED")
        self.assertTrue(row["decision_payload"]["reason"].startswith("FEATURE_EXPIRED: not submitted"),
                        row["decision_payload"]["reason"])


# ===================================================================== end to end
class RewarmCase(RealCoreCase):
    LISTED = ("A",)

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def first_process(self) -> date:
        """A runner decides the previous session (shadow mode, before today's decision instant), then exits."""
        early = ManualClock(decision_instant(self.day, CLOCK_UTC) - 3600 * NS_PER_S)
        prev = latest_eligible_session(early.now_ns(), WEEKDAYS_ONLY, CLOCK_UTC)
        with StrategyRunner(RunnerConfig(self.ids["A"], ACCOUNT, "shadow", self.paths), clock=early) as r:
            s = r.run_once()
            self.assertEqual((s["session_date"], s["rewarm"]["status"]), (prev.isoformat(), "NO_SNAPSHOT"))
            snap = {x["instrument_id"]: x["detail"] for x in r.journal.rows(prev)}
        self.assertEqual(set(snap), {"FIX-EQ-1", "FIX-EQ-2"})
        for detail in snap.values():
            self.assertEqual([f["feature_id"] for f in detail["features"]], ["ema_spread_log_price", "ewma_vol"])
            self.assertIsNotNone(detail["strategy_target"])
        return prev


class RewarmMatchTest(RewarmCase):
    def test_a_restarted_runner_rewarms_from_the_store_and_computes_the_same_targets(self):
        prev = self.first_process()
        with self.runner("A", "live") as r:                     # a new process, the next session
            s = r.run_once()
            today = {x["instrument_id"]: x["detail"] for x in r.journal.rows(self.day)}
        rw = s["rewarm"]
        self.assertEqual((rw["status"], rw["session_date"], rw["compared"], rw["targets_compared"]),
                         ("MATCH", prev.isoformat(), ["FIX-EQ-1", "FIX-EQ-2"], True))
        self.assertEqual({x["outcome"] for x in s["instruments"].values()}, {"INTENT_SUBMITTED"}, s)
        # what an uninterrupted process computes for today: the research code path on the store's history
        store = ResearchStore.open_readonly(self.root / "research" / "research.sqlite")
        try:
            t = decision_instant(self.day, CLOCK_UTC)
            series = load_series(store, DATASET, ("FIX-EQ-1", "FIX-EQ-2"), t)
        finally:
            store.close()
        decision, _ = strategy_decision(self.rf.spec, series, self.day, t)
        for inst in ("FIX-EQ-1", "FIX-EQ-2"):
            self.assertEqual(today[inst]["strategy_target"], repr(decision.targets[inst]))
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class RewarmMismatchTest(RewarmCase):
    def test_a_silently_corrected_history_is_detected_and_blocks_new_entries(self):
        prev = self.first_process()
        # the provider silently corrects FIX-EQ-1's bar of the previous session (same publication, new close)
        bars = trend_bars("FIX-EQ-1", self.day, 60, 800, 1000)
        old = bars[-2]
        self.assertEqual(old.session_date, prev)
        fixed = manual_bar("FIX-EQ-1", prev, old.open.to_str(), old.high.to_str(), old.low.to_str(), old.low.to_str(),
                           int(old.volume.to_str()))
        store = ResearchStore(self.root / "research" / "research.sqlite")
        record_bars(store, [fixed], DATASET, time.time_ns())
        store.close()
        with self.runner("A", "live") as r, self.assertLogs("unknown.strategy", level="ERROR") as logs:
            s = r.run_once()
        rw = s["rewarm"]
        self.assertEqual((rw["status"], rw["session_date"]), ("MISMATCH", prev.isoformat()))
        self.assertEqual({m["instrument_id"] for m in rw["mismatches"]}, {"FIX-EQ-1"})
        self.assertTrue(any("new entries are blocked" in line for line in logs.output))
        for inst in ("FIX-EQ-1", "FIX-EQ-2"):
            x = s["instruments"][inst]
            self.assertEqual((x["outcome"], x["submitted"]), ("BLOCKED", False), x)
            self.assertTrue(x["reason"].startswith("FEATURE_STATE_MISMATCH"), x["reason"])
        self.assertEqual(self.orders(self.ids["A"]), [])
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


del RealCoreCase, RewarmCase     # bases only

if __name__ == "__main__":
    unittest.main()
