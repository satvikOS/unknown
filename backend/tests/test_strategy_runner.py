"""Strategy runner (ADR-0017 #1, #2, #4, #5; book ch. 15 algorithm A15).

Unit tests: the artifact id is derived from the research registry's artifact hash (no second scheme); registry
status gates the mode; code drift and expiry refuse; eligible sessions follow the calendar; targets and A15 orders
are computed by the same code path as the backtest; sizing; deterministic ids.

End to end: a real ``CoreService`` (simulated broker + FIXTURE market, deterministic quotes) in a thread behind
its command socket, bootstrapped by the owner over the API token; a session whose mandate lists the artifact;
owner-issued strategy tokens; SYNTHETIC bars in a point-in-time store; a research registry holding the
artifacts. Shadow decisions are recorded without intents; live intents are accepted or rejected and every
decision is recorded; a restart, a crash between submission and record, and a lost journal never submit twice;
an artifact the mandate does not list is refused by the core. Every number here is a TEST FIXTURE."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import load_policy  # noqa: E402
from test_research_promotion import GATES, PASSING  # noqa: E402  (evidence payloads are TEST FIXTURES)
from unknown.core import ledger  # noqa: E402
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY, CalendarHorizonError, load_calendar  # noqa: E402
from unknown.domain.clock import NS_PER_S, ManualClock, SystemClock, utc_ns  # noqa: E402
from unknown.domain.fixed import Fixed, Rounding  # noqa: E402
from unknown.domain.ids import validate_id  # noqa: E402
from unknown.domain.instruments import AssetClass, Instrument, TickRule  # noqa: E402
from unknown.domain.principal import ARTIFACT_ID, Principal, Role  # noqa: E402
from unknown.market.quotes import QuoteBook  # noqa: E402
from unknown.research.bars import DatasetView, record_bars  # noqa: E402
from unknown.research.engine import BacktestConfig, Sizing, run_backtest  # noqa: E402
from unknown.research.fixtures import fixture_instrument, manual_bar, synthetic_bars  # noqa: E402
from unknown.research.manifest import build_manifest  # noqa: E402
from unknown.research.promotion import STATUSES, PromotionService  # noqa: E402
from unknown.research.registry import ResearchRegistry  # noqa: E402
from unknown.research.spec import parse_spec  # noqa: E402
from unknown.research.strategies import TrendDailyV1, trend_spec  # noqa: E402
from unknown.research.study import FIXTURE_PROTOCOL  # noqa: E402
from unknown.service.core import CoreService, Intervals, _simulated_broker  # noqa: E402
from unknown.service.fixture_market import FixtureInstrument, FixtureMarket  # noqa: E402
from unknown.service.ipc import CoreClient  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.store.db import Tx, connect  # noqa: E402
from unknown.strategy import artifacts  # noqa: E402
from unknown.strategy.artifacts import ArtifactRefused  # noqa: E402
from unknown.strategy.journal import decision_ids  # noqa: E402
from unknown.strategy.runner import (RunnerConfig, RunnerError, StrategyRunner, decision_instant,  # noqa: E402
                                     execution_window, latest_eligible_session, strategy_decision)
from unknown.strategy.sizing import Holding, plan  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"
REPO = Path(__file__).resolve().parents[2]
ACCOUNT = "SIM-1"
DATASET = "FIXTURE.synthetic_daily_bars.v1"
DEV, HOLD = (date(2021, 1, 4), date(2022, 12, 30)), (date(2023, 1, 2), date(2023, 12, 29))
AUTHOR = Principal("research.author", Role.SYSTEM)
EVAL = Principal("research.evaluator", Role.SYSTEM)
CLOCK_UTC = dtime(21, 30)
INSTRUMENTS = ("FIX-EQ-1", "FIX-EQ-2")


def runtime_spec(instruments=INSTRUMENTS):
    """TREND_DAILY_V1 as specified, with a minimum order notional that fits the 100.00 fixture session."""
    body = json.loads(json.dumps(trend_spec(list(instruments), DEV, HOLD).body))
    body["rules"]["sizing"]["min_order_notional"] = "10"
    return parse_spec(body)


class ResearchFixture:
    """A research registry holding artifacts moved up the promotion ladder with TEST FIXTURE evidence
    (the same steps as tests/test_research_promotion.py)."""

    def __init__(self, path: Path, spec):
        self.reg = ResearchRegistry(path)
        self.promo = PromotionService(self.reg)
        self.spec = spec
        self.protocol = self.promo.approve_protocol(FIXTURE_PROTOCOL, self.owner())
        self.started = self.reg.now() + 1
        time.sleep(0.002)
        self.reg.preregister("EXP-P", spec, search_space={"fast_span": ["5", "10", "20"]}, budget=5, rejection_criteria=["x"],
                             benchmark="BUY_AND_HOLD", principal=AUTHOR, fixture=True)
        time.sleep(0.002)
        self.manifest = self.make_manifest(spec, DEV[1])
        self.trial = self.reg.start_trial("EXP-P", spec=spec, segment="DEVELOPMENT", manifest=self.manifest, principal=AUTHOR)
        self.reg.finish_trial(self.trial, status="SUCCEEDED", metrics={})
        self.n = 0

    def close(self) -> None:
        self.reg.close()

    def owner(self) -> Principal:
        return Principal("owner", Role.OWNER, authenticated_at_ns=self.reg.now())

    @staticmethod
    def make_manifest(spec, until) -> dict:
        cfg = BacktestConfig.from_spec(spec, Fixed(100000))
        return build_manifest(run_kind="DEVELOPMENT", strategy_id=spec.strategy_id, spec_hash=spec.spec_hash,
                              parameters={k: v.to_str() for k, v in spec.parameters.items()}, config=cfg.to_json(),
                              datasets={"FIXTURE": {"fingerprint": "f" * 64, "until": until.isoformat()}}, seeds={},
                              include_environment=False)

    def register(self, fast_span: str = "10", *, code_hash: str | None = None, expires_on: date = date(2099, 1, 1)):
        spec = self.spec.with_parameters(fast_span=fast_span)
        h = self.promo.register_artifact(spec, AUTHOR, code_hash=code_hash or artifacts.current_code_hash(),
                                         training_interval=DEV, evaluation_manifest_hash=self.manifest["manifest_hash"],
                                         allowed_markets=["FIXTURE"], horizon="daily", capacity_estimate="n/a",
                                         expires_on=expires_on)
        return h, artifacts.artifact_id(spec.strategy_id, h)

    def ev(self, art: str, kind: str) -> str:
        refs, payload = [{"type": "trial", "id": self.trial}], PASSING[kind]
        if kind == "HOLDOUT":
            payload, refs = self.holdout_evidence(art)
        r = self.promo.add_evidence(art, kind, payload, refs=refs, principal=EVAL, evaluation_started_at_ns=self.started,
                                    protocol_hash=self.protocol)
        assert r["passed"], r
        return r["evidence_id"]

    def holdout_evidence(self, art: str):
        self.n += 1
        exp = f"EXP-H{self.n}"
        spec = self.reg.spec(self.promo.artifact(art)["spec_hash"])
        self.reg.preregister(exp, self.spec, search_space={"fast_span": ["5", "10", "20"]}, budget=5, rejection_criteria=["x"],
                             benchmark="BUY_AND_HOLD", principal=AUTHOR, fixture=True)
        time.sleep(0.002)
        grant, tid = self.reg.consume_holdout(exp, spec=spec, artifact_hash=art, manifest=self.make_manifest(spec, HOLD[1]),
                                              principal=EVAL)
        self.reg.finish_trial(tid, status="SUCCEEDED", metrics={})
        return ({**PASSING["HOLDOUT"], "holdout_access_id": grant.access_id},
                [{"type": "trial", "id": tid}, {"type": "holdout_access", "id": grant.access_id}])

    def to_validated(self, art: str) -> None:
        self.promo.transition(art, "reproducible", EVAL, evidence_ids=[self.ev(art, "REPLAY_EXACT")], reason="replayed")
        self.promo.transition(art, "validated", EVAL, evidence_ids=[self.ev(art, k) for k in GATES], reason="gates passed")

    def to_paper(self, art: str) -> None:
        self.to_validated(art)
        self.promo.transition(art, "paper", self.owner(), evidence_ids=[self.ev(art, "CORE_REPLAY_MATCH")], reason="owner: paper")

    def quarantine(self, art: str) -> None:
        self.promo.transition(art, "quarantined", EVAL, evidence_ids=[self.ev(art, "MONITORING_INCIDENT")], reason="incident")


def cents(c: int) -> str:
    return f"{c // 100}.{c % 100:02d}"


def trend_bars(instrument: str, last_day: date, sessions: int, start_cents: int, end_cents: int) -> list:
    """SYNTHETIC rising daily bars ending with the completed bar of ``last_day`` (fixture session times)."""
    days, d = [], last_day
    while len(days) < sessions:
        if WEEKDAYS_ONLY.is_business_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    out, prev = [], start_cents
    for k, day in enumerate(days):
        c = start_cents + (end_cents - start_cents) * k // (sessions - 1)
        out.append(manual_bar(instrument, day, cents(prev), cents(max(prev, c) + 1), cents(min(prev, c) - 1), cents(c), 1_000_000))
        prev = c
    return out


def stable_now() -> int:
    """Now, at least three minutes before the next decision instant, so the eligible session and the intent
    deadline cannot move while a test runs (waits out the boundary in the rare case it is that close)."""
    while True:
        now = time.time_ns()
        d = latest_eligible_session(now, WEEKDAYS_ONLY, CLOCK_UTC)
        gap = decision_instant(WEEKDAYS_ONLY.add_business_days(d, 1), CLOCK_UTC) - now
        if gap > 180 * NS_PER_S:
            return now
        time.sleep(gap / NS_PER_S + 1)


def wait_for(predicate, timeout_s: float = 30.0, what: str = "condition"):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ======================================================================= unit tests
class ArtifactIdentityAndEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rf = ResearchFixture(Path(self.tmp.name) / "experiments.sqlite", runtime_spec())
        self.addCleanup(self.rf.close)
        self.today = datetime.now(timezone.utc).date()

    def test_id_is_derived_from_the_registry_hash_not_a_second_scheme(self):
        h, aid = self.rf.register()
        self.assertEqual(aid, f"TREND_DAILY_V1@{h[:16]}")
        self.assertRegex(aid, ARTIFACT_ID)
        self.assertEqual(artifacts.resolve(self.rf.reg, aid), h)
        self.rf.to_paper(h)
        loaded = artifacts.load(self.rf.reg, aid, mode="live", today=self.today)
        self.assertEqual(loaded.artifact_hash, h)
        # the registry's own bundle hash IS the identity: code, parameters, features, spec (datasets) in one hash
        bundle = self.rf.promo.artifact(h)["body"]["bundle"]
        self.assertEqual(sorted(bundle), ["code_hash", "dependency_lock", "features", "parameters", "schema", "spec_hash",
                                          "strategy_id"])
        self.assertEqual((loaded.dataset_id, loaded.fixture_allowed), (DATASET, True))
        # changing a parameter is a new artifact with a new id
        h2, aid2 = self.rf.register("5")
        self.assertNotEqual((h, aid), (h2, aid2))
        # the owner's way to learn the id to put in a mandate and a token
        import contextlib
        import io
        from unknown.strategy.__main__ import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["--home", self.tmp.name, "id", "--hash", h2, "--registry", str(Path(self.tmp.name) / "experiments.sqlite")])
        self.assertEqual((rc, out.getvalue().strip()), (0, aid2))
        with self.assertRaises(ArtifactRefused) as cm:
            artifacts.load(self.rf.reg, "TREND_DAILY_V1@" + "0" * 16, mode="shadow", today=self.today)
        self.assertEqual(cm.exception.code, "ARTIFACT_UNKNOWN")
        with self.assertRaises(ArtifactRefused) as cm:
            artifacts.load(self.rf.reg, "trend@xyz", mode="shadow", today=self.today)
        self.assertEqual(cm.exception.code, "INVALID_ARTIFACT_ID")

    def test_status_gates_each_mode(self):
        expect = {"proposed": (False, False), "reproducible": (False, False), "validated": (False, False),
                  "shadow": (True, False), "paper": (True, True), "eligible": (True, True), "active": (True, True),
                  "quarantined": (False, False), "retired": (False, False)}
        self.assertTrue(set(STATUSES) <= set(expect))
        for status, (shadow, live) in expect.items():
            self.assertEqual((artifacts.status_permits(status, "shadow"), artifacts.status_permits(status, "live")),
                             (shadow, live), status)
        self.assertNotIn("shadow", STATUSES)   # ADR-0017 gap: the registry cannot record 'shadow' yet

        h, aid = self.rf.register()
        for mode in ("shadow", "live"):
            with self.assertRaises(ArtifactRefused) as cm:
                artifacts.load(self.rf.reg, aid, mode=mode, today=self.today)
            self.assertEqual(cm.exception.code, "STATUS_NOT_PERMITTED")
        self.rf.to_validated(h)
        with self.assertRaises(ArtifactRefused) as cm:     # validated ranks below shadow
            artifacts.load(self.rf.reg, aid, mode="shadow", today=self.today)
        self.assertEqual(cm.exception.code, "STATUS_NOT_PERMITTED")
        self.rf.promo.transition(h, "paper", self.rf.owner(), evidence_ids=[self.rf.ev(h, "CORE_REPLAY_MATCH")], reason="paper")
        for mode in ("shadow", "live"):
            self.assertEqual(artifacts.load(self.rf.reg, aid, mode=mode, today=self.today).status, "paper")
        self.rf.quarantine(h)
        with self.assertRaises(ArtifactRefused) as cm:
            artifacts.load(self.rf.reg, aid, mode="shadow", today=self.today)
        self.assertEqual(cm.exception.code, "STATUS_NOT_PERMITTED")
        self.assertIn("quarantined", cm.exception.message)

    def test_code_drift_and_expiry_refuse(self):
        h, aid = self.rf.register("5", code_hash="c" * 64)
        self.rf.to_paper(h)
        with self.assertRaises(ArtifactRefused) as cm:
            artifacts.load(self.rf.reg, aid, mode="live", today=self.today)
        self.assertEqual(cm.exception.code, "CODE_DRIFT")
        self.assertEqual(artifacts.load(self.rf.reg, aid, mode="live", today=self.today, code_hash="c" * 64).status, "paper")
        h2, aid2 = self.rf.register("20", expires_on=self.today - timedelta(days=1))
        self.rf.to_paper(h2)
        with self.assertRaises(ArtifactRefused) as cm:
            artifacts.load(self.rf.reg, aid2, mode="shadow", today=self.today)
        self.assertEqual(cm.exception.code, "ARTIFACT_EXPIRED")


class CalendarTest(unittest.TestCase):
    def test_latest_eligible_session_and_execution_window(self):
        cal = WEEKDAYS_ONLY
        self.assertEqual(latest_eligible_session(utc_ns(2026, 10, 24, 12), cal, CLOCK_UTC), date(2026, 10, 23))  # Saturday
        self.assertEqual(latest_eligible_session(utc_ns(2026, 10, 22, 15), cal, CLOCK_UTC), date(2026, 10, 21))  # before 21:30
        self.assertEqual(latest_eligible_session(utc_ns(2026, 10, 22, 21, 30), cal, CLOCK_UTC), date(2026, 10, 22))
        exec_date, deadline = execution_window(date(2026, 10, 23), cal, CLOCK_UTC, 1)
        self.assertEqual((exec_date, deadline), (date(2026, 10, 26), utc_ns(2026, 10, 26, 21, 30)))  # no hours: next decision

    def test_exchange_calendar_holidays_early_closes_and_horizon(self):
        xnys = load_calendar(REPO / "config" / "calendars" / "XNYS.json")
        # Thanksgiving 2026-11-26 is a holiday: the latest eligible session that evening is the day before.
        self.assertEqual(latest_eligible_session(utc_ns(2026, 11, 26, 23), xnys, CLOCK_UTC), date(2026, 11, 25))
        exec_date, deadline = execution_window(date(2026, 11, 25), xnys, CLOCK_UTC, 1)
        self.assertEqual(exec_date, date(2026, 11, 27))
        self.assertEqual(deadline, utc_ns(2026, 11, 27, 18, 0))     # early close 13:00 New York, before 21:30 UTC
        with self.assertRaises(CalendarHorizonError):
            latest_eligible_session(utc_ns(2029, 1, 3, 23), xnys, CLOCK_UTC)


class SameCodePathAsBacktestTest(unittest.TestCase):
    """The runtime target and the A15 orders are the backtest's, computed by the same research code."""

    def setUp(self):
        self.insts = ["FIX-SR-1", "FIX-SR-2"]
        self.spec = trend_spec(self.insts, DEV, HOLD)
        bars = {i: synthetic_bars("trend", i, start=date(2024, 1, 1), sessions=180, seed=s, start_price=40.0)
                for i, s in zip(self.insts, (11, 12))}
        from unknown.research.bars import BarSeries
        self.series = {i: BarSeries(i, b) for i, b in bars.items()}
        self.cfg = BacktestConfig.from_spec(self.spec, Fixed.parse("100000"))
        view = DatasetView(dict(self.series), until=bars[self.insts[0]][-1].session_date, label="parity")
        self.result = run_backtest(TrendDailyV1(self.spec), view, {i: fixture_instrument(i) for i in self.insts}, self.cfg)

    def test_targets_match_the_backtest_decisions(self):
        decisions = [e for e in self.result.events if e["type"] == "DECISION"]
        stop = next((e["seq"] for e in self.result.events if e["type"] == "RISK_STOP"), None)
        compared = 0
        for e in decisions:
            if stop is not None and e["seq"] > stop:
                break
            day = date.fromisoformat(e["date"])
            decision, _ = strategy_decision(self.spec, self.series, day, decision_instant(day, CLOCK_UTC))
            self.assertEqual(decision.targets, e["targets"], day)
            compared += 1
        self.assertGreater(compared, 150)

    def test_a15_orders_match_the_backtest_from_a_flat_book(self):
        created = [e for e in self.result.events if e["type"] == "ORDER_CREATED"]
        self.assertTrue(created, "the fixture produced no orders")
        day = date.fromisoformat(created[0]["decision_date"])
        engine_orders = sorted((e["instrument_id"], e["side"], e["quantity"], e["limit_price"]) for e in created
                               if e["decision_date"] == day.isoformat())
        t = decision_instant(day, CLOCK_UTC)
        decision, visible = strategy_decision(self.spec, self.series, day, t)
        sizing = Sizing.from_spec(self.spec)
        adv = {i: (sum((b.volume for b in visible[i][-sizing.adv_window:]), Fixed(0))
                   .div(Fixed(len(visible[i][-sizing.adv_window:])), 0, Rounding.FLOOR))
               for i in self.insts}
        plans, _ = plan(decision.targets, sizing=sizing, limit_offset_bps=self.spec.execution["limit_offset_bps"],
                        equity=self.cfg.initial_cash, marks={i: visible[i][-1].close for i in self.insts}, holdings={},
                        instruments={i: fixture_instrument(i) for i in self.insts}, budget=self.cfg.initial_cash,
                        fee_envelope=self.cfg.cost_model.fee_envelope, adv=adv, ledger_scale=6)
        runtime_orders = sorted((p.order.instrument_id, p.order.side, p.order.quantity.to_str(), p.order.limit_price.to_str())
                                for p in plans.values() if p.order is not None)
        self.assertEqual(runtime_orders, engine_orders)


