"""Causal daily-bar backtester (CAP-RES-04; RES-03; DAT-04). Every bar here is a TEST FIXTURE.

Covers: fill rules (trade-through, touch, gap, partial participation, tick
snapping), causality (same-bar fills refused, execution before the decision
refused), point-in-time correctness (future reads raise, perturbing the
future changes no past decision, late publications and corrections are
invisible until available, equivalence with ``data.pit.ResearchStore.as_of``),
cash-account rules (settled cash, T+1 reuse of proceeds, no shorting), exact
money, decision events for every rebalance, determinism, a closed-form
buy-and-hold check and the corrected implementation shortfall (BR-DAT-03)."""
from __future__ import annotations

import random
import tempfile
import unittest
from datetime import date, time
from pathlib import Path

from unknown.data.pit import ResearchStore
from unknown.domain.fixed import Fixed
from unknown.research.bars import BarSeries, DatasetView, bars_as_of_pit, load_series, record_bars
from unknown.research.common import CausalityError, HoldoutError, LookaheadError
from unknown.research.costs import CostModel, IbkrFixedUS
from unknown.research.engine import (BacktestConfig, ExecutionModel, OrderRequest, Sizing, StrategyDecision,
                                     implementation_shortfall, run_backtest, simulate_fill)
from unknown.research.fixtures import fixture_instrument, manual_bar, session_times, synthetic_bars

F = Fixed.parse
A, B = "FIX-A-1", "FIX-B-1"
D = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11), date(2024, 1, 12), date(2024, 1, 15)]


def zero_cost(**kw) -> CostModel:
    base = dict(schedule=IbkrFixedUS(), spread_bps=F("0"), slippage_bps=F("0"), impact_y=F("0"), third_party_sell_bps=F("0"))
    base.update(kw)
    return CostModel(**base)


def config(cash: str = "10000", cost: CostModel | None = None, participation: str = "0.10", **kw) -> BacktestConfig:
    return BacktestConfig(initial_cash=F(cash), cost_model=cost or zero_cost(),
                          execution=ExecutionModel(participation=F(participation), limit_offset_bps=50),
                          sizing=Sizing(F("0.95"), F("0.95"), F("0.05"), F("0"), F("0")), **kw)


def flat_bars(inst: str, days=D, price: str = "50.00", volume: int = 1_000_000) -> list:
    p = F(price)
    return [manual_bar(inst, d, price, (p + 1).to_str(), (p - 1).to_str(), price, volume) for d in days]


def view(*series: list, until: date | None = None, holdout=None, grant=None) -> DatasetView:
    s = {bars[0].instrument_id: BarSeries(bars[0].instrument_id, bars) for bars in series}
    return DatasetView(s, until=until or max(b.session_date for bars in series for b in bars), label="test", holdout=holdout,
                       grant=grant)


def instruments(*ids: str) -> dict:
    return {i: fixture_instrument(i) for i in ids}


class Scripted:
    """Explicit orders per decision date; records what it could see."""
    strategy_id = "SCRIPTED"
    spec = None

    def __init__(self, script: dict | None = None, probe=None):
        self.script = script or {}
        self.probe = probe
        self.seen: list = []

    def reset(self) -> None:
        self.seen = []

    def decide(self, ctx) -> StrategyDecision:
        self.seen.append((ctx.session_date, {i: [(b.session_date, b.version, b.close.to_str()) for b in ctx.history(i)]
                                             for i in ctx.instrument_ids}, {i: ctx.revised(i) for i in ctx.instrument_ids}))
        if self.probe:
            self.probe(ctx)
        return StrategyDecision(targets=None, orders=tuple(self.script.get(ctx.session_date, ())))


def buy(inst: str, qty: str, limit: str, **kw) -> OrderRequest:
    return OrderRequest(inst, "BUY", F(qty), F(limit), **kw)


def sell(inst: str, qty: str, limit: str, **kw) -> OrderRequest:
    return OrderRequest(inst, "SELL", F(qty), F(limit), **kw)


def events(res, kind: str) -> list[dict]:
    return [e for e in res.events if e["type"] == kind]


