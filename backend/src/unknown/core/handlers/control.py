"""Red stop, green preflight, exception resolution (ADR-0010; RSK-03..RSK-05)."""
from __future__ import annotations

import json
import re

from ...domain.errors import Rejected
from ...domain.fixed import Fixed
from ...domain.ids import InvalidIdentifier, validate_id
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
    """Agents' only write path: a typed, non-executable proposal (contracts/research_proposal.schema.json).

    Version 1 (free-text evidence) stays accepted unchanged, so a proposal already waiting in a worker's
    outbox is still recorded. Version 2 is validated strictly (``_validate_proposal_v2``) and recorded with a
    core-assigned label. Neither version reserves, orders or changes policy: the only change is one
    ``proposals`` row."""
    body = p.get("proposal")
    if isinstance(body, dict) and type(body.get("schema_version")) is int and body["schema_version"] == 2:
        label = _validate_proposal_v2(ctx, body)
        pid = ctx.new_id("prop-")
        ctx.emit("ResearchProposalRecorded", {"proposal_id": pid, "author": ctx.principal.id, "schema_version": 2, "label": label,
                                              "note": "advisory only; cannot reserve, order or change policy"},
                 {"proposals": [{"proposal_id": pid, "author": ctx.principal.id, "body": body, "recorded_at_ns": ctx.now_ns,
                                 "schema_version": 2, "label": label}]})
        return {"proposal_id": pid, "schema_version": 2, "label": label}
    _validate_proposal_v1(body)
    pid = ctx.new_id("prop-")
    ctx.emit("ResearchProposalRecorded", {"proposal_id": pid, "author": ctx.principal.id,
                                          "note": "advisory only; cannot reserve, order or change policy"},
             {"proposals": [{"proposal_id": pid, "author": ctx.principal.id, "body": body, "recorded_at_ns": ctx.now_ns}]})
    return {"proposal_id": pid}


def _validate_proposal_v1(body: object) -> None:
    required = {"schema_version", "job_id", "hypothesis", "evidence", "horizon_seconds", "assumptions", "abstain", "reason"}
    if not isinstance(body, dict) or set(body) != required or body.get("schema_version") != 1:
        raise Rejected("PROPOSAL_INVALID", "proposal must match research_proposal schema version 1 or 2 exactly")
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


# ----------------------------------------------------------------- research proposal v2 (finding BR-DAT-12)
PROPOSAL_V2_KEYS = frozenset({"schema_version", "job_id", "as_of_ns", "hypothesis", "claims", "evidence", "instrument_ids",
                              "entity_ids", "observation_window", "horizon_seconds", "assumptions", "forecast", "abstain",
                              "reason"})
EVIDENCE_REF_KEYS = frozenset({"span_id", "dataset_id", "ref_type", "ref_id", "content_hash", "observed_at_ns",
                               "available_at_ns"})
EVIDENCE_REF_TYPES = frozenset({"observation", "document_span"})
FORECAST_KEYS = frozenset({"target", "horizon_seconds", "unit", "distribution", "calibration", "uncertainty",
                           "invalidation_conditions"})
UNCERTAINTY_KEYS = frozenset({"observation_count", "effective_sample_size", "training_window", "evaluation_window",
                              "interval_method", "data_version"})
# Measurable quantities only: a forecast states what may happen to a quantity, never what to do about it.
FORECAST_QUANTITIES = frozenset({"return", "log_return", "price", "volatility", "fundamental", "probability"})
MAX_TIME_NS = 2 ** 63 - 1
MAX_HORIZON_S = 31_536_000
_HEX64 = re.compile(r"[0-9a-f]{64}")
_DATASET_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SPAN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_REF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+#=-]{0,199}")
_UNIT = re.compile(r"fraction|percent|bp|ratio|count|[A-Z]{3}|[A-Z]{3}/share")


def _bad(field: str, problem: str) -> Rejected:
    return Rejected("PROPOSAL_INVALID", f"{field}: {problem}")