class SizingTest(unittest.TestCase):
    def setUp(self):
        self.inst = {"A": fixture_instrument("A")}
        self.sizing = Sizing(max_weight=Fixed.parse("0.45"), aggregate_cap=Fixed.parse("0.95"), cash_reserve=Fixed.parse("0.05"),
                             no_trade_band=Fixed.parse("0.05"), min_order_notional=Fixed.parse("10"),
                             min_adv_shares=Fixed(100_000))

    def run_plan(self, target, holding=Holding(), budget="100", adv=1_000_000):
        envelope = lambda side, q, limit: Fixed.parse("1.20") if side == "BUY" else Fixed(0)  # noqa: E731
        plans, _ = plan({"A": target}, sizing=self.sizing, limit_offset_bps=50, equity=Fixed.parse("100"),
                        marks={"A": Fixed.parse("10.00")}, holdings={"A": holding}, instruments=self.inst,
                        budget=Fixed.parse(budget), fee_envelope=envelope, adv={"A": Fixed(adv)}, ledger_scale=6)
        return plans["A"]

    def test_band_minimum_size_cash_and_exits(self):
        p = self.run_plan(0.9)
        self.assertEqual((p.order.side, p.order.quantity, p.order.limit_price), ("BUY", Fixed(4), Fixed.parse("10.05")))
        self.assertEqual(p.target_weight, Fixed.parse("0.45"))      # per-name cap
        p = self.run_plan(0.45, Holding(owned=Fixed(4), unreserved=Fixed(4)))
        self.assertIsNone(p.order)
        self.assertIn("NO_TRADE_BAND", p.note)                      # 0.40 held vs 0.45 target is inside the band
        p = self.run_plan(0.45, Holding(owned=Fixed(0), pending_buy=Fixed(4)))
        self.assertIsNone(p.order)                                  # pending buys count as current exposure
        p = self.run_plan(0.45, budget="21.30")
        self.assertEqual(p.order.quantity, Fixed(2))                # sized down to what the session can reserve
        self.assertIn("sized down", p.note)
        p = self.run_plan(0.45, budget="10.00")
        self.assertIsNone(p.order)
        self.assertIn("NO_TRADE", p.note)
        p = self.run_plan(0.45, adv=50_000)
        self.assertIsNone(p.order)
        self.assertIn("BELOW_MIN_ADV", p.note)
        p = self.run_plan(0.0, Holding(owned=Fixed(1), unreserved=Fixed(1)))
        self.assertEqual((p.order.side, p.order.quantity, p.order.reason), ("SELL", Fixed(1), "EXIT"))  # exit below minimum
        self.assertEqual(p.order.limit_price, Fixed.parse("9.95"))
        p = self.run_plan(0.0, Holding(owned=Fixed(1), unreserved=Fixed(0), pending_sell=Fixed(0)))
        self.assertIsNone(p.order)
        self.assertIn("NO_UNRESERVED_QUANTITY", p.note)

    def test_ids_are_deterministic_valid_and_distinct(self):
        a = decision_ids("TREND_DAILY_V1@0123456789abcdef", date(2026, 10, 21), "FIX-EQ-1")
        self.assertEqual(a, decision_ids("TREND_DAILY_V1@0123456789abcdef", date(2026, 10, 21), "FIX-EQ-1"))
        for v in a.values():
            validate_id(v)
        self.assertFalse(a["submit_command_id"].startswith(("process.", "sys.", "conflict.")))
        others = [decision_ids("TREND_DAILY_V1@0123456789abcdef", date(2026, 10, 22), "FIX-EQ-1"),
                  decision_ids("TREND_DAILY_V1@0123456789abcdef", date(2026, 10, 21), "FIX-EQ-2"),
                  decision_ids("TREND_DAILY_V1@fedcba9876543210", date(2026, 10, 21), "FIX-EQ-1")]
        for o in others:
            self.assertTrue(set(o.values()).isdisjoint(a.values()))


