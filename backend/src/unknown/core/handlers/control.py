"""Red stop, green preflight, exception resolution (ADR-0010; RSK-03..RSK-05)."""
from __future__ import annotations

import json

from ...domain.errors import Rejected
from ...domain.fixed import Fixed
from ...domain.money import Money
from ...domain.principal import Role
from ...policy.model import Mode
from .. import ledger, state
from ..desk import Context
from .common import control_update, entries_permitted


def stop(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    reason = str(p.get("reason") or ("OWNER_STOP" if ctx.principal.role is Role.OWNER else "SYSTEM_STOP"))
    return latch_stop(ctx, key, reason)


def latch_stop(ctx: Context, key: str, reason: str) -> dict:
    from .orders import cancel_order
    c = state.control(ctx.tx, key)
    first = not c["stop_latched"]
    new = dict(c)
    if first:
        new.update(stop_latched=True, latch_reason=reason, latched_by=ctx.principal.id, latched_at_ns=ctx.now_ns,
                   latch_command_id=ctx.command.command_id)
    ctx.emit("StopLatched" if first else "StopReasserted",
             {"reason": reason, "by": ctx.principal.id, "liquidation_policy": "NONE",
              "note": "new exposure blocked; open entries are being canceled; residual positions are not sold automatically"},
             {"control": [new]}, account_key=key)
    canceled = []
    for o in state.orders(ctx.tx, key, open_only=True):
        if o["side"] == "BUY":
            r = cancel_order(ctx, o, f"stop: {reason}")
            canceled.append({"order_id": o["order_id"], "state": r["state"]})
    return {"stop_latched": True, "first": first, "entries_canceled": canceled, "progress": stop_progress(ctx, key)}


def stop_progress(ctx: Context, key: str) -> dict:
    """What the terminal must show after red (book Ch. 22): never 'flat' from an empty order list alone."""
    open_orders = state.orders(ctx.tx, key, open_only=True)
    positions = {}
    for r in ctx.tx.all("SELECT owner, instrument_id, open_qty FROM lots WHERE account_key=?", (key,)):
        q = Fixed.parse(r["open_qty"])
        if not q.is_zero():
            positions.setdefault(r["owner"], {})
            positions[r["owner"]][r["instrument_id"]] = (Fixed.parse(positions[r["owner"]].get(r["instrument_id"], "0")) + q).to_str()
    rec = state.latest_reconciliation(ctx.tx, key)
    return {
        "entries_blocked": state.control(ctx.tx, key)["stop_latched"],
        "open_buys": sum(1 for o in open_orders if o["side"] == "BUY"),
        "cancels_pending": sum(1 for o in open_orders if o["cancel_state"] in ("REQUESTED", "SENT", "ACKED", "UNKNOWN")),
        "unknown_orders": sum(1 for o in open_orders if o["state"] == "UNKNOWN"),
        "open_sells": sum(1 for o in open_orders if o["side"] == "SELL"),
        "residual_positions": positions,
        "reconciled_flat": bool(rec and rec["status"] == "CLEAN" and not positions and not open_orders),
    }


def preflight(ctx: Context, key: str) -> tuple[list[str], dict]:
    blockers: list[str] = []
    acct = state.account(ctx.tx, key)
    evidence: dict = {"epoch": ctx.epoch}
    auth = ctx.principal.authenticated_at_ns
    try:
        policy = state.active_policy(ctx.tx, key)
    except Rejected:
        return ["NO_ACTIVE_POLICY"], evidence
    evidence["policy_hash"] = policy.policy_hash
    if ctx.principal.role is not Role.OWNER:
        blockers.append("OWNER_REQUIRED")
    if auth is None or ctx.now_ns - auth > policy.limits.reauth_max_age_s * 1_000_000_000:
        blockers.append("OWNER_REAUTHENTICATION_REQUIRED")
    if policy.mode.value != acct["mode"]:
        blockers.append("POLICY_MODE_MISMATCH")
    if acct["mode"] == Mode.LIVE.value:
        blockers.append("LIVE_NOT_AUTHORIZED: deployment record gates unresolved (GATE-01)")
    own = state.ownership(ctx.tx, key)
    if not (own and own["status"] == "HELD" and own["deployment_id"] == ctx.desk.deployment_id):
        blockers.append("EXECUTION_OWNERSHIP_NOT_HELD")
    h = ctx.desk.health()
    evidence["health"] = {"broker_connected": h.broker_connected, "broker_authenticated": h.broker_authenticated,
                          "broker_server_time_ns": h.broker_server_time_ns, "disk_free_mb": h.disk_free_mb, "detail": h.detail}
    if not h.broker_connected:
        blockers.append("BROKER_DISCONNECTED")
    if not h.broker_authenticated:
        blockers.append("BROKER_AUTHENTICATION_REQUIRED")
    if h.broker_server_time_ns is not None and abs(h.broker_server_time_ns - ctx.now_ns) > policy.limits.max_clock_skew_ms * 1_000_000:
        blockers.append("CLOCK_SKEW")
    if h.disk_free_mb < policy.limits.min_free_disk_mb:
        blockers.append("DISK_SPACE")
    c = state.control(ctx.tx, key)
    rec = state.latest_reconciliation(ctx.tx, key)
    evidence["reconciliation"] = rec["run_id"] if rec else None
    if c["needs_reconciliation"] or c["reconciled_epoch"] != ctx.epoch or rec is None or rec["status"] != "CLEAN":
        blockers.append("RECONCILIATION_REQUIRED")
    elif ctx.now_ns - rec["completed_at_ns"] > policy.limits.max_reconciliation_age_s * 1_000_000_000:
        blockers.append("RECONCILIATION_STALE")
    open_blocking = state.open_exceptions(ctx.tx, key, blocking_only=True)
    if open_blocking:
        blockers.append("BLOCKING_EXCEPTIONS:" + ",".join(sorted({e["kind"] for e in open_blocking})))
    if any(o["state"] == "UNKNOWN" for o in state.orders(ctx.tx, key, open_only=True)):
        blockers.append("UNKNOWN_ORDERS")
    r = state.risk_state(ctx.tx, key)
    if r.get("breach"):
        blockers.append(f"LOSS_BUDGET:{r['breach']}")
    if not r.get("valuation_ok", True):
        blockers.append("VALUATION_STALE")
    for s in state.sessions(ctx.tx, key, ("RUNNING", "WINDING_DOWN", "SUSPENDED")):
        for lot in ledger.owner_lots(ctx.tx, key, ledger.session_owner(s["session_id"])):
            q = ctx.desk.quotes.get(lot["instrument_id"])
            if q is None or q.age_ns(ctx.now_ns) > policy.limits.quote_max_age_ms * 1_000_000:
                blockers.append(f"MARKET_DATA_STALE:{lot['instrument_id']}")
    return sorted(set(blockers)), evidence


def start(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    blockers, evidence = preflight(ctx, key)
    if blockers:
        raise Rejected("PREFLIGHT_FAILED", "; ".join(blockers), blockers=blockers, evidence=evidence)
    c = state.control(ctx.tx, key)
    new = {**c, "stop_latched": False, "latch_reason": None, "resume_requires_owner": False, "resume_reason": None,
           "last_start": {"by": ctx.principal.id, "at_ns": ctx.now_ns, "command_id": ctx.command.command_id, "evidence": evidence}}
    ctx.emit("StartGranted", {"evidence": evidence, "cleared_latch_reason": c.get("latch_reason")}, {"control": [new]}, account_key=key)
    from .sessions import retry_waiting_funding
    retry_waiting_funding(ctx, key)
    return {"started": True, "evidence": evidence}


def resolve_exception(ctx: Context, p: dict) -> dict:
    r = ctx.tx.one("SELECT row FROM exceptions WHERE case_id=?", (p.get("case_id", ""),))
    if r is None:
        raise Rejected("UNKNOWN_CASE", str(p.get("case_id")))
    case = json.loads(r["row"])
    if case["status"] != "OPEN":
        return {"case_id": case["case_id"], "status": case["status"], "noop": True}
    action = p.get("action", "acknowledge")
    note = str(p.get("note", ""))
    if not note:
        raise Rejected("NOTE_REQUIRED", "resolution requires an explanatory note")
    changes: dict = {}
    key = case["account_key"]
    if action == "record_external_cash_flow":
        if case["kind"] != "CASH_MISMATCH":
            raise Rejected("INVALID_ACTION", "external cash flows resolve CASH_MISMATCH cases")
        try:
            amount = Money(p["currency"], Fixed.parse(p["amount"])).at_ledger_scale()
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise Rejected("INVALID_AMOUNT", str(exc)) from None
        changes["journal"] = [ledger.entry(ctx.new_id("je-"), key, "EXTERNAL_FLOW", f"owner-attributed external cash flow: {note}",
                                           [ledger.Line(ledger.CASH, ledger.OUTSIDE, amount.currency, amount.amount),
                                            ledger.Line(ledger.CONTRIBUTED, ledger.OUTSIDE, amount.currency, -amount.amount)])]
        if amount.is_negative() and ledger.available_cash(ctx.tx, key, ledger.OUTSIDE, amount.currency) + amount.amount < 0:
            raise Rejected("WITHDRAWAL_EXCEEDS_OUTSIDE",
                           "withdrawal larger than unauthorized cash: deauthorize capital first or record a loss of authorized capital")
    elif action != "acknowledge":
        raise Rejected("INVALID_ACTION", action)
    changes["exceptions"] = [{**case, "status": "RESOLVED",
                              "resolution": {"by": ctx.principal.id, "at_ns": ctx.now_ns, "action": action, "note": note}}]
    c = state.control(ctx.tx, key)
    if action == "record_external_cash_flow":
        changes["control"] = [{**c, "needs_reconciliation": True}]
    ctx.emit("ExceptionResolved", {"case_id": case["case_id"], "kind": case["kind"], "action": action, "note": note}, changes,
             account_key=key, session_id=case.get("session_id"))
    if case.get("session_id"):
        s = state.session(ctx.tx, case["session_id"])
        others = [e for e in state.open_exceptions(ctx.tx, key, blocking_only=True) if e.get("session_id") == s["session_id"]]
        if s["state"] == "FAILED_REVIEW" and not others:
            back = s["review_return_state"] or "SUSPENDED"
            back = "SUSPENDED" if back == "RUNNING" else back
            ctx.emit("SessionReviewResolved", {"returns_to": back},
                     {"sessions": [{**s, "state": back, "review_return_state": None}]}, account_key=key, session_id=s["session_id"])
    return {"case_id": case["case_id"], "status": "RESOLVED"}


def accept_fee_estimate(ctx: Context, p: dict) -> dict:
    ex = state.execution(ctx.tx, p.get("exec_key", ""))
    if ex is None:
        raise Rejected("UNKNOWN_EXECUTION", str(p.get("exec_key")))
    if ex["fee_status"] != "ESTIMATED":
        return {"fee_status": ex["fee_status"], "noop": True}
    ctx.emit("FeeEstimateAccepted", {"exec_key": ex["exec_key"], "estimate": ex["fee_estimate"],
                                     "note": "owner accepted the estimate for completion; a later broker report posts as an adjustment"},
             {"executions": [{**ex, "fee_status": "ACCEPTED_ESTIMATE"}]}, account_key=ex["account_key"], session_id=ex.get("session_id"))
    return {"fee_status": "ACCEPTED_ESTIMATE"}


def submit_research_proposal(ctx: Context, p: dict) -> dict:
    """Agents' only write path: a typed, non-executable proposal (contracts/research_proposal.schema.json)."""
    required = {"schema_version", "job_id", "hypothesis", "evidence", "horizon_seconds", "assumptions", "abstain", "reason"}
    body = p.get("proposal")
    if not isinstance(body, dict) or set(body) != required or body.get("schema_version") != 1:
        raise Rejected("PROPOSAL_INVALID", "proposal must match research_proposal schema version 1 exactly")
    if not isinstance(body["hypothesis"], str) or len(body["hypothesis"]) > 4000 or not isinstance(body["abstain"], bool):
        raise Rejected("PROPOSAL_INVALID", "hypothesis/abstain invalid")
    if not isinstance(body["evidence"], list) or len(body["evidence"]) > 50 or not all(isinstance(e, str) and len(e) <= 500 for e in body["evidence"]):
        raise Rejected("PROPOSAL_INVALID", "evidence must be <= 50 strings of <= 500 chars")
    if type(body["horizon_seconds"]) is not int or not 1 <= body["horizon_seconds"] <= 31_536_000:
        raise Rejected("PROPOSAL_INVALID", "horizon_seconds out of range")
    pid = ctx.new_id("prop-")
    ctx.emit("ResearchProposalRecorded", {"proposal_id": pid, "author": ctx.principal.id,
                                          "note": "advisory only; cannot reserve, order or change policy"},
             {"proposals": [{"proposal_id": pid, "author": ctx.principal.id, "body": body, "recorded_at_ns": ctx.now_ns}]})
    return {"proposal_id": pid}
