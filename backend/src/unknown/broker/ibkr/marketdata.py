"""IBKR Level 1 streaming quotes, market-data line budget and entitlement states (read-only market data).

Capability records implemented here (docs/capabilities/register.json):

* CAP-MKT-01: Level 1 quotes carry the market-data type IBKR *delivered* (``EWrapper.marketDataType``: 1 live,
  2 frozen, 3 delayed, 4 delayed frozen), never the type requested. They also carry a source, a receipt time and
  a field-level null reason. "A tickPrice of -1 or 0 followed by a tickSize of 0" becomes null with the reason
  'no data currently available', never a price.
* CAP-MKT-02 and CAP-DATA-10: consolidation label. The owner's US equity feed is IBKR's free real-time Cboe One + IEX
  stream, which IBKR's pricing page calls *non-consolidated*. The label comes only from the owner's local feed
  configuration (an explicit registry assertion). Without one the label is ``unknown``, never ``consolidated``.
* CAP-IBKR-08: requests go through a pacing scheduler. The streaming line budget refuses a new subscription
  locally instead of triggering IBKR error 101. Error 100 triggers back-off.
* CAP-IBKR-12: entitlement errors 354, 10089, 10090, 10186 and 10197 become explicit, specific unavailable
  states. Only type 1 with no delayed tick ids can be marked live. Delayed or frozen data never passes as live.
* CAP-MKT-11: line headroom = allocated lines - active streaming subscriptions.

Quote mapping (``unknown.market.quotes.Quote`` has no field for consolidation or market-data type):
``source = "ibkr:<type>:<consolidation>"``, e.g. ``ibkr:live:non_consolidated``. ``Quote.delayed`` is True for
every quote that is not proven live: types 3 and 4, but also frozen (2), unreported, mismatched or unavailable.
``risk.pretrade`` checks only ``delayed`` and age, and a frozen quote with a receipt-time age would otherwise pass
as fresh. The core change needed is a ``market_data_type`` and a ``consolidation`` field on ``Quote``, plus a
pre-trade check on them (see docs/integrations/ibkr-marketdata.md).

Interruptions (CAP-MKT-01, CAP-IBKR-12): when a subscription's stream is interrupted (an entitlement error such as
10197, IBKR 1100/2110 upstream loss, a 2103 data-farm break, or socket loss), every field value received before the
interruption is discarded. IBKR streams only *changes*, so a value kept across an outage would be republished as
live, with a fresh receipt time, as soon as any one field ticked again. After resumption a quote holds only the
values received since then (it is one-sided or empty until both sides tick).

Publish ordering: ``QuoteBook.put`` drops a quote whose receipt time is older than the stored one. A publish never
goes backwards in time for its instrument, so a blanking publish is never dropped when the wall clock steps back.

Storage: no price value is written to disk by this module. CAP-IBKR-12 says to "treat [storage] as not permitted
for exchange data until verified". The status file holds statuses, entitlement states, ages and null reasons only.
Prices reach the core through the in-memory QuoteBook.

SDK surface. These are the only TWS API 10.51.01 (``ibapi``) names used, with their location in
IBJts/source/pythonclient/ibapi of the official 1051.01 package; the installed copy is byte-identical:
  EClient: connect client.py:442, disconnect :542, isConnected :554, run :586, reqMktData :744 (reqId, contract,
  genericTickList, snapshot, regulatorySnapshot, mktDataOptions), cancelMktData :931, reqMarketDataType :987,
  reqMarketRule :1112, reqContractDetails :4150, reqHistoricalData :4863, cancelHistoricalData :5073.
  EWrapper: error wrapper.py:147, connectAck :174, marketDataType :178, tickPrice :189, tickSize :196,
  tickSnapshotEnd :201, tickGeneric :207, tickString :210, connectionClosed :303, nextValidId :340,
  contractDetails :345, contractDetailsEnd :358, managedAccounts :439, historicalData :455,
  historicalDataEnd :473, tickReqParams :768, marketRule :817.
  Tick ids: ticktype.py TickTypeEnum (0-based): BID_SIZE 0, BID 1, ASK 2, ASK_SIZE 3, LAST 4, LAST_SIZE 5,
  HIGH 6, LOW 7, VOLUME 8, CLOSE 9, OPEN 14, LAST_TIMESTAMP 45, HALTED 49, DELAYED_BID 66 ... DELAYED_OPEN 76,
  DELAYED_LAST_TIMESTAMP 88, DELAYED_HALTED 90.
  decoder.py:177-195: every BID/ASK/LAST tickPrice is followed by the paired tickSize (which is how the
  "-1/0 then size 0" rule is observed).

Status (ADR-0001): implemented and tested offline against fakes. Never connected to IBKR; NOT paper-verified.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ...domain.clock import Clock, SystemClock
from ...domain.fixed import Fixed
from ...market.quotes import Quote, QuoteBook
from . import mapping as M
from .config import IbkrConfig
from .pacing import PacingScheduler

log = logging.getLogger(__name__)

STATUS_FILE_NAME = "ibkr-marketdata-status.json"   # under <UNKNOWN_HOME>/run (0700); read by api/routes/marketdata.py
STATUS_FILE_VERSION = 1
LOCAL_CONFIG_NAME = "ibkr_marketdata"              # ~/.unknown/config/ibkr_marketdata.json (outside git)

# ------------------------------------------------------------------ market data types (CAP-MKT-01, CAP-IBKR-12)
MARKET_DATA_TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}
DELAYED_TYPES = frozenset({3, 4})
CONSOLIDATION_VALUES = frozenset({"consolidated", "non_consolidated", "unknown"})

# ------------------------------------------------------------------ tick ids (ibapi/ticktype.py TickTypeEnum)
PRICE_TICKS = {1: "bid", 2: "ask", 4: "last", 6: "high", 7: "low", 9: "close", 14: "open"}
DELAYED_PRICE_TICKS = {66: "bid", 67: "ask", 68: "last", 72: "high", 73: "low", 75: "close", 76: "open"}
SIZE_TICKS = {0: "bid_size", 3: "ask_size", 5: "last_size", 8: "volume"}
DELAYED_SIZE_TICKS = {69: "bid_size", 70: "ask_size", 71: "last_size", 74: "volume"}
STRING_TICKS = {45: "last_timestamp"}
DELAYED_STRING_TICKS = {88: "last_timestamp"}
GENERIC_TICKS = {49: "halted"}
DELAYED_GENERIC_TICKS = {90: "halted"}
PAIRED_SIZE = {"bid": "bid_size", "ask": "ask_size", "last": "last_size"}
DELAYED_TICK_IDS = frozenset(DELAYED_PRICE_TICKS) | frozenset(DELAYED_SIZE_TICKS) | frozenset(DELAYED_STRING_TICKS) \
    | frozenset(DELAYED_GENERIC_TICKS)
NO_DATA_REASON = "no data currently available"     # CAP-MKT-01 wording

# ------------------------------------------------------------------ error codes
# Texts: IBKR message codes page (interactivebrokers.github.io/tws-api/message_codes.html, fetched 2026-10-22).
# 10089 is absent from that page; its meaning is from CAP-IBKR-12 ("error 10089 'API data requires subscription'").
ENTITLEMENT_ERRORS: dict[int, tuple[str, str, bool]] = {
    # code: (state, specific cause shown to the owner, terminal for this subscription)
    354: ("NOT_SUBSCRIBED", "Not subscribed to requested market data: no live data for this instrument on this "
                            "username, or the Market Data API Acknowledgement is not signed (CAP-IBKR-12)", True),
    10089: ("API_SUBSCRIPTION_REQUIRED", "Requested data requires an additional subscription for API use: free "
                                         "on-platform TWS data is not available through the API (CAP-IBKR-12)", True),
    10090: ("PARTIALLY_SUBSCRIBED", "Part of requested market data is not subscribed; some tick types need "
                                    "subscriptions this account does not hold", False),
    10186: ("NOT_SUBSCRIBED_DELAYED_DISABLED", "Requested market data is not subscribed and delayed market data is "
                                               "not enabled", True),
    10197: ("COMPETING_SESSION", "No market data during competing session: the live and paper usernames are logged "
                                 "in at the same time and IBKR gives the live session preference (CAP-IBKR-12)",
            False),
}
ENTITLEMENT_CODES = frozenset(ENTITLEMENT_ERRORS)
MAX_TICKERS = 101                       # "Max number of tickers has been reached" (TWS and API altogether)
NO_SECURITY_DEFINITION = 200
CANT_FIND_TICKER = 300                  # cancel for an id that is no longer subscribed: harmless
REQUEST_ERRORS = frozenset({321, 322})  # server error validating / processing an API client request
DATA_FARM_BROKEN = 2103                 # market data farm connection is broken (R7: DATA_FARM_DOWN)
DATA_FARM_OK = 2104

# ------------------------------------------------------------------ SDK citations used by the conformance test
EWRAPPER_CALLBACKS = ("error", "connectAck", "connectionClosed", "nextValidId", "managedAccounts", "marketDataType",
                      "tickPrice", "tickSize", "tickString", "tickGeneric", "tickReqParams", "tickSnapshotEnd",
                      "historicalData", "historicalDataEnd", "contractDetails", "contractDetailsEnd", "marketRule")
ECLIENT_METHODS = {
    "connect": ["self", "host", "port", "clientId"], "disconnect": ["self"], "isConnected": ["self"],
    "run": ["self"],
    "reqMktData": ["self", "reqId", "contract", "genericTickList", "snapshot", "regulatorySnapshot",
                   "mktDataOptions"],
    "cancelMktData": ["self", "reqId"], "reqMarketDataType": ["self", "marketDataType"],
    "reqHistoricalData": ["self", "reqId", "contract", "endDateTime", "durationStr", "barSizeSetting", "whatToShow",
                          "useRTH", "formatDate", "keepUpToDate", "chartOptions"],
    "cancelHistoricalData": ["self", "reqId"], "reqContractDetails": ["self", "reqId", "contract"],
    "reqMarketRule": ["self", "marketRuleId"],
}


class MarketDataConfigError(ValueError):
    """The owner's market-data configuration is missing or invalid."""