class JournalPerAccountTest(unittest.TestCase):
    """Review fix: the journal and the runner lock were keyed by the artifact alone, so one home running the same
    artifact for a second account found the first account's final rows and reported every instrument "already
    decided": the second account's decisions were never made or recorded (and the lock refused the second runner)."""

    def test_two_accounts_have_separate_journals_and_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp))
            aid, day, clock = "TREND_DAILY_V1@0123456789abcdef", date(2026, 10, 21), ManualClock(utc_ns(2026, 10, 22, 12))
            first = StrategyRunner(RunnerConfig(aid, "SIM-1", "live", paths), clock=clock)
            self.addCleanup(lambda: first.lock.fd is not None and first.close())
            first.journal.plan([{"artifact_id": aid, "session_date": day, "instrument_id": "FIX-EQ-1", "mode": "live",
                                 "state": "RECORDED", **decision_ids(aid, day, "FIX-EQ-1"), "decision_payload": {}}], clock.now_ns())
            second = StrategyRunner(RunnerConfig(aid, "SIM-2", "live", paths), clock=clock)   # same artifact, other account
            self.addCleanup(lambda: second.lock.fd is not None and second.close())
            self.assertNotEqual(first.cfg.journal_path, second.cfg.journal_path)
            self.assertIsNone(second.journal.get(day, "FIX-EQ-1"))
            self.assertEqual(first.journal.get(day, "FIX-EQ-1")["state"], "RECORDED")
            with self.assertRaises(RunnerError) as cm:      # still one runner per (account, artifact)
                StrategyRunner(RunnerConfig(aid, "SIM-1", "shadow", paths), clock=clock)
            self.assertEqual(cm.exception.code, "RUNNER_ALREADY_RUNNING")
            for bad in ("../SIM-1", "a/b", "", ".hidden"):
                with self.assertRaises(ValueError):
                    RunnerConfig(aid, bad, "live", paths)


