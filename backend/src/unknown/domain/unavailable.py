"""Why a value is not shown: the one reason vocabulary for every API value that is unavailable (ADR-0020).

The platform shows only real data. When a value is not available the API sends ``null`` for the value and a
reason code from :data:`REASONS`; the terminal renders an em dash that carries the code (hover, focus and
assistive technology), the way Bloomberg (``#N/A ...``), Capital IQ (``NA``/``NM``) and LSEG (``n/a`` with a
reason) do. A value is never replaced by zero, a made-up number or an example. An error is shown as an error.

Field convention (docs/design/unavailable-values.md):

* the value field is ``null``;
* ``<field>_reason`` (or ``reason`` on an object that describes one value) holds a code from :data:`REASONS`;
* a free-text ``<field>_detail`` / ``detail`` may say more (provider error text, the stale age, ...);
* older detail codes (``NOT_INGESTED``, ``NO_PRICE``, ...) stay where they are and map to a code with
  :func:`classify`.

Standard library only; importable from every layer.
"""
from __future__ import annotations

# code -> meaning. Order is the documented order (docs/design/unavailable-values.md).
REASONS: dict[str, str] = {
    "NO_SOURCE": "no data source is configured for this value",
    "NOT_CONNECTED": "the source is configured but not connected now",
    "NOT_ENTITLED": "the source refused it: no subscription, permission or licence for this value",
    "PENDING": "requested from the source; no value has arrived yet",
    "NO_DATA": "the source answered and has no value for this request (date, security or field)",
    "NOT_REPORTED": "the issuer or publisher did not report this value",
    "NOT_APPLICABLE": "the value does not apply to this item (a ratio with no meaning for it, ...)",
    "NOT_FOUND": "the security, series or record is not known to any configured source",
    "STALE": "the last value is older than its freshness budget, so it is not shown as current",
    "ERROR": "the source or the computation failed; the detail carries the error",
}

NO_SOURCE = "NO_SOURCE"
NOT_CONNECTED = "NOT_CONNECTED"
NOT_ENTITLED = "NOT_ENTITLED"
PENDING = "PENDING"
NO_DATA = "NO_DATA"
NOT_REPORTED = "NOT_REPORTED"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_FOUND = "NOT_FOUND"
STALE = "STALE"
ERROR = "ERROR"

# Specific codes already used by adapters and stores -> the vocabulary code they belong to.
_DETAIL = {
    "UNAVAILABLE": NO_DATA, "NOT_AVAILABLE": NO_DATA, "NOT_INGESTED": NO_DATA, "NO_PRICE": NO_DATA,
    "NO_QUOTE": NO_DATA, "NO_BARS": NO_DATA, "NO_BAR": NO_DATA, "NO_TRADE": NO_DATA, "NO_SNAPSHOT": NO_DATA,
    "NOT_AVAILABLE_AT_AS_OF": NO_DATA, "NO_FUNDAMENTALS_AT_AS_OF": NO_DATA, "NO_VISIBLE_DATA": NO_DATA,
    "NO_FILINGS_INDEXED": NO_DATA, "COVERAGE_GAP": NO_DATA, "NOT_COVERED": NO_SOURCE,
    "NOT_YET_PUBLISHED": NOT_REPORTED, "NOT_RECEIVED": PENDING, "NOT_READY": PENDING,
    "NOT_DERIVABLE": NOT_APPLICABLE, "NO_SESSION": NOT_APPLICABLE,
    "NOT_LICENSED": NOT_ENTITLED, "NOT_SUBSCRIBED": NOT_ENTITLED, "NOT_SUBSCRIBED_DELAYED_DISABLED": NOT_ENTITLED,
    "NO_SECURITY_DEFINITION": NOT_FOUND, "NO_MATCH": NOT_FOUND, "UNKNOWN": ERROR,
    "STATUS_FILE_STALE": STALE, "NO_QUOTE_SOURCE": NO_SOURCE,
}


def is_reason(code: object) -> bool:
    return isinstance(code, str) and code in REASONS


def classify(code: str | None) -> str:
    """The vocabulary code for ``code``: itself when it is one, its mapping when it is a known detail code (a
    ``CODE: text`` string is read up to the colon), otherwise ``ERROR`` (an unknown reason is never hidden)."""
    if not code:
        return ERROR
    head = str(code).split(":", 1)[0].strip()
    if head in REASONS:
        return head
    return _DETAIL.get(head, ERROR)


def unavailable(reason: str, detail: str | None = None, **extra) -> dict:
    """``{"value": None, "reason": <code>, "detail": ...}`` for an object that describes one value."""
    if reason not in REASONS:
        raise ValueError(f"unknown unavailable reason {reason!r}; use one of {sorted(REASONS)}")
    return {"value": None, "reason": reason, "detail": detail, **extra}


def vocabulary() -> list[dict]:
    """The vocabulary as served by GET /api/meta/unavailable-reasons (and checked against the design doc)."""
    return [{"code": k, "meaning": v} for k, v in REASONS.items()]
