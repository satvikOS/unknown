"""Second validation path: the fast engine's orders replayed through the REAL core (RES-05; CAP-RES-04).

A temporary core database with the SimulatedBroker fixture receives every
order the engine submitted; fills, cash (settled and unsettled), positions,
lot cost, final commissions and cash-account rejections must agree exactly,
and the core's ledger invariants must hold after every session
(``Desk.verify() == []``). A mutation test proves the comparison can fail.
Every number is a TEST FIXTURE."""
from __future__ import annotations

import copy
import unittest
from datetime import date

from unknown.domain.fixed import Fixed
from unknown.research.bars import BarSeries, DatasetView
from unknown.research.core_replay import replay_through_core
from unknown.research.costs import CostModel, IbkrFixedUS
from unknown.research.engine import (BacktestConfig, ExecutionModel, OrderRequest, Sizing, StrategyDecision, run_backtest)
from unknown.research.fixtures import fixture_instrument, manual_bar, synthetic_bars
from unknown.research.strategies import build, trend_spec

F = Fixed.parse
A, B = "FIX-CR-1", "FIX-CR-2"
D = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11), date(2024, 1, 12), date(2024, 1, 15),
     date(2024, 1, 16)]


class Scripted:
    strategy_id = "SCRIPTED"
    spec = None

    def __init__(self, script):
        self.script = script

    def reset(self):
        pass

    def decide(self, ctx):
        return StrategyDecision(targets=None, orders=tuple(self.script.get(ctx.session_date, ())))


def flat(inst, price="50.00", volume=1_000_000, days=D):
    p = F(price)
    return [manual_bar(inst, d, price, (p + 1).to_str(), (p - 1).to_str(), price, volume) for d in days]


