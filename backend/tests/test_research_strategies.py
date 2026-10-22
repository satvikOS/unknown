"""TREND_DAILY_V1 (book ch. 15 eq. 15.1) and its baselines. All series are TEST FIXTURES."""
from __future__ import annotations

import copy
import math
import unittest
from datetime import date

from unknown.domain.fixed import Fixed
from unknown.research.bars import BarSeries, DatasetView
from unknown.research.engine import BacktestConfig, run_backtest
from unknown.research.fixtures import business_days, fixture_instrument, manual_bar
from unknown.research.spec import parse_spec
from unknown.research.strategies import (BuyAndHold, TrendDailyV1, VolCappedPassive, build, buy_and_hold_spec, trend_score,
                                         trend_spec, vol_capped_spec)

F = Fixed.parse
A, B = "FIX-UP-1", "FIX-UP-2"
DAYS = business_days(date(2023, 1, 2), 140)
DEV, HOLD = (DAYS[0], DAYS[119]), (DAYS[120], DAYS[-1])


def series(inst: str, closes: list[float]) -> list:
    out = []
    for d, c in zip(DAYS, closes):
        p = F(f"{c:.2f}")
        out.append(manual_bar(inst, d, p.to_str(), (p + 1).to_str(), (p - 1).to_str(), p.to_str(), 2_000_000))
    return out


def run(spec, *bar_lists):
    view = DatasetView({b[0].instrument_id: BarSeries(b[0].instrument_id, b) for b in bar_lists}, until=DEV[1], label="t",
                       holdout=HOLD)
    inst = {i: fixture_instrument(i) for i in view.instrument_ids}
    return run_backtest(build(spec), view, inst, BacktestConfig.from_spec(spec, F("100000")))


def decisions(res):
    return [e for e in res.events if e["type"] == "DECISION"]


class TrendScoreKnownAnswer(unittest.TestCase):
    def test_eq_15_1_by_hand(self):
        x1, x2, x3 = math.log(100.0), math.log(110.0), math.log(99.0)
        ema_f = x3                                   # span 1: a = 1
        ema_s = 0.5 * (0.5 * x1 + 0.5 * x2) + 0.5 * x3  # span 3: a = 0.5
        var = 0.5 * (x2 - x1) ** 2 + 0.5 * (x3 - x2) ** 2  # half-life 1: lambda = 0.5, v_2 = r_2^2
        expected = (ema_f - ema_s) / (math.sqrt(var) * math.sqrt(4) + 0.001)
        got = trend_score([100.0, 110.0, 99.0], fast_span=1, slow_span=3, vol_half_life=1, horizon_h=4, epsilon=0.001)
        self.assertAlmostEqual(got["z"], expected, places=14)
        self.assertAlmostEqual(got["sigma"], math.sqrt(var), places=14)
        self.assertLess(got["z"], 0)

    def test_single_observation_has_no_score(self):
        self.assertIsNone(trend_score([100.0], fast_span=2, slow_span=5, vol_half_life=5, horizon_h=1, epsilon=0.001)["z"])

    def test_fast_must_be_faster_than_slow(self):
        with self.assertRaises(Exception):
            TrendDailyV1(trend_spec([A], DEV, HOLD, parameters={"fast_span": "40", "slow_span": "40"}))


class TrendBehaviourTest(unittest.TestCase):
    def test_warm_up_then_capped_long_only_weights(self):
        up = series(A, [50 * 1.004 ** i for i in range(140)])
        spec = trend_spec([A], DEV, HOLD, parameters={"fast_span": "5", "slow_span": "20", "k": "5"})
        res = run(spec, up)
        ds = decisions(res)
        self.assertTrue(all(d["notes"][A]["state"] == "WARMING" for d in ds[:19]))
        self.assertEqual(ds[19]["notes"][A]["formula"], "eq15.1@1")
        late = ds[-1]
        self.assertAlmostEqual(late["targets"][A], 0.45)          # min(w_max, k z) with a large z
        self.assertEqual(late["plan"]["weights"][A], "0.450000")
        self.assertGreater(len(res.fills), 0)
        self.assertTrue(all(f["side"] == "BUY" or f["side"] == "SELL" for f in res.fills))

    def test_downtrend_never_buys(self):
        down = series(A, [80 * 0.996 ** i for i in range(140)])
        res = run(trend_spec([A], DEV, HOLD, parameters={"fast_span": "5", "slow_span": "20"}), down)
        self.assertEqual(res.fills, [])
        self.assertTrue(all(d["targets"].get(A, 0.0) == 0.0 for d in decisions(res)))

    def test_aggregate_cap_and_cash_reserve_scale_weights(self):
        body = copy.deepcopy(trend_spec([A, B], DEV, HOLD, parameters={"fast_span": "5", "slow_span": "20", "k": "5"}).body)
        body["rules"]["sizing"].update({"max_weight": "0.6", "aggregate_cap": "0.95", "cash_reserve": "0.10"})
        body["risk_limits"]["max_single_name_weight"] = "0.95"
        body["parameters"]["w_max"] = "0.6"
        spec = parse_spec(body)
        res = run(spec, series(A, [50 * 1.004 ** i for i in range(140)]), series(B, [30 * 1.005 ** i for i in range(140)]))
        plan = decisions(res)[-1]["plan"]
        self.assertEqual(plan["scaled_to_aggregate_cap"], "0.90")
        self.assertEqual(plan["weights"], {A: "0.450000", B: "0.450000"})

    def test_no_trade_band_suppresses_small_rebalances(self):
        up = series(A, [50 * 1.004 ** i for i in range(140)])
        res = run(trend_spec([A], DEV, HOLD, parameters={"fast_span": "5", "slow_span": "20", "k": "5"}), up)
        banded = [d for d in decisions(res) if "NO_TRADE_BAND" in str(d["plan"].get("instruments", {}).get(A, ""))]
        self.assertGreater(len(banded), 50)
        self.assertLess(len(res.fills), 30)


class BaselineTest(unittest.TestCase):
    def test_buy_and_hold_buys_once_and_holds(self):
        up = series(A, [50 * 1.002 ** i for i in range(140)])
        spec = buy_and_hold_spec([A], DEV, HOLD)
        self.assertIsInstance(build(spec), BuyAndHold)
        res = run(spec, up)
        self.assertEqual([f["side"] for f in res.fills], ["BUY"])
        self.assertEqual(sum(1 for d in decisions(res) if d["outcome"] == "INTENTS"), 1)

    def test_vol_capped_weight_follows_sigma_target(self):
        rng_closes = [50.0]
        for i in range(139):
            rng_closes.append(rng_closes[-1] * (1.02 if i % 2 else 0.98))
        spec = vol_capped_spec([A], DEV, HOLD)
        self.assertIsInstance(build(spec), VolCappedPassive)
        res = run(spec, series(A, rng_closes))
        last = decisions(res)[-1]["notes"][A]
        self.assertAlmostEqual(last["w"], min(0.95, 0.01 / last["sigma"]))
        self.assertLess(last["w"], 0.95)


if __name__ == "__main__":
    unittest.main()