def _int(value: object, field: str, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise _bad(field, f"must be an integer in [{lo}, {hi}]")
    return value


def _text(value: object, field: str, max_len: int, *, min_len: int = 0) -> str:
    if not isinstance(value, str) or not min_len <= len(value) <= max_len:
        raise _bad(field, f"must be a string of {min_len}..{max_len} characters")
    if min_len and not value.strip():
        raise _bad(field, "must not be blank")
    return value


def _optional_text(value: object, field: str, max_len: int) -> str | None:
    return None if value is None else _text(value, field, max_len, min_len=1)


def _texts(value: object, field: str, max_items: int, max_len: int, *, min_items: int = 0, min_len: int = 0) -> list:
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        raise _bad(field, f"must be a list of {min_items}..{max_items} strings")
    return [_text(v, f"{field}[{i}]", max_len, min_len=min_len) for i, v in enumerate(value)]


def _keys(value: object, field: str, keys: frozenset) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        got = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise _bad(field, f"must be an object with exactly the keys {sorted(keys)}, got {got}")
    return value


def _ids(value: object, field: str, max_items: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise _bad(field, f"must be a list of at most {max_items} identifiers")
    for i, v in enumerate(value):
        try:
            validate_id(v, field)
        except InvalidIdentifier as exc:
            raise _bad(f"{field}[{i}]", str(exc)) from None
    if len(set(value)) != len(value):
        raise _bad(field, "identifiers must be unique")
    return value


def _window(value: object, field: str, as_of_ns: int) -> tuple[int, int]:
    w = _keys(value, field, frozenset({"from_ns", "to_ns"}))
    lo = _int(w["from_ns"], f"{field}.from_ns", 0, MAX_TIME_NS)
    hi = _int(w["to_ns"], f"{field}.to_ns", 0, MAX_TIME_NS)
    if lo > hi:
        raise _bad(field, "from_ns is after to_ns")
    if hi > as_of_ns:
        raise _bad(field, "to_ns is after the job's as_of_ns")
    return lo, hi


def _decimal(value: object, field: str) -> Fixed:
    if not isinstance(value, str) or not 1 <= len(value) <= 40 or value != value.strip():
        raise _bad(field, "must be a plain decimal string of 1..40 characters")
    try:
        return Fixed.parse(value)
    except (ValueError, TypeError, ArithmeticError):
        raise _bad(field, f"not a plain decimal: {value!r}") from None


def _validate_forecast(f: object, as_of_ns: int, subjects: set[str]) -> bool:
    """Validate a forecast block. Returns whether it is a quantified uncertainty record (book Ch. 4 line 54):
    calibration method and version, and every uncertainty field, present."""
    f = _keys(f, "forecast", FORECAST_KEYS)
    t = _keys(f["target"], "forecast.target", frozenset({"subject_id", "quantity", "definition"}))
    if not isinstance(t["subject_id"], str) or t["subject_id"] not in subjects:
        raise _bad("forecast.target.subject_id", "must be one of instrument_ids or entity_ids")
    if not isinstance(t["quantity"], str) or t["quantity"] not in FORECAST_QUANTITIES:
        raise _bad("forecast.target.quantity",
                   f"must be one of {sorted(FORECAST_QUANTITIES)}; a forecast is never a trade instruction")
    _text(t["definition"], "forecast.target.definition", 500, min_len=1)
    _int(f["horizon_seconds"], "forecast.horizon_seconds", 1, MAX_HORIZON_S)
    if not isinstance(f["unit"], str) or not _UNIT.fullmatch(f["unit"]):
        raise _bad("forecast.unit", "must be fraction, percent, bp, ratio, count, an ISO 4217 code or <ISO 4217>/share")
    d = f["distribution"]
    kind = d.get("kind") if isinstance(d, dict) else None
    if kind == "interval":
        d = _keys(d, "forecast.distribution", frozenset({"kind", "level", "lower", "upper"}))
        level = _decimal(d["level"], "forecast.distribution.level")
        if not (level.is_positive() and level < Fixed(1)):
            raise _bad("forecast.distribution.level", "must be strictly between 0 and 1")
        if _decimal(d["lower"], "forecast.distribution.lower") > _decimal(d["upper"], "forecast.distribution.upper"):
            raise _bad("forecast.distribution", "lower is above upper")
    elif kind == "quantiles":
        d = _keys(d, "forecast.distribution", frozenset({"kind", "points"}))
        points = d["points"]
        if not isinstance(points, list) or not 2 <= len(points) <= 99:
            raise _bad("forecast.distribution.points", "must be a list of 2..99 points")
        prev = None
        for i, pt in enumerate(points):
            pt = _keys(pt, f"forecast.distribution.points[{i}]", frozenset({"p", "value"}))
            prob = _decimal(pt["p"], f"forecast.distribution.points[{i}].p")
            value = _decimal(pt["value"], f"forecast.distribution.points[{i}].value")
            if not (prob.is_positive() and prob < Fixed(1)):
                raise _bad(f"forecast.distribution.points[{i}].p", "must be strictly between 0 and 1")
            if prev is not None and (prob <= prev[0] or value < prev[1]):
                raise _bad("forecast.distribution.points", "probabilities must increase strictly and values must not decrease")
            prev = (prob, value)
    else:
        raise _bad("forecast.distribution", "kind must be 'interval' or 'quantiles'")
    c = _keys(f["calibration"], "forecast.calibration", frozenset({"method", "version"}))
    calibration = (_optional_text(c["method"], "forecast.calibration.method", 200),
                   _optional_text(c["version"], "forecast.calibration.version", 200))
    u = _keys(f["uncertainty"], "forecast.uncertainty", UNCERTAINTY_KEYS)
    if u["observation_count"] is not None:
        _int(u["observation_count"], "forecast.uncertainty.observation_count", 0, 10 ** 12)
    if u["effective_sample_size"] is not None and _decimal(
            u["effective_sample_size"], "forecast.uncertainty.effective_sample_size").is_negative():
        raise _bad("forecast.uncertainty.effective_sample_size", "must not be negative")
    for name in ("training_window", "evaluation_window"):
        if u[name] is not None:
            _window(u[name], f"forecast.uncertainty.{name}", as_of_ns)
    _optional_text(u["interval_method"], "forecast.uncertainty.interval_method", 200)
    _optional_text(u["data_version"], "forecast.uncertainty.data_version", 200)
    _texts(f["invalidation_conditions"], "forecast.invalidation_conditions", 10, 500, min_items=1, min_len=1)
    return all(x is not None for x in calibration) and all(u[k] is not None for k in UNCERTAINTY_KEYS)


def _validate_evidence_refs(refs: object, as_of: int, window: tuple[int, int] | None) -> list:
    if not isinstance(refs, list) or len(refs) > 50:
        raise _bad("evidence", "must be a list of at most 50 evidence references")
    seen_refs, seen_spans = set(), set()
    for i, r in enumerate(refs):
        f = f"evidence[{i}]"
        r = _keys(r, f, EVIDENCE_REF_KEYS)
        if not isinstance(r["span_id"], str) or not _SPAN_ID.fullmatch(r["span_id"]):
            raise _bad(f"{f}.span_id", "invalid span id")
        if not isinstance(r["dataset_id"], str) or not _DATASET_ID.fullmatch(r["dataset_id"]):
            raise _bad(f"{f}.dataset_id", "invalid dataset id")
        if not isinstance(r["ref_type"], str) or r["ref_type"] not in EVIDENCE_REF_TYPES:
            raise _bad(f"{f}.ref_type", f"must be one of {sorted(EVIDENCE_REF_TYPES)}")
        if not isinstance(r["ref_id"], str) or not _REF_ID.fullmatch(r["ref_id"]):
            raise _bad(f"{f}.ref_id", "invalid record or span id")
        if not isinstance(r["content_hash"], str) or not _HEX64.fullmatch(r["content_hash"]):
            raise _bad(f"{f}.content_hash", "must be 64 lowercase hexadecimal characters")
        available = _int(r["available_at_ns"], f"{f}.available_at_ns", 0, MAX_TIME_NS)
        if available > as_of:
            raise _bad(f"{f}.available_at_ns", "is after the job's as_of_ns (look-ahead)")
        observed = r["observed_at_ns"]
        if observed is not None and _int(observed, f"{f}.observed_at_ns", 0, MAX_TIME_NS) > as_of:
            raise _bad(f"{f}.observed_at_ns", "is after the job's as_of_ns")
        at = available if observed is None else observed
        if window is not None and not window[0] <= at <= window[1]:
            raise _bad(f"{f}.observed_at_ns", "lies outside observation_window")
        key = (r["dataset_id"], r["ref_type"], r["ref_id"])
        if key in seen_refs or r["span_id"] in seen_spans:
            raise _bad(f, "duplicate evidence reference")
        seen_refs.add(key)
        seen_spans.add(r["span_id"])
    return refs


def _validate_claims(claims: object, n_refs: int) -> list:
    if not isinstance(claims, list) or len(claims) > 20:
        raise _bad("claims", "must be a list of at most 20 claims")
    for i, c in enumerate(claims):
        f = f"claims[{i}]"
        c = _keys(c, f, frozenset({"text", "quote", "evidence_indexes"}))
        _text(c["text"], f"{f}.text", 1000, min_len=1)
        _optional_text(c["quote"], f"{f}.quote", 1000)
        idx = c["evidence_indexes"]
        if not isinstance(idx, list) or not 1 <= len(idx) <= 10:
            raise _bad(f"{f}.evidence_indexes", "must be a list of 1..10 indexes")
        for j, k in enumerate(idx):
            _int(k, f"{f}.evidence_indexes[{j}]", 0, n_refs - 1)
        if len(set(idx)) != len(idx):
            raise _bad(f"{f}.evidence_indexes", "indexes must be distinct")
    return claims


def _validate_proposal_v2(ctx: Context, body: dict) -> str:
    """Strict validation of research_proposal v2 (finding BR-DAT-12; book Ch. 19 line 13: cited evidence,
    instrument identity and timestamps are checked separately from the shape). Returns the core-assigned label:
    ABSTENTION, QUALITATIVE (no forecast block, so it cannot feed a strategy feature), FORECAST_UNCALIBRATED or
    FORECAST_QUANTIFIED."""
    _keys(body, "proposal", PROPOSAL_V2_KEYS)
    _text(body["job_id"], "job_id", 160, min_len=1)
    as_of = _int(body["as_of_ns"], "as_of_ns", 0, MAX_TIME_NS)
    if as_of > ctx.now_ns:
        raise _bad("as_of_ns", "is in the future")
    _text(body["hypothesis"], "hypothesis", 4000)
    if not isinstance(body["abstain"], bool):
        raise _bad("abstain", "must be a boolean")
    _text(body["reason"], "reason", 2000)
    _texts(body["assumptions"], "assumptions", 30, 500)
    instruments = _ids(body["instrument_ids"], "instrument_ids", 20)
    for i, iid in enumerate(instruments):
        if state.instrument(ctx.tx, iid) is None:
            raise _bad(f"instrument_ids[{i}]", f"{iid} is not in the instrument master")
    entities = _ids(body["entity_ids"], "entity_ids", 20)
    if body["horizon_seconds"] is not None:
        _int(body["horizon_seconds"], "horizon_seconds", 1, MAX_HORIZON_S)
    window = None if body["observation_window"] is None else _window(body["observation_window"], "observation_window", as_of)
    refs = _validate_evidence_refs(body["evidence"], as_of, window)
    claims = _validate_claims(body["claims"], len(refs))
    if body["abstain"]:
        if not body["reason"].strip():
            raise _bad("reason", "an abstention needs a reason")
        if body["forecast"] is not None:
            raise _bad("forecast", "an abstention carries no forecast")
        return "ABSTENTION"
    if not body["hypothesis"].strip():
        raise _bad("hypothesis", "required unless the proposal abstains")
    if not refs:
        raise _bad("evidence", "at least one evidence reference is required unless the proposal abstains")
    if not claims:
        raise _bad("claims", "at least one claim is required unless the proposal abstains")
    if window is None:
        raise _bad("observation_window", "required unless the proposal abstains")
    if body["horizon_seconds"] is None:
        raise _bad("horizon_seconds", "required unless the proposal abstains")
    if body["forecast"] is None:
        return "QUALITATIVE"
    quantified = _validate_forecast(body["forecast"], as_of, set(instruments) | set(entities))
    return "FORECAST_QUANTIFIED" if quantified else "FORECAST_UNCALIBRATED"
