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
    # The core service records what it did while the database could not record a stop (BR-SES-13): the error, the
    # stop commands the in-memory latch stood in for and the cancels it sent to the broker directly. Only SYSTEM
    # (the service itself) carries this evidence.
    evidence = p.get("memory_latch") if ctx.principal.role is Role.SYSTEM and isinstance(p.get("memory_latch"), dict) else None
    return latch_stop(ctx, key, reason, evidence=evidence)


def latch_stop(ctx: Context, key: str, reason: str, *, evidence: dict | None = None) -> dict:
    from .orders import cancel_order
    c = state.control(ctx.tx, key)
    first = not c["stop_latched"]
    new = dict(c)
    if first:
        new.update(stop_latched=True, latch_reason=reason, latched_by=ctx.principal.id, latched_at_ns=ctx.now_ns,
                   latch_command_id=ctx.command.command_id)
    info = {"reason": reason, "by": ctx.principal.id, "liquidation_policy": "NONE",
            "note": "new exposure blocked; open entries are being canceled; residual positions are not sold automatically"}
    if evidence is not None:
        info["memory_latch"] = evidence
    ctx.emit("StopLatched" if first else "StopReasserted", info, {"control": [new]}, account_key=key)
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
    open_orders = state.orders(ctx.tx, key, open_only=True)
    if any(o["state"] == "UNKNOWN" for o in open_orders):
        blockers.append("UNKNOWN_ORDERS")
    if any(o["cancel_state"] == "UNKNOWN" for o in open_orders):
        # The order may still be working at the broker (review finding 35): look it up and re-cancel first.
        blockers.append("UNRESOLVED_CANCELS")
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
    elif action == "close_unknown_order":
        extra = _close_unknown_order(ctx, case, p, note)
    elif action != "acknowledge":
        raise Rejected("INVALID_ACTION", action)
    elif case["kind"] == "OVERCOMMITTED" and _shortfall(ctx, case)[1].is_positive():
        raise Rejected("SHORTFALL_UNCOVERED", "an overcommitment is resolved by cover_shortfall, not by acknowledgement")
    changes["exceptions"] = [{**case, "status": "RESOLVED",
                              "resolution": {"by": ctx.principal.id, "at_ns": ctx.now_ns, "action": action, "note": note,
                                             **(extra if action == "close_unknown_order" else {})}}]
    c = state.control(ctx.tx, key)
    if action in ("record_external_cash_flow", "close_unknown_order"):
        # The account's position must be confirmed against the broker again before new exposure.
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
    if action == "close_unknown_order":
        return {"case_id": case["case_id"], "status": "RESOLVED",
                "closed": {"order_id": extra["order_id"], "state": extra["order_state"]}}
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


CLOSE_EVIDENCE_KINDS = ("BROKER_STATEMENT", "BROKER_CONFIRM", "SUPPORT_REFERENCE")
# Reconciliation differences that a stuck UNKNOWN order itself causes (and that therefore cannot be required absent
# to close it): the broker cannot find it, or finds no trace of an order that had fills.
_UNKNOWN_ORDER_DIFFERENCES = ("ORDER_UNRESOLVED", "ORDER_MISSING_AT_BROKER")
_NS = 1_000_000_000


