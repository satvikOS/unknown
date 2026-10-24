"""BR-DAT-19: TREND_DAILY_V1's execution rule. Submit inside the next regular session between open + N and close - M
minutes (calendar-aware, from the spec), never in an auction, with the limit referenced at transmission, and the
remainder cancelled at the window's end. ``research.execution`` is the one implementation the backtest and the
strategy runner use. Every bar and price is a TEST FIXTURE."""
from __future__ import annotations

import copy
import unittest
from datetime import date
from pathlib import Path

from unknown.domain.calendar import WEEKDAYS_ONLY, load_calendar
from unknown.domain.clock import utc_ns
from unknown.domain.fixed import Fixed
from unknown.research import execution
from unknown.research.bars import BarSeries, DatasetView
from unknown.research.common import SpecError
from unknown.research.costs import CostModel, IbkrFixedUS
from unknown.research.engine import BacktestConfig, ExecutionModel, OrderRequest, Sizing, StrategyDecision, run_backtest
from unknown.research.execution import SessionHoursUnknown, SubmissionWindow, transmission_limit, window_times
from support.research_fixtures import fixture_instrument, manual_bar
from unknown.research.spec import parse_spec
from unknown.research.strategies import trend_spec

F = Fixed.parse
REPO = Path(__file__).resolve().parents[2]
XNYS = load_calendar(REPO / "config" / "calendars" / "XNYS.json")
DEV, HOLD = (date(2021, 1, 4), date(2022, 12, 30)), (date(2023, 1, 2), date(2023, 12, 29))
A = "FIX-W-1"
W = SubmissionWindow(15, 15, 50)


class SpecContractTest(unittest.TestCase):
    def test_trend_daily_v1_declares_its_window_and_no_auctions(self):
        spec = trend_spec([A], DEV, HOLD)
        self.assertEqual(spec.schema_version, 2)
        self.assertEqual(spec.submission_window, {"open_offset_minutes": 15, "close_offset_minutes": 15, "auctions": False,
                                                  "limit_reference": "TRANSMISSION_QUOTE_MID", "collar_bps": 50})
        self.assertEqual(spec.execution["unfilled_policy"], "CANCEL_AT_WINDOW_END")
        self.assertEqual(SubmissionWindow.from_spec(spec), W)
        cfg = BacktestConfig.from_spec(spec, F("100000"))
        self.assertEqual(cfg.execution.window, W)
        self.assertEqual(cfg.execution.to_json()["submission_window"]["rule"], "execution.window.br-dat-19@1")

    def test_the_window_is_strict(self):
        base = copy.deepcopy(trend_spec([A], DEV, HOLD).body)
        for path, value, fragment in ((("open_offset_minutes",), 0, "open_offset_minutes"),
                                      (("close_offset_minutes",), 0, "close_offset_minutes"),
                                      (("auctions",), True, "auction"), (("limit_reference",), "PRIOR_CLOSE", "limit_reference"),
                                      (("collar_bps",), 5000, "collar_bps")):
            b = copy.deepcopy(base)
            b["rules"]["execution"]["submission_window"][path[0]] = value
            with self.assertRaises(SpecError) as cm:
                parse_spec(b)
            self.assertIn(fragment, str(cm.exception))
        b = copy.deepcopy(base)
        del b["rules"]["execution"]["submission_window"]
        with self.assertRaises(SpecError):                  # required in schema v2
            parse_spec(b)
        b = copy.deepcopy(base)
        b["rules"]["execution"]["unfilled_policy"] = "EXPIRE_END_OF_DAY"
        with self.assertRaises(SpecError):                  # v2 cancels at the window's end
            parse_spec(b)
        b = copy.deepcopy(base)
        b["schema_version"] = 1                             # a v1 document may not carry v2 contracts
        with self.assertRaises(SpecError):
            parse_spec(b)