# =================================================================== fill rules
class FillRuleTest(unittest.TestCase):
    inst = fixture_instrument(A)

    def fill(self, side, qty, limit, o, h, l, c, volume=1_000_000, cost=None, participation="1"):
        bar = manual_bar(A, D[1], o, h, l, c, volume)
        return simulate_fill(side, F(qty), F(limit), bar, 0.02, cost or zero_cost(), F(participation), self.inst)

    def test_touch_at_the_limit_is_not_a_fill(self):
        q = self.fill("BUY", "100", "50.00", "50.50", "51.00", "50.00", "50.80")
        self.assertFalse(q.filled)
        self.assertEqual(q.reason, "NOT_TRADED_THROUGH")
        q = self.fill("SELL", "100", "51.00", "50.50", "51.00", "50.00", "50.80")
        self.assertFalse(q.filled)

    def test_limit_not_reached(self):
        self.assertEqual(self.fill("BUY", "100", "49.00", "50.50", "51.00", "50.00", "50.80").reason, "LIMIT_NOT_REACHED")

    def test_trade_through_fills_at_the_limit(self):
        q = self.fill("BUY", "100", "50.00", "50.50", "51.00", "49.90", "50.20")
        self.assertTrue(q.filled)
        self.assertEqual((q.quantity, q.price, q.reference), (F("100"), F("50.00"), F("50.00")))

    def test_gap_through_the_limit_fills_from_the_open(self):
        q = self.fill("BUY", "100", "50.00", "48.00", "49.00", "47.50", "48.50")
        self.assertEqual(q.price, F("48.00"))
        q = self.fill("SELL", "100", "50.00", "52.00", "53.00", "51.50", "52.50")
        self.assertEqual(q.price, F("52.00"))

    def test_costs_worsen_the_price_snap_to_tick_and_never_cross_the_limit(self):
        cost = zero_cost(spread_bps=F("10"), slippage_bps=F("5"))  # 10 bps per side
        q = self.fill("BUY", "100", "50.00", "48.00", "49.00", "47.50", "48.50", cost=cost)
        self.assertEqual(q.price, F("48.05"))     # 48.00 * 1.001 = 48.048 -> up to the tick
        q = self.fill("SELL", "100", "50.00", "52.00", "53.00", "51.50", "52.50", cost=cost)
        self.assertEqual(q.price, F("51.94"))     # 52.00 * 0.999 = 51.948 -> down to the tick
        q = self.fill("BUY", "100", "50.00", "50.01", "50.50", "49.00", "50.20", cost=cost)
        self.assertEqual(q.price, F("50.00"))     # 50.01 * 1.001 would cross the limit
        self.assertTrue(q.limit_bound)

    def test_partial_fill_capped_by_participation_of_bar_volume(self):
        q = self.fill("BUY", "5000", "50.00", "48.00", "49.00", "47.50", "48.50", volume=20_000, participation="0.10")
        self.assertEqual(q.quantity, F("2000"))
        self.assertEqual(q.reason, "PARTIAL_PARTICIPATION")
        q = self.fill("BUY", "5", "50.00", "48.00", "49.00", "47.50", "48.50", volume=9, participation="0.10")
        self.assertFalse(q.filled)
        self.assertEqual(q.reason, "PARTICIPATION_CAP_ZERO")


