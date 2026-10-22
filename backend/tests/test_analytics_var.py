"""Historical VaR, quantile-integral ES (book eq. 6.3) and the Kupiec backtest (CAP-PORT-10).

The loss-vector tests compare against an independent reference built from the empirical CDF
(unique values and cumulative probabilities), including tied losses. Ledger-driven tests use
real snapshot histories; every price path is a TEST FIXTURE."""
from __future__ import annotations

import math
import random
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import DAY, booted, buy, eod, quotes, ro, snapshot_store  # noqa: E402
from support import ACCOUNT  # noqa: E402

from unknown.analytics.common import SYSTEM, parse_scope  # noqa: E402
from unknown.analytics.holdings import holdings  # noqa: E402
from unknown.analytics.var import (backtest, history_as_of, kupiec, mark_table, returns_from_prices, var_es,  # noqa: E402
                                   var_report)


def reference(losses: list[int], alpha: Fraction) -> tuple[Fraction, Fraction, Fraction]:
    """Independent definitions: VaR = inf{l: F(l) >= alpha}; ES = (1/(1-alpha)) int_alpha^1 VaR_u du; tail mean."""
    n = len(losses)
    values = sorted(set(losses))
    cdf, acc = [], 0
    for v in values:
        acc += losses.count(v)
        cdf.append(Fraction(acc, n))
    var = next(v for v, f in zip(values, cdf) if f >= alpha)
    if alpha == 1:
        es = Fraction(values[-1])
    else:
        integral, prev = Fraction(0), Fraction(0)
        for v, f in zip(values, cdf):
            lo, hi = max(prev, alpha), f
            if hi > lo:
                integral += v * (hi - lo)
            prev = f
        es = integral / (1 - alpha)
    tail = [x for x in losses if x >= var]
    return Fraction(var), es, Fraction(sum(tail), len(tail))


class LossVectorTest(unittest.TestCase):
    def setUp(self):
        rng = random.Random(42)
        self.losses = [round(rng.gauss(0, 10)) for _ in range(1000)]  # integers: many ties (atoms)

    def test_matches_independent_reference_with_ties(self):
        for a in ("0.95", "0.975", "0.99", "0.995", "0.999", "1"):
            r = var_es(self.losses, a)
            v, es, tail = reference(self.losses, Fraction(a))
            self.assertEqual(r["var"], float(v), a)
            self.assertAlmostEqual(r["es"], float(es), places=12, msg=a)
            self.assertAlmostEqual(r["es_tail_mean_ibkr_form"], float(tail), places=12, msg=a)

    def test_es_dominates_var_and_both_rise_with_confidence(self):
        prev = None
        for a in ("0.9", "0.95", "0.975", "0.99", "0.995", "0.999", "1"):
            r = var_es(self.losses, a)
            self.assertGreaterEqual(r["es"], r["var"])
            if prev:
                self.assertGreaterEqual(r["var"], prev["var"])
                self.assertGreaterEqual(r["es"], prev["es"])
            prev = r
        full = var_es(self.losses, "1")
        self.assertEqual(full["var"], max(self.losses))
        self.assertEqual(full["es"], max(self.losses))

    def test_atoms_separate_the_two_es_forms(self):
        losses = [0] * 95 + [10] * 5
        r = var_es(losses, "0.9")  # VaR = 0; eq. 6.3 averages the top 10%; the tail mean averages every loss >= 0
        self.assertEqual(r["var"], 0.0)
        self.assertAlmostEqual(r["es"], 5.0, places=12)
        self.assertAlmostEqual(r["es_tail_mean_ibkr_form"], 0.5, places=12)

    def test_kupiec_matches_hand_computation(self):
        x, T, p = 5, 250, 0.01
        lr = -2 * ((T - x) * math.log(1 - p) + x * math.log(p)) + 2 * ((T - x) * math.log(1 - x / T) + x * math.log(x / T))
        r = kupiec(x, T, "0.99")
        self.assertAlmostEqual(r["lr"], lr, places=12)
        self.assertAlmostEqual(r["p_value"], math.erfc(math.sqrt(lr / 2)), places=12)
        self.assertAlmostEqual(r["expected_exceptions"], 2.5, places=12)
        zero = kupiec(0, 250, "0.99")
        self.assertAlmostEqual(zero["lr"], -2 * 250 * math.log(0.99), places=12)
        self.assertIsNone(kupiec(1, 10, "1")["lr"])


