"""TREND_DAILY_V1 (book ch. 15, eq. 15.1) and its baselines (buy-and-hold, volatility-capped passive).

These are research candidates, not recommendations to trade (book ch. 15).
Each strategy is constructed from a validated ``StrategySpec``; its parameters
come only from the spec, so the spec hash identifies the exact rule set.

TREND_DAILY_V1 per instrument, on completed daily closes visible at the
decision instant:

* EMA by span (``ema.span@1``): a = 2 / (span + 1), initialized at the first close;
* sigma from the EWMA of squared log returns with half-life H (``eq5.2.ewma_var@1``),
  initialized at the first squared return;
* z = (EMA_f(log P) - EMA_s(log P)) / (sigma sqrt(h) + eps)  (``eq15.1@1``);
* w = min(w_max, k max(0, z)); zero until ``slow_span`` closes have been seen
  (warm-up, recorded in the decision notes).

The engine's A15 mapper then applies the per-name cap, the aggregate cap with
cash reserve (BR-DAT-15) and the no-trade band, and creates price-bounded DAY
limit orders for the next session.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from .bars import Bar
from .common import SpecError, fixed_to_float
from .engine import DecisionContext, StrategyDecision
from .spec import StrategySpec, parse_spec


# ------------------------------------------------------------- features
@dataclass
class TrendState:
    fast_a: float
    slow_a: float
    lam: float
    n: int = 0
    ema_f: float = 0.0
    ema_s: float = 0.0
    var: float | None = None
    last_log: float | None = None

    def update(self, close: float) -> None:
        x = math.log(close)
        if self.n == 0:
            self.ema_f = self.ema_s = x
        else:
            self.ema_f = (1 - self.fast_a) * self.ema_f + self.fast_a * x
            self.ema_s = (1 - self.slow_a) * self.ema_s + self.slow_a * x
            r2 = (x - self.last_log) ** 2
            self.var = r2 if self.var is None else self.lam * self.var + (1 - self.lam) * r2
        self.last_log = x
        self.n += 1


def trend_score(closes: list[float], *, fast_span: int, slow_span: int, vol_half_life: int, horizon_h: int,
                epsilon: float) -> dict:
    """Pure eq. 15.1 on a close series (known-answer tests use this directly)."""
    st = TrendState(2.0 / (fast_span + 1), 2.0 / (slow_span + 1), 2.0 ** (-1.0 / vol_half_life))
    for c in closes:
        st.update(c)
    return _score(st, horizon_h, epsilon)


def _score(st: TrendState, h: int, eps: float) -> dict:
    sigma = math.sqrt(st.var) if st.var is not None else None
    if sigma is None:
        return {"z": None, "ema_f": st.ema_f, "ema_s": st.ema_s, "sigma": None, "n": st.n}
    z = (st.ema_f - st.ema_s) / (sigma * math.sqrt(h) + eps)
    return {"z": z, "ema_f": st.ema_f, "ema_s": st.ema_s, "sigma": sigma, "n": st.n}


class TrendDailyV1:
    strategy_id = "TREND_DAILY_V1"
    # The feature versions this implementation computes (checked against a v2 spec's contract; BR-DAT-18).
    FEATURES = {"ema_spread_log_price": 1, "ewma_vol": 1}
    STATELESS_TARGETS = True          # targets depend only on the visible bars, never on positions or equity

    def __init__(self, spec: StrategySpec):
        if spec.strategy_id != self.strategy_id:
            raise SpecError(f"spec {spec.strategy_id} is not {self.strategy_id}")
        self.spec = spec
        self.fast = spec.param_int("fast_span")
        self.slow = spec.param_int("slow_span")
        if not self.fast < self.slow:
            raise SpecError("TREND_DAILY_V1 requires fast_span < slow_span (book ch. 15)")
        self.half_life = spec.param_int("vol_half_life")
        self.h = spec.param_int("horizon_h")
        self.k = fixed_to_float(spec.param("k"))
        self.w_max = fixed_to_float(spec.param("w_max"))
        self.eps = fixed_to_float(spec.param("epsilon"))
        self.state: dict[str, TrendState] = {}

    def _new_state(self) -> TrendState:
        return TrendState(2.0 / (self.fast + 1), 2.0 / (self.slow + 1), 2.0 ** (-1.0 / self.half_life))

    def reset(self) -> None:
        self.state = {}

    def feature_values(self, instrument_id: str) -> dict[str, float | None]:
        """The contract's features after the last decision: the EMA spread of log prices and the EWMA volatility."""
        st = self.state.get(instrument_id)
        if st is None or st.n == 0:
            return {"ema_spread_log_price": None, "ewma_vol": None}
        return {"ema_spread_log_price": st.ema_f - st.ema_s, "ewma_vol": math.sqrt(st.var) if st.var is not None else None}

    def feature_warm_up(self, feature_id: str) -> int | None:
        """Observations before the feature is usable: the slow span for the spread (the score's warm-up), two closes
        for the volatility (one return)."""
        return {"ema_spread_log_price": self.slow, "ewma_vol": 2}.get(feature_id)

    def decide(self, ctx: DecisionContext) -> StrategyDecision:
        targets: dict[str, float] = {}
        notes: dict[str, dict] = {}
        for inst in ctx.instrument_ids:
            st = self.state.get(inst)
            if st is None or ctx.revised(inst):
                st = self._new_state()
                bars: list[Bar] = ctx.history(inst)
            else:
                bars = ctx.new_bars(inst)
            for b in bars:
                st.update(fixed_to_float(b.close))
            self.state[inst] = st
            sc = _score(st, self.h, self.eps)
            if st.n < self.slow or sc["z"] is None:
                targets[inst] = 0.0
                notes[inst] = {"state": "WARMING", "observations": st.n, "required": self.slow}
                continue
            w = min(self.w_max, self.k * max(0.0, sc["z"]))
            targets[inst] = w
            notes[inst] = {"z": sc["z"], "sigma": sc["sigma"], "ema_f": sc["ema_f"], "ema_s": sc["ema_s"], "w": w,
                           "formula": "eq15.1@1"}
        return StrategyDecision(targets=targets, notes=notes)