class WindowTimesTest(unittest.TestCase):
    def test_calendar_hours_early_closes_and_the_fixture_convention(self):
        # XNYS Friday 2026-10-23 (EDT): 09:30-16:00 New York = 13:30-20:00 UTC -> window 13:45-19:45 UTC
        wt = window_times(W, XNYS, date(2026, 10, 23), fixture=False)
        self.assertEqual((wt.open_ns, wt.start_ns, wt.end_ns, wt.close_ns),
                         (utc_ns(2026, 10, 23, 13, 30), utc_ns(2026, 10, 23, 13, 45), utc_ns(2026, 10, 23, 19, 45),
                          utc_ns(2026, 10, 23, 20, 0)))
        self.assertEqual(wt.minutes, (360, 390))
        self.assertTrue(wt.basis.startswith("XNYS@"))
        # early close 2026-11-27 (EST): 09:30-13:00 New York = 14:30-18:00 UTC -> window 14:45-17:45 UTC
        wt = window_times(W, XNYS, date(2026, 11, 27), fixture=False)
        self.assertEqual((wt.start_ns, wt.end_ns), (utc_ns(2026, 11, 27, 14, 45), utc_ns(2026, 11, 27, 17, 45)))
        self.assertEqual(wt.minutes, (180, 210))
        # the WEEKDAYS calendar has no hours: FIXTURE data uses the fixture session convention, real data is refused
        wt = window_times(W, WEEKDAYS_ONLY, date(2026, 10, 23), fixture=True)
        self.assertEqual((wt.start_ns, wt.end_ns), (utc_ns(2026, 10, 23, 14, 45), utc_ns(2026, 10, 23, 20, 45)))
        self.assertEqual(wt.basis, execution.FIXTURE_SESSION_BASIS)
        with self.assertRaises(SessionHoursUnknown):
            window_times(W, WEEKDAYS_ONLY, date(2026, 10, 23), fixture=False)
        with self.assertRaises(SessionHoursUnknown):        # a holiday has no session
            window_times(W, XNYS, date(2026, 11, 26), fixture=False)
        # offsets that leave nothing of a short session give an empty window
        self.assertTrue(window_times(SubmissionWindow(120, 120, 50), XNYS, date(2026, 11, 27), fixture=False).empty)

    def test_transmission_limit_is_the_model_bound_tightened_to_the_collar(self):
        inst = fixture_instrument(A)
        self.assertEqual(transmission_limit("BUY", F("10.05"), F("9.00"), 50, inst), F("9.04"))     # gap down: collar binds
        self.assertEqual(transmission_limit("BUY", F("10.05"), F("10.20"), 50, inst), F("10.05"))   # model bound binds
        self.assertEqual(transmission_limit("SELL", F("9.95"), F("10.50"), 50, inst), F("10.45"))   # gap up: collar binds
        self.assertEqual(transmission_limit("SELL", F("9.95"), F("9.00"), 50, inst), F("9.95"))
        self.assertEqual(transmission_limit("BUY", F("0.5050"), F("0.4000"), 50, inst), F("0.4020"))  # sub-dollar tick


class Scripted:
    strategy_id = "SCRIPTED"
    spec = None

    def __init__(self, script):
        self.script = script

    def reset(self):
        pass

    def decide(self, ctx):
        return StrategyDecision(targets=None, orders=tuple(self.script.get(ctx.session_date, ())))


def run(bars, script, *, window: SubmissionWindow | None, cash="10000", participation="0.10"):
    cost = CostModel(schedule=IbkrFixedUS(), spread_bps=F("0"), slippage_bps=F("0"), impact_y=F("0"),
                     third_party_sell_bps=F("0"))
    cfg = BacktestConfig(initial_cash=F(cash), cost_model=cost,
                         execution=ExecutionModel(participation=F(participation), limit_offset_bps=50, window=window),
                         sizing=Sizing(F("0.95"), F("0.95"), F("0.05"), F("0"), F("0")))
    view = DatasetView({A: BarSeries(A, bars)}, until=bars[-1].session_date, label="window")
    return run_backtest(Scripted(script), view, {A: fixture_instrument(A)}, cfg)


def ev(res, kind):
    return [e for e in res.events if e["type"] == kind]