# ===================================================================== causality
class CausalityTest(unittest.TestCase):
    def test_same_bar_close_fill_is_rejected_with_a_causality_error(self):
        s = Scripted({D[1]: [buy(A, "10", "50.00", execution="SAME_BAR_CLOSE")]})
        with self.assertRaises(CausalityError):
            run_backtest(s, view(flat_bars(A)), instruments(A), config())

    def test_execution_bar_starting_before_the_decision_is_refused(self):
        bars = flat_bars(A)
        bad = bars[2]
        start, _, _ = session_times(D[1])
        early = manual_bar(A, D[2], "50.00", "51.00", "49.00", "50.00")
        early = type(early)(A, D[2], start + 7 * 3600 * 10**9, early.end_ns, early.available_ns, early.open, early.high,
                            early.low, early.close, early.volume, "USD", early.source, True, 1)  # starts at D1 21:30 UTC
        bars[2] = early
        self.assertNotEqual(bad.start_ns, early.start_ns)
        s = Scripted({D[1]: [buy(A, "10", "50.00")]})
        cfg = config(decision_clock_utc=time(21, 30))
        with self.assertRaises(CausalityError):
            run_backtest(s, view(bars), instruments(A), cfg)

    def test_reading_a_future_bar_raises_lookahead(self):
        def probe(ctx):
            if ctx.session_date == D[1]:
                ctx.bar(A, D[2])
        with self.assertRaises(LookaheadError):
            run_backtest(Scripted(probe=probe), view(flat_bars(A)), instruments(A), config())

    def test_decisions_see_only_bars_available_at_the_decision_instant(self):
        s = Scripted()
        run_backtest(s, view(flat_bars(A)), instruments(A), config())
        for day, hist, _ in s.seen:
            self.assertTrue(all(d <= day for d, _, _ in hist[A]))
            self.assertEqual(hist[A][-1][0], day)   # today's close is available 21:15 < decision 21:30

    def test_a_late_publication_is_invisible_until_available(self):
        bars = flat_bars(A)
        bars[2] = manual_bar(A, D[2], "50.00", "51.00", "49.00", "50.00", available_delay_s=2 * 3600)  # 23:00 UTC
        s = Scripted()
        run_backtest(s, view(bars), instruments(A), config())
        seen = {day: [d for d, _, _ in hist[A]] for day, hist, _ in s.seen}
        self.assertNotIn(D[2], seen[D[2]])
        self.assertIn(D[2], seen[D[3]])

    def test_a_correction_is_invisible_before_its_publication_and_flags_a_revision_after(self):
        bars = flat_bars(A)
        corrected = manual_bar(A, D[1], "50.00", "51.00", "49.00", "50.75", version=2, available_delay_s=2 * 86400)
        s = Scripted()
        run_backtest(s, view(bars + [corrected]), instruments(A), config())
        by_day = {day: (hist[A], rev[A]) for day, hist, rev in s.seen}
        self.assertEqual([c for d, v, c in by_day[D[2]][0] if d == D[1]], ["50.00"])
        self.assertFalse(by_day[D[2]][1])
        self.assertEqual([c for d, v, c in by_day[D[3]][0] if d == D[1]], ["50.75"])
        self.assertTrue(by_day[D[3]][1])

    def test_perturbing_the_future_changes_no_earlier_decision(self):
        """Look-ahead proof: replace every bar after day k; all decisions and orders up to k are identical."""
        days = [b.session_date for b in synthetic_bars("random_walk", A, start=date(2023, 1, 2), sessions=160, seed=5)]
        k = 100
        base = synthetic_bars("trend", A, start=days[0], sessions=160, seed=5)
        other = synthetic_bars("mean_reverting", A, start=days[0], sessions=160, seed=99)
        mixed = base[:k + 1] + other[k + 1:]
        from unknown.research.strategies import build, trend_spec
        spec = trend_spec([A], (days[0], days[130]), (days[131], days[-1]), parameters={"fast_span": "5", "slow_span": "20"})
        cfg = BacktestConfig.from_spec(spec, F("100000"))
        r1 = run_backtest(build(spec), view(base, until=days[130], holdout=(days[131], days[-1])), instruments(A), cfg)
        r2 = run_backtest(build(spec), view(mixed, until=days[130], holdout=(days[131], days[-1])), instruments(A), cfg)

        def upto(res):
            return [e for e in res.events if e["type"] in ("DECISION", "ORDER_CREATED") and e["date"] <= days[k].isoformat()]
        self.assertGreater(len(upto(r1)), k)
        self.assertEqual(upto(r1), upto(r2))
        self.assertNotEqual(r1.event_log_hash, r2.event_log_hash)


