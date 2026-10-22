"""Session state machine (ADR-0008; SES-01..SES-12).

States: REQUESTED, WAITING_FUNDS, READY, RUNNING, SUSPENDED, WINDING_DOWN,
RECONCILING, COMPLETED_UNSETTLED, COMPLETED_SETTLED, CLOSED, CANCELLED,
FAILED_REVIEW. Every transition is an event; automatic transitions are
issued by SYSTEM through ``session_tick``, ``valuation`` and reconciliation."""
from __future__ import annotations

import json

from ...domain.errors import Rejected
from ...domain.fixed import Fixed, Rounding
from ...domain.ids import validate_id
from ...domain.money import Money, currency
from ...domain.principal import Role
from ...policy.model import BelowTarget, Mandate, PolicyError
from .. import ledger, state
from ..desk import Context
from .common import entries_permitted

TERMINAL = ("COMPLETED_SETTLED", "CLOSED", "CANCELLED")
MAX_ACTIVE_SESSIONS = 1  # book Ch. 10: one active trading session per account initially
ACTIVE = ("RUNNING", "SUSPENDED", "WINDING_DOWN", "RECONCILING", "FAILED_REVIEW")
# Accounting anomalies attributed to a session freeze it for owner review. Order-outcome
# uncertainty (UNKNOWN_ORDER) already blocks entries account-wide and clears through
# reconciliation, so it does not by itself move a session to FAILED_REVIEW.
REVIEW_KINDS = frozenset({"EXECUTION_ANOMALY", "OVERCOMMITTED"})
# Preflight checks only an owner can satisfy; automatic starts run the rest (ADR-0010).
OWNER_ACTION_CHECKS = frozenset({"OWNER_REQUIRED", "OWNER_REAUTHENTICATION_REQUIRED"})


def _refs(s: dict) -> dict:
    return dict(account_key=s["account_key"], session_id=s["session_id"])


def _merge(*lists: list[str]) -> list[str]:
    out: list[str] = []
    for items in lists:
        out += [x for x in items if x not in out]
    return out


def attributed_exceptions(ctx: Context, s: dict, kinds: frozenset[str] | None = None) -> list[dict]:
    """Open blocking exceptions attributed to the session: by session id, or by naming the session's
    ledger owner (an uncovered sell or a correction below the sold quantity carries only the owner)."""
    owner = ledger.session_owner(s["session_id"])
    return [e for e in state.open_exceptions(ctx.tx, s["account_key"], blocking_only=True)
            if (e.get("session_id") == s["session_id"] or e.get("owner_hint") == owner)
            and (kinds is None or e["kind"] in kinds)]


def sessions_of_case(case: dict) -> list[str]:
    """Ids of the sessions an exception case is attributed to (session id and/or session owner)."""
    ids = [case["session_id"]] if case.get("session_id") else []
    hint = case.get("owner_hint") or ""
    if hint.startswith("session:") and hint.split(":", 1)[1] not in ids:
        ids.append(hint.split(":", 1)[1])
    return ids


def preflight_blockers(ctx: Context, account_key: str, *, owner_decision: bool) -> tuple[list[str], dict]:
    """ADR-0010 green preflight for a session start or resume (review finding 45). An automatic start
    runs the subset that needs no owner action, as ADR-0010 defines for automatic resumption."""
    from .control import preflight
    blockers, evidence = preflight(ctx, account_key)
    if not owner_decision:
        blockers = [b for b in blockers if b not in OWNER_ACTION_CHECKS]
    return blockers, evidence


def _cancel_open_buys(ctx: Context, s: dict, reason: str) -> None:
    from .orders import cancel_order
    for o in state.orders(ctx.tx, s["account_key"], session_id=s["session_id"], open_only=True):
        if o["side"] == "BUY":
            cancel_order(ctx, o, reason)


def _new_row(session_id: str, account_key: str, m: Mandate, predecessor: str | None, now_ns: int) -> dict:
    return {"session_id": session_id, "account_key": account_key, "state": "REQUESTED", "mandate_hash": m.mandate_hash,
            "predecessor_id": predecessor, "currency": m.currency, "seed": m.seed.amount.to_str(),
            "target": m.target_terminal_equity.amount.to_str(), "outcome": None, "created_at_ns": now_ns,
            "funded_at_ns": None, "started_at_ns": None, "ended_at_ns": None, "completed_at_ns": None, "settled_at_ns": None,
            "wind_down_reason": None, "wind_down_evidence": None, "wind_down_attempts": {}, "funding": None,
            "high_water": m.seed.amount.to_str(), "protected_value": None, "terminal_equity": None, "resumes_used": 0,
            "review_return_state": None, "blockers": [], "suspend_reason": None}


