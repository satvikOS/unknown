"""Causal event-driven daily-bar backtester (CAP-RES-04; RES-03, RES-05; book ch. 7, A15).

Timeline of one session date ``d`` (all instants UTC):

1. **Settlement** at the start of ``d``: every unsettled amount whose
   settlement date is on or before ``d`` moves into settled cash.
2. **Submission and execution** of the orders decided at the previous
   decision. Each order is checked against the cash-account rules at
   submission (the reservation): a buy needs ``limit x qty + fee envelope``
   within settled cash minus unsettled payables and accrued fees (sale
   proceeds are NOT spendable until they settle, T+1); a sell needs owned,
   unreserved quantity (no shorting). A rejected order is logged with a
   reason code. An accepted order executes only against the instrument's bar
   for ``d``, which must start after the decision instant (else
   ``CausalityError``).
3. **Mark** at the latest bar visible at the decision instant.
4. **Decision** at ``d`` + decision clock: the strategy sees only bars whose
   ``available_ns`` <= the decision instant. Every decision is recorded,
   including no-trade decisions. Target weights are mapped to price-bounded
   limit orders for the next session (algorithm A15). New buys are skipped
   when the mean visible volume of the last ``adv_window`` bars is below the
   spec's ``liquidity.min_adv_shares``. Once the drawdown stop fires, every
   instrument is targeted to zero and explicit BUY orders are refused.
   Holding-period rules are not implemented: a spec that declares one is
   refused by ``BacktestConfig.from_spec`` instead of being ignored.

Fill rules (daily bars cannot justify more precision; book ch. 7):

* No fill unless the bar trades THROUGH the limit: buy needs ``low < limit``,
  sell needs ``high > limit``. A touch at the limit is not a fill.
* Gap handling: a buy whose session opens at or below the limit executes from
  the open; otherwise from the limit (the price traded down through it). Sells
  mirror this.
* Price costs (half-spread + slippage + impact ``Y sigma sqrt(Q/V)``) worsen
  the price, which is snapped to the tick against the trader and never crosses
  the limit.
* Partial fills: at most ``participation x bar volume`` shares; the remainder
  expires at the end of the day (TIF DAY, never carried over; BR-DAT-19).

Money is exact: cash, unsettled amounts, lots, fees and prices are ``Fixed``
at the USD ledger scale. FIFO lot disposal and its HALF_EVEN cost split mirror
the core ledger exactly so ``core_replay`` can compare to the cent. Returns
and other statistics are floats derived from the exact NAV series.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Protocol

from ..domain.calendar import WEEKDAYS_ONLY, BusinessCalendar
from ..domain.fixed import Fixed, Rounding
from ..domain.instruments import Instrument
from .bars import Bar, Cursor, DatasetView
from .common import NS_PER_S, CausalityError, SpecError, fixed_to_float, stable_hash
from .costs import LEDGER_SCALE, CostModel, PriceCost
from .execution import BACKTEST_REFERENCE, SubmissionWindow, transmission_limit, window_times
from .sizing import WEIGHT_SCALE, Holding, RiskTerm, plan_orders

SIMULATOR_VERSION = "unknown.research.engine.daily@1"
ZERO6 = Fixed(0, LEDGER_SCALE)
ONE = Fixed(1)


# ===================================================================== config
@dataclass(frozen=True)
class ExecutionModel:
    participation: Fixed = Fixed.parse("0.10")     # max fraction of the execution bar's volume
    limit_offset_bps: int = 50                     # buy limit = ref * (1 + bps), sell limit = ref * (1 - bps)
    latency_s: int = 60                            # legacy (no window): submission = session start + latency
    execution_delay_sessions: int = 1
    # Schema v2 (BR-DAT-19): submit inside [open + N, close - M], limit tightened at transmission, remainder cancelled
    # at the window's end (``research.execution``). None: the legacy rule of schema v1 specs.
    window: SubmissionWindow | None = None

    def __post_init__(self) -> None:
        if not (self.participation.is_positive() and self.participation <= ONE):
            raise ValueError("participation must be in (0, 1]")
        if self.execution_delay_sessions != 1:
            raise ValueError("daily engine v1 supports execution in the next session only")

    def to_json(self) -> dict:
        if self.window is None:
            return {"participation": self.participation.to_str(), "limit_offset_bps": self.limit_offset_bps,
                    "latency_s": self.latency_s, "execution_delay_sessions": self.execution_delay_sessions,
                    "fill_rule": "trade-through required (buy low<limit, sell high>limit); open-gap execution; "
                                 "price costs never cross the limit; partial fills capped by participation; DAY expiry"}
        return {"participation": self.participation.to_str(), "limit_offset_bps": self.limit_offset_bps,
                "execution_delay_sessions": self.execution_delay_sessions, "submission_window": self.window.to_json(),
                "transmission_reference": BACKTEST_REFERENCE,
                "fill_rule": "submitted at open + N; limit = min(model bound, collar around the reference) (max for sells); "
                             "trade-through required (buy low<limit, sell high>limit); open-gap execution; price costs "
                             "never cross the limit; partial fills capped by participation of the window's share of the "
                             "bar volume (uniform intraday volume: optimistic, labelled); remainder cancelled at close - M"}


@dataclass(frozen=True)
class Sizing:
    max_weight: Fixed
    aggregate_cap: Fixed
    cash_reserve: Fixed
    no_trade_band: Fixed
    min_order_notional: Fixed
    # spec ``liquidity.min_adv_shares``: no new buy when the mean volume of the last ``adv_window`` visible bars is
    # below it (exits are never blocked). 0 disables the filter.
    min_adv_shares: Fixed = Fixed(0)
    adv_window: int = 20

    @classmethod
    def from_spec(cls, spec) -> "Sizing":
        s = spec.sizing
        return cls(s["max_weight"], s["aggregate_cap"], s["cash_reserve"], s["no_trade_band"], s["min_order_notional"],
                   Fixed.parse(spec.body["liquidity"]["min_adv_shares"]))

    def to_json(self) -> dict:
        out = {k: getattr(self, k).to_str() for k in ("max_weight", "aggregate_cap", "cash_reserve", "no_trade_band",
                                                      "min_order_notional", "min_adv_shares")}
        out["adv_window"] = self.adv_window
        return out


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: Fixed
    cost_model: CostModel
    execution: ExecutionModel
    sizing: Sizing
    currency: str = "USD"
    decision_clock_utc: time = time(21, 30)
    calendar: BusinessCalendar = WEEKDAYS_ONLY
    settlement_days: int = 1
    sigma_half_life: int = 20
    sigma_default: float = 0.03            # conservative warm-up volatility for impact (labelled)
    max_drawdown_stop: Fixed | None = None # engine risk stop: flatten and stop entries beyond this drawdown
    all_in_cost_per_day: Fixed = ZERO6     # infrastructure + data + model cost per session (book ch. 7)
    all_in_cost_note: str = "NOT CONFIGURED: all-in costs default to 0 and are reported as such"
    seed: int = 0
    # Schema v2 (BR-DAT-04): the risk term of the A15 sizer, entry and exit fees by ``risk.fees.envelope``.
    # None: the legacy target-weight sizer of schema v1 specs.
    risk: RiskTerm | None = None

    @classmethod
    def from_spec(cls, spec, initial_cash: Fixed, *, cost_multiplier: Fixed = ONE, impact_y: Fixed | None = None,
                  participation: Fixed | None = None, limit_offset_bps: int | None = None, seed: int = 0,
                  calendar: BusinessCalendar = WEEKDAYS_ONLY) -> "BacktestConfig":
        x = spec.execution
        hold = spec.body["rules"]["holding"]
        if hold["min_holding_sessions"] != 0 or hold["max_holding_sessions"] is not None:
            # Refuse rather than silently ignore a declared rule (review fix 2026-10-22).
            raise SpecError("rules.holding: daily engine v1 does not implement minimum or maximum holding periods; "
                            "set min_holding_sessions 0 and max_holding_sessions null, or extend the engine")
        hh, mm = (int(v) for v in x["decision_clock_utc"].split(":"))
        cost_model = CostModel.from_spec(spec, multiplier=cost_multiplier, impact_y=impact_y)
        return cls(initial_cash=initial_cash.rescale(LEDGER_SCALE), cost_model=cost_model,
                   execution=ExecutionModel(participation=x["participation_cap"] if participation is None else participation,
                                            limit_offset_bps=x["limit_offset_bps"] if limit_offset_bps is None else limit_offset_bps,
                                            execution_delay_sessions=x["execution_delay_sessions"],
                                            window=SubmissionWindow.from_spec(spec)),
                   sizing=Sizing.from_spec(spec), decision_clock_utc=time(hh, mm), calendar=calendar,
                   max_drawdown_stop=Fixed.parse(spec.body["risk_limits"]["max_drawdown_stop"]), seed=seed,
                   risk=RiskTerm.from_spec(spec, cost_model.core_fee_model(), cost_multiplier=cost_multiplier))

    def to_json(self) -> dict:
        out = {"initial_cash": self.initial_cash.to_str(), "currency": self.currency, "cost_model": self.cost_model.describe(),
               "execution": self.execution.to_json(), "sizing": self.sizing.to_json(),
               "decision_clock_utc": self.decision_clock_utc.strftime("%H:%M"),
               "calendar": f"{self.calendar.calendar_id}@{self.calendar.version}", "settlement_days": self.settlement_days,
               "sigma_half_life": self.sigma_half_life, "sigma_default": self.sigma_default,
               "max_drawdown_stop": self.max_drawdown_stop.to_str() if self.max_drawdown_stop is not None else None,
               "all_in_cost_per_day": self.all_in_cost_per_day.to_str(), "all_in_cost_note": self.all_in_cost_note,
               "seed": self.seed, "simulator_version": SIMULATOR_VERSION}
        if self.risk is not None:      # absent for a v1 spec, so a legacy run's config (and its hash) is unchanged
            out["risk_term"] = self.risk.to_json()
        return out

    def config_hash(self) -> str:
        return stable_hash(self.to_json())


# ================================================================ strategy API
@dataclass(frozen=True)
class OrderRequest:
    """An explicit order from a strategy. ``execution`` must be NEXT_SESSION; anything else is a causality error."""
    instrument_id: str
    side: str
    quantity: Fixed
    limit_price: Fixed
    reason: str = "explicit"
    execution: str = "NEXT_SESSION"


@dataclass
class StrategyDecision:
    targets: dict[str, float] | None = None       # desired weight per instrument (fraction of marked equity)
    notes: dict[str, Any] = field(default_factory=dict)
    orders: tuple[OrderRequest, ...] = ()


class Strategy(Protocol):
    strategy_id: str
    spec: Any

    def reset(self) -> None: ...

    def decide(self, ctx: "DecisionContext") -> StrategyDecision: ...


class DecisionContext:
    """What a strategy may see at one decision instant. Everything else raises ``LookaheadError``."""

    def __init__(self, engine: "_Run", decision_ns: int, session_date: date, new: dict[str, list[Bar]],
                 revised: dict[str, bool]):
        self._e = engine
        self.decision_ns = decision_ns
        self.session_date = session_date
        self._new = new
        self._revised = revised
        self.instrument_ids = engine.view.instrument_ids

    def history(self, instrument_id: str) -> list[Bar]:
        return list(self._e.cursors[instrument_id].history)

    def new_bars(self, instrument_id: str) -> list[Bar]:
        return list(self._new.get(instrument_id, ()))

    def revised(self, instrument_id: str) -> bool:
        return self._revised.get(instrument_id, False)

    def latest(self, instrument_id: str) -> Bar | None:
        h = self._e.cursors[instrument_id].history
        return h[-1] if h else None

    def bar(self, instrument_id: str, session_date: date) -> Bar:
        return self._e.cursors[instrument_id].visible(session_date)

    def position(self, instrument_id: str) -> Fixed:
        return self._e.position(instrument_id)

    @property
    def equity(self) -> Fixed:
        return self._e.equity_at_marks()


# ================================================================== results
@dataclass
class BacktestResult:
    strategy_id: str
    spec_hash: str
    parameters: dict[str, str]
    config: dict
    view: dict
    events: list[dict]
    event_log_hash: str
    dates: list[date]
    nav: list[Fixed]
    returns: list[float]
    turnover: list[float]
    exposure: list[float]
    fills: list[dict]
    orders: list[dict]
    final: dict
    costs: dict
    decisions: int
    no_trade_decisions: int

    def return_dates(self) -> list[date]:
        return self.dates[1:]

    def returns_between(self, start: date, end: date) -> tuple[list[date], list[float]]:
        ds, rs = [], []
        for d, r in zip(self.dates[1:], self.returns):
            if start <= d <= end:
                ds.append(d)
                rs.append(r)
        return ds, rs

    def summary(self) -> dict:
        from . import stats
        out = stats.summary(self.returns, [fixed_to_float(v) for v in self.nav], turnover=self.turnover,
                            exposure=self.exposure)
        orders = [o for o in self.orders if o["status"] != "REJECTED"]
        ordered = sum((Fixed.parse(o["quantity"]) for o in orders), Fixed(0))     # exact sums: scales may differ
        filled = sum((Fixed.parse(o["filled"]) for o in orders), Fixed(0))
        out["fill_ratio"] = fixed_to_float(filled) / fixed_to_float(ordered) if ordered.is_positive() else None
        out["orders"] = len(self.orders)
        out["rejected_orders"] = sum(1 for o in self.orders if o["status"] == "REJECTED")
        out["fills"] = len(self.fills)
        out["decisions"] = self.decisions
        out["no_trade_decisions"] = self.no_trade_decisions
        out["costs"] = self.costs
        return out


# =================================================================== engine
@dataclass
class _Lot:
    lot_id: str
    qty: Fixed
    cost: Fixed
    opened_ns: int


@dataclass
class _Pending:
    settle_date: date
    amount: Fixed          # signed: + receivable, - payable/fee
    kind: str              # TRADE | FEE
    ref: str


@dataclass
class _Order:
    order_id: str
    instrument_id: str
    side: str
    quantity: Fixed
    limit_price: Fixed
    decision_ns: int
    decision_date: date
    exec_date: date
    ref_price: Fixed
    sigma: float
    reason: str
    status: str = "CREATED"
    filled: Fixed = field(default_factory=lambda: Fixed(0))
    notional: Fixed = field(default_factory=lambda: ZERO6)
    fees: Fixed = field(default_factory=lambda: ZERO6)
    detail: str = ""
    # Schema v2 (BR-DAT-19): the decision's model bound; ``limit_price`` becomes the limit set at transmission.
    model_bound: Fixed | None = None
    transmission: dict | None = None

    def to_json(self) -> dict:
        out = {"order_id": self.order_id, "instrument_id": self.instrument_id, "side": self.side,
               "quantity": self.quantity.to_str(), "limit_price": self.limit_price.to_str(),
               "decision_date": self.decision_date.isoformat(), "exec_date": self.exec_date.isoformat(),
               "ref_price": self.ref_price.to_str(), "sigma": self.sigma, "reason": self.reason, "status": self.status,
               "filled": self.filled.to_str(), "notional": self.notional.to_str(), "fees": self.fees.to_str(),
               "detail": self.detail}
        if self.model_bound is not None:
            out["model_bound"] = self.model_bound.to_str()
        return out


@dataclass(frozen=True)
class FillQuote:
    """Outcome of applying the fill rules to one order and one bar (pure; see ``simulate_fill``)."""
    filled: bool
    reason: str
    quantity: Fixed = Fixed(0)
    price: Fixed = Fixed(0)
    reference: Fixed = Fixed(0)          # cost-free execution price (open or limit)
    cost: PriceCost | None = None
    limit_bound: bool = False            # the cost-adjusted price was capped at the limit


def simulate_fill(side: str, quantity: Fixed, limit: Fixed, bar: Bar, sigma: float, cost_model: CostModel,
                  participation: Fixed, instrument: Instrument, volume_window: tuple[int, int] | None = None) -> FillQuote:
    """Apply the daily-bar fill rules. Pure function of its inputs.

    ``volume_window`` (window minutes, session minutes): the order may trade only inside its submission window, so the
    participation cap applies to that share of the bar's volume (uniform intraday volume; BR-DAT-19)."""
    if not bar.volume.is_positive():
        return FillQuote(False, "NO_VOLUME")
    if volume_window is None:
        cap = (participation * bar.volume).rescale(0, Rounding.FLOOR)
    else:
        w, s = volume_window
        cap = (participation * bar.volume * w).div(s, 0, Rounding.FLOOR) if s > 0 else Fixed(0)
    qty = quantity if quantity <= cap else cap
    if not qty.is_positive():
        return FillQuote(False, "PARTICIPATION_CAP_ZERO")
    if side == "BUY":
        if not bar.low < limit:
            return FillQuote(False, "NOT_TRADED_THROUGH" if bar.low == limit else "LIMIT_NOT_REACHED")
        ref = bar.open if bar.open <= limit else limit
        pc = cost_model.price_cost(sigma, qty, bar.volume)
        raw = ref * (ONE + pc.fraction)
        price = raw.to_increment(instrument.tick_for(raw), Rounding.CEILING)
        bound = price > limit
        price = limit if bound else price
    elif side == "SELL":
        if not bar.high > limit:
            return FillQuote(False, "NOT_TRADED_THROUGH" if bar.high == limit else "LIMIT_NOT_REACHED")
        ref = bar.open if bar.open >= limit else limit
        pc = cost_model.price_cost(sigma, qty, bar.volume)
        raw = ref * (ONE - pc.fraction)
        price = raw.to_increment(instrument.tick_for(raw), Rounding.FLOOR) if raw.is_positive() else limit
        bound = price < limit
        price = limit if bound else price
    else:
        raise ValueError(f"unknown side {side}")
    return FillQuote(True, "FILLED" if qty == quantity else "PARTIAL_PARTICIPATION", qty, price, ref, pc, bound)


