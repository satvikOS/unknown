"""BR-DAT-04: the A15 sizer's risk term includes entry and exit fees from the core's reservation fee function, and
sizes a buy to zero when the risk budget cannot cover them (book ch. 6 eq. 6.4, corrected). One sizing function serves
the backtest and the strategy runner. Every number here is a TEST FIXTURE."""
from __future__ import annotations

import copy
import unittest
from datetime import date

from unknown.domain.fixed import Fixed
from unknown.domain.money import Money
from unknown.policy.model import FeeModel
from unknown.research import engine as engine_module
from unknown.research import sizing as research_sizing
from unknown.research.bars import BarSeries, DatasetView
from unknown.research.costs import CostModel, IbkrFixedUS, IbkrTieredUS
from unknown.research.engine import BacktestConfig, Sizing, run_backtest
from support.research_fixtures import fixture_instrument, manual_bar
from unknown.research.sizing import Holding, RiskTerm, fee_envelope_of, plan_orders, risk_quantity
from unknown.research.spec import parse_spec
from unknown.research.strategies import TrendDailyV1, trend_spec
from unknown.risk import fees as core_fees
from unknown.strategy import sizing as runtime_sizing

F = Fixed.parse
DEV, HOLD = (date(2021, 1, 4), date(2022, 12, 30)), (date(2023, 1, 2), date(2023, 12, 29))


def fee_model(per_share="0.005", minimum="1.00", margin_bps=0, cap_bps=100) -> FeeModel:
    return FeeModel(per_share=F(per_share), minimum=Money("USD", F(minimum)), maximum_bps_of_notional=cap_bps,
                    estimate_margin_bps=margin_bps, source="TEST FIXTURE", verified_on="test")


def v1_body() -> dict:
    """The TREND_DAILY_V1 builder's body as a schema v1 document (no window, no risk budget, no validity windows)."""
    b = copy.deepcopy(trend_spec(["FIX-RS-1"], DEV, HOLD).body)
    b["schema_version"] = 1
    del b["rules"]["sizing"]["risk_budget"]
    del b["rules"]["execution"]["submission_window"]
    b["rules"]["execution"]["unfilled_policy"] = "EXPIRE_END_OF_DAY"
    for f in b["features"]:
        del f["validity_window_s"]
    return b


class BookExampleTest(unittest.TestCase):
    """Book ch. 6: B_risk = 0.50, d = 0.20, c = 0.05, price 60, a 0.75 minimum commission (each way)."""

    def test_the_minimum_commission_sizes_the_trade_to_zero(self):
        fm = fee_model(per_share="0.005", minimum="0.75", cap_bps=0)
        price = F("60")
        q = risk_quantity(per_share_risk=F("0.25"), budget=F("0.50"), current=Fixed(0), want=Fixed(10), increment=Fixed(1),
                          entry_fee=lambda n: fee_envelope_of(fm, n, n * price),
                          exit_fee=lambda n: fee_envelope_of(fm, n, n * (price - F("0.20"))))
        self.assertEqual(q, Fixed(0))           # 1 share loses 0.25 + 0.75 + 0.75 = 1.75 > 0.50
        no_fees = fee_model(per_share="0", minimum="0", cap_bps=0)
        q = risk_quantity(per_share_risk=F("0.25"), budget=F("0.50"), current=Fixed(0), want=Fixed(10), increment=Fixed(1),
                          entry_fee=lambda n: fee_envelope_of(no_fees, n, n * price),
                          exit_fee=lambda n: fee_envelope_of(no_fees, n, n * price))
        self.assertEqual(q, Fixed(2))           # the uncorrected formula: floor(0.50 / 0.25)

    def test_the_largest_quantity_whose_loss_with_fees_fits_the_budget(self):
        fm = fee_model(per_share="0.005", minimum="1.00", margin_bps=2000)
        price, risk = F("10.05"), F("0.52")
        entry = lambda n: fee_envelope_of(fm, n, n * price)            # noqa: E731
        exit_ = lambda n: fee_envelope_of(fm, n, n * (price - F("0.50")))  # noqa: E731
        for budget, current in (("3.00", "0"), ("10.00", "0"), ("10.00", "4"), ("2.39", "0"), ("100", "37")):
            b, cur = F(budget), F(current)
            q = risk_quantity(per_share_risk=risk, budget=b, current=cur, want=Fixed(500), increment=Fixed(1),
                              entry_fee=entry, exit_fee=exit_)
            brute = max([n for n in range(0, 501) if (cur + n) * risk + (entry(Fixed(n)) if n else Fixed(0))
                         + exit_(cur + n) <= b], default=0)
            self.assertEqual(q, Fixed(brute), (budget, current))


