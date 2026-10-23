"""Algorithm A15 sizing: target weights -> price-bounded limit orders (book ch. 15, ch. 6 eq. 6.4; BR-DAT-04, BR-DAT-15).

ONE implementation, used by the backtest engine (``engine._Run.plan``) and by the strategy runner
(``strategy.sizing.plan``), so a decision sized in research and one sized at runtime go through the same code path.
Each caller supplies what it knows from its own ledger: the engine from its simulated cash account, the runner from
the core (read-only). In order:

1. weight = floor(max(0, target), 6 dp), capped at ``max_weight``;
2. the total is scaled down to ``min(aggregate_cap, 1 - cash_reserve)`` over EVERY target (BR-DAT-15), so an
   instrument that may not trade now keeps its share of the cap;
3. per instrument, current = owned + pending buys - pending sells, and its weight at the mark;
4. no trade inside the ``no_trade_band`` unless the target is a full exit;
5. desired shares = floor(weight x equity / mark) to the quantity increment; the model limit is the mark
   +/- ``limit_offset_bps`` snapped to the tick against us (buy FLOOR, sell CEILING);
6. sells first, capped at the unreserved quantity, below ``min_order_notional`` only for a full exit;
7. buys: liquidity filter (mean visible volume of the last ``adv_window`` bars >= ``min_adv_shares``), then the
   RISK TERM (below), then sized down to what the funding budget can reserve including the fee envelope (the cash
   term), then the minimum notional. Zero means no order.

Risk term (BR-DAT-04, corrected eq. 6.4). A buy may bring the position to the largest whole quantity Q (in quantity
increments) with

    Q x (d + c) + Fee_entry(Q - current) + Fee_exit(Q)  <=  B_risk

* ``d`` = limit x ``adverse_move_fraction``: the planned adverse price move per share;
* ``c`` = limit x the spec's per-share price costs (half spread + slippage + third-party sell fee, times the cost
  multiplier): a conservative per-share cost allowance;
* ``Fee_entry``, ``Fee_exit`` = ``risk.fees.envelope``, THE function the core reserves fees with (per share, minimum
  per order, estimate margin, no cap), under the caller's fee model: the engine's schedule, or the active policy's;
* ``B_risk`` = equity x ``loss_fraction_of_equity``.

The fees are inside the risk term, so a minimum commission that the budget cannot cover sizes the buy to zero
instead of passing a trade whose loss at the adverse move exceeds its budget (book ch. 6 example: B = 0.50,
d + c = 0.25, a 0.75 minimum each way gives 0, where the uncorrected formula gave 2). The risk term never creates a
sell: it only limits how much a buy may add.

Money is exact ``Fixed``; nothing here is a float except the strategy's target weights on input."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..domain.fixed import Fixed, Rounding
from ..domain.instruments import Instrument
from ..domain.money import Money
from ..policy.model import FeeModel
from ..risk import fees as fee_function
from .common import float_to_fixed

ONE = Fixed(1)
WEIGHT_SCALE = 6
RISK_SCALE = 6
FeeEnvelope = Callable[[str, Fixed, Fixed], Fixed]   # (side, quantity, limit) -> reservation envelope at ledger scale


@dataclass(frozen=True)
class Holding:
    """The exposure in one instrument as the ledger records it."""
    owned: Fixed = Fixed(0)
    unreserved: Fixed = Fixed(0)       # owned minus active quantity holds (what a sell may still use)
    pending_buy: Fixed = Fixed(0)      # leaves of open BUY orders
    pending_sell: Fixed = Fixed(0)     # leaves of open SELL orders

    @property
    def current(self) -> Fixed:
        return self.owned + self.pending_buy - self.pending_sell


@dataclass(frozen=True)
class OrderPlan:
    instrument_id: str
    side: str
    quantity: Fixed
    limit_price: Fixed
    reference_price: Fixed
    reason: str                        # EXIT | REDUCE | ENTRY_OR_ADD


@dataclass
class InstrumentPlan:
    instrument_id: str
    target_weight: Fixed | None = None
    current_weight: Fixed | None = None
    current_quantity: Fixed = Fixed(0)
    desired_quantity: Fixed | None = None
    order: OrderPlan | None = None
    notes: list[str] = field(default_factory=list)
    risk: dict | None = None           # the risk term's inputs and result for a buy (BR-DAT-04)

    @property
    def note(self) -> str:
        return "; ".join(self.notes)


@dataclass(frozen=True)
class RiskTerm:
    """The spec's ``rules.sizing.risk_budget`` (schema v2) plus the cost allowance and the fee function's model."""
    loss_fraction_of_equity: Fixed
    adverse_move_fraction: Fixed
    cost_fraction: Fixed               # per-share price costs as a fraction of price (spec costs x multiplier)
    fee_model: FeeModel | None         # None: no fee model to evaluate the term with, so no buy is sized
    currency: str = "USD"

    @classmethod
    def from_spec(cls, spec, fee_model: FeeModel | None, *, cost_multiplier: Fixed = ONE) -> "RiskTerm | None":
        """None for a schema v1 spec (no risk budget: the legacy target-weight sizer, labelled in the config)."""
        rb = getattr(spec, "risk_budget", None)
        if rb is None:
            return None
        c = spec.costs
        per_share_bps = c["spread_bps"].div(2, 6, Rounding.CEILING) + c["slippage_bps"] + c["third_party_sell_bps"]
        cost = (per_share_bps * cost_multiplier).div(10_000, 10, Rounding.CEILING)
        return cls(rb["loss_fraction_of_equity"], rb["adverse_move_fraction"], cost, fee_model)

    def to_json(self) -> dict:
        return {"loss_fraction_of_equity": self.loss_fraction_of_equity.to_str(),
                "adverse_move_fraction": self.adverse_move_fraction.to_str(), "cost_fraction": self.cost_fraction.to_str(),
                "fee_function": "risk.fees.envelope@1 (entry and exit; per share, minimum, estimate margin, no cap)",
                "formula": "sizing.risk_term.br-dat-04@1"}


def fee_envelope_of(model: FeeModel, quantity: Fixed, notional: Fixed, currency: str = "USD") -> Fixed:
    """``risk.fees.envelope``: the core's reservation fee envelope for ``quantity`` at ``notional``."""
    if not quantity.is_positive():
        return Fixed(0)
    return fee_function.envelope(model, quantity, Money(currency, notional.rescale(6, Rounding.CEILING)
                                                         if notional.scale > 6 else notional)).amount


