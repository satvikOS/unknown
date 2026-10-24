"""Wire objects of docs/design/marketdata-stream-contract.md (the shared format; change both together).

Every decimal is a string and never a float. Timestamps are integer nanoseconds since the Unix epoch, sent as strings
when they exceed 2^53 (current epoch nanoseconds always do). A field the source did not send is ``None`` (JSON null),
never zero.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

DATA_TYPES = ("REALTIME", "DELAYED", "FROZEN")   # real data only (ADR-0020)
STATUS_STATES = ("SUBSCRIBED", "UNAVAILABLE", "NOT_ENTITLED", "LINE_BUDGET", "STALE", "ENDED")
QUOTE_FIELDS = ("bid", "ask", "bid_size", "ask_size", "last", "last_size", "volume", "open", "high", "low",
                "prev_close")
SIZE_UNITS = ("shares", "round_lots")
MAX_SAFE_INT = 2 ** 53
MIC = re.compile(r"^[A-Z0-9]{4}$")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-/]{0,23}$")
_INSTRUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-:]{0,63}$")


class InvalidKey(ValueError):
    """A key that is neither ``<SYMBOL>.<MIC>`` nor a core instrument id."""


def parse_key(key: str) -> tuple[str, str | None]:
    """``AAPL.XNAS`` -> ("AAPL", "XNAS"); a core instrument id -> (id, None). Raises InvalidKey otherwise.

    The MIC is the part after the last dot when it is four upper-case letters or digits (ISO 10383 form, so ARCX and
    BATS parse as well as XNAS). Whether the MIC is in config/markets/venues.json is checked by the source that
    serves the key, which answers UNAVAILABLE for a venue it cannot map."""
    if not isinstance(key, str) or not key or len(key) > 64:
        raise InvalidKey(f"invalid key {key!r}")
    dot = key.rfind(".")
    if 0 < dot < len(key) - 1 and MIC.match(key[dot + 1:]) and _SYMBOL.match(key[:dot]):
        return key[:dot], key[dot + 1:]
    if _INSTRUMENT_ID.match(key):
        return key, None
    raise InvalidKey(f"invalid key {key!r}: expected <SYMBOL>.<MIC> or a core instrument id")


def dec(value) -> str | None:
    """Exact decimal text, or None. Accepts str, int, Decimal or any object whose str() is a decimal (Fixed)."""
    if value is None:
        return None
    if isinstance(value, float):
        raise TypeError("market data decimals are never floats")
    text = value if isinstance(value, str) else str(value)
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a decimal: {text!r}") from None
    if not d.is_finite():
        raise ValueError(f"not a finite decimal: {text!r}")
    return text


def ns(value: int | None):
    """Integer nanoseconds; as a string when above 2^53 (contract)."""
    if value is None:
        return None
    value = int(value)
    return str(value) if value > MAX_SAFE_INT else value


def check_data_type(data_type: str) -> str:
    if data_type not in DATA_TYPES:
        raise ValueError(f"data_type {data_type!r} not in {DATA_TYPES}")
    return data_type


def quote_event(key: str, seq: int, fields: dict, *, currency, source: str, data_type: str, feed, size_unit,
                ts_source_ns, ts_recv_ns, ts_pub_ns) -> dict:
    out = {"key": key, "seq": seq}
    for name in QUOTE_FIELDS:
        out[name] = fields.get(name)
    out.update({"currency": currency, "source": source, "data_type": data_type, "feed": feed, "size_unit": size_unit,
                "ts_source_ns": ns(ts_source_ns), "ts_recv_ns": ns(ts_recv_ns), "ts_pub_ns": ns(ts_pub_ns)})
    return out


def trade_event(key: str, seq: int, *, price, size, exchange, conditions, source: str, data_type: str, feed,
                ts_source_ns, ts_recv_ns, ts_pub_ns) -> dict:
    return {"key": key, "seq": seq, "price": dec(price), "size": dec(size), "exchange": exchange,
            "conditions": list(conditions or []), "ts_source_ns": ns(ts_source_ns), "ts_recv_ns": ns(ts_recv_ns),
            "ts_pub_ns": ns(ts_pub_ns), "source": source, "data_type": data_type, "feed": feed}


def depth_event(key: str, seq: int, *, side: str, levels: list, source: str, data_type: str, ts_recv_ns) -> dict:
    if side not in ("bid", "ask"):
        raise ValueError(f"depth side {side!r}")
    return {"key": key, "seq": seq, "side": side,
            "levels": [{"price": dec(lv["price"]), "size": dec(lv["size"]), "mm": lv.get("mm")} for lv in levels],
            "source": source, "data_type": data_type, "ts_recv_ns": ns(ts_recv_ns)}


# status state -> the unavailable-reason code it implies (ADR-0020); SUBSCRIBED carries none unless no data yet.
_STATE_CODE = {"NOT_ENTITLED": "NOT_ENTITLED", "LINE_BUDGET": "NOT_ENTITLED", "STALE": "STALE", "ENDED": "NO_DATA"}


def status_code(state: str, reason: str) -> str | None:
    """The vocabulary code (``unknown.domain.unavailable``) for a status: a ``CODE:`` prefix of the reason wins."""
    from ..domain.unavailable import REASONS, classify
    head = (reason or "").split(":", 1)[0].strip()
    if head in REASONS:
        return head
    if state == "SUBSCRIBED":
        return "PENDING" if "no data yet" in (reason or "") else None
    return _STATE_CODE.get(state) or classify(reason)


def status_event(key: str, state: str, reason: str) -> dict:
    if state not in STATUS_STATES:
        raise ValueError(f"status state {state!r} not in {STATUS_STATES}")
    return {"key": key, "state": state, "reason": reason, "code": status_code(state, reason)}