class BuyAndHold:
    """Baseline: buy an equal share of ``weight`` once, then hold (no rebalancing)."""
    strategy_id = "BUY_AND_HOLD"
    FEATURES = {"close": 1}
    STATELESS_TARGETS = False         # targets depend on the positions held

    def __init__(self, spec: StrategySpec):
        if spec.strategy_id != self.strategy_id:
            raise SpecError(f"spec {spec.strategy_id} is not {self.strategy_id}")
        self.spec = spec
        self.weight = fixed_to_float(spec.param("weight"))
        self.latest: dict[str, float] = {}

    def reset(self) -> None:
        self.latest: dict[str, float] = {}

    def feature_values(self, instrument_id: str) -> dict[str, float | None]:
        return {"close": self.latest.get(instrument_id)}

    def decide(self, ctx: DecisionContext) -> StrategyDecision:
        n = len(ctx.instrument_ids)
        self.latest = {i: fixed_to_float(ctx.latest(i).close) for i in ctx.instrument_ids if ctx.latest(i) is not None}
        want = {}
        for inst in ctx.instrument_ids:
            if ctx.position(inst).is_positive() or ctx.latest(inst) is None:
                continue
            want[inst] = self.weight / n
        if not want:
            return StrategyDecision(targets=None, notes={"state": "HOLD"})
        # Keep current holdings at their (drifted) weight: only instruments without a position get targets.
        return StrategyDecision(targets=want, notes={"state": "ESTABLISHING", "instruments": sorted(want)})