class ExpiredIntentTest(unittest.TestCase):
    """Review fix: a PLANNED intent whose deadline has passed (a core outage past the deadline, e.g. after an XNYS
    close) is never sent. If an earlier process did send it before crashing, the core's stored result is recorded,
    never a BLOCKED that would contradict an order the core holds; a command another principal stored under the id
    is not taken as ours."""
    AID = "TREND_DAILY_V1@0123456789abcdef"
    DAY = date(2026, 10, 21)

    class Client:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        def command(self, command_id, kind, payload, principal):
            self.sent.append((command_id, kind))
            if kind == "submit_intent":
                raise AssertionError("an intent past its deadline was sent")
            return {"accepted": True, "duplicate": False, "body": {"recorded": True}, "events": []}

    class Core:
        def __init__(self, stored: dict):
            self.stored = stored

        def commands(self, ids):
            return {i: self.stored[i] for i in ids if i in self.stored}

    def run_case(self, stored_by: str | None) -> tuple[dict, list]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ids = decision_ids(self.AID, self.DAY, "FIX-EQ-1")
        deadline = decision_instant(date(2026, 10, 22), CLOCK_UTC)
        clock, client = ManualClock(deadline + 1), self.Client()
        r = StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", Paths(Path(tmp.name))), clock=clock, client=client)
        self.addCleanup(r.close)
        r.core = self.Core({} if stored_by is None else {ids["submit_command_id"]: {
            "status": "ACCEPTED", "result": {"order_id": ids["order_id"], "state": "RESERVED"}, "principal_id": stored_by,
            "principal_role": "STRATEGY"}})
        submit = {"account_key": ACCOUNT, "session_id": "S1", "order_id": ids["order_id"], "instrument_id": "FIX-EQ-1",
                  "side": "BUY", "quantity": "4", "limit_price": "10.05", "deadline_ns": deadline}
        r.journal.plan([{"artifact_id": self.AID, "session_date": self.DAY, "instrument_id": "FIX-EQ-1", "mode": "live", **ids,
                         "submit_payload": submit, "decision_payload": {"outcome": None, "reason": "z=1", "order_ids": []}}],
                       clock.now_ns())
        with self.assertLogs("unknown.strategy", level="INFO"):
            row = r._execute(r.journal.get(self.DAY, "FIX-EQ-1"))
        self.assertEqual(client.sent, [(ids["decision_command_id"], "record_strategy_decision")])   # never the intent
        self.assertEqual(row["state"], "RECORDED")
        return row["decision_payload"], ids

    def test_not_sent_and_recorded_blocked(self):
        decision, _ = self.run_case(None)
        self.assertEqual((decision["outcome"], decision["order_ids"]), ("BLOCKED", []))
        self.assertTrue(decision["reason"].startswith("RUNNER_LATE: not submitted"), decision["reason"])
        self.assertIn("BUY 4 @ 10.05", decision["reason"])

    def test_an_intent_the_core_already_holds_is_recorded_as_submitted(self):
        decision, ids = self.run_case(self.AID)
        self.assertEqual((decision["outcome"], decision["order_ids"]), ("INTENT_SUBMITTED", [ids["order_id"]]))

    def test_a_command_another_principal_stored_under_the_id_is_not_ours(self):
        decision, _ = self.run_case("TREND_DAILY_V1@ffffffffffffffff")
        self.assertEqual(decision["outcome"], "BLOCKED")