def _history(test, n_days: int, seed: int = 3):
    h = booted(test)
    conn = ro(test, h)
    store = snapshot_store(test)
    buy(h, "S1", "B1", "5", "9.00", bid="9.00", ask="9.02")
    rng = random.Random(seed)
    price = Fraction(9)
    times, prices = [], []
    for i in range(n_days):
        if i:
            h.clock.advance(DAY)
            price = max(Fraction(1), Fraction(round(float(price) * (1 + rng.gauss(0, 0.02)), 2)).limit_denominator(100))
        text = f"{float(price):.2f}"
        eod(test, h, store, conn, {"FIX-EQ-1": text})
        times.append(h.clock.now_ns())
        prices.append(Fraction(text))
    return h, conn, store, times, prices


class HistoricalVarTest(unittest.TestCase):
    def test_full_revaluation_matches_hand_scenarios_and_is_point_in_time(self):
        h, conn, store, times, prices = _history(self, 31)
        rep = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=parse_scope("session:S1"))
        r = var_report(rep, store, alpha="0.95", lookback=250, min_scenarios=20)
        usd = r["by_currency"]["USD"]
        exposure = 5 * prices[-1]
        losses = [-(exposure * (prices[i] / prices[i - 1] - 1)) for i in range(1, len(prices))]
        ref = var_es(losses, "0.95")
        self.assertEqual(usd["scenarios"], 30)
        self.assertAlmostEqual(usd["var"], ref["var"], places=12)
        self.assertAlmostEqual(usd["es"], ref["es"], places=12)
        self.assertTrue(r["fixture_data"])
        self.assertEqual(r["statement"], "VaR does not represent the worst-case loss")
        # Point in time: as of day 10, the marks of days 11..30 do not exist.
        dates, hist = history_as_of(mark_table(store, ACCOUNT, as_of_ns=times[-1]), times[10])
        self.assertEqual(len(dates), 11)
        self.assertEqual(len(returns_from_prices(hist, dates)["FIX-EQ-1"]), 10)

    def test_overlapping_multi_day_returns(self):
        h, conn, store, times, prices = _history(self, 25)
        dates, hist = history_as_of(mark_table(store, ACCOUNT, as_of_ns=times[-1]), times[-1])
        r5 = returns_from_prices(hist, dates, horizon=5)["FIX-EQ-1"]
        self.assertEqual(len(r5), 20)
        self.assertEqual(r5[dates[5]], prices[5] / prices[0] - 1)

    def test_too_few_scenarios_is_reported_not_guessed(self):
        h, conn, store, _, _ = _history(self, 5)
        rep = holdings(conn, ACCOUNT, quotes(h), now_ns=h.clock.now_ns(), scope=SYSTEM)
        usd = var_report(rep, store, alpha="0.99")["by_currency"]["USD"]
        self.assertIsNone(usd["var"])
        self.assertIn("< 20 required", usd["reason"])

    def test_backtest_counts_exceptions_against_prior_day_forecasts(self):
        h, conn, store, times, prices = _history(self, 40, seed=5)
        bt = backtest(store, ACCOUNT, parse_scope("session:S1"), "USD", as_of_ns=times[-1], alpha="0.9", window=250,
                      lookback=250, min_scenarios=10)
        # Independent recomputation: forecast for day t uses the returns up to day t-1 and the day t-1 position value.
        expected_rows = []
        for t in range(1, len(prices)):
            past = [prices[i] / prices[i - 1] - 1 for i in range(1, t)]
            if len(past) < 10:
                continue
            exposure = 5 * prices[t - 1]
            v = var_es([-(exposure * r) for r in past], "0.9")["var"]
            realized = -(5 * (prices[t] - prices[t - 1]))
            expected_rows.append((float(realized) > v, v))
        self.assertEqual(bt["observations"], len(expected_rows))
        self.assertEqual(bt["exceptions"], sum(1 for e, _ in expected_rows if e))
        for row, (_, v) in zip(bt["days"], expected_rows):
            self.assertAlmostEqual(row["var_forecast"], v, places=12)
        self.assertEqual(bt["kupiec"]["observations"], len(expected_rows))


if __name__ == "__main__":
    unittest.main()
