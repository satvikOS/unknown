"""Shared building blocks for handlers: exceptions, control rows, holds."""
from __future__ import annotations

from ...domain.fixed import Fixed, Rounding
from ...domain.money import currency
from .. import ledger, state
from ..desk import Context

# Exception kinds -----------------------------------------------------------
UNKNOWN_ORDER = "UNKNOWN_ORDER"
EXECUTION_ANOMALY = "EXECUTION_ANOMALY"
FOREIGN_ACTIVITY = "FOREIGN_ACTIVITY"
POSITION_MISMATCH = "POSITION_MISMATCH"
CASH_MISMATCH = "CASH_MISMATCH"
SETTLEMENT_LATE = "SETTLEMENT_LATE"
POST_COMPLETION_ADJUSTMENT = "POST_COMPLETION_ADJUSTMENT"
EXTERNAL_CASH_FLOW = "EXTERNAL_CASH_FLOW"
RECONCILIATION_INCOMPLETE = "RECONCILIATION_INCOMPLETE"
OVERCOMMITTED = "OVERCOMMITTED"
RESTORED_FROM_BACKUP = "RESTORED_FROM_BACKUP"


def new_exception(ctx: Context, account_key: str, kind: str, *, blocking: bool, severity: str, detail: dict,
                  key: str, session_id: str | None = None, order_id: str | None = None,
                  owner_hint: str | None = None) -> dict | None:
    """Return a new exception row, or None if an open case with the same key exists."""
    if state.exception_by_key(ctx.tx, account_key, key) is not None:
        return None
    return {"case_id": ctx.new_id("case-"), "account_key": account_key, "kind": kind, "blocking": blocking,
            "status": "OPEN", "severity": severity, "opened_at_ns": ctx.now_ns, "session_id": session_id,
            "order_id": order_id, "owner_hint": owner_hint, "detail": detail, "key": key, "resolution": None}


def control_update(ctx: Context, account_key: str, **fields) -> dict:
    row = state.control(ctx.tx, account_key)
    row.update(fields)
    return row


def entries_permitted(ctx: Context, account_key: str) -> tuple[bool, list[str]]:
    """Account-level gates for exposure-increasing activity (not instrument specific)."""
    c = state.control(ctx.tx, account_key)
    blockers = []
    if c["stop_latched"]:
        blockers.append(f"STOP_LATCHED:{c.get('latch_reason')}")
    if c["resume_requires_owner"]:
        blockers.append(f"RESUME_HOLD:{c.get('resume_reason')}")
    if c["needs_reconciliation"] or c["reconciled_epoch"] != ctx.epoch:
        blockers.append("RECONCILIATION_REQUIRED")
    if state.open_exceptions(ctx.tx, account_key, blocking_only=True):
        blockers.append("BLOCKING_EXCEPTIONS")
    r = state.risk_state(ctx.tx, account_key)
    if r.get("breach"):
        blockers.append(f"LOSS_BUDGET:{r['breach']}")
    if not r.get("valuation_ok", True):
        blockers.append("VALUATION_STALE")
    return (not blockers, blockers)


def cash_hold_amount(order: dict, multiplier: Fixed) -> Fixed:
    """Remaining buy commitment: ceil(leaves * limit * multiplier) + unused fee envelope (ADR-0006)."""
    ccy = order["currency"]
    scale = currency(ccy).ledger_scale
    leaves = Fixed.parse(order["leaves_qty"])
    limit = Fixed.parse(order["limit_price"])
    commitment = (leaves * limit * multiplier).rescale(scale, Rounding.CEILING)
    envelope = Fixed.parse(order["fee_envelope"])
    used = Fixed.parse(order["fees_estimated"])
    remaining_fee = envelope - used if envelope > used else Fixed(0, scale)
    return (commitment + remaining_fee).rescale(scale)


def order_owner(order: dict) -> str:
    """The ledger owner an order reserves for: its session's owner, or the owner fixed at acceptance for an owner
    disposal of residual inventory (protected:<id> or unallocated; ADR-0008, BR-LED-03)."""
    return order.get("owner") or ledger.session_owner(order["session_id"])


def hold_rows(order: dict, multiplier: Fixed, *, release: bool) -> list[dict]:
    """Recompute the order's hold from its current state (holds are a function of the order)."""
    owner = order_owner(order)
    if order["side"] == "BUY":
        amount = Fixed(0, currency(order["currency"]).ledger_scale) if release else cash_hold_amount(order, multiplier)
        return [{"hold_id": f"{order['order_id']}:cash", "order_id": order["order_id"], "account_key": order["account_key"],
                 "owner": owner, "kind": "CASH", "unit": order["currency"], "amount": amount.to_str(),
                 "status": "RELEASED" if release else "ACTIVE"}]
    amount = Fixed(0) if release else Fixed.parse(order["leaves_qty"])
    return [{"hold_id": f"{order['order_id']}:qty", "order_id": order["order_id"], "account_key": order["account_key"],
             "owner": owner, "kind": "QUANTITY", "unit": order["instrument_id"], "amount": amount.to_str(),
             "status": "RELEASED" if release else "ACTIVE"}]


def multiplier_of(ctx: Context, instrument_id: str) -> Fixed:
    inst = state.instrument(ctx.tx, instrument_id)
    return inst.multiplier if inst is not None else Fixed(1)
