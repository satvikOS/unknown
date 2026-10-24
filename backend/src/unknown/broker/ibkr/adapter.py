"""IBKR TWS API paper adapter implementing ``BrokerPort`` (BRK-02; ADR-0011, ADR-0012).

Every behaviour cites the verified research record ``capreg/ibkr-integration.json``
(records R1..R14, IBKR documentation fetched 2026-10-22) and uses only methods and
callbacks present in the pinned official SDK (TWS API 10.51.01, ``ibapi``).

Licence boundary: ``ibapi`` is GPL-3.0-or-later and is never copied or vendored.
It is imported lazily from the owner's local installation inside the adapter
constructor, so importing this module never imports ``ibapi`` and the core never
depends on it.

Threads: ``EClient.connect`` starts the SDK's EReader thread; this adapter runs
``EClient.run`` (the message loop that invokes EWrapper callbacks) on its own
daemon thread. Callbacks update state under one condition variable and append
observations to a FIFO deque; ``drain_events`` pops it. Port methods are
synchronous with bounded waits and never retry anything at the broker.

Nothing here has been run against a real IBKR host. Status: implemented and
tested offline against recorded-shape fixtures; NOT paper-verified (ADR-0001).
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ...domain.clock import Clock, SystemClock
from ...domain.fixed import Fixed
from ..port import (
    AccountMode,
    AccountSnapshot,
    BrokerCapabilities,
    BrokerEvent,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerSessionState,
    CashBalance,
    CommissionEvent,
    CommissionRow,
    ConnectivityEvent,
    ExecutionBatch,
    ExecutionEvent,
    ExecutionRow,
    LookupResult,
    OpenOrderRow,
    OrderStatusEvent,
    PacingLimit,
    PositionRow,
    SubmitResult,
    SubmitStatus,
)
from . import mapping as M
from .config import IbkrConfig
from .ids import Allocation, OrderIdStore
from .pacing import PacingScheduler

log = logging.getLogger(__name__)

PROVIDER = "ibkr"
EVIDENCE_LEVEL = "BROKER_PAPER"            # R13: every paper result is tagged with its evidence level
NOT_TRANSMITTED = "NOT_TRANSMITTED"        # reason prefix: placeOrder/cancelOrder was never called
REQUEST_ID_BASE = 1_900_000_000            # request ids kept far from order ids so error(reqId) is unambiguous
SUMMARY_TAGS = ("AccountType", "NetLiquidation", "TotalCashValue", "SettledCash", "AvailableFunds")
MAX_SUMMARY_SUBSCRIPTIONS = 2              # R6: "only two active summary subscriptions are allowed at a time"
CASH_SEMANTICS = ("IBKR accountSummary: settled = SettledCash ('cash recognized at the time of settlement - purchases "
                  "at the time of trade - commissions - taxes - fees'); total = TotalCashValue ('total cash balance "
                  "recognized at the time of trade + futures PNL'); refreshed on IBKR's fixed ~3-minute cadence (R6). "
                  "AvailableFunds/NetLiquidation are not cash and appear only in raw.")
PACING_SOURCE = "https://www.interactivebrokers.com/docs/tws-api/doc/pacing-limitations/introduction.md"
INSTALL_HELP = ("Install the official TWS API from https://download2.interactivebrokers.com/installers/tws-api/ "
                "(Latest 1051.01 includes the Python client), then `pip install protobuf==5.29.5` and "
                "`pip install <IBJts>/source/pythonclient` into the interpreter that runs the core. "
                "See docs/integrations/ibkr.md.")


SERVER_TIME_REFRESH_NS = 30 * 1_000_000_000    # re-ask the host clock when the last answer is older
SERVER_TIME_MAX_AGE_NS = 120 * 1_000_000_000   # beyond this a measurement is not evidence of current skew
SERVER_TIME_RETRY_NS = 10 * 1_000_000_000      # an unanswered clock query is re-sent after this
SERVER_TIME_MAX_RTT_NS = 250_000_000           # a sample with a longer round trip bounds skew too loosely
# ibapi/server_versions.py (TWS API 10.51.01): reqCurrentTimeInMillis needs server version >= 197.
MIN_SERVER_VER_CURRENT_TIME_IN_MILLIS = 197


class IbkrSdkUnavailable(ImportError):
    """``ibapi`` (the official TWS API Python client) is not importable."""


class IbkrSdkMismatch(RuntimeError):
    """The imported ``ibapi`` is not the pinned official release (R1)."""


class IbkrNotReady(ConnectionError):
    """No verified PAPER session; a ConnectionError so the reconciler records an INCOMPLETE run."""


# ---------------------------------------------------------------------------- SDK loading
@dataclass(frozen=True)
class Sdk:
    EClient: type
    EWrapper: type
    Contract: type
    Order: type
    OrderCancel: type
    ExecutionFilter: type
    version: str
    path: str
    fingerprint: str


def sdk_fingerprint(package_dir: Path) -> str:
    """sha256 over the imported package's .py files (relative path + bytes), for the R1 pin."""
    h = hashlib.sha256()
    for p in sorted(Path(package_dir).rglob("*.py")):
        h.update(p.relative_to(package_dir).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def load_sdk(config: IbkrConfig | None = None) -> Sdk:
    """Import ``ibapi`` from the owner's installation and check it against the R1 pin."""
    try:
        ibapi = importlib.import_module("ibapi")
        client = importlib.import_module("ibapi.client")
        wrapper = importlib.import_module("ibapi.wrapper")
        contract = importlib.import_module("ibapi.contract")
        order = importlib.import_module("ibapi.order")
        order_cancel = importlib.import_module("ibapi.order_cancel")
        execution = importlib.import_module("ibapi.execution")
    except ImportError as exc:
        raise IbkrSdkUnavailable(f"ibapi is not importable ({exc}). {INSTALL_HELP}") from exc
    version = str(getattr(ibapi, "__version__", ""))
    major = version.split(".")[0]
    # R1: PyPI 'ibapi' is 9.81.x (2020) and not an IBKR-supported channel; the adapter must reject it.
    if not major.isdigit() or int(major) < 1000:
        raise IbkrSdkMismatch(f"ibapi {version!r} is not an official 10.x release (PyPI 'ibapi' is refused, R1). "
                              f"{INSTALL_HELP}")
    package_dir = Path(ibapi.__file__).resolve().parent
    fingerprint = sdk_fingerprint(package_dir)
    if config is not None and config.sdk_version_pin and version != config.sdk_version_pin:
        raise IbkrSdkMismatch(f"ibapi {version} differs from the pinned version {config.sdk_version_pin} (R1)")
    if config is not None and config.sdk_sha256_pin and fingerprint != config.sdk_sha256_pin:
        raise IbkrSdkMismatch(f"ibapi at {package_dir} has fingerprint {fingerprint}, not the pinned "
                              f"{config.sdk_sha256_pin} (R1)")
    return Sdk(EClient=client.EClient, EWrapper=wrapper.EWrapper, Contract=contract.Contract, Order=order.Order,
               OrderCancel=order_cancel.OrderCancel, ExecutionFilter=execution.ExecutionFilter, version=version,
               path=str(package_dir), fingerprint=fingerprint)


def _wrapper_class(base: type) -> type:
    """EWrapper subclass forwarding the callbacks this adapter uses.

    Names and parameter lists match ibapi/wrapper.py in TWS API 10.51.01 (see the
    signature-conformance test)."""

    class AdapterWrapper(base):
        def __init__(self, adapter: "IbkrAdapter"):
            base.__init__(self)
            self._adapter = adapter

        def connectAck(self):
            self._adapter._dispatch("connectAck")

        def nextValidId(self, orderId: int):
            self._adapter._dispatch("nextValidId", orderId)

        def managedAccounts(self, accountsList: str):
            self._adapter._dispatch("managedAccounts", accountsList)

        def error(self, reqId, errorTime: int, errorCode: int, errorString: str, advancedOrderRejectJson=""):
            self._adapter._dispatch("error", reqId, errorTime, errorCode, errorString, advancedOrderRejectJson)

        def connectionClosed(self):
            self._adapter._dispatch("connectionClosed")

        def orderStatus(self, orderId, status: str, filled, remaining, avgFillPrice: float, permId: int,
                        parentId: int, lastFillPrice: float, clientId: int, whyHeld: str, mktCapPrice: float):
            self._adapter._dispatch("orderStatus", orderId, status, filled, remaining, avgFillPrice, permId,
                                    parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)

        def openOrder(self, orderId, contract, order, orderState):
            self._adapter._dispatch("openOrder", orderId, contract, order, orderState)

        def openOrderEnd(self):
            self._adapter._dispatch("openOrderEnd")

        def completedOrder(self, contract, order, orderState):
            self._adapter._dispatch("completedOrder", contract, order, orderState)

        def completedOrdersEnd(self):
            self._adapter._dispatch("completedOrdersEnd")

        def execDetails(self, reqId: int, contract, execution):
            self._adapter._dispatch("execDetails", reqId, contract, execution)

        def execDetailsEnd(self, reqId: int):
            self._adapter._dispatch("execDetailsEnd", reqId)

        def commissionAndFeesReport(self, commissionAndFeesReport):
            self._adapter._dispatch("commissionAndFeesReport", commissionAndFeesReport)

        def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
            self._adapter._dispatch("accountSummary", reqId, account, tag, value, currency)

        def accountSummaryEnd(self, reqId: int):
            self._adapter._dispatch("accountSummaryEnd", reqId)

        def position(self, account: str, contract, position, avgCost: float):
            self._adapter._dispatch("position", account, contract, position, avgCost)

        def positionEnd(self):
            self._adapter._dispatch("positionEnd")

        def orderBound(self, permId: int, clientId: int, orderId: int):
            self._adapter._dispatch("orderBound", permId, clientId, orderId)

        def currentTimeInMillis(self, timeInMillis: int):
            self._adapter._dispatch("currentTimeInMillis", timeInMillis)

    return AdapterWrapper


# ---------------------------------------------------------------------------- internal state
@dataclass
class _Live:
    """Latest host-reported facts about one order (ours or foreign)."""
    client_id: int
    order_id: int
    perm_id: int = 0
    status: str | None = None              # latest orderStatus status
    open_status: str | None = None         # latest openOrder orderState.status
    latest: str | None = None              # most recent status from either callback
    filled: Fixed | None = None
    remaining: Fixed | None = None
    why_held: str = ""
    order_ref: str | None = None
    con_id: int = 0
    side: str | None = None
    total_qty: Fixed | None = None
    warning: str = ""
    reject_reason: str = ""
    errors: list = field(default_factory=list)   # (seq, code, text)
    updates: int = 0                             # raw status/openOrder callbacks (before de-duplication)
    last_key: tuple | None = None
    cancel_requested: bool = False

    def codes_since(self, seq: int) -> list[tuple[int, int, str]]:
        return [e for e in self.errors if e[0] > seq]

    def rejection_seen(self) -> bool:
        codes = {e[1] for e in self.errors}
        return M.ORDER_REJECTED in codes or (M.ORDER_CANCELLED_BY_SYSTEM in codes and not self.cancel_requested)


@dataclass
class _ExecRequest:
    req_id: int
    end: bool = False
    error: str | None = None


@dataclass
class _SummaryRequest:
    req_id: int
    rows: list = field(default_factory=list)
    end: bool = False
    error: str | None = None


@dataclass(frozen=True)
class _Watermark:
    instance: str
    executions: int
    commissions: int
    issue_day: date


def _make_watermark(instance: str, executions: int, commissions: int, day: date) -> str:
    return f"ibkr1:{instance}:{executions}:{commissions}:{day:%Y%m%d}"


def _parse_watermark(text: str | None) -> tuple[_Watermark | None, bool]:
    """Returns (watermark, valid). ``None`` input is valid and means "from the beginning"."""
    if text is None:
        return None, True
    parts = str(text).split(":")
    if len(parts) != 5 or parts[0] != "ibkr1":
        return None, False
    try:
        return _Watermark(parts[1], int(parts[2]), int(parts[3]), datetime.strptime(parts[4], "%Y%m%d").date()), True
    except ValueError:
        return None, False


_EXCLUDED = object()   # an open-order callback for an order that is no longer open (Filled/Cancelled)


def _safe_call(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------- adapter
class IbkrAdapter:
    """``BrokerPort`` over TWS / IB Gateway for one explicitly identified PAPER account."""

    def __init__(self, config: IbkrConfig, *, instrument_resolver: Callable[[int], str | None],
                 clock: Clock | None = None, client_factory: Callable[[object], object] | None = None,
                 sdk: Sdk | None = None, pacer: PacingScheduler | None = None,
                 contract_fields: Callable[[BrokerOrder], dict] | None = None):
        config.validate()   # raises for LIVE or any non-PAPER profile before anything else happens
        self._cfg = config
        self._sdk = sdk or load_sdk(config)
        self._clock = clock or SystemClock()
        self._resolver = instrument_resolver
        self._contract_fields = contract_fields
        self._client_factory = client_factory or (lambda wrapper: self._sdk.EClient(wrapper))
        self._wrapper = _wrapper_class(self._sdk.EWrapper)(self)
        self._pacer = pacer or PacingScheduler(config.max_requests_per_second, config.reserved_pacing_percent)
        self._store = OrderIdStore(config.state_dir, account_id=config.expected_account_id,
                                   client_id=config.client_id, now_ns=self._clock.now_ns,
                                   retention_days=config.allocation_retention_days)
        self._tz = ZoneInfo(config.host_time_zone)
        self._instance = uuid.uuid4().hex[:12]
        self._cv = threading.Condition(threading.RLock())   # re-entrant: helpers may nest
        self._req_lock = threading.RLock()       # serialises request/End flows so End callbacks are unambiguous
        self._client = None
        self._socket_up = False
        self._next_valid_id: int | None = None
        self._nvid_seq = 0
        self._managed: tuple[str, ...] | None = None
        self._mode_verified = False
        self._unsafe: str | None = None
        self._auth_reason: str | None = None
        self._last_connect_error: str | None = None
        self._upstream_down: int | None = None
        self._login_failed: str | None = None    # R9: login-failure message seen; cleared by restore or reconnect
        self._server_version = None
        self._connection_time = None
        self._last_message_ns: int | None = None
        self._entry_blocks: dict[str, str] = {}
        self._pacing_violations = 0
        self._events: deque[BrokerEvent] = deque()
        self._anomalies: deque[dict] = deque(maxlen=2000)
        self._seq = 0
        self._req_seq = REQUEST_ID_BASE
        self._live: dict[int, _Live] = {}               # our client's orders by orderId
        self._foreign: dict[tuple, _Live] = {}          # other clients' / manual orders
        self._exec_log: list[ExecutionRow] = []
        self._exec_by_id: dict[str, ExecutionRow] = {}
        self._revisions: dict[tuple[str, str], dict[int, str]] = {}
        self._unrepresentable: dict[str, dict] = {}
        self._comm_log: list[CommissionRow] = []
        self._comm_seen: set[tuple[str, str, str]] = set()
        self._pending_comm: dict[str, list[dict]] = {}
        self._exec_reqs: dict[int, _ExecRequest] = {}
        self._summary_reqs: dict[int, _SummaryRequest] = {}
        self._active_summary: set[int] = set()
        self._positions_collect: list | None = None
        self._positions_end = False
        self._oo_collect: dict | None = None
        self._oo_ends = 0
        self._co_collect: list | None = None
        self._co_end = False
        self._coverage_start_day: date | None = None
        self._down_since_day: date | None = None
        self._server_time: tuple[int, int] | None = None   # (host ns, local monotonic ns at the round-trip midpoint)
        self._time_req_sent: int | None = None              # monotonic ns of the clock query in flight
        self._server_time_unsupported = False
        self._late_handshake = False                        # connect() timed out before nextValidId/managedAccounts

    # ======================================================================= lifecycle
    def connect(self) -> BrokerSessionState:
        """Open the socket, wait for nextValidId AND managedAccounts, then verify the PAPER identity (R2).

        Never automates login: if the host is not logged in, nextValidId does not
        arrive and the session reports AUTH_REQUIRED (R9)."""
        cfg = self._cfg
        with self._cv:
            if self._socket_up and not self._unsafe and not _safe_call(self._client.isConnected, False):
                # The socket died without connectionClosed reaching us (session() already reports it as not
                # connected): record SOCKET_DOWN so this reconnect is not refused as "already connected".
                self._socket_down("socket found closed at reconnect")
            if self._unsafe or self._socket_up:
                return self._session_locked()
            self._next_valid_id = None
            self._managed = None
            self._mode_verified = False
            self._late_handshake = False
            self._auth_reason = None
            self._last_connect_error = None
            self._upstream_down = None
            self._login_failed = None
        client = self._client_factory(self._wrapper)
        with self._cv:
            self._client = client
        # EClient.connect blocks in the version handshake and loops while the host sends nothing (for example while
        # TWS shows its "accept incoming API connection" dialog), so it runs on a helper thread with a deadline.
        def _connect() -> None:
            try:
                client.connect(cfg.host, cfg.port, cfg.client_id)
            except Exception as exc:
                with self._cv:
                    self._last_connect_error = f"connect raised {type(exc).__name__}: {exc}"

        handshake = threading.Thread(target=_connect, name="ibkr-connect", daemon=True)
        handshake.start()
        handshake.join(cfg.connect_timeout_s)
        if handshake.is_alive():
            _safe_call(client.disconnect)
            handshake.join(2.0)
            with self._cv:
                self._last_connect_error = (f"HANDSHAKE_TIMEOUT: the host did not complete the API handshake within "
                                            f"{cfg.connect_timeout_s}s (accept the API connection in TWS/IB Gateway, "
                                            "check 'Trusted IPs' and the socket port)")
        if not _safe_call(client.isConnected, False):
            with self._cv:
                reason = self._last_connect_error or ("HOST_DOWN: could not open the socket (502: is TWS/IB Gateway "
                                                      "running with 'Enable ActiveX and Socket Clients' on this port?)")
                self._auth_reason = reason
                self._emit(ConnectivityEvent(False, False, reason, self._clock.now_ns()))
                return self._session_locked()
        with self._cv:
            self._socket_up = True
            self._server_version = _safe_call(client.serverVersion)
            self._connection_time = _safe_call(client.twsConnectionTime)
        threading.Thread(target=self._run_loop, args=(client,), name="ibkr-msg", daemon=True).start()
        with self._cv:
            got = self._cv.wait_for(lambda: (self._next_valid_id is not None and self._managed is not None)
                                    or not self._socket_up or self._unsafe is not None, timeout=cfg.connect_timeout_s)
            if self._unsafe is not None or not self._socket_up:
                return self._session_locked()
            if not got:
                self._auth_reason = (f"{M.AUTH_REQUIRED_REASON} (no nextValidId/managedAccounts within "
                                     f"{cfg.connect_timeout_s}s)")
                # If they arrive later on this socket, verification completes then (no reconnect needed).
                self._late_handshake = True
                self._emit(ConnectivityEvent(True, False, self._auth_reason, self._clock.now_ns()))
                return self._session_locked()
        return self._verify_identity(client)

    def _verify_identity(self, client) -> BrokerSessionState:
        """R2 identity check once nextValidId and managedAccounts are both known; READY or latched unsafe."""
        with self._cv:
            problems = self._identity_problems(client)
            if problems:
                self._unsafe = "DISCONNECTED_UNSAFE: " + "; ".join(problems)
                self._anomaly("IDENTITY_MISMATCH", self._unsafe)
                self._emit(ConnectivityEvent(False, False, self._unsafe, self._clock.now_ns()))
        if problems:
            # R2 degraded behaviour: disconnect, latch, alert; nothing is subscribed.
            _safe_call(client.disconnect)
            with self._cv:
                self._socket_up = False
                return self._session_locked()
        with self._cv:
            self._mode_verified = True
            self._auth_reason = None
            today = self._host_day()
            if self._coverage_start_day is None or (self._down_since_day is not None and today > self._down_since_day):
                self._coverage_start_day = today
            self._down_since_day = None
            if self._next_valid_id is not None and self._store.max_order_id >= self._next_valid_id:
                self._anomaly("ID_REGRESSION", f"nextValidId {self._next_valid_id} <= persisted max order id "
                                               f"{self._store.max_order_id}; allocations use persisted max + 1 (R3)")
            self._emit(ConnectivityEvent(True, True, f"READY: PAPER identity verified ({self._describe()})",
                                         self._clock.now_ns()))
            self._cv.notify_all()
        self._request_server_time(client)   # answered asynchronously; preflight fails closed until it arrives
        with self._cv:
            return self._session_locked()

    def disconnect(self) -> None:
        with self._cv:
            client = self._client
        if client is not None:
            _safe_call(client.disconnect)
        with self._cv:
            self._socket_down("disconnect requested by the adapter owner")

    def _identity_problems(self, client) -> list[str]:
        cfg = self._cfg
        problems = []
        if cfg.mode != "PAPER":
            problems.append(f"mode {cfg.mode} is not PAPER")
        connected_port = getattr(client, "port", cfg.port)
        if connected_port != cfg.profile_port or cfg.port != cfg.profile_port:
            problems.append(f"port {connected_port} does not match the PAPER profile port {cfg.profile_port}")
        managed = set(self._managed or ())
        if managed != {cfg.expected_account_id}:
            masked = sorted(self._mask(a) for a in managed)
            problems.append(f"managed accounts {masked} != expected {{{cfg.masked_account()}}}")
        return problems

    def _mask(self, account: str) -> str:
        return account[:2] + "*" * max(0, len(account) - 4) + account[-2:] if len(account) > 4 else "*" * len(account)

    def _run_loop(self, client) -> None:
        try:
            client.run()
        except Exception as exc:   # the SDK loop only catches KeyboardInterrupt/SystemExit/BadMessage
            log.exception("IBKR message loop failed")
            with self._cv:
                self._anomaly("MESSAGE_LOOP_FAILED", f"{type(exc).__name__}: {exc}")
        finally:
            if not _safe_call(client.isConnected, False):
                with self._cv:
                    if self._client is client:
                        self._socket_down("message loop ended (EReader/socket closed)")

    # ======================================================================= port: info
    def capabilities(self) -> BrokerCapabilities:
        with self._cv:
            reason = self._ready_reason()
            if reason:
                # account_mode is PAPER only after verification; there is no UNVERIFIED value in the port.
                raise IbkrNotReady(f"IBKR capabilities are not established: {reason}")
            server_version = self._server_version
        cfg = self._cfg
        return BrokerCapabilities(
            provider=PROVIDER,
            account_mode=AccountMode.PAPER,
            base_currency=cfg.base_currency,
            legal_entity=cfg.legal_entity,
            currencies=tuple(cfg.currencies),
            markets=tuple(cfg.markets),
            order_types=("LIMIT",),
            time_in_force=("DAY",),
            outside_regular_hours=False,
            fractional_quantities=False,
            short_selling=False,
            margin=False,
            atomic_replace=False,
            # R3: orderRef is "Intended for institutional customers only" (Order reference) yet "user-customizable"
            # (Execution object); no length limit is documented. It is sent as a tag but never used for identity,
            # so the conservative capability is 0 until paper evidence shows it is echoed and how long it may be.
            order_reference_max_length=0,
            execution_history_days=cfg.execution_history_days,
            execution_corrections=("IBKR re-sends execDetails with an execId differing only in the digits after the "
                                   "final period ('.02' corrects '.01'); the correction replaces the earlier record "
                                   "and is not additive (R5). correction_of names the latest prior revision seen, or "
                                   "the presumed previous revision when the earlier one was never observed."),
            settlement_evidence="accountSummary SettledCash (≈3 min cadence)",
            pacing=(PacingLimit("tws-api-requests", cfg.max_requests_per_second, 1, PACING_SOURCE),),
            extensions={
                "ibkr.mode_verified": True,
                "ibkr.evidence_level": EVIDENCE_LEVEL,
                "ibkr.host_type": cfg.host_type,
                "ibkr.client_id": cfg.client_id,
                "ibkr.account": cfg.masked_account(),
                "ibkr.server_version": server_version,
                "ibkr.sdk_version": self._sdk.version,
                "ibkr.sdk_fingerprint": self._sdk.fingerprint,
                "ibkr.pacing_reserved_requests_per_second": self._pacer.reserved,
                "ibkr.pacing_reserved_for": ["cancel", "reconcile"],
                "ibkr.account_summary_max_subscriptions": MAX_SUMMARY_SUBSCRIPTIONS,
                "ibkr.order_ref": "sent on every order as '<deployment>:<epoch>:<client order id>'; not identity",
                "ibkr.paper_unsupported_order_types": ["VWAP", "Auction", "RFQ", "Pegged to Market"],
                "ibkr.capability_status": "implemented, tested offline; NOT paper-verified",
            },
        )

    def session(self) -> BrokerSessionState:
        with self._cv:
            state = self._session_locked()
            mono = self._clock.monotonic_ns()
            stale = self._server_time is None or mono - self._server_time[1] > SERVER_TIME_REFRESH_NS
            client = self._client if (stale and self._socket_up and self._mode_verified) else None
        if client is not None:
            self._request_server_time(client)   # never blocks; the next call sees the fresh measurement
        return state

    def _request_server_time(self, client) -> None:
        """Ask the host for its clock (reqCurrentTimeInMillis). Never blocks the caller (the core loop): it is
        skipped when pacing has no room, when a query is already in flight, or when the host is too old."""
        with self._cv:
            if self._server_time_unsupported:
                return
            version = self._server_version
            if version is not None and int(version) < MIN_SERVER_VER_CURRENT_TIME_IN_MILLIS:
                self._server_time_unsupported = True
                self._anomaly("SERVER_TIME_UNSUPPORTED", f"server version {version} < "
                              f"{MIN_SERVER_VER_CURRENT_TIME_IN_MILLIS}; clock skew stays unknown (preflight fails closed)")
                return
            mono = self._clock.monotonic_ns()
            if self._time_req_sent is not None and mono - self._time_req_sent < SERVER_TIME_RETRY_NS:
                return
            if not self._pacer.try_acquire("query"):
                return
            self._time_req_sent = mono
        try:
            client.reqCurrentTimeInMillis()
        except Exception as exc:
            with self._cv:
                self._anomaly("SERVER_TIME_UNAVAILABLE", f"{type(exc).__name__}: {exc}")

    def _session_locked(self) -> BrokerSessionState:
        client = self._client
        alive = self._socket_up and client is not None and bool(_safe_call(client.isConnected, False))
        authenticated = (alive and self._mode_verified and self._next_valid_id is not None
                         and self._upstream_down is None and self._unsafe is None and self._login_failed is None)
        reason = None
        if not authenticated:
            if self._unsafe:
                reason = self._unsafe
            elif self._login_failed:
                reason = self._login_failed
            elif self._upstream_down is not None and alive:
                reason = f"UPSTREAM_DOWN: IBKR {self._upstream_down} (host lost connectivity to IBKR)"
            elif self._auth_reason:
                reason = self._auth_reason
            elif not alive:
                reason = "NOT_CONNECTED"
            else:
                reason = "MODE_UNVERIFIED"
        server_time_ns = None
        if self._server_time is not None and alive:
            host_ns, at_mono = self._server_time
            age = self._clock.monotonic_ns() - at_mono
            if 0 <= age <= SERVER_TIME_MAX_AGE_NS:
                # The host clock carried forward on the monotonic clock: preflight compares it with the current
                # wall clock, so a local clock step after the measurement shows up as skew.
                server_time_ns = host_ns + age
        return BrokerSessionState(connected=alive, authenticated=authenticated, auth_required_reason=reason,
                                  server_time_ns=server_time_ns, last_heartbeat_ns=self._last_message_ns,
                                  detail=self._describe())

    def _describe(self) -> str:
        cfg = self._cfg
        blocks = ",".join(sorted(self._entry_blocks)) or "none"
        return (f"ibkr {cfg.host_type} {cfg.host}:{cfg.port} client {cfg.client_id} account {cfg.masked_account()} "
                f"server_version {self._server_version} mode {'PAPER verified' if self._mode_verified else 'unverified'}"
                f" entry_blocks {blocks}")

    def anomalies(self) -> list[dict]:
        """Observations the port cannot carry (unknown statuses, unrepresentable executions, gaps...)."""
        with self._cv:
            return list(self._anomalies)

    def entry_blocks(self) -> dict[str, str]:
        with self._cv:
            return dict(self._entry_blocks)

    def clear_entry_block(self, name: str, *, by: str, note: str) -> bool:
        """Owner acknowledgement of a latched entry block (e.g. SHORT_SALE_SIGNAL, HOST_OUT_OF_DATE)."""
        with self._cv:
            removed = self._entry_blocks.pop(name, None)
            if removed is not None:
                self._anomaly("ENTRY_BLOCK_CLEARED", f"{name} cleared by {by}: {note}")
            return removed is not None

    def acknowledge_history_gap(self, through_day: date, *, by: str, note: str) -> None:
        """Owner attests that executions up to ``through_day`` were reconciled from statements/Flex (R5)."""
        if through_day >= self._host_day():
            raise ValueError(f"{through_day} is not a completed day on the host calendar ({self._cfg.host_time_zone})")
        self._store.acknowledge_history_gap(through_day.strftime("%Y%m%d"), by, note)

    # ======================================================================= port: orders
    def allocate_broker_order_id(self, client_order_id: str) -> str:
        """Persist and return the IBKR orderId for ``client_order_id`` (R3).

        ``max(nextValidId, persisted max + 1)``, fsync'd before returning, so a
        crash after placeOrder never reuses an id. Idempotent per client order id."""
        with self._cv:
            reason = self._ready_reason()
            if reason or self._next_valid_id is None:
                raise IbkrNotReady(f"cannot allocate an IBKR order id: {reason or 'nextValidId not received'}")
            nvid = self._next_valid_id
        allocation, anomaly = self._store.allocate(client_order_id, nvid)
        if anomaly:
            with self._cv:
                self._anomaly("ID_REGRESSION", anomaly)
        return str(allocation.order_id)

    def submit(self, order: BrokerOrder) -> SubmitResult:
        problem = self._validate_order(order)
        if problem:
            return SubmitResult(SubmitStatus.REJECTED, reason=f"{NOT_TRANSMITTED}: {problem}")
        if "DUPLICATE_ORDER_ID" in self.entry_blocks():
            self._resync_order_ids()
        with self._cv:
            block = self._entry_block_reason()
            nvid = self._next_valid_id
            client = self._client
        if block:
            return SubmitResult(SubmitStatus.REJECTED, reason=f"{NOT_TRANSMITTED}: {block}")
        existing = self._store.get(order.client_order_id)
        if existing is not None and existing.transmitted:
            # Re-sending the same orderId would MODIFY the live order; a fresh id could duplicate it. Never retry.
            return SubmitResult(SubmitStatus.UNKNOWN, broker_order_id=str(existing.order_id),
                                reason=f"DUPLICATE_SUBMIT: {order.client_order_id} was already transmitted as orderId "
                                       f"{existing.order_id}; not re-sent; reconcile (lookup) before any retry")
        try:
            contract, ib_order = self._build_contract_and_order(order)
        except ValueError as exc:
            return SubmitResult(SubmitStatus.REJECTED, reason=f"{NOT_TRANSMITTED}: {exc}")
        if not self._pacer.acquire("order", self._cfg.pacing_wait_s):
            return SubmitResult(SubmitStatus.REJECTED, reason=f"{NOT_TRANSMITTED}: pacing budget exhausted (R8)")
        details = {"order_ref": order.order_ref, "instrument_id": order.instrument_id,
                   "provider_ref": order.provider_ref, "side": order.side, "quantity": order.quantity.to_str()}
        allocation, anomaly = self._store.prepare_transmit(order.client_order_id, nvid, details)  # fsync'd first
        order_id = allocation.order_id
        with self._cv:
            if anomaly:
                self._anomaly("ID_REGRESSION", anomaly)
            live = self._live.setdefault(order_id, _Live(self._cfg.client_id, order_id))
            live.order_ref, live.side, live.total_qty = order.order_ref, order.side, order.quantity
            start_seq, start_updates = self._seq, live.updates
        try:
            client.placeOrder(order_id, contract, ib_order)
        except Exception as exc:
            return SubmitResult(SubmitStatus.UNKNOWN, broker_order_id=str(order_id),
                                reason=f"transport error after transmit attempt: {type(exc).__name__}: {exc}",
                                raw={"order_id": order_id})
        with self._cv:
            self._cv.wait_for(lambda: self._submit_decided(live, start_seq, start_updates),
                              timeout=self._cfg.submit_timeout_s)
            status, reason = self._submit_outcome(live, start_seq, start_updates)
            raw = {"order_id": order_id, "perm_id": live.perm_id, "status": live.latest,
                   "errors": [{"code": c, "text": t} for _, c, t in live.codes_since(start_seq)],
                   "superseded_order_ids": list(allocation.superseded_order_ids), "evidence_level": EVIDENCE_LEVEL}
        return SubmitResult(status, broker_order_id=str(order_id), reason=reason, raw=raw)

    def _validate_order(self, order: BrokerOrder) -> str:
        if order.side not in ("BUY", "SELL"):
            return f"unsupported side {order.side!r} (BUY/SELL only; SSHORT/SLONG are institutional, R11)"
        if order.time_in_force != "DAY":
            return f"unsupported time in force {order.time_in_force!r} (DAY only)"
        if order.outside_regular_hours:
            return "outside regular trading hours is not enabled"
        if order.currency not in self._cfg.currencies:
            return f"currency {order.currency} is not configured for this account"
        if not order.quantity.is_positive() or not order.quantity.is_multiple_of(Fixed(1)):
            return "quantity must be a positive whole number of shares (fractional quantities disabled)"
        if not order.limit_price.is_positive():
            return "limit price must be positive"
        if not order.provider_ref:
            return "provider_ref (IBKR conId or symbol) is required"
        return ""

    def _build_contract_and_order(self, order: BrokerOrder):
        contract = self._sdk.Contract()
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = order.currency
        if order.provider_ref.isdigit():
            contract.conId = int(order.provider_ref)
        else:
            contract.symbol = order.provider_ref
        for name, value in (self._contract_fields(order) if self._contract_fields else {}).items():
            if not hasattr(contract, name):
                raise ValueError(f"contract field {name!r} does not exist on ibapi Contract")
            setattr(contract, name, value)
        ib = self._sdk.Order()
        ib.action = order.side
        ib.totalQuantity = Decimal(order.quantity.to_str())
        ib.orderType = "LMT"
        ib.lmtPrice = M.fixed_to_order_float(order.limit_price)
        ib.tif = "DAY"
        ib.orderRef = order.order_ref            # a tag only; R3 open question: echoed for individual accounts?
        ib.outsideRth = False
        ib.transmit = True
        ib.account = self._cfg.expected_account_id
        return contract, ib

    def _submit_decided(self, live: _Live, seq: int, updates: int) -> bool:
        if not self._socket_up or self._upstream_down is not None:
            return True
        codes = {c for _, c, _ in live.codes_since(seq)}
        if codes & M.SUBMIT_REJECT_CODES or M.ORDER_CANCELLED_BY_SYSTEM in codes:
            return True
        return live.updates > updates and live.latest is not None and live.latest != "PendingSubmit"

    def _submit_outcome(self, live: _Live, seq: int, updates: int) -> tuple[SubmitStatus, str]:
        """R4 + the brief: ACCEPTED on PreSubmitted/Submitted/Filled (or a host-acknowledged PendingSubmit at the
        deadline); REJECTED on a rejection code with no fills; UNKNOWN on timeout or connectivity loss."""
        errors = live.codes_since(seq)
        text = "; ".join(f"IBKR {c}: {t}" for _, c, t in errors)
        has_fills = live.filled is not None and live.filled.is_positive()
        rejects = [c for _, c, _ in errors if c in M.SUBMIT_REJECT_CODES]
        if rejects and not has_fills:
            if M.DUPLICATE_ORDER_ID in rejects:
                self._entry_blocks["DUPLICATE_ORDER_ID"] = "error 103: order-id resync required (R3)"
            return SubmitStatus.REJECTED, text
        status = live.latest if live.updates > updates else None
        if M.ORDER_CANCELLED_BY_SYSTEM in {c for _, c, _ in errors} and not has_fills:
            return SubmitStatus.REJECTED, text
        if status == "Cancelled":
            return (SubmitStatus.ACCEPTED, f"Cancelled after fills; {text}") if has_fills else \
                (SubmitStatus.REJECTED, text or "Cancelled by the host before acceptance")
        if status in M.ACCEPTANCE_STATUSES:
            return SubmitStatus.ACCEPTED, f"IBKR {status}" + (f"; {text}" if text else "")
        if not self._socket_up:
            return SubmitStatus.UNKNOWN, "socket lost while awaiting acknowledgement; outcome unknown"
        if self._upstream_down is not None:
            return SubmitStatus.UNKNOWN, (f"IBKR {self._upstream_down} while awaiting acknowledgement; the host may "
                                          "resubmit when connectivity returns (R7)")
        if status == "Inactive":
            return SubmitStatus.UNKNOWN, ("IBKR Inactive (precaution/hold/rejection; R4 open question); "
                                          "reconcile" + (f"; {text}" if text else ""))
        if status == "PendingSubmit":
            return SubmitStatus.ACCEPTED, "IBKR PendingSubmit: host acknowledged; destination confirmation pending"
        if status is not None:
            return SubmitStatus.UNKNOWN, f"UNRECOGNIZED_STATUS {status!r}" + (f"; {text}" if text else "")
        return SubmitStatus.UNKNOWN, f"timeout after {self._cfg.submit_timeout_s}s without acknowledgement" + \
            (f"; {text}" if text else "")

    def cancel(self, client_order_id: str, broker_order_id: str | None) -> SubmitResult:
        with self._cv:
            reason = self._ready_reason()
            if reason is None and self._upstream_down is not None:
                reason = f"IBKR {self._upstream_down}: upstream down; cancel not sent (R7)"
            client = self._client
        if reason:
            return SubmitResult(SubmitStatus.REJECTED, broker_order_id=broker_order_id,
                                reason=f"{NOT_TRANSMITTED}: {reason}")
        allocation = self._store.get(client_order_id)
        if allocation is None:
            return SubmitResult(SubmitStatus.REJECTED, broker_order_id=broker_order_id,
                                reason=f"{NOT_TRANSMITTED}: no IBKR order id allocated for {client_order_id}")
        order_id = allocation.order_id
        if broker_order_id is not None and broker_order_id != str(order_id):
            return SubmitResult(SubmitStatus.REJECTED, broker_order_id=broker_order_id,
                                reason=f"{NOT_TRANSMITTED}: broker order id {broker_order_id} does not match {order_id}")
        if not allocation.transmitted:
            return SubmitResult(SubmitStatus.REJECTED, broker_order_id=str(order_id),
                                reason=f"{NOT_TRANSMITTED}: order id {order_id} was never transmitted")
        with self._cv:
            live = self._live.setdefault(order_id, _Live(self._cfg.client_id, order_id))
            if live.latest == "Filled":
                return SubmitResult(SubmitStatus.REJECTED, broker_order_id=str(order_id),
                                    reason=f"{NOT_TRANSMITTED}: too late, IBKR reports Filled")
        if not self._pacer.acquire("cancel", self._cfg.pacing_wait_s):
            return SubmitResult(SubmitStatus.REJECTED, broker_order_id=str(order_id),
                                reason=f"{NOT_TRANSMITTED}: pacing budget exhausted (R8)")
        with self._cv:
            live.cancel_requested = True
            start_seq, start_updates = self._seq, live.updates
        try:
            # SDK 10.51.01 signature: cancelOrder(self, orderId: OrderId, orderCancel: OrderCancel).
            client.cancelOrder(order_id, self._sdk.OrderCancel())
        except Exception as exc:
            return SubmitResult(SubmitStatus.UNKNOWN, broker_order_id=str(order_id),
                                reason=f"transport error after cancel attempt: {type(exc).__name__}: {exc}")
        with self._cv:
            self._cv.wait_for(lambda: self._cancel_decided(live, start_seq, start_updates),
                              timeout=self._cfg.cancel_timeout_s)
            status, reason = self._cancel_outcome(live, start_seq, start_updates)
            raw = {"order_id": order_id, "status": live.latest,
                   "errors": [{"code": c, "text": t} for _, c, t in live.codes_since(start_seq)]}
        return SubmitResult(status, broker_order_id=str(order_id), reason=reason, raw=raw)

    def _cancel_decided(self, live: _Live, seq: int, updates: int) -> bool:
        if not self._socket_up:
            return True
        codes = {c for _, c, _ in live.codes_since(seq)}
        if codes & M.CANCEL_REJECT_CODES or M.ORDER_CANCELLED_BY_SYSTEM in codes:
            return True
        return live.updates > updates and live.latest in (M.CANCEL_ACK_STATUSES | {"Filled"})

    def _cancel_outcome(self, live: _Live, seq: int, updates: int) -> tuple[SubmitStatus, str]:
        errors = live.codes_since(seq)
        text = "; ".join(f"IBKR {c}: {t}" for _, c, t in errors)
        if live.updates > updates and live.latest in M.CANCEL_ACK_STATUSES:
            return SubmitStatus.ACCEPTED, f"IBKR {live.latest}" + (f"; {text}" if text else "")
        if M.ORDER_CANCELLED_BY_SYSTEM in {c for _, c, _ in errors}:
            return SubmitStatus.ACCEPTED, text   # 202 "Order cancelled - Reason:" after our own cancel request
        if any(c in M.CANCEL_REJECT_CODES for _, c, _ in errors):
            return SubmitStatus.REJECTED, text
        if live.updates > updates and live.latest == "Filled":
            return SubmitStatus.REJECTED, "too late: IBKR reports Filled"
        if not self._socket_up:
            return SubmitStatus.UNKNOWN, "socket lost while awaiting cancel acknowledgement"
        return SubmitStatus.UNKNOWN, f"timeout after {self._cfg.cancel_timeout_s}s without cancel acknowledgement" + \
            (f"; {text}" if text else "")

    # ======================================================================= port: snapshots
    def account_snapshot(self) -> AccountSnapshot:
        self._require_ready()
        cfg = self._cfg
        with self._req_lock:
            client = self._client_checked()
            self._cancel_lingering_summaries(client)
            with self._cv:
                if len(self._active_summary) >= MAX_SUMMARY_SUBSCRIPTIONS:
                    raise IbkrNotReady("R6: two account-summary subscriptions are already active; a third is refused")
                req = _SummaryRequest(self._new_req_id())
                self._summary_reqs[req.req_id] = req
                self._positions_collect, self._positions_end = [], False
            reasons: list[str] = []
            summary_sent = positions_sent = False
            try:
                # SDK 10.51.01: reqAccountSummary(self, reqId: int, groupName: str, tags: str); reqPositions(self).
                err = self._send("reconcile", client.reqAccountSummary, req.req_id, "All", ",".join(SUMMARY_TAGS))
                if err is None:
                    summary_sent = True
                    with self._cv:
                        self._active_summary.add(req.req_id)
                    err = self._send("reconcile", client.reqPositions)
                    positions_sent = err is None
                if err:
                    reasons.append(err)
                else:
                    with self._cv:
                        self._cv.wait_for(lambda: ((req.end or req.error is not None) and self._positions_end)
                                          or not self._socket_up, timeout=cfg.request_timeout_s)
            finally:
                # Subscriptions are cancelled after every snapshot so at most two are ever active (R6).
                if summary_sent and self._send("reconcile", client.cancelAccountSummary, req.req_id) is None:
                    with self._cv:
                        self._active_summary.discard(req.req_id)
                if positions_sent:
                    self._send("reconcile", client.cancelPositions)
                with self._cv:
                    rows, summary_end, summary_error = list(req.rows), req.end, req.error
                    positions, positions_end = list(self._positions_collect or ()), self._positions_end
                    self._positions_collect = None
                    self._summary_reqs.pop(req.req_id, None)
        if not summary_end:
            reasons.append(summary_error or "accountSummaryEnd not received within the timeout")
        if not positions_end:
            reasons.append("positionEnd not received within the timeout")
        acct = cfg.expected_account_id
        values: dict[tuple[str, str], str] = {}
        for account, tag, value, currency in rows:
            if account == acct:
                values[(tag, currency or "")] = value
            else:
                reasons.append(f"account summary row for unexpected account {self._mask(account)}")
        account_type = next((v for (t, _), v in values.items() if t == "AccountType"), None)
        if cfg.expected_account_type is not None and account_type != cfg.expected_account_type:
            # R11: an AccountType other than the expected one is a blocking mismatch.
            reasons.append(f"ACCOUNT_TYPE_MISMATCH: reported {account_type!r}, expected {cfg.expected_account_type!r}")
        cash = []
        for ccy in sorted({c for (t, c) in values if t in ("TotalCashValue", "SettledCash") and c}):
            settled = M.summary_value_to_fixed(values.get(("SettledCash", ccy)))
            total = M.summary_value_to_fixed(values.get(("TotalCashValue", ccy)))
            if settled is None or total is None:
                reasons.append(f"SettledCash/TotalCashValue missing or unset for {ccy}")
                continue
            cash.append(CashBalance(ccy, settled, total, CASH_SEMANTICS))
        if not cash:
            reasons.append("no SettledCash/TotalCashValue rows received")
        position_rows = []
        for account, con_id, ccy, symbol, quantity, avg_cost in positions:
            if account != acct:
                reasons.append(f"position for unexpected account {self._mask(account)}")
                continue
            qty = M.decimal_to_fixed(quantity)
            if qty is None:
                reasons.append(f"unset position quantity for conId {con_id}")
                continue
            if qty.is_zero():
                continue   # closed today; reported with zero quantity
            position_rows.append(PositionRow(instrument_id=self._resolve_instrument(con_id, None),
                                             provider_ref=str(con_id), quantity=qty,
                                             average_cost=M.float_to_fixed(avg_cost), currency=ccy))
        raw = {"provider": PROVIDER, "evidence_level": EVIDENCE_LEVEL, "account": cfg.masked_account(),
               "account_type": account_type,
               "summary": {f"{t}:{c}" if c else t: v for (t, c), v in sorted(values.items())},
               "incomplete_reasons": reasons, "cash_semantics": CASH_SEMANTICS}
        return AccountSnapshot(as_of_ns=self._clock.now_ns(), cash=tuple(cash),
                               positions=tuple(sorted(position_rows, key=lambda p: p.provider_ref)),
                               complete=not reasons, server_time_ns=None, raw=raw)

    def _cancel_lingering_summaries(self, client) -> None:
        with self._cv:
            lingering = sorted(self._active_summary)
        for req_id in lingering:
            if self._send("reconcile", client.cancelAccountSummary, req_id) is None:
                with self._cv:
                    self._active_summary.discard(req_id)

    def open_orders(self) -> tuple[list[OpenOrderRow], bool]:
        """reqOpenOrders (our client) then reqAllOpenOrders (all clients and TWS), each to openOrderEnd (R3)."""
        self._require_ready()
        with self._req_lock:
            client = self._client_checked()
            collected, complete = self._collect_open_orders(client, (client.reqOpenOrders, client.reqAllOpenOrders))
        rows = []
        for snap in collected.values():
            row = self._open_row(snap)
            if row is None:
                complete = False
            elif row is not _EXCLUDED:
                rows.append(row)
        return rows, complete

    def _collect_open_orders(self, client, requests) -> tuple[dict, bool]:
        with self._cv:
            self._oo_collect, self._oo_ends = {}, 0
        complete = True
        try:
            for n, fn in enumerate(requests, start=1):
                if self._send("reconcile", fn) is not None:
                    complete = False
                    break
                with self._cv:
                    if not self._cv.wait_for(lambda n=n: self._oo_ends >= n or not self._socket_up,
                                             timeout=self._cfg.request_timeout_s) or self._oo_ends < n:
                        complete = False
                        break
        finally:
            with self._cv:
                collected = dict(self._oo_collect or {})
                self._oo_collect = None
        return collected, complete

    def _open_row(self, snap: dict):
        with self._cv:
            live = self._live_for(snap["client_id"], snap["order_id"], snap["perm_id"], create=False)
            status_text = (live.latest if live and live.latest else None) or snap["status"]
            filled = live.filled if live and live.filled is not None else None
            remaining = live.remaining if live and live.remaining is not None else None
            rejection = live.rejection_seen() if live else False
        if status_text in M.OPEN_ORDER_EXCLUDED:
            return _EXCLUDED
        if filled is None:
            filled = M.decimal_to_fixed(snap["filled_quantity"]) or Fixed(0)
        total = M.decimal_to_fixed(snap["total_quantity"])
        if remaining is None and total is not None:
            remaining = total - filled if total > filled else Fixed(0)
        status, note = M.normalize_status(status_text, filled, rejection_seen=rejection)
        if status is None or remaining is None:
            with self._cv:
                self._anomaly("OPEN_ORDER_UNREPRESENTABLE", f"{note or 'remaining quantity unknown'}: {snap}")
            return None
        ours = snap["client_id"] == self._cfg.client_id
        alloc = self._store.by_order_id(snap["order_id"]) if ours else None
        return OpenOrderRow(
            client_order_id=alloc.client_order_id if alloc else None,
            order_ref=(alloc.order_ref if alloc and alloc.order_ref else None) or snap["order_ref"] or None,
            broker_order_id=M.broker_order_key(self._cfg.client_id, snap["client_id"], snap["order_id"],
                                               snap["perm_id"]),
            instrument_id=self._resolve_instrument(snap["con_id"], alloc), side=snap["side"], status=status,
            filled_quantity=filled, remaining_quantity=remaining)

    # ======================================================================= port: executions
    def executions_since(self, watermark: str | None) -> ExecutionBatch:
        """reqExecutions(empty ExecutionFilter) to execDetailsEnd, then rows after ``watermark``.

        The watermark indexes this adapter instance's append-only execution and
        commission logs; any other watermark returns everything retrievable
        (overlap is deduplicated by the core on exec_key). complete=True only
        after execDetailsEnd, with no unrepresentable execution and no
        unacknowledged history gap (R5)."""
        self._require_ready()
        complete, why = self._fetch_executions()
        with self._cv:
            today = self._host_day()
            wm, valid = _parse_watermark(watermark)
            gap = self._history_gap(wm, valid, today)
            if (wm is not None and wm.instance == self._instance and wm.executions <= len(self._exec_log)
                    and wm.commissions <= len(self._comm_log)):
                executions, commissions = self._exec_log[wm.executions:], self._comm_log[wm.commissions:]
            else:
                executions, commissions = list(self._exec_log), list(self._comm_log)
            if self._unrepresentable:
                complete = False
            if gap:
                complete = False
                self._anomaly("HISTORY_GAP", gap)
            new_watermark = _make_watermark(self._instance, len(self._exec_log), len(self._comm_log), today)
        if not complete:
            log.warning("IBKR executions incomplete: %s", why or gap or "unrepresentable executions held")
        return ExecutionBatch(executions=tuple(executions), commissions=tuple(commissions), watermark=new_watermark,
                              complete=complete)

    def _fetch_executions(self) -> tuple[bool, str]:
        with self._req_lock:
            client = self._client_checked()
            req = _ExecRequest(self._new_req_id())
            with self._cv:
                self._exec_reqs[req.req_id] = req
            try:
                exec_filter = self._sdk.ExecutionFilter()   # empty filter = all available executions (R5)
                if self._cfg.execution_last_n_days:
                    exec_filter.lastNDays = self._cfg.execution_last_n_days
                err = self._send("reconcile", client.reqExecutions, req.req_id, exec_filter)
                if err:
                    return False, err
                with self._cv:
                    self._cv.wait_for(lambda: req.end or req.error is not None or not self._socket_up,
                                      timeout=self._cfg.request_timeout_s)
                    if req.error:
                        return False, req.error
                    if not req.end:
                        return False, "execDetailsEnd not received within the timeout"
                    return True, ""
            finally:
                with self._cv:
                    self._exec_reqs.pop(req.req_id, None)

    def _history_gap(self, wm: _Watermark | None, valid: bool, today: date) -> str | None:
        if not valid:
            return "unrecognized watermark: completeness of earlier executions cannot be established"
        if wm is None:
            return None
        window_start = today - timedelta(days=self._cfg.execution_history_days - 1)
        covered_from = window_start
        if wm.instance == self._instance and self._coverage_start_day is not None:
            covered_from = min(window_start, self._coverage_start_day)
        if wm.issue_day >= covered_from:
            return None
        acked = self._store.history_gap_acked_through()
        if acked is not None and acked >= (covered_from - timedelta(days=1)).strftime("%Y%m%d"):
            return None
        return (f"HISTORY_GAP: watermark issued {wm.issue_day} but executions are retrievable only from "
                f"{covered_from} (IB Gateway: current day since midnight, R5); reconcile from statements/Flex and "
                f"acknowledge through {covered_from - timedelta(days=1)}")

    # ======================================================================= port: lookup
    def lookup(self, client_order_id: str) -> LookupResult:
        """Tri-state: True (broker trace found), False (never transmitted), None (cannot determine)."""
        with self._cv:
            reason = self._ready_reason()
        if reason:
            return LookupResult(found=None, reason=f"disconnected: {reason}")
        allocation = self._store.get(client_order_id)
        if allocation is None:
            return LookupResult(found=None, reason="no IBKR order-id allocation recorded for this client order id; "
                                                   "adapter state cannot prove it was never sent")
        if not allocation.transmitted:
            return LookupResult(found=False, broker_order_id=str(allocation.order_id),
                                reason="order id allocated but placeOrder was never called (persisted state)")
        incomplete: list[str] = []
        try:
            with self._req_lock:
                client = self._client_checked()
                found = self._lookup_open(client, allocation, incomplete)
                if found is None:
                    found = self._lookup_completed(client, allocation, incomplete)
                if found is None:
                    found = self._lookup_executions(allocation, incomplete)
        except IbkrNotReady as exc:
            return LookupResult(found=None, reason=f"disconnected: {exc}")
        if found is not None:
            return found
        why = "; ".join(incomplete) if incomplete else ("transmitted but no open, completed or execution trace "
                                                         "(host may still resubmit; R7)")
        return LookupResult(found=None, broker_order_id=str(allocation.order_id), reason=why)

    def _lookup_open(self, client, allocation: Allocation, incomplete: list[str]) -> LookupResult | None:
        collected, complete = self._collect_open_orders(client, (client.reqOpenOrders,))
        if not complete:
            incomplete.append("reqOpenOrders did not complete")
        for snap in collected.values():
            if snap["client_id"] == self._cfg.client_id and snap["order_id"] == allocation.order_id:
                row = self._open_row(snap)
                if row is None:
                    return LookupResult(found=True, broker_order_id=str(allocation.order_id),
                                        reason=f"open at IBKR with unrecognized status {snap['status']!r}")
                if row is _EXCLUDED:
                    continue
                return LookupResult(found=True, status=row.status, broker_order_id=row.broker_order_id,
                                    filled_quantity=row.filled_quantity, remaining_quantity=row.remaining_quantity,
                                    reason="open order (reqOpenOrders)")
        return None

    def _lookup_completed(self, client, allocation: Allocation, incomplete: list[str]) -> LookupResult | None:
        with self._cv:
            self._co_collect, self._co_end = [], False
            live = self._live.get(allocation.order_id)
            perm_id = allocation.perm_id or (live.perm_id if live else 0)
        try:
            # SDK 10.51.01: reqCompletedOrders(self, apiOnly: bool) -> completedOrder(...) ... completedOrdersEnd().
            if self._send("reconcile", client.reqCompletedOrders, True) is not None:
                incomplete.append("reqCompletedOrders not sent")
                return None
            with self._cv:
                self._cv.wait_for(lambda: self._co_end or not self._socket_up, timeout=self._cfg.request_timeout_s)
                done, collected = self._co_end, list(self._co_collect or ())
        finally:
            with self._cv:
                self._co_collect = None
        if not done:
            incomplete.append("completedOrdersEnd not received")
        for snap in collected:
            match = ((perm_id and snap["perm_id"] == perm_id)
                     or (snap["client_id"] == self._cfg.client_id and snap["order_id"] == allocation.order_id
                         and snap["order_id"] not in (0, M.UNSET_INTEGER))
                     or (allocation.order_ref and snap["order_ref"] == allocation.order_ref))
            if not match:
                continue
            filled = M.decimal_to_fixed(snap["filled_quantity"]) or Fixed(0)
            total = M.decimal_to_fixed(snap["total_quantity"])
            remaining = (total - filled if total is not None and total > filled else Fixed(0))
            with self._cv:
                rejection = live.rejection_seen() if live else False
            status, note = M.normalize_status(snap["status"], filled, rejection_seen=rejection)
            return LookupResult(found=True, status=status, broker_order_id=str(allocation.order_id),
                                filled_quantity=filled, remaining_quantity=remaining,
                                reason=f"completed order: {snap['status']} {snap['completed_status']}".strip()
                                + (f" ({note})" if note else ""))
        return None

    def _lookup_executions(self, allocation: Allocation, incomplete: list[str]) -> LookupResult | None:
        complete, why = self._fetch_executions()
        if not complete:
            incomplete.append(f"executions incomplete: {why}")
        with self._cv:
            effective: dict[str, tuple[int, Fixed]] = {}
            for row in self._exec_log:
                if row.client_order_id != allocation.client_order_id:
                    continue
                base, rev, _ = M.parse_exec_id(row.provider_exec_id)
                if base not in effective or (rev or 0) >= effective[base][0]:
                    effective[base] = (rev or 0, row.quantity)   # R5: highest revision per base is effective
        if not effective:
            return None
        filled = sum((q for _, q in effective.values()), Fixed(0))
        total = Fixed.parse(allocation.quantity) if allocation.quantity else None
        status = BrokerOrderStatus.FILLED if total is not None and filled == total else None
        return LookupResult(found=True, status=status, broker_order_id=str(allocation.order_id),
                            filled_quantity=filled,
                            remaining_quantity=(total - filled) if total is not None and total > filled else Fixed(0),
                            reason="executions found but the order is neither open nor completed"
                            if status is None else "fully executed (executions)")

    def drain_events(self, limit: int = 1000) -> list[BrokerEvent]:
        out: list[BrokerEvent] = []
        with self._cv:
            while self._events and len(out) < limit:
                out.append(self._events.popleft())
        return out

    # ======================================================================= helpers
    def _ready_reason(self) -> str | None:
        if self._unsafe:
            return self._unsafe
        if not self._socket_up or self._client is None:
            return self._auth_reason or "NOT_CONNECTED"
        if self._next_valid_id is None:
            return self._auth_reason or "awaiting nextValidId (R2: no request is sent before it)"
        if not self._mode_verified:
            return "MODE_UNVERIFIED"
        return None

    def _entry_block_reason(self) -> str | None:
        reason = self._ready_reason()
        if reason:
            return reason
        if self._upstream_down is not None:
            return f"IBKR {self._upstream_down}: host lost connectivity to IBKR; new entries blocked (R7)"
        if self._login_failed:
            return self._login_failed
        if self._entry_blocks:
            return "entry blocked: " + "; ".join(f"{k}: {v}" for k, v in sorted(self._entry_blocks.items()))
        return None

    def _require_ready(self) -> None:
        with self._cv:
            reason = self._ready_reason()
        if reason:
            raise IbkrNotReady(reason)

    def _client_checked(self):
        with self._cv:
            reason = self._ready_reason()
            client = self._client
        if reason or client is None:
            raise IbkrNotReady(reason or "NOT_CONNECTED")
        return client

    def _new_req_id(self) -> int:
        with self._cv:
            self._req_seq += 1
            return self._req_seq

    def _send(self, cls: str, fn, *args) -> str | None:
        """Pace then call one EClient request; returns an error string instead of raising."""
        if not self._pacer.acquire(cls, self._cfg.pacing_wait_s):
            return f"pacing budget exhausted for {cls} requests (R8)"
        try:
            fn(*args)
            return None
        except Exception as exc:
            return f"transport error: {type(exc).__name__}: {exc}"

    def _resync_order_ids(self) -> None:
        """After error 103 ask the host for a fresh nextValidId (R3) before any further entry."""
        with self._cv:
            before = self._nvid_seq
            client = self._client
        if client is None or self._send("query", client.reqIds, 1) is not None:
            return
        with self._cv:
            if self._cv.wait_for(lambda: self._nvid_seq > before or not self._socket_up,
                                 timeout=self._cfg.request_timeout_s) and self._nvid_seq > before:
                self._entry_blocks.pop("DUPLICATE_ORDER_ID", None)

    def _host_day(self) -> date:
        return datetime.fromtimestamp(self._clock.now_ns() // 10**9, self._tz).date()

    def _emit(self, event: BrokerEvent) -> None:
        self._events.append(event)

    def _anomaly(self, kind: str, detail: str) -> None:
        self._anomalies.append({"at_ns": self._clock.now_ns(), "kind": kind, "detail": detail})
        log.warning("IBKR %s: %s", kind, detail)

    def _socket_down(self, detail: str) -> None:
        if not self._socket_up:
            return
        self._socket_up = False
        self._mode_verified = False
        self._late_handshake = False
        self._time_req_sent = None
        self._down_since_day = self._host_day()
        self._emit(ConnectivityEvent(False, False, f"SOCKET_DOWN: {detail}", self._clock.now_ns()))
        self._cv.notify_all()

    def _live_for(self, client_id: int, order_id: int, perm_id: int, *, create: bool = True) -> _Live | None:
        if client_id == self._cfg.client_id or (client_id in (M.UNSET_INTEGER, None)
                                                 and self._store.by_order_id(order_id) is not None):
            live = self._live.get(order_id)
            if live is None and create:
                live = self._live[order_id] = _Live(self._cfg.client_id, order_id)
            return live
        key = ("perm", perm_id) if perm_id else ("client", client_id, order_id)
        live = self._foreign.get(key)
        if live is None and create:
            live = self._foreign[key] = _Live(client_id, order_id)
        return live

    def _resolve_instrument(self, con_id, allocation: Allocation | None) -> str | None:
        instrument = None
        if con_id:
            try:
                instrument = self._resolver(int(con_id))
            except Exception as exc:
                with self._cv:
                    self._anomaly("INSTRUMENT_RESOLVER_FAILED", f"conId {con_id}: {type(exc).__name__}: {exc}")
        if instrument is None and allocation is not None and allocation.provider_ref == str(con_id):
            instrument = allocation.instrument_id
        return instrument

    # ======================================================================= callbacks (message thread)
    def _dispatch(self, name: str, *args) -> None:
        handler = getattr(self, f"_on_{name}")
        try:
            with self._cv:
                self._last_message_ns = self._clock.now_ns()
            handler(*args)
        except Exception as exc:   # a handler failure must never kill the SDK message loop
            log.exception("IBKR callback %s failed", name)
            with self._cv:
                self._anomaly("CALLBACK_FAILED", f"{name}: {type(exc).__name__}: {exc}")

    def _on_connectAck(self) -> None:
        pass

    def _on_currentTimeInMillis(self, time_in_millis: int) -> None:
        with self._cv:
            recv = self._clock.monotonic_ns()
            sent, self._time_req_sent = self._time_req_sent, None
            if sent is None:
                self._anomaly("SERVER_TIME_UNSOLICITED", "currentTimeInMillis without a pending query; ignored")
                return
            rtt = recv - sent
            if rtt > SERVER_TIME_MAX_RTT_NS:
                self._anomaly("SERVER_TIME_SLOW", f"clock round trip {rtt // 1_000_000} ms; sample discarded")
                return
            # The host read its clock somewhere inside the round trip: take the midpoint (error <= rtt / 2).
            self._server_time = (int(time_in_millis) * 1_000_000, sent + rtt // 2)
            self._cv.notify_all()

    def _on_nextValidId(self, order_id: int) -> None:
        with self._cv:
            self._next_valid_id = int(order_id)
            self._nvid_seq += 1
            self._cv.notify_all()
        self._maybe_finish_late_handshake()

    def _maybe_finish_late_handshake(self) -> None:
        """A slow host sent nextValidId/managedAccounts after connect() gave up waiting: verify now."""
        with self._cv:
            ready = (self._late_handshake and self._socket_up and self._unsafe is None and not self._mode_verified
                     and self._next_valid_id is not None and self._managed is not None)
            if not ready:
                return
            self._late_handshake = False
            client = self._client
        if client is not None:
            self._verify_identity(client)

    def _on_managedAccounts(self, accounts_list: str) -> None:
        accounts = tuple(a.strip() for a in (accounts_list or "").split(",") if a.strip())
        disconnect = None
        with self._cv:
            self._managed = accounts
            if self._mode_verified and set(accounts) != {self._cfg.expected_account_id}:
                self._unsafe = "DISCONNECTED_UNSAFE: managed accounts changed during the session"
                self._mode_verified = False
                self._anomaly("IDENTITY_MISMATCH", self._unsafe)
                self._emit(ConnectivityEvent(False, False, self._unsafe, self._clock.now_ns()))
                disconnect = self._client
            self._cv.notify_all()
        if disconnect is not None:
            _safe_call(disconnect.disconnect)
            return
        self._maybe_finish_late_handshake()

    def _on_connectionClosed(self) -> None:
        with self._cv:
            self._socket_down("connectionClosed")

    def _on_error(self, req_id, error_time, code, text, advanced) -> None:
        code = int(code)
        text = str(text)
        now = self._clock.now_ns()
        disconnect = None
        with self._cv:
            self._seq += 1
            seq = self._seq
            if M.LOGIN_FAILED_MARKER in text:
                # R9 analytics: auth_required = login failure message observed. Order access is unavailable until
                # the owner logs in manually; the adapter never automates login.
                self._login_failed = f"{M.AUTH_REQUIRED_REASON} ({text})"
                self._emit(ConnectivityEvent(self._socket_up, False, self._login_failed, now))
            if code in M.UPSTREAM_DOWN:
                if self._upstream_down is None:
                    self._upstream_down = code
                    self._emit(ConnectivityEvent(False, False, f"UPSTREAM_DOWN {code}: {text}", now))
            elif code in (M.RESTORED_DATA_LOST, M.RESTORED_DATA_KEPT):
                was = self._upstream_down
                self._upstream_down = None
                self._login_failed = None
                kind = "data lost; market data must be re-requested" if code == M.RESTORED_DATA_LOST else \
                    "data maintained"
                self._emit(ConnectivityEvent(self._socket_up, self._socket_up and self._mode_verified,
                                             f"RESTORED {code} ({kind}) after {was}: reconcile before new entries", now))
            elif code == M.SOCKET_PORT_RESET:
                disconnect = self._client
                self._socket_down(f"1300 socket port reset: {text}")
            elif code == M.CLIENT_ID_IN_USE:
                # ADR-0011 #4: refuse to run when the client id is already in use.
                self._unsafe = f"DISCONNECTED_UNSAFE: client id {self._cfg.client_id} already in use (326)"
                self._anomaly("CLIENT_ID_IN_USE", text)
                disconnect = self._client
                self._socket_down(self._unsafe)
            elif code == M.CONNECT_FAIL:
                self._last_connect_error = f"HOST_DOWN 502: {text}"
            elif code == M.NOT_CONNECTED:
                self._socket_down(f"504 not connected: {text}")
            elif code == M.HOST_OUT_OF_DATE:
                self._entry_blocks["HOST_OUT_OF_DATE"] = f"503: {text} (R1 READ_ONLY)"
                self._anomaly("HOST_OUT_OF_DATE", text)
            elif code == M.PACING_VIOLATION:
                self._pacing_violations += 1
                self._pacer.penalize(self._cfg.pacing_backoff_s)
                self._anomaly("PACING_VIOLATION", f"error 100 #{self._pacing_violations}: {text}")
                if self._pacing_violations >= 2:
                    self._entry_blocks["PACING_VIOLATION"] = "error 100 twice in this session (R8)"
            elif code in M.INFORMATIONAL or code in M.DATA_FARM_DOWN or code in M.MARKET_DATA_ONLY:
                log.info("IBKR %s: %s", code, text)
            if code in M.SHORT_SALE_SIGNALS:
                self._entry_blocks["SHORT_SALE_SIGNAL"] = f"IBKR {code}: {text} (R11)"
                self._anomaly("SHORT_SALE_SIGNAL", f"order {req_id}: {code} {text}")
            exec_req = self._exec_reqs.get(req_id)
            summary_req = self._summary_reqs.get(req_id)
            if exec_req is not None:
                exec_req.error = f"IBKR {code}: {text}"
            elif summary_req is not None:
                summary_req.error = f"IBKR {code}: {text}"
            elif isinstance(req_id, int) and 0 < req_id < REQUEST_ID_BASE and (
                    req_id in self._live or self._store.by_order_id(req_id) is not None):
                live = self._live.setdefault(req_id, _Live(self._cfg.client_id, req_id))
                live.errors.append((seq, code, text + (f" {advanced}" if advanced else "")))
            self._cv.notify_all()
        if disconnect is not None:
            _safe_call(disconnect.disconnect)

    def _on_orderStatus(self, order_id, status, filled, remaining, avg_fill_price, perm_id, parent_id,
                        last_fill_price, client_id, why_held, mkt_cap_price) -> None:
        now = self._clock.now_ns()
        with self._cv:
            live = self._live_for(client_id, order_id, perm_id)
            ours = live.client_id == self._cfg.client_id
            if perm_id and perm_id != M.UNSET_LONG and live.perm_id != perm_id:
                live.perm_id = perm_id
                if ours:
                    self._store.record_perm_id(order_id, perm_id)
            filled_fx, remaining_fx = M.decimal_to_fixed(filled), M.decimal_to_fixed(remaining)
            live.status, live.latest, live.why_held, live.updates = status, status, why_held or "", live.updates + 1
            if filled_fx is not None:
                if live.filled is not None and filled_fx < live.filled:
                    self._anomaly("FILL_MONOTONICITY", f"order {order_id}: filled {filled_fx} < {live.filled} (R4)")
                live.filled = filled_fx
            if remaining_fx is not None:
                live.remaining = remaining_fx
            if why_held == "locate":
                self._entry_blocks["SHORT_SALE_SIGNAL"] = f"order {order_id} held for locate (R11)"
                self._anomaly("SHORT_SALE_SIGNAL", f"order {order_id} whyHeld=locate")
            key = M.status_dedupe_key(live.perm_id, client_id, order_id, status, filled, remaining, avg_fill_price,
                                      last_fill_price, why_held, mkt_cap_price)
            if key == live.last_key:
                self._cv.notify_all()
                return   # R4: duplicate orderStatus messages are normal; one transition per distinct message
            live.last_key = key
            normalized, note = M.normalize_status(status, live.filled, rejection_seen=live.rejection_seen())
            if normalized is None or filled_fx is None or remaining_fx is None:
                self._anomaly("UNREPRESENTABLE_STATUS", f"order {order_id} client {client_id}: "
                                                        f"{note or 'unset filled/remaining'}")
                self._cv.notify_all()
                return
            alloc = self._store.by_order_id(order_id) if ours else None
            reasons = [note] if note else []
            reasons += [f"IBKR {c}: {t}" for _, c, t in live.errors[-3:]]
            if why_held:
                reasons.append(f"whyHeld={why_held}")
            self._emit(OrderStatusEvent(
                client_order_id=alloc.client_order_id if alloc else None,
                order_ref=(alloc.order_ref if alloc and alloc.order_ref else None) or live.order_ref or None,
                broker_order_id=M.broker_order_key(self._cfg.client_id, live.client_id, order_id, live.perm_id),
                status=normalized, filled_quantity=filled_fx, remaining_quantity=remaining_fx,
                reason="; ".join(reasons), event_time_ns=now,
                raw={"provider": PROVIDER, "status": status, "filled": str(filled), "remaining": str(remaining),
                     "avgFillPrice": repr(avg_fill_price), "permId": perm_id, "parentId": parent_id,
                     "lastFillPrice": repr(last_fill_price), "clientId": client_id, "whyHeld": why_held,
                     "mktCapPrice": repr(mkt_cap_price), "evidence_level": EVIDENCE_LEVEL}))
            self._cv.notify_all()

    def _on_openOrder(self, order_id, contract, order, order_state) -> None:
        with self._cv:
            client_id, perm_id = order.clientId, order.permId
            live = self._live_for(client_id, order_id, perm_id)
            if perm_id and live.perm_id != perm_id:
                live.perm_id = perm_id
                if live.client_id == self._cfg.client_id:
                    self._store.record_perm_id(order_id, perm_id)
            live.open_status = live.latest = order_state.status
            live.order_ref = order.orderRef or live.order_ref
            live.con_id = contract.conId
            live.side = order.action
            live.total_qty = M.decimal_to_fixed(order.totalQuantity)
            live.warning = order_state.warningText or ""
            live.reject_reason = order_state.rejectReason or ""
            live.updates += 1
            if self._oo_collect is not None:
                self._oo_collect[(client_id, order_id, perm_id)] = {
                    "client_id": client_id, "order_id": order_id, "perm_id": perm_id, "status": order_state.status,
                    "total_quantity": order.totalQuantity, "filled_quantity": order.filledQuantity,
                    "side": order.action, "order_ref": order.orderRef, "con_id": contract.conId}
            self._cv.notify_all()

    def _on_openOrderEnd(self) -> None:
        with self._cv:
            self._oo_ends += 1
            self._cv.notify_all()

    def _on_completedOrder(self, contract, order, order_state) -> None:
        with self._cv:
            if self._co_collect is not None:
                self._co_collect.append({
                    "client_id": order.clientId, "order_id": order.orderId, "perm_id": order.permId,
                    "status": order_state.status, "completed_status": order_state.completedStatus,
                    "total_quantity": order.totalQuantity, "filled_quantity": order.filledQuantity,
                    "order_ref": order.orderRef, "con_id": contract.conId})
            self._cv.notify_all()

    def _on_completedOrdersEnd(self) -> None:
        with self._cv:
            self._co_end = True
            self._cv.notify_all()

    def _on_execDetails(self, req_id, contract, execution) -> None:
        with self._cv:
            exec_id = execution.execId
            if exec_id in self._exec_by_id:
                self._cv.notify_all()
                return   # the same execId from the live push and from reqExecutions is stored once (R5)
            row, problem = self._execution_row(contract, execution)
            if row is None:
                # Never booked at a guessed value; held so executions_since reports complete=False.
                self._unrepresentable[exec_id] = {"problem": problem, "execution": str(execution)}
                self._anomaly("UNREPRESENTABLE_EXECUTION", f"{exec_id}: {problem}")
                self._cv.notify_all()
                return
            self._unrepresentable.pop(exec_id, None)
            self._exec_by_id[exec_id] = row
            self._exec_log.append(row)
            self._emit(ExecutionEvent(row))
            if row.client_order_id is None:
                self._anomaly("EXTERNAL_EXECUTION", f"{row.exec_key}: not placed by this adapter (permId "
                                                    f"{execution.permId}, clientId {execution.clientId}) (R3)")
            for report in self._pending_comm.pop(exec_id, []):
                self._accept_commission(report)
            self._cv.notify_all()

    def _execution_row(self, contract, execution) -> tuple[ExecutionRow | None, str]:
        cfg = self._cfg
        exec_id = execution.execId
        account = execution.acctNumber or cfg.expected_account_id
        side = M.side_from_execution(execution.side)
        if side is None:
            return None, f"unrecognized side {execution.side!r}"
        quantity = M.decimal_to_fixed(execution.shares)
        if quantity is None or not quantity.is_positive():
            return None, f"unset or non-positive shares {execution.shares!r}"
        price = M.float_to_fixed(execution.price)
        if price is None or not price.is_positive():
            return None, f"unset or non-positive price {execution.price!r}"
        try:
            exec_time_ns, time_source = M.parse_exec_time(execution.time, cfg.host_time_zone)
        except ValueError as exc:
            return None, str(exc)
        key = M.exec_key(account, exec_id)
        base, rev, width = M.parse_exec_id(exec_id)
        correction_of, link_raw = self._correction_link(account, base, rev, width, key)
        client_id, order_id, perm_id = execution.clientId, execution.orderId, execution.permId
        ours = client_id == cfg.client_id and perm_id != 0   # R3: permId 0 = trade originating outside IB
        alloc = None
        if ours:
            alloc = self._store.by_order_id(order_id) or self._store.by_perm_id(perm_id)
        if ours:
            broker_order_id = str(order_id)
        elif perm_id:
            broker_order_id = f"perm:{perm_id}"
        else:
            broker_order_id = f"client:{client_id}:order:{order_id}:exec:{base}"
        if account != cfg.expected_account_id:
            self._anomaly("UNEXPECTED_ACCOUNT", f"execution {exec_id} for account {self._mask(account)}")
        raw = {"provider": PROVIDER, "execId": exec_id, "time": execution.time, "time_source": time_source,
               "acctNumber": self._mask(account), "exchange": execution.exchange, "side": execution.side,
               "shares": str(execution.shares), "price": repr(execution.price), "permId": perm_id,
               "clientId": client_id, "orderId": order_id, "cumQty": str(execution.cumQty),
               "avgPrice": repr(execution.avgPrice), "orderRef": execution.orderRef,
               "lastLiquidity": execution.lastLiquidity, "price_provisional": bool(execution.pendingPriceRevision),
               "conId": contract.conId, "symbol": contract.symbol, "secType": contract.secType,
               "evidence_level": EVIDENCE_LEVEL, **link_raw}
        return ExecutionRow(
            exec_key=key, provider_exec_id=exec_id, client_order_id=alloc.client_order_id if alloc else None,
            order_ref=(alloc.order_ref if alloc and alloc.order_ref else None) or execution.orderRef or None,
            broker_order_id=broker_order_id, instrument_id=self._resolve_instrument(contract.conId, alloc),
            provider_ref=str(contract.conId), side=side, quantity=quantity, price=price, currency=contract.currency,
            exec_time_ns=exec_time_ns, correction_of=correction_of, raw=raw), ""

    def _correction_link(self, account: str, base: str, rev: int | None, width: int, key: str) -> tuple[str | None, dict]:
        """R5: exec_rev is the integer after the final '.'; a higher revision replaces the latest prior one."""
        if rev is None:
            return None, {}
        seen = self._revisions.setdefault((account, base), {})
        extra: dict = {}
        lower = [r for r in seen if r < rev]
        higher = [r for r in seen if r > rev]
        if lower:
            link = seen[max(lower)]
        elif rev > 1:
            # The earlier revision was never observed by this process (e.g. booked before a restart): link the
            # presumed previous id so the core applies a correction (or opens an orphan-correction case) instead
            # of booking a second, additive execution. R5 open question: are revisions always sequential?
            link = M.exec_key(account, f"{base}.{str(rev - 1).zfill(width)}")
            extra["correction_of_presumed"] = True
        else:
            link = None
        if higher:
            extra["superseded_by"] = seen[min(higher)]
            self._anomaly("OUT_OF_ORDER_REVISION", f"{key} arrived after a higher revision {extra['superseded_by']}")
        seen[rev] = key
        return link, extra

    def _on_execDetailsEnd(self, req_id) -> None:
        with self._cv:
            req = self._exec_reqs.get(req_id)
            if req is not None:
                req.end = True
            self._cv.notify_all()

    def _on_commissionAndFeesReport(self, report) -> None:
        with self._cv:
            data = {"execId": report.execId, "commissionAndFees": report.commissionAndFees,
                    "currency": report.currency, "realizedPNL": report.realizedPNL, "yield": report.yield_,
                    "yieldRedemptionDate": report.yieldRedemptionDate}
            if data["execId"] in self._exec_by_id:
                self._accept_commission(data)
            else:
                # R5: "A commission report arriving before its execution is held and joined when it arrives."
                self._pending_comm.setdefault(data["execId"], []).append(data)
            self._cv.notify_all()

    def _accept_commission(self, data: dict) -> None:
        row = self._exec_by_id[data["execId"]]
        amount = M.float_to_fixed(data["commissionAndFees"])
        if amount is None:
            self._anomaly("COMMISSION_UNSET", f"{row.exec_key}: commissionAndFees unset; awaiting a later report")
            return
        seen_key = (data["execId"], amount.to_str(), data["currency"])
        if seen_key in self._comm_seen:
            return
        self._comm_seen.add(seen_key)
        realized = M.float_to_fixed(data["realizedPNL"]) if isinstance(data["realizedPNL"], float) else None
        bond_yield = M.float_to_fixed(data["yield"]) if isinstance(data["yield"], float) else None
        commission = CommissionRow(exec_key=row.exec_key, amount=amount, currency=data["currency"], raw={
            "provider": PROVIDER, "execId": data["execId"], "commissionAndFees": repr(data["commissionAndFees"]),
            "currency": data["currency"], "realizedPNL": realized.to_str() if realized is not None else None,
            "yield": bond_yield.to_str() if bond_yield is not None else None,
            "yieldRedemptionDate": data["yieldRedemptionDate"] or None, "evidence_level": EVIDENCE_LEVEL})
        self._comm_log.append(commission)
        self._emit(CommissionEvent(commission))

    def _on_accountSummary(self, req_id, account, tag, value, currency) -> None:
        with self._cv:
            req = self._summary_reqs.get(req_id)
            if req is not None:
                req.rows.append((account, tag, value, currency))

    def _on_accountSummaryEnd(self, req_id) -> None:
        with self._cv:
            req = self._summary_reqs.get(req_id)
            if req is not None:
                req.end = True
            self._cv.notify_all()

    def _on_position(self, account, contract, position, avg_cost) -> None:
        with self._cv:
            if self._positions_collect is not None:
                self._positions_collect.append((account, contract.conId, contract.currency, contract.symbol,
                                                position, avg_cost))

    def _on_positionEnd(self) -> None:
        with self._cv:
            if self._positions_collect is not None:
                self._positions_end = True
            self._cv.notify_all()

    def _on_orderBound(self, perm_id, client_id, order_id) -> None:
        with self._cv:
            # Binding happens only for client 0 (R3); this adapter never binds, so any binding is noteworthy.
            self._anomaly("ORDER_BOUND", f"permId {perm_id} bound to client {client_id} order {order_id}")