def risk_quantity(*, per_share_risk: Fixed, budget: Fixed, current: Fixed, want: Fixed, increment: Fixed,
                  entry_fee: Callable[[Fixed], Fixed], exit_fee: Callable[[Fixed], Fixed]) -> Fixed:
    """Largest ``q`` in [0, want], a multiple of ``increment``, with
    ``(current + q) x per_share_risk + entry_fee(q) + exit_fee(current + q) <= budget``; 0 when none fits.

    Pure; the loss is non-decreasing in ``q`` (fees are non-decreasing in quantity), so the search is a bisection
    over whole increments."""
    if not want.is_positive() or not increment.is_positive():
        return Fixed(0)

    def loss(q: Fixed) -> Fixed:
        total = current + q
        return total * per_share_risk + (entry_fee(q) if q.is_positive() else Fixed(0)) + exit_fee(total)

    hi = int(want.div(increment, 0, Rounding.FLOOR).mantissa)
    if hi <= 0 or loss(increment) > budget:
        return Fixed(0)
    lo = 1                                 # loss(lo x increment) <= budget holds
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if loss(increment * mid) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return increment * lo


def affordable(budget: Fixed, limit: Fixed, want: Fixed, fee_envelope: FeeEnvelope, ledger_scale: int) -> Fixed:
    """Cash term: largest whole quantity <= ``want`` whose notional (rounded up) plus fee envelope fits ``budget``."""
    if not budget.is_positive():
        return Fixed(0)
    q0 = budget.div(limit, 0, Rounding.FLOOR)
    q0 = q0 if q0 < want else want
    env = fee_envelope("BUY", q0, limit) if q0.is_positive() else Fixed(0)
    q = (budget - env).div(limit, 0, Rounding.FLOOR) if budget > env else Fixed(0)
    q = q if q < want else want
    while q.is_positive() and (q * limit).rescale(ledger_scale, Rounding.CEILING) + fee_envelope("BUY", q, limit) > budget:
        q = q - 1
    return q if q.is_positive() else Fixed(0)


