"""Transaction cost model for backtests (CAP-RES-05; RES-03; book ch. 7; BR-DAT-20).

Fees are exact ``Fixed`` amounts at the USD ledger scale (6 dp). Price costs
(half-spread, slippage, square-root impact) are a *fraction of price* that
moves the simulated fill price; that fraction is a statistical estimate (a
float), crosses into exact arithmetic once via ``float_to_fixed`` rounded UP
to 1e-8, and the resulting fill price is then snapped to the instrument tick
against the trader (up for buys, down for sells).

Schedules (values from the IBKR US-stock commission page as recorded in the
capability register, fetched 2026-10-22; re-verify before any policy use):

* ``fee.ibkr.us.fixed@2026-10-22`` - C = min(max(0.005 x shares, 1.00), 1% of trade value) per order.
* ``fee.ibkr.us.tiered@2026-10-22`` - marginal per-share rates by calendar-month
  volume (<=300,000: 0.0035; 300,001-3,000,000: 0.0020; -20,000,000: 0.0015;
  -100,000,000: 0.0010; above: 0.0005), per order minimum 0.35 and maximum 1%
  of trade value, before third-party fees.

Third-party (regulatory/exchange) fees are UNVERIFIED in this pass. Following
CAP-RES-05 ("unknown components default to a conservative estimate rather than
zero") the model charges ``third_party_sell_bps`` of sell notional, an explicit
assumption carried in every spec and manifest, never a sourced fact.

Impact (book ch. 7): I = Y x sigma x sqrt(Q / V), "a scenario model, not a
universal law". sigma is the decision-time daily log-return volatility, Q the
fill quantity and V the execution bar's volume. Several Y values are always
evaluated (spec ``impact_y_values``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from ..domain.fixed import Fixed, Rounding, fmax, fmin
from .common import float_to_fixed

LEDGER_SCALE = 6
FRACTION_SCALE = 8
BPS = Fixed(10_000)


def _ceil6(x: Fixed) -> Fixed:
    return x.rescale(LEDGER_SCALE, Rounding.CEILING) if x.scale > LEDGER_SCALE else x.rescale(LEDGER_SCALE)


@dataclass(frozen=True)
class IbkrFixedUS:
    schedule_id: str = "fee.ibkr.us.fixed@2026-10-22"
    per_share: Fixed = Fixed.parse("0.005")
    minimum: Fixed = Fixed.parse("1.00")
    max_fraction_of_value: Fixed = Fixed.parse("0.01")
    source: str = "https://www.interactivebrokers.com/en/pricing/commissions-stocks.php (capability record CAP-RES-05, fetched 2026-10-22)"
    currency: str = "USD"          # the schedule covers USD-traded US stocks only (CAP-RES-05)

    def commission(self, quantity: Fixed, price: Fixed, month_volume_before: int = 0) -> Fixed:
        base = fmax(self.per_share * quantity, self.minimum)
        cap = self.max_fraction_of_value * quantity * price
        return _ceil6(fmin(base, cap))

    def envelope(self, quantity: Fixed) -> Fixed:
        """Uncapped estimate: never below the charged commission (the cap only lowers it)."""
        return _ceil6(fmax(self.per_share * quantity, self.minimum))

    def describe(self) -> dict:
        return {"schedule_id": self.schedule_id, "per_share": self.per_share.to_str(), "minimum": self.minimum.to_str(),
                "max_fraction_of_value": self.max_fraction_of_value.to_str(), "source": self.source, "currency": self.currency,
                "third_party_fees": "not in commission; see cost model third_party_sell_bps (UNVERIFIED assumption)"}


TIERS: tuple[tuple[int | None, Fixed], ...] = (
    (300_000, Fixed.parse("0.0035")),
    (3_000_000, Fixed.parse("0.0020")),
    (20_000_000, Fixed.parse("0.0015")),
    (100_000_000, Fixed.parse("0.0010")),
    (None, Fixed.parse("0.0005")),
)


@dataclass(frozen=True)
class IbkrTieredUS:
    schedule_id: str = "fee.ibkr.us.tiered@2026-10-22"
    minimum: Fixed = Fixed.parse("0.35")
    max_fraction_of_value: Fixed = Fixed.parse("0.01")
    source: str = "https://www.interactivebrokers.com/en/pricing/commissions-stocks.php (capability record CAP-RES-05, fetched 2026-10-22)"
    currency: str = "USD"

    @staticmethod
    def base_commission(quantity: Fixed, month_volume_before: int) -> Fixed:
        """Marginal tiered per-share charge for ``quantity`` shares after ``month_volume_before`` this month."""
        if not quantity.is_multiple_of(Fixed(1)):
            raise ValueError("tiered schedule needs whole shares")
        remaining = int(quantity.rescale(0).mantissa)
        position = month_volume_before
        total = Fixed(0, 4)
        for upper, rate in TIERS:
            if remaining <= 0:
                break
            if upper is not None and position >= upper:
                continue
            room = remaining if upper is None else min(remaining, upper - position)
            total = total + rate * room
            remaining -= room
            position += room
        return total

    def commission(self, quantity: Fixed, price: Fixed, month_volume_before: int = 0) -> Fixed:
        base = fmax(self.base_commission(quantity, month_volume_before), self.minimum)
        return _ceil6(fmin(base, self.max_fraction_of_value * quantity * price))

    def envelope(self, quantity: Fixed) -> Fixed:
        return _ceil6(fmax(TIERS[0][1] * quantity, self.minimum))

    def describe(self) -> dict:
        return {"schedule_id": self.schedule_id, "tiers": [[u, r.to_str()] for u, r in TIERS],
                "minimum": self.minimum.to_str(), "max_fraction_of_value": self.max_fraction_of_value.to_str(),
                "source": self.source, "currency": self.currency,
                "application": "marginal within the calendar month of the trade date"}


SCHEDULES = {s.schedule_id: s for s in (IbkrFixedUS(), IbkrTieredUS())}


@dataclass(frozen=True)
class FeeBreakdown:
    commission: Fixed
    third_party: Fixed

    @property
    def total(self) -> Fixed:
        return self.commission + self.third_party

    def to_json(self) -> dict:
        return {"commission": self.commission.to_str(), "third_party": self.third_party.to_str(), "total": self.total.to_str()}


@dataclass(frozen=True)
class PriceCost:
    fraction: Fixed            # total per-side price cost, fraction of price (scale 8, rounded up)
    half_spread: Fixed
    slippage: Fixed
    impact: Fixed
    sigma: float
    participation: float

    def to_json(self) -> dict:
        return {"fraction": self.fraction.to_str(), "half_spread": self.half_spread.to_str(), "slippage": self.slippage.to_str(),
                "impact": self.impact.to_str(), "sigma": self.sigma, "q_over_v": self.participation,
                "formula": ["cost.price_fraction@1", "impact.sqrt@1"]}


@dataclass(frozen=True)
class CostModel:
    schedule: IbkrFixedUS | IbkrTieredUS
    spread_bps: Fixed
    slippage_bps: Fixed
    impact_y: Fixed
    third_party_sell_bps: Fixed
    multiplier: Fixed = Fixed(1)
    model_version: str = "cost.equity.us@1"

    @classmethod
    def from_spec(cls, spec, *, multiplier: Fixed = Fixed(1), impact_y: Fixed | None = None) -> "CostModel":
        c = spec.costs
        return cls(schedule=SCHEDULES[c["fee_schedule_id"]], spread_bps=c["spread_bps"], slippage_bps=c["slippage_bps"],
                   impact_y=c["impact_y_default"] if impact_y is None else impact_y,
                   third_party_sell_bps=c["third_party_sell_bps"], multiplier=multiplier)

    def scaled(self, k: Fixed) -> "CostModel":
        return replace(self, multiplier=self.multiplier * k)

    def with_impact(self, y: Fixed) -> "CostModel":
        return replace(self, impact_y=y)

    def describe(self) -> dict:
        return {"model_version": self.model_version, "schedule": self.schedule.describe(),
                "spread_bps": self.spread_bps.to_str(), "slippage_bps": self.slippage_bps.to_str(),
                "impact_y": self.impact_y.to_str(), "third_party_sell_bps": self.third_party_sell_bps.to_str(),
                "third_party_status": "UNVERIFIED conservative assumption (CAP-RES-05 degraded rule)",
                "multiplier": self.multiplier.to_str()}

    # --------------------------------------------------------------- fees
    def fees(self, side: str, quantity: Fixed, price: Fixed, month_volume_before: int = 0) -> FeeBreakdown:
        commission = _ceil6(self.schedule.commission(quantity, price, month_volume_before) * self.multiplier)
        third = Fixed(0, LEDGER_SCALE)
        if side == "SELL" and self.third_party_sell_bps.is_positive():
            third = (quantity * price * self.third_party_sell_bps * self.multiplier).div(BPS, LEDGER_SCALE, Rounding.CEILING)
        return FeeBreakdown(commission, third)

    def core_fee_model(self):
        """This schedule as the core's ``policy.model.FeeModel``, so ``risk.fees.envelope`` (the function the core
        reserves fees with) prices it: per share (the tiered schedule's first-tier rate, as its envelope does), the
        per-order minimum and the cap, scaled by the cost multiplier, with no estimate margin. Its envelope equals
        ``fee_envelope("BUY", ...)`` (tested). Used by the A15 risk term (BR-DAT-04)."""
        from ..domain.money import Money
        from ..policy.model import FeeModel
        s = self.schedule
        per_share = s.per_share if isinstance(s, IbkrFixedUS) else TIERS[0][1]
        cap = s.max_fraction_of_value * 10_000
        if not cap.is_multiple_of(Fixed(1)):
            raise ValueError("fee cap is not a whole number of basis points")
        return FeeModel(per_share=per_share * self.multiplier,
                        minimum=Money(s.currency, _ceil6(s.minimum * self.multiplier)),
                        maximum_bps_of_notional=int(cap.rescale(0).mantissa), estimate_margin_bps=0,
                        source=f"{s.schedule_id} x{self.multiplier.to_str()} (research cost model)",
                        verified_on="2026-10-22")

    def fee_envelope(self, side: str, quantity: Fixed, limit_price: Fixed) -> Fixed:
        """Reservation envelope: >= any fee the fill can be charged (uncapped commission, sell fees at the limit)."""
        env = _ceil6(self.schedule.envelope(quantity) * self.multiplier)
        if side == "SELL" and self.third_party_sell_bps.is_positive():
            env = env + (quantity * limit_price * self.third_party_sell_bps * self.multiplier).div(BPS, LEDGER_SCALE, Rounding.CEILING)
        return env

    # --------------------------------------------------------- price costs
    def price_cost(self, sigma: float, quantity: Fixed, volume: Fixed) -> PriceCost:
        if not volume.is_positive():
            raise ValueError("impact needs positive bar volume")
        if not math.isfinite(sigma) or sigma < 0:
            raise ValueError("sigma must be a finite non-negative float")
        half_spread = self.spread_bps.div(2 * 10_000, FRACTION_SCALE + 2, Rounding.CEILING)
        slippage = self.slippage_bps.div(10_000, FRACTION_SCALE + 2, Rounding.CEILING)
        q_over_v = (quantity.mantissa / 10 ** quantity.scale) / (volume.mantissa / 10 ** volume.scale)
        y = self.impact_y.mantissa / 10 ** self.impact_y.scale
        impact = float_to_fixed(y * sigma * math.sqrt(q_over_v), FRACTION_SCALE, Rounding.CEILING)
        total = ((half_spread + slippage + impact) * self.multiplier).rescale(FRACTION_SCALE, Rounding.CEILING)
        return PriceCost(total, half_spread, slippage, impact, sigma, q_over_v)