class VolCappedPassive:
    """Baseline: passive long with weight min(w_cap, sigma_target / sigma_hat) / N, rebalanced through the band."""
    strategy_id = "VOL_CAPPED_PASSIVE"
    FEATURES = {"ewma_vol": 1}
    STATELESS_TARGETS = True

    def __init__(self, spec: StrategySpec):
        if spec.strategy_id != self.strategy_id:
            raise SpecError(f"spec {spec.strategy_id} is not {self.strategy_id}")
        self.spec = spec
        self.w_cap = fixed_to_float(spec.param("w_cap"))
        self.sigma_target = fixed_to_float(spec.param("sigma_target"))
        self.half_life = spec.param_int("vol_half_life")
        self.state: dict[str, TrendState] = {}

    def reset(self) -> None:
        self.state = {}

    def feature_values(self, instrument_id: str) -> dict[str, float | None]:
        st = self.state.get(instrument_id)
        return {"ewma_vol": math.sqrt(st.var) if st is not None and st.var is not None else None}

    def feature_warm_up(self, feature_id: str) -> int | None:
        return {"ewma_vol": self.half_life}.get(feature_id)

    def decide(self, ctx: DecisionContext) -> StrategyDecision:
        n = len(ctx.instrument_ids)
        targets, notes = {}, {}
        for inst in ctx.instrument_ids:
            st = self.state.get(inst)
            if st is None or ctx.revised(inst):
                st = TrendState(0.5, 0.5, 2.0 ** (-1.0 / self.half_life))
                bars = ctx.history(inst)
            else:
                bars = ctx.new_bars(inst)
            for b in bars:
                st.update(fixed_to_float(b.close))
            self.state[inst] = st
            if st.var is None or st.n < self.half_life or st.var <= 0:
                targets[inst] = 0.0
                notes[inst] = {"state": "WARMING", "observations": st.n}
                continue
            sigma = math.sqrt(st.var)
            w = min(self.w_cap, self.sigma_target / sigma) / n
            targets[inst] = w
            notes[inst] = {"sigma": sigma, "w": w, "formula": "eq5.2.ewma_var@1"}
        return StrategyDecision(targets=targets, notes=notes)


STRATEGIES = {c.strategy_id: c for c in (TrendDailyV1, BuyAndHold, VolCappedPassive)}


def build(spec: StrategySpec):
    """The implementation of a spec. For a schema v2 spec, the implementation's feature ids and versions must be the
    spec's approved feature contract (BR-DAT-18, ``features.check_contract``), in the backtest and at runtime."""
    try:
        cls = STRATEGIES[spec.strategy_id]
    except KeyError:
        raise SpecError(f"no implementation registered for {spec.strategy_id}") from None
    implementation = cls(spec)
    from .features import check_contract
    check_contract(spec, implementation)
    return implementation


