"""Algorithm A15 at runtime: target weights -> price-bounded limit intents (book ch. 15; ADR-0017 #4).

This is the same mapping, step for step, as the research engine's ``_Run.plan`` (``research/engine.py``), so a
runtime decision and a backtest decision with the same inputs produce the same orders (the parity test in
``tests/test_strategy_runner.py`` runs both on one state):

1. weight = floor(max(0, target), 6 dp), capped at ``max_weight``;
2. the total is scaled down to ``min(aggregate_cap, 1 - cash_reserve)`` (BR-DAT-15);
3. per instrument, current = owned + pending buys - pending sells, and its weight at the mark;
4. no trade inside the ``no_trade_band`` unless the target is a full exit;
5. desired shares = floor(weight x equity / mark) to the quantity increment; the limit is the mark
   +/- ``limit_offset_bps`` snapped to the tick against us (buy FLOOR, sell CEILING);
6. sells first, capped at the unreserved quantity, below ``min_order_notional`` only for a full exit;
7. buys: liquidity filter (mean visible volume of the last ``adv_window`` bars >= ``min_adv_shares``), then sized
   down to what the funding budget can reserve including the fee envelope, then the minimum notional.

What the engine takes from its own simulated ledger, the runtime takes from the core (read-only): owned and
unreserved quantity, pending orders of the session, the session's available cash (``ledger.available_cash``,
which already nets reservations and payables) as the budget, and the active policy's fee envelope, so the
estimate matches the core's own reservation check. The core's pretrade checks remain the authority."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from ..domain.fixed import Fixed, Rounding
from ..domain.instruments import Instrument
from ..research.common import float_to_fixed
from ..research.engine import WEIGHT_SCALE, Sizing

ONE = Fixed(1)
FeeEnvelope = Callable[[str, Fixed, Fixed], Fixed]   # (side, quantity, limit) -> envelope at ledger scale


@dataclass(frozen=True)
class Holding:
    """The session's exposure in one instrument, as the core records it."""
    owned: Fixed = Fixed(0)
    unreserved: Fixed = Fixed(0)       # owned minus active quantity holds (what a sell may still use)
    pending_buy: Fixed = Fixed(0)      # leaves of the session's open BUY orders
    pending_sell: Fixed = Fixed(0)     # leaves of the session's open SELL orders

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

    @property
    def note(self) -> str:
        return "; ".join(self.notes)


def plan(targets: dict[str, float], *, sizing: Sizing, limit_offset_bps: int, equity: Fixed, marks: dict[str, Fixed | None],
         holdings: dict[str, Holding], instruments: dict[str, Instrument], budget: Fixed, fee_envelope: FeeEnvelope,
         adv: dict[str, Fixed | None], ledger_scale: int) -> tuple[dict[str, InstrumentPlan], dict]:
    """Map target weights to orders. Returns (per-instrument plans for every target, notes)."""
    out = {i: InstrumentPlan(i, current_quantity=holdings.get(i, Holding()).current) for i in sorted(targets)}
    notes: dict = {"equity": equity.to_str(), "budget": budget.to_str()}
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
        affordable = _affordable(budget, limit, qty, fee_envelope, ledger_scale)
        if affordable < qty:
            p.notes.append(f"BUY sized down {qty}->{affordable} by available cash")
        qty = affordable
        if not qty.is_positive() or qty * limit < sizing.min_order_notional:
            p.notes.append("NO_TRADE (unfunded or below minimum notional)")
            continue
        budget = budget - (qty * limit).rescale(ledger_scale, Rounding.CEILING) - fee_envelope("BUY", qty, limit)
        p.order = OrderPlan(i, "BUY", qty, limit, ref, "ENTRY_OR_ADD")
        p.notes.append(f"BUY {qty} @ {limit}")
    notes["funding_projection"] = budget.to_str()
    notes["weights"] = {i: v.to_str() for i, v in w.items()}
    return out, notes


def _affordable(budget: Fixed, limit: Fixed, want: Fixed, fee_envelope: FeeEnvelope, ledger_scale: int) -> Fixed:
    """Largest whole quantity <= ``want`` whose notional (rounded up) plus fee envelope fits the budget
    (the engine's ``_Run._affordable``)."""
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
