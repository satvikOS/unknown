"""Pure translation rules from TWS API values to the broker port.

No ``ibapi`` import: these functions take plain values so they can be tested
and audited on their own. Every rule cites the verified research record
(``capreg/ibkr-integration.json``, records R1..R14, fetched 2026-10-22) or the
pinned SDK source (TWS API 10.51.01).
"""
from __future__ import annotations

import math
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...domain.fixed import Fixed
from ..port import BrokerOrderStatus

# Unset sentinels: values of ibapi/const.py in the pinned SDK (UNSET_DOUBLE = float(sys.float_info.max),
# UNSET_INTEGER = 2**31 - 1, UNSET_LONG = 2**63 - 1, UNSET_DECIMAL = Decimal(2**127 - 1)). R5/R6: "Unset doubles
# (1.7976931348623157E308) ... map to null"; "Unset values parse to null and never to 0".
UNSET_DOUBLE = float(sys.float_info.max)
UNSET_INTEGER = 2**31 - 1
UNSET_LONG = 2**63 - 1
UNSET_DECIMAL = Decimal(2**127 - 1)

# ------------------------------------------------------------------ error codes
# Sources: error-codes.md and system-message-codes.md (vibkr pages 014, 069) and records R2, R3, R4, R7, R8, R11.
UPSTREAM_DOWN = frozenset({1100, 2110})          # R7: host lost IB connectivity / host-server connectivity broken
RESTORED_DATA_LOST = 1101                        # R7: restored, market data lost (resubscribe)
RESTORED_DATA_KEPT = 1102                        # R7: restored, data maintained
SOCKET_PORT_RESET = 1300                         # R7: socket port changed; reconnect on the new port
CONNECT_FAIL = 502                               # R2: OS could not open the socket (host down / API disabled / port)
ALREADY_CONNECTED = 501
NOT_CONNECTED = 504
CLIENT_ID_IN_USE = 326                           # error-codes.md; ADR-0011 #4: refuse to run
HOST_OUT_OF_DATE = 503                           # R1: READ_ONLY (no new orders) and alert
PACING_VIOLATION = 100                           # R8: back off; third violation terminates the session
INFORMATIONAL = frozenset({2104, 2106, 2107, 2108, 2158, 2119})   # R7: farm OK / inactive / connecting: never alert
DATA_FARM_DOWN = frozenset({2103, 2105})         # R7: market/historical data farm disconnected (not order flow)
MARKET_DATA_ONLY = frozenset({10197, 354, 10089, 10090, 10186})   # R12 entitlement codes: not order connectivity
DUPLICATE_ORDER_ID = 103                         # R3: id <= an earlier id from this client
ORDER_REJECTED = 201                             # R4: "Order rejected - Reason:"
ORDER_CANCELLED_BY_SYSTEM = 202                  # R4: "Order cancelled - Reason:" (price far through the market)
# Rejections of a new order (the host did not accept it). 201/103 from R3/R4; the rest are documented in
# error-codes.md as request-level refusals of the order itself.
SUBMIT_REJECT_CODES = frozenset({103, 110, 200, 201, 203, 426, 434, 435, 10268, 10269})
# R4: "Modify and cancel errors: 104 (filled), ... 135 and 10147 (not found), 136, 161 and 10148 (not cancellable)".
CANCEL_REJECT_CODES = frozenset({104, 135, 136, 161, 10147, 10148})
# Warnings attached to an order that do not decide its fate: 2109 outsideRth ignored (order processed), 10311 direct
# routing notice, 2168/2169 attribute warnings, 399 order message error (text varies), 2102 modify still processing.
ORDER_WARNINGS = frozenset({399, 2102, 2109, 2168, 2169, 10311})
# R11: 426 (cash account cannot go short) and 404 (held while locating shares) must alert and block entries.
SHORT_SALE_SIGNALS = frozenset({404, 426})

LOGIN_FAILED_MARKER = "Login failed"            # R9: "Login failed = Soft token=0 received ..." = manual login needed
AUTH_REQUIRED_REASON = "AUTH_REQUIRED: manual login in TWS/IB Gateway (2FA); no automation"

