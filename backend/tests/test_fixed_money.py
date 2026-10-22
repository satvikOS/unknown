"""Exact numeric representation, money types, canonical hashing and identifiers.

Requirements exercised: DOM-02 (exact money/price/quantity/FX types with
explicit currency and scale; no binary floats), ADR-0004 #1-#6 (Fixed,
rounding modes, typed wrappers, ledger scale, adapter float boundary,
canonical text), ORD-01 / ADR-0005 #4 / ADR-0007 (payload, policy and mandate
hashes over a fixed canonical encoding), DOM-04 / ADR-0005 #1 (UUIDv7 event
identity) and REC-01 (reconciliation over broker amounts at ledger scale).

``decimal.Decimal`` is used ONLY as an independent oracle, always through an
explicit local ``Context`` with a very large precision so no hidden context
rounding can leak into an expected value. Division oracles first round with
``ROUND_05UP`` at 1000 digits, which makes the second rounding to the target
scale exact for any mode (the standard double-rounding-safe construction).

Every number here is a TEST FIXTURE, not market data.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pickle
import random
import sys
import threading
import unittest
import uuid
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402

from unknown.core import ledger  # noqa: E402
from unknown.domain.canonical import canonical_json, content_hash, sha256_hex  # noqa: E402
from unknown.domain.errors import InvariantViolation  # noqa: E402
from unknown.domain.fixed import MAX_SCALE, Fixed, InexactError, Rounding, round_div  # noqa: E402
from unknown.domain.ids import IdGenerator, InvalidIdentifier, SequenceRandom, validate_id  # noqa: E402
from unknown.domain.money import (  # noqa: E402
    CURRENCIES,
    CurrencyMismatch,
    FxRate,
    Money,
    Price,
    Quantity,
    UnknownCurrency,
    currency,
    notional,
)

F = Fixed.parse

# Every rounding mode that actually rounds, mapped to its Decimal oracle.
ORACLE_MODES = {
    Rounding.UP: ROUND_UP,
    Rounding.DOWN: ROUND_DOWN,
    Rounding.CEILING: ROUND_CEILING,
    Rounding.FLOOR: ROUND_FLOOR,
    Rounding.HALF_EVEN: ROUND_HALF_EVEN,
    Rounding.HALF_UP: ROUND_HALF_UP,
}
ADR_0004_MODES = ("UP", "DOWN", "CEILING", "FLOOR", "HALF_EVEN", "UNNECESSARY")
FIAT = ("USD", "EUR", "INR", "GBP", "CHF", "CAD", "HKD", "JPY")


# ---------------------------------------------------------------- oracle helpers
def _ctx(rounding: str = ROUND_HALF_EVEN, prec: int = 2000) -> Context:
    return Context(prec=prec, rounding=rounding, Emax=MAX_EMAX, Emin=MIN_EMIN,
                   traps=[InvalidOperation, DivisionByZero, Overflow])


def dec(x: Fixed) -> Decimal:
    """Exact Decimal for a Fixed (string construction never consults a context)."""
    return Decimal(f"{x.mantissa}E-{x.scale}")


def _fixed_at(q: Decimal, scale: int) -> Fixed:
    sign, digits, exponent = q.as_tuple()
    assert exponent == -scale, (q, scale)
    m = int("".join(map(str, digits)))
    return Fixed(-m if sign else m, scale)


def oracle_quantize(value: Decimal, scale: int, mode: str) -> tuple[Fixed, bool]:
    """(value rounded to ``scale`` under ``mode``, whether nonzero digits were discarded)."""
    ctx = _ctx()
    q = value.quantize(Decimal(f"1E-{scale}"), rounding=mode, context=ctx)
    return _fixed_at(q, scale), bool(ctx.flags[Inexact])


def oracle_div(a: Fixed, b: Fixed, scale: int, mode: str) -> tuple[Fixed, bool]:
    ctx = _ctx(rounding=ROUND_05UP, prec=1000)
    q = ctx.divide(dec(a), dec(b))
    inexact_first = bool(ctx.flags[Inexact])
    result, inexact_second = oracle_quantize(q, scale, mode)
    return result, inexact_first or inexact_second


def rand_fixed(rng: random.Random, *, max_digits: int = 30, max_scale: int = MAX_SCALE,
               nonzero: bool = False, positive: bool = False) -> Fixed:
    roll = rng.random()
    if roll < 0.05 and not nonzero:
        m = 0
    elif roll < 0.25:
        m = rng.randint(1, 99)
    else:
        digits = rng.randint(1, max_digits)
        m = rng.randrange(10 ** (digits - 1), 10 ** digits)
    if not positive and rng.random() < 0.5:
        m = -m
    return Fixed(m, rng.randint(0, max_scale))


class _Case(unittest.TestCase):
    def assertSameFixed(self, actual: Fixed, expected: Fixed, msg: str = "") -> None:
        """Value AND scale must match (Fixed.__eq__ alone is scale-insensitive)."""
        self.assertIsInstance(actual, Fixed, msg)
        self.assertEqual((actual.mantissa, actual.scale), (expected.mantissa, expected.scale),
                         f"{msg}: got {actual!r}, expected {expected!r}")


# ===================================================================== text
class FixedTextTests(_Case):
    def test_parse_str_round_trip_matches_decimal_oracle(self):
        """DOM-02 / ADR-0004 #6: canonical TEXT has no exponent, keeps exactly the
        value's scale, and parses back to the identical mantissa and scale."""
        rng = random.Random(4001)
        for i in range(4000):
            x = rand_fixed(rng, max_digits=40)
            text = x.to_str()
            self.assertEqual(text, str(x))
            self.assertEqual(text, format(dec(x), "f"), f"case {i}: {x.mantissa}e-{x.scale}")
            self.assertNotIn("e", text.lower())
            frac = text.partition(".")[2]
            self.assertEqual(len(frac), x.scale, text)
            self.assertSameFixed(Fixed.parse(text), x, f"case {i}")
            self.assertSameFixed(Fixed.from_decimal(Decimal(text)), x, f"case {i}")

    def test_parse_preserves_scale_and_sign(self):
        """ADR-0004 #1: parse is exact and scale-preserving; zero is never negative in text."""
        cases = {
            "-12.340": (-12340, 3), "+1.50": (150, 2), "007.50": (750, 2), "0": (0, 0),
            "-0.00": (0, 2), "0.000000000000000001": (1, 18), "  2  ": (2, 0),
            "123456789012345678901234567890.123456789012345678": (123456789012345678901234567890123456789012345678, 18),
        }
        for text, (m, s) in cases.items():
            with self.subTest(text=text):
                self.assertSameFixed(F(text), Fixed(m, s), text)
        self.assertEqual(F("-0.00").to_str(), "0.00")
        self.assertEqual(Fixed(-5, 3).to_str(), "-0.005")
        self.assertEqual(repr(F("-1.050")), "Fixed('-1.050')")

    def test_parse_rejects_non_plain_text(self):
        """ADR-0004 #6: only plain decimal text, no exponent/locale/special forms; scale capped."""
        bad = ["", " ", "1e3", "1E-2", "1.5e0", ".5", "5.", "1,000", "1_000", "0x10", "nan", "NaN", "inf",
               "-inf", "Infinity", "1.2.3", "--1", "+-1", "- 1", "1 000", "$1", "1.0 USD",
               "0." + "0" * (MAX_SCALE + 1)]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    Fixed.parse(text)

    def test_parse_refuses_non_ascii_digits(self):
        """DOM-02 / ADR-0004 #6: canonical decimal TEXT is ASCII. Unicode decimal digits
        (Arabic-Indic, fullwidth, mathematical) must not be accepted as numbers: a homoglyph
        amount would parse to a value whose stored text differs from what was signed/hashed."""
        for text in ("١٢٣", "１.５", "𝟏𝟎𝟎", "1٠", "۱.00"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    Fixed.parse(text)

    def test_scale_bounds(self):
        """ADR-0004 #1: scale is a non-negative int (bounded by MAX_SCALE)."""
        self.assertEqual(Fixed(1, MAX_SCALE).scale, MAX_SCALE)
        for bad in (-1, MAX_SCALE + 1, 1.0, "2", True, None):
            with self.subTest(scale=bad):
                with self.assertRaises((ValueError, TypeError)):
                    Fixed(1, bad)

    def test_from_decimal_and_float_repr_boundary(self):
        """ADR-0004 #5: broker floats convert through repr exactly, binary noise is kept
        visible (never hidden by rounding), and non-finite values are refused."""
        self.assertSameFixed(Fixed.from_decimal(Decimal("1.50")), Fixed(150, 2))
        self.assertSameFixed(Fixed.from_decimal(Decimal("1E+3")), Fixed(1000, 0))
        self.assertSameFixed(Fixed.from_decimal(Decimal("-0")), Fixed(0, 0))
        self.assertSameFixed(Fixed.from_float_repr(0.1), Fixed(1, 1))
        self.assertSameFixed(Fixed.from_float_repr(9.99), Fixed(999, 2))
        self.assertSameFixed(Fixed.from_float_repr(0.1 + 0.2), F("0.30000000000000004"))
        self.assertSameFixed(Fixed.from_float_repr(1e-07), Fixed(1, 7))
        self.assertSameFixed(Fixed.from_float_repr(1e20), Fixed(10 ** 20, 0))
        self.assertSameFixed(Fixed.from_float_repr(-0.0), Fixed(0, 1))
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    Fixed.from_float_repr(bad)
        with self.assertRaises(ValueError):
            Fixed.from_float_repr(1e-19)  # finer than MAX_SCALE: refused, not truncated
        for bad in (1, True, "0.1", Decimal("0.1")):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    Fixed.from_float_repr(bad)
        for bad in (Decimal("NaN"), Decimal("Infinity"), 1.5, "1.5"):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    Fixed.from_decimal(bad)

    def test_immutable_and_serialisable_with_scale(self):
        """ADR-0004 #1: Fixed is an immutable value; copies keep the exact scale."""
        x = F("-1.050")
        with self.assertRaises(AttributeError):
            x.mantissa = 5  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            x.extra = 1  # type: ignore[attr-defined]
        self.assertSameFixed(pickle.loads(pickle.dumps(x)), x)
        self.assertSameFixed(copy.deepcopy(x), x)
        self.assertSameFixed(F("120.500").normalized(), Fixed(1205, 1))
        self.assertSameFixed(F("0.000").normalized(), Fixed(0, 0))


# ===================================================================== arithmetic
class FixedArithmeticOracleTests(_Case):
    def test_add_sub_mul_exact_against_decimal(self):
        """ADR-0004 #1: add/sub align scales exactly; mul adds scales; nothing is rounded."""
        rng = random.Random(4002)
        ctx = _ctx()
        for i in range(3000):
            a = rand_fixed(rng, max_scale=9)
            b = rand_fixed(rng, max_scale=9)
            s = max(a.scale, b.scale)
            self.assertSameFixed(a + b, _fixed_at(ctx.add(dec(a), dec(b)), s), f"add {i}")
            self.assertSameFixed(a - b, _fixed_at(ctx.subtract(dec(a), dec(b)), s), f"sub {i}")
            self.assertSameFixed(a * b, _fixed_at(ctx.multiply(dec(a), dec(b)), a.scale + b.scale), f"mul {i}")
            self.assertSameFixed(-a, _fixed_at(ctx.minus(dec(a)) if a.mantissa else dec(a), a.scale), f"neg {i}")
            self.assertSameFixed(abs(a), _fixed_at(ctx.abs(dec(a)), a.scale), f"abs {i}")
            self.assertSameFixed(a + 7, _fixed_at(ctx.add(dec(a), Decimal(7)), a.scale), f"add int {i}")
            self.assertSameFixed(7 - a, _fixed_at(ctx.subtract(Decimal(7), dec(a)), a.scale), f"rsub int {i}")

    def test_product_scale_overflow_is_refused_not_rounded(self):
        """ADR-0004 #1: a product whose scale exceeds MAX_SCALE raises instead of silently rounding."""
        with self.assertRaises(ValueError):
            Fixed(1, 10) * Fixed(1, 9)
        self.assertSameFixed(Fixed(3, 9) * Fixed(7, 9), Fixed(21, 18))

    def test_implicit_division_is_forbidden(self):
        """ADR-0004 #1: division requires an explicit scale and rounding mode."""
        with self.assertRaises(TypeError):
            F("1") / F("3")  # noqa: B018
        with self.assertRaises(TypeError):
            F("1") // 3  # noqa: B018
        with self.assertRaises(TypeError):
            F("1") % 3  # noqa: B018
        with self.assertRaises(ZeroDivisionError):
            F("1").div(F("0.00"), 2, Rounding.HALF_EVEN)
        with self.assertRaises(ZeroDivisionError):
            round_div(1, 0, Rounding.DOWN)


class RoundingOracleTests(_Case):
    def test_rounding_modes_cover_adr(self):
        """ADR-0004 #1: UP, DOWN, CEILING, FLOOR, HALF_EVEN and UNNECESSARY all exist."""
        names = {r.name for r in Rounding}
        for name in ADR_0004_MODES:
            self.assertIn(name, names)
        self.assertTrue(issubclass(InexactError, ArithmeticError))

    def test_rescale_every_mode_matches_decimal_over_random_values(self):
        """ADR-0004 #1: reducing scale under each explicit mode equals Decimal.quantize."""
        rng = random.Random(4003)
        for i in range(3000):
            x = rand_fixed(rng)
            target = rng.randint(0, MAX_SCALE)
            for mode, oracle in ORACLE_MODES.items():
                expected, _ = oracle_quantize(dec(x), target, oracle)
                self.assertSameFixed(x.rescale(target, mode), expected, f"case {i} {x!r} -> {target} {mode.name}")

    def test_rescale_exact_halves_and_neighbours(self):
        """ADR-0004 #1: ties (exact halves) and their neighbours, positive and negative,
        including HALF_EVEN's parity rule, match Decimal for every mode."""
        rng = random.Random(4004)
        for s in range(1, MAX_SCALE + 1):
            for t in range(0, s):
                d = s - t
                for _ in range(6):
                    k = rng.choice([0, 1, 2, 3, rng.randrange(10 ** 12), rng.randrange(10 ** 12)])
                    sign = rng.choice([1, -1])
                    half = k * 10 ** d + 5 * 10 ** (d - 1)
                    for m in (half, half - 1, half + 1):
                        x = Fixed(sign * m, s)
                        for mode, oracle in ORACLE_MODES.items():
                            expected, _ = oracle_quantize(dec(x), t, oracle)
                            self.assertSameFixed(x.rescale(t, mode), expected, f"{x!r} -> {t} {mode.name}")
                    tie = Fixed(sign * half, s)
                    even = k if k % 2 == 0 else k + 1
                    self.assertSameFixed(tie.rescale(t, Rounding.HALF_EVEN), Fixed(sign * even, t), f"HALF_EVEN {tie!r}")
                    self.assertSameFixed(tie.rescale(t, Rounding.HALF_UP), Fixed(sign * (k + 1), t), f"HALF_UP {tie!r}")
                    with self.assertRaises(InexactError):
                        tie.rescale(t, Rounding.UNNECESSARY)

    def test_specific_directions(self):
        """ADR-0004 #1: UP is away from zero, DOWN toward zero, CEILING toward +inf, FLOOR toward -inf."""
        table = [
            ("2.5", Rounding.HALF_EVEN, "2"), ("3.5", Rounding.HALF_EVEN, "4"), ("-2.5", Rounding.HALF_EVEN, "-2"),
            ("-3.5", Rounding.HALF_EVEN, "-4"), ("2.5", Rounding.HALF_UP, "3"), ("-2.5", Rounding.HALF_UP, "-3"),
            ("1.01", Rounding.UP, "2"), ("-1.01", Rounding.UP, "-2"), ("1.99", Rounding.DOWN, "1"),
            ("-1.99", Rounding.DOWN, "-1"), ("1.01", Rounding.CEILING, "2"), ("-1.99", Rounding.CEILING, "-1"),
            ("1.99", Rounding.FLOOR, "1"), ("-1.01", Rounding.FLOOR, "-2"), ("-0.4", Rounding.CEILING, "0"),
        ]
        for text, mode, out in table:
            with self.subTest(text=text, mode=mode.name):
                self.assertSameFixed(F(text).rescale(0, mode), F(out))

    def test_unnecessary_raises_exactly_when_inexact(self):
        """ADR-0004 #1: UNNECESSARY raises if and only if digits would be discarded; it is the default."""
        rng = random.Random(4005)
        for i in range(3000):
            x = rand_fixed(rng)
            target = rng.randint(0, MAX_SCALE)
            expected, inexact = oracle_quantize(dec(x), target, ROUND_DOWN)
            if inexact:
                with self.assertRaises(InexactError, msg=f"case {i} {x!r} -> {target}"):
                    x.rescale(target, Rounding.UNNECESSARY)
                with self.assertRaises(InexactError):
                    x.rescale(target)
            else:
                self.assertSameFixed(x.rescale(target, Rounding.UNNECESSARY), expected, f"case {i}")
                self.assertSameFixed(x.rescale(target), expected, f"case {i} default")
        self.assertSameFixed(F("1.2500").rescale(2), F("1.25"))
        self.assertSameFixed(F("1.25").rescale(6), F("1.250000"))
        for bad in (-1, MAX_SCALE + 1, 2.0):
            with self.subTest(scale=bad):
                with self.assertRaises(ValueError):
                    F("1").rescale(bad)

    def test_rounding_argument_validated_even_when_exact(self):
        """ADR-0004 #1: an explicit rounding MODE is required. A non-Rounding argument must be
        refused on every call, not only when the value happens to be inexact; otherwise a wrong
        argument passes every exact test and first fails on a live inexact amount."""
        with self.assertRaises(TypeError):
            F("4").div(F("2"), 0, None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            F("1.50").rescale(1, "DOWN")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            F("10").to_increment(F("5"), "FLOOR")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            F("1.25").div(F("1.25"), 2, None)  # type: ignore[arg-type]


class DivisionOracleTests(_Case):
    def test_div_every_mode_matches_decimal(self):
        """ADR-0004 #1: div(other, scale, rounding) is the correctly rounded quotient at ``scale``."""
        rng = random.Random(4006)
        for i in range(2500):
            a = rand_fixed(rng, max_digits=25)
            b = rand_fixed(rng, max_digits=12, nonzero=True)
            target = rng.randint(0, MAX_SCALE)
            for mode, oracle in ORACLE_MODES.items():
                expected, _ = oracle_div(a, b, target, oracle)
                self.assertSameFixed(a.div(b, target, mode), expected, f"case {i} {a!r}/{b!r} @{target} {mode.name}")
            expected, inexact = oracle_div(a, b, target, ROUND_DOWN)
            if inexact:
                with self.assertRaises(InexactError, msg=f"case {i} {a!r}/{b!r} @{target}"):
                    a.div(b, target, Rounding.UNNECESSARY)
            else:
                self.assertSameFixed(a.div(b, target, Rounding.UNNECESSARY), expected, f"case {i} exact")

    def test_div_exact_quotients_round_trip(self):
        """ADR-0004 #1: (b * c) / b at c's scale is exactly c under UNNECESSARY."""
        rng = random.Random(4007)
        for i in range(2000):
            b = rand_fixed(rng, max_digits=10, max_scale=9, nonzero=True)
            c = rand_fixed(rng, max_digits=15, max_scale=9)
            a = b * c
            self.assertSameFixed(a.div(b, c.scale, Rounding.UNNECESSARY), c, f"case {i}")

    def test_div_specific_values_and_int_or_text_divisors(self):
        """ADR-0004 #1: concrete quotients, negative operands and int/str divisors."""
        self.assertSameFixed(F("10").div(F("3"), 4, Rounding.HALF_EVEN), F("3.3333"))
        self.assertSameFixed(F("-10").div(3, 2, Rounding.FLOOR), F("-3.34"))
        self.assertSameFixed(F("-10").div("3", 2, Rounding.CEILING), F("-3.33"))
        self.assertSameFixed(F("10").div(F("-4"), 1, Rounding.UNNECESSARY), F("-2.5"))
        self.assertSameFixed(F("1").div(F("8"), 3, Rounding.UNNECESSARY), F("0.125"))
        self.assertSameFixed(F("0.05").div(F("0.0001"), 0, Rounding.UNNECESSARY), F("500"))
        self.assertSameFixed(F("2").div(F("3"), 0, Rounding.HALF_EVEN), F("1"))
        self.assertSameFixed(F("1").div(F("3"), 18, Rounding.UP), Fixed(333333333333333334, 18))
        with self.assertRaises(InexactError):
            F("1").div(F("3"), 18, Rounding.UNNECESSARY)
        with self.assertRaises((ValueError, TypeError)):
            F("1").div(F("3"), MAX_SCALE + 1, Rounding.DOWN)
        with self.assertRaises((ValueError, TypeError)):
            F("1").div(F("3"), -1, Rounding.DOWN)


class IncrementOracleTests(_Case):
    def test_to_increment_every_mode_matches_decimal(self):
        """ADR-0004 #1/#4: snapping to a tick or lot equals round(x / inc) * inc in the
        increment's scale, for every mode, and the result is a multiple of the increment."""
        rng = random.Random(4008)
        for i in range(2500):
            x = rand_fixed(rng, max_digits=14, max_scale=9)
            inc = rand_fixed(rng, max_digits=4, max_scale=6, nonzero=True, positive=True)
            for mode, oracle in ORACLE_MODES.items():
                k, _ = oracle_div(x, inc, 0, oracle)
                expected = Fixed(k.mantissa * inc.mantissa, inc.scale)
                got = x.to_increment(inc, mode)
                self.assertSameFixed(got, expected, f"case {i} {x!r} to {inc!r} {mode.name}")
                self.assertTrue(got.is_multiple_of(inc))
            _, inexact = oracle_div(x, inc, 0, ROUND_DOWN)
            if inexact:
                with self.assertRaises(InexactError):
                    x.to_increment(inc, Rounding.UNNECESSARY)
            else:
                self.assertEqual(x.to_increment(inc, Rounding.UNNECESSARY), x)

    def test_adr_directions_for_lots_and_reservations(self):
        """ADR-0004 #4: sellable quantity rounds DOWN to the lot; buy reservations round CEILING."""
        self.assertSameFixed(F("17.9").to_increment(F("5"), Rounding.DOWN), F("15"))
        self.assertSameFixed(F("-17.9").to_increment(F("5"), Rounding.DOWN), F("-15"))
        self.assertSameFixed(F("-17.9").to_increment(F("5"), Rounding.FLOOR), F("-20"))
        self.assertSameFixed(F("0.0999").to_increment(F("0.01"), Rounding.DOWN), F("0.09"))
        self.assertSameFixed(F("10.001").to_increment(F("0.01"), Rounding.CEILING), F("10.01"))
        self.assertSameFixed(F("10.057").to_increment(F("0.05"), Rounding.FLOOR), F("10.05"))
        self.assertSameFixed(F("10.075").to_increment(F("0.05"), Rounding.HALF_EVEN), F("10.10"))
        self.assertSameFixed(F("7").to_increment(F("0.25"), Rounding.UNNECESSARY), F("7.00"))
        for bad in (F("0"), F("0.00"), F("-0.01")):
            with self.subTest(increment=bad):
                with self.assertRaises(ValueError):
                    F("1").to_increment(bad, Rounding.DOWN)

    def test_is_multiple_of_matches_decimal_remainder(self):
        """ADR-0004 #5: tick/lot validation is exact (Decimal remainder == 0 as oracle)."""
        rng = random.Random(4009)
        ctx = _ctx()
        for i in range(4000):
            inc = rand_fixed(rng, max_digits=4, max_scale=6, nonzero=True, positive=True)
            if rng.random() < 0.5:
                x = inc * Fixed(rng.randint(-10 ** 6, 10 ** 6), rng.randint(0, 3)).normalized().rescale(0, Rounding.DOWN)
            else:
                x = rand_fixed(rng, max_digits=14, max_scale=9)
            expected = ctx.remainder(dec(x), dec(inc)).is_zero()
            self.assertEqual(x.is_multiple_of(inc), expected, f"case {i} {x!r} % {inc!r}")
        self.assertTrue(F("10.05").is_multiple_of(F("0.05")))
        self.assertFalse(F("10.051").is_multiple_of(F("0.05")))
        self.assertTrue(F("-0.10").is_multiple_of(F("0.05")))
        self.assertTrue(F("0").is_multiple_of(F("0.0001")))
        self.assertTrue(F("9.000000").is_multiple_of(F("0.01")))
        for bad in (F("0"), F("-1")):
            with self.assertRaises(ValueError):
                F("1").is_multiple_of(bad)


# ===================================================================== comparison / hash
class ComparisonAndHashTests(_Case):
    def test_ordering_matches_decimal_including_cross_scale(self):
        """ADR-0004 #1: comparison is by value, independent of scale."""
        rng = random.Random(4010)
        for i in range(4000):
            a = rand_fixed(rng, max_digits=12)
            if rng.random() < 0.3:
                k = rng.randint(0, MAX_SCALE - a.scale)
                b = Fixed(a.mantissa * 10 ** k, a.scale + k)  # same value, wider scale
            else:
                b = rand_fixed(rng, max_digits=12)
            da, db = dec(a), dec(b)
            msg = f"case {i} {a!r} vs {b!r}"
            self.assertEqual(a == b, da == db, msg)
            self.assertEqual(a != b, da != db, msg)
            self.assertEqual(a < b, da < db, msg)
            self.assertEqual(a <= b, da <= db, msg)
            self.assertEqual(a > b, da > db, msg)
            self.assertEqual(a >= b, da >= db, msg)
            self.assertEqual(a.sign(), (da > 0) - (da < 0), msg)
            if a == b:
                self.assertEqual(hash(a), hash(b), msg)

    def test_equal_values_collapse_in_sets_and_dicts_across_scales(self):
        """ADR-0004 #1 / #6: hashing uses the value, so 1.5, 1.50 and 1.500 are one key."""
        self.assertEqual(len({F("1.5"), F("1.50"), F("1.500"), Fixed(15, 1)}), 1)
        table = {F("0.10"): "ten cents"}
        self.assertEqual(table[F("0.1")], "ten cents")
        self.assertEqual(table[F("0.100000")], "ten cents")
        self.assertEqual(hash(Fixed(0)), hash(Fixed(0, MAX_SCALE)))
        self.assertEqual(F("-2.50"), F("-2.5"))
        self.assertEqual(F("2.00"), 2)
        self.assertTrue(F("2.001") > 2)
        self.assertTrue(1 < F("1.000001"))

    def test_equal_to_int_implies_equal_hash(self):
        """ADR-0004 #1 and the Python data model: Fixed('2.00') == 2 is True, so their
        hashes must be equal; otherwise a Fixed and an int that compare equal are two
        different dict/set keys (silent duplicate keys in any int/Fixed keyed table)."""
        self.assertEqual(F("2.00"), 2)
        self.assertEqual(hash(F("2.00")), hash(2))
        self.assertEqual(hash(F("-7")), hash(-7))
        self.assertEqual(hash(F("0.000")), hash(0))
        self.assertIn(2, {F("2.00")})
        self.assertEqual(len({F("5"), 5}), 1)

    def test_money_and_quantity_equality_across_scales(self):
        """DOM-02: typed wrappers compare by currency and value, not by scale."""
        self.assertEqual(Money.of("USD", "1.5"), Money.of("USD", "1.500000"))
        self.assertEqual(hash(Money.of("USD", "1.5")), hash(Money.of("USD", "1.500000")))
        self.assertNotEqual(Money.of("USD", "1.5"), Money.of("EUR", "1.5"))
        self.assertEqual(Quantity.of("10"), Quantity.of("10.000"))
        self.assertEqual(hash(Quantity.of("10")), hash(Quantity.of("10.000")))
        self.assertEqual(Price.of("USD", "9.9"), Price.of("USD", "9.90"))


# ===================================================================== float refusal
class FloatRefusalTests(_Case):
    def _refuse(self, label, fn, exc=TypeError):
        with self.subTest(label):
            with self.assertRaises(exc):
                fn()

    def test_fixed_refuses_floats_everywhere(self):
        """DOM-02 / ADR-0004 #1: constructing or combining Fixed with a float raises TypeError."""
        one = F("1")
        r = self._refuse
        r("ctor float", lambda: Fixed(1.5))
        r("ctor bool", lambda: Fixed(True))
        r("ctor Decimal", lambda: Fixed(Decimal("1")))
        r("ctor Fraction", lambda: Fixed(Fraction(1, 2)))
        r("parse float", lambda: Fixed.parse(1.5))
        r("parse bytes", lambda: Fixed.parse(b"1.5"))
        r("of float", lambda: Fixed.of(1.5))
        r("of Decimal", lambda: Fixed.of(Decimal("1.5")))
        r("of bool", lambda: Fixed.of(True))
        r("add", lambda: one + 0.5)
        r("radd", lambda: 0.5 + one)
        r("sub", lambda: one - 0.5)
        r("rsub", lambda: 0.5 - one)
        r("mul", lambda: one * 0.5)
        r("rmul", lambda: 0.5 * one)
        r("add Decimal", lambda: one + Decimal("1"))
        r("add bool", lambda: one + True)
        r("lt", lambda: one < 0.5)
        r("le", lambda: one <= 0.5)
        r("gt", lambda: one > 0.5)
        r("ge", lambda: one >= 0.5)
        r("div by float", lambda: one.div(0.5, 2, Rounding.DOWN))
        r("truediv", lambda: one / 2)
        r("float()", lambda: float(one))
        r("int()", lambda: int(one))
        r("round()", lambda: round(one, 2))
        r("to_increment float", lambda: one.to_increment(0.5, Rounding.DOWN), (TypeError, AttributeError))
        r("round_div float", lambda: round_div(1.0, 2, Rounding.DOWN))
        self.assertFalse(one == 1.0)
        self.assertTrue(one != 1.0)

    def test_money_types_refuse_floats(self):
        """DOM-02: Money, Price, Quantity and FxRate never hold or multiply by a float."""
        r = self._refuse
        r("Money.of", lambda: Money.of("USD", 1.5))
        r("Money ctor", lambda: Money("USD", 1.5))
        r("Money ctor Decimal", lambda: Money("USD", Decimal("1.5")))
        r("Money.times", lambda: Money.of("USD", "1").times(0.5))
        r("Money.from_json", lambda: Money.from_json({"currency": "USD", "amount": 1.5}))
        r("Price.of", lambda: Price.of("USD", 9.99))
        r("Price.from_json", lambda: Price.from_json({"currency": "USD", "amount": 9.99}))
        r("Quantity.of", lambda: Quantity.of(10.0))
        r("FxRate float", lambda: FxRate("EUR", "USD", 1.08), (TypeError, ValueError))
        r("Money + float", lambda: Money.of("USD", "1") + 1.0)

    def test_canonical_json_refuses_floats(self):
        """DOM-02 / ADR-0004 #6: canonical payloads (hashed for ORD-01) contain no binary floats."""
        r = self._refuse
        r("top-level", lambda: canonical_json(1.5))
        r("nested value", lambda: canonical_json({"a": {"b": [1, 2, 0.5]}}))
        r("in tuple", lambda: canonical_json({"pair": ("x", 2.0)}))
        r("integral float", lambda: canonical_json({"qty": 10.0}))
        r("nan", lambda: canonical_json({"x": float("nan")}), (TypeError, ValueError))
        r("inf", lambda: canonical_json([float("inf")]), (TypeError, ValueError))
        r("Decimal", lambda: canonical_json({"x": Decimal("1.5")}))
        r("object", lambda: canonical_json({"x": object()}))
        r("hash path", lambda: content_hash({"x": 0.1}))

    def test_canonical_json_refuses_float_keys(self):
        """DOM-02 / ADR-0004 #6: a float used as a mapping key is still a binary float in the
        payload; json.dumps would silently freeze its repr ("0.30000000000000004") into the
        hashed text. Floats must be refused in keys as well as values."""
        with self.assertRaises(TypeError):
            canonical_json({0.1 + 0.2: "x"})
        with self.assertRaises(TypeError):
            canonical_json({"prices": {9.99: "10"}})


# ===================================================================== money / FX / notional
class MoneyTests(_Case):
    def test_currency_mismatch_is_loud(self):
        """DOM-02 / ADR-0004 #2: Money combines only within one currency; otherwise CurrencyMismatch."""
        usd, eur = Money.of("USD", "1.00"), Money.of("EUR", "1.00")
        for label, fn in (("add", lambda: usd + eur), ("sub", lambda: usd - eur), ("lt", lambda: usd < eur),
                          ("le", lambda: usd <= eur), ("gt", lambda: usd > eur), ("ge", lambda: usd >= eur)):
            with self.subTest(label):
                with self.assertRaises(CurrencyMismatch):
                    fn()
        self.assertTrue(issubclass(CurrencyMismatch, ValueError))
        self.assertFalse(usd == eur)

    def test_unit_types_do_not_mix(self):
        """ADR-0004 consequences: adding a price or a bare number to cash is a type error."""
        usd = Money.of("USD", "1.00")
        with self.assertRaises(TypeError):
            usd + Price.of("USD", "1.00")  # type: ignore[operator]
        with self.assertRaises(TypeError):
            usd + F("1.00")  # type: ignore[operator]
        with self.assertRaises(TypeError):
            Price.of("USD", "1") + Price.of("USD", "1")  # type: ignore[operator]
        with self.assertRaises(TypeError):
            Quantity.of("1") + F("1")  # type: ignore[operator]
        with self.assertRaises(TypeError):
            F("1") + usd  # type: ignore[operator]

    def test_unknown_currencies_are_rejected(self):
        """DOM-02 / ADR-0004 #3: amounts in an unregistered currency are rejected, never guessed."""
        for code in ("XXX", "usd", "", "US", None):
            with self.subTest(code=code):
                with self.assertRaises(UnknownCurrency):
                    Money.of(code, "1")  # type: ignore[arg-type]
                with self.assertRaises(UnknownCurrency):
                    currency(code)  # type: ignore[arg-type]
        with self.assertRaises(UnknownCurrency):
            Price.of("ZZZ", "1")

    def test_money_arithmetic_is_exact_and_json_keeps_scale(self):
        """DOM-02 / ADR-0004 #1/#6: same-currency arithmetic is exact; JSON is canonical text."""
        total = Money.of("USD", "0.10") + Money.of("USD", "0.20")
        self.assertSameFixed(total.amount, F("0.30"))
        self.assertSameFixed((Money.of("USD", "1") - Money.of("USD", "0.000001")).amount, F("0.999999"))
        self.assertSameFixed(Money.of("USD", "19.99").times(3).amount, F("59.97"))
        self.assertSameFixed(Money.of("USD", "19.99").times("0.5").amount, F("9.995"))
        self.assertSameFixed((-Money.of("USD", "2.50")).amount, F("-2.50"))
        self.assertSameFixed(abs(Money.of("USD", "-2.50")).amount, F("2.50"))
        m = Money.of("EUR", "-1250.500")
        self.assertEqual(m.to_json(), {"currency": "EUR", "amount": "-1250.500"})
        back = Money.from_json(m.to_json())
        self.assertSameFixed(back.amount, m.amount)
        self.assertEqual(back.currency, "EUR")
        self.assertEqual(str(Money.of("USD", "3.10")), "3.10 USD")
        self.assertTrue(Money.of("USD", "0.000001") > Money.zero("USD"))


class LedgerScaleTests(_Case):
    def test_registry_ledger_scale_is_six_for_every_fiat(self):
        """ADR-0004 #3: the ledger scale 'is 6 for every fiat currency registered initially'."""
        for code in FIAT:
            with self.subTest(code=code):
                self.assertIn(code, CURRENCIES)
                self.assertEqual(currency(code).ledger_scale, 6)

    def test_money_zero_and_at_ledger_scale(self):
        """ADR-0004 #3: postings are exactly representable at ledger scale; an inexact
        amount is refused unless the caller rounds explicitly."""
        self.assertSameFixed(Money.zero("USD").amount, Fixed(0, 6))
        self.assertSameFixed(Money.of("USD", "1.5").at_ledger_scale().amount, F("1.500000"))
        self.assertSameFixed(Money.of("USD", "1.0000010").at_ledger_scale().amount, F("1.000001"))
        with self.assertRaises(InexactError):
            Money.of("USD", "1.0000001").at_ledger_scale()
        self.assertSameFixed(Money.of("USD", "1.0000001").at_ledger_scale(Rounding.CEILING).amount, F("1.000001"))
        self.assertSameFixed(Money.of("USD", "-1.0000001").at_ledger_scale(Rounding.CEILING).amount, F("-1.000000"))
        self.assertSameFixed(Money.of("USD", "-1.0000001").at_ledger_scale(Rounding.FLOOR).amount, F("-1.000001"))
        self.assertSameFixed(Money.of("USD", "2.0000005").at_ledger_scale(Rounding.HALF_EVEN).amount, F("2.000000"))

    def test_journal_entry_requires_exact_ledger_scale(self):
        """ADR-0004 #3 / LED-01: a journal line finer than ledger scale is refused (never
        truncated); exact lines are stored as canonical text at ledger scale and must balance."""
        good = ledger.entry("je-1", ACCOUNT, "OWNER_TRANSFER", "fixture",
                            ledger.transfer(ledger.CASH, "USD", F("1.2500000"), ledger.OUTSIDE, ledger.UNALLOCATED))
        self.assertEqual([l["amount"] for l in good["lines"]], ["1.250000", "-1.250000"])
        with self.assertRaises(InexactError):
            ledger.entry("je-2", ACCOUNT, "OWNER_TRANSFER", "fixture",
                         ledger.transfer(ledger.CASH, "USD", F("0.0000001"), ledger.OUTSIDE, ledger.UNALLOCATED))
        with self.assertRaises(InvariantViolation):
            ledger.entry("je-3", ACCOUNT, "TRADE", "fixture",
                         [ledger.Line(ledger.CASH, ledger.OUTSIDE, "USD", F("1.000001")),
                          ledger.Line(ledger.OPENING, ledger.OUTSIDE, "USD", F("-1.000000"))])


class NotionalTests(_Case):
    def test_notional_is_exact_product(self):
        """DOM-02 / ADR-0004 #1 (book eq. 3.1): N = p * q * M with no rounding; scales add."""
        n = notional(Price.of("USD", "9.99"), Quantity.of("3"), F("100"))
        self.assertEqual(n.currency, "USD")
        self.assertSameFixed(n.amount, F("2997.00"))
        n = notional(Price.of("EUR", "1234.5678"), Quantity.of("0.125"), F("1"))
        self.assertSameFixed(n.amount, F("154.3209750"))
        rng = random.Random(4011)
        ctx = _ctx()
        for i in range(2000):
            p = rand_fixed(rng, max_digits=12, max_scale=6)
            q = rand_fixed(rng, max_digits=8, max_scale=6)
            m = rand_fixed(rng, max_digits=4, max_scale=4, nonzero=True, positive=True)
            got = notional(Price("USD", p), Quantity(q), m)
            expected = _fixed_at(ctx.multiply(ctx.multiply(dec(p), dec(q)), dec(m)), p.scale + q.scale + m.scale)
            self.assertSameFixed(got.amount, expected, f"case {i}")

    def test_notional_scale_overflow_refused(self):
        """ADR-0004 #1: a product finer than MAX_SCALE raises rather than being rounded."""
        with self.assertRaises(ValueError):
            notional(Price.of("USD", "0.0000000001"), Quantity.of("0.000000001"), F("1"))


class FxRateTests(_Case):
    def test_inverse_matches_decimal_for_every_mode(self):
        """DOM-02 / ADR-0004 #2: inversion needs an explicit scale and mode and is the correctly
        rounded 1/rate with base and quote swapped."""
        rng = random.Random(4012)
        pairs = [("EUR", "USD"), ("USD", "INR"), ("GBP", "JPY"), ("CHF", "HKD")]
        for i in range(800):
            base, quote = rng.choice(pairs)
            rate = Fixed(rng.randint(1, 10 ** 8), rng.randint(0, 8))
            target = rng.randint(4, 12)
            fx = FxRate(base, quote, rate)
            for mode, oracle in ORACLE_MODES.items():
                expected, _ = oracle_div(F("1"), rate, target, oracle)
                if expected.is_positive():
                    inv = fx.inverse(target, mode)
                    self.assertEqual((inv.base, inv.quote), (quote, base))
                    self.assertSameFixed(inv.amount, expected, f"case {i} 1/{rate!r} @{target} {mode.name}")
                else:
                    with self.assertRaises(ValueError, msg=f"case {i}: inverse rounds to zero"):
                        fx.inverse(target, mode)

    def test_inverse_specifics(self):
        """ADR-0004 #2: exact inverses pass UNNECESSARY; inexact ones raise; a zero rate is refused."""
        eurusd = FxRate("EUR", "USD", F("1.0850"))
        self.assertSameFixed(eurusd.inverse(8, Rounding.HALF_EVEN).amount, F("0.92165899"))
        self.assertSameFixed(eurusd.inverse(8, Rounding.FLOOR).amount, F("0.92165898"))
        self.assertSameFixed(eurusd.inverse(8, Rounding.CEILING).amount, F("0.92165899"))
        with self.assertRaises(InexactError):
            eurusd.inverse(8, Rounding.UNNECESSARY)
        self.assertSameFixed(FxRate("GBP", "USD", F("1.25")).inverse(1, Rounding.UNNECESSARY).amount, F("0.8"))
        with self.assertRaises(ValueError):
            FxRate("USD", "JPY", F("1000000")).inverse(2, Rounding.DOWN)
        self.assertSameFixed(FxRate("USD", "JPY", F("1000000")).inverse(2, Rounding.CEILING).amount, F("0.01"))

    def test_convert_orientation_and_validation(self):
        """ADR-0004 #2 / LED-12: a rate converts base -> quote exactly; wrong orientation is a
        CurrencyMismatch; same-currency, zero and negative rates are refused."""
        fx = FxRate("EUR", "USD", F("1.0850"))
        out = fx.convert(Money.of("EUR", "100.00"))
        self.assertEqual(out.currency, "USD")
        self.assertSameFixed(out.amount, F("108.500000"))
        with self.assertRaises(CurrencyMismatch):
            fx.convert(Money.of("USD", "100.00"))
        for args in (("USD", "USD", F("1")), ("EUR", "USD", F("0")), ("EUR", "USD", F("-1.08"))):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    FxRate(*args)
        with self.assertRaises(UnknownCurrency):
            FxRate("EUR", "XYZ", F("1"))


# ===================================================================== canonical JSON
PINNED_TEXT = ('{"amount":"1250.500","big":1000000000000000000000000000000,'
               '"money":{"amount":"-0.010000","currency":"USD"},'
               '"nested":{"a":null,"b":true,"é":"ünïcode"},"pair":["x",1],"side":"HALF_EVEN",'
               '"tags":["alpha","beta"],"z":[3,2,1]}')
PINNED_SHA256 = "dd3598fc682781a5199d681356282edf45ae6a2c23759868433e77043e6aa56a"


def _pinned_payload() -> dict:
    return {
        "z": [3, 2, 1],
        "amount": F("1250.500"),
        "money": Money.of("USD", "-0.010000"),
        "nested": {"é": "ünïcode", "b": True, "a": None},
        "side": Rounding.HALF_EVEN,
        "tags": frozenset({"beta", "alpha"}),
        "pair": ("x", 1),
        "big": 10 ** 30,
    }


class CanonicalJsonTests(_Case):
    def test_pinned_encoding_and_hash(self):
        """ORD-01 / ADR-0005 #4 / ADR-0007 / ADR-0004 #6: the canonical encoding is fixed;
        any drift changes every payload, policy and mandate hash, so one hash is pinned."""
        text = canonical_json(_pinned_payload())
        self.assertEqual(text, PINNED_TEXT)
        self.assertEqual(hashlib.sha256(PINNED_TEXT.encode("utf-8")).hexdigest(), PINNED_SHA256)
        self.assertEqual(sha256_hex(text), PINNED_SHA256)
        self.assertEqual(content_hash(_pinned_payload()), PINNED_SHA256)

    def test_key_order_and_insertion_independence(self):
        """ORD-01: logically equal payloads hash identically whatever the insertion order."""
        a = {"b": 1, "a": {"d": 2, "c": [3, {"f": 4, "e": 5}]}}
        b = {"a": {"c": [3, {"e": 5, "f": 4}], "d": 2}, "b": 1}
        self.assertEqual(canonical_json(a), '{"a":{"c":[3,{"e":5,"f":4}],"d":2},"b":1}')
        self.assertEqual(canonical_json(a), canonical_json(b))
        self.assertEqual(content_hash(a), content_hash(b))
        self.assertEqual(canonical_json({"é": 1, "a": 2, "Z": 3, "_": 4}), '{"Z":3,"_":4,"a":2,"é":1}')
        self.assertNotEqual(content_hash({"a": [1, 2]}), content_hash({"a": [2, 1]}))  # list order is data

    def test_stability_and_idempotence(self):
        """ADR-0004 #6: re-encoding the decoded canonical text reproduces it byte for byte."""
        rng = random.Random(4013)
        for i in range(300):
            payload = {f"k{rng.randint(0, 50)}": rng.choice([
                rng.randint(-10 ** 20, 10 ** 20), rand_fixed(rng), "text-é", None, True,
                [rand_fixed(rng), rng.randint(0, 9)], {"inner": rand_fixed(rng)}]) for _ in range(rng.randint(1, 8))}
            text = canonical_json(payload)
            self.assertEqual(canonical_json(payload), text, f"case {i}")
            self.assertEqual(canonical_json(json.loads(text)), text, f"case {i}")
            self.assertNotIn(" ", text.replace("text-é", ""))

    def test_fixed_encodes_with_its_scale(self):
        """ADR-0004 #6: amounts encode as canonical decimal TEXT including their scale."""
        self.assertEqual(canonical_json({"a": F("1.5")}), '{"a":"1.5"}')
        self.assertEqual(canonical_json({"a": F("1.50")}), '{"a":"1.50"}')
        self.assertEqual(canonical_json({"q": Quantity.of("10")}), '{"q":"10"}')
        self.assertEqual(canonical_json({"p": Price.of("USD", "9.90")}), '{"p":{"amount":"9.90","currency":"USD"}}')
        self.assertEqual(canonical_json([F("-0.000001"), F("0.000")]), '["-0.000001","0.000"]')


# ===================================================================== identifiers
def _is_uuid7(text: str) -> bool:
    u = uuid.UUID(text)
    return str(u) == text and u.variant == uuid.RFC_4122 and u.version == 7


class IdGeneratorTests(_Case):
    T_MS = 1_792_504_800_000  # fixture time: 2026-10-20 14:00 UTC

    def test_layout_is_rfc9562_uuid7(self):
        """DOM-04 / ADR-0005 #1: event ids are UUIDv7 carrying the millisecond timestamp."""
        gen = IdGenerator(lambda: self.T_MS, SequenceRandom(7))
        for _ in range(50):
            text = gen.new()
            self.assertTrue(_is_uuid7(text), text)
            self.assertEqual(uuid.UUID(text).int >> 80, self.T_MS)

    def test_same_millisecond_burst_is_strictly_monotonic(self):
        """DOM-04: ids generated in one millisecond still sort strictly by creation order,
        even when each random tail is smaller than the previous one."""
        state = {"n": 2 ** 70}

        def descending(size: int) -> bytes:
            state["n"] -= 2 ** 40 + 12345
            return state["n"].to_bytes(size, "big")

        gen = IdGenerator(lambda: self.T_MS, descending)
        ids = [gen.new() for _ in range(2000)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))
        for text in ids:
            self.assertTrue(_is_uuid7(text), text)
            self.assertEqual(uuid.UUID(text).int >> 80, self.T_MS)
        ints = [uuid.UUID(t).int for t in ids]
        self.assertTrue(all(b > a for a, b in zip(ints, ints[1:])))

    def test_monotonic_bump_never_corrupts_version_or_variant(self):
        """DOM-04 / ADR-0005 #1: every generated id is a valid UUIDv7 (version 7, RFC 9562
        variant 0b10), including the same-millisecond bump path when the previous random
        tail is all ones; the bump must not carry into the variant bits."""
        gen = IdGenerator(lambda: self.T_MS, lambda size: b"\xff" * size)
        ids = [gen.new() for _ in range(3)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 3)
        for text in ids:
            self.assertEqual(uuid.UUID(text).variant, uuid.RFC_4122, text)
            self.assertEqual(uuid.UUID(text).version, 7, text)

    def test_clock_regression_keeps_order_and_time_advance_wins(self):
        """DOM-04 / DOM-05: a clock stepping backwards never produces a smaller id; a later
        millisecond sorts after an earlier one whatever the random tail."""
        clock = {"ms": self.T_MS}
        tails = iter([b"\x00" * 10, b"\xff" * 10, b"\x00" * 10, b"\x80" * 10, b"\x00" * 10])
        gen = IdGenerator(lambda: clock["ms"], lambda size: next(tails))
        a = gen.new()
        clock["ms"] -= 10
        b = gen.new()
        clock["ms"] += 1
        c = gen.new()
        clock["ms"] = self.T_MS + 1
        d = gen.new()
        e = gen.new()
        self.assertEqual([a, b, c, d, e], sorted([a, b, c, d, e]))
        self.assertEqual(len({a, b, c, d, e}), 5)
        self.assertEqual(uuid.UUID(d).int >> 80, self.T_MS + 1)
        for text in (a, b, c, d, e):
            self.assertTrue(_is_uuid7(text), text)

    def test_deterministic_with_injected_sources_and_prefix(self):
        """DOM-04 / BRK-05: injected clock and randomness make generation reproducible; prefixed
        ids are valid identifiers and keep their suffix order."""
        g1 = IdGenerator(lambda: self.T_MS, SequenceRandom(3))
        g2 = IdGenerator(lambda: self.T_MS, SequenceRandom(3))
        s1 = [g1.new("evt-") for _ in range(20)]
        s2 = [g2.new("evt-") for _ in range(20)]
        self.assertEqual(s1, s2)
        self.assertEqual(s1, sorted(s1))
        for text in s1:
            self.assertTrue(text.startswith("evt-"))
            self.assertEqual(validate_id(text, "event id"), text)
            self.assertTrue(_is_uuid7(text[len("evt-"):]))

    def test_concurrent_generation_is_unique(self):
        """DOM-04: the generator is safe across threads (one lock around read-compare-write)."""
        gen = IdGenerator(lambda: self.T_MS, SequenceRandom(11))
        out: list[str] = []
        lock = threading.Lock()

        def worker():
            local = [gen.new() for _ in range(250)]
            with lock:
                out.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(out), 2000)
        self.assertEqual(len(set(out)), 2000)
        for text in out:
            self.assertTrue(_is_uuid7(text), text)