def implementation_shortfall(side: str, ordered: Fixed, fills: list[tuple[Fixed, Fixed]], fees: Fixed, p0: Fixed,
                             p_mark: Fixed) -> dict:
    """BR-DAT-03 corrected eq. 7.1: execution, fee and opportunity components (exact) plus bps (float)."""
    s = 1 if side == "BUY" else -1
    q_filled = sum((q for q, _ in fills), Fixed(0))
    paid = sum((q * p for q, p in fills), Fixed(0))
    execution = (paid - q_filled * p0) * s
    opportunity = (ordered - q_filled) * (p_mark - p0) * s
    total = execution + fees + opportunity
    base = fixed_to_float(ordered * p0)
    return {"execution": execution.to_str(), "fees": fees.to_str(), "opportunity": opportunity.to_str(),
            "total": total.to_str(), "bps": (fixed_to_float(total) / base * 1e4) if base else None,
            "p0": p0.to_str(), "p_mark": p_mark.to_str(), "formula": "eq7.1.br-dat-03@1",
            "p_mark_rule": "close of the execution session"}


def _decision_ns(d: date, clock: time) -> int:
    return int(datetime.combine(d, clock, tzinfo=timezone.utc).timestamp()) * NS_PER_S


class _Sigma:
    """Causal EWMA volatility of daily log returns (eq. 5.2) for the impact model."""

    def __init__(self, half_life: int):
        self.lam = 2.0 ** (-1.0 / half_life)
        self.v: float | None = None
        self.last: float | None = None
        self.n = 0

    def update(self, close: Fixed) -> None:
        x = math.log(fixed_to_float(close))
        if self.last is not None:
            r2 = (x - self.last) ** 2
            self.v = r2 if self.v is None else self.lam * self.v + (1 - self.lam) * r2
            self.n += 1
        self.last = x

    def value(self, default: float) -> float:
        return math.sqrt(self.v) if self.v is not None and self.n >= 2 else default