# ------------------------------------------------------------------ statuses
# R4 / understanding-order-status-message.md (vibkr 047): the documented status set.
DOCUMENTED_STATUSES = frozenset({"Inactive", "PendingSubmit", "PreSubmitted", "Submitted", "Filled", "PendingCancel",
                                 "PreCancelled", "Cancelled", "WarnState"})
ACCEPTANCE_STATUSES = frozenset({"PreSubmitted", "Submitted", "Filled", "WarnState", "PendingCancel", "PreCancelled"})
CANCEL_ACK_STATUSES = frozenset({"PendingCancel", "PreCancelled", "Cancelled"})
OPEN_ORDER_EXCLUDED = frozenset({"Filled", "Cancelled"})   # open-order requests never return these (R3)


def normalize_status(status: str, filled: Fixed | None, *, rejection_seen: bool) -> tuple[BrokerOrderStatus | None, str]:
    """Map an IBKR status string to the port's status (R4 mapping table).

    Returns ``(None, note)`` for a string outside the documented set; R4 says an
    unknown status must not be guessed but alerted and reconciled."""
    has_fills = filled is not None and filled.is_positive()
    if status == "PendingSubmit":
        return BrokerOrderStatus.PENDING_SUBMIT, ""
    if status in ("PreSubmitted", "Submitted"):
        return (BrokerOrderStatus.PARTIALLY_FILLED if has_fills else BrokerOrderStatus.ACKNOWLEDGED), ""
    if status == "WarnState":
        return (BrokerOrderStatus.PARTIALLY_FILLED if has_fills else BrokerOrderStatus.ACKNOWLEDGED), \
            "WARNING: IBKR WarnState (order has a specific warning message)"
    if status == "Filled":
        return BrokerOrderStatus.FILLED, ""
    if status in ("PendingCancel", "PreCancelled"):
        return BrokerOrderStatus.PENDING_CANCEL, "cancel not yet confirmed; executions may still arrive"
    if status == "Cancelled":
        if rejection_seen and not has_fills:
            return BrokerOrderStatus.REJECTED, "Cancelled preceded by error 201/202 with no fills"
        return BrokerOrderStatus.CANCELED, ""
    if status == "Inactive":
        return BrokerOrderStatus.INACTIVE, "IBKR Inactive (not working; e.g. precaution, hold or rejection)"
    return None, f"UNRECOGNIZED_STATUS: {status!r} is outside the documented IBKR status set"


def status_dedupe_key(perm_id: int, client_id: int, order_id: int, status: str, filled, remaining,
                      avg_fill_price, last_fill_price, why_held: str, mkt_cap_price) -> tuple:
    """R4 analytics: (permId or (clientId, orderId), status, filled, remaining, avgFillPrice, lastFillPrice,
    whyHeld, mktCapPrice)."""
    ident = ("perm", perm_id) if perm_id else ("client", client_id, order_id)
    return (ident, status, str(filled), str(remaining), repr(avg_fill_price), repr(last_fill_price), why_held or "",
            repr(mkt_cap_price))