# ================================================================ cash account
class CashAccountTest(unittest.TestCase):
    def test_unsettled_sale_proceeds_cannot_fund_a_buy_until_t_plus_1(self):
        a = [manual_bar(A, D[0], "50.00", "51.00", "49.00", "50.00"), manual_bar(A, D[1], "49.00", "50.00", "48.00", "49.50"),
             manual_bar(A, D[2], "52.00", "53.00", "51.00", "52.50")] + flat_bars(A, D[3:], "52.00")
        b = flat_bars(B, D, "52.00")
        b[2] = manual_bar(B, D[2], "51.00", "53.00", "50.00", "52.00")
        b[3] = manual_bar(B, D[3], "51.00", "53.00", "50.00", "52.00")
        script = {D[0]: [buy(A, "100", "50.00")],
                  D[1]: [sell(A, "100", "50.00"), buy(B, "100", "52.00")],
                  D[2]: [buy(B, "100", "52.00")]}
        res = run_backtest(Scripted(script), view(a, b), instruments(A, B), config("10000"))
        orders = {o["order_id"]: o for o in res.orders}
        self.assertEqual([o["status"] for o in orders.values()], ["FILLED", "FILLED", "REJECTED", "FILLED"])
        rej = events(res, "ORDER_REJECTED")[0]
        self.assertEqual(rej["reason"], "INSUFFICIENT_SETTLED_CASH")
        # 10000 - 4901 (D1 buy, settled) - 1 (the D2 sale's fee accrues at once, like the core's FEES_ACCRUED);
        # the 5200 sale proceeds are a receivable and are not spendable until D3.
        self.assertEqual((rej["required"], rej["available"]), ("5201.000000", "5098.000000"))
        # Exact money: 10000 - (4900 + 1) + (5200 - 1) - (5100 + 1)  (B bought at its 51.00 open on D3)
        marks = {e["date"]: e for e in events(res, "MARK")}
        self.assertEqual(marks[D[1].isoformat()]["settled_cash"], "10000.000000")
        self.assertEqual(marks[D[2].isoformat()]["settled_cash"], "5099.000000")
        self.assertEqual(marks[D[3].isoformat()]["settled_cash"], "10298.000000")
        self.assertEqual(marks[D[3].isoformat()]["unsettled"], "-5101.000000")
        self.assertEqual(res.final["settled_cash"], "5197.000000")
        self.assertEqual(res.final["positions"][B]["quantity"], "100")
        self.assertEqual(res.final["realized"], "300.000000")

    def test_selling_more_than_owned_is_rejected(self):
        res = run_backtest(Scripted({D[0]: [sell(A, "1", "49.00")]}), view(flat_bars(A)), instruments(A), config())
        self.assertEqual(res.orders[0]["status"], "REJECTED")
        self.assertEqual(res.orders[0]["detail"], "NO_SHORTING")

    def test_order_on_a_session_without_a_bar_expires(self):
        bars = flat_bars(A)
        del bars[1]
        res = run_backtest(Scripted({D[0]: [buy(A, "10", "50.00")]}), view(bars, flat_bars(B)), instruments(A, B), config())
        self.assertEqual(res.orders[0]["status"], "EXPIRED")
        self.assertEqual(res.orders[0]["detail"], "NO_BAR")

    def test_partial_fill_remainder_expires_end_of_day(self):
        bars = flat_bars(A, volume=500)
        res = run_backtest(Scripted({D[0]: [buy(A, "100", "50.00")]}), view(bars), instruments(A), config())
        self.assertEqual(res.orders[0]["status"], "PARTIALLY_FILLED_EXPIRED")
        self.assertEqual(res.orders[0]["filled"], "50")
        self.assertEqual(events(res, "ORDER_EXPIRED")[0]["leaves"], "50")


