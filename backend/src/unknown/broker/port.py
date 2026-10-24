"""Typed broker adapter contract (ADR-0012, BRK-01).

Every capability that an adapter does not positively establish is disabled.
Results carry completeness so a partial page is never mistaken for the whole
account. A submission timeout is ``UNKNOWN``, never ``REJECTED`` or
``ACCEPTED``. Broker-specific detail is preserved in ``extensions``/``raw``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, Sequence, Union

from ..domain.fixed import Fixed


class AccountMode(enum.Enum):
    SIMULATED = "SIMULATED"
    PAPER = "PAPER"
    LIVE = "LIVE"


class SubmitStatus(enum.Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class BrokerOrderStatus(enum.Enum):
    """Normalized broker-side order status. Provider statuses map onto these."""
    PENDING_SUBMIT = "PENDING_SUBMIT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    INACTIVE = "INACTIVE"


# INACTIVE is not terminal: the broker holds the order without saying it is final (IBKR Inactive), so the core keeps
# every reservation and looks it up (core/order_machine.py, decision 2026-10-22).
TERMINAL_BROKER_STATUSES = frozenset({BrokerOrderStatus.FILLED, BrokerOrderStatus.CANCELED,
                                      BrokerOrderStatus.EXPIRED, BrokerOrderStatus.REJECTED})


@dataclass(frozen=True)
class PacingLimit:
    name: str
    max_requests: int
    per_seconds: int
    source: str          # documentation URL or "unverified"


@dataclass(frozen=True)
class BrokerCapabilities:
    provider: str
    account_mode: AccountMode
    base_currency: str
    legal_entity: str | None = None
    currencies: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()                 # venue MICs the account may trade, if established
    order_types: tuple[str, ...] = ()
    time_in_force: tuple[str, ...] = ()
    outside_regular_hours: bool = False
    fractional_quantities: bool = False
    short_selling: bool = False
    margin: bool = False
    atomic_replace: bool = False
    order_reference_max_length: int = 0          # 0 = cannot carry our client order id
    execution_history_days: int = 0
    execution_corrections: str = "unknown"       # e.g. "new execution id with suffix; original retained"
    settlement_evidence: str = "unknown"         # how settled cash is exposed
    pacing: tuple[PacingLimit, ...] = ()
    extensions: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerSessionState:
    connected: bool
    authenticated: bool
    auth_required_reason: str | None
    server_time_ns: int | None
    last_heartbeat_ns: int | None
    detail: str = ""


@dataclass(frozen=True)
class CashBalance:
    currency: str
    settled: Fixed      # broker-confirmed settled cash (not buying power)
    total: Fixed        # settled plus unsettled cash as the broker reports it
    semantics: str      # adapter documentation of the source fields
    # None: settled is the broker's own settled-cash field. Otherwise the adapter DERIVED settled from
    # another field and names the source and the evidence (ADR-0012 amendment 2026-10-24).
    settled_source: str | None = None


@dataclass(frozen=True)
class PositionRow:
    instrument_id: str | None     # resolved through provider mappings; None if unmapped
    provider_ref: str             # provider instrument identifier (e.g. IBKR conId)
    quantity: Fixed
    average_cost: Fixed | None
    currency: str


@dataclass(frozen=True)
class AccountSnapshot:
    as_of_ns: int
    cash: tuple[CashBalance, ...]
    positions: tuple[PositionRow, ...]
    complete: bool
    server_time_ns: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    order_ref: str                 # "<deployment>:<epoch>:<client order id>" (ADR-0011)
    instrument_id: str
    provider_ref: str
    side: str                      # BUY | SELL
    quantity: Fixed
    limit_price: Fixed
    currency: str
    time_in_force: str
    outside_regular_hours: bool = False


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    broker_order_id: str | None = None
    reason: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LookupResult:
    found: bool | None             # None = could not determine
    status: BrokerOrderStatus | None = None
    broker_order_id: str | None = None
    filled_quantity: Fixed | None = None
    remaining_quantity: Fixed | None = None
    reason: str = ""


@dataclass(frozen=True)
class OpenOrderRow:
    client_order_id: str | None
    order_ref: str | None
    broker_order_id: str
    instrument_id: str | None
    side: str
    status: BrokerOrderStatus
    filled_quantity: Fixed
    remaining_quantity: Fixed


@dataclass(frozen=True)
class ExecutionRow:
    exec_key: str                  # provider-scoped stable identity, e.g. "ibkr:<acct>:<execId>"
    provider_exec_id: str
    client_order_id: str | None
    order_ref: str | None
    broker_order_id: str | None
    instrument_id: str | None
    provider_ref: str
    side: str
    quantity: Fixed
    price: Fixed
    currency: str
    exec_time_ns: int
    correction_of: str | None = None   # exec_key of the execution this one corrects
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CommissionRow:
    exec_key: str
    amount: Fixed                  # positive = charge, negative = rebate
    currency: str
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionBatch:
    executions: tuple[ExecutionRow, ...]
    commissions: tuple[CommissionRow, ...]
    watermark: str | None
    complete: bool


@dataclass(frozen=True)
class OrderStatusEvent:
    client_order_id: str | None
    order_ref: str | None
    broker_order_id: str
    status: BrokerOrderStatus
    filled_quantity: Fixed
    remaining_quantity: Fixed
    reason: str = ""
    event_time_ns: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionEvent:
    execution: ExecutionRow


@dataclass(frozen=True)
class CommissionEvent:
    commission: CommissionRow


@dataclass(frozen=True)
class ConnectivityEvent:
    connected: bool
    authenticated: bool
    detail: str
    event_time_ns: int | None = None


BrokerEvent = Union[OrderStatusEvent, ExecutionEvent, CommissionEvent, ConnectivityEvent]


class BrokerPort(Protocol):
    def capabilities(self) -> BrokerCapabilities: ...
    def session(self) -> BrokerSessionState: ...
    def account_snapshot(self) -> AccountSnapshot: ...
    def open_orders(self) -> tuple[Sequence[OpenOrderRow], bool]: ...
    def executions_since(self, watermark: str | None) -> ExecutionBatch: ...
    def submit(self, order: BrokerOrder) -> SubmitResult: ...
    def cancel(self, client_order_id: str, broker_order_id: str | None) -> SubmitResult: ...
    def lookup(self, client_order_id: str) -> LookupResult: ...
    def drain_events(self, limit: int = 1000) -> list[BrokerEvent]: ...