class _Run:
    def __init__(self, strategy: Strategy, view: DatasetView, instruments: dict[str, Instrument], config: BacktestConfig):
        missing = [i for i in view.instrument_ids if i not in instruments]
        if missing:
            raise ValueError(f"no instrument metadata for {missing}")
        sched = config.cost_model.schedule
        for i in view.instrument_ids:
            inst = instruments[i]
            # CAP-RES-05: a market without a recorded fee schedule is refused, never priced with another market's fees.
            if not (inst.currency == inst.settlement_currency == config.currency == sched.currency):
                raise ValueError(f"no recorded fee schedule for {i} ({inst.currency}/{inst.settlement_currency}): "
                                 f"{sched.schedule_id} covers {sched.currency} only")
            if inst.settlement_days != config.settlement_days:
                raise ValueError(f"{i} settles T+{inst.settlement_days} but the run settles T+{config.settlement_days}")
        self.s = strategy
        self.view = view
        self.inst = instruments
        self.cfg = config
        self.cursors = {i: Cursor(view.series[i]) for i in view.instrument_ids}
        self.sigma = {i: _Sigma(config.sigma_half_life) for i in view.instrument_ids}
        self.settled = config.initial_cash.rescale(LEDGER_SCALE)
        self.pending: list[_Pending] = []
        self.lots: dict[str, list[_Lot]] = {i: [] for i in view.instrument_ids}
        self.realized = ZERO6
        self.events: list[dict] = []
        self.orders: list[_Order] = []
        self.open: list[_Order] = []
        self.fills: list[dict] = []
        self.month_volume: dict[tuple[int, int], int] = {}
        self.commission = ZERO6
        self.third_party = ZERO6
        self.price_cost = ZERO6
        self.order_seq = 0
        self.fill_seq = 0
        self.risk_stopped = False
        self.peak = self.settled

    # ---------------------------------------------------------------- state
    def position(self, instrument_id: str) -> Fixed:
        return sum((l.qty for l in self.lots[instrument_id]), Fixed(0))

    def mark(self, instrument_id: str) -> Fixed | None:
        h = self.cursors[instrument_id].history
        return h[-1].close if h else None

    def unsettled(self) -> Fixed:
        return sum((p.amount for p in self.pending), ZERO6)

    def available(self) -> Fixed:
        """Settled cash minus unsettled payables and accrued fees; receivables excluded (LED-03)."""
        return self.settled + sum((p.amount for p in self.pending if p.amount.is_negative()), ZERO6)

    def equity_at_marks(self) -> Fixed:
        total = self.settled + self.unsettled()
        for i, lots in self.lots.items():
            q = sum((l.qty for l in lots), Fixed(0))
            if q.is_positive():
                m = self.mark(i)
                total = total + q * (m if m is not None else Fixed(0))
        return total

    def emit(self, kind: str, day: date, t_ns: int, **data: Any) -> None:
        self.events.append({"seq": len(self.events) + 1, "type": kind, "date": day.isoformat(), "t_ns": t_ns, **data})

    # ------------------------------------------------------------- phases
    def settle(self, day: date, t_ns: int) -> None:
        due = [p for p in self.pending if p.settle_date <= day]
        if not due:
            return
        self.pending = [p for p in self.pending if p.settle_date > day]
        for p in due:
            self.settled = self.settled + p.amount
        self.emit("SETTLEMENT", day, t_ns, items=[{"ref": p.ref, "kind": p.kind, "amount": p.amount.to_str(),
                                                    "settle_date": p.settle_date.isoformat()} for p in due],
                  settled_cash=self.settled.to_str())

    def execute(self, day: date) -> list[_Order]:
        due = [o for o in self.open if o.exec_date <= day]
        self.open = [o for o in self.open if o.exec_date > day]
        due.sort(key=lambda o: (0 if o.side == "SELL" else 1, o.instrument_id, o.order_id))
        done = []
        window = self.cfg.execution.window
        for o in due:
            bar = self.view.series[o.instrument_id].final(day) if o.exec_date == day else None
            wt = None
            if bar is not None and window is not None:
                # BR-DAT-19: submission at open + N of this session (calendar hours; FIXTURE convention without them)
                wt = window_times(window, self.cfg.calendar, day, fixture=bar.fixture)
                t_sub = wt.start_ns
            else:
                t_sub = (bar.start_ns if bar else _decision_ns(day, time(0, 0))) + self.cfg.execution.latency_s * NS_PER_S
            if bar is None:
                o.status, o.detail = "EXPIRED", ("NO_BAR" if o.exec_date == day else "NO_SESSION")
                self.emit("ORDER_EXPIRED", day, t_sub, order_id=o.order_id, reason=o.detail, leaves=o.quantity.to_str())
                done.append(o)
                continue
            if bar.start_ns <= o.decision_ns:
                raise CausalityError(f"order {o.order_id} decided at {o.decision_ns} cannot execute in a bar starting "
                                     f"at {bar.start_ns}")
            if wt is not None:
                if wt.empty:
                    o.status, o.detail = "EXPIRED", "NO_SUBMISSION_WINDOW"
                    self.emit("ORDER_EXPIRED", day, t_sub, order_id=o.order_id, reason=o.detail, leaves=o.quantity.to_str(),
                              window=wt.to_json())
                    done.append(o)
                    continue
                if bar.start_ns >= wt.start_ns:
                    raise CausalityError(f"order {o.order_id}: the {day} bar opens at {bar.start_ns}, not before the "
                                         f"submission window ({wt.start_ns}): its open is not known at transmission")
                # The limit referenced at transmission: the model bound tightened to the collar around the session open,
                # the last price known when the window opens (daily bars carry no quote; labelled).
                o.model_bound = o.limit_price
                o.limit_price = transmission_limit(o.side, o.model_bound, bar.open, window.collar_bps, self.inst[o.instrument_id])
                o.transmission = {"model_bound": o.model_bound.to_str(), "reference": bar.open.to_str(),
                                  "reference_source": BACKTEST_REFERENCE, "collar_bps": window.collar_bps,
                                  "window": wt.to_json()}
            # ---- reservation at submission (cash-account rules)
            if o.side == "BUY":
                notional_bound = (o.limit_price * o.quantity).rescale(LEDGER_SCALE, Rounding.CEILING)
                required = notional_bound + self.cfg.cost_model.fee_envelope("BUY", o.quantity, o.limit_price)
                available = self.available()
                if required > available:
                    o.status, o.detail = "REJECTED", "INSUFFICIENT_SETTLED_CASH"
                    self.emit("ORDER_REJECTED", day, t_sub, order_id=o.order_id, reason=o.detail,
                              required=required.to_str(), available=available.to_str())
                    done.append(o)
                    continue
            else:
                owned = self.position(o.instrument_id)
                if o.quantity > owned:
                    o.status, o.detail = "REJECTED", "NO_SHORTING"
                    self.emit("ORDER_REJECTED", day, t_sub, order_id=o.order_id, reason=o.detail,
                              owned=owned.to_str(), quantity=o.quantity.to_str())
                    done.append(o)
                    continue
            if o.transmission is None:
                self.emit("ORDER_SUBMITTED", day, t_sub, order_id=o.order_id, instrument_id=o.instrument_id, side=o.side,
                          quantity=o.quantity.to_str(), limit_price=o.limit_price.to_str())
            else:
                self.emit("ORDER_SUBMITTED", day, t_sub, order_id=o.order_id, instrument_id=o.instrument_id, side=o.side,
                          quantity=o.quantity.to_str(), limit_price=o.limit_price.to_str(), transmission=o.transmission)
            fq = simulate_fill(o.side, o.quantity, o.limit_price, bar, o.sigma, self.cfg.cost_model,
                               self.cfg.execution.participation, self.inst[o.instrument_id],
                               volume_window=wt.minutes if wt is not None else None)
            fills: list[tuple[Fixed, Fixed]] = []
            if fq.filled:
                self.book_fill(o, day, t_sub, fq)
                fills.append((fq.quantity, fq.price))
            leaves = o.quantity - o.filled
            if leaves.is_positive():
                self.emit("ORDER_EXPIRED", day, t_sub, order_id=o.order_id, reason=fq.reason if not fq.filled else
                          ("WINDOW_END_REMAINDER" if wt is not None else "END_OF_DAY_REMAINDER"), leaves=leaves.to_str(),
                          bar={"open": bar.open.to_str(), "high": bar.high.to_str(), "low": bar.low.to_str(),
                               "volume": bar.volume.to_str()})
            o.status = "FILLED" if leaves.is_zero() else ("PARTIALLY_FILLED_EXPIRED" if o.filled.is_positive() else "EXPIRED")
            if not fq.filled:
                o.detail = fq.reason
            is_ = implementation_shortfall(o.side, o.quantity, fills, o.fees, o.ref_price, bar.close)
            self.emit("ORDER_DONE", day, t_sub, order_id=o.order_id, status=o.status, shortfall=is_)
            done.append(o)
        return done

    def book_fill(self, o: _Order, day: date, t_ns: int, fq: FillQuote) -> None:
        self.fill_seq += 1
        fill_id = f"F{self.fill_seq:06d}"
        qty, price = fq.quantity, fq.price
        notional = (qty * price).rescale(LEDGER_SCALE, Rounding.HALF_EVEN)
        month = (day.year, day.month)
        before = self.month_volume.get(month, 0)
        fees = self.cfg.cost_model.fees(o.side, qty, price, before)
        self.month_volume[month] = before + int(qty.rescale(0).mantissa)
        settle = self.cfg.calendar.add_business_days(day, self.cfg.settlement_days)
        s = 1 if o.side == "BUY" else -1
        price_cost = ((price - fq.reference) * qty * s).rescale(LEDGER_SCALE, Rounding.HALF_EVEN)
        consumed = []
        if o.side == "BUY":
            self.lots[o.instrument_id].append(_Lot(fill_id, qty, notional, t_ns))
            self.pending.append(_Pending(settle, -notional, "TRADE", fill_id))
            realized = ZERO6
        else:
            remaining = qty
            cost_removed = ZERO6
            kept = []
            for lot in self.lots[o.instrument_id]:
                if not remaining.is_positive():
                    kept.append(lot)
                    continue
                take = remaining if remaining < lot.qty else lot.qty
                cost = lot.cost if take == lot.qty else (lot.cost * take).div(lot.qty, LEDGER_SCALE, Rounding.HALF_EVEN)
                consumed.append({"lot_id": lot.lot_id, "qty": take.to_str(), "cost": cost.to_str()})
                cost_removed = cost_removed + cost
                remaining = remaining - take
                if take < lot.qty:
                    kept.append(_Lot(lot.lot_id, lot.qty - take, (lot.cost - cost).rescale(LEDGER_SCALE), lot.opened_ns))
            if remaining.is_positive():  # unreachable: reservation checked owned quantity
                raise AssertionError("sell beyond owned quantity")
            self.lots[o.instrument_id] = kept
            self.pending.append(_Pending(settle, notional, "TRADE", fill_id))
            realized = notional - cost_removed
            self.realized = self.realized + realized
        if fees.total.is_positive():
            self.pending.append(_Pending(settle, -fees.total, "FEE", fill_id))
        self.commission = self.commission + fees.commission
        self.third_party = self.third_party + fees.third_party
        self.price_cost = self.price_cost + price_cost
        o.filled = o.filled + qty
        o.notional = o.notional + notional
        o.fees = o.fees + fees.total
        rec = {"fill_id": fill_id, "order_id": o.order_id, "instrument_id": o.instrument_id, "side": o.side,
               "quantity": qty.to_str(), "price": price.to_str(), "notional": notional.to_str(), "fees": fees.to_json(),
               "settle_date": settle.isoformat(), "trade_date": day.isoformat(), "exec_time_ns": t_ns,
               "reference_price": fq.reference.to_str(), "price_cost": price_cost.to_str(),
               "cost_fraction": fq.cost.to_json() if fq.cost else None, "limit_bound": fq.limit_bound,
               "realized": realized.to_str(), "lots_consumed": consumed}
        self.fills.append(rec)
        self.emit("FILL", day, t_ns, **rec)

    def advance(self, day: date) -> tuple[int, dict[str, list[Bar]], dict[str, bool]]:
        """Make visible everything available at this session's decision instant (and nothing later)."""
        t = _decision_ns(day, self.cfg.decision_clock_utc)
        new: dict[str, list[Bar]] = {}
        revised: dict[str, bool] = {}
        for i, c in self.cursors.items():
            appended, rev = c.advance(t)
            new[i], revised[i] = appended, rev
            if rev:
                self.sigma[i] = _Sigma(self.cfg.sigma_half_life)
                for b in c.history:
                    self.sigma[i].update(b.close)
            else:
                for b in appended:
                    self.sigma[i].update(b.close)
        return t, new, revised

    def decide(self, day: date, t: int, new: dict[str, list[Bar]], revised: dict[str, bool]) -> bool:
        ctx = DecisionContext(self, t, day, new, revised)
        decision = self.s.decide(ctx)
        if not isinstance(decision, StrategyDecision):
            raise TypeError("strategy.decide must return a StrategyDecision")
        equity = self.equity_at_marks()
        if self.cfg.max_drawdown_stop is not None and not self.risk_stopped:
            self.peak = equity if equity > self.peak else self.peak
            dd = (self.peak - equity).div(self.peak, 8, Rounding.HALF_EVEN) if self.peak.is_positive() else Fixed(0)
            if dd > self.cfg.max_drawdown_stop:
                self.risk_stopped = True
                self.emit("RISK_STOP", day, t, drawdown=dd.to_str(), limit=self.cfg.max_drawdown_stop.to_str(),
                          note="drawdown stop: targets forced to zero for the rest of the run")
        targets = decision.targets
        if self.risk_stopped:
            # Flatten every instrument, whatever the strategy returned (including no targets at all).
            targets = {k: 0.0 for k in self.view.instrument_ids}
        exec_date = self.cfg.calendar.add_business_days(day, self.cfg.execution.execution_delay_sessions)
        planned, plan_notes = self.plan(targets, ctx, day, exec_date, t) if targets is not None else ([], {})
        refused = []
        for req in decision.orders:
            if self.risk_stopped and req.side == "BUY":
                # The drawdown stop blocks entries for explicit orders too (review fix 2026-10-22).
                refused.append({"instrument_id": req.instrument_id, "side": req.side, "quantity": req.quantity.to_str(),
                                "limit_price": req.limit_price.to_str(), "reason": "RISK_STOPPED"})
                continue
            planned.append(self.explicit(req, day, exec_date, t))
        if refused:
            plan_notes = {**plan_notes, "explicit_orders_refused": refused}
        for o in planned:
            self.orders.append(o)
            self.open.append(o)
            self.emit("ORDER_CREATED", day, t, **o.to_json())
        no_trade = not planned
        self.emit("DECISION", day, t, targets={k: v for k, v in sorted((targets or {}).items())}, notes=decision.notes,
                  plan=plan_notes, orders=[o.order_id for o in planned], outcome="NO_TRADE" if no_trade else "INTENTS",
                  equity=equity.to_str(), exec_date=exec_date.isoformat())
        return no_trade

    def _new_order(self, inst: str, side: str, qty: Fixed, limit: Fixed, day: date, exec_date: date, t: int,
                   ref: Fixed, reason: str) -> _Order:
        self.order_seq += 1
        return _Order(f"O{self.order_seq:06d}", inst, side, qty, limit, t, day, exec_date, ref,
                      self.sigma[inst].value(self.cfg.sigma_default), reason)

    def explicit(self, req: OrderRequest, day: date, exec_date: date, t: int) -> _Order:
        if req.execution != "NEXT_SESSION":
            raise CausalityError(f"order execution {req.execution!r} refused: a bar's close cannot both generate a "
                                 "signal and fill at that close; the earliest fill is the next session (CAP-RES-04)")
        inst = self.inst[req.instrument_id]
        if req.side not in ("BUY", "SELL") or not req.quantity.is_positive() or not req.quantity.is_multiple_of(inst.quantity_increment):
            raise ValueError(f"invalid explicit order {req}")
        if not req.limit_price.is_positive() or not req.limit_price.is_multiple_of(inst.tick_for(req.limit_price)):
            raise ValueError(f"limit {req.limit_price} is not a positive multiple of the tick")
        ref = self.mark(req.instrument_id) or req.limit_price
        return self._new_order(req.instrument_id, req.side, req.quantity, req.limit_price, day, exec_date, t, ref, req.reason)

    def plan(self, targets: dict[str, float], ctx: DecisionContext, day: date, exec_date: date, t: int) -> tuple[list[_Order], dict]:
        """Algorithm A15 through ``research.sizing.plan_orders``, the code path the strategy runner uses (BR-DAT-15
        aggregate cap; BR-DAT-04 risk term for schema v2 specs). What this ledger supplies: holdings (lots and open
        orders), the settled-cash projection at the execution date as the budget, the schedule's fee envelope, and
        the mean visible volume of the last ``adv_window`` bars."""
        sz = self.cfg.sizing
        unknown = sorted(set(targets) - set(self.view.instrument_ids))
        if unknown:
            raise ValueError(f"targets for instruments outside the view: {unknown}")
        equity = self.equity_at_marks()
        settles_by_exec = [p for p in self.pending if p.settle_date <= exec_date]
        later_debits = [p for p in self.pending if p.settle_date > exec_date and p.amount.is_negative()]
        budget = self.settled + sum((p.amount for p in settles_by_exec), ZERO6) + sum((p.amount for p in later_debits), ZERO6)
        holdings: dict[str, Holding] = {}
        adv: dict[str, Fixed] = {}
        for i in targets:
            held = self.position(i)
            pending_buy = sum((o.quantity for o in self.open if o.instrument_id == i and o.side == "BUY"), Fixed(0))
            pending_sell = sum((o.quantity for o in self.open if o.instrument_id == i and o.side == "SELL"), Fixed(0))
            holdings[i] = Holding(owned=held, unreserved=held - pending_sell, pending_buy=pending_buy, pending_sell=pending_sell)
            hist = self.cursors[i].history[-sz.adv_window:]          # visible bars only (causal)
            adv[i] = sum((b.volume for b in hist), Fixed(0)).div(Fixed(len(hist)), 0, Rounding.FLOOR) if hist else Fixed(0)
        plans, notes = plan_orders(targets, sizing=sz, limit_offset_bps=self.cfg.execution.limit_offset_bps, equity=equity,
                                   marks={i: self.mark(i) for i in targets}, holdings=holdings, instruments=self.inst,
                                   budget=budget, fee_envelope=self.cfg.cost_model.fee_envelope, adv=adv,
                                   ledger_scale=LEDGER_SCALE, risk=self.cfg.risk)
        orders = []
        for side in ("SELL", "BUY"):                                  # sells first, then buys, each in instrument order
            for i in sorted(plans):
                o = plans[i].order
                if o is not None and o.side == side:
                    orders.append(self._new_order(i, o.side, o.quantity, o.limit_price, day, exec_date, t, o.reference_price,
                                                  o.reason))
        notes = {k: v for k, v in notes.items() if k != "budget"}
        notes["instruments"] = {i: p.note for i, p in sorted(plans.items()) if p.note}
        risk = {i: p.risk for i, p in sorted(plans.items()) if p.risk is not None}
        if risk:
            notes["risk"] = risk
        return orders, notes