class MarketDataNotReady(ConnectionError):
    """No verified PAPER market-data session."""


# ============================================================================ configuration
@dataclass(frozen=True)
class FeedLabel:
    """Owner assertion about one feed (registry entry for CAP-MKT-02 'consolidation type comes from feed metadata')."""
    consolidation: str          # consolidated | non_consolidated | unknown
    label: str
    evidence: str               # where the assertion comes from (e.g. CAP-DATA-10 + owner screenshot date)
    observed_on: str

    def to_json(self) -> dict:
        return {"consolidation": self.consolidation, "label": self.label, "evidence": self.evidence,
                "observed_on": self.observed_on}


UNKNOWN_FEED = FeedLabel("unknown", "no feed label configured", "none", "")
_MD_KEYS = frozenset({"client_id", "requested_market_data_type", "tws_reserved_lines", "pacing_share_percent",
                      "feeds", "freshness_budgets_ms", "comment"})


@dataclass(frozen=True)
class MarketDataConfig:
    """Local market-data settings (``~/.unknown/config/ibkr_marketdata.json``; holds no account identifier).

    ``client_id`` is the API client id of the separate, read-only market-data connection. It must differ from the
    order adapter's client id: the adapter's EWrapper does not forward tick callbacks, and orders are never sent
    on this connection."""
    client_id: int
    requested_market_data_type: int = 1
    tws_reserved_lines: int = 0          # lines the owner uses in TWS watchlists (lines are shared, CAP-IBKR-08)
    pacing_share_percent: int = 50       # share of floor(lines / 2) req/s given to this connection
    feeds: dict = field(default_factory=dict)          # "STK:USD" -> FeedLabel
    freshness_budgets_ms: dict = field(default_factory=dict)   # display / signal / pre_trade (owner-set)

    def validate(self, order_client_id: int | None = None, lines: int | None = None) -> None:
        problems = []
        if type(self.client_id) is not int or not 0 < self.client_id < 2**31 - 1:
            problems.append("client_id must be a positive integer (0 binds manual TWS orders)")
        elif order_client_id is not None and self.client_id == order_client_id:
            problems.append("client_id must differ from the order adapter's client_id")
        if self.requested_market_data_type not in MARKET_DATA_TYPES:
            problems.append("requested_market_data_type must be 1, 2, 3 or 4")
        if type(self.tws_reserved_lines) is not int or self.tws_reserved_lines < 0 or \
                (lines is not None and self.tws_reserved_lines >= lines):
            problems.append("tws_reserved_lines must be an integer >= 0 and below market_data_lines")
        if type(self.pacing_share_percent) is not int or not 1 <= self.pacing_share_percent <= 90:
            problems.append("pacing_share_percent must be an integer in [1, 90]")
        for key, feed in self.feeds.items():
            if not isinstance(feed, FeedLabel) or feed.consolidation not in CONSOLIDATION_VALUES:
                problems.append(f"feed {key}: consolidation must be one of {sorted(CONSOLIDATION_VALUES)}")
            elif feed.consolidation != "unknown" and not feed.evidence.strip():
                problems.append(f"feed {key}: a consolidation claim needs evidence")
        for use, budget in self.freshness_budgets_ms.items():
            if use not in ("display", "signal", "pre_trade") or type(budget) is not int or budget <= 0:
                problems.append("freshness_budgets_ms keys are display/signal/pre_trade with positive int ms")
        if problems:
            raise MarketDataConfigError("; ".join(problems))

    def feed_for(self, sec_type: str, currency: str) -> FeedLabel:
        return self.feeds.get(f"{sec_type}:{currency}", UNKNOWN_FEED)


def market_data_config_from_dict(data: dict) -> MarketDataConfig:
    if not isinstance(data, dict):
        raise MarketDataConfigError("configuration must be a JSON object")
    unknown = sorted(set(data) - _MD_KEYS)
    if unknown:
        raise MarketDataConfigError(f"unknown configuration keys: {unknown}")
    if "client_id" not in data:
        raise MarketDataConfigError("client_id (market-data connection) is required")
    feeds = {}
    for key, raw in (data.get("feeds") or {}).items():
        if not isinstance(raw, dict):
            raise MarketDataConfigError(f"feed {key} must be an object")
        feeds[key] = FeedLabel(str(raw.get("consolidation", "")), str(raw.get("label", "")),
                               str(raw.get("evidence", "")), str(raw.get("observed_on", "")))
    cfg = MarketDataConfig(client_id=data["client_id"],
                           requested_market_data_type=data.get("requested_market_data_type", 1),
                           tws_reserved_lines=data.get("tws_reserved_lines", 0),
                           pacing_share_percent=data.get("pacing_share_percent", 50), feeds=feeds,
                           freshness_budgets_ms=dict(data.get("freshness_budgets_ms") or {}))
    cfg.validate()
    return cfg


def load_market_data_config(home: Path | None = None) -> MarketDataConfig:
    """Read ``<UNKNOWN_HOME>/config/ibkr_marketdata.json`` (see docs/integrations/ibkr-marketdata.md)."""
    from ...service.paths import Paths
    paths = Paths(home) if home else Paths.default()
    path = paths.root / "config" / f"{LOCAL_CONFIG_NAME}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MarketDataConfigError(f"no market-data configuration at {path}; see "
                                    "docs/integrations/ibkr-marketdata.md") from None
    except json.JSONDecodeError as exc:
        raise MarketDataConfigError(f"{path} is not valid JSON: {exc}") from None
    return market_data_config_from_dict(data)