# ------------------------------------------------------------ spec builders
def _common(strategy_id: str, family: str, title: str, instrument_ids: list[str], development: tuple[date, date],
            holdout: tuple[date, date], *, author: str, dataset_id: str, fixture: bool) -> dict:
    return {
        "schema_version": 2, "strategy_id": strategy_id, "version": 1, "family": family, "title": title, "author": author,
        "data_requirements": [{"dataset_id": dataset_id, "fields": ["open", "high", "low", "close", "volume"],
                               "frequency": "DAILY", "point_in_time": True,
                               "rights_ref": "FIXTURE: synthetic data generated in-repo; no third-party rights" if fixture
                               else "see provider registry entry for the dataset", "fixture_allowed": fixture}],
        "universe": {"rule": "explicit instrument list fixed before evaluation", "instrument_ids": list(instrument_ids),
                     "point_in_time_membership": False,
                     "survivorship_note": "explicit synthetic list: survivorship is not applicable to fixtures; a real "
                                          "universe must come from point-in-time membership (CAP-RES-02)"},
        "costs": {"fee_schedule_id": "fee.ibkr.us.fixed@2026-10-22", "spread_bps": "10", "slippage_bps": "5",
                  "impact_y_values": ["0.5", "1.0", "2.0"], "impact_y_default": "1.0", "third_party_sell_bps": "1.0",
                  "expected_cost_bps_per_unit_turnover": "25"},
        "capacity": {"method": "rerun at scaled capital and report net utility (CAP-RES-05); participation cap binds first",
                     "estimate": "100000", "currency": "USD"},
        "liquidity": {"min_adv_shares": "100000", "max_participation": "0.10"},
        "operational_constraints": ["cash account: no borrowing, no shorting, T+1 settlement before proceeds are reused",
                                    "one decision per session after the official close is available",
                                    "orders are DAY limits for the next regular session; unfilled remainder expires"],
        "risk_limits": {"max_gross_exposure": "0.95", "max_single_name_weight": "0.95", "max_drawdown_stop": "0.35",
                        "long_only": True, "allow_short": False, "allow_margin": False},
        "validation_plan": {"development": {"start": development[0].isoformat(), "end": development[1].isoformat()},
                            "untouched_holdout": {"start": holdout[0].isoformat(), "end": holdout[1].isoformat()},
                            "walk_forward": {"train_sessions": 250, "test_sessions": 60, "expanding": False,
                                             "label_horizon_sessions": 1, "purge_sessions": 2, "embargo_sessions": 0},
                            "selection_metric": "sharpe.per_period@1"},
        "promotion_evidence": ["REPLAY_EXACT", "CORE_REPLAY_MATCH", "WALK_FORWARD_OOS", "PAIRED_BOOTSTRAP", "HOLDOUT",
                               "COST_SENSITIVITY", "STRESS", "DSR", "PBO", "N_EFF"],
        "stress_plan": [
            {"id": "costs_x3", "description": "all costs tripled", "kind": "COST_MULTIPLIER", "value": "3"},
            {"id": "participation_quarter", "description": "participation cap at 25% of plan", "kind": "PARTICIPATION",
             "value": "0.25"},
            {"id": "impact_y_max", "description": "largest impact coefficient", "kind": "IMPACT_Y", "value": "2.0"},
            {"id": "limit_tight", "description": "limit offset 10 bps (fewer fills)", "kind": "LIMIT_OFFSET", "value": "10"},
        ],
    }


def _execution(offset_bps: int = 50) -> dict:
    """BR-DAT-19: decide after the official close; submit in the next regular session between open + 15 and
    close - 15 minutes (no auctions); at transmission the limit is min(model bound, the quote mid + 50 bps) for a buy
    (max(model bound, mid - 50 bps) for a sell); the remainder is cancelled at the window's end, never carried over."""
    return {"decision_clock_utc": "21:30", "order_type": "LIMIT", "time_in_force": "DAY", "limit_offset_bps": offset_bps,
            "price_bound_source": "model bound: latest visible close +/- limit_offset_bps; at transmission bounded by a "
                                  "collar around a fresh quote mid (the backtest uses the session open: daily bars "
                                  "carry no quote; labelled)",
            "execution_delay_sessions": 1, "participation_cap": "0.10", "same_bar_fill": False,
            "unfilled_policy": "CANCEL_AT_WINDOW_END",
            "submission_window": {"open_offset_minutes": 15, "close_offset_minutes": 15, "auctions": False,
                                  "limit_reference": "TRANSMISSION_QUOTE_MID", "collar_bps": 50}}


# BR-DAT-04 risk budgets. TREND_DAILY_V1: a 5% adverse move on a position, fees in and out included, may cost at most
# 3% of equity (non-binding at the 0.45 per-name cap for ordinary equity: the fees decide only for very small
# accounts). The passive baselines hold up to 95% and take the whole move: their budget is the whole equity.
TREND_RISK_BUDGET = {"loss_fraction_of_equity": "0.03", "adverse_move_fraction": "0.05"}
BASELINE_RISK_BUDGET = {"loss_fraction_of_equity": "1", "adverse_move_fraction": "0.05"}
# BR-DAT-18: a daily feature value is usable for four days after its newest close became available, which covers a
# weekend plus a holiday between the decision and the end of the next session's submission window.
DAILY_VALIDITY_S = 4 * 86400