# ---------------------------------------------------------------- owner commands
def create_session(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    state.account(ctx.tx, key)
    policy = state.active_policy(ctx.tx, key)
    session_id = validate_id(p.get("session_id") or ctx.new_id("ses-"), "session id")
    if ctx.tx.one("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)):
        raise Rejected("SESSION_EXISTS", session_id)
    try:
        seed = Money(policy.currency, Fixed.parse(p["seed"])) if p.get("seed") is not None else None
        target = Money(policy.currency, Fixed.parse(p["target_terminal_equity"])) if p.get("target_terminal_equity") is not None else None
        m = Mandate.from_template(policy.session_template, session_id=session_id, account_key=key,
                                  currency_code=policy.currency, policy_hash_value=policy.policy_hash, now_ns=ctx.now_ns,
                                  origin="OWNER", seed=seed, target=target)
    except (PolicyError, ValueError, ArithmeticError, TypeError) as exc:
        raise Rejected("MANDATE_INVALID", str(exc)) from None
    if m.seed > policy.limits.authorized_capital:
        raise Rejected("MANDATE_INVALID", "seed exceeds authorized capital")
    row = _new_row(session_id, key, m, None, ctx.now_ns)
    required = (m.target_terminal_equity.amount - m.seed.amount).div(m.seed.amount, 4, Rounding.HALF_EVEN) * 100
    ctx.emit("SessionRequested", {"mandate": m.to_json(), "required_net_return_pct": required.to_str(),
                                  "note": "target is a configured mandate, not a forecast or funding authorization"},
             {"mandates": [{"mandate_hash": m.mandate_hash, "session_id": session_id, "body": m.to_json()}], "sessions": [row]},
             **_refs(row))
    try_fund(ctx, state.session(ctx.tx, session_id))
    return {"session_id": session_id, "state": state.session(ctx.tx, session_id)["state"], "mandate_hash": m.mandate_hash}


def amend_session_request(ctx: Context, p: dict) -> dict:
    s = state.session(ctx.tx, p.get("session_id", ""))
    if s["state"] not in ("REQUESTED", "WAITING_FUNDS"):
        raise Rejected("INVALID_STATE", "only unfunded requests can be amended")
    old = state.mandate(ctx.tx, s["mandate_hash"])
    policy = state.policy_by_hash(ctx.tx, old.policy_hash)
    try:
        seed = Money(old.currency, Fixed.parse(p.get("seed", old.seed.amount.to_str())))
        target = Money(old.currency, Fixed.parse(p.get("target_terminal_equity", old.target_terminal_equity.amount.to_str())))
        m = Mandate.from_template(policy.session_template, session_id=s["session_id"], account_key=s["account_key"],
                                  currency_code=old.currency, policy_hash_value=old.policy_hash, now_ns=ctx.now_ns,
                                  origin=old.origin, seed=seed, target=target)
    except (PolicyError, ValueError, ArithmeticError, TypeError) as exc:
        raise Rejected("MANDATE_INVALID", str(exc)) from None
    if m.seed > policy.limits.authorized_capital:
        raise Rejected("MANDATE_INVALID", "seed exceeds authorized capital")
    new = {**s, "mandate_hash": m.mandate_hash, "seed": m.seed.amount.to_str(), "target": m.target_terminal_equity.amount.to_str(),
           "high_water": m.seed.amount.to_str()}
    ctx.emit("SessionMandateAmended", {"previous": s["mandate_hash"], "mandate": m.to_json()},
             {"mandates": [{"mandate_hash": m.mandate_hash, "session_id": s["session_id"], "body": m.to_json()}], "sessions": [new]},
             **_refs(s))
    try_fund(ctx, state.session(ctx.tx, s["session_id"]))
    return {"session_id": s["session_id"], "mandate_hash": m.mandate_hash}


def cancel_session(ctx: Context, p: dict) -> dict:
    s = state.session(ctx.tx, p.get("session_id", ""))
    if s["state"] not in ("REQUESTED", "WAITING_FUNDS", "READY"):
        raise Rejected("INVALID_STATE", f"cannot cancel a session in {s['state']}")
    changes: dict = {"sessions": [{**s, "state": "CANCELLED", "ended_at_ns": ctx.now_ns, "outcome": "CANCELLED"}]}
    if s["state"] == "READY":
        changes["journal"] = _release_entries(ctx, s, ledger.UNALLOCATED, "return cancelled session allocation")
    ctx.emit("SessionCancelled", {}, changes, **_refs(s))
    retry_waiting_funding(ctx, s["account_key"])
    return {"session_id": s["session_id"], "state": "CANCELLED"}


def start_session(ctx: Context, p: dict) -> dict:
    s = state.session(ctx.tx, p.get("session_id", ""))
    if s["state"] != "READY":
        raise Rejected("INVALID_STATE", f"session is {s['state']}")
    m = state.mandate(ctx.tx, s["mandate_hash"])
    if ctx.principal.role is Role.SYSTEM and not m.auto_start:
        raise Rejected("AUTO_START_NOT_PERMITTED", "mandate does not allow automatic start")
    blockers, evidence = start_blockers(ctx, s, owner_decision=ctx.principal.role is Role.OWNER)
    if blockers:
        raise Rejected("START_BLOCKED", ", ".join(blockers), blockers=blockers, evidence=evidence)
    _start(ctx, s, evidence)
    return {"session_id": s["session_id"], "state": "RUNNING"}


def start_blockers(ctx: Context, s: dict, *, owner_decision: bool) -> tuple[list[str], dict]:
    """Entry gates plus the ADR-0010 preflight (full for an owner start, the no-owner-action subset for an
    automatic one), execution ownership and the one-active-session limit. Returns (blockers, evidence)."""
    ok, blockers = entries_permitted(ctx, s["account_key"])
    checks, evidence = preflight_blockers(ctx, s["account_key"], owner_decision=owner_decision)
    blockers = _merge(blockers, checks)
    own = state.ownership(ctx.tx, s["account_key"])
    if not (own and own["status"] == "HELD" and own["deployment_id"] == ctx.desk.deployment_id):
        blockers = _merge(blockers, ["EXECUTION_OWNERSHIP_NOT_HELD"])
    active = [x for x in state.sessions(ctx.tx, s["account_key"], ACTIVE) if x["session_id"] != s["session_id"]]
    if len(active) >= MAX_ACTIVE_SESSIONS:
        blockers.append(f"ACTIVE_SESSION_LIMIT:{active[0]['session_id']}")
    return blockers, evidence


def _start(ctx: Context, s: dict, evidence: dict) -> None:
    ctx.emit("SessionStarted", {"by": ctx.principal.id, "preflight": evidence},
             {"sessions": [{**s, "state": "RUNNING", "started_at_ns": ctx.now_ns, "blockers": []}]}, **_refs(s))


def suspend_session(ctx: Context, p: dict) -> dict:
    s = state.session(ctx.tx, p.get("session_id", ""))
    if s["state"] != "RUNNING":
        raise Rejected("INVALID_STATE", f"session is {s['state']}")
    suspend(ctx, s, p.get("reason") or "OWNER")
    return {"session_id": s["session_id"], "state": "SUSPENDED"}


def suspend(ctx: Context, s: dict, reason: str, evidence: dict | None = None) -> None:
    ctx.emit("SessionSuspended", {"reason": reason, "evidence": evidence or {}},
             {"sessions": [{**s, "state": "SUSPENDED", "suspend_reason": reason}]}, **_refs(s))
    _cancel_open_buys(ctx, s, f"session suspended: {reason}")


def resume_session(ctx: Context, p: dict) -> dict:
    """SUSPENDED -> RUNNING (OWNER only): preflight passes and the session's own budgets are not breached
    on fresh marks (ADR-0008; review findings 34 and 45)."""
    from .valuation import session_budget_breaches
    s = state.session(ctx.tx, p.get("session_id", ""))
    if s["state"] != "SUSPENDED":
        raise Rejected("INVALID_STATE", f"session is {s['state']}")
    ok, blockers = entries_permitted(ctx, s["account_key"])
    checks, evidence = preflight_blockers(ctx, s["account_key"], owner_decision=True)
    breaches, valued = session_budget_breaches(ctx, s)
    blockers = _merge(blockers, checks, breaches)
    if blockers:
        raise Rejected("RESUME_BLOCKED", ", ".join(blockers), blockers=blockers, evidence=evidence, valuation=valued)
    ctx.emit("SessionResumed", {"by": ctx.principal.id, "preflight": evidence, "valuation": valued},
             {"sessions": [{**s, "state": "RUNNING", "suspend_reason": None}]}, **_refs(s))
    return {"session_id": s["session_id"], "state": "RUNNING"}


def end_session(ctx: Context, p: dict) -> dict:
    s = state.session(ctx.tx, p.get("session_id", ""))
    if s["state"] not in ("RUNNING", "SUSPENDED"):
        raise Rejected("INVALID_STATE", f"session is {s['state']}")
    begin_wind_down(ctx, s, "OWNER_ENDED", {"by": ctx.principal.id})
    return {"session_id": s["session_id"], "state": state.session(ctx.tx, s["session_id"])["state"]}


# ---------------------------------------------------------------- funding
def try_fund(ctx: Context, s: dict) -> None:
    if s["state"] not in ("REQUESTED", "WAITING_FUNDS"):
        return
    key = s["account_key"]
    m = state.mandate(ctx.tx, s["mandate_hash"])
    policy = state.active_policy(ctx.tx, key)
    ctl = state.control(ctx.tx, key)
    reason = None
    shortfall = None
    if ctl["stop_latched"]:
        reason = f"STOP_LATCHED:{ctl['latch_reason']}"
    elif ctl["needs_reconciliation"] or ctl["reconciled_epoch"] != ctx.epoch:
        reason = "RECONCILIATION_REQUIRED"
    elif state.open_exceptions(ctx.tx, key, blocking_only=True):
        reason = "BLOCKING_EXCEPTIONS"
    else:
        available = ledger.available_cash(ctx.tx, key, ledger.UNALLOCATED, m.currency) - policy.limits.operational_buffer.amount
        if m.seed.amount > available:
            reason = "INSUFFICIENT_UNALLOCATED_SETTLED_CASH"
            shortfall = (m.seed.amount - available).to_str()
    if reason is not None:
        funding = {"status": "WAITING", "reason": reason, "shortfall": shortfall,
                   "note": "funded only from reconciled, settled, unallocated cash; protected and unsettled proceeds are never used"}
        if s["state"] != "WAITING_FUNDS" or s.get("funding") != funding:
            ctx.emit("SessionAwaitingFunds", funding, {"sessions": [{**s, "state": "WAITING_FUNDS", "funding": funding}]}, **_refs(s))
        return
    e = ledger.entry(ctx.new_id("je-"), key, "OWNER_TRANSFER", f"fund session {s['session_id']}",
                     ledger.transfer(ledger.CASH, m.currency, m.seed.amount, ledger.UNALLOCATED, ledger.session_owner(s["session_id"])))
    funded = {**s, "state": "READY", "funded_at_ns": ctx.now_ns, "funding": {"status": "FUNDED", "amount": m.seed.amount.to_str()}}
    ctx.emit("SessionFunded", {"amount": m.seed.to_json(), "source": ledger.UNALLOCATED}, {"journal": [e], "sessions": [funded]}, **_refs(s))
    maybe_auto_start(ctx, state.session(ctx.tx, s["session_id"]))


def maybe_auto_start(ctx: Context, s: dict) -> None:
    if s["state"] != "READY":
        return
    m = state.mandate(ctx.tx, s["mandate_hash"])
    if not m.auto_start:
        return
    blockers, evidence = start_blockers(ctx, s, owner_decision=False)
    if blockers:
        if s.get("blockers") != blockers:
            ctx.emit("SessionStartDeferred", {"blockers": blockers}, {"sessions": [{**s, "blockers": blockers}]}, **_refs(s))
        return
    _start(ctx, s, evidence)


def retry_waiting_funding(ctx: Context, account_key: str) -> None:
    for s in state.sessions(ctx.tx, account_key, ("REQUESTED", "WAITING_FUNDS")):
        try_fund(ctx, state.session(ctx.tx, s["session_id"]))
    for s in state.sessions(ctx.tx, account_key, ("READY",)):
        maybe_auto_start(ctx, state.session(ctx.tx, s["session_id"]))


# ---------------------------------------------------------------- wind-down
def begin_wind_down(ctx: Context, s: dict, reason: str, evidence: dict) -> None:
    ctx.emit("SessionWindDownStarted", {"reason": reason, "evidence": evidence,
                                        "note": "target observation on marks is not completion" if reason == "TARGET_OBSERVED" else ""},
             {"sessions": [{**s, "state": "WINDING_DOWN", "wind_down_reason": reason, "wind_down_evidence": evidence,
                            "ended_at_ns": ctx.now_ns}]}, **_refs(s))
    advance_wind_down(ctx, state.session(ctx.tx, s["session_id"]))


def advance_wind_down(ctx: Context, s: dict) -> None:
    from .orders import WIND_DOWN, cancel_order, create_order
    key = s["account_key"]
    owner = ledger.session_owner(s["session_id"])
    policy = state.active_policy(ctx.tx, key)
    m = state.mandate(ctx.tx, s["mandate_hash"])
    open_orders = state.orders(ctx.tx, key, session_id=s["session_id"], open_only=True)
    ctl = state.control(ctx.tx, key)
    attempts = dict(s.get("wind_down_attempts") or {})
    for o in open_orders:
        if o["side"] == "BUY":
            cancel_order(ctx, o, "session winding down")
        elif o["purpose"] == WIND_DOWN and ctx.now_ns - o["accepted_at_ns"] > policy.wind_down.reprice_after_s * 1_000_000_000:
            # A working reduction is pulled to reprice only if a replacement may follow. Under a stop, sells
            # already working stay working (ADR-0010, on_stop_liquidation = NONE); after the last permitted
            # attempt the working order is the last one (review finding 32).
            if ctl["stop_latched"] or attempts.get(o["instrument_id"], 0) >= policy.wind_down.max_attempts:
                continue
            cancel_order(ctx, o, "wind-down reprice")
    blockers: list[str] = []
    lots = ledger.owner_lots(ctx.tx, key, owner)
    instruments = sorted({l["instrument_id"] for l in lots})
    for inst_id in instruments:
        unreserved = ledger.owned_quantity(ctx.tx, key, owner, inst_id) - ledger.active_quantity_holds(ctx.tx, key, owner, inst_id)
        if not unreserved.is_positive():
            continue
        if ctl["stop_latched"]:
            blockers.append(f"{inst_id}:STOP_LATCHED (no automatic liquidation under a stop; on_stop_liquidation=NONE)")
            continue
        if attempts.get(inst_id, 0) >= policy.wind_down.max_attempts:
            blockers.append(f"{inst_id}:MAX_ATTEMPTS")
            continue
        inst = state.instrument(ctx.tx, inst_id)
        q = ctx.desk.quotes.get(inst_id)
        if inst is None or q is None or q.bid is None or q.age_ns(ctx.now_ns) > policy.limits.quote_max_age_ms * 1_000_000:
            blockers.append(f"{inst_id}:NO_FRESH_BID")
            continue
        price = (q.bid * (10_000 - policy.wind_down.collar_bps)).div(10_000, q.bid.scale + 4, Rounding.FLOOR)
        price = inst.round_price(price, Rounding.FLOOR)
        mid = q.mid()
        if mid is not None:
            # Never below the pretrade sell floor ceil(mid x (1 - price_collar_bps)): on a spread wider than
            # about the two collars' difference the bid-based price would fail its own pretrade check on every
            # attempt and liquidation would never start (review finding 46).
            floor = (mid * (10_000 - policy.limits.price_collar_bps)).div(10_000, mid.scale + 4, Rounding.CEILING)
            floor = inst.round_price(inst.round_price(floor, Rounding.CEILING), Rounding.CEILING)
            if price < floor:
                price = floor
        qty = unreserved.to_increment(inst.quantity_increment, Rounding.DOWN)
        if not qty.is_positive() or not price.is_positive():
            blockers.append(f"{inst_id}:BELOW_MINIMUM")
            continue
        try:
            create_order(ctx, account_key=key, session_id=s["session_id"], order_id=ctx.new_id("wd-"), instrument_id=inst_id,
                         side="SELL", quantity=qty, limit_price=price, order_type="LIMIT", tif="DAY",
                         deadline_ns=ctx.now_ns + policy.wind_down.reprice_after_s * 1_000_000_000,
                         policy_hash=m.policy_hash, strategy_id="system:wind-down", purpose=WIND_DOWN,
                         decision_ref={"session_wind_down": s["wind_down_reason"]})
            attempts[inst_id] = attempts.get(inst_id, 0) + 1
        except Rejected as r:
            blockers.append(f"{inst_id}:{r.code}")
    s = state.session(ctx.tx, s["session_id"])
    if attempts != (s.get("wind_down_attempts") or {}) or blockers != s.get("blockers"):
        ctx.emit("SessionWindDownProgress", {"blockers": blockers, "attempts": attempts},
                 {"sessions": [{**s, "wind_down_attempts": attempts, "blockers": blockers}]}, **_refs(s))
        s = state.session(ctx.tx, s["session_id"])
    if not state.orders(ctx.tx, key, session_id=s["session_id"], open_only=True) and not ledger.owner_lots(ctx.tx, key, owner):
        ctx.emit("SessionReconciling", {"note": "flat with no open orders; awaiting final fees and a clean reconciliation"},
                 {"sessions": [{**s, "state": "RECONCILING", "blockers": []}]}, **_refs(s))
        evaluate_reconciling(ctx, state.session(ctx.tx, s["session_id"]))


def evaluate_reconciling(ctx: Context, s: dict) -> None:
    key = s["account_key"]
    owner = ledger.session_owner(s["session_id"])
    open_orders = state.orders(ctx.tx, key, session_id=s["session_id"], open_only=True)
    lots = ledger.owner_lots(ctx.tx, key, owner)
    if open_orders or lots:
        # Activity after the session went flat (a late fill or a broker correction) gave it inventory or
        # orders again. RECONCILING cannot reduce them, so wind-down resumes (review finding 9); the broker
        # observation path has already opened a session-attributed case for the late activity.
        ctx.emit("SessionWindDownResumed", {"reason": "NOT_FLAT", "open_orders": [o["order_id"] for o in open_orders],
                                            "lots": {l["lot_id"]: l["open_qty"] for l in lots}},
                 {"sessions": [{**s, "state": "WINDING_DOWN", "blockers": []}]}, **_refs(s))
        advance_wind_down(ctx, state.session(ctx.tx, s["session_id"]))
        return
    blockers: list[str] = []
    execs = [json.loads(r["row"]) for r in ctx.tx.all(
        "SELECT row FROM executions WHERE json_extract(row,'$.session_id')=?", (s["session_id"],))]
    pending_fees = [e["exec_key"] for e in execs if e["status"] == "ACTIVE" and e["fee_status"] not in ("FINAL", "ACCEPTED_ESTIMATE")]
    if pending_fees:
        blockers.append(f"FEES_NOT_FINAL:{len(pending_fees)}")
    last_exec_ns = max([e["exec_time_ns"] for e in execs], default=0)
    last_seq_row = ctx.tx.one("SELECT MAX(seq) AS s FROM events WHERE session_id=? AND type IN ('ExecutionRecorded','CommissionFinalized','CommissionAdjusted')",
                              (s["session_id"],))
    rec = state.latest_reconciliation(ctx.tx, key)
    if rec is None or rec["status"] != "CLEAN" or rec["epoch"] != ctx.epoch or rec.get("event_seq_at_start", 0) < (last_seq_row["s"] or 0):
        blockers.append("CLEAN_RECONCILIATION_AFTER_LAST_EXECUTION_REQUIRED")
    if attributed_exceptions(ctx, s):
        blockers.append("SESSION_EXCEPTIONS_OPEN")
    m = state.mandate(ctx.tx, s["mandate_hash"])
    available = ledger.available_cash(ctx.tx, key, owner, m.currency)
    if available.is_negative():
        # Completion and close move every balance out of the session; a deficit must never be moved to
        # protected or unallocated cash (that would fail the ledger invariants on every tick, finding 31).
        blockers.append(f"NEGATIVE_AVAILABLE_CASH:{available.to_str()}")
    if blockers:
        if blockers != s.get("blockers"):
            ctx.emit("SessionReconcilingBlocked", {"blockers": blockers, "last_execution_ns": last_exec_ns},
                     {"sessions": [{**s, "blockers": blockers}]}, **_refs(s))
        return
    equity = ledger.liquid_value(ctx.tx, key, owner, m.currency)
    if s["wind_down_reason"] == "TARGET_OBSERVED":
        if equity >= m.target_terminal_equity.amount:
            complete(ctx, s, equity)
            return
        if m.below_target_after_target_observed is BelowTarget.RESUME and s["resumes_used"] < m.max_resumes:
            ok, why = entries_permitted(ctx, key)
            if ok:
                # A later target observation starts a new wind-down with its own attempt budget (finding 48).
                ctx.emit("SessionResumedBelowTarget", {"terminal_equity": equity.to_str(), "target": m.target_terminal_equity.amount.to_str()},
                         {"sessions": [{**s, "state": "RUNNING", "resumes_used": s["resumes_used"] + 1, "wind_down_reason": None,
                                        "wind_down_evidence": None, "ended_at_ns": None, "blockers": [],
                                        "wind_down_attempts": {}}]}, **_refs(s))
                return
        close(ctx, s, "BELOW_TARGET", equity)
        return
    close(ctx, s, {"OWNER_ENDED": "OWNER_ENDED", "RISK_ENDED": "RISK_ENDED", "EXPIRED": "EXPIRED"}.get(s["wind_down_reason"], "ENDED"), equity)


def _release_entries(ctx: Context, s: dict, to_owner: str, memo: str) -> list[dict]:
    """Owner transfers moving every liquid balance of the session to ``to_owner``."""
    key = s["account_key"]
    owner = ledger.session_owner(s["session_id"])
    lines: list[ledger.Line] = []
    for (code, ccy, inst), amount in sorted(ledger.owner_balances(ctx.tx, key, owner).items()):
        if code in ledger.LIQUID_CODES and not amount.is_zero():
            lines += ledger.transfer(code, ccy, amount, owner, to_owner)
        elif code == ledger.INVENTORY and not amount.is_zero():
            raise Rejected("NOT_FLAT", "session still carries inventory cost")
    if not lines:
        return []
    return [ledger.entry(ctx.new_id("je-"), key, "OWNER_TRANSFER", memo, lines)]


def _reown_settlements(ctx: Context, s: dict, to_owner: str) -> list[dict]:
    owner = ledger.session_owner(s["session_id"])
    return [{**r, "owner": to_owner} for r in state.settlements(ctx.tx, s["account_key"], owner=owner, status="EXPECTED")]


def complete(ctx: Context, s: dict, equity: Fixed) -> None:
    key = s["account_key"]
    protected = ledger.protected_owner(s["session_id"])
    settlements = _reown_settlements(ctx, s, protected)
    final_state = "COMPLETED_UNSETTLED" if settlements else "COMPLETED_SETTLED"
    row = {**s, "state": final_state, "completed_at_ns": ctx.now_ns, "settled_at_ns": None if settlements else ctx.now_ns,
           "protected_value": equity.to_str(), "terminal_equity": equity.to_str(), "outcome": "TARGET_REACHED", "blockers": []}
    ctx.emit("SessionCompleted", {"terminal_equity": equity.to_str(), "protected_owner": protected,
                                  "note": "proceeds are protected allocations inside the broker account; no transfer is made"},
             {"journal": _release_entries(ctx, s, protected, f"protect completed session {s['session_id']}"),
              "settlements": settlements, "sessions": [row]}, **_refs(s))
    _create_successor(ctx, state.session(ctx.tx, s["session_id"]))


def _create_successor(ctx: Context, s: dict) -> None:
    key = s["account_key"]
    if ctx.tx.one("SELECT 1 FROM successors WHERE predecessor_id=?", (s["session_id"],)):
        return
    own_policy = state.policy_by_hash(ctx.tx, state.mandate(ctx.tx, s["mandate_hash"]).policy_hash)
    active = state.active_policy(ctx.tx, key)
    if not (own_policy.successor.create_on_success and active.successor.create_on_success):
        ctx.emit("SuccessorNotCreated", {"reason": "successor creation disabled by the session or active policy"}, {}, **_refs(s))
        return
    successor_id = ctx.new_id("ses-")
    m = Mandate.from_template(active.session_template, session_id=successor_id, account_key=key, currency_code=active.currency,
                              policy_hash_value=active.policy_hash, now_ns=ctx.now_ns, origin=f"SUCCESSOR_OF:{s['session_id']}")
    row = _new_row(successor_id, key, m, s["session_id"], ctx.now_ns)
    ctx.emit("SuccessorRequested", {"predecessor": s["session_id"], "mandate": m.to_json(),
                                    "note": "mandate frozen now from the active template; later default changes do not alter it"},
             {"mandates": [{"mandate_hash": m.mandate_hash, "session_id": successor_id, "body": m.to_json()}],
              "sessions": [row], "successors": [{"predecessor_id": s["session_id"], "successor_id": successor_id}]},
             account_key=key, session_id=successor_id)
    try_fund(ctx, state.session(ctx.tx, successor_id))


def close(ctx: Context, s: dict, outcome: str, equity: Fixed) -> None:
    row = {**s, "state": "CLOSED", "outcome": outcome, "terminal_equity": equity.to_str(), "blockers": [],
           "ended_at_ns": s.get("ended_at_ns") or ctx.now_ns}
    ctx.emit("SessionClosed", {"outcome": outcome, "terminal_equity": equity.to_str(),
                               "note": "remaining capital returned to unallocated; no successor for an unsuccessful session"},
             {"journal": _release_entries(ctx, s, ledger.UNALLOCATED, f"close session {s['session_id']} ({outcome})"),
              "settlements": _reown_settlements(ctx, s, ledger.UNALLOCATED), "sessions": [row]}, **_refs(s))
    retry_waiting_funding(ctx, s["account_key"])


def mark_settled_sessions(ctx: Context, account_key: str) -> None:
    for s in state.sessions(ctx.tx, account_key, ("COMPLETED_UNSETTLED",)):
        if not state.settlements(ctx.tx, account_key, owner=ledger.protected_owner(s["session_id"]), status="EXPECTED"):
            ctx.emit("SessionSettled", {"note": "every protected receivable confirmed settled"},
                     {"sessions": [{**s, "state": "COMPLETED_SETTLED", "settled_at_ns": ctx.now_ns}]}, **_refs(s))


def flag_review(ctx: Context, session_id: str, case_id: str) -> None:
    s = state.session(ctx.tx, session_id)
    if s["state"] in TERMINAL or s["state"] == "FAILED_REVIEW" or s["state"].startswith("COMPLETED"):
        return
    ctx.emit("SessionFailedReview", {"case_id": case_id},
             {"sessions": [{**s, "state": "FAILED_REVIEW", "review_return_state": s["state"]}]}, **_refs(s))
    # A frozen session must not keep adding inventory it can no longer manage (review finding 33).
    _cancel_open_buys(ctx, s, f"session failed review: {case_id}")


def exit_review_if_clear(ctx: Context, s: dict) -> bool:
    """FAILED_REVIEW -> previous state once no blocking exception is attributed to the session (ADR-0008).

    Runs when the owner resolves a case and on every session tick, so a case the system resolves itself
    (reconciliation, a broker status) also ends the review (review finding 10). The exit never grants new
    exposure: a RUNNING session returns SUSPENDED and needs an owner resume."""
    if s["state"] != "FAILED_REVIEW" or attributed_exceptions(ctx, s):
        return False
    back = s.get("review_return_state") or "SUSPENDED"
    back = "SUSPENDED" if back == "RUNNING" else back
    ctx.emit("SessionReviewResolved", {"returns_to": back, "by": ctx.principal.id},
             {"sessions": [{**s, "state": back, "review_return_state": None}]}, **_refs(s))
    return True


# ---------------------------------------------------------------- tick
def session_tick(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    state.account(ctx.tx, key)
    for s in state.sessions(ctx.tx, key):
        s = state.session(ctx.tx, s["session_id"])
        if exit_review_if_clear(ctx, s):
            s = state.session(ctx.tx, s["session_id"])
        if s["state"] in ("RUNNING", "SUSPENDED"):
            m = state.mandate(ctx.tx, s["mandate_hash"])
            if m.expires_at_ns is not None and ctx.now_ns >= m.expires_at_ns:
                begin_wind_down(ctx, s, "EXPIRED", {"expires_at_ns": m.expires_at_ns})
        elif s["state"] == "WINDING_DOWN":
            advance_wind_down(ctx, s)
        elif s["state"] == "RECONCILING":
            evaluate_reconciling(ctx, s)
        for b in attributed_exceptions(ctx, s, REVIEW_KINDS):
            flag_review(ctx, s["session_id"], b["case_id"])
    retry_waiting_funding(ctx, key)
    mark_settled_sessions(ctx, key)
    return {"sessions": {x["session_id"]: x["state"] for x in state.sessions(ctx.tx, key)}}


def protected_total(ctx: Context, account_key: str, ccy: str) -> Fixed:
    total = Fixed(0, currency(ccy).ledger_scale)
    for s in state.sessions(ctx.tx, account_key, ("COMPLETED_UNSETTLED", "COMPLETED_SETTLED")):
        total = total + ledger.liquid_value(ctx.tx, account_key, ledger.protected_owner(s["session_id"]), ccy)
    return total
