"""Interface specification only. No provider SDK or live implementation."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Money:
    currency: str
    units: int
    scale: int


@dataclass(frozen=True)
class OrderIntent:
    command_id: str
    session_id: str
    instrument_id: str
    policy_hash: str
    side: str
    quantity_units: int
    quantity_scale: int
    limit_price_units: int
    price_scale: int
    deadline_utc_ns: int


@dataclass(frozen=True)
class SubmissionResult:
    status: str  # ACCEPTED, REJECTED or UNKNOWN; never infer acceptance from timeout
    broker_order_id: str | None
    raw_event_id: str


class BrokerPort(Protocol):
    async def capabilities(self) -> dict: ...
    async def account_snapshot(self) -> dict: ...
    async def instrument(self, instrument_id: str) -> dict: ...
    async def executions_since(self, watermark: str | None) -> dict: ...
    async def open_orders(self) -> dict: ...
    async def submit(self, intent: OrderIntent) -> SubmissionResult: ...
    async def cancel(self, broker_order_id: str, command_id: str) -> SubmissionResult: ...
    async def lookup(self, command_id: str) -> SubmissionResult: ...
