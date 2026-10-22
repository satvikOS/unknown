"""Deterministic pretrade evaluation (RSK-01, RSK-02, ORD-08; book Ch. 13).

A pure function of a fully materialized ``PretradeInput``. It evaluates every
check so the decision record shows complete diagnostics, and reports the first
decisive failure as the rejection code. It never performs I/O.

Exposure-increasing orders (buys) must pass every gate. Risk-reducing sells of
confirmed, unreserved session inventory remain possible under a stop latch,
because the stop blocks new exposure but does not prohibit reduction
(ADR-0010); they still require reconciled positions."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.fixed import Fixed
from ..domain.instruments import Instrument, InstrumentStatus
from ..market.quotes import Quote
from ..policy.model import Eligibility

BLOCKS_REDUCTION = frozenset({"POSITION_MISMATCH", "FOREIGN_ACTIVITY", "EXECUTION_ANOMALY", "LEDGER_INVARIANT"})


@dataclass(frozen=True)
class PretradeInput:
    now_ns: int
    side: str
    order_type: str
    time_in_force: str
    quantity: Fixed
    limit_price: Fixed
    order_currency: str
    deadline_ns: int
    intent_policy_hash: str
    # account and control
    account_mode: str
    stop_latched: bool
    resume_requires_owner: bool
    reconciled_this_epoch: bool
    ownership_held: bool
    blocking_exception_kinds: tuple[str, ...]
    loss_budget_breached: str | None
    # session
    session_state: str | None
    mandate_policy_hash: str | None
    strategy_permitted: bool
    # instrument and market
    instrument: Instrument | None
    eligibility: Eligibility
    quote: Quote | None
    quote_max_age_ms: int
    price_collar_bps: int
    # money and limits (amounts in the order currency at ledger scale)
    required_cash: Fixed
    available_cash: Fixed
    unreserved_quantity: Fixed
    order_notional: Fixed
    max_order_notional: Fixed
    position_notional_after: Fixed
    session_equity: Fixed
    max_position_bps: int
    gross_exposure_after: Fixed
    max_gross_exposure: Fixed
    orders_last_minute: int
    max_orders_per_minute: int
    average_daily_volume: Fixed | None
    max_participation_bps_of_adv: int
    require_liquidity_data: bool


@dataclass
class Decision:
    approved: bool
    code: str
    checks: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"approved": self.approved, "code": self.code, "checks": self.checks}


def evaluate(i: PretradeInput) -> Decision:
    checks: list[dict] = []
    buy = i.side == "BUY"

    def check(name: str, passed: bool, detail: str = "", applies: bool = True) -> None:
        if applies:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

    check("SIDE", i.side in ("BUY", "SELL"), i.side)
    check("MODE", i.account_mode in ("SIMULATED", "PAPER"),
          f"account mode {i.account_mode}; LIVE routing is not authorized in this release")
    check("DEADLINE", i.now_ns <= i.deadline_ns, f"now={i.now_ns} deadline={i.deadline_ns}")
    check("OWNERSHIP", i.ownership_held, "execution ownership not held by this deployment" if not i.ownership_held else "")
    check("STOP_LATCH", not i.stop_latched, "stop latched; only reductions permitted", applies=buy)
    check("RESUME_HOLD", not i.resume_requires_owner, "restart recovery hold: owner start required", applies=buy)
    check("RECOVERY_GATE", i.reconciled_this_epoch, "no clean reconciliation since process start")
    blocking = [k for k in i.blocking_exception_kinds if buy or k in BLOCKS_REDUCTION]
    check("OPEN_EXCEPTIONS", not blocking, ",".join(sorted(set(blocking))))
    check("LOSS_BUDGET", i.loss_budget_breached is None, i.loss_budget_breached or "", applies=buy)
    entry_states = ("RUNNING",)
    reduce_states = ("RUNNING", "SUSPENDED", "WINDING_DOWN")
    check("SESSION_STATE", i.session_state in (entry_states if buy else reduce_states), f"session state {i.session_state}")
    check("POLICY_VERSION", i.intent_policy_hash == i.mandate_policy_hash, "intent policy hash differs from mandate")
    check("STRATEGY_PERMITTED", i.strategy_permitted, "strategy not in mandate eligibility")

    e = i.eligibility
    inst = i.instrument
    check("INSTRUMENT_KNOWN", inst is not None, "instrument not in master")
    if inst is not None:
        check("INSTRUMENT_STATUS", inst.status is InstrumentStatus.ACTIVE, inst.status.value)
        check("ASSET_CLASS", inst.asset_class.value in e.asset_classes, inst.asset_class.value)
        check("VENUE", inst.venue_mic in e.venues, inst.venue_mic)
        check("LEVERAGED_OR_INVERSE", e.allow_leveraged_or_inverse or not (inst.leveraged or inst.inverse),
              "leveraged/inverse product not eligible")
        check("CURRENCY", inst.currency == i.order_currency, f"{inst.currency} vs {i.order_currency}")
        check("FRACTIONAL", e.allow_fractional or i.quantity.is_multiple_of(Fixed(1)), "fractional quantity not eligible")
        check("QUANTITY_INCREMENT", i.quantity.is_positive() and i.quantity >= inst.min_quantity
              and i.quantity.is_multiple_of(inst.quantity_increment), f"qty {i.quantity}")
        check("PRICE_INCREMENT", i.limit_price.is_positive() and i.limit_price.is_multiple_of(inst.tick_for(i.limit_price)),
              f"limit {i.limit_price} tick {inst.tick_for(i.limit_price) if i.limit_price.is_positive() else '?'}")
    check("ORDER_TYPE", i.order_type in e.order_types, i.order_type)
    check("TIME_IN_FORCE", i.time_in_force in e.time_in_force, i.time_in_force)

    q = i.quote
    fresh = q is not None and q.age_ns(i.now_ns) <= i.quote_max_age_ms * 1_000_000
    check("QUOTE_FRESH", fresh, "no quote" if q is None else f"age {q.age_ns(i.now_ns) // 1_000_000} ms, source {q.source}")
    check("QUOTE_NOT_DELAYED", q is None or not q.delayed, "delayed quote cannot validate an order")
    mid = q.mid() if q is not None else None
    check("QUOTE_TWO_SIDED", mid is not None, "bid/ask unavailable or crossed")
    if mid is not None:
        bps = i.price_collar_bps
        if buy:
            cap = (mid * (10_000 + bps)).div(10_000, i.limit_price.scale + 4, rounding=_FLOOR)
            check("PRICE_COLLAR", i.limit_price <= cap, f"buy limit {i.limit_price} vs cap {cap} (mid {mid}, {bps} bps)")
        else:
            floor = (mid * (10_000 - bps)).div(10_000, i.limit_price.scale + 4, rounding=_CEILING)
            check("PRICE_COLLAR", i.limit_price >= floor, f"sell limit {i.limit_price} vs floor {floor} (mid {mid}, {bps} bps)")

    check("MAX_ORDER_NOTIONAL", i.order_notional <= i.max_order_notional, f"{i.order_notional} > {i.max_order_notional}",
          applies=buy)
    check("CASH", i.required_cash <= i.available_cash, f"required {i.required_cash} available {i.available_cash}", applies=buy)
    check("OWNED_QUANTITY", i.quantity <= i.unreserved_quantity, f"sell {i.quantity} unreserved {i.unreserved_quantity}",
          applies=not buy)
    if buy:
        limit = (i.session_equity * i.max_position_bps).div(10_000, i.session_equity.scale, rounding=_FLOOR)
        check("CONCENTRATION", i.position_notional_after <= limit, f"position after {i.position_notional_after} limit {limit}")
        check("GROSS_EXPOSURE", i.gross_exposure_after <= i.max_gross_exposure,
              f"{i.gross_exposure_after} > {i.max_gross_exposure}")
    check("ORDER_RATE", i.orders_last_minute < i.max_orders_per_minute, f"{i.orders_last_minute} in last 60s")
    if i.average_daily_volume is None:
        check("LIQUIDITY", not i.require_liquidity_data, "not evaluated: no average daily volume data")
    else:
        max_qty = (i.average_daily_volume * i.max_participation_bps_of_adv).div(10_000, 0, rounding=_FLOOR)
        check("LIQUIDITY", i.quantity <= max_qty, f"qty {i.quantity} > {i.max_participation_bps_of_adv} bps of ADV")

    failed = next((c for c in checks if not c["passed"]), None)
    return Decision(approved=failed is None, code=failed["check"] if failed else "APPROVED", checks=checks)


from ..domain.fixed import Rounding as _R  # noqa: E402

_FLOOR = _R.FLOOR
_CEILING = _R.CEILING
