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
    last_exec = ctx.tx.one("SELECT MAX(seq) AS s FROM events WHERE account_key=? AND type IN ('ExecutionRecorded', "
                           "'ExternalExecutionRecorded', 'ForeignExecutionRecorded', 'UnmappedExecutionRecorded', "
                           "'ExecutionCorrectionReversed', 'OrphanCorrectionRecorded')", (key,))["s"] or 0
    covered = bool(rec and rec["status"] == "CLEAN" and rec.get("epoch") == ctx.epoch
                   and rec.get("event_seq_at_start", 0) >= last_exec)
    return {
        "entries_blocked": state.control(ctx.tx, key)["stop_latched"],
        "open_buys": sum(1 for o in open_orders if o["side"] == "BUY"),
        "cancels_pending": sum(1 for o in open_orders if o["cancel_state"] in ("REQUESTED", "SENT", "ACKED", "UNKNOWN")),
        "unknown_orders": sum(1 for o in open_orders if o["state"] == "UNKNOWN"),
        "open_sells": sum(1 for o in open_orders if o["side"] == "SELL"),
        "residual_positions": positions,
        # "Flat" only from a clean reconciliation in this process that covers the last execution (book Ch. 22).
        "reconciled_flat": bool(covered and not positions and not open_orders),
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
                          "broker_server_time_ns": h.broker_server_time_ns, "disk_free_mb": h.disk_free_mb, "detail": h.detail,
                          "broker_account_mode": h.broker_account_mode}
    if not h.broker_connected:
        blockers.append("BROKER_DISCONNECTED")
    if not h.broker_authenticated:
        blockers.append("BROKER_AUTHENTICATION_REQUIRED")
    # Missing evidence never counts as a pass (fail closed).
    if h.broker_account_mode is None:
        blockers.append("BROKER_ACCOUNT_MODE_UNKNOWN")
    elif h.broker_account_mode != acct["mode"]:
        blockers.append(f"BROKER_ACCOUNT_MODE_MISMATCH:{h.broker_account_mode}")
    if h.broker_account_mode == Mode.LIVE.value:
        blockers.append("BROKER_REPORTS_LIVE_ACCOUNT")
    if h.broker_server_time_ns is None:
        blockers.append("CLOCK_SKEW_UNKNOWN")
    elif abs(h.broker_server_time_ns - ctx.now_ns) > policy.limits.max_clock_skew_ms * 1_000_000:
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
    elif action == "cover_shortfall":
        changes["journal"] = [_cover_shortfall(ctx, case, p)]
    elif action != "acknowledge":
        raise Rejected("INVALID_ACTION", action)
    elif case["kind"] == "OVERCOMMITTED" and _shortfall(ctx, case)[1].is_positive():
        raise Rejected("SHORTFALL_UNCOVERED", "an overcommitment is resolved by cover_shortfall, not by acknowledgement")
    changes["exceptions"] = [{**case, "status": "RESOLVED",
                              "resolution": {"by": ctx.principal.id, "at_ns": ctx.now_ns, "action": action, "note": note}}]
    c = state.control(ctx.tx, key)
    if action == "record_external_cash_flow":
        changes["control"] = [{**c, "needs_reconciliation": True}]
    ctx.emit("ExceptionResolved", {"case_id": case["case_id"], "kind": case["kind"], "action": action, "note": note}, changes,
             account_key=key, session_id=case.get("session_id"))
    # FAILED_REVIEW exit (ADR-0008). The rule lives with the session machine, which also applies it on every
    # tick so that a case the system resolves ends the review too (review finding 10). A case names its
    # session by id or only by owner (owner_hint session:<id>).
    from .sessions import exit_review_if_clear, sessions_of_case
    for session_id in sessions_of_case(case):
        if ctx.tx.one("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)):
            exit_review_if_clear(ctx, state.session(ctx.tx, session_id))
    return {"case_id": case["case_id"], "status": "RESOLVED"}


def _shortfall(ctx: Context, case: dict) -> tuple[str, Fixed, str]:
    """(owner holding the deficit, deficit, currency) for an OVERCOMMITTED case."""
    key = case["account_key"]
    ccy = case["detail"].get("currency") or state.account(ctx.tx, key)["base_currency"]
    for owner in (ledger.exception_owner(case["case_id"]), case.get("owner_hint")):
        if owner:
            avail = ledger.available_cash(ctx.tx, key, owner, ccy)
            if avail.is_negative():
                return owner, -avail, ccy
    return ledger.exception_owner(case["case_id"]), Fixed(0), ccy


def _cover_shortfall(ctx: Context, case: dict, p: dict) -> dict:
    """Fund an overcommitment from cash the owner chooses: unallocated, buffer or outside (ADR-0006)."""
    if case["kind"] != "OVERCOMMITTED":
        raise Rejected("INVALID_ACTION", "cover_shortfall resolves OVERCOMMITTED cases")
    source = p.get("from_owner", ledger.OUTSIDE)
    if source not in (ledger.UNALLOCATED, ledger.BUFFER, ledger.OUTSIDE):
        raise Rejected("INVALID_FIELD", "from_owner must be unallocated, buffer or outside")
    key = case["account_key"]
    target, need, ccy = _shortfall(ctx, case)
    if not need.is_positive():
        raise Rejected("NO_SHORTFALL", "nothing to cover; acknowledge the case instead")
    if ledger.available_cash(ctx.tx, key, source, ccy) < need:
        raise Rejected("INSUFFICIENT_FUNDS", f"{source} has less than {need} {ccy} available")
    return ledger.entry(ctx.new_id("je-"), key, "OWNER_TRANSFER", f"cover shortfall of {case['case_id']} from {source}",
                        ledger.transfer(ledger.CASH, ccy, need, source, target))


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
    if type(body["schema_version"]) is not int:
        raise Rejected("PROPOSAL_INVALID", "schema_version must be the integer 1")
    if not isinstance(body["job_id"], str) or not 1 <= len(body["job_id"]) <= 160:
        raise Rejected("PROPOSAL_INVALID", "job_id must be a string of 1..160 characters")
    if not isinstance(body["assumptions"], list) or len(body["assumptions"]) > 30 or not all(
            isinstance(a, str) and len(a) <= 500 for a in body["assumptions"]):
        raise Rejected("PROPOSAL_INVALID", "assumptions must be <= 30 strings of <= 500 chars")
    if not isinstance(body["reason"], str) or len(body["reason"]) > 2000:
        raise Rejected("PROPOSAL_INVALID", "reason must be a string of <= 2000 chars")
    pid = ctx.new_id("prop-")
    ctx.emit("ResearchProposalRecorded", {"proposal_id": pid, "author": ctx.principal.id,
                                          "note": "advisory only; cannot reserve, order or change policy"},
             {"proposals": [{"proposal_id": pid, "author": ctx.principal.id, "body": body, "recorded_at_ns": ctx.now_ns}]})
    return {"proposal_id": pid}