class CoreReplayTest(unittest.TestCase):
    def assertClean(self, rep: dict) -> None:
        self.assertTrue(rep["comparable"], rep.get("reason"))
        self.assertEqual(rep["mismatches"], [])
        self.assertEqual(rep["ledger_problems"], [])

    def test_trend_strategy_run_matches_the_core_exactly(self):
        a = synthetic_bars("trend", A, start=date(2022, 1, 3), sessions=110, seed=1)
        b = synthetic_bars("mean_reverting", B, start=date(2022, 1, 3), sessions=110, seed=2)
        days = [x.session_date for x in a]
        spec = trend_spec([A, B], (days[0], days[79]), (days[80], days[-1]), parameters={"fast_span": "3", "slow_span": "10", "k": "2.0"})
        view = DatasetView({A: BarSeries(A, a), B: BarSeries(B, b)}, until=days[79], label="cr", holdout=(days[80], days[-1]))
        inst = {i: fixture_instrument(i) for i in (A, B)}
        res = run_backtest(build(spec), view, inst, BacktestConfig.from_spec(spec, F("10000")))
        sides = {f["side"] for f in res.fills}
        self.assertEqual(sides, {"BUY", "SELL"})
        rep = replay_through_core(res, inst)
        self.assertClean(rep)
        self.assertEqual(rep["fills_compared"], len(res.fills))
        self.assertEqual(rep["days_compared"], 80)
        self.assertGreater(F(res.costs["third_party"]), F("0"))   # sell-side fees reached the core as commission

    def test_partial_fills_t_plus_1_and_rejections_agree(self):
        a = [manual_bar(A, D[0], "50.00", "51.00", "49.00", "50.00"),
             manual_bar(A, D[1], "49.00", "50.00", "48.00", "49.50", 1_000_000),
             manual_bar(A, D[2], "52.00", "53.00", "51.00", "52.50")] + flat(A, "52.00", days=D[3:])
        b = flat(B, "52.00")
        b[2] = manual_bar(B, D[2], "51.00", "53.00", "50.00", "52.00")
        b[3] = manual_bar(B, D[3], "51.00", "53.00", "50.00", "52.00", 600)      # participation cap: 60 of 100
        script = {D[0]: [OrderRequest(A, "BUY", F("100"), F("50.00"))],
                  D[1]: [OrderRequest(A, "SELL", F("100"), F("50.00")), OrderRequest(B, "BUY", F("100"), F("52.00"))],
                  D[2]: [OrderRequest(B, "BUY", F("100"), F("52.00")), OrderRequest(A, "SELL", F("5"), F("40.00"))],
                  D[4]: [OrderRequest(B, "SELL", F("60"), F("51.00"))]}
        cost = CostModel(IbkrFixedUS(), F("10"), F("5"), F("1.0"), F("1.0"))
        cfg = BacktestConfig(initial_cash=F("10000"), cost_model=cost,
                             execution=ExecutionModel(participation=F("0.10"), limit_offset_bps=50),
                             sizing=Sizing(F("0.95"), F("0.95"), F("0.05"), F("0"), F("0")))
        view = DatasetView({A: BarSeries(A, a), B: BarSeries(B, b)}, until=D[-1], label="script")
        inst = {i: fixture_instrument(i) for i in (A, B)}
        res = run_backtest(Scripted(script), view, inst, cfg)
        statuses = [(o["side"], o["status"], o["detail"]) for o in res.orders]
        self.assertIn(("BUY", "REJECTED", "INSUFFICIENT_SETTLED_CASH"), statuses)
        self.assertIn(("SELL", "REJECTED", "NO_SHORTING"), statuses)
        self.assertIn(("BUY", "PARTIALLY_FILLED_EXPIRED", ""), statuses)
        rep = replay_through_core(res, inst)
        self.assertClean(rep)
        self.assertEqual(rep["rejections_matched"], 2)
        self.assertEqual(rep["fills_compared"], 4)

    def test_a_tampered_engine_result_is_detected(self):
        a = flat(A)
        res = run_backtest(Scripted({D[0]: [OrderRequest(A, "BUY", F("10"), F("50.00"))],
                                     D[2]: [OrderRequest(A, "SELL", F("10"), F("49.00"))]}),
                           DatasetView({A: BarSeries(A, a)}, until=D[-1], label="t"), {A: fixture_instrument(A)},
                           BacktestConfig(initial_cash=F("1000"), cost_model=CostModel(IbkrFixedUS(), F("0"), F("0"), F("0"), F("0")),
                                          execution=ExecutionModel(), sizing=Sizing(F("0.95"), F("0.95"), F("0.05"), F("0"), F("0"))))
        self.assertClean(replay_through_core(res, {A: fixture_instrument(A)}))
        bad = copy.deepcopy(res)
        bad.fills[0]["fees"]["total"] = "2.000000"           # the core would book a different commission
        rep = replay_through_core(bad, {A: fixture_instrument(A)})
        self.assertTrue(any("unsettled" in m or "fee" in m for m in rep["mismatches"]), rep["mismatches"])
        bad = copy.deepcopy(res)
        mark = next(e for e in bad.events if e["type"] == "MARK" and e["date"] == D[1].isoformat())
        mark["positions"][A] = "11"
        rep = replay_through_core(bad, {A: fixture_instrument(A)})
        self.assertTrue(any("position" in m for m in rep["mismatches"]), rep["mismatches"])

    def test_a_consistently_wrong_engine_commission_is_caught_by_the_core_fee_model(self):
        """Review fix: the core books whatever commission it is told, so a self-consistent engine fee bug used to
        replay 'clean'. The engine's commission is now recomputed with the core's own risk.fees model."""
        from unknown.research.costs import _ceil6

        class NoMinimum(IbkrFixedUS):          # bug: forgets the 1.00 minimum; describe() is unchanged
            def commission(self, quantity, price, month_volume_before=0):
                return _ceil6(self.per_share * quantity)
        a = flat(A)
        script = {D[0]: [OrderRequest(A, "BUY", F("10"), F("50.00"))], D[2]: [OrderRequest(A, "SELL", F("10"), F("49.00"))]}

        def run(schedule):
            return run_backtest(Scripted(script), DatasetView({A: BarSeries(A, a)}, until=D[-1], label="fee"),
                                {A: fixture_instrument(A)},
                                BacktestConfig(initial_cash=F("1000"), cost_model=CostModel(schedule, F("0"), F("0"), F("0"), F("0")),
                                               execution=ExecutionModel(), sizing=Sizing(F("0.95"), F("0.95"), F("0.05"), F("0"), F("0"))))
        good = replay_through_core(run(IbkrFixedUS()), {A: fixture_instrument(A)})
        self.assertClean(good)
        self.assertEqual(good["commissions_checked_against_core_fee_model"], 2)
        bad = replay_through_core(run(NoMinimum()), {A: fixture_instrument(A)})
        self.assertTrue(any("core fee model" in m for m in bad["mismatches"]), bad["mismatches"])

    def test_inexpressible_configurations_are_reported_not_compared(self):
        a = flat(A)
        cfg = BacktestConfig(initial_cash=F("1000"), cost_model=CostModel(IbkrFixedUS(), F("0"), F("0"), F("0"), F("0"), F("2")),
                             execution=ExecutionModel(), sizing=Sizing(F("0.95"), F("0.95"), F("0.05"), F("0"), F("0")))
        res = run_backtest(Scripted({}), DatasetView({A: BarSeries(A, a)}, until=D[-1], label="t"), {A: fixture_instrument(A)}, cfg)
        rep = replay_through_core(res, {A: fixture_instrument(A)})
        self.assertFalse(rep["comparable"])
        self.assertTrue(rep["mismatches"])


if __name__ == "__main__":
    unittest.main()
