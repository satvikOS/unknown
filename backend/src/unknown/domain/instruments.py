"""Instrument master records (book Ch. 3; DOM-03, DOM-06).

A display ticker is never a key. ``Instrument.instrument_id`` is internal and
immutable; provider identifiers (IBKR conId, FIGI, ISIN, NSE token...) are
versioned ``ProviderMapping`` children. Asset classes beyond cash equities
are representable so research can use them, but eligibility to trade them is
a separate policy decision (ADR-0009).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .fixed import Fixed, Rounding
from .ids import validate_id
from .money import Price, Quantity, currency


class AssetClass(enum.Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    FUND = "FUND"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FX = "FX"
    BOND = "BOND"
    CRYPTO = "CRYPTO"
    INDEX = "INDEX"      # reference only; not tradable
    CFD = "CFD"
    STRUCTURED = "STRUCTURED"


class InstrumentStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"


@dataclass(frozen=True, slots=True)
class Venue:
    mic: str              # ISO 10383 market identifier code
    name: str
    country: str          # ISO 3166-1 alpha-2
    timezone: str         # IANA zone for session rules
    calendar_id: str      # versioned trading calendar used for sessions and settlement


@dataclass(frozen=True, slots=True)
class TickRule:
    """Applies to prices >= ``from_price`` (until the next rule's from_price)."""
    from_price: Fixed
    tick: Fixed

    def __post_init__(self) -> None:
        if not self.tick.is_positive() or self.from_price.is_negative():
            raise ValueError("tick must be positive and from_price non-negative")


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    asset_class: AssetClass
    symbol: str                      # display only; may be reused by another issuer over time
    name: str
    currency: str                    # trading currency
    settlement_currency: str
    venue_mic: str
    quantity_increment: Fixed        # lot / fractional increment
    min_quantity: Fixed
    multiplier: Fixed
    settlement_days: int             # business days after trade date under the venue calendar
    tick_rules: tuple[TickRule, ...]
    leveraged: bool = False
    inverse: bool = False
    status: InstrumentStatus = InstrumentStatus.ACTIVE
    metadata_version: int = 1
    attributes: dict = field(default_factory=dict)   # namespaced extras, e.g. {"ibkr.primaryExchange": "NASDAQ"}

    def __post_init__(self) -> None:
        validate_id(self.instrument_id, "instrument id")
        currency(self.currency)
        currency(self.settlement_currency)
        if not self.quantity_increment.is_positive() or not self.min_quantity.is_positive():
            raise ValueError("quantity increment and minimum must be positive")
        if not self.multiplier.is_positive():
            raise ValueError("multiplier must be positive")
        if not self.tick_rules:
            raise ValueError("tick rules are required; missing tick metadata rejects orders")
        froms = [r.from_price for r in self.tick_rules]
        if froms != sorted(froms) or len(set(froms)) != len(froms) or not froms[0].is_zero():
            raise ValueError("tick rules must start at 0 and be strictly increasing")
        if self.settlement_days < 0:
            raise ValueError("settlement_days must be >= 0")

    def tick_for(self, price: Fixed) -> Fixed:
        tick = self.tick_rules[0].tick
        for rule in self.tick_rules:
            if price >= rule.from_price:
                tick = rule.tick
        return tick

    def is_valid_price(self, price: Price) -> bool:
        return (price.currency == self.currency and price.amount.is_positive()
                and price.amount.is_multiple_of(self.tick_for(price.amount)))

    def round_price(self, amount: Fixed, rounding: Rounding) -> Fixed:
        return amount.to_increment(self.tick_for(amount), rounding)

    def is_valid_quantity(self, quantity: Quantity) -> bool:
        q = quantity.amount
        return q.is_positive() and q >= self.min_quantity and q.is_multiple_of(self.quantity_increment)

    def to_json(self) -> dict:
        return {
            "instrument_id": self.instrument_id,
            "asset_class": self.asset_class.value,
            "symbol": self.symbol,
            "name": self.name,
            "currency": self.currency,
            "settlement_currency": self.settlement_currency,
            "venue_mic": self.venue_mic,
            "quantity_increment": self.quantity_increment.to_str(),
            "min_quantity": self.min_quantity.to_str(),
            "multiplier": self.multiplier.to_str(),
            "settlement_days": self.settlement_days,
            "tick_rules": [{"from_price": r.from_price.to_str(), "tick": r.tick.to_str()} for r in self.tick_rules],
            "leveraged": self.leveraged,
            "inverse": self.inverse,
            "status": self.status.value,
            "metadata_version": self.metadata_version,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_json(cls, d: dict) -> "Instrument":
        return cls(
            instrument_id=d["instrument_id"],
            asset_class=AssetClass(d["asset_class"]),
            symbol=d["symbol"],
            name=d["name"],
            currency=d["currency"],
            settlement_currency=d["settlement_currency"],
            venue_mic=d["venue_mic"],
            quantity_increment=Fixed.parse(d["quantity_increment"]),
            min_quantity=Fixed.parse(d["min_quantity"]),
            multiplier=Fixed.parse(d["multiplier"]),
            settlement_days=int(d["settlement_days"]),
            tick_rules=tuple(TickRule(Fixed.parse(r["from_price"]), Fixed.parse(r["tick"])) for r in d["tick_rules"]),
            leveraged=bool(d.get("leveraged", False)),
            inverse=bool(d.get("inverse", False)),
            status=InstrumentStatus(d.get("status", "ACTIVE")),
            metadata_version=int(d.get("metadata_version", 1)),
            attributes=dict(d.get("attributes", {})),
        )


@dataclass(frozen=True, slots=True)
class ProviderMapping:
    provider: str                 # e.g. "ibkr", "openfigi", "nse"
    external_id: str              # provider's identifier (conId, FIGI, token)
    instrument_id: str
    version: int
    verified_at_ns: int | None    # None = never verified against provider contract metadata
    details: dict = field(default_factory=dict)