class ExcludedInstrumentKeepsItsShareOfTheCapTest(unittest.TestCase):
    """Review fix: an instrument the runtime may not trade at a decision (here: no bar for the session) keeps its
    share of the aggregate cap, as in the engine. It used to be dropped from the targets before sizing, so the
    other names were sized as if it did not exist: with three names at w_max 0.45 (1.35, scaled to 0.95 in the
    backtest) and the stale one held, the session was bought to ~100% invested, through the 0.95 aggregate cap
    and the 5% cash reserve (BR-DAT-15). Every number is a TEST FIXTURE."""
    INSTS = ("FIX-EQ-1", "FIX-EQ-2", "FIX-EQ-3")
    DAY = date(2026, 10, 21)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.paths = Paths(root)
        self.paths.ensure()
        (root / "research").mkdir()
        self.t = decision_instant(self.DAY, CLOCK_UTC)
        store = ResearchStore(root / "research" / "research.sqlite")
        for i in self.INSTS:
            bars = trend_bars(i, self.DAY, 60, 800, 1000)        # strongly rising: every name targets w_max 0.45
            record_bars(store, bars[:-1] if i == "FIX-EQ-3" else bars, DATASET, self.t)
        store.close()
        self.spec = runtime_spec(self.INSTS)
        self.art = artifacts.LoadedArtifact("TREND_DAILY_V1@0123456789abcdef", "0" * 64, "paper", self.spec, {},
                                            date(2099, 1, 1), DATASET, True)

    def test_a_stale_held_instrument_still_counts_toward_the_aggregate_cap(self):
        from unknown.strategy.coreview import CoreSnapshot
        held, mark3 = Fixed(3100), Fixed.parse("9.96")          # FIX-EQ-3: held, last visible close 9.96 (D-1)
        cash = Fixed.parse("68000")
        snap = CoreSnapshot(ACCOUNT, "SIMULATED", "S1", "RUNNING", "p" * 64, "USD", 6,
                            {i: fixture_instrument(i) for i in self.INSTS}, {"FIX-EQ-3": Holding(owned=held, unreserved=held)},
                            cash, cash, None)
        r = StrategyRunner(RunnerConfig(self.art.artifact_id, ACCOUNT, "live", self.paths), clock=ManualClock(self.t))
        self.addCleanup(r.close)
        _, deadline = execution_window(self.DAY, WEEKDAYS_ONLY, CLOCK_UTC, 1)
        rows = {x["instrument_id"]: x for x in r._decide(self.art, self.DAY, self.t, deadline, self.t, snap, list(self.INSTS), {})}
        self.assertEqual(rows["FIX-EQ-3"]["decision_payload"]["outcome"], "SKIPPED_STALE_DATA")
        equity = cash + held * mark3
        bought = Fixed(0)
        for i in ("FIX-EQ-1", "FIX-EQ-2"):
            # the engine's weight: 0.45 x 0.95 / 1.35, floored to 6 dp (not 0.45, as if FIX-EQ-3 did not exist)
            self.assertEqual(rows[i]["detail"]["target_weight"], "0.316666", rows[i]["decision_payload"]["reason"])
            sub = rows[i]["submit_payload"]
            self.assertEqual(sub["side"], "BUY")
            self.assertEqual(Fixed.parse(sub["quantity"]), (Fixed.parse("0.316666") * equity).div(Fixed.parse("10.00"), 0, Rounding.FLOOR))
            bought = bought + Fixed.parse(sub["quantity"]) * Fixed.parse("10.00")
        invested = (bought + held * mark3).div(equity, 6, Rounding.HALF_EVEN)
        self.assertLessEqual(invested, Fixed.parse("0.95"), f"invested {invested} of equity {equity}")