class SameFeeFunctionTest(unittest.TestCase):
    def test_the_engine_schedule_as_a_core_fee_model_reserves_what_the_engine_reserves(self):
        for schedule in (IbkrFixedUS(), IbkrTieredUS()):
            for mult in ("1", "2", "0.5"):
                cm = CostModel(schedule, F("10"), F("5"), F("1"), F("1"), multiplier=F(mult))
                fm = cm.core_fee_model()
                for q in (1, 7, 199, 200, 201, 5000):
                    n, limit = Fixed(q), F("12.34")
                    core = core_fees.envelope(fm, n, Money("USD", (n * limit).rescale(6))).amount
                    self.assertEqual(core, cm.fee_envelope("BUY", n, limit), (schedule.schedule_id, mult, q))

    def test_the_risk_term_prices_fees_with_the_cores_envelope_function(self):
        calls = []
        real = core_fees.envelope

        def spy(model, quantity, notional):
            calls.append((model, quantity, notional))
            return real(model, quantity, notional)
        research_sizing.fee_function.envelope = spy
        try:
            spec = trend_spec(["FIX-RS-1"], DEV, HOLD)
            fm = fee_model(minimum="1.00", margin_bps=2000)
            risk = RiskTerm.from_spec(spec, fm)
            plans, _ = plan_orders({"FIX-RS-1": 0.45}, sizing=Sizing.from_spec(spec), limit_offset_bps=50,
                                   equity=F("1000"), marks={"FIX-RS-1": F("10.00")}, holdings={},
                                   instruments={"FIX-RS-1": fixture_instrument("FIX-RS-1")}, budget=F("1000"),
                                   fee_envelope=lambda s, q, l: Fixed(0), adv={"FIX-RS-1": Fixed(10**7)}, ledger_scale=6,
                                   risk=risk)
        finally:
            research_sizing.fee_function.envelope = real
        self.assertTrue(calls)
        self.assertTrue(all(c[0] is fm for c in calls))
        self.assertIsNotNone(plans["FIX-RS-1"].risk)

    def test_one_sizing_function_serves_the_backtest_and_the_runner(self):
        self.assertIs(engine_module.plan_orders, research_sizing.plan_orders)
        self.assertIs(runtime_sizing.plan_orders, research_sizing.plan_orders)
        self.assertIs(runtime_sizing.Holding, Holding)


class RiskTermInThePlanTest(unittest.TestCase):
    def setUp(self):
        self.spec = trend_spec(["FIX-RS-1"], DEV, HOLD)
        self.inst = {"FIX-RS-1": fixture_instrument("FIX-RS-1")}

    def plan(self, *, equity: str, holding: Holding = Holding(), risk: RiskTerm | None = "spec", target=0.45, fm=None):
        if risk == "spec":
            risk = RiskTerm.from_spec(self.spec, fm or fee_model(minimum="1.00", margin_bps=2000))
        envelope = lambda side, q, limit: (fee_envelope_of(fm or fee_model(minimum="1.00", margin_bps=2000), q, q * limit)  # noqa: E731
                                           if side == "BUY" else Fixed(0))
        sizing = Sizing(F("0.45"), F("0.95"), F("0.05"), F("0.05"), F("1"), Fixed(0))
        plans, notes = plan_orders({"FIX-RS-1": target}, sizing=sizing, limit_offset_bps=50, equity=F(equity),
                                   marks={"FIX-RS-1": F("10.00")}, holdings={"FIX-RS-1": holding}, instruments=self.inst,
                                   budget=F(equity), fee_envelope=envelope, adv={}, ledger_scale=6, risk=risk)
        return plans["FIX-RS-1"], notes

    def test_a_small_account_buys_nothing_when_fees_exceed_the_risk_budget(self):
        # 100.00 of equity: B = 3.00; one share at 10.05 loses 0.5025 + 0.011 + 1.20 + 1.20 = 2.91 <= 3.00, two lose 3.43
        p, notes = self.plan(equity="100")
        self.assertEqual((p.order.side, p.order.quantity), ("BUY", Fixed(1)))
        self.assertIn("RISK_BUDGET: buy limited 4->1", p.note)
        self.assertEqual(p.risk["budget"], "3.000000")
        self.assertEqual(notes["risk_term"]["loss_fraction_of_equity"], "0.03")
        # 80.00: B = 2.40 cannot cover even one share with a 1.20 fee each way: zero, no order
        p, _ = self.plan(equity="80")
        self.assertIsNone(p.order)
        self.assertIn("risk budget cannot cover", p.note)
        # without the risk term (a schema v1 spec) the same state buys 3 shares
        p, _ = self.plan(equity="80", risk=None)
        self.assertEqual(p.order.quantity, Fixed(3))

    def test_large_accounts_are_bounded_by_weight_and_cash_not_by_the_risk_term(self):
        p, _ = self.plan(equity="100000")
        self.assertEqual(p.order.quantity, Fixed(4500))
        self.assertNotIn("RISK_BUDGET", p.note)

    def test_the_risk_term_limits_adds_to_a_position_and_never_sells(self):
        fm = fee_model(minimum="1.00", margin_bps=2000)
        tight = RiskTerm(F("0.01"), F("0.05"), F("0.0011"), fm)          # B = 1% of equity
        # 1000.00 of equity, B = 10.00; per share 0.5025 + 0.011055; 10 held: (10 + q) x 0.513555 + 1.20 + 1.20 <= 10
        p, _ = self.plan(equity="1000", holding=Holding(owned=F("10"), unreserved=F("10")), risk=tight, fm=fm)
        self.assertEqual((p.order.side, p.order.quantity), ("BUY", Fixed(4)))     # weight wanted 35 more
        self.assertIn("RISK_BUDGET: buy limited 35->4", p.note)
        self.assertLessEqual(F(p.risk["loss_at_move"]), F("10"))
        # 20 held already lose 20 x 0.513555 + 1.20 = 11.47 > 10: no buy, and no sell (the risk term only limits additions)
        p, _ = self.plan(equity="1000", holding=Holding(owned=F("20"), unreserved=F("20")), risk=tight, fm=fm)
        self.assertIsNone(p.order)
        self.assertEqual(p.risk["max_add"], "0")

    def test_no_fee_model_means_no_buy(self):
        risk = RiskTerm(F("0.03"), F("0.05"), F("0.0011"), None)
        p, _ = self.plan(equity="100000", risk=risk)
        self.assertIsNone(p.order)
        self.assertEqual(p.risk["why"], "no fee model: entry and exit fees cannot be evaluated")