class ValidateIdTests(_Case):
    def test_accepts_well_formed_identifiers(self):
        """DOM-03 / DOM-04: well-formed opaque identifiers pass through unchanged."""
        for value in ("SIM-1", "a", "A" * 160, "rec-01a1599c-a300-7000-8000-00000000000a", "sim:tag:E1",
                      "lot:sim:x:E1:short", "owner@host", "a/b", "a+b", "a.b", "a_b", "0"):
            with self.subTest(value=value):
                self.assertEqual(validate_id(value), value)

    def test_rejects_malformed_and_hostile_values(self):
        """DOM-03 / DOM-04: validate_id keeps hostile or malformed values out of the store."""
        bad = ["", "-a", ".a", "_a", "/a", ":a", "A" * 161, "a b", " a", "a\tb", "a\x00b", "a;b", "a'b", 'a"b',
               "a\\b", "a%b", "a*b", "a?b", "a#b", "a,b", "a=b", "a<b", "é", "SIM‐1", "ЅIM-1",
               "Ａ", "a​b", "\nabc", "abc\r", None, 1, 1.0, b"abc", ["a"], ("a",)]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(InvalidIdentifier):
                    validate_id(value)
        self.assertTrue(issubclass(InvalidIdentifier, ValueError))
        with self.assertRaisesRegex(InvalidIdentifier, "order id"):
            validate_id("bad id", "order id")

    def test_rejects_trailing_newline(self):
        """DOM-03 / DOM-04: 'abc\\n' is not the identifier 'abc'. A regex ending in '$' matches
        before a final newline, so without a full-string match a key such as 'SIM-1\\n' is
        accepted as a distinct identifier (key confusion, log/line injection)."""
        for value in ("abc\n", "SIM-1\n", "order-1\n"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidIdentifier):
                    validate_id(value)


