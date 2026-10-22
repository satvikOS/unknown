"""Strategy decision records (ADR-0017, book algorithm A15).

A strategy runner records every evaluated decision, including "no trade" and
"blocked", so restraint and missed opportunities are auditable; shadow mode
uses the same record without submitting intents. The record has no economic
effect and grants nothing: intents still go through submit_intent and the
common risk pipeline."""
from __future__ import annotations

import re

from ...domain.errors import Rejected
from ...domain.fixed import Fixed
from ...domain.ids import validate_id
from ...domain.principal import Role
from .. import state
from ..desk import Context

OUTCOMES = ("NO_TRADE", "INTENT_SUBMITTED", "BLOCKED", "SKIPPED_STALE_DATA")
MODES = ("live", "shadow")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


def _decimal(p: dict, name: str, *, required: bool = True) -> str | None:
    v = p.get(name)
    if v is None and not required:
        return None
    if not isinstance(v, str):
        raise Rejected("INVALID_FIELD", f"{name} must be a decimal string")
    try:
        return Fixed.parse(v).to_str()
    except ValueError as exc:
        raise Rejected("INVALID_FIELD", f"{name}: {exc}") from None


def record_strategy_decision(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    state.account(ctx.tx, key)
    artifact = validate_id(p.get("artifact_id"), "artifact id")
    if ctx.principal.role is Role.STRATEGY and artifact != ctx.principal.id:
        raise Rejected("STRATEGY_IDENTITY_MISMATCH", "artifact_id must equal the authenticated strategy principal")
    session_id = p.get("session_id")
    session = state.session(ctx.tx, session_id)
    if session["account_key"] != key:
        raise Rejected("SESSION_ACCOUNT_MISMATCH", str(session_id))
    mandate = state.mandate(ctx.tx, session["mandate_hash"])
    if ctx.principal.role is Role.STRATEGY and artifact not in mandate.strategy_eligibility:
        raise Rejected("STRATEGY_NOT_PERMITTED", "artifact is not in the session mandate's strategy eligibility")
    mode = p.get("mode", "live")
    if mode not in MODES:
        raise Rejected("INVALID_FIELD", f"mode must be one of {', '.join(MODES)}")
    outcome = p.get("outcome")
    if outcome not in OUTCOMES:
        raise Rejected("INVALID_FIELD", f"outcome must be one of {', '.join(OUTCOMES)}")
    if mode == "shadow" and outcome == "INTENT_SUBMITTED":
        raise Rejected("INVALID_FIELD", "a shadow decision never submits intents")
    digest = p.get("inputs_digest")
    if not isinstance(digest, str) or not _HEX64.match(digest):
        raise Rejected("INVALID_FIELD", "inputs_digest must be a lowercase SHA-256 hex digest")
    observed = p.get("observation_time_ns")
    if type(observed) is not int or observed > ctx.now_ns:
        raise Rejected("INVALID_FIELD", "observation_time_ns must be an integer not in the future")
    reason = p.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 1000:
        raise Rejected("INVALID_FIELD", "reason must be a string of at most 1000 characters")
    order_ids = p.get("order_ids", [])
    if not isinstance(order_ids, list) or len(order_ids) > 50:
        raise Rejected("INVALID_FIELD", "order_ids must be a list of at most 50 ids")
    order_ids = [validate_id(o, "order id") for o in order_ids]
    if outcome == "INTENT_SUBMITTED" and not order_ids:
        raise Rejected("INVALID_FIELD", "INTENT_SUBMITTED names the order ids it submitted")
    for oid in order_ids:
        o = state.order(ctx.tx, oid)
        if o is None or o["session_id"] != session_id or (ctx.principal.role is Role.STRATEGY and (
                o["principal_id"], o.get("principal_role")) != (ctx.principal.id, Role.STRATEGY.value)):
            raise Rejected("UNKNOWN_ORDER", f"{oid} is not an order of this strategy in this session")
    instrument_id = p.get("instrument_id")
    if instrument_id is not None and (not isinstance(instrument_id, str)
                                      or state.instrument(ctx.tx, validate_id(instrument_id, "instrument id")) is None):
        raise Rejected("INVALID_FIELD", "instrument_id must name a registered instrument")
    record = {"artifact_id": artifact, "mode": mode, "outcome": outcome, "inputs_digest": digest,
              "observation_time_ns": observed, "instrument_id": instrument_id,
              "score": _decimal(p, "score", required=False), "target_quantity": _decimal(p, "target_quantity", required=False),
              "current_quantity": _decimal(p, "current_quantity", required=False), "reason": reason, "order_ids": order_ids}
    ctx.emit("StrategyDecisionRecorded", record, account_key=key, session_id=session_id, instrument_id=instrument_id,
             event_time_ns=observed)
    return {"recorded": True}