# ------------------------------------------------------------------ numbers
def decimal_to_fixed(value) -> Fixed | None:
    """TWS ``Decimal`` sizes (shares, filled, remaining, position) to Fixed; unset -> None."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"not a decimal: {value!r}") from None
    if not isinstance(value, Decimal):
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    if not value.is_finite() or value == UNSET_DECIMAL:
        return None
    return Fixed.from_decimal(value).normalized()


def float_to_fixed(value) -> Fixed | None:
    """Broker binary floats to Fixed via ``repr`` (ADR-0004 #5); unset / non-finite -> None.

    The caller keeps the raw value next to the result."""
    if value is None:
        return None
    if type(value) is int:
        return None if value in (UNSET_INTEGER, UNSET_LONG) else Fixed(value)
    if type(value) is not float:
        raise TypeError(f"expected float, got {type(value).__name__}")
    if not math.isfinite(value) or value >= UNSET_DOUBLE:
        return None
    return Fixed.from_float_repr(value)


def summary_value_to_fixed(text: str) -> Fixed | None:
    """accountSummary values are strings (R6: "Values are strings with a currency and must be parsed to Decimal")."""
    if text is None or not str(text).strip():
        return None
    try:
        d = Decimal(str(text).strip())
    except InvalidOperation:
        return None
    if not d.is_finite() or abs(d) >= Decimal(repr(UNSET_DOUBLE)):
        return None
    return Fixed.from_decimal(d)


def fixed_to_order_float(value: Fixed) -> float:
    """Order.lmtPrice is a double in the SDK; refuse a price that does not survive the round trip."""
    as_float = float(Decimal(value.to_str()))
    back = Fixed.from_float_repr(as_float)
    if back != value:
        raise ValueError(f"price {value} is not exactly representable as an IBKR double")
    return as_float


# ------------------------------------------------------------------ executions
def side_from_execution(side: str) -> str | None:
    """Execution.side: "BOT for bought, SLD for sold" (the-execution-object.md)."""
    return {"BOT": "BUY", "SLD": "SELL"}.get(side)


def parse_exec_id(exec_id: str) -> tuple[str, int | None, int]:
    """R5 analytics: exec_base = text before the final '.', exec_rev = integer after it.

    Returns (base, revision or None, digit width of the revision)."""
    if "." not in exec_id:
        return exec_id, None, 0
    base, _, suffix = exec_id.rpartition(".")
    if not suffix.isdigit():
        return exec_id, None, 0
    return base, int(suffix), len(suffix)


def exec_key(account: str, exec_id: str) -> str:
    return f"ibkr:{account}:{exec_id}"


_TIME_UTC = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})$")
_TIME_LOCAL = re.compile(r"^(\d{4})(\d{2})(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\s+(\S+))?$")


def parse_exec_time(text: str, default_tz: str) -> tuple[int, str]:
    """Execution.time ("The execution's server time") to UTC nanoseconds.

    Formats from IBKR's own docs: "YYYYMMDD-HH:mm:ss" is UTC and "YYYYMMDD
    HH:mm:ss TMZ" carries a zone (execution-filter.md, vibkr 059; the general
    time format "yyyymmdd hh:mm:ss xxx ... E.g. 20031126 15:59:00 EST",
    error-codes.md). The zone suffix is resolved as an IANA name; a missing
    suffix uses the configured host zone and says so in the returned source.
    Raises ValueError for anything else: an execution with an unparseable time
    is held as unrepresentable, never booked at a guessed time."""
    text = (text or "").strip()
    m = _TIME_UTC.match(text)
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
        return int(dt.timestamp()) * 10**9, "utc"
    m = _TIME_LOCAL.match(text)
    if m:
        y, mo, d, h, mi, s = map(int, m.groups()[:6])
        zone_name = m.group(7)
        if zone_name:
            if zone_name.upper() in ("UTC", "GMT", "Z"):
                tz, source = timezone.utc, f"suffix:{zone_name}"
            else:
                try:
                    tz, source = ZoneInfo(zone_name), f"suffix:{zone_name}"
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError(f"unrecognized time zone suffix {zone_name!r} in execution time {text!r}") from None
        else:
            tz, source = ZoneInfo(default_tz), f"assumed:{default_tz}"
        dt = datetime(y, mo, d, h, mi, s, tzinfo=tz)
        return int(dt.timestamp()) * 10**9, source
    raise ValueError(f"unrecognized execution time format {text!r}")


def broker_order_key(our_client_id: int, client_id: int, order_id: int, perm_id: int) -> str:
    """broker_order_id: ``str(orderId)`` for our client; a namespaced key otherwise.

    orderId is per client and "May not be unique to an account" (Execution
    object, R3), so a foreign order must never share our ``str(orderId)``
    namespace; permId is "unique account-wide" (R3)."""
    if client_id == our_client_id:
        return str(order_id)
    if perm_id:
        return f"perm:{perm_id}"
    return f"client:{client_id}:order:{order_id}"
