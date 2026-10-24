"""Alert rule schema and validation (CAP-MKT-09, CAP-NEWS-13).

A rule is plain JSON, validated here before it is stored. Thresholds are exact decimal strings. Kinds:

  price_cross  {key, field: last|bid|ask|mid, level, direction: up|down|either, hysteresis}
  pct_move     {key, threshold (0.05 = 5 %), direction}             move = last / prev_close - 1
  volume       {key, threshold}                                     session volume reported by the source
  time         {at: ISO 8601 with offset}                           fires once at that instant
  study        {key, study: sma|ema|rsi, n, compare: value|price, level?, direction, hysteresis}
  news         {issuer: CIK or TICKER[.MIC], forms?: [..]}          a new news-store item tagged with the issuer
  filing       {issuer, forms?: [..]}                               the same, SEC filings (items with a form) only

Common fields: name, repeat (once | rearm), allow_delayed (false = real-time only), expires_at (ISO 8601, optional).
An alert rule has no order fields and the alerts package has no path to the order or command services: an alert
never acts on orders.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ..marketdata.events import InvalidKey, parse_key
from .studies import STUDIES

KINDS = ("price_cross", "pct_move", "volume", "time", "study", "news", "filing")
MARKET_KINDS = ("price_cross", "pct_move", "volume", "study")
NEWS_KINDS = ("news", "filing")
DIRECTIONS = ("up", "down", "either")
FIELDS = ("last", "bid", "ask", "mid")
FORM = re.compile(r"^[A-Z0-9][A-Z0-9\-/ ]{0,15}$")
ISSUER = re.compile(r"^(\d{1,10}|[A-Z][A-Z0-9.\-]{0,14})$")
COMMON = {"kind", "name", "repeat", "allow_delayed", "expires_at", "note"}
ALLOWED = {
    "price_cross": {"key", "field", "level", "direction", "hysteresis"},
    "pct_move": {"key", "threshold", "direction", "hysteresis"},
    "volume": {"key", "threshold"},
    "time": {"at"},
    "study": {"key", "study", "n", "compare", "level", "direction", "hysteresis"},
    "news": {"issuer", "forms"},
    "filing": {"issuer", "forms"},
}


class RuleError(ValueError):
    pass


def _decimal(body: dict, name: str, *, positive: bool = False, nonneg: bool = False, default: str | None = None) -> str:
    v = body.get(name, default)
    if v is None:
        raise RuleError(f"{name} is required")
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        raise RuleError(f"{name} must be a decimal string")
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        raise RuleError(f"{name} must be a decimal string") from None
    if not d.is_finite():
        raise RuleError(f"{name} must be finite")
    if positive and d <= 0:
        raise RuleError(f"{name} must be greater than 0")
    if nonneg and d < 0:
        raise RuleError(f"{name} must not be negative")
    return str(d)


def _choice(body: dict, name: str, options: tuple, default: str | None = None) -> str:
    v = body.get(name, default)
    if v not in options:
        raise RuleError(f"{name} must be one of {', '.join(options)}")
    return v


def parse_instant(text: object, name: str) -> int:
    if not isinstance(text, str):
        raise RuleError(f"{name} must be an ISO 8601 date-time with a UTC offset")
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise RuleError(f"{name} must be an ISO 8601 date-time with a UTC offset") from None
    if dt.tzinfo is None:
        raise RuleError(f"{name} needs a UTC offset (for example 2026-10-24T15:30:00-04:00)")
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1000


def _key(body: dict) -> str:
    key = body.get("key")
    try:
        parse_key(key)
    except InvalidKey as exc:
        raise RuleError(str(exc)) from None
    return key


def validate(body: object) -> dict:
    """A normalised rule (decimal strings, explicit defaults) or RuleError naming the first problem."""
    if not isinstance(body, dict):
        raise RuleError("a rule is a JSON object")
    kind = body.get("kind")
    if kind not in KINDS:
        raise RuleError(f"kind must be one of {', '.join(KINDS)}")
    extra = set(body) - COMMON - ALLOWED[kind]
    if extra:
        raise RuleError(f"unknown field(s) for a {kind} rule: {', '.join(sorted(extra))}")
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise RuleError("name must be 1..120 characters")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise RuleError("note must be at most 500 characters")
    allow_delayed = body.get("allow_delayed", True)
    if not isinstance(allow_delayed, bool):
        raise RuleError("allow_delayed must be true or false")
    rule = {"kind": kind, "name": name.strip(), "note": note, "allow_delayed": allow_delayed,
            "repeat": _choice(body, "repeat", ("once", "rearm"), "once" if kind == "time" else "rearm")}
    if body.get("expires_at") is not None:
        rule["expires_at"] = body["expires_at"]
        rule["expires_ns"] = parse_instant(body["expires_at"], "expires_at")
    if kind == "price_cross":
        rule.update(key=_key(body), field=_choice(body, "field", FIELDS, "last"), level=_decimal(body, "level", positive=True),
                    direction=_choice(body, "direction", DIRECTIONS, "either"),
                    hysteresis=_decimal(body, "hysteresis", nonneg=True, default="0"))
    elif kind == "pct_move":
        rule.update(key=_key(body), threshold=_decimal(body, "threshold", positive=True),
                    direction=_choice(body, "direction", DIRECTIONS, "either"),
                    hysteresis=_decimal(body, "hysteresis", nonneg=True, default="0"))
        if Decimal(rule["threshold"]) >= 10:
            raise RuleError("threshold is a fraction: 0.05 means 5 %")
    elif kind == "volume":
        rule.update(key=_key(body), threshold=_decimal(body, "threshold", positive=True))
    elif kind == "time":
        rule.update(at=body.get("at"), at_ns=parse_instant(body.get("at"), "at"), repeat="once")
    elif kind == "study":
        n = body.get("n", 14)
        if not isinstance(n, int) or isinstance(n, bool) or not 2 <= n <= 500:
            raise RuleError("n must be an integer in [2, 500]")
        compare = _choice(body, "compare", ("value", "price"), "value")
        rule.update(key=_key(body), study=_choice(body, "study", STUDIES), n=n, compare=compare,
                    direction=_choice(body, "direction", DIRECTIONS, "either"),
                    hysteresis=_decimal(body, "hysteresis", nonneg=True, default="0"))
        if compare == "value":
            rule["level"] = _decimal(body, "level")
        elif "level" in body:
            raise RuleError("level applies only when compare is 'value'")
    else:
        issuer = body.get("issuer")
        if not isinstance(issuer, str) or not ISSUER.fullmatch(issuer.strip().upper()):
            raise RuleError("issuer must be CIK digits or a ticker (TICKER or TICKER.MIC)")
        forms = body.get("forms") or []
        if not isinstance(forms, list) or len(forms) > 20 or not all(isinstance(f, str) and FORM.fullmatch(f.strip().upper())
                                                                     for f in forms):
            raise RuleError("forms must be a list of up to 20 form types such as 8-K")
        rule.update(issuer=issuer.strip().upper(), forms=sorted({f.strip().upper() for f in forms}))
    return rule


def describe(rule: dict) -> str:
    """One line for the inbox and the notification."""
    k = rule["kind"]
    if k == "price_cross":
        return f"{rule['key']} {rule['field']} crosses {rule['level']} ({rule['direction']})"
    if k == "pct_move":
        return f"{rule['key']} moves {Decimal(rule['threshold']) * 100}% from the previous close ({rule['direction']})"
    if k == "volume":
        return f"{rule['key']} session volume reaches {rule['threshold']}"
    if k == "time":
        return f"time {rule['at']}"
    if k == "study":
        what = f"{rule['study'].upper()} {rule['n']}"
        subject = what if rule["compare"] == "value" else "last price"
        target = rule["level"] if rule["compare"] == "value" else what
        return f"{rule['key']} {subject} crosses {target} ({rule['direction']})"
    forms = f" {'/'.join(rule['forms'])}" if rule["forms"] else ""
    return f"new {'filing' if k == 'filing' else 'news'}{forms} for {rule['issuer']}"
