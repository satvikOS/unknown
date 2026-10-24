"""Pure alert evaluation: (rule, state, observation) -> (trigger or None, new state, evaluation status).

Crossing rules use the previous and the current value, so a value that stays beyond the level fires once. After a
trigger the direction is disarmed; it re-arms when the value comes back past ``level - hysteresis`` (up) or
``level + hysteresis`` (down). With ``repeat: once`` the engine retires the rule after its first trigger.

Level rules (percentage move, volume) treat the first observation as coming from the safe side, so a stock already
up 6 % fires a 5 % rule on the first quote; crossing rules (price, study) need a previous value.

Every trigger records the observed value, the threshold, the data source, the data type and both timestamps, and is
labelled when it fired on delayed data. FROZEN data (the last values of a closed market) is never evaluated, and a
real-time-only rule is not evaluated on DELAYED data.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from . import studies

LIVE_TYPES = ("REALTIME", "DELAYED")


def _d(v) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def edge(state: dict, x: Decimal, up_level: Decimal, down_level: Decimal, direction: str, h: Decimal, *,
         fire_on_first: bool) -> tuple[str | None, dict]:
    prev = _d(state.get("prev"))
    armed_up, armed_down = state.get("armed_up", True), state.get("armed_down", True)
    fired = None
    if direction in ("up", "either"):
        from_below = prev < up_level if prev is not None else fire_on_first
        if armed_up and from_below and x >= up_level:
            fired, armed_up = "up", False
        elif not armed_up and x < up_level - h:
            armed_up = True
    if fired is None and direction in ("down", "either"):
        from_above = prev > down_level if prev is not None else fire_on_first
        if armed_down and from_above and x <= down_level:
            fired, armed_down = "down", False
        elif not armed_down and x > down_level + h:
            armed_down = True
    return fired, {**state, "prev": str(x), "armed_up": armed_up, "armed_down": armed_down}


def quote_value(ev: dict, field: str) -> Decimal | None:
    if field == "mid":
        bid, ask = _d(ev.get("bid")), _d(ev.get("ask"))
        return (bid + ask) / 2 if bid is not None and ask is not None else None
    return _d(ev.get(field))


def _status(evaluating: bool, reason: str | None = None, detail: str | None = None) -> dict:
    return {"evaluating": evaluating, "reason": reason, "detail": detail}


def evaluate_quote(rule: dict, state: dict, ev: dict, closes: list[float] | None = None):
    """A quote event from the market-data bus against one market rule."""
    data_type = ev.get("data_type")
    if data_type not in LIVE_TYPES:
        return None, state, _status(False, "STALE", f"{data_type} data is not evaluated (not a live value)")
    if data_type != "REALTIME" and not rule.get("allow_delayed", True):
        return None, state, _status(False, "NOT_ENTITLED", f"real-time only: {ev.get('source')} delivers {data_type} data")
    kind = rule["kind"]
    h = Decimal(rule.get("hysteresis", "0"))
    meta: dict = {}
    if kind == "price_cross":
        x = quote_value(ev, rule["field"])
        if x is None:
            return None, state, _status(False, "NO_DATA", f"no {rule['field']} in the quote")
        level = Decimal(rule["level"])
        fired, new = edge(state, x, level, level, rule["direction"], h, fire_on_first=False)
        observed, threshold = str(x), rule["level"]
    elif kind == "pct_move":
        last, prev_close = _d(ev.get("last")), _d(ev.get("prev_close"))
        if last is None or prev_close is None or prev_close <= 0:
            return None, state, _status(False, "NO_DATA", "the quote has no last price or previous close")
        x = last / prev_close - 1
        t = Decimal(rule["threshold"])
        fired, new = edge(state, x, t, -t, rule["direction"], h, fire_on_first=True)
        observed, threshold = str(x.quantize(Decimal("0.000001"))), rule["threshold"]
        meta = {"last": str(last), "prev_close": str(prev_close)}
    elif kind == "volume":
        x = _d(ev.get("volume"))
        if x is None:
            return None, state, _status(False, "NO_DATA", "the quote has no session volume")
        t = Decimal(rule["threshold"])
        fired, new = edge(state, x, t, t, "up", Decimal(0), fire_on_first=True)
        observed, threshold = str(x), rule["threshold"]
        meta = {"size_unit": ev.get("size_unit")}
    elif kind == "study":
        last = _d(ev.get("last"))
        if last is None:
            return None, state, _status(False, "NO_DATA", "the quote has no last price")
        if not closes:
            return None, state, _status(False, "NO_DATA", "no stored daily bars for the study")
        series = list(closes) + [float(last)]
        sv = studies.value(rule["study"], series, rule["n"])
        if sv is None:
            return None, state, _status(False, "NO_DATA", f"{len(series)} closes: the study is still warming up")
        if rule["compare"] == "value":
            x, level = Decimal(repr(sv)), Decimal(rule["level"])
            observed, threshold = f"{sv:.6g}", rule["level"]
        else:
            x, level = last - Decimal(repr(sv)), Decimal(0)
            observed, threshold = str(last), f"{sv:.6g}"
        fired, new = edge(state, x, level, level, rule["direction"], h, fire_on_first=False)
        meta = {"study": f"{rule['study'].upper()} {rule['n']}", "study_value": sv, "study_version": studies.STUDY_VERSION,
                "closes_used": len(series), "provisional_bar": "the live last price is the current session's close"}
    else:
        raise ValueError(f"{kind} is not a market rule")
    if fired is None:
        return None, new, _status(True)
    trigger = {"observed": observed, "threshold": threshold, "direction": fired, "source": ev.get("source"),
               "data_type": data_type, "delayed": data_type != "REALTIME",
               "label": "triggered on delayed data" if data_type != "REALTIME" else None,
               "ts_source_ns": ev.get("ts_source_ns"), "ts_recv_ns": ev.get("ts_recv_ns"), "key": ev.get("key"), **meta}
    return trigger, new, _status(True)


def evaluate_time(rule: dict, state: dict, now_ns: int):
    if state.get("fired") or now_ns < rule["at_ns"]:
        return None, state
    return ({"observed": str(now_ns), "threshold": rule["at"], "direction": None, "source": "clock",
             "data_type": "REALTIME", "delayed": False, "late_by_ns": now_ns - rule["at_ns"]}, {**state, "fired": True})


def news_matches(rule: dict, item: dict, ciks: set[int]) -> list[int]:
    """CIKs of the rule's issuer that tag this item, or [] (duplicates and other forms never match)."""
    if item.get("duplicate_of") is not None:
        return []
    form = (item.get("form") or "").upper()
    if rule["kind"] == "filing" and not form:
        return []
    if rule["forms"] and not any(form == f or form == f + "/A" for f in rule["forms"]):
        return []
    return sorted({int(e["value"]) for e in item.get("entities", []) if e.get("scheme") == "SEC_CIK"} & ciks)


def news_trigger(item: dict, hit: list[int]) -> dict:
    return {"observed": item.get("title"), "threshold": None, "direction": None, "source": item.get("source_id"),
            "data_type": "REALTIME", "delayed": False, "item_id": item.get("id"), "form": item.get("form"),
            "accession": item.get("accession"), "link": item.get("link"), "published": item.get("published"),
            "first_seen_ns": item.get("first_seen_ns"), "issuers": hit,
            "untrusted_text": "headline text is publisher content, shown as data"}