def _risk_cap(risk: RiskTerm, *, equity: Fixed, limit: Fixed, current: Fixed, want: Fixed,
              increment: Fixed) -> tuple[Fixed, dict]:
    """The risk term for one buy: (largest addable quantity, the evaluated inputs for the decision record)."""
    budget = (equity * risk.loss_fraction_of_equity).rescale(RISK_SCALE, Rounding.FLOOR)
    d = (limit * risk.adverse_move_fraction).rescale(RISK_SCALE, Rounding.CEILING)
    c = (limit * risk.cost_fraction).rescale(RISK_SCALE, Rounding.CEILING)
    detail = {"budget": budget.to_str(), "adverse_move_per_share": d.to_str(), "cost_allowance_per_share": c.to_str(),
              "current": current.to_str()}
    if risk.fee_model is None:
        detail["max_add"] = "0"
        detail["why"] = "no fee model: entry and exit fees cannot be evaluated"
        return Fixed(0), detail
    stop = limit - d if limit > d else Fixed(0)
    model = risk.fee_model
    q = risk_quantity(per_share_risk=d + c, budget=budget, current=current, want=want, increment=increment,
                      entry_fee=lambda n: fee_envelope_of(model, n, n * limit, risk.currency),
                      exit_fee=lambda n: fee_envelope_of(model, n, n * stop, risk.currency))
    detail["max_add"] = q.to_str()
    if current.is_positive() or q.is_positive():
        held = current + q
        detail["loss_at_move"] = (held * (d + c) + (fee_envelope_of(model, q, q * limit, risk.currency) if q.is_positive()
                                                     else Fixed(0)) + fee_envelope_of(model, held, held * stop, risk.currency)).to_str()
    return q, detail


