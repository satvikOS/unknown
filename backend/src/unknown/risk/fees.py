"""Fee estimation for reservations. Estimates are conservative envelopes; the
broker's commission report is the final amount (LED-07)."""
from __future__ import annotations

from ..domain.fixed import Fixed, Rounding, fmax, fmin
from ..domain.money import Money, currency
from ..policy.model import FeeModel


def estimate(model: FeeModel, quantity: Fixed, notional: Money, *, apply_cap: bool = True) -> Money:
    """Per-share commission with a minimum and an optional percentage cap, at ledger scale (CEILING).

    The cap can lower the fee below the minimum on very small trades; the
    reservation envelope therefore ignores the cap so it never under-reserves."""
    scale = currency(notional.currency).ledger_scale
    fee = fmax(model.per_share * quantity, model.minimum.amount)
    if apply_cap and model.maximum_bps_of_notional:
        cap = (notional.amount * model.maximum_bps_of_notional).div(10_000, scale, Rounding.CEILING)
        fee = fmin(fee, fmax(cap, Fixed(0)))
    return Money(notional.currency, fee.rescale(scale, Rounding.CEILING))


def envelope(model: FeeModel, quantity: Fixed, notional: Money) -> Money:
    """Reserved fee envelope: uncapped estimate plus a margin, rounded up (ADR-0004 #4)."""
    est = estimate(model, quantity, notional, apply_cap=False)
    scale = currency(est.currency).ledger_scale
    margin = (est.amount * model.estimate_margin_bps).div(10_000, scale, Rounding.CEILING)
    return Money(est.currency, est.amount + margin)