# ===================================================================== end to end
class RealCoreCase(unittest.TestCase):
    """A real core service with its socket, a research registry and a point-in-time store under one home."""

    LISTED: tuple[str, ...] = ()        # registry handles (set up by subclasses) whose ids the mandate lists
    LAST_CLOSE = {"FIX-EQ-1": 1000, "FIX-EQ-2": 4200}     # cents; the FIXTURE market's mids are 10.00 and 42.00
    STALE: tuple[str, ...] = ()        # instruments whose bar for the eligible session is missing

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.now = stable_now()
        self.clock = ManualClock(self.now)        # the runner's clock; the core runs on the system clock
        self.day = latest_eligible_session(self.now, WEEKDAYS_ONLY, CLOCK_UTC)
        self.rf = ResearchFixture(self.root / "research" / "experiments.sqlite", runtime_spec())
        self.addCleanup(self.rf.close)
        self.ids: dict[str, str] = {}
        self.hashes: dict[str, str] = {}
        self.make_artifacts()
        for name in self.ids:
            self.paths.create_strategy_token(self.ids[name])
        store = ResearchStore(self.root / "research" / "research.sqlite")
        for inst in INSTRUMENTS:
            bars = trend_bars(inst, self.day, 60, self.LAST_CLOSE[inst] * 8 // 10, self.LAST_CLOSE[inst])
            record_bars(store, bars[:-1] if inst in self.STALE else bars, DATASET, time.time_ns())
        store.close()
        self.n = 0
        self.start_core()
        self.bootstrap([self.ids[n] for n in self.LISTED])

    def make_artifacts(self) -> None:
        raise NotImplementedError

    def add(self, name: str, fast_span: str, status: str) -> None:
        h, aid = self.rf.register(fast_span)
        {"paper": self.rf.to_paper, "validated": self.rf.to_validated, "proposed": lambda _: None}[status](h)
        self.ids[name], self.hashes[name] = aid, h

    # ---------------------------------------------------------------- core
    def start_core(self) -> None:
        clock = SystemClock()
        broker = _simulated_broker(clock, "300.00")
        market = FixtureMarket(clock, broker, QuoteBook(), [
            FixtureInstrument("FIX-EQ-1", Fixed.parse("10.00"), Fixed.parse("0.02"), Fixed.parse("0.01")),
            FixtureInstrument("FIX-EQ-2", Fixed.parse("42.00"), Fixed.parse("0.04"), Fixed.parse("0.01"))], step_bps=0)
        self.svc = CoreService(paths=self.paths, account_key=ACCOUNT, deployment_id="test", broker=broker, clock=clock,
                               intervals=Intervals(loop_s=0.05, valuation_s=0.5, reconcile_s=2.0, reconcile_retry_s=0.2,
                                                   heartbeat_s=1.0), fixture_market=market)
        self.thread = threading.Thread(target=self.svc.run, daemon=True)
        self.thread.start()
        self.assertTrue(self.svc.ready.wait(30), "core did not start")
        self.addCleanup(self.stop_core)
        self.owner_client = CoreClient(self.paths.socket, self.paths.token("api"))

    def stop_core(self) -> None:
        if self.thread.is_alive():
            self.svc.shutdown()
            self.thread.join(30)

    def owner(self) -> dict:
        return {"id": "owner", "role": "OWNER", "authenticated_at_ns": time.time_ns()}

    def cmd(self, kind: str, **payload) -> dict:
        self.n += 1
        r = self.owner_client.command(f"test.{kind}.{self.n}", kind, payload, self.owner())
        self.assertTrue(r["accepted"], (kind, r["body"]))
        return r

    def bootstrap(self, listed: list[str]) -> None:
        self.cmd("register_account", account_key=ACCOUNT, provider="simulated", mode="SIMULATED", base_currency="USD")
        pol = self.cmd("publish_policy", body=load_policy(session_template__strategy_eligibility=["owner-manual", *listed]))
        self.cmd("activate_policy", account_key=ACCOUNT, policy_hash=pol["body"]["policy_hash"])
        for iid, sym in (("FIX-EQ-1", "FIXA"), ("FIX-EQ-2", "FIXB")):
            inst = Instrument(instrument_id=iid, asset_class=AssetClass.EQUITY, symbol=sym, name=f"Fixture {sym} (test only)",
                              currency="USD", settlement_currency="USD", venue_mic="XNAS", quantity_increment=Fixed(1),
                              min_quantity=Fixed(1), multiplier=Fixed(1), settlement_days=1,
                              tick_rules=(TickRule(Fixed(0), Fixed.parse("0.0001")), TickRule(Fixed(1), Fixed.parse("0.01"))),
                              attributes={"fixture": True})
            self.cmd("register_instrument", instrument=inst.to_json(), mappings=[{"provider": "simulated", "external_id": iid}])
        self.cmd("acquire_execution_ownership", account_key=ACCOUNT, deployment_id="test")
        wait_for(lambda: "RECONCILIATION_REQUIRED" not in self.owner_client.query(
            "preflight", authenticated_at_ns=time.time_ns())["blockers"], what="the opening reconciliation")
        self.cmd("authorize_capital", account_key=ACCOUNT, amount="300.00")
        self.green_start()
        self.cmd("create_session", account_key=ACCOUNT, session_id="S1")
        wait_for(lambda: self.core_rows("SELECT row FROM sessions WHERE session_id='S1'")[0]["state"] == "RUNNING",
                 what="session S1 RUNNING")

    def green_start(self) -> None:
        wait_for(lambda: not self.owner_client.query("preflight", authenticated_at_ns=time.time_ns())["blockers"],
                 what="a green preflight")
        self.cmd("start", account_key=ACCOUNT)

    # ---------------------------------------------------------------- reads
    def core_rows(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = connect(self.paths.db(ACCOUNT), readonly=True)
        try:
            return [json.loads(r["row"]) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def orders(self, principal: str | None = None) -> list[dict]:
        rows = self.core_rows("SELECT row FROM orders ORDER BY accepted_at_ns")
        return [o for o in rows if principal is None or o["principal_id"] == principal]

    def decisions(self, principal: str | None = None) -> list[dict]:
        conn = connect(self.paths.db(ACCOUNT), readonly=True)
        try:
            rows = conn.execute("SELECT principal_id, command_id, payload FROM events WHERE type='StrategyDecisionRecorded' "
                                "ORDER BY seq").fetchall()
        finally:
            conn.close()
        out = [{**json.loads(r["payload"])["info"], "principal_id": r["principal_id"], "command_id": r["command_id"]}
               for r in rows]
        return [d for d in out if principal is None or d["principal_id"] == principal]

    def verify_ledger(self) -> list[str]:
        conn = connect(self.paths.db(ACCOUNT), readonly=True)
        try:
            return ledger.verify(Tx(conn))
        finally:
            conn.close()

    def lose_journal(self, name: str) -> None:
        """Delete the runner's journal (as a lost disk would); it must have existed."""
        path = RunnerConfig(self.ids[name], ACCOUNT, "live", self.paths).journal_path
        self.assertTrue(path.is_file(), path)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()

    def runner(self, name: str, mode: str, **kw) -> StrategyRunner:
        r = StrategyRunner(RunnerConfig(self.ids[name], ACCOUNT, mode, self.paths), clock=self.clock, **kw)
        self.addCleanup(lambda: r.lock.fd is not None and r.close())
        return r


class LiveRunnerTest(RealCoreCase):
    LISTED = ("A",)
    LAST_CLOSE = {"FIX-EQ-1": 1000, "FIX-EQ-2": 4400}     # FIX-EQ-2's limit (44.22) is outside the 1% price collar

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")      # listed in the session mandate
        self.add("B", "5", "paper")       # has a token but no mandate listing

    def test_unlisted_artifact_is_refused_by_the_core(self):
        b = self.ids["B"]
        with self.runner("B", "live") as r, self.assertLogs("unknown.strategy", level="ERROR") as logs:
            s = r.run_once()
        self.assertEqual(sum("STRATEGY_NOT_PERMITTED" in line for line in logs.output), 2)
        self.assertEqual(s["result"], "OK")
        self.assertEqual(self.orders(b), [])
        self.assertEqual(self.decisions(b), [])            # not even a record: the mandate does not list it
        refused = [x for x in s["instruments"].values() if x["core_refusal"] == "STRATEGY_NOT_PERMITTED"]
        self.assertEqual(len(refused), 2, s)
        submitted = [x for x in s["instruments"].values() if x["submitted"]]
        self.assertTrue(submitted, s)
        for x in submitted:
            self.assertIsNone(x["order_id"])
            self.assertEqual(x["state"], "REFUSED")
        with self.runner("B", "live") as again:
            row = again.journal.get(self.day, "FIX-EQ-2")
            checks = {c["check"]: c["passed"] for c in row["submit_result"]["body"]["details"]["decision"]["checks"]}
            self.assertFalse(checks["STRATEGY_PERMITTED"])
            s2 = again.run_once()
        self.assertTrue(all(x["already_decided"] for x in s2["instruments"].values()))
        self.assertEqual(self.orders(b), [])

    def test_live_intents_accepted_and_rejected_restart_and_lost_journal_never_resubmit(self):
        a = self.ids["A"]
        with self.runner("A", "live") as r:
            with self.assertRaises(RunnerError) as cm:          # one runner per artifact
                StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=self.clock)
            self.assertEqual(cm.exception.code, "RUNNER_ALREADY_RUNNING")
            s = r.run_once()
        self.assertEqual((s["result"], s["session_id"], s["session_date"]), ("OK", "S1", self.day.isoformat()))
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertEqual((one["outcome"], one["state"]), ("INTENT_SUBMITTED", "RECORDED"), one)
        self.assertEqual((two["outcome"], two["state"], two["submit_rejection"]), ("BLOCKED", "RECORDED", "PRICE_COLLAR"), two)
        self.assertIn("PRICE_COLLAR", two["reason"])

        orders = self.orders(a)
        self.assertEqual(len(orders), 1)
        o = orders[0]
        ids = decision_ids(a, self.day, "FIX-EQ-1")
        self.assertEqual((o["order_id"], o["instrument_id"], o["side"], o["quantity"], o["limit_price"]),
                         (ids["order_id"], "FIX-EQ-1", "BUY", "4", "10.05"))
        self.assertEqual((o["strategy_id"], o["principal_role"], o["purpose"], o["time_in_force"]), (a, "STRATEGY", "ENTRY", "DAY"))
        self.assertEqual(o["deadline_ns"], s["deadline_ns"])
        self.assertEqual(o["decision_ref"]["decision_command_id"], ids["decision_command_id"])
        recs = self.decisions(a)
        self.assertEqual(sorted((d["instrument_id"], d["outcome"], d["mode"]) for d in recs),
                         [("FIX-EQ-1", "INTENT_SUBMITTED", "live"), ("FIX-EQ-2", "BLOCKED", "live")])
        rec1 = next(d for d in recs if d["instrument_id"] == "FIX-EQ-1")
        self.assertEqual((rec1["order_ids"], rec1["target_quantity"], rec1["current_quantity"], rec1["artifact_id"]),
                         ([ids["order_id"]], "4", "0", a))
        self.assertEqual(rec1["observation_time_ns"], decision_instant(self.day, CLOCK_UTC))
        self.assertRegex(rec1["inputs_digest"], r"^[0-9a-f]{64}$")
        self.assertIsNotNone(rec1["score"])
        wait_for(lambda: self.orders(a)[0]["state"] == "FILLED", what="the FIXTURE market to fill the intent")

        # restart: a new process decides nothing again
        with self.runner("A", "live") as r2:
            s2 = r2.run_once()
        self.assertTrue(all(x["already_decided"] for x in s2["instruments"].values()), s2)
        self.assertEqual((len(self.orders(a)), len(self.decisions(a))), (1, 2))

        # lost journal: the core's stored commands are adopted instead of deciding again
        self.lose_journal("A")
        with self.runner("A", "live") as r3:
            s3 = r3.run_once()
            adopted = r3.journal.rows(self.day)
        self.assertTrue(all(x["recovered"] and x["already_decided"] for x in s3["instruments"].values()), s3)
        self.assertEqual(sorted(r["decision_payload"]["outcome"] for r in adopted), ["BLOCKED", "INTENT_SUBMITTED"])
        self.assertEqual((len(self.orders(a)), len(self.decisions(a))), (1, 2))

        # the scheduled loop is the same idempotent cycle
        with self.runner("A", "live") as r4:
            stop = threading.Event()
            t = threading.Thread(target=r4.run_loop, args=(stop,), kwargs={"grace_s": 0, "poll_s": 0.05})
            t.start()
            wait_for(lambda: r4.cycles >= 2, what="two loop cycles")
            stop.set()
            t.join(10)
            self.assertFalse(t.is_alive())
            self.assertTrue(all(x["already_decided"] for x in r4.last_summary["instruments"].values()), r4.last_summary)
        self.assertEqual((len(self.orders(a)), len(self.decisions(a))), (1, 2))
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class Crash(BaseException):
    pass


class CrashingClient:
    """The real client; the process 'dies' right after the core accepted the first intent, before the journal
    learned of it."""

    def __init__(self, inner: CoreClient):
        self.inner = inner
        self.crashed = False

    def command(self, command_id, kind, payload, principal):
        result = self.inner.command(command_id, kind, payload, principal)
        if kind == "submit_intent" and not self.crashed:
            self.crashed = True
            raise Crash()
        return result


class CrashResumeTest(RealCoreCase):
    LISTED = ("A",)

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def test_crash_after_submission_resends_the_same_command_and_records_once(self):
        a = self.ids["A"]
        client = CrashingClient(CoreClient(self.paths.socket, self.paths.strategy_tokens()[a]))
        r = self.runner("A", "live", client=client)
        with self.assertRaises(Crash):
            r.run_once()
        r.close()
        self.assertEqual(len(self.orders(a)), 1)            # the core took the first intent
        self.assertEqual(self.decisions(a), [])
        with self.runner("A", "live") as again:
            self.assertEqual({x["state"] for x in again.journal.rows(self.day)}, {"PLANNED"})
            s = again.run_once()
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertTrue(one["submit_duplicate"], one)       # the same id and payload: the core's stored result
        self.assertFalse(two["submit_duplicate"], two)
        self.assertEqual({x["outcome"] for x in (one, two)}, {"INTENT_SUBMITTED"})
        orders = self.orders(a)
        self.assertEqual(sorted((o["instrument_id"], o["quantity"]) for o in orders), [("FIX-EQ-1", "4"), ("FIX-EQ-2", "1")])
        self.assertEqual(sorted(d["instrument_id"] for d in self.decisions(a)), ["FIX-EQ-1", "FIX-EQ-2"])
        wait_for(lambda: all(o["state"] == "FILLED" for o in self.orders(a)), what="fills")
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])

    def test_lost_journal_after_a_crash_records_the_cores_intent_without_resubmitting(self):
        a = self.ids["A"]
        r = self.runner("A", "live", client=CrashingClient(CoreClient(self.paths.socket, self.paths.strategy_tokens()[a])))
        with self.assertRaises(Crash):
            r.run_once()
        r.close()
        self.lose_journal("A")
        with self.runner("A", "live") as again:
            s = again.run_once()
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        ids = decision_ids(a, self.day, "FIX-EQ-1")
        self.assertEqual((one["outcome"], one["recovered"], one["order_id"]), ("INTENT_SUBMITTED", True, ids["order_id"]), one)
        self.assertTrue(one["reason"].startswith("RECOVERED after journal loss"), one["reason"])
        self.assertEqual((two["outcome"], two["recovered"]), ("INTENT_SUBMITTED", False), two)
        self.assertEqual(sorted(o["instrument_id"] for o in self.orders(a)), ["FIX-EQ-1", "FIX-EQ-2"])   # never resubmitted
        rec = {d["instrument_id"]: d for d in self.decisions(a)}
        self.assertEqual((len(rec), rec["FIX-EQ-1"]["order_ids"]), (2, [ids["order_id"]]))
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])

    def test_core_outage_keeps_decisions_planned_and_sends_them_once_after_restart(self):
        a = self.ids["A"]
        self.stop_core()
        with self.runner("A", "live") as r, self.assertLogs("unknown.strategy", level="ERROR") as logs:
            s = r.run_once()
            self.assertEqual(s["result"], "CORE_UNREACHABLE")
            self.assertIn("stays PLANNED", logs.output[0])
            self.assertEqual({x["state"] for x in r.journal.rows(self.day)}, {"PLANNED"})
            self.assertEqual(r.run_once()["result"], "CORE_UNREACHABLE")      # still down: nothing new is planned
            planned = {x["instrument_id"]: x["submit_payload"] for x in r.journal.rows(self.day)}
        self.assertEqual(len(planned), 2)
        self.start_core()                     # same home and database; the simulator restarts with the same cash
        self.green_start()                    # a restarted core needs a fresh owner start before new exposure
        with self.runner("A", "live") as r:
            s = r.run_once()
            sent = {x["instrument_id"]: x["submit_payload"] for x in r.journal.rows(self.day)}
        self.assertEqual(sent, planned)        # exactly the payloads planned before the outage
        self.assertEqual({x["outcome"] for x in s["instruments"].values()}, {"INTENT_SUBMITTED"}, s)
        self.assertFalse(any(x["submit_duplicate"] for x in s["instruments"].values()))
        self.assertEqual((len(self.orders(a)), len(self.decisions(a))), (2, 2))
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])

    def test_an_outage_past_the_session_records_its_decisions_late_and_never_submits_them(self):
        """Review fix: a cycle only looked at the latest eligible session, so decisions an outage left PLANNED (or
        SUBMITTED without a record) stayed so forever once the next session became eligible: never recorded. They
        are now finished first; an intent past its deadline is never sent and is recorded BLOCKED (RUNNER_LATE)."""
        a = self.ids["A"]
        early = ManualClock(decision_instant(self.day, CLOCK_UTC) - 3600 * NS_PER_S)
        prev = latest_eligible_session(early.now_ns(), WEEKDAYS_ONLY, CLOCK_UTC)
        self.assertLess(prev, self.day)
        self.stop_core()
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=early) as r, \
                self.assertLogs("unknown.strategy", level="ERROR"):
            s = r.run_once()
            planned = r.journal.rows(prev)
        self.assertEqual((s["result"], s["session_date"]), ("CORE_UNREACHABLE", prev.isoformat()))
        self.assertEqual({x["state"] for x in planned}, {"PLANNED"})
        intents = sorted(x["instrument_id"] for x in planned if x["submit_payload"] is not None)
        self.assertTrue(intents, planned)
        self.start_core()
        self.green_start()
        with self.runner("A", "live") as r2, self.assertLogs("unknown.strategy", level="WARNING"):
            s2 = r2.run_once()             # the latest eligible session is now self.day; prev's intents are late
            late = {x["instrument_id"]: x for x in r2.journal.rows(prev)}
        self.assertEqual({x["state"] for x in late.values()}, {"RECORDED"})
        self.assertEqual(sorted(s2["carried_over"]), sorted(f"{prev.isoformat()}|{i}" for i in INSTRUMENTS))
        for i in intents:
            self.assertEqual(late[i]["decision_payload"]["outcome"], "BLOCKED")
            self.assertTrue(late[i]["decision_payload"]["reason"].startswith("RUNNER_LATE"), late[i]["decision_payload"])
            self.assertIsNone(late[i]["submit_result"])
        prev_order_ids = {decision_ids(a, prev, i)["order_id"] for i in INSTRUMENTS}
        self.assertFalse(prev_order_ids & {o["order_id"] for o in self.orders(a)})     # never submitted
        self.assertEqual({x["outcome"] for x in s2["instruments"].values()}, {"INTENT_SUBMITTED"}, s2)
        t_prev = decision_instant(prev, CLOCK_UTC)
        recs = self.decisions(a)
        self.assertEqual(sorted(d["instrument_id"] for d in recs if d["observation_time_ns"] == t_prev), sorted(INSTRUMENTS))
        self.assertEqual(len(recs), 4)
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class ShadowRunnerTest(RealCoreCase):
    LISTED = ("C", "V", "Q")
    STALE = ("FIX-EQ-2",)

    def make_artifacts(self) -> None:
        self.add("C", "10", "paper")
        self.add("V", "5", "validated")
        self.add("Q", "20", "paper")
        self.rf.quarantine(self.hashes["Q"])

    def test_shadow_records_decisions_without_intents_through_the_process_entry_point(self):
        c = self.ids["C"]
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        proc = subprocess.run([sys.executable, "-m", "unknown.strategy", "--home", str(self.root), "run", "--artifact", c,
                               "--account", ACCOUNT, "--mode", "shadow", "--once"], env=env, capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        s = json.loads(proc.stdout)
        self.assertEqual((s["mode"], s["session_date"]), ("shadow", self.day.isoformat()))
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertEqual((one["outcome"], one["submitted"]), ("NO_TRADE", False))
        self.assertTrue(one["reason"].startswith("SHADOW_WOULD_SUBMIT BUY 4 @ 10.05"), one["reason"])
        self.assertEqual(two["outcome"], "SKIPPED_STALE_DATA")
        self.assertEqual(self.orders(), [])
        recs = self.decisions(c)
        self.assertEqual(sorted((d["instrument_id"], d["outcome"], d["mode"]) for d in recs),
                         [("FIX-EQ-1", "NO_TRADE", "shadow"), ("FIX-EQ-2", "SKIPPED_STALE_DATA", "shadow")])
        self.assertEqual(next(d for d in recs if d["instrument_id"] == "FIX-EQ-1")["target_quantity"], "4")
        with self.runner("C", "shadow") as r:                # a second run (in process) is idempotent
            s2 = r.run_once()
        self.assertTrue(all(x["already_decided"] for x in s2["instruments"].values()))
        self.assertEqual(len(self.decisions(c)), 2)

    def test_registry_status_and_token_gate_the_runner_before_the_core(self):
        for name, code in (("V", "STATUS_NOT_PERMITTED"), ("Q", "STATUS_NOT_PERMITTED")):
            with self.runner(name, "shadow") as r:
                with self.assertRaises(ArtifactRefused) as cm:
                    r.run_once()
            self.assertEqual(cm.exception.code, code, name)
            self.assertEqual(self.decisions(self.ids[name]), [])
        # a paper artifact may also run live, but only with the owner-issued token bound to it
        cfg = RunnerConfig(self.ids["C"], ACCOUNT, "live", Paths(self.root / "elsewhere"),
                           registry_path=self.root / "research" / "experiments.sqlite",
                           research_path=self.root / "research" / "research.sqlite")
        with StrategyRunner(cfg, clock=self.clock) as r:
            with self.assertRaises(ArtifactRefused) as cm:
                r.run_once()
        self.assertEqual(cm.exception.code, "NO_STRATEGY_TOKEN")
        self.assertEqual((self.orders(), self.decisions()), ([], []))


if __name__ == "__main__":
    unittest.main()
