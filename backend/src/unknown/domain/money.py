"""Currency registry and unit-carrying numeric types (ADR-0004).

Money, Price, Quantity and FxRate are distinct types so that unit errors such
as adding a price to cash fail loudly instead of silently producing a number.
"""
from __future__ import annotations

from dataclasses import dataclass

from .fixed import Fixed, FixedLike, Rounding


class CurrencyMismatch(ValueError):
    pass


class UnknownCurrency(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Currency:
    code: str
    ledger_scale: int   # precision at which journal postings are stored
    display_scale: int  # conventional minor-unit precision for display only
    name: str


# Registry of currencies the platform can account in. Adding a currency is a
# code change reviewed by the owner; amounts in an unregistered currency are
# rejected rather than guessed. Ledger scale 6 leaves room for broker-reported
# fractional fees; display scale follows ISO 4217 minor units.
CURRENCIES: dict[str, Currency] = {
    c.code: c for c in (
        Currency("USD", 6, 2, "US dollar"),
        Currency("EUR", 6, 2, "Euro"),
        Currency("INR", 6, 2, "Indian rupee"),
        Currency("GBP", 6, 2, "Pound sterling"),
        Currency("CHF", 6, 2, "Swiss franc"),
        Currency("CAD", 6, 2, "Canadian dollar"),
        Currency("HKD", 6, 2, "Hong Kong dollar"),
        Currency("JPY", 4, 0, "Japanese yen"),
    )
}


def currency(code: str) -> Currency:
    try:
        return CURRENCIES[code]
    except (KeyError, TypeError):
        raise UnknownCurrency(f"unregistered currency {code!r}") from None


def _fixed(value: FixedLike) -> Fixed:
    return Fixed.of(value)


@dataclass(frozen=True, slots=True)
class Money:
    currency: str
    amount: Fixed

    def __post_init__(self) -> None:
        currency(self.currency)
        if not isinstance(self.amount, Fixed):
            raise TypeError("Money.amount must be Fixed")

    @classmethod
    def of(cls, ccy: str, amount: FixedLike) -> "Money":
        return cls(ccy, _fixed(amount))

    @classmethod
    def zero(cls, ccy: str) -> "Money":
        return cls(ccy, Fixed(0, currency(ccy).ledger_scale))

    def _same(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.currency, self.amount + other.amount)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.currency, self.amount - other.amount)

    def __neg__(self) -> "Money":
        return Money(self.currency, -self.amount)

    def __abs__(self) -> "Money":
        return Money(self.currency, abs(self.amount))

    def times(self, factor: FixedLike) -> "Money":
        return Money(self.currency, self.amount * _fixed(factor))

    def __lt__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount >= other.amount

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.currency == other.currency and self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.currency, self.amount))

    def is_zero(self) -> bool:
        return self.amount.is_zero()

    def is_negative(self) -> bool:
        return self.amount.is_negative()

    def is_positive(self) -> bool:
        return self.amount.is_positive()

    def at_ledger_scale(self, rounding: Rounding = Rounding.UNNECESSARY) -> "Money":
        """Rescale to the currency's ledger scale; UNNECESSARY raises if inexact."""
        return Money(self.currency, self.amount.rescale(currency(self.currency).ledger_scale, rounding))

    def to_json(self) -> dict:
        return {"currency": self.currency, "amount": self.amount.to_str()}

    @classmethod
    def from_json(cls, data: dict) -> "Money":
        return cls(data["currency"], Fixed.parse(data["amount"]))

    def __str__(self) -> str:
        return f"{self.amount.to_str()} {self.currency}"


@dataclass(frozen=True, slots=True)
class Price:
    """Currency per one unit of an instrument (before contract multiplier)."""
    currency: str
    amount: Fixed

    def __post_init__(self) -> None:
        currency(self.currency)
        if not isinstance(self.amount, Fixed):
            raise TypeError("Price.amount must be Fixed")

    @classmethod
    def of(cls, ccy: str, amount: FixedLike) -> "Price":
        return cls(ccy, _fixed(amount))

    def to_json(self) -> dict:
        return {"currency": self.currency, "amount": self.amount.to_str()}

    @classmethod
    def from_json(cls, data: dict) -> "Price":
        return cls(data["currency"], Fixed.parse(data["amount"]))

    def __str__(self) -> str:
        return f"{self.amount.to_str()} {self.currency}/unit"


@dataclass(frozen=True, slots=True)
class Quantity:
    """A count of instrument units; sign carries direction only where documented."""
    amount: Fixed

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Fixed):
            raise TypeError("Quantity.amount must be Fixed")

    @classmethod
    def of(cls, amount: FixedLike) -> "Quantity":
        return cls(_fixed(amount))

    def __add__(self, other: "Quantity") -> "Quantity":
        if not isinstance(other, Quantity):
            raise TypeError("Quantity + non-Quantity")
        return Quantity(self.amount + other.amount)

    def __sub__(self, other: "Quantity") -> "Quantity":
        if not isinstance(other, Quantity):
            raise TypeError("Quantity - non-Quantity")
        return Quantity(self.amount - other.amount)

    def __neg__(self) -> "Quantity":
        return Quantity(-self.amount)

    def __lt__(self, other: "Quantity") -> bool:
        return self.amount < other.amount

    def __le__(self, other: "Quantity") -> bool:
        return self.amount <= other.amount

    def __gt__(self, other: "Quantity") -> bool:
        return self.amount > other.amount

    def __ge__(self, other: "Quantity") -> bool:
        return self.amount >= other.amount

    def is_zero(self) -> bool:
        return self.amount.is_zero()

    def is_positive(self) -> bool:
        return self.amount.is_positive()

    def to_str(self) -> str:
        return self.amount.to_str()


@dataclass(frozen=True, slots=True)
class FxRate:
    """``amount`` units of ``quote`` per one unit of ``base``."""
    base: str
    quote: str
    amount: Fixed

    def __post_init__(self) -> None:
        currency(self.base)
        currency(self.quote)
        if self.base == self.quote:
            raise ValueError("FxRate base and quote must differ")
        if not isinstance(self.amount, Fixed) or not self.amount.is_positive():
            raise ValueError("FxRate must be a positive Fixed")

    def convert(self, money: Money) -> Money:
        if money.currency != self.base:
            raise CurrencyMismatch(f"rate converts {self.base}, got {money.currency}")
        return Money(self.quote, money.amount * self.amount)

    def inverse(self, scale: int, rounding: Rounding) -> "FxRate":
        return FxRate(self.quote, self.base, Fixed(1).div(self.amount, scale, rounding))


def notional(price: Price, quantity: Quantity, multiplier: Fixed) -> Money:
    """Exact trade value N = p * q * M (book eq. 3.1); no rounding is applied."""
    return Money(price.currency, price.amount * quantity.amount * multiplier)
