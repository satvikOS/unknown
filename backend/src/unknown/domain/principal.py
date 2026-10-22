"""Authenticated principals and the role permission table (ADR-0007)."""
from __future__ import annotations

import enum
from dataclasses import dataclass

from .ids import validate_id


class Role(enum.Enum):
    OWNER = "OWNER"
    SYSTEM = "SYSTEM"
    STRATEGY = "STRATEGY"
    AGENT = "AGENT"


@dataclass(frozen=True, slots=True)
class Principal:
    """Identity supplied by the trusted caller (API layer or service), never by payload."""
    id: str
    role: Role
    authenticated_at_ns: int | None = None  # owner re-authentication time, for green start

    def __post_init__(self) -> None:
        validate_id(self.id, "principal id")
        if not isinstance(self.role, Role):
            raise TypeError("role must be Role")


OWNER_ONLY = frozenset({
    "register_account", "publish_policy", "activate_policy", "acquire_execution_ownership",
    "release_execution_ownership", "authorize_capital", "deauthorize_capital", "create_session",
    "cancel_session", "suspend_session", "resume_session", "end_session", "start", "resolve_exception",
    "accept_fee_estimate", "register_instrument", "amend_session_request", "submit_intent", "broker_admin_action",
    "record_strategy_decision",
})
SYSTEM_ONLY = frozenset({
    "process_started", "outbox_begin", "outbox_result", "broker_order_status", "broker_execution",
    "broker_commission", "broker_connectivity", "reconciliation_result", "valuation", "session_tick",
    "settlement_check",
})
STRATEGY_ALLOWED = frozenset({"submit_intent", "request_cancel", "record_strategy_decision"})
# SYSTEM never submits exposure-increasing intents; its wind-down sells go through create_order directly (ADR-0007).
SHARED_OWNER_SYSTEM = frozenset({"stop", "start_session", "request_cancel"})
AGENT_ALLOWED = frozenset({"submit_research_proposal"})


def permitted(role: Role, kind: str) -> bool:
    if role is Role.OWNER:
        return kind in OWNER_ONLY or kind in SHARED_OWNER_SYSTEM or kind in AGENT_ALLOWED
    if role is Role.SYSTEM:
        return kind in SYSTEM_ONLY or kind in SHARED_OWNER_SYSTEM
    if role is Role.STRATEGY:
        return kind in STRATEGY_ALLOWED
    if role is Role.AGENT:
        return kind in AGENT_ALLOWED
    return False