class BacktestRiskTermTest(unittest.TestCase):
    """The backtest applies the risk term through the shared sizer; a schema v1 spec keeps the legacy sizer."""

    def bars(self) -> BarSeries:
        from support.research_fixtures import business_days
        out, p = [], 1000
        for d in business_days(date(2024, 1, 2), 25):
            out.append(manual_bar("FIX-RS-1", d, f"{p // 100}.{p % 100:02d}", f"{(p + 50) // 100}.{(p + 50) % 100:02d}",
                                  f"{(p - 50) // 100}.{(p - 50) % 100:02d}", f"{p // 100}.{p % 100:02d}", 1_000_000))
            p += 20
        return BarSeries("FIX-RS-1", out)

    def run_spec(self, spec, cash: str):
        series = self.bars()
        view = DatasetView({"FIX-RS-1": series}, until=series.dates[-1], label="risk")
        cfg = BacktestConfig.from_spec(spec, F(cash))
        return run_backtest(TrendDailyV1(spec), view, {"FIX-RS-1": fixture_instrument("FIX-RS-1")}, cfg), cfg

    def test_a_small_account_is_sized_to_zero_by_fees_in_the_backtest(self):
        params = {"fast_span": "2", "slow_span": "10", "k": "10"}      # warm after 10 closes; steady uptrend -> w_max
        body2 = copy.deepcopy(trend_spec(["FIX-RS-1"], DEV, HOLD, parameters=params).body)
        body1 = v1_body()
        body1["parameters"].update(params)
        for b in (body1, body2):
            b["rules"]["sizing"]["min_order_notional"] = "1"             # a 30.00 account (TEST FIXTURE)
        v1, v2 = parse_spec(body1), parse_spec(body2)
        res2, cfg2 = self.run_spec(v2, "30")
        res1, cfg1 = self.run_spec(v1, "30")
        self.assertIsNotNone(cfg2.risk)
        self.assertIsNone(cfg1.risk)
        self.assertNotIn("risk_term", cfg1.to_json())
        # 30.00 of equity: B = 0.90 < the 1.00 minimum commission each way -> v2 never buys; v1 buys
        self.assertEqual([e for e in res2.events if e["type"] == "ORDER_CREATED"], [])
        notes = [e["plan"].get("instruments", {}).get("FIX-RS-1", "") for e in res2.events if e["type"] == "DECISION"]
        self.assertTrue(any("risk budget cannot cover" in n for n in notes), notes)
        self.assertTrue([e for e in res1.events if e["type"] == "ORDER_CREATED"])
        res2b, _ = self.run_spec(v2, "100000")
        self.assertTrue([e for e in res2b.events if e["type"] == "ORDER_CREATED"])


if __name__ == "__main__":
    unittest.main()