def _close_unknown_order(ctx: Context, case: dict, p: dict, note: str) -> dict:
    """Close a stuck UNKNOWN order on attached broker evidence (BR-ORD-03; ADR-0009 unknown outcomes).

    The owner's exit for an order whose outcome the broker cannot report (e.g. an IBKR order transmitted with no trace
    in open orders, completed orders or executions, so every lookup answers "cannot determine"). It never releases a
    hold the broker may still be working: the account's latest reconciliation must be complete, recorded in this
    process, younger than ``max_reconciliation_age_s`` and include the order's last material change (any change but
    cancel bookkeeping, including a held report the broker repeats after a run); the broker's open orders in it must
    not include the order and its lookup must not have found it; and its only blocking differences may be those of unresolved UNKNOWN orders (an UNKNOWN order the
    broker cannot find always leaves the run
    DIFFERENCES, so a literally clean run never contains one, and matching positions and cash in that run are what
    show no fill is missing). Fresh owner authentication and evidence (a statement, confirm or support reference)
    are required. The order ends REJECTED (no fills) or CANCELED (fills: the remainder is not working), every hold
    is released, the related cases are resolved with the evidence and the account must reconcile again."""
    from .broker import finalize
    key = case["account_key"]
    if case["kind"] not in ("UNKNOWN_ORDER", "RECONCILIATION_DIFFERENCE"):
        raise Rejected("INVALID_ACTION", "close_unknown_order resolves an UNKNOWN_ORDER case, or the reconciliation "
                                         "difference that names the order")
    order_id = case.get("order_id") or (case.get("detail") or {}).get("order_id")
    order = state.order(ctx.tx, order_id) if isinstance(order_id, str) else None
    if order is None or order["account_key"] != key:
        raise Rejected("INVALID_ACTION", "the case names no order of this account")
    if order["state"] != "UNKNOWN":
        raise Rejected("ORDER_NOT_UNKNOWN", f"order {order_id} is {order['state']}: only an UNKNOWN order is closed on evidence")
    policy = state.active_policy(ctx.tx, key)
    auth = ctx.principal.authenticated_at_ns
    if auth is None or ctx.now_ns - auth > policy.limits.reauth_max_age_s * _NS:
        raise Rejected("OWNER_REAUTHENTICATION_REQUIRED", "re-authenticate before closing an order on evidence")
    evidence = p.get("evidence")
    reference = evidence.get("reference") if isinstance(evidence, dict) else None
    if not isinstance(evidence, dict) or evidence.get("kind") not in CLOSE_EVIDENCE_KINDS \
            or not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
        raise Rejected("EVIDENCE_REQUIRED", "attach broker evidence: {kind: " + " | ".join(CLOSE_EVIDENCE_KINDS)
                       + ", reference: the statement, confirm or support reference (1..200 characters)}")
    evidence = {"kind": evidence["kind"], "reference": reference.strip()}
    rec = state.latest_reconciliation(ctx.tx, key)
    lookup = (rec or {}).get("lookups", {}).get(order_id)
    problems: list[str] = []
    if rec is None:
        problems.append("NO_RECONCILIATION")
    else:
        last_change = _last_material_change(ctx, order_id)
        if rec["status"] == "INCOMPLETE":
            problems.append("RECONCILIATION_INCOMPLETE")
        if rec.get("epoch") != ctx.epoch:
            problems.append("RECONCILIATION_NOT_IN_THIS_PROCESS")
        if ctx.now_ns - rec["completed_at_ns"] > policy.limits.max_reconciliation_age_s * _NS:
            problems.append("RECONCILIATION_STALE")
        if rec.get("event_seq_at_completion", rec.get("event_seq_at_start", 0)) < last_change:
            problems.append("RECONCILIATION_PREDATES_THE_ORDERS_LAST_CHANGE")
        if "open_order_ids" not in rec:
            problems.append("RECONCILIATION_WITHOUT_ORDER_EVIDENCE")
        elif order_id in rec["open_order_ids"]:
            problems.append("BROKER_LISTS_THE_ORDER_AS_OPEN")
        if lookup is None:
            problems.append("ORDER_NOT_LOOKED_UP")
        elif lookup.get("found") is True:
            problems.append(f"BROKER_LOOKUP_FINDS_THE_ORDER:{lookup.get('status')}")
        others = sorted({d["kind"] for d in rec.get("differences", []) if d.get("blocking") and not (
            d["kind"] in _UNKNOWN_ORDER_DIFFERENCES and _is_unknown(ctx, (d.get("detail") or {}).get("order_id")))})
        if others:
            problems.append("RECONCILIATION_DIFFERENCES:" + ",".join(others))
    if problems:
        raise Rejected("CLOSE_NOT_PROVEN", "; ".join(problems), problems=problems,
                       reconciliation=rec["run_id"] if rec else None)
    target = "REJECTED" if Fixed.parse(order["cum_qty"]).is_zero() else "CANCELED"
    reason = f"closed by the owner on broker evidence ({evidence['kind']} {evidence['reference']})"
    resolution = {"by": ctx.principal.id, "at_ns": ctx.now_ns, "action": "close_unknown_order", "note": note,
                  "evidence": evidence, "reconciliation": rec["run_id"]}
    related = []
    for k in (f"unknown-order:{order_id}",) + tuple(f"recon:{kind}:{order_id}" for kind in _UNKNOWN_ORDER_DIFFERENCES):
        other = state.exception_by_key(ctx.tx, key, k)
        if other is not None and other["case_id"] != case["case_id"]:
            related.append({**other, "status": "RESOLVED", "resolution": resolution})
    finalize(ctx, order, target, reason, extra_exceptions=related, via="owner_evidence")
    out = [{**b, "state": "DONE", "result": {"status": "OWNER_CLOSED", "evidence": evidence, "at_ns": ctx.now_ns}}
           for b in state.outbox_for_order(ctx.tx, order_id) if b["state"] not in ("DONE", "SKIPPED")]
    ctx.emit("UnknownOrderClosedByOwner", {"order_id": order_id, "state": target, "evidence": evidence, "note": note,
                                           "reconciliation": rec["run_id"], "lookup": lookup,
                                           "rule": "absent from the broker's open orders and lookups in a complete, fresh "
                                                   "reconciliation that includes the order's last material change"},
             {"outbox": out}, account_key=key, session_id=order["session_id"], order_id=order_id)
    return {"evidence": evidence, "reconciliation": rec["run_id"], "order_id": order_id, "order_state": target}


_CANCEL_BOOKKEEPING = ("cancel_state", "reason")


def _last_material_change(ctx: Context, order_id: str) -> int:
    """Sequence of the last event that changed the order other than by cancel bookkeeping: its state, quantities,
    broker id, or a broker report recorded on it (a held/Inactive report, which can arrive with nothing else
    changing). Cancel bookkeeping (a cancel queued, sent or refused: ``cancel_state`` and its ``reason``) does not
    count: a reconciliation that fails re-queues a cancel for every open buy, which would otherwise make every run
    look stale."""
    last, seen = 0, None
    for r in ctx.tx.all("SELECT seq, payload FROM events WHERE order_id=? ORDER BY seq", (order_id,)):
        for row in json.loads(r["payload"])["changes"].get("orders", []):
            if row.get("order_id") == order_id:
                material = json.dumps({k: v for k, v in row.items() if k not in _CANCEL_BOOKKEEPING}, sort_keys=True)
                if material != seen:
                    last, seen = r["seq"], material
    return last


def _is_unknown(ctx: Context, order_id: object) -> bool:
    o = state.order(ctx.tx, order_id) if isinstance(order_id, str) else None
    return o is not None and o["state"] == "UNKNOWN"


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
