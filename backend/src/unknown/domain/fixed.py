"""Exact decimal arithmetic without a hidden context (ADR-0004).

``Fixed`` is an immutable value ``mantissa * 10**-scale`` with an unbounded
integer mantissa. Addition and subtraction align scales exactly, multiplication
adds scales, and every operation that could lose information (division,
reducing scale, snapping to an increment) takes an explicit ``Rounding``.
Floats are refused at every entry point.
"""
from __future__ import annotations

import enum
import re
from decimal import Decimal
from typing import Union

MAX_SCALE = 18

_NUMBER = re.compile(r"^([+-]?)(\d+)(?:\.(\d+))?$")


class InexactError(ArithmeticError):
    """An operation with ``Rounding.UNNECESSARY`` would have discarded digits."""


class Rounding(enum.Enum):
    UP = "UP"                    # away from zero
    DOWN = "DOWN"                # toward zero
    CEILING = "CEILING"          # toward +infinity
    FLOOR = "FLOOR"              # toward -infinity
    HALF_EVEN = "HALF_EVEN"
    HALF_UP = "HALF_UP"          # half away from zero
    UNNECESSARY = "UNNECESSARY"  # raise InexactError instead of rounding


def round_div(numerator: int, denominator: int, rounding: Rounding) -> int:
    """Integer quotient of numerator/denominator under ``rounding``."""
    if type(numerator) is not int or type(denominator) is not int:
        raise TypeError("round_div requires int operands")
    if denominator == 0:
        raise ZeroDivisionError("division by zero")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    q, r = divmod(numerator, denominator)  # floor division, 0 <= r < denominator
    if r == 0:
        return q
    if rounding is Rounding.FLOOR:
        return q
    if rounding is Rounding.CEILING:
        return q + 1
    if rounding is Rounding.DOWN:
        return q if numerator >= 0 else q + 1
    if rounding is Rounding.UP:
        return q + 1 if numerator >= 0 else q
    twice = 2 * r
    if rounding is Rounding.HALF_EVEN:
        if twice != denominator:
            return q + 1 if twice > denominator else q
        return q if q % 2 == 0 else q + 1
    if rounding is Rounding.HALF_UP:
        if twice != denominator:
            return q + 1 if twice > denominator else q
        return q + 1 if numerator >= 0 else q
    if rounding is Rounding.UNNECESSARY:
        raise InexactError(f"{numerator}/{denominator} is not exact")
    raise TypeError(f"unknown rounding {rounding!r}")


FixedLike = Union["Fixed", int, str]