class BacktestWindowTest(unittest.TestCase):
    D0, D1 = date(2024, 1, 8), date(2024, 1, 9)

    def test_submission_at_open_plus_n_with_the_limit_set_at_transmission(self):
        # decided on D0 at a model bound of 10.05; D1 gaps down to open at 9.00
        bars = [manual_bar(A, self.D0, "10.00", "10.10", "9.90", "10.00"), manual_bar(A, self.D1, "9.00", "9.30", "8.90", "9.20")]
        script = {self.D0: (OrderRequest(A, "BUY", F("100"), F("10.05")),)}
        legacy, windowed = run(bars, script, window=None), run(bars, script, window=W)
        (l_sub,), (w_sub,) = ev(legacy, "ORDER_SUBMITTED"), ev(windowed, "ORDER_SUBMITTED")
        self.assertEqual(l_sub["t_ns"], bars[1].start_ns + 60 * 10**9)                     # legacy: 60 s after the open
        self.assertEqual(w_sub["t_ns"], bars[1].start_ns + 15 * 60 * 10**9)                # open + 15 minutes
        self.assertEqual(l_sub["limit_price"], "10.05")
        self.assertEqual(w_sub["limit_price"], "9.04")                                      # min(10.05, 9.00 x 1.005)
        self.assertEqual(w_sub["transmission"]["model_bound"], "10.05")
        self.assertEqual(w_sub["transmission"]["reference"], "9.00")
        self.assertEqual(w_sub["transmission"]["window"]["basis"], execution.FIXTURE_SESSION_BASIS)
        self.assertEqual(windowed.orders[0]["model_bound"], "10.05")
        self.assertNotIn("model_bound", legacy.orders[0])
        # the reservation is made at the transmitted limit: 100 x 9.04 + 1.00 = 905.00 fits 905.00 of cash, while
        # 100 x 10.05 + 1.00 = 1006.00 does not
        legacy, windowed = run(bars, script, window=None, cash="905.00"), run(bars, script, window=W, cash="905.00")
        self.assertEqual(ev(legacy, "ORDER_REJECTED")[0]["reason"], "INSUFFICIENT_SETTLED_CASH")
        self.assertEqual([f["price"] for f in windowed.fills], ["9.00"])

    def test_the_order_trades_only_the_windows_share_of_the_volume_and_the_remainder_is_cancelled(self):
        bars = [manual_bar(A, self.D0, "10.00", "10.10", "9.90", "10.00", 1000),
                manual_bar(A, self.D1, "10.00", "10.10", "9.80", "10.00", 1000)]
        script = {self.D0: (OrderRequest(A, "BUY", F("500"), F("10.05")),)}
        legacy, windowed = run(bars, script, window=None), run(bars, script, window=W)
        self.assertEqual([f["quantity"] for f in legacy.fills], ["100"])                     # 10% of 1000
        self.assertEqual([f["quantity"] for f in windowed.fills], ["92"])                    # 10% of 1000 x 360 / 390
        (exp,) = ev(windowed, "ORDER_EXPIRED")
        self.assertEqual((exp["reason"], exp["leaves"]), ("WINDOW_END_REMAINDER", "408"))
        self.assertEqual(ev(legacy, "ORDER_EXPIRED")[0]["reason"], "END_OF_DAY_REMAINDER")
        nxt = [o for o in windowed.orders if o["status"] != "PARTIALLY_FILLED_EXPIRED"]
        self.assertEqual(nxt, [])                                                            # never carried over

    def test_a_gap_up_keeps_the_model_bound_and_real_data_needs_session_hours(self):
        bars = [manual_bar(A, self.D0, "10.00", "10.10", "9.90", "10.00"), manual_bar(A, self.D1, "10.40", "10.60", "10.02", "10.50")]
        script = {self.D0: (OrderRequest(A, "BUY", F("10"), F("10.05")),)}
        res = run(bars, script, window=W)
        self.assertEqual(ev(res, "ORDER_SUBMITTED")[0]["limit_price"], "10.05")              # min(10.05, 10.452)
        self.assertEqual([f["price"] for f in res.fills], ["10.05"])                          # traded down through it
        real = [manual_bar(A, b.session_date, b.open.to_str(), b.high.to_str(), b.low.to_str(), b.close.to_str(),
                           source="TEST:not-a-fixture") for b in bars]
        real = [type(b)(**{**b.__dict__, "fixture": False}) for b in real]
        with self.assertRaises(SessionHoursUnknown):
            run(real, script, window=W)

    def test_the_backtest_uses_the_shared_implementation(self):
        from unknown.research import engine
        self.assertIs(engine.window_times, execution.window_times)
        self.assertIs(engine.transmission_limit, execution.transmission_limit)


if __name__ == "__main__":
    unittest.main()