# ===================================================================== end to end
class LedgerScaleEndToEndTests(_Case):
    def _harness(self, **kw) -> Harness:
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h

    def test_capital_authorization_is_exact_at_ledger_scale(self):
        """DOM-02 / ADR-0004 #3 / LED-01: an amount finer than ledger scale is rejected with no
        money moved; an exact amount posts and is stored as canonical text at scale 6."""
        h = self._harness()
        h.bootstrap(authorize="", start=False)
        outside_before = h.balance("CASH", "outside")
        self.assertSameFixed(outside_before, F("300.000000"))
        r = h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="250.0000001")
        self.assertFalse(r.accepted)
        self.assertEqual(r.body["code"], "INVALID_AMOUNT")
        self.assertSameFixed(h.balance("CASH", "outside"), outside_before)
        self.assertEqual(h.balance("CASH", "unallocated"), F("0"))
        r = h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="250.0000010")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body["authorized_total"], "250.000001")
        self.assertSameFixed(h.balance("CASH", "unallocated"), F("250.000001"))
        self.assertSameFixed(h.balance("CASH", "outside"), F("49.999999"))
        stored = {row["amount"] for row in h.desk.store.query("SELECT amount FROM balances WHERE account_key=?", (ACCOUNT,))}
        for text in stored:
            self.assertEqual(len(text.partition(".")[2]), 6, text)
        self.assertEqual(h.verify(), [])

    def _sub_scale_fill(self) -> Harness:
        h = self._harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        r = h.intent("B1", "BUY", "7", "9.00", "A")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        # Broker reports a price-improved fill whose notional (62.99999965) is finer than ledger scale.
        h.broker.fill("B1", F("7"), F("8.99999995"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "FILLED")
        return h

    def test_sub_ledger_scale_fill_is_rounded_explicitly_and_raw_retained(self):
        """ADR-0004 #3/#5 / LED-01: a broker amount finer than ledger scale is rounded
        explicitly at ledger scale for posting, and the raw exact value is retained."""
        h = self._sub_scale_fill()
        (row,) = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM executions")]
        self.assertEqual(row["price"], "8.99999995")
        self.assertEqual(row["raw_notional"], "62.99999965")
        self.assertEqual(row["notional"], "63.000000")
        self.assertSameFixed(F(row["raw_notional"]).rescale(6, Rounding.HALF_EVEN), F(row["notional"]))
        self.assertSameFixed(h.balance("INVENTORY", "session:A", instrument_id="FIX-EQ-1"), F("63.000000"))
        self.assertEqual(h.verify(), [])

    def test_sub_ledger_scale_residual_does_not_become_unexplained_cash(self):
        """ADR-0004 #3 / ADR-0006 (ROUNDING) / REC-01: the residual from rounding a broker amount
        is recorded, 'so nothing is silently truncated'. After the trade settles, a residual
        smaller than one ledger unit must not surface as a blocking, unexplained CASH_MISMATCH
        (which the owner cannot even resolve: record_external_cash_flow refuses sub-ledger amounts)."""
        h = self._sub_scale_fill()
        h.clock.advance(24 * 3600 * 10 ** 9)
        self.assertEqual(h.broker.advance_settlement(), 1)
        h.quote("8.99", "9.01")
        rec = h.reconcile()
        self.assertTrue(rec.accepted, rec.body)
        blocking_cash = [d for d in rec.body.get("differences", []) if d["kind"] == "CASH_MISMATCH" and d["blocking"]]
        self.assertEqual(blocking_cash, [], rec.body)
        self.assertEqual([e for e in h.open_exceptions() if e["kind"] == "CASH_MISMATCH"], [])
        self.assertEqual(h.verify(), [])

    def test_opening_import_of_sub_ledger_scale_broker_cash_does_not_crash(self):
        """ADR-0004 #3 / REC-01: 'an adapter that receives a broker amount with finer precision
        rounds it explicitly and records the residual'. Broker cash finer than ledger scale at
        the first reconciliation must yield a reconciliation result (explicit rounding or an
        exception case), not an uncaught InexactError that leaves the account unreconcilable."""
        h = self._harness(initial_cash="300.0000001")
        self.assertTrue(h.owner_cmd("register_account", account_key=ACCOUNT, provider="simulated", mode="SIMULATED",
                                    base_currency="USD").accepted)
        r = h.owner_cmd("publish_policy", body=h.policy_body)
        self.assertTrue(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=r.body["policy_hash"]).accepted)
        self.assertTrue(h.owner_cmd("acquire_execution_ownership", account_key=ACCOUNT, deployment_id="mac-dev").accepted)
        h.connect()
        try:
            rec = h.reconcile()
        except InexactError as exc:  # pragma: no cover - the defect path
            self.fail(f"reconciliation raised {exc!r} instead of rounding explicitly or recording an exception")
        self.assertIn(rec.body.get("status"), ("CLEAN", "DIFFERENCES"), rec.body)
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