def trend_spec(instrument_ids: list[str], development: tuple[date, date], holdout: tuple[date, date], *,
               author: str = "research.owner", dataset_id: str = "FIXTURE.synthetic_daily_bars.v1", fixture: bool = True,
               parameters: dict[str, str] | None = None, grid: dict[str, list[str]] | None = None) -> StrategySpec:
    body = _common("TREND_DAILY_V1", "TREND", "Daily EMA trend, long-only, capped (book ch. 15)", instrument_ids,
                   development, holdout, author=author, dataset_id=dataset_id, fixture=fixture)
    body.update({
        "hypothesis": {"statement": "Persistent drift regimes make a volatility-normalized EMA spread of log prices "
                                    "positively related to next-session returns, enough to beat buy-and-hold net of costs.",
                       "mechanism": "slow information diffusion and herding produce autocorrelated drift (research hypothesis)",
                       "market_conditions": ["persistent directional regimes", "liquid long-only cash equities/ETFs",
                                             "fails in choppy, mean-reverting markets"],
                       "expected_effect": "positive paired utility difference versus buy-and-hold over the untouched holdout"},
        "equations": [{"id": "trend_score", "expression": "z_t = (EMA_f(log P)_t - EMA_s(log P)_t) / (sigma_t sqrt(h) + eps)",
                       "book_ref": "ch. 15 eq. 15.1", "formula_id": "eq15.1@1"},
                      {"id": "volatility", "expression": "v_t = lambda v_{t-1} + (1 - lambda) r_t^2", "book_ref": "ch. 5 eq. 5.2",
                       "formula_id": "eq5.2.ewma_var@1"}],
        "variables": [{"symbol": "P_t", "description": "completed daily close visible at the decision", "unit": "USD/share", "kind": "input"},
                      {"symbol": "f", "description": "fast EMA span", "unit": "sessions", "kind": "parameter"},
                      {"symbol": "s", "description": "slow EMA span", "unit": "sessions", "kind": "parameter"},
                      {"symbol": "H", "description": "volatility half-life", "unit": "sessions", "kind": "parameter"},
                      {"symbol": "h", "description": "normalization horizon", "unit": "sessions", "kind": "parameter"},
                      {"symbol": "k", "description": "weight per unit score", "unit": "fraction per unit z", "kind": "parameter"},
                      {"symbol": "w_max", "description": "per-name weight cap", "unit": "fraction of marked equity", "kind": "parameter"},
                      {"symbol": "eps", "description": "denominator floor", "unit": "log-price units", "kind": "parameter"},
                      {"symbol": "sigma_t", "description": "EWMA daily log-return volatility", "unit": "per session", "kind": "derived"},
                      {"symbol": "z_t", "description": "trend score", "unit": "dimensionless", "kind": "derived"},
                      {"symbol": "w_t", "description": "target weight", "unit": "fraction of marked equity", "kind": "output"}],
        "assumptions": ["daily closes are final once available (corrections are handled as new versions)",
                        "no corporate actions in the fixture window (BR-DAT-11 applies to real data)",
                        "the denominator is a chosen normalization, not a distributional claim (book ch. 15)"],
        "features": [{"id": "ema_spread_log_price", "version": 1, "formula_id": "ema.span@1", "inputs": ["close"],
                      "unit": "log-price", "availability_basis": "BAR_CLOSE_PLUS_LAG", "freshness_budget_s": 4 * 86400,
                      "warm_up_observations": 250, "causal": True,  # upper bound: the slow_span domain maximum
                      "validity_window_s": DAILY_VALIDITY_S},
                     {"id": "ewma_vol", "version": 1, "formula_id": "eq5.2.ewma_var@1", "inputs": ["close"],
                      "unit": "per session", "availability_basis": "BAR_CLOSE_PLUS_LAG", "freshness_budget_s": 4 * 86400,
                      "warm_up_observations": 2, "causal": True, "validity_window_s": DAILY_VALIDITY_S}],
        "rules": {"entry": "target weight w_t > 0 after warm-up",
                  "exit": "target weight 0 (score <= 0) exits fully; drawdown stop flattens and blocks entries",
                  "sizing": {"method": "TARGET_WEIGHT", "max_weight": "0.45", "aggregate_cap": "0.95", "cash_reserve": "0.05",
                             "no_trade_band": "0.05", "min_order_notional": "100", "risk_budget": dict(TREND_RISK_BUDGET)},
                  "holding": {"min_holding_sessions": 0, "max_holding_sessions": None},
                  "execution": _execution()},
        "failure_conditions": ["relies on same-close fills (refused by the engine)",
                               "edge disappears at 2x costs", "holdout paired lower bound below the hurdle",
                               "PBO above the protocol maximum", "drawdown beyond max_drawdown_stop"],
        "baselines": [{"id": "BUY_AND_HOLD", "description": "equal-weight buy-and-hold of the same universe"},
                      {"id": "VOL_CAPPED_PASSIVE", "description": "passive long sized by min(cap, sigma_target / sigma_hat)"}],
        "sensitivity_plan": {"parameter_grid": grid or {"fast_span": ["5", "10", "20"], "slow_span": ["40", "80"]},
                             "cost_multipliers": ["0.5", "1", "2", "3"]},
        "parameter_domain": {"fast_span": {"kind": "INTEGER", "min": "2", "max": "60"},
                             "slow_span": {"kind": "INTEGER", "min": "10", "max": "250"},
                             "vol_half_life": {"kind": "INTEGER", "min": "5", "max": "120"},
                             "horizon_h": {"kind": "INTEGER", "min": "1", "max": "60"},
                             "k": {"kind": "DECIMAL", "min": "0.01", "max": "10"},
                             "w_max": {"kind": "DECIMAL", "min": "0.01", "max": "0.95"},
                             "epsilon": {"kind": "DECIMAL", "min": "0.0000001", "max": "0.1"}},
        "parameters": {"fast_span": "10", "slow_span": "40", "vol_half_life": "20", "horizon_h": "10", "k": "0.5",
                       "w_max": "0.45", "epsilon": "0.0001", **(parameters or {})},
    })
    return parse_spec(body)