# ================================================================ determinism
class DeterminismTest(unittest.TestCase):
    def setUp(self):
        from unknown.research.strategies import build, trend_spec
        self.a = synthetic_bars("trend", A, start=date(2022, 1, 3), sessions=220, seed=3)
        self.b = synthetic_bars("random_walk", B, start=date(2022, 1, 3), sessions=220, seed=4)
        days = [x.session_date for x in self.a]
        self.spec = trend_spec([A, B], (days[0], days[150]), (days[151], days[-1]), parameters={"fast_span": "5", "slow_span": "20"})
        self.build = build
        self.until = days[150]
        self.holdout = (days[151], days[-1])

    def run_once(self, a, b):
        cfg = BacktestConfig.from_spec(self.spec, F("100000"))
        return run_backtest(self.build(self.spec), view(a, b, until=self.until, holdout=self.holdout), instruments(A, B), cfg)

    def test_replay_gives_a_byte_identical_event_log(self):
        r1, r2 = self.run_once(self.a, self.b), self.run_once(self.a, self.b)
        self.assertEqual(r1.event_log_hash, r2.event_log_hash)
        self.assertGreater(len(r1.fills), 5)

    def test_input_order_of_bars_does_not_matter(self):
        rng = random.Random(1)
        a, b = list(self.a), list(self.b)
        rng.shuffle(a)
        rng.shuffle(b)
        self.assertEqual(self.run_once(self.a, self.b).event_log_hash, self.run_once(a, b).event_log_hash)

    def test_every_rebalance_produces_a_decision_event(self):
        res = self.run_once(self.a, self.b)
        decisions = events(res, "DECISION")
        self.assertEqual(len(decisions), len(res.dates))
        self.assertTrue(any(d["outcome"] == "NO_TRADE" for d in decisions))
        self.assertEqual(res.no_trade_decisions, sum(1 for d in decisions if d["outcome"] == "NO_TRADE"))

    def test_holdout_is_unreadable_without_a_grant(self):
        with self.assertRaises(HoldoutError):
            view(self.a, self.b, until=self.holdout[1], holdout=self.holdout)

    def test_costs_lower_net_results_monotonically(self):
        from unknown.research.engine import BacktestConfig as C
        navs = []
        for k in ("0.5", "1", "2", "3"):
            cfg = C.from_spec(self.spec, F("100000"), cost_multiplier=F(k))
            res = run_backtest(self.build(self.spec), view(self.a, self.b, until=self.until, holdout=self.holdout),
                               instruments(A, B), cfg)
            navs.append(F(res.costs["net_pnl"]))
        self.assertEqual(navs, sorted(navs, reverse=True))


class ClosedFormTest(unittest.TestCase):
    def test_buy_and_hold_matches_the_closed_form(self):
        from unknown.research.strategies import build, buy_and_hold_spec
        bars = synthetic_bars("random_walk", A, start=date(2022, 1, 3), sessions=60, seed=8)
        days = [b.session_date for b in bars]
        spec = buy_and_hold_spec([A], (days[0], days[49]), (days[50], days[-1]))
        cfg = BacktestConfig.from_spec(spec, F("100000"))
        res = run_backtest(build(spec), view(bars, until=days[49], holdout=(days[50], days[-1])), instruments(A), cfg)
        self.assertEqual(len(res.fills), 1)
        f = res.fills[0]
        q, notional, fee = F(f["quantity"]), F(f["notional"]), F(f["fees"]["total"])
        last_close = bars[49].close
        self.assertEqual(res.nav[-1], F("100000") - notional - fee + q * last_close)
        growth = 1.0
        for r in res.returns:
            growth *= 1 + r
        self.assertAlmostEqual(growth, float(res.nav[-1].to_str()) / 100000, places=9)


class ImplementationShortfallTest(unittest.TestCase):
    def test_br_dat_03_known_answers(self):
        # Buy Q=100 at p0 10.00; 50 fill at 10.10; fees 1; unfilled marked at 10.50: IS = 5 + 1 + 25 = 31
        r = implementation_shortfall("BUY", F("100"), [(F("50"), F("10.10"))], F("1"), F("10.00"), F("10.50"))
        self.assertEqual((F(r["execution"]), F(r["fees"]), F(r["opportunity"]), F(r["total"])), (F("5"), F("1"), F("25"), F("31")))
        self.assertAlmostEqual(r["bps"], 310.0)
        # Sell mirror: fills at 9.90 cost 5; mark 9.50 costs 25 more
        r = implementation_shortfall("SELL", F("100"), [(F("50"), F("9.90"))], F("1"), F("10.00"), F("9.50"))
        self.assertEqual(F(r["total"]), F("31"))
        # Zero fill: only opportunity cost
        r = implementation_shortfall("BUY", F("100"), [], F("0"), F("10.00"), F("10.50"))
        self.assertEqual((F(r["execution"]), F(r["total"])), (F("0"), F("50")))
        # Full fill: no opportunity component
        r = implementation_shortfall("BUY", F("100"), [(F("100"), F("10.02"))], F("1"), F("10.00"), F("12.00"))
        self.assertEqual((F(r["opportunity"]), F(r["total"])), (F("0"), F("3")))