def plan_orders(targets: dict[str, float], *, sizing, limit_offset_bps: int, equity: Fixed, marks: dict[str, Fixed | None],
                holdings: dict[str, Holding], instruments: dict[str, Instrument], budget: Fixed, fee_envelope: FeeEnvelope,
                adv: dict[str, Fixed | None], ledger_scale: int, risk: RiskTerm | None = None,
                tradable: Iterable[str] | None = None) -> tuple[dict[str, InstrumentPlan], dict]:
    """Map target weights to orders (steps 1-7 above). Returns (per-instrument plans for every target, notes).

    ``sizing`` is ``research.engine.Sizing``. ``tradable`` (default: every target) names the instruments that may get
    an order at this decision; the weights are always capped and scaled over every target. ``risk`` None is the
    legacy target-weight sizer of schema v1 specs (no risk term)."""
    allowed = set(targets) if tradable is None else set(tradable)
    out = {i: InstrumentPlan(i, current_quantity=holdings.get(i, Holding()).current) for i in sorted(targets)}
    notes: dict = {"equity": equity.to_str(), "budget": budget.to_str()}
    if risk is not None:
        notes["risk_term"] = risk.to_json()
    if not equity.is_positive():
        for p in out.values():
            p.notes.append("NON_POSITIVE_EQUITY")
        return out, {**notes, "skipped": "NON_POSITIVE_EQUITY"}
    w: dict[str, Fixed] = {}
    for i in sorted(targets):
        x = targets[i]
        if x is None or not math.isfinite(x):
            raise ValueError(f"target weight for {i} is not finite")
        wf = float_to_fixed(max(0.0, x), WEIGHT_SCALE, Rounding.FLOOR)
        w[i] = wf if wf <= sizing.max_weight else sizing.max_weight
    cap_total = sizing.aggregate_cap if sizing.aggregate_cap <= ONE - sizing.cash_reserve else ONE - sizing.cash_reserve
    total = sum(w.values(), Fixed(0))
    if total > cap_total:
        w = {i: (v * cap_total).div(total, WEIGHT_SCALE, Rounding.FLOOR) for i, v in w.items()}
        notes["scaled_to_aggregate_cap"] = cap_total.to_str()
    sells: list[tuple[str, Fixed, Fixed, Fixed, str]] = []
    buys: list[tuple[str, Fixed, Fixed, Fixed]] = []
    for i in sorted(w):
        p = out[i]
        p.target_weight = w[i]
        if i not in allowed:
            p.notes.append("NOT_TRADABLE_AT_THIS_DECISION (its weight still counts toward the aggregate cap)")
            continue
        ref = marks.get(i)
        if ref is None:
            p.notes.append("NO_VISIBLE_DATA")
            continue
        inst = instruments[i]
        h = holdings.get(i, Holding())
        current = h.current
        current_w = (current * ref).div(equity, WEIGHT_SCALE, Rounding.HALF_EVEN)
        p.current_weight = current_w
        target_w = w[i]
        full_exit = target_w.is_zero() and current.is_positive()
        if abs(target_w - current_w) <= sizing.no_trade_band and not full_exit:
            p.desired_quantity = current
            p.notes.append(f"NO_TRADE_BAND |{target_w}-{current_w}|<={sizing.no_trade_band}")
            continue
        desired = (target_w * equity).div(ref, 0, Rounding.FLOOR).to_increment(inst.quantity_increment, Rounding.FLOOR)
        p.desired_quantity = desired
        delta = desired - current
        if delta.is_zero():
            p.notes.append("NO_CHANGE")
            continue
        off = Fixed(limit_offset_bps)
        if delta.is_positive():
            limit = (ref * (Fixed(10_000) + off)).div(10_000, ref.scale + 4, Rounding.FLOOR)
            limit = limit.to_increment(inst.tick_for(limit), Rounding.FLOOR)
            buys.append((i, delta, limit, ref))
        else:
            limit = (ref * (Fixed(10_000) - off)).div(10_000, ref.scale + 4, Rounding.CEILING)
            limit = limit.to_increment(inst.tick_for(limit), Rounding.CEILING)
            qty = -delta if -delta <= h.unreserved else h.unreserved
            if qty.is_positive():
                sells.append((i, qty, limit, ref, "EXIT" if full_exit else "REDUCE"))
            else:
                p.notes.append("NO_UNRESERVED_QUANTITY")
    for i, qty, limit, ref, why in sells:
        if (qty * limit) < sizing.min_order_notional and not why == "EXIT":
            out[i].notes.append("BELOW_MIN_ORDER_NOTIONAL")
            continue
        out[i].order = OrderPlan(i, "SELL", qty, limit, ref, why)
        out[i].notes.append(f"SELL {qty} @ {limit}")
    for i, qty, limit, ref in buys:
        p = out[i]
        if sizing.min_adv_shares.is_positive():
            a = adv.get(i)
            if a is None or a < sizing.min_adv_shares:
                p.notes.append(f"BELOW_MIN_ADV {a.to_str() if a is not None else 'none'}<{sizing.min_adv_shares.to_str()}")
                continue
        if risk is not None:
            cap, p.risk = _risk_cap(risk, equity=equity, limit=limit, current=holdings.get(i, Holding()).current, want=qty,
                                    increment=instruments[i].quantity_increment)
            if cap < qty:
                p.notes.append(f"RISK_BUDGET: buy limited {qty}->{cap}: loss at the adverse move plus entry and exit fees "
                               f"must fit {p.risk['budget']}")
                qty = cap
            if not qty.is_positive():
                p.notes.append("NO_TRADE (the risk budget cannot cover the adverse move plus entry and exit fees)")
                continue
        can = affordable(budget, limit, qty, fee_envelope, ledger_scale)
        if can < qty:
            p.notes.append(f"BUY sized down {qty}->{can} by available cash")
        qty = can
        if not qty.is_positive() or qty * limit < sizing.min_order_notional:
            p.notes.append("NO_TRADE (unfunded or below minimum notional)")
            continue
        budget = budget - (qty * limit).rescale(ledger_scale, Rounding.CEILING) - fee_envelope("BUY", qty, limit)
        p.order = OrderPlan(i, "BUY", qty, limit, ref, "ENTRY_OR_ADD")
        p.notes.append(f"BUY {qty} @ {limit}")
    notes["funding_projection"] = budget.to_str()
    notes["weights"] = {i: v.to_str() for i, v in w.items()}
    return out, notes