def run_backtest(strategy: Strategy, view: DatasetView, instruments: dict[str, Instrument],
                 config: BacktestConfig) -> BacktestResult:
    """Run one deterministic backtest. Raises ``CausalityError``/``LookaheadError`` on causal violations."""
    run = _Run(strategy, view, instruments, config)
    strategy.reset()
    sessions = view.sessions()
    if len(sessions) < 2:
        raise ValueError("a backtest needs at least two sessions")
    spec = getattr(strategy, "spec", None)
    run.emit("RUN_START", sessions[0], _decision_ns(sessions[0], time(0, 0)), strategy_id=strategy.strategy_id,
             spec_hash=spec.spec_hash if spec else None, config_hash=config.config_hash(),
             # names (view label, grant id) are not inputs: they stay out of the event log so equal inputs hash equal
             view={k: v for k, v in view.describe().items() if k not in ("label", "holdout_access_id")})
    dates: list[date] = []
    nav: list[Fixed] = []
    turnover: list[float] = []
    exposure: list[float] = []
    decisions = no_trades = 0
    prev_nav: Fixed | None = None
    for day in sessions:
        start_ns = _decision_ns(day, time(0, 0))
        run.settle(day, start_ns)
        done = run.execute(day)
        t, new, revised = run.advance(day)  # marks and decisions use only what is visible at the decision instant
        equity = run.equity_at_marks()
        traded = sum((o.notional for o in done), ZERO6)
        held_value = equity - run.settled - run.unsettled()
        dates.append(day)
        nav.append(equity)
        turnover.append(0.5 * fixed_to_float(traded) / fixed_to_float(prev_nav) if prev_nav and prev_nav.is_positive() else 0.0)
        exposure.append(fixed_to_float(held_value) / fixed_to_float(equity) if equity.is_positive() else 0.0)
        run.emit("MARK", day, t, nav=equity.to_str(), settled_cash=run.settled.to_str(), unsettled=run.unsettled().to_str(),
                 positions={i: run.position(i).to_str() for i in run.lots}, formula="nav@1")
        prev_nav = equity
        no_trade = run.decide(day, t, new, revised)
        decisions += 1
        no_trades += 1 if no_trade else 0
    last = sessions[-1]
    for o in run.open:
        o.status, o.detail = "EXPIRED", "END_OF_DATA"
        run.emit("ORDER_EXPIRED", last, _decision_ns(last, time(23, 59)), order_id=o.order_id, reason="END_OF_DATA",
                 leaves=o.quantity.to_str())
    run.open = []
    returns = [fixed_to_float(b) / fixed_to_float(a) - 1.0 for a, b in zip(nav, nav[1:])]
    days = len(sessions)
    net_pnl = nav[-1] - config.initial_cash
    fees_total = run.commission + run.third_party
    all_in_costs = config.all_in_cost_per_day * days
    costs = {"commission": run.commission.to_str(), "third_party": run.third_party.to_str(),
             "price_cost": run.price_cost.to_str(), "gross_pnl": (net_pnl + fees_total + run.price_cost).to_str(),
             "net_pnl": net_pnl.to_str(), "all_in_pnl": (net_pnl - all_in_costs).to_str(),
             "all_in_costs": all_in_costs.to_str(), "all_in_note": config.all_in_cost_note,
             "definitions": {"gross": "net + commissions + third-party fees + price costs (spread, slippage, impact)",
                             "net": "NAV_T - initial cash", "all_in": "net - all_in_cost_per_day x sessions"}}
    final = {"settled_cash": run.settled.to_str(), "unsettled": [{"settle_date": p.settle_date.isoformat(), "amount": p.amount.to_str(),
                                                                   "kind": p.kind, "ref": p.ref} for p in run.pending],
             "positions": {i: {"quantity": run.position(i).to_str(),
                               "cost": sum((l.cost for l in run.lots[i]), ZERO6).to_str(),
                               "lots": [{"lot_id": l.lot_id, "qty": l.qty.to_str(), "cost": l.cost.to_str()} for l in run.lots[i]]}
                           for i in run.lots},
             "realized": run.realized.to_str(), "nav": nav[-1].to_str(), "risk_stopped": run.risk_stopped}
    run.emit("RUN_END", last, _decision_ns(last, time(23, 59)), final=final, costs=costs)
    params = {k: v.to_str() for k, v in (spec.parameters.items() if spec else ())}
    return BacktestResult(
        strategy_id=strategy.strategy_id, spec_hash=spec.spec_hash if spec else "", parameters=params,
        config=config.to_json(), view=view.describe(), events=run.events, event_log_hash=stable_hash(run.events),
        dates=dates, nav=nav, returns=returns, turnover=turnover[1:], exposure=exposure[1:], fills=run.fills,
        orders=[o.to_json() for o in run.orders], final=final, costs=costs, decisions=decisions,
        no_trade_decisions=no_trades)