class EngineReviewTest(unittest.TestCase):
    """Regression tests for the 2026-10-22 adversarial review of the engine."""

    def crash_bars(self):
        closes = ["50.00", "50.00", "20.00", "20.00", "20.00", "20.00"]
        return [manual_bar(A, d, c, (F(c) + 1).to_str(), (F(c) - 1).to_str(), c) for d, c in zip(D, closes)]

    def test_drawdown_stop_refuses_explicit_buys_and_flattens_without_targets(self):
        script = {D[0]: [buy(A, "150", "50.50")], D[3]: [buy(A, "10", "20.50")], D[4]: [buy(A, "10", "20.50")]}
        cfg = config(participation="1", max_drawdown_stop=F("0.10"))
        res = run_backtest(Scripted(script), view(self.crash_bars()), instruments(A), cfg)   # Scripted returns targets=None
        (stop,) = events(res, "RISK_STOP")
        self.assertEqual(stop["date"], D[2].isoformat())
        later_buys = [f for f in res.fills if f["side"] == "BUY" and f["trade_date"] > stop["date"]]
        self.assertEqual(later_buys, [])
        refused = [d["plan"].get("explicit_orders_refused") for d in events(res, "DECISION") if d["date"] >= D[3].isoformat()]
        self.assertTrue(refused[0] and refused[0][0]["reason"] == "RISK_STOPPED")
        self.assertEqual(res.final["positions"][A]["quantity"], "0")             # flattened although targets were None
        self.assertTrue(any(f["side"] == "SELL" for f in res.fills))

    def test_fill_ratio_is_exact_across_quantity_scales(self):
        script = {D[0]: [buy(A, "10.00", "50.50"), buy(A, "10", "10.00")]}   # first fills, second cannot (limit far below)
        res = run_backtest(Scripted(script), view(flat_bars(A)), instruments(A), config(participation="1"))
        self.assertEqual([(o["quantity"], o["filled"]) for o in res.orders], [("10.00", "10.00"), ("10", "0")])
        self.assertEqual(res.summary()["fill_ratio"], 0.5)

    def test_declared_holding_rules_are_refused_not_ignored(self):
        from unknown.research.common import SpecError
        from unknown.research.spec import parse_spec
        from unknown.research.strategies import trend_spec
        days = [date(2023, 1, 2), date(2023, 6, 30), date(2023, 7, 3), date(2023, 12, 29)]
        body = trend_spec([A], (days[0], days[1]), (days[2], days[3])).body
        for holding in ({"min_holding_sessions": 5, "max_holding_sessions": None},
                        {"min_holding_sessions": 0, "max_holding_sessions": 30}):
            spec = parse_spec({**body, "rules": {**body["rules"], "holding": holding}})
            with self.assertRaises(SpecError):
                BacktestConfig.from_spec(spec, F("100000"))
        BacktestConfig.from_spec(parse_spec(body), F("100000"))

    def test_min_adv_blocks_new_buys_but_never_exits(self):
        from unknown.research.engine import Sizing as Sz
        thin = flat_bars(A, volume=5_000)
        sz = Sz(F("0.95"), F("0.95"), F("0.05"), F("0"), F("0"), F("10000"))

        class Target:
            strategy_id, spec = "T", None

            def __init__(self, w):
                self.w = w

            def reset(self):
                pass

            def decide(self, ctx):
                return StrategyDecision(targets={A: self.w(ctx.session_date)})
        cfg = BacktestConfig(initial_cash=F("10000"), cost_model=zero_cost(), execution=ExecutionModel(participation=F("1")),
                             sizing=sz)
        res = run_backtest(Target(lambda d: 0.5), view(thin), instruments(A), cfg)
        self.assertEqual(res.fills, [])
        self.assertIn("BELOW_MIN_ADV", events(res, "DECISION")[0]["plan"]["instruments"][A])
        liquid = flat_bars(A, volume=1_000_000)
        mixed = liquid[:2] + thin[2:]                       # liquid while entering, thin afterwards
        res = run_backtest(Target(lambda d: 0.5 if d < D[2] else 0.0), view(mixed),
                           instruments(A), BacktestConfig(initial_cash=F("10000"), cost_model=zero_cost(),
                                                          execution=ExecutionModel(participation=F("1")),
                                                          sizing=Sz(F("0.95"), F("0.95"), F("0.05"), F("0.05"), F("0"),
                                                                    F("10000"), 1)))
        self.assertEqual([f["side"] for f in res.fills], ["BUY", "SELL"])    # the exit is not blocked by thin volume
        self.assertEqual(res.final["positions"][A]["quantity"], "0")


    def test_instruments_without_a_recorded_fee_schedule_or_settlement_cycle_are_refused(self):
        """CAP-RES-05: a market without a recorded fee schedule is refused, not priced with US fees."""
        import dataclasses
        eur = dataclasses.replace(fixture_instrument(A), currency="EUR", settlement_currency="EUR")
        with self.assertRaises(ValueError):
            run_backtest(Scripted(), view(flat_bars(A)), {A: eur}, config())
        t2 = dataclasses.replace(fixture_instrument(A), settlement_days=2)
        with self.assertRaises(ValueError):
            run_backtest(Scripted(), view(flat_bars(A)), {A: t2}, config())

    def test_a_fixture_recipe_regenerates_non_default_generators(self):
        from unknown.research.fixtures import from_recipe, recipe
        kw = dict(start=date(2022, 1, 3), sessions=30, seed=4, start_price=20.0)
        bars = synthetic_bars("random_walk", A, sigma=0.05, mean_volume=5_000.0, **kw)
        r = recipe("random_walk", A, sigma=0.05, mean_volume=5_000.0, **kw)
        self.assertEqual([b.to_json() for b in from_recipe(r)], [b.to_json() for b in bars])