def buy_and_hold_spec(instrument_ids: list[str], development: tuple[date, date], holdout: tuple[date, date], *,
                      author: str = "research.owner", dataset_id: str = "FIXTURE.synthetic_daily_bars.v1",
                      fixture: bool = True) -> StrategySpec:
    body = _common("BUY_AND_HOLD", "BASELINE", "Equal-weight buy-and-hold baseline", instrument_ids, development, holdout,
                   author=author, dataset_id=dataset_id, fixture=fixture)
    body.update({
        "hypothesis": {"statement": "Passive exposure is the incumbent any active candidate must beat net of costs.",
                       "mechanism": "baseline (book ch. 7: compare against relevant passive exposure and cash)",
                       "market_conditions": ["all"], "expected_effect": "reference performance"},
        "equations": [{"id": "weights", "expression": "w_i = weight / N once; then hold", "book_ref": "ch. 15 worked candidate",
                       "formula_id": "nav@1"}],
        "variables": [{"symbol": "weight", "description": "total invested fraction", "unit": "fraction", "kind": "parameter"}],
        "assumptions": ["no rebalancing after the initial purchase"],
        "features": [{"id": "close", "version": 1, "formula_id": "nav@1", "inputs": ["close"], "unit": "USD/share",
                      "availability_basis": "BAR_CLOSE_PLUS_LAG", "freshness_budget_s": 4 * 86400,
                      "warm_up_observations": 1, "causal": True, "validity_window_s": DAILY_VALIDITY_S}],
        "rules": {"entry": "first session with visible data", "exit": "none (hold)",
                  "sizing": {"method": "TARGET_WEIGHT", "max_weight": "0.95", "aggregate_cap": "0.95", "cash_reserve": "0.05",
                             "no_trade_band": "0", "min_order_notional": "0", "risk_budget": dict(BASELINE_RISK_BUDGET)},
                  "holding": {"min_holding_sessions": 0, "max_holding_sessions": None}, "execution": _execution()},
        "failure_conditions": ["not applicable: baseline"],
        "baselines": [{"id": "CASH", "description": "uninvested cash (zero return)"}],
        "sensitivity_plan": {"parameter_grid": {"weight": ["0.95"]}, "cost_multipliers": ["0.5", "1", "2", "3"]},
        "parameter_domain": {"weight": {"kind": "DECIMAL", "min": "0.01", "max": "0.95"}},
        "parameters": {"weight": "0.95"},
    })
    body["risk_limits"]["max_drawdown_stop"] = "0.99"
    return parse_spec(body)