class Fixed:
    __slots__ = ("_m", "_s")

    def __init__(self, mantissa: int, scale: int = 0):
        if type(mantissa) is not int:
            raise TypeError("Fixed mantissa must be int (not bool, float or Decimal)")
        if type(scale) is not int or not 0 <= scale <= MAX_SCALE:
            raise ValueError(f"Fixed scale must be an int in [0, {MAX_SCALE}]")
        self._m = mantissa
        self._s = scale

    # ------------------------------------------------------------------ build
    @classmethod
    def parse(cls, text: str) -> "Fixed":
        """Parse a plain decimal string exactly ("-12.340" -> scale 3)."""
        if not isinstance(text, str):
            raise TypeError("Fixed.parse requires str")
        match = _NUMBER.match(text.strip())
        if not match:
            raise ValueError(f"not a plain decimal number: {text!r}")
        sign, whole, frac = match.groups()
        frac = frac or ""
        if len(frac) > MAX_SCALE:
            raise ValueError(f"more than {MAX_SCALE} fractional digits: {text!r}")
        mantissa = int(whole + frac)
        return cls(-mantissa if sign == "-" else mantissa, len(frac))

    @classmethod
    def of(cls, value: FixedLike) -> "Fixed":
        if isinstance(value, Fixed):
            return value
        if type(value) is int:
            return cls(value, 0)
        if isinstance(value, str):
            return cls.parse(value)
        raise TypeError(f"cannot build Fixed from {type(value).__name__}")

    @classmethod
    def from_decimal(cls, value: Decimal) -> "Fixed":
        if not isinstance(value, Decimal) or not value.is_finite():
            raise TypeError("from_decimal requires a finite Decimal")
        sign, digits, exponent = value.as_tuple()
        mantissa = int("".join(map(str, digits)) or "0")
        if sign:
            mantissa = -mantissa
        if exponent >= 0:
            return cls(mantissa * 10 ** exponent, 0)
        if -exponent > MAX_SCALE:
            raise ValueError("Decimal has more than MAX_SCALE fractional digits")
        return cls(mantissa, -exponent)

    @classmethod
    def from_float_repr(cls, value: float) -> "Fixed":
        """Adapter-boundary conversion of a broker-supplied float via ``repr``.

        Only for ingesting third-party APIs that emit binary floats (ADR-0004 #5).
        The caller must retain the raw value and validate against tick/lot rules.
        """
        if type(value) is not float:
            raise TypeError("from_float_repr requires float")
        text = repr(value)
        if "e" in text or "E" in text or text in ("nan", "inf", "-inf"):
            return cls.from_decimal(Decimal(text))
        return cls.parse(text)

    # ------------------------------------------------------------- accessors
    @property
    def mantissa(self) -> int:
        return self._m

    @property
    def scale(self) -> int:
        return self._s

    def normalized(self) -> "Fixed":
        m, s = self._m, self._s
        while s > 0 and m % 10 == 0:
            m //= 10
            s -= 1
        return Fixed(m, s)

    def is_zero(self) -> bool:
        return self._m == 0

    def is_negative(self) -> bool:
        return self._m < 0

    def is_positive(self) -> bool:
        return self._m > 0

    def sign(self) -> int:
        return (self._m > 0) - (self._m < 0)

    # ------------------------------------------------------------- arithmetic
    @staticmethod
    def _coerce(other: object) -> "Fixed":
        if isinstance(other, Fixed):
            return other
        if type(other) is int:
            return Fixed(other, 0)
        raise TypeError(f"unsupported operand type for Fixed: {type(other).__name__}")

    def _aligned(self, other: "Fixed") -> tuple[int, int, int]:
        scale = max(self._s, other._s)
        return self._m * 10 ** (scale - self._s), other._m * 10 ** (scale - other._s), scale

    def __add__(self, other: object) -> "Fixed":
        a, b, s = self._aligned(self._coerce(other))
        return Fixed(a + b, s)

    __radd__ = __add__

    def __sub__(self, other: object) -> "Fixed":
        a, b, s = self._aligned(self._coerce(other))
        return Fixed(a - b, s)

    def __rsub__(self, other: object) -> "Fixed":
        return self._coerce(other) - self

    def __mul__(self, other: object) -> "Fixed":
        o = self._coerce(other)
        scale = self._s + o._s
        if scale > MAX_SCALE:
            raise ValueError("product scale exceeds MAX_SCALE; rescale an operand first")
        return Fixed(self._m * o._m, scale)

    __rmul__ = __mul__

    def __neg__(self) -> "Fixed":
        return Fixed(-self._m, self._s)

    def __pos__(self) -> "Fixed":
        return self

    def __abs__(self) -> "Fixed":
        return Fixed(abs(self._m), self._s)

    def __truediv__(self, other: object):  # pragma: no cover - guard
        raise TypeError("use Fixed.div(other, scale, rounding); implicit division is forbidden")

    def div(self, other: FixedLike, scale: int, rounding: Rounding) -> "Fixed":
        o = Fixed.of(other)
        if o._m == 0:
            raise ZeroDivisionError("Fixed division by zero")
        # self/o = (m1/10^s1)/(m2/10^s2); target mantissa = that * 10^scale
        numerator = self._m * 10 ** (scale + o._s)
        denominator = o._m * 10 ** self._s
        return Fixed(round_div(numerator, denominator, rounding), scale)

    def rescale(self, scale: int, rounding: Rounding = Rounding.UNNECESSARY) -> "Fixed":
        if type(scale) is not int or not 0 <= scale <= MAX_SCALE:
            raise ValueError("invalid target scale")
        if scale >= self._s:
            return Fixed(self._m * 10 ** (scale - self._s), scale)
        return Fixed(round_div(self._m, 10 ** (self._s - scale), rounding), scale)

    def to_increment(self, increment: "Fixed", rounding: Rounding) -> "Fixed":
        """Snap to an integer multiple of a positive ``increment`` (tick or lot)."""
        if not increment.is_positive():
            raise ValueError("increment must be positive")
        a, b, _ = self._aligned(increment)
        k = round_div(a, b, rounding)
        # k * increment is exactly representable at the increment's own scale.
        return Fixed(k * increment._m, increment._s)

    def is_multiple_of(self, increment: "Fixed") -> bool:
        if not increment.is_positive():
            raise ValueError("increment must be positive")
        a, b, _ = self._aligned(increment)
        return a % b == 0

    # ------------------------------------------------------------- comparison
    def _cmp(self, other: object) -> int:
        a, b, _ = self._aligned(self._coerce(other))
        return (a > b) - (a < b)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Fixed) or type(other) is int:
            return self._cmp(other) == 0
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        return self._cmp(other) < 0

    def __le__(self, other: object) -> bool:
        return self._cmp(other) <= 0

    def __gt__(self, other: object) -> bool:
        return self._cmp(other) > 0

    def __ge__(self, other: object) -> bool:
        return self._cmp(other) >= 0

    def __hash__(self) -> int:
        n = self.normalized()
        return hash(("Fixed", n._m, n._s))

    def __bool__(self) -> bool:
        return self._m != 0

    def __float__(self):  # pragma: no cover - guard
        raise TypeError("Fixed cannot be converted to float")

    # ------------------------------------------------------------- text
    def to_str(self) -> str:
        """Canonical text preserving scale: Fixed(-1050, 3) -> '-1.050'."""
        digits = str(abs(self._m))
        sign = "-" if self._m < 0 else ""
        if self._s == 0:
            return sign + digits
        digits = digits.rjust(self._s + 1, "0")
        return f"{sign}{digits[:-self._s]}.{digits[-self._s:]}"

    def __str__(self) -> str:
        return self.to_str()

    def __repr__(self) -> str:
        return f"Fixed('{self.to_str()}')"

    def __reduce__(self):
        return (Fixed, (self._m, self._s))


def fmax(a: Fixed, b: Fixed) -> Fixed:
    return a if a >= b else b


def fmin(a: Fixed, b: Fixed) -> Fixed:
    return a if a <= b else b


ZERO = Fixed(0, 0)