def split_host_budget(config: IbkrConfig, md_config: MarketDataConfig, *, monotonic_ns=None,
                      sleep=None) -> tuple[PacingScheduler, PacingScheduler]:
    """Two schedulers whose per-second limits sum to at most R_max = floor(lines / 2) (CAP-IBKR-08).

    IBKR states the request limit for the username, not per connection; the order connection and the
    market-data connection therefore split it. The service must pass the first scheduler to ``IbkrAdapter(pacer=)``;
    without that, the adapter keeps the full R_max and the sum can exceed it."""
    r_max = config.max_requests_per_second
    md = max(1, (r_max * md_config.pacing_share_percent) // 100)
    orders = r_max - md
    if orders < 1:
        raise MarketDataConfigError("market_data_lines too small to split between orders and market data")
    kw = {}
    if monotonic_ns is not None:
        kw["monotonic_ns"] = monotonic_ns
    if sleep is not None:
        kw["sleep"] = sleep
    return (PacingScheduler(orders, config.reserved_pacing_percent, **kw),
            PacingScheduler(md, config.reserved_pacing_percent, **kw))


# ============================================================================ line budget
class LineBudget:
    """Streaming market-data lines (CAP-IBKR-08 / CAP-DATA-10).

    Capacity = allocated lines - lines the owner reserves for TWS watchlists. Lines are shared between TWS and every
    API connection of the username; exceeding them gives error 101, so a subscription is refused locally first."""

    def __init__(self, lines: int, reserved_lines: int = 0):
        if type(lines) is not int or lines < 1 or type(reserved_lines) is not int or not 0 <= reserved_lines < lines:
            raise ValueError("lines must be a positive int and reserved_lines in [0, lines)")
        self.lines = lines
        self.reserved_lines = reserved_lines
        self._held: set[int] = set()
        self.refused = 0

    @property
    def capacity(self) -> int:
        return self.lines - self.reserved_lines

    @property
    def in_use(self) -> int:
        return len(self._held)

    @property
    def headroom(self) -> int:
        """CAP-MKT-11: line headroom = allocated lines - active streaming subscriptions (here, minus TWS reserve)."""
        return self.capacity - self.in_use

    def acquire(self, req_id: int) -> bool:
        if req_id in self._held:
            return True
        if self.in_use >= self.capacity:
            self.refused += 1
            return False
        self._held.add(req_id)
        return True

    def release(self, req_id: int) -> None:
        self._held.discard(req_id)

    def to_json(self) -> dict:
        return {"lines": self.lines, "tws_reserved_lines": self.reserved_lines, "capacity": self.capacity,
                "in_use": self.in_use, "headroom": self.headroom, "refused_locally": self.refused,
                "formula": "mkt.line_headroom@1 = lines - tws_reserved_lines - active API subscriptions"}


# ============================================================================ contracts for requests
@dataclass(frozen=True)
class ContractSpec:
    """What is sent to IBKR. conId is preferred; a symbol needs a primary exchange (never resolve by name)."""
    sec_type: str = "STK"
    currency: str = "USD"
    exchange: str = "SMART"
    con_id: int = 0
    symbol: str = ""
    primary_exchange: str = ""

    def problems(self) -> list[str]:
        out = []
        if self.sec_type != "STK":
            out.append(f"secType {self.sec_type!r} is not implemented for market data (STK only)")
        if type(self.con_id) is not int or self.con_id < 0:
            out.append("con_id must be a non-negative int")
        if not self.con_id and not (self.symbol and self.primary_exchange):
            out.append("a conId, or a symbol with its primary exchange, is required (no lookup by name)")
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            out.append("currency must be an ISO 4217 code")
        if self.primary_exchange.upper() == "SMART":
            out.append("primary_exchange must be an actual exchange, never SMART (ibapi contract.py)")
        return out

    def to_sdk(self, contract_cls) -> object:
        c = contract_cls()
        c.secType, c.currency, c.exchange = self.sec_type, self.currency, self.exchange
        if self.con_id:
            c.conId = self.con_id
        if self.symbol:
            c.symbol = self.symbol
        if self.primary_exchange:
            c.primaryExchange = self.primary_exchange
        return c

    def to_json(self) -> dict:
        return {"secType": self.sec_type, "currency": self.currency, "exchange": self.exchange, "conId": self.con_id,
                "symbol": self.symbol, "primaryExchange": self.primary_exchange}

    @property
    def feed_key(self) -> str:
        return f"{self.sec_type}:{self.currency}"


# ============================================================================ quote state
@dataclass
class FieldValue:
    value: Fixed | None
    raw: str
    receipt_ns: int
    null_reason: str | None = None


def classify_quote(bid: Fixed | None, ask: Fixed | None) -> str:
    """CAP-MKT-01: crossed if bid > ask; locked if bid = ask; one_sided if one side null; empty if both null."""
    if bid is None and ask is None:
        return "empty"
    if bid is None or ask is None:
        return "one_sided"
    if bid > ask:
        return "crossed"
    if bid == ask:
        return "locked"
    return "normal"


@dataclass
class _Subscription:
    req_id: int
    instrument_id: str
    contract: ContractSpec
    feed: FeedLabel
    requested_type: int
    created_ns: int
    delivered_type: int | None = None
    live_fields: dict = field(default_factory=dict)
    delayed_fields: dict = field(default_factory=dict)
    delayed_ticks_seen: bool = False
    last_receipt_ns: int | None = None
    ticks: int = 0
    min_tick: str | None = None
    bbo_exchange: str | None = None
    snapshot_permissions: int | None = None
    unavailable: dict | None = None
    cancel_required: bool = False
    active: bool = True
    resubscribe_required: bool = False
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    state_counts: dict = field(default_factory=dict)
    last_state: str | None = None

    def fields(self) -> dict:
        if self.delivered_type in DELAYED_TYPES or (self.delivered_type is None and self.delayed_ticks_seen):
            return self.delayed_fields
        return self.live_fields

    def value(self, name: str) -> Fixed | None:
        fv = self.fields().get(name)
        return fv.value if fv is not None else None

    def type_name(self) -> str:
        if self.delivered_type is None:
            return "delayed" if self.delayed_ticks_seen else "unreported"
        return MARKET_DATA_TYPES[self.delivered_type]

    def mismatch(self) -> bool:
        """Type 1 reported but delayed tick ids observed: never trusted as live."""
        return self.delivered_type in (1, 2) and self.delayed_ticks_seen

    def is_live(self, connection_stale: str | None) -> bool:
        return (self.delivered_type == 1 and not self.delayed_ticks_seen and self.unavailable is None
                and self.active and not self.resubscribe_required and connection_stale is None)

    def status(self, connection_stale: str | None) -> str:
        if not self.active:
            return "CANCELLED" if self.unavailable is None else "UNAVAILABLE"
        if self.unavailable is not None:
            return "UNAVAILABLE"
        if connection_stale is not None or self.resubscribe_required:
            return "STALE"
        if self.mismatch():
            return "TYPE_MISMATCH"
        if self.delivered_type is not None:
            return MARKET_DATA_TYPES[self.delivered_type].upper()
        if self.ticks:
            return "DELAYED" if self.delayed_ticks_seen else "TYPE_UNREPORTED"
        return "PENDING"

    def consolidation(self) -> str:
        # The feed label describes the subscribed real-time feed; delayed data's consolidation is not asserted.
        return self.feed.consolidation if self.delivered_type in (1, 2) and not self.delayed_ticks_seen else "unknown"


# ============================================================================ SDK wrapper
def _md_wrapper_class(base: type) -> type:
    """EWrapper subclass forwarding the callbacks of this module (signatures = ibapi/wrapper.py 10.51.01)."""

    class MarketDataWrapper(base):
        def __init__(self, session: "IbkrMarketDataSession"):
            base.__init__(self)
            self._session = session

        def error(self, reqId, errorTime: int, errorCode: int, errorString: str, advancedOrderRejectJson=""):
            self._session._dispatch("error", reqId, errorTime, errorCode, errorString, advancedOrderRejectJson)

        def connectAck(self):
            self._session._dispatch("connectAck")

        def connectionClosed(self):
            self._session._dispatch("connectionClosed")

        def nextValidId(self, orderId: int):
            self._session._dispatch("nextValidId", orderId)

        def managedAccounts(self, accountsList: str):
            self._session._dispatch("managedAccounts", accountsList)

        def marketDataType(self, reqId, marketDataType: int):
            self._session._dispatch("marketDataType", reqId, marketDataType)

        def tickPrice(self, reqId, tickType, price: float, attrib):
            self._session._dispatch("tickPrice", reqId, tickType, price, attrib)

        def tickSize(self, reqId, tickType, size):
            self._session._dispatch("tickSize", reqId, tickType, size)

        def tickString(self, reqId, tickType, value: str):
            self._session._dispatch("tickString", reqId, tickType, value)

        def tickGeneric(self, reqId, tickType, value: float):
            self._session._dispatch("tickGeneric", reqId, tickType, value)

        def tickReqParams(self, tickerId: int, minTick: float, bboExchange: str, snapshotPermissions: int):
            self._session._dispatch("tickReqParams", tickerId, minTick, bboExchange, snapshotPermissions)

        def tickSnapshotEnd(self, reqId: int):
            self._session._dispatch("tickSnapshotEnd", reqId)

        def historicalData(self, reqId: int, bar):
            self._session._dispatch("historicalData", reqId, bar)

        def historicalDataEnd(self, reqId: int, start: str, end: str):
            self._session._dispatch("historicalDataEnd", reqId, start, end)

        def contractDetails(self, reqId: int, contractDetails):
            self._session._dispatch("contractDetails", reqId, contractDetails)

        def contractDetailsEnd(self, reqId: int):
            self._session._dispatch("contractDetailsEnd", reqId)

        def marketRule(self, marketRuleId: int, priceIncrements):
            self._session._dispatch("marketRule", marketRuleId, priceIncrements)

    return MarketDataWrapper


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


@dataclass(frozen=True)
class SubscribeResult:
    ok: bool
    instrument_id: str
    req_id: int | None = None
    reason: str = ""


# ============================================================================ session
class IbkrMarketDataSession:
    """One read-only TWS API connection for quotes, historical bars and contract details (PAPER only).

    It never places, modifies or cancels orders. It verifies the same PAPER identity as the order adapter (configured
    paper port, managedAccounts == the expected paper account), using its own client id. ``history.HistoryClient``
    and ``contracts.ContractResolver`` use ``request``, ``register`` and ``wait`` on this session."""

    def __init__(self, config: IbkrConfig, md_config: MarketDataConfig, *, quote_book: QuoteBook | None = None,
                 clock: Clock | None = None, client_factory: Callable[[object], object] | None = None,
                 sdk=None, pacer: PacingScheduler | None = None, status_path: Path | None = None):
        config.validate()                       # PAPER-only profile, loopback host, paper port
        md_config.validate(order_client_id=config.client_id, lines=config.market_data_lines)
        self._cfg = config
        self._md = md_config
        if sdk is None:
            from .adapter import load_sdk      # imports ibapi lazily; raises IbkrSdkUnavailable with install help
            sdk = load_sdk(config)
        self._sdk = sdk
        self._clock = clock or SystemClock()
        self._book = quote_book if quote_book is not None else QuoteBook()
        self._client_factory = client_factory or (lambda wrapper: self._sdk.EClient(wrapper))
        self._wrapper = _md_wrapper_class(self._sdk.EWrapper)(self)
        if pacer is None:
            _, pacer = split_host_budget(config, md_config)
        self._pacer = pacer
        self.lines = LineBudget(config.market_data_lines, md_config.tws_reserved_lines)
        self._status_path = status_path
        self.historical_pacer = None            # set by the first history.HistoryClient; shared by all of them
        self.cv = threading.Condition(threading.RLock())
        self._client = None
        self._socket_up = False
        self._verified = False
        self._next_valid_id: int | None = None
        self._managed: tuple[str, ...] | None = None
        self._unsafe: str | None = None
        self._reason: str | None = None
        self._upstream_down: int | None = None
        self._farms_down: set[str] = set()
        self._req_seq = 0
        self._subs: dict[int, _Subscription] = {}
        self._by_instrument: dict[str, int] = {}
        self._handlers: dict[int, object] = {}
        self._rule_listeners: list[Callable[[int, list], None]] = []
        self._audit: deque = deque(maxlen=500)
        self._anomalies: deque = deque(maxlen=500)
        self.requests_sent = 0
        self.entitlement_errors = 0
        self.pacing_violations = 0
        self.publish_clock_regressions = 0      # publishes whose wall-clock time was behind the stored quote

    # ------------------------------------------------------------------ properties used by history/contracts
    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def sdk(self):
        return self._sdk

    @property
    def config(self) -> IbkrConfig:
        return self._cfg

    @property
    def quote_book(self) -> QuoteBook:
        return self._book

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> dict:
        """Open the socket, wait for nextValidId and managedAccounts, and verify the PAPER identity.

        Never automates login (R9). On identity mismatch the session latches unsafe and disconnects."""
        cfg = self._cfg
        with self.cv:
            if self._unsafe or self._socket_up:
                return self.connection_state()
            self._next_valid_id, self._managed, self._verified, self._reason = None, None, False, None
        client = self._client_factory(self._wrapper)
        with self.cv:
            self._client = client

        def _connect():
            try:
                client.connect(cfg.host, cfg.port, self._md.client_id)
            except Exception as exc:
                with self.cv:
                    self._reason = f"connect raised {type(exc).__name__}: {exc}"

        t = threading.Thread(target=_connect, name="ibkr-md-connect", daemon=True)
        t.start()
        t.join(cfg.connect_timeout_s)
        if t.is_alive():
            _safe(client.disconnect)
            with self.cv:
                self._reason = f"HANDSHAKE_TIMEOUT after {cfg.connect_timeout_s}s"
        if not _safe(client.isConnected, False):
            with self.cv:
                self._reason = self._reason or "HOST_DOWN: could not open the socket (502)"
                return self.connection_state()
        with self.cv:
            self._socket_up = True
        threading.Thread(target=self._run_loop, args=(client,), name="ibkr-md-msg", daemon=True).start()
        problems: list[str] = []
        with self.cv:
            got = self.cv.wait_for(lambda: (self._next_valid_id is not None and self._managed is not None)
                                   or not self._socket_up or self._unsafe is not None, timeout=cfg.connect_timeout_s)
            if self._unsafe is not None or not self._socket_up:
                return self.connection_state()
            if not got:
                self._reason = "AUTH_REQUIRED: manual login in TWS/IB Gateway (no nextValidId/managedAccounts)"
                return self.connection_state()
            connected_port = getattr(client, "port", cfg.port)
            if connected_port != cfg.profile_port:
                problems.append(f"port {connected_port} is not the PAPER profile port {cfg.profile_port}")
            if set(self._managed or ()) != {cfg.expected_account_id}:
                problems.append(f"managed accounts {len(self._managed or ())} entries != the expected paper account "
                                f"{cfg.masked_account()}")
            if problems:
                self._unsafe = "DISCONNECTED_UNSAFE: " + "; ".join(problems)
                self._anomaly("IDENTITY_MISMATCH", self._unsafe)
        if problems:
            _safe(client.disconnect)
            with self.cv:
                self._socket_up = False
                return self.connection_state()
        with self.cv:
            self._verified = True
            self._audit_entry(None, "SESSION_READY", f"PAPER identity verified; client {self._md.client_id}")
        # reqMarketDataType applies to later reqMktData calls; the delivered type is still taken from marketDataType.
        err = self.request("query", "reqMarketDataType", self._md.requested_market_data_type)
        if err:
            with self.cv:
                self._anomaly("REQ_MARKET_DATA_TYPE_FAILED", err)
        return self.connection_state()

    def disconnect(self) -> None:
        with self.cv:
            client = self._client
        if client is not None:
            _safe(client.disconnect)
        with self.cv:
            self._socket_lost("disconnect requested")

    def _run_loop(self, client) -> None:
        try:
            client.run()
        except Exception as exc:
            with self.cv:
                self._anomaly("MESSAGE_LOOP_FAILED", f"{type(exc).__name__}: {exc}")
        finally:
            if not _safe(client.isConnected, False):
                with self.cv:
                    if self._client is client:
                        self._socket_lost("message loop ended")

    def ready_reason(self) -> str | None:
        with self.cv:
            if self._unsafe:
                return self._unsafe
            if not self._socket_up or self._client is None:
                return self._reason or "NOT_CONNECTED"
            if not self._verified:
                return self._reason or "MODE_UNVERIFIED"
            return None

    def connection_state(self) -> dict:
        with self.cv:
            return {"connected": self._socket_up, "verified_paper": self._verified,
                    "reason": None if (self._verified and self._socket_up) else
                    (self._unsafe or self._reason or "NOT_CONNECTED"),
                    "upstream_down": self._upstream_down, "data_farms_down": sorted(self._farms_down),
                    "client_id": self._md.client_id, "account": self._cfg.masked_account(),
                    "host": f"{self._cfg.host}:{self._cfg.port}", "evidence_level": "OFFLINE_TESTED_NOT_PAPER_VERIFIED"}

    def _connection_stale(self) -> str | None:
        if not self._socket_up:
            return "SOCKET_DOWN"
        if self._upstream_down is not None:
            return f"UPSTREAM_DOWN {self._upstream_down}"
        if self._farms_down:
            return "MARKET_DATA_FARM_DOWN " + ",".join(sorted(self._farms_down))
        return None

    # ------------------------------------------------------------------ request plumbing (history, contracts)
    def new_req_id(self) -> int:
        with self.cv:
            self._req_seq += 1
            return self._req_seq

    def request(self, cls: str, method: str, *args) -> str | None:
        """Pace (CAP-IBKR-08) then call one EClient method; returns an error string instead of raising."""
        reason = self.ready_reason()
        if reason:
            return f"NOT_READY: {reason}"
        if not self._pacer.acquire(cls, self._cfg.pacing_wait_s):
            return f"PACING_REFUSED: request budget for {cls!r} exhausted (CAP-IBKR-08)"
        with self.cv:
            client = self._client
        try:
            getattr(client, method)(*args)
        except Exception as exc:
            return f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}"
        with self.cv:
            self.requests_sent += 1
        return None

    def register(self, req_id: int, handler) -> None:
        with self.cv:
            self._handlers[req_id] = handler

    def unregister(self, req_id: int) -> None:
        with self.cv:
            self._handlers.pop(req_id, None)

    def add_market_rule_listener(self, fn: Callable[[int, list], None]) -> None:
        with self.cv:
            self._rule_listeners.append(fn)

    def remove_market_rule_listener(self, fn) -> None:
        with self.cv:
            if fn in self._rule_listeners:
                self._rule_listeners.remove(fn)

    def wait(self, predicate: Callable[[], bool], timeout_s: float) -> bool:
        with self.cv:
            return self.cv.wait_for(lambda: predicate() or not self._socket_up, timeout=timeout_s) and predicate()

    # ------------------------------------------------------------------ quotes: subscribe / cancel
    def subscribe(self, instrument_id: str, contract: ContractSpec) -> SubscribeResult:
        """reqMktData for one instrument (streaming, no snapshot, no regulatory snapshot: never a paid request)."""
        problems = contract.problems()
        if problems:
            return SubscribeResult(False, instrument_id, reason="REFUSED: " + "; ".join(problems))
        reason = self.ready_reason()
        if reason:
            return SubscribeResult(False, instrument_id, reason=f"NOT_READY: {reason}")
        with self.cv:
            existing = self._by_instrument.get(instrument_id)
            if existing is not None and self._subs[existing].active:
                return SubscribeResult(True, instrument_id, existing, "already subscribed")
            req_id = self.new_req_id()
            if not self.lines.acquire(req_id):
                self._audit_entry(instrument_id, "LINE_BUDGET_EXHAUSTED",
                                  f"{self.lines.in_use}/{self.lines.capacity} lines in use; not sent (CAP-IBKR-08)")
                return SubscribeResult(False, instrument_id, reason=(
                    f"LINE_BUDGET_EXHAUSTED: {self.lines.in_use} of {self.lines.capacity} lines in use "
                    f"({self.lines.lines} allocated, {self.lines.reserved_lines} reserved for TWS); refused locally "
                    "instead of triggering IBKR error 101"))
            sub = _Subscription(req_id, instrument_id, contract, self._md.feed_for(contract.sec_type, contract.currency),
                                self._md.requested_market_data_type, self._clock.now_ns())
            self._subs[req_id] = sub
            self._by_instrument[instrument_id] = req_id
        sdk_contract = contract.to_sdk(self._sdk.Contract)
        err = self.request("query", "reqMktData", req_id, sdk_contract, "", False, False, [])
        if err:
            with self.cv:
                sub.active = False
                self.lines.release(req_id)
                self._audit_entry(instrument_id, "SUBSCRIBE_NOT_SENT", err)
            return SubscribeResult(False, instrument_id, req_id, err)
        with self.cv:
            self._audit_entry(instrument_id, "SUBSCRIBED", f"reqMktData {req_id} requested type "
                                                          f"{MARKET_DATA_TYPES[sub.requested_type]}")
        return SubscribeResult(True, instrument_id, req_id, "requested; status follows marketDataType")

    def unsubscribe(self, instrument_id: str) -> bool:
        with self.cv:
            req_id = self._by_instrument.get(instrument_id)
            sub = self._subs.get(req_id) if req_id is not None else None
            if sub is None or not sub.active:
                return False
        err = self.request("cancel", "cancelMktData", req_id)
        with self.cv:
            if err is None:
                sub.active = False
                sub.cancel_required = False
                self.lines.release(req_id)
                self._audit_entry(instrument_id, "CANCELLED", f"cancelMktData {req_id}")
                self._publish(sub, blank=True)
                return True
            self._audit_entry(instrument_id, "CANCEL_NOT_SENT", err)
            return False

    def maintain(self) -> dict:
        """Periodic housekeeping by the owning service: cancel dead subscriptions (freeing lines), resubscribe after
        IBKR 1101 ('data lost'), and write the status file. Never called from the message thread."""
        cancelled, resubscribed = [], []
        with self.cv:
            dead = [s for s in self._subs.values() if s.active and s.cancel_required]
            lost = [s for s in self._subs.values() if s.active and s.resubscribe_required]
        for sub in dead:
            if self.request("cancel", "cancelMktData", sub.req_id) is None:
                with self.cv:
                    sub.active = False
                    sub.cancel_required = False
                    self.lines.release(sub.req_id)
                    cancelled.append(sub.instrument_id)
        for sub in lost:
            with self.cv:
                # Re-checked here: a subscription cancelled just above (terminal entitlement error followed by a
                # socket loss) or one now awaiting a cancel must never be requested again.
                if not sub.active or sub.cancel_required or not sub.resubscribe_required:
                    continue
            if self.request("query", "reqMktData", sub.req_id, sub.contract.to_sdk(self._sdk.Contract), "", False,
                            False, []) is None:
                with self.cv:
                    sub.resubscribe_required = False
                    sub.live_fields.clear()
                    sub.delayed_fields.clear()
                    self._audit_entry(sub.instrument_id, "RESUBSCRIBED", f"reqMktData {sub.req_id} after data loss")
                    resubscribed.append(sub.instrument_id)
        if self._status_path is not None:
            self.write_status_file(self._status_path)
        return {"cancelled": cancelled, "resubscribed": resubscribed}

    # ------------------------------------------------------------------ status
    def status_snapshot(self) -> dict:
        """Statuses and entitlement states only; no price values (see module docstring on storage)."""
        now = self._clock.now_ns()
        with self.cv:
            stale = self._connection_stale()
            subs = []
            for sub in sorted(self._subs.values(), key=lambda s: s.req_id):
                fields = {name: {"present": fv.value is not None, "null_reason": fv.null_reason,
                                 "receipt_ns": fv.receipt_ns} for name, fv in sorted(sub.fields().items())
                          if name != "last_timestamp"}
                subs.append({
                    "instrument_id": sub.instrument_id, "req_id": sub.req_id,
                    # False for an earlier record of a re-subscribed instrument (kept for the audit trail)
                    "current": self._by_instrument.get(sub.instrument_id) == sub.req_id,
                    "contract": sub.contract.to_json(),
                    "status": sub.status(stale), "live": sub.is_live(stale),
                    "requested_type": MARKET_DATA_TYPES[sub.requested_type],
                    "delivered_type": sub.delivered_type, "delivered_type_name": sub.type_name(),
                    "source": self._source(sub), "consolidation": sub.consolidation(), "feed": sub.feed.to_json(),
                    "unavailable": sub.unavailable, "connection_stale": stale,
                    "resubscribe_required": sub.resubscribe_required, "cancel_required": sub.cancel_required,
                    "quote_state": sub.last_state, "quote_state_counts": dict(sub.state_counts),
                    "fields": fields, "last_receipt_ns": sub.last_receipt_ns,
                    "age_ms": (now - sub.last_receipt_ns) // 1_000_000 if sub.last_receipt_ns else None,
                    "age_basis": "receipt time of the latest tick on this subscription (IBKR L1 carries no provider "
                                 "timestamp; CAP-MKT-01 'receipt-time age')",
                    "ticks": sub.ticks, "min_tick": sub.min_tick, "bbo_exchange": sub.bbo_exchange,
                    "snapshot_permissions": sub.snapshot_permissions, "warnings": list(sub.warnings),
                    "errors": list(sub.errors[-10:]), "created_ns": sub.created_ns,
                    "sampling_mode": "IBKR L1 aggregate snapshots, not every quote (CAP-MKT-01)"})
            requests = self.requests_sent
            return {
                "status_file_version": STATUS_FILE_VERSION, "written_at_ns": now, "provider": "ibkr",
                "connection": self.connection_state(), "line_budget": self.lines.to_json(),
                "pacing": {"max_per_second": self._pacer.max_per_second, "reserved": self._pacer.reserved,
                           "in_window": self._pacer.in_window(), "violations": self.pacing_violations},
                "requested_market_data_type": MARKET_DATA_TYPES[self._md.requested_market_data_type],
                "entitlement_errors": self.entitlement_errors, "requests_sent": self.requests_sent,
                "entitlement_error_rate": {
                    "value": f"{self.entitlement_errors}/{requests}" if requests else None,
                    "numerator": self.entitlement_errors, "denominator": requests,
                    "formula": "ibkr.entitlement_error_rate@1 = entitlement errors (354, 10089, 10090, 10186, 10197) "
                               "/ requests sent on this connection (CAP-IBKR-12)",
                    **({} if requests else {"missing_reason": "no requests sent on this connection yet"})},
                "publish_clock_regressions": self.publish_clock_regressions,
                "freshness_budgets_ms": dict(self._md.freshness_budgets_ms),
                "subscriptions": subs, "audit": list(self._audit), "anomalies": list(self._anomalies)[-50:],
                "storage": "no price values are written by the market-data session (CAP-IBKR-12)",
            }

    def write_status_file(self, path: Path | None = None) -> Path:
        path = Path(path or self._status_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.status_snapshot(), f, sort_keys=True)
        tmp.replace(path)
        return path

    def audit(self) -> list[dict]:
        with self.cv:
            return list(self._audit)

    def anomalies(self) -> list[dict]:
        with self.cv:
            return list(self._anomalies)

    def subscription(self, instrument_id: str) -> dict | None:
        """Status of the instrument's *current* subscription (not an earlier, cancelled one)."""
        with self.cv:
            req_id = self._by_instrument.get(instrument_id)
        for s in self.status_snapshot()["subscriptions"]:
            if s["req_id"] == req_id:
                return s
        return None

    # ------------------------------------------------------------------ helpers
    def _audit_entry(self, instrument_id: str | None, kind: str, detail: str) -> None:
        self._audit.append({"at_ns": self._clock.now_ns(), "instrument_id": instrument_id, "kind": kind,
                            "detail": detail})

    def _anomaly(self, kind: str, detail: str) -> None:
        self._anomalies.append({"at_ns": self._clock.now_ns(), "kind": kind, "detail": detail})
        log.warning("IBKR market data %s: %s", kind, detail)

    def _source(self, sub: _Subscription) -> str:
        if sub.unavailable is not None:
            return f"ibkr:unavailable:{sub.unavailable['state'].lower()}"
        return f"ibkr:{sub.type_name()}:{sub.consolidation()}"

    def _publish(self, sub: _Subscription, *, blank: bool = False) -> None:
        """Replace the instrument's quote in the QuoteBook (called under ``cv``).

        ``blank`` publishes an empty, not-live quote so the book never keeps showing a quote whose stream became
        unavailable, was cancelled or lost its connection."""
        stale = self._connection_stale()
        live = (not blank) and sub.is_live(stale)
        bid = None if blank else sub.value("bid")
        ask = None if blank else sub.value("ask")
        last = None if blank else sub.value("last")
        source = self._source(sub)
        if blank and sub.unavailable is None:
            why = stale or ("cancelled" if not sub.active else "resubscribe_required")
            source = "ibkr:stale:" + why.split(" ", 1)[0].lower()
        now = self._clock.now_ns()
        current = self._book.get(sub.instrument_id)
        receipt = now
        if current is not None and current.receipt_time_ns > now:
            # QuoteBook.put silently keeps the quote with the later receipt time. After a backwards wall-clock step
            # that would drop this publish (a blanking one included) and keep the previous, possibly live, quote.
            receipt = current.receipt_time_ns
            self.publish_clock_regressions += 1
            if self.publish_clock_regressions == 1 or blank:
                self._anomaly("CLOCK_STEPPED_BACK", f"{sub.instrument_id}: wall clock {now} is behind the stored "
                                                    f"quote {current.receipt_time_ns}; publish time held monotonic")
        # Core Quote labels (CAP-MKT-02): the delivered type ("unreported" for a blank or unavailable stream, which is
        # also marked delayed) and the owner-asserted consolidation of a live feed.
        kind = "live" if live else sub.type_name()
        if kind in ("live", "unreported") and not live:
            kind = "unreported"
        self._book.put(Quote(instrument_id=sub.instrument_id, bid=bid, ask=ask, last=last,
                             currency=sub.contract.currency, source=source, event_time_ns=None,
                             receipt_time_ns=receipt, delayed=not live, fixture=False,
                             consolidation=sub.consolidation() if live else "unknown", market_data_type=kind))

    def _invalidate_fields(self, sub: _Subscription, why: str) -> None:
        """Discard every value received before an interruption (called under ``cv``).

        IBKR streams only changes, so a value kept across an outage would be republished as current, with a fresh
        receipt time, as soon as any other field ticks. ``delayed_ticks_seen`` is kept: a type mismatch observed
        before the interruption still blocks 'live'."""
        if sub.live_fields or sub.delayed_fields:
            sub.live_fields.clear()
            sub.delayed_fields.clear()
            self._audit_entry(sub.instrument_id, "FIELDS_INVALIDATED", f"values before the interruption discarded "
                                                                       f"({why})")

    def _socket_lost(self, detail: str) -> None:
        if not self._socket_up:
            return
        self._socket_up = False
        self._verified = False
        self._reason = f"SOCKET_DOWN: {detail}"
        for sub in self._subs.values():
            if sub.active:
                sub.resubscribe_required = True     # IBKR drops subscriptions with the socket
                self._invalidate_fields(sub, f"socket down: {detail}")
                self._publish(sub, blank=True)
        self._audit_entry(None, "SOCKET_DOWN", detail)
        self.cv.notify_all()

    # ------------------------------------------------------------------ callbacks (message thread)
    def _dispatch(self, name: str, *args) -> None:
        try:
            getattr(self, f"_on_{name}")(*args)
        except Exception as exc:   # a handler failure must never kill the SDK message loop
            log.exception("IBKR market-data callback %s failed", name)
            with self.cv:
                self._anomaly("CALLBACK_FAILED", f"{name}: {type(exc).__name__}: {exc}")

    def _on_connectAck(self) -> None:
        pass

    def _on_connectionClosed(self) -> None:
        with self.cv:
            self._socket_lost("connectionClosed")

    def _on_nextValidId(self, order_id) -> None:
        with self.cv:
            self._next_valid_id = int(order_id)
            self.cv.notify_all()

    def _on_managedAccounts(self, accounts_list: str) -> None:
        accounts = tuple(a.strip() for a in (accounts_list or "").split(",") if a.strip())
        disconnect = None
        with self.cv:
            self._managed = accounts
            if self._verified and set(accounts) != {self._cfg.expected_account_id}:
                self._unsafe = "DISCONNECTED_UNSAFE: managed accounts changed during the session"
                self._anomaly("IDENTITY_MISMATCH", self._unsafe)
                disconnect = self._client
            self.cv.notify_all()
        if disconnect is not None:
            _safe(disconnect.disconnect)

    def _on_error(self, req_id, error_time, code, text, advanced) -> None:
        code, text = int(code), str(text)
        disconnect = None
        with self.cv:
            handler = self._handlers.get(req_id) if isinstance(req_id, int) else None
            sub = self._subs.get(req_id) if isinstance(req_id, int) else None
            if code == M.PACING_VIOLATION:
                self.pacing_violations += 1
                self._pacer.penalize(self._cfg.pacing_backoff_s)
                self._anomaly("PACING_VIOLATION", f"error 100 #{self.pacing_violations}: {text} (back-off "
                                                  f"{self._cfg.pacing_backoff_s}s)")
            elif code in M.UPSTREAM_DOWN:
                self._upstream_down = code
                for s in self._subs.values():
                    if s.active:
                        self._invalidate_fields(s, f"IBKR {code} upstream connectivity lost")
                        self._publish(s, blank=True)
                self._audit_entry(None, "UPSTREAM_DOWN", f"{code}: {text}")
            elif code == M.RESTORED_DATA_LOST:
                self._upstream_down = None
                for s in self._subs.values():
                    if s.active:
                        s.resubscribe_required = True
                self._audit_entry(None, "RESTORED_DATA_LOST", f"{code}: {text}; market data must be re-requested")
            elif code == M.RESTORED_DATA_KEPT:
                self._upstream_down = None
                self._audit_entry(None, "RESTORED_DATA_KEPT", f"{code}: {text}")
            elif code == DATA_FARM_BROKEN:
                self._farms_down.add(text.rsplit(":", 1)[-1].strip() if ":" in text else "unnamed")
                for s in self._subs.values():
                    if s.active:
                        # The farm-to-subscription mapping is not reported, so every subscription is treated as hit.
                        self._invalidate_fields(s, f"IBKR {code} market data farm down")
                        self._publish(s, blank=True)
                self._audit_entry(None, "DATA_FARM_DOWN", f"{code}: {text}")
            elif code == DATA_FARM_OK:
                farm = text.rsplit(":", 1)[-1].strip() if ":" in text else "unnamed"
                if farm in self._farms_down:
                    self._farms_down.discard(farm)
                    self._audit_entry(None, "DATA_FARM_OK", f"{code}: {text}")
            elif code == M.CLIENT_ID_IN_USE:
                self._unsafe = f"DISCONNECTED_UNSAFE: client id {self._md.client_id} already in use (326)"
                disconnect = self._client
            elif code == M.SOCKET_PORT_RESET:
                disconnect = self._client
            elif code == M.CONNECT_FAIL:
                self._reason = f"HOST_DOWN 502: {text}"
            if sub is not None and handler is None:
                self._subscription_error(sub, code, text)
            elif handler is not None:
                on_error = getattr(handler, "on_error", None)
                if on_error is not None:
                    on_error(code, text)
            self.cv.notify_all()
        if disconnect is not None:
            _safe(disconnect.disconnect)
            with self.cv:
                self._socket_lost(f"{code}: {text}")

    def _subscription_error(self, sub: _Subscription, code: int, text: str) -> None:
        now = self._clock.now_ns()
        sub.errors.append({"at_ns": now, "code": code, "text": text})
        if code in ENTITLEMENT_CODES:
            self.entitlement_errors += 1
            state, cause, terminal = ENTITLEMENT_ERRORS[code]
            if not terminal and code == 10090:
                sub.warnings.append(f"IBKR 10090: {cause}")
                self._audit_entry(sub.instrument_id, state, f"{code}: {text}")
                return
            sub.unavailable = {"code": code, "state": state, "cause": cause, "text": text, "at_ns": now}
            sub.cancel_required = terminal
            self._audit_entry(sub.instrument_id, state, f"{code}: {cause}")
            self._invalidate_fields(sub, f"IBKR {code} {state}")
            self._publish(sub, blank=True)
        elif code == MAX_TICKERS:
            sub.unavailable = {"code": code, "state": "LINE_LIMIT_REACHED",
                               "cause": "Max number of tickers reached across TWS and the API: raise "
                                        "tws_reserved_lines or free lines (CAP-IBKR-08)", "text": text, "at_ns": now}
            sub.active = False
            self.lines.release(sub.req_id)
            self._anomaly("LINE_LIMIT_101", f"{sub.instrument_id}: IBKR refused despite local budget "
                                            f"{self.lines.in_use}/{self.lines.capacity}")
            self._audit_entry(sub.instrument_id, "LINE_LIMIT_REACHED", text)
            self._invalidate_fields(sub, "IBKR 101")
            self._publish(sub, blank=True)
        elif code == NO_SECURITY_DEFINITION or code in REQUEST_ERRORS:
            state = "NO_SECURITY_DEFINITION" if code == NO_SECURITY_DEFINITION else "REQUEST_ERROR"
            sub.unavailable = {"code": code, "state": state, "cause": text, "text": text, "at_ns": now}
            sub.active = False
            self.lines.release(sub.req_id)
            self._audit_entry(sub.instrument_id, state, f"{code}: {text}")
            self._invalidate_fields(sub, f"IBKR {code} {state}")
            self._publish(sub, blank=True)
        elif code == CANT_FIND_TICKER:
            pass
        else:
            sub.warnings.append(f"IBKR {code}: {text}")

    def _sub_for_data(self, req_id) -> _Subscription | None:
        sub = self._subs.get(req_id)
        if sub is None or not sub.active:
            return None
        if sub.unavailable is not None and sub.unavailable["code"] in ENTITLEMENT_CODES \
                and not ENTITLEMENT_ERRORS[sub.unavailable["code"]][2]:
            # data resumed after a non-terminal entitlement state (e.g. 10197 competing session ended)
            self._audit_entry(sub.instrument_id, "DATA_RESUMED", f"after IBKR {sub.unavailable['code']}")
            sub.unavailable = None
        return sub

    def _on_marketDataType(self, req_id, market_data_type) -> None:
        with self.cv:
            sub = self._subs.get(req_id)
            if sub is None:
                return
            t = int(market_data_type)
            if t not in MARKET_DATA_TYPES:
                self._anomaly("UNKNOWN_MARKET_DATA_TYPE", f"{sub.instrument_id}: {market_data_type!r}")
                return
            if sub.delivered_type != t:
                previous = sub.type_name()
                if sub.delivered_type is None:
                    # First report: ticks that arrived before it are kept only if their id class matches the type.
                    (sub.live_fields if t in DELAYED_TYPES else sub.delayed_fields).clear()
                else:
                    # Values received under another type are never carried into this one (CAP-MKT-01: delayed
                    # values are never merged into a real-time series; frozen values never become live).
                    sub.live_fields.clear()
                    sub.delayed_fields.clear()
                sub.delayed_ticks_seen = bool(sub.delayed_fields)
                sub.delivered_type = t
                self._audit_entry(sub.instrument_id, "MARKET_DATA_TYPE",
                                  f"{previous} -> {MARKET_DATA_TYPES[t]} (reported by IBKR marketDataType)")
            self._publish(sub)

    def _store_field(self, sub: _Subscription, delayed: bool, name: str, fv: FieldValue) -> None:
        (sub.delayed_fields if delayed else sub.live_fields)[name] = fv
        if delayed and not sub.delayed_ticks_seen:
            sub.delayed_ticks_seen = True
            if sub.delivered_type in (1, 2):
                self._anomaly("TYPE_MISMATCH", f"{sub.instrument_id}: delayed tick ids while type "
                                               f"{MARKET_DATA_TYPES[sub.delivered_type]} was reported; not live")
        sub.ticks += 1
        sub.last_receipt_ns = fv.receipt_ns

    def _after_update(self, sub: _Subscription) -> None:
        state = classify_quote(sub.value("bid"), sub.value("ask"))
        if state != sub.last_state:
            sub.last_state = state
            sub.state_counts[state] = sub.state_counts.get(state, 0) + 1
        self._publish(sub)

    def _on_tickPrice(self, req_id, tick_type, price, attrib) -> None:
        now = self._clock.now_ns()
        tick_type = int(tick_type)
        delayed = tick_type in DELAYED_PRICE_TICKS
        name = (DELAYED_PRICE_TICKS if delayed else PRICE_TICKS).get(tick_type)
        with self.cv:
            sub = self._sub_for_data(req_id)
            if sub is None or name is None:
                return
            value = M.float_to_fixed(price) if isinstance(price, (float, int)) and not isinstance(price, bool) \
                else None
            flags = []
            if attrib is not None:
                flags = [f for f in ("canAutoExecute", "pastLimit", "preOpen") if getattr(attrib, f, False)]
            raw = f"{price!r}" + (f" [{','.join(flags)}]" if flags else "")
            reason = None
            if value is None:
                reason = "unset price from provider"
            elif not value.is_positive():
                # CAP-MKT-01: -1 or 0 means no data; confirmed by the paired size-0 tick when one follows.
                reason = (f"provider sent {price!r}; awaiting paired size" if name in PAIRED_SIZE
                          else NO_DATA_REASON)
                value = None
            self._store_field(sub, delayed, name, FieldValue(value, raw, now, reason))
            self._after_update(sub)

    def _on_tickSize(self, req_id, tick_type, size) -> None:
        now = self._clock.now_ns()
        tick_type = int(tick_type)
        delayed = tick_type in DELAYED_SIZE_TICKS
        name = (DELAYED_SIZE_TICKS if delayed else SIZE_TICKS).get(tick_type)
        with self.cv:
            sub = self._sub_for_data(req_id)
            if sub is None or name is None:
                return
            try:
                value = M.decimal_to_fixed(size)
            except (TypeError, ValueError):
                value = None
            reason = None if value is not None else "unset size from provider"
            self._store_field(sub, delayed, name, FieldValue(value, str(size), now, reason))
            price_name = next((p for p, s in PAIRED_SIZE.items() if s == name), None)
            fields = sub.delayed_fields if delayed else sub.live_fields
            if price_name and value is not None and value.is_zero():
                pv = fields.get(price_name)
                if pv is not None and pv.value is None:
                    pv.null_reason = NO_DATA_REASON
            self._after_update(sub)

    def _on_tickString(self, req_id, tick_type, value) -> None:
        now = self._clock.now_ns()
        tick_type = int(tick_type)
        delayed = tick_type in DELAYED_STRING_TICKS
        name = (DELAYED_STRING_TICKS if delayed else STRING_TICKS).get(tick_type)
        with self.cv:
            sub = self._sub_for_data(req_id)
            if sub is None or name is None:
                return
            # LAST_TIMESTAMP is the last trade's time; it is not the age basis of bid/ask, so it is kept as metadata.
            self._store_field(sub, delayed, name, FieldValue(None, str(value), now, "metadata (last trade time)"))
            self._after_update(sub)

    def _on_tickGeneric(self, req_id, tick_type, value) -> None:
        tick_type = int(tick_type)
        with self.cv:
            sub = self._sub_for_data(req_id)
            name = GENERIC_TICKS.get(tick_type) or DELAYED_GENERIC_TICKS.get(tick_type)
            if sub is None or name is None:
                return
            # The HALTED tick's value meanings are not on the fetched pages: kept raw, not interpreted.
            sub.warnings = [w for w in sub.warnings if not w.startswith("halted tick")]
            if value not in (0, 0.0):
                sub.warnings.append(f"halted tick {tick_type} value {value!r} (raw; meaning not verified)")
            self._audit_entry(sub.instrument_id, "HALTED_TICK", f"{tick_type}={value!r}")

    def _on_tickReqParams(self, ticker_id, min_tick, bbo_exchange, snapshot_permissions) -> None:
        with self.cv:
            sub = self._subs.get(ticker_id)
            if sub is None:
                return
            fx = M.float_to_fixed(min_tick) if isinstance(min_tick, float) else None
            sub.min_tick = fx.to_str() if fx is not None else None
            sub.bbo_exchange = bbo_exchange or None
            sub.snapshot_permissions = snapshot_permissions

    def _on_tickSnapshotEnd(self, req_id) -> None:
        pass   # snapshots are never requested by this module

    # forwarded to history / contracts handlers registered by request id
    def _on_historicalData(self, req_id, bar) -> None:
        with self.cv:
            handler = self._handlers.get(req_id)
            if handler is not None:
                handler.on_historical_data(bar)
            self.cv.notify_all()

    def _on_historicalDataEnd(self, req_id, start, end) -> None:
        with self.cv:
            handler = self._handlers.get(req_id)
            if handler is not None:
                handler.on_historical_end(start, end)
            self.cv.notify_all()

    def _on_contractDetails(self, req_id, details) -> None:
        with self.cv:
            handler = self._handlers.get(req_id)
            if handler is not None:
                handler.on_contract_details(details)
            self.cv.notify_all()

    def _on_contractDetailsEnd(self, req_id) -> None:
        with self.cv:
            handler = self._handlers.get(req_id)
            if handler is not None:
                handler.on_contract_details_end()
            self.cv.notify_all()

    def _on_marketRule(self, market_rule_id, price_increments) -> None:
        with self.cv:
            for fn in list(self._rule_listeners):
                fn(int(market_rule_id), list(price_increments or ()))
            self.cv.notify_all()


def default_status_path(home: Path | None = None) -> Path:
    from ...service.paths import Paths
    paths = Paths(home) if home else Paths.default()
    return paths.run / STATUS_FILE_NAME