def vol_capped_spec(instrument_ids: list[str], development: tuple[date, date], holdout: tuple[date, date], *,
                    author: str = "research.owner", dataset_id: str = "FIXTURE.synthetic_daily_bars.v1",
                    fixture: bool = True) -> StrategySpec:
    body = _common("VOL_CAPPED_PASSIVE", "BASELINE", "Volatility-capped passive baseline", instrument_ids, development,
                   holdout, author=author, dataset_id=dataset_id, fixture=fixture)
    body.update({
        "hypothesis": {"statement": "Passive exposure scaled down in high volatility is a stricter passive incumbent.",
                       "mechanism": "baseline (book ch. 15: volatility-capped passive baseline)",
                       "market_conditions": ["all"], "expected_effect": "reference performance"},
        "equations": [{"id": "weights", "expression": "w_i = min(w_cap, sigma_target / sigma_i) / N",
                       "book_ref": "ch. 15 worked candidate", "formula_id": "eq5.2.ewma_var@1"}],
        "variables": [{"symbol": "w_cap", "description": "maximum total weight", "unit": "fraction", "kind": "parameter"},
                      {"symbol": "sigma_target", "description": "target daily volatility", "unit": "per session", "kind": "parameter"},
                      {"symbol": "H", "description": "volatility half-life", "unit": "sessions", "kind": "parameter"}],
        "assumptions": ["volatility from the EWMA of squared log returns (zero-mean approximation)"],
        "features": [{"id": "ewma_vol", "version": 1, "formula_id": "eq5.2.ewma_var@1", "inputs": ["close"],
                      "unit": "per session", "availability_basis": "BAR_CLOSE_PLUS_LAG", "freshness_budget_s": 4 * 86400,
                      # upper bound: the vol_half_life domain maximum (the implementation warms up for half_life closes)
                      "warm_up_observations": 120, "causal": True, "validity_window_s": DAILY_VALIDITY_S}],
        "rules": {"entry": "after volatility warm-up", "exit": "weight falls through the band",
                  "sizing": {"method": "TARGET_WEIGHT", "max_weight": "0.95", "aggregate_cap": "0.95", "cash_reserve": "0.05",
                             "no_trade_band": "0.05", "min_order_notional": "100", "risk_budget": dict(BASELINE_RISK_BUDGET)},
                  "holding": {"min_holding_sessions": 0, "max_holding_sessions": None}, "execution": _execution()},
        "failure_conditions": ["not applicable: baseline"],
        "baselines": [{"id": "BUY_AND_HOLD", "description": "unscaled passive"}],
        "sensitivity_plan": {"parameter_grid": {"sigma_target": ["0.01"]}, "cost_multipliers": ["0.5", "1", "2", "3"]},
        "parameter_domain": {"w_cap": {"kind": "DECIMAL", "min": "0.01", "max": "0.95"},
                             "sigma_target": {"kind": "DECIMAL", "min": "0.001", "max": "0.1"},
                             "vol_half_life": {"kind": "INTEGER", "min": "5", "max": "120"}},
        "parameters": {"w_cap": "0.95", "sigma_target": "0.01", "vol_half_life": "20"},
    })
    body["risk_limits"]["max_drawdown_stop"] = "0.99"
    return parse_spec(body)