class PointInTimeStoreBridgeTest(unittest.TestCase):
    def test_ten_or_more_versions_agree_with_the_store(self):
        """Review fix: the store's string tie-break on publication id must order v10 after v9."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp) / "pit.sqlite")
            versions = [manual_bar(A, D[0], "50.00", "51.00", "49.00", f"{50 + v / 100:.2f}", version=v, available_delay_s=3600)
                        for v in range(1, 12)]
            record_bars(store, versions, "FIXTURE.test", known_at_ns=1)
            t = versions[0].available_ns
            self.assertEqual(BarSeries(A, versions).as_of(t)[0].version, 11)
            self.assertEqual(bars_as_of_pit(store, "FIXTURE.test", A, t)[0].version, 11)
            store.conn.close()

    def test_bar_series_as_of_equals_research_store_as_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp) / "pit.sqlite")
            bars = synthetic_bars("trend", A, start=date(2024, 1, 2), sessions=15, seed=2)
            corr = manual_bar(A, bars[5].session_date, "60.00", "61.00", "59.00", "60.50", version=2, available_delay_s=3 * 86400)
            late = manual_bar(B, bars[3].session_date, "10.00", "10.50", "9.50", "10.25", available_delay_s=5 * 3600)
            record_bars(store, bars + [corr], "FIXTURE.test", known_at_ns=1)
            record_bars(store, [late], "FIXTURE.test", known_at_ns=1)
            series = BarSeries(A, bars + [corr])
            checkpoints = sorted({b.available_ns + d for b in bars + [corr] for d in (-1, 0, 1)})
            for t in checkpoints:
                self.assertEqual([x.to_json() for x in bars_as_of_pit(store, "FIXTURE.test", A, t)],
                                 [x.to_json() for x in series.as_of(t)], f"as_of disagreement at {t}")
            loaded = load_series(store, "FIXTURE.test", [A, B], cutoff_ns=2**62)
            self.assertEqual(loaded[A].fingerprint(), series.fingerprint())
            self.assertEqual(len(loaded[B]), 1)
            # A backtest on bars loaded from the store is the same backtest.
            cfg = config()
            r1 = run_backtest(Scripted({bars[2].session_date: [buy(A, "10", "99.00")]}),
                              DatasetView({A: series}, until=bars[-1].session_date, label="x"), instruments(A), cfg)
            r2 = run_backtest(Scripted({bars[2].session_date: [buy(A, "10", "99.00")]}),
                              DatasetView({A: loaded[A]}, until=bars[-1].session_date, label="x"), instruments(A), cfg)
            self.assertEqual(r1.event_log_hash, r2.event_log_hash)
            store.conn.close()


if __name__ == "__main__":
    unittest.main()
