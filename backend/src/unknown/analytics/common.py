"""Shared helpers: scopes, exact/statistical conversions, lineage records, read snapshots (CAP-PORT-01..13; DAT-05).

Scopes (CAP-PORT-01 'scope' selector; CAP-PORT-04 flow semantics depend on it):
  account          every owner in the broker account, including ``outside`` and ``external``
  system           owners whose value belongs to the system's authorized capital
                   (``unallocated``, ``buffer``, ``session:*``, ``protected:*``; ledger.is_system_owner)
  owner:<owner>    exactly one owner, e.g. ``owner:session:S1``; ``session:<id>``,
                   ``protected:<id>``, ``unallocated``, ``buffer``, ``outside`` and ``external``
                   are accepted as shorthands for the same thing.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterator

from ..core import ledger
from ..domain.fixed import Fixed, Rounding

WEIGHT_SCALE = 8


@dataclass(frozen=True)
class Scope:
    kind: str            # "account" | "system" | "owner"
    owner: str | None = None

    def includes(self, owner: str) -> bool:
        if self.kind == "account":
            return True
        if self.kind == "system":
            return ledger.is_system_owner(owner)
        return owner == self.owner

    @property
    def label(self) -> str:
        return self.kind if self.owner is None else f"owner:{self.owner}"

    def flow_note(self) -> str:
        if self.kind == "account":
            return "flows are deposits/withdrawals recorded as EXTERNAL_FLOW and the OPENING import; owner transfers are internal"
        if self.kind == "system":
            return ("flows are capital authorizations and deauthorizations (owner transfers between 'outside' and system "
                    "owners); session funding and protection are internal moves")
        return "flows are every owner transfer into or out of this owner (allocations, protection, closure)"


ACCOUNT = Scope("account")
SYSTEM = Scope("system")


def parse_scope(text: str | None) -> Scope:
    if text in (None, "", "account"):
        return ACCOUNT
    if text == "system":
        return SYSTEM
    owner = text[len("owner:"):] if text.startswith("owner:") else text
    if not ledger.valid_owner(owner):
        raise ValueError(f"unknown scope {text!r}: use account, system, owner:<owner> or session:<id>")
    return Scope("owner", owner)


def owner_kind(owner: str) -> str:
    for prefix in ("session", "protected", "exception"):
        if owner.startswith(prefix + ":"):
            return prefix
    return owner


# ------------------------------------------------------------- exact <-> statistical
def frac(x: Fixed) -> Fraction:
    """Exact rational value of a Fixed (for statistics computed from money)."""
    return Fraction(x.mantissa, 10 ** x.scale)


def to_float(x: Fixed) -> float:
    """Statistical use only: correctly rounded nearest float of an exact decimal. Never posts money."""
    return x.mantissa / 10 ** x.scale


def ratio(a: Fixed, b: Fixed) -> float:
    """a / b as a float computed from exact rationals (statistical use only)."""
    return float(frac(a) / frac(b))


def weight(a: Fixed, b: Fixed) -> str | None:
    """Exact-to-display weight a/b at WEIGHT_SCALE decimals (half-even); None when b is not positive."""
    if not b.is_positive():
        return None
    return a.div(b, WEIGHT_SCALE, Rounding.HALF_EVEN).to_str()


def finite(x: float | None) -> float | None:
    """JSON-safe float: NaN or infinity becomes None (callers attach a reason)."""
    if x is None or not math.isfinite(x):
        return None
    return x


def parse_fixed_param(text: str | None, what: str) -> Fixed | None:
    if text is None or text == "":
        return None
    try:
        return Fixed.parse(text)
    except ValueError:
        raise ValueError(f"{what} must be a plain decimal string, got {text!r}") from None


def parse_fraction(value: object, what: str) -> Fraction:
    """Confidence levels and shocks as exact fractions ('0.99' -> 99/100); floats are converted via repr."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, Fixed):
        return frac(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    if isinstance(value, (str, float)):
        try:
            return Fraction(str(value))
        except (ValueError, ZeroDivisionError):
            raise ValueError(f"{what} must be a decimal number, got {value!r}") from None
    raise ValueError(f"{what} must be a decimal number")


def fixed_of_fraction(x: Fraction, scale: int, rounding: Rounding = Rounding.HALF_EVEN) -> Fixed:
    return Fixed(x.numerator).div(Fixed(x.denominator), scale, rounding)


def lineage(formulas: list[str], *, inputs: dict, units: str, source_dates: dict | None = None,
            missing: list | None = None, notes: list[str] | None = None) -> dict:
    """The provenance block attached to every published result (DAT-05, TRM-03)."""
    from .formulas import ref
    return {"formulas": [ref(f) for f in formulas], "inputs": inputs, "units": units,
            "source_dates": source_dates or {}, "missing": missing or [], "notes": notes or []}


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One consistent read transaction over the (read-only) core connection.

    If the caller is already inside a transaction, it is reused unchanged."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")


def latest_seq(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()["s"]
