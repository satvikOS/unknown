"""Identifier validation before any lookup or network call (CAP-INS-01, CAP-INS-02).

The check-digit algorithms are the standard published ones as recorded in the capability register
(CAP-INS-01 ISIN; CAP-INS-02 CUSIP and SEDOL), each marked there as "stated without a fetched
source; verify against the standard before release". FIGI is checked for format only: 12
characters, consonants and digits, third character 'G', first two not in the reserved prefixes;
its check digit is not verified here.

CUSIP and SEDOL are licensed identifiers (CAP-INS-03): validating a value locally is fine, but
callers must not store or send them unless a licence is recorded.
"""
from __future__ import annotations

import re

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CUSIP = re.compile(r"^[0-9A-Z*@#]{8}[0-9]$")
_SEDOL = re.compile(r"^[0-9B-DF-HJ-NP-TV-Z]{6}[0-9]$")
_FIGI = re.compile(r"^[B-DF-HJ-NP-TV-Z]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}[0-9]$")
_FIGI_RESERVED_PREFIXES = {"BS", "BM", "GG", "GB", "GH", "KY", "VG"}


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def isin_valid(value: str) -> bool:
    """ISO 6166 shape plus Luhn mod-10 over letters expanded to numbers (A=10 .. Z=35)."""
    if not isinstance(value, str) or not _ISIN.match(value):
        return False
    return _luhn_ok("".join(str(int(c, 36)) for c in value))


def _cusip_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if "A" <= c <= "Z":
        return ord(c) - ord("A") + 10
    return {"*": 36, "@": 37, "#": 38}[c]


def cusip_valid(value: str) -> bool:
    if not isinstance(value, str) or not _CUSIP.match(value):
        return False
    total = 0
    for i, c in enumerate(value[:8]):
        v = _cusip_value(c)
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(value[8])


def sedol_valid(value: str) -> bool:
    if not isinstance(value, str) or not _SEDOL.match(value):
        return False
    weights = (1, 3, 1, 7, 3, 9)
    total = sum(w * (int(c) if c.isdigit() else ord(c) - ord("A") + 10) for w, c in zip(weights, value[:6]))
    return (10 - total % 10) % 10 == int(value[6])


def figi_format_ok(value: str) -> bool:
    return isinstance(value, str) and bool(_FIGI.match(value)) and value[:2] not in _FIGI_RESERVED_PREFIXES


def classify(query: str) -> str:
    """Best-effort identifier type of a search query: CIK, ISIN, FIGI, TICKER or NAME.
    CUSIP/SEDOL-shaped inputs are reported as LICENSED_ID and are not looked up."""
    q = query.strip().upper()
    if re.fullmatch(r"(CIK)?0*\d{1,10}", q):
        return "CIK"
    if isin_valid(q):
        return "ISIN"
    if figi_format_ok(q):
        return "FIGI"
    if cusip_valid(q) or (len(q) == 7 and sedol_valid(q)):
        return "LICENSED_ID"
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", q):
        return "TICKER"
    return "NAME"
