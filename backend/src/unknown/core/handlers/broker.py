"""Broker observations (ADR-0006 observation path, ADR-0009).

Observations always record the economic fact. When a fact violates an
expected invariant (unknown order, overfill, price outside limit, late fill,
changed content under one execution id, foreign activity) the handler records
it, opens an exception that blocks new exposure, and never discards it."""
from __future__ import annotations

import json
from datetime import date

from ...domain.calendar import local_date
from ...domain.errors import Rejected
from ...domain.fixed import Fixed, Rounding
from ...domain.money import Money, currency
from ...risk import fees as fee_model
from .. import ledger, state
from ..desk import Context
from .common import (EXECUTION_ANOMALY, FOREIGN_ACTIVITY, OVERCOMMITTED, POST_COMPLETION_ADJUSTMENT, UNKNOWN_ORDER,
                     control_update, hold_rows, multiplier_of, new_exception)

TERMINAL_MAP = {"CANCELED": "CANCELED", "EXPIRED": "EXPIRED", "REJECTED": "REJECTED", "INACTIVE": "REJECTED"}


def _fx(v) -> Fixed:
    return v if isinstance(v, Fixed) else Fixed.parse(v)


def economic_owner(tx, session_id: str | None) -> str:
    """Owner that bears economic events for an order's session right now (ADR-0008)."""
    if session_id is None:
        return ledger.EXTERNAL
    s = state.session(tx, session_id)
    if s["state"] in ("COMPLETED_UNSETTLED", "COMPLETED_SETTLED"):
        return ledger.protected_owner(session_id)
    if s["state"] in ("CLOSED", "CANCELLED"):
        return ledger.UNALLOCATED
    return ledger.session_owner(session_id)


def _resolve_case(ctx: Context, account_key: str, key: str, resolution: str) -> list[dict]:
    case = state.exception_by_key(ctx.tx, account_key, key)
    if case is None:
        return []
    return [{**case, "status": "RESOLVED", "resolution": {"by": ctx.principal.id, "at_ns": ctx.now_ns, "note": resolution}}]


def _find_order(ctx: Context, account_key: str, client_order_id: str | None, order_ref: str | None,
                broker_order_id: str | None) -> tuple[dict | None, str | None]:
    """Locate our order. Returns (order, foreign_reason)."""
    if order_ref:
        parts = order_ref.split(":")
        if len(parts) >= 3 and parts[0] != ctx.desk.deployment_id:
            return None, f"order reference names deployment {parts[0]}"
    if client_order_id:
        o = state.order(ctx.tx, client_order_id)
        if o is not None and o["account_key"] == account_key:
            return o, None
    if broker_order_id:
        o = state.order_by_broker_id(ctx.tx, account_key, broker_order_id)
        if o is not None:
            return o, None
    return None, None


def _latch_changes(ctx: Context, account_key: str, reason: str) -> dict:
    c = state.control(ctx.tx, account_key)
    if c["stop_latched"]:
        return c
    return {**c, "stop_latched": True, "latch_reason": reason, "latched_by": ctx.principal.id,
            "latched_at_ns": ctx.now_ns, "latch_command_id": ctx.command.command_id}


# ----------------------------------------------------------------- status
def broker_order_status(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    order, foreign = _find_order(ctx, key, p.get("client_order_id"), p.get("order_ref"), p.get("broker_order_id"))
    if order is None:
        case = new_exception(ctx, key, FOREIGN_ACTIVITY, blocking=True, severity="critical",
                             detail={"observation": "order status", "payload": p, "reason": foreign or "not our order"},
                             key=f"foreign-order:{p.get('broker_order_id')}")
        ctx.emit("ForeignOrderObserved", {"payload": p}, {"exceptions": [case] if case else [],
                                                         "control": [_latch_changes(ctx, key, "FOREIGN_ACTIVITY")]},
                 account_key=key)
        return {"foreign": True}
    return apply_status(ctx, order, p["status"], _fx(p.get("filled_quantity", "0")), p.get("reason", ""),
                        p.get("broker_order_id"), via="status")


def apply_status(ctx: Context, order: dict, status: str, broker_filled: Fixed, reason: str,
                 broker_order_id: str | None, via: str) -> dict:
    key = order["account_key"]
    refs = dict(account_key=key, session_id=order["session_id"], order_id=order["order_id"])
    mult = multiplier_of(ctx, order["instrument_id"])
    local = Fixed.parse(order["cum_qty"])
    new = {**order}
    if broker_order_id and not order.get("broker_order_id"):
        new["broker_order_id"] = broker_order_id
    resolved = _resolve_case(ctx, key, f"unknown-order:{order['order_id']}", f"broker reported {status}")
    if order["state"] in state.TERMINAL_ORDER_STATES:
        if broker_filled > local:
            pending = {"status": status, "filled": broker_filled.to_str(), "reason": reason}
            ctx.emit("TerminalOrderAwaitingExecutions", {"status": status},
                     {"orders": [{**new, "broker_terminal_pending": pending}], "exceptions": resolved}, **refs)
        return {"state": order["state"], "noop": True}
    if status in ("PENDING_SUBMIT", "ACKNOWLEDGED", "PARTIALLY_FILLED", "PENDING_CANCEL"):
        if order["state"] in ("SUBMIT_PENDING", "UNKNOWN", "RESERVED"):
            new.update(state="PARTIALLY_FILLED" if local.is_positive() else "ACKNOWLEDGED", prev_state=order["state"])
        if status == "PENDING_CANCEL" and new["state"] != "CANCEL_PENDING":
            new.update(state="CANCEL_PENDING", prev_state=new["state"])
        if new != order or resolved:
            ctx.emit("OrderStatusObserved", {"status": status, "via": via}, {"orders": [new], "exceptions": resolved}, **refs)
        return {"state": new["state"]}
    if status == "FILLED" or status in TERMINAL_MAP:
        target = "FILLED" if status == "FILLED" else TERMINAL_MAP[status]
        if broker_filled > local:
            pending = {"status": status, "filled": broker_filled.to_str(), "reason": reason}
            if order["state"] in ("SUBMIT_PENDING", "UNKNOWN"):
                new.update(state="PARTIALLY_FILLED" if local.is_positive() else "ACKNOWLEDGED", prev_state=order["state"])
            ctx.emit("TerminalStatusAwaitingExecutions", {"status": status, "broker_filled": broker_filled.to_str(),
                                                          "local_filled": local.to_str(),
                                                          "note": "reservation kept until executions are ingested"},
                     {"orders": [{**new, "broker_terminal_pending": pending}], "exceptions": resolved}, **refs)
            return {"state": new["state"], "awaiting_executions": True}
        if broker_filled < local:
            case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                                 detail={"order_id": order["order_id"], "broker_filled": broker_filled.to_str(), "local_filled": local.to_str()},
                                 key=f"fill-count:{order['order_id']}", session_id=order["session_id"], order_id=order["order_id"])
            ctx.emit("OrderFillCountMismatch", {"status": status}, {"exceptions": ([case] if case else []) + resolved}, **refs)
            return {"state": order["state"], "anomaly": True}
        if target == "FILLED" and local < Fixed.parse(order["quantity"]):
            target = "CANCELED"  # broker says done with fewer shares: remainder is no longer working
        if target == "REJECTED" and local.is_positive():
            target = "CANCELED"
        return finalize(ctx, new, target, reason or status, extra_exceptions=resolved, via=via)
    raise Rejected("INVALID_FIELD", f"unknown broker status {status}")


def finalize(ctx: Context, order: dict, target: str, reason: str, *, extra_exceptions: list | None = None, via: str = "") -> dict:
    mult = multiplier_of(ctx, order["instrument_id"])
    new = {**order, "state": target, "prev_state": order["state"], "leaves_qty": "0", "terminal_at_ns": ctx.now_ns,
           "reason": reason, "broker_terminal_pending": None,
           "cancel_state": "CONFIRMED" if target == "CANCELED" else order["cancel_state"]}
    ctx.emit(f"Order{target.title().replace('_', '')}", {"reason": reason, "via": via,
                                                         "note": "remaining reservation released on broker-confirmed terminal state"},
             {"orders": [new], "holds": hold_rows(new, mult, release=True), "exceptions": extra_exceptions or []},
             account_key=order["account_key"], session_id=order["session_id"], order_id=order["order_id"])
    return {"state": target}


# -------------------------------------------------------------- executions
def broker_execution(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    exec_key = p["exec_key"]
    existing = state.execution(ctx.tx, exec_key)
    economic = {k: p.get(k) for k in ("side", "quantity", "price", "instrument_id", "currency")}
    if existing is not None:
        if {k: existing.get(k) for k in economic} == economic or existing.get("raw_economic") == economic:
            return {"exec_key": exec_key, "duplicate": True}
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="critical",
                             detail={"exec_key": exec_key, "issue": "same execution id with changed content",
                                     "stored": {k: existing.get(k) for k in economic}, "received": economic},
                             key=f"exec-changed:{exec_key}:{ctx.payload_hash[:12]}")
        ctx.emit("ExecutionContentConflict", {"exec_key": exec_key, "received": p}, {"exceptions": [case] if case else []},
                 account_key=key)
        return {"exec_key": exec_key, "conflict": True}
    if p.get("correction_of"):
        return _apply_correction(ctx, p)
    order, foreign = _find_order(ctx, key, p.get("client_order_id"), p.get("order_ref"), p.get("broker_order_id"))
    if order is None:
        return _external_execution(ctx, p, foreign)
    return apply_order_execution(ctx, order, p)


def _trade_date(ctx: Context, account_key: str, exec_time_ns: int) -> tuple[date, date, str]:
    policy = state.active_policy(ctx.tx, account_key)
    tz = policy.limits.risk_day_timezone
    trade = local_date(exec_time_ns, tz)
    cal = ctx.desk.calendars.get(policy.settlement_calendar, ctx.desk.calendars["WEEKDAYS"])
    return trade, cal, tz


def _settlement_row(ctx: Context, account_key: str, owner: str, ccy: str, direction: str, amount: Fixed,
                    exec_key: str, exec_time_ns: int, settlement_days: int) -> dict:
    trade, cal, _ = _trade_date(ctx, account_key, exec_time_ns)
    expected = cal.add_business_days(trade, settlement_days)
    return {"settlement_id": f"stl:{exec_key}", "account_key": account_key, "owner": owner, "currency": ccy,
            "direction": direction, "amount": amount.to_str(), "trade_date": trade.isoformat(),
            "expected_date": expected.isoformat(), "calendar": cal.calendar_id, "status": "EXPECTED",
            "exec_key": exec_key, "settled_at_ns": None}


def _consume_lots(ctx: Context, account_key: str, owner: str, instrument_id: str, qty: Fixed, scale: int) -> tuple[list[dict], list[dict], Fixed, Fixed]:
    """FIFO disposal within one owner. Returns (new lot rows, consumed records, cost removed, uncovered qty)."""
    remaining = qty
    cost_removed = Fixed(0, scale)
    rows, consumed = [], []
    for lot in ledger.owner_lots(ctx.tx, account_key, owner, instrument_id):
        if not remaining.is_positive():
            break
        open_qty = Fixed.parse(lot["open_qty"])
        open_cost = Fixed.parse(lot["open_cost"])
        if not open_qty.is_positive():
            continue
        take = remaining if remaining < open_qty else open_qty
        cost = open_cost if take == open_qty else (open_cost * take).div(open_qty, scale, Rounding.HALF_EVEN)
        rows.append({**lot, "open_qty": (open_qty - take).to_str(), "open_cost": (open_cost - cost).rescale(scale).to_str()})
        consumed.append({"lot_id": lot["lot_id"], "qty": take.to_str(), "cost": cost.to_str()})
        cost_removed = cost_removed + cost
        remaining = remaining - take
    return rows, consumed, cost_removed, remaining


def _book_execution(ctx: Context, p: dict, *, owner: str, order: dict | None, settlement_days: int,
                    mult: Fixed, fee_estimate: Money | None) -> tuple[dict, dict, list[dict], dict]:
    """Economic effects of one execution for ``owner``. Returns (changes, exec_row, anomalies, effects)."""
    key = p["account_key"]
    exec_key = p["exec_key"]
    ccy = p["currency"]
    scale = currency(ccy).ledger_scale
    qty, price = _fx(p["quantity"]), _fx(p["price"])
    raw_notional = qty * price * mult
    notional = raw_notional.rescale(scale, Rounding.HALF_EVEN)
    inst_id = p["instrument_id"]
    changes: dict = {"journal": [], "lots": [], "settlements": [], "exceptions": []}
    anomalies: list[dict] = []
    effects: dict = {"entries": [], "lots_opened": [], "lots_consumed": []}
    memo = f"{p['side']} {qty} {inst_id} @ {price} ({exec_key})"
    if p["side"] == "BUY":
        lot_id = f"lot:{exec_key}"
        changes["lots"].append({"lot_id": lot_id, "account_key": key, "instrument_id": inst_id, "owner": owner,
                                "currency": ccy, "open_qty": qty.to_str(), "open_cost": notional.to_str(),
                                "original_qty": qty.to_str(), "opened_by": exec_key, "opened_at_ns": p["exec_time_ns"]})
        lines = [ledger.Line(ledger.INVENTORY, owner, ccy, notional, inst_id), ledger.Line(ledger.PAYABLE, owner, ccy, -notional)]
        effects["lots_opened"].append(lot_id)
        settlement = _settlement_row(ctx, key, owner, ccy, "PAY", notional, exec_key, p["exec_time_ns"], settlement_days)
    else:
        rows, consumed, cost, uncovered = _consume_lots(ctx, key, owner, inst_id, qty, scale)
        changes["lots"].extend(rows)
        effects["lots_consumed"] = consumed
        covered_qty = qty - uncovered
        covered_proceeds = (covered_qty * price * mult).rescale(scale, Rounding.HALF_EVEN) if uncovered.is_positive() else notional
        lines = [ledger.Line(ledger.RECEIVABLE, owner, ccy, covered_proceeds),
                 ledger.Line(ledger.INVENTORY, owner, ccy, -cost, inst_id),
                 ledger.Line(ledger.REALIZED, owner, ccy, -(covered_proceeds - cost))]
        if uncovered.is_positive():
            case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="critical",
                                 detail={"exec_key": exec_key, "issue": "sell exceeds owned quantity", "uncovered": uncovered.to_str(),
                                         "owner": owner}, key=f"uncovered-sell:{exec_key}")
            case_id = case["case_id"] if case else f"case-uncovered-{exec_key}"
            ex_owner = ledger.exception_owner(case_id)
            excess = notional - covered_proceeds
            lines += [ledger.Line(ledger.RECEIVABLE, ex_owner, ccy, excess), ledger.Line(ledger.SUSPENSE, ex_owner, ccy, -excess)]
            changes["lots"].append({"lot_id": f"lot:{exec_key}:short", "account_key": key, "instrument_id": inst_id,
                                    "owner": ex_owner, "currency": ccy, "open_qty": (-uncovered).to_str(),
                                    "open_cost": Fixed(0, scale).to_str(), "original_qty": (-uncovered).to_str(),
                                    "opened_by": exec_key, "opened_at_ns": p["exec_time_ns"]})
            if case:
                case["owner_hint"] = owner
                changes["exceptions"].append(case)
            anomalies.append({"issue": "uncovered sell", "qty": uncovered.to_str()})
        settlement = _settlement_row(ctx, key, owner, ccy, "RECEIVE", notional, exec_key, p["exec_time_ns"], settlement_days)
    trade_entry = ledger.entry(ctx.new_id("je-"), key, "TRADE", memo, lines)
    changes["journal"].append(trade_entry)
    effects["entries"].append(trade_entry["entry_id"])
    if fee_estimate is not None and fee_estimate.is_positive():
        fe = ledger.entry(ctx.new_id("je-"), key, "FEE_ESTIMATE", f"estimated fee {exec_key}",
                          [ledger.Line(ledger.FEE_ESTIMATE, owner, ccy, fee_estimate.amount),
                           ledger.Line(ledger.FEES_ACCRUED, owner, ccy, -fee_estimate.amount)])
        changes["journal"].append(fe)
        effects["entries"].append(fe["entry_id"])
    changes["settlements"].append(settlement)
    exec_row = {"exec_key": exec_key, "account_key": key, "order_id": order["order_id"] if order else None,
                "session_id": order["session_id"] if order else None, "owner": owner, "instrument_id": inst_id,
                "side": p["side"], "quantity": qty.to_str(), "price": price.to_str(), "currency": ccy,
                "notional": notional.to_str(), "raw_notional": raw_notional.to_str(), "exec_time_ns": p["exec_time_ns"],
                "provider_exec_id": p.get("provider_exec_id"), "correction_of": p.get("correction_of"),
                "corrected_by": None, "status": "ACTIVE",
                "fee_estimate": fee_estimate.amount.to_str() if fee_estimate else Fixed(0, scale).to_str(),
                "fee_final": None, "fee_status": "ESTIMATED", "effects": effects, "settlement_id": settlement["settlement_id"],
                "raw_economic": {k: p.get(k) for k in ("side", "quantity", "price", "instrument_id", "currency")},
                "raw": p.get("raw", {})}
    return changes, exec_row, anomalies, effects


def apply_order_execution(ctx: Context, order: dict, p: dict, *, allowance: Fixed | None = None) -> dict:
    """Book an execution against our order. ``allowance`` is the quantity a correction
    re-fills on an order that is already terminal (its original fill was reversed)."""
    key = order["account_key"]
    qty, price = _fx(p["quantity"]), _fx(p["price"])
    inst = state.instrument(ctx.tx, order["instrument_id"])
    mult = inst.multiplier if inst else Fixed(1)
    settlement_days = inst.settlement_days if inst else 1
    owner = economic_owner(ctx.tx, order["session_id"])
    ccy = order["currency"]
    scale = currency(ccy).ledger_scale
    policy = state.active_policy(ctx.tx, key)
    est = fee_model.estimate(policy.fees, qty, Money(ccy, (qty * price * mult).rescale(scale, Rounding.HALF_EVEN)))
    p = {**p, "instrument_id": order["instrument_id"], "currency": p.get("currency") or ccy}
    anomalies = []
    if p["side"] != order["side"]:
        anomalies.append({"issue": "side differs from order", "order_side": order["side"]})
    leaves = Fixed.parse(order["leaves_qty"])
    terminal = order["state"] in state.TERMINAL_ORDER_STATES
    if qty > leaves + (allowance or Fixed(0)):
        anomalies.append({"issue": "fill exceeds remaining quantity" if not terminal else "fill after terminal state",
                          "leaves": leaves.to_str(), "qty": qty.to_str()})
    limit = Fixed.parse(order["limit_price"])
    if (order["side"] == "BUY" and price > limit) or (order["side"] == "SELL" and price < limit):
        anomalies.append({"issue": "price outside limit", "limit": limit.to_str()})
    changes, exec_row, more, effects = _book_execution(ctx, p, owner=owner, order=order, settlement_days=settlement_days,
                                                       mult=mult, fee_estimate=est)
    anomalies += more
    cum = Fixed.parse(order["cum_qty"]) + qty
    new_leaves = leaves - qty if qty <= leaves else Fixed(0)
    filled_notional = Fixed.parse(order["notional_filled"]) + (qty * price)
    new = {**order, "cum_qty": cum.to_str(), "leaves_qty": new_leaves.to_str(),
           "notional_filled": filled_notional.to_str(), "avg_price": filled_notional.div(cum, 8, Rounding.HALF_EVEN).to_str(),
           "fees_estimated": (Fixed.parse(order["fees_estimated"]) + est.amount).rescale(scale).to_str()}
    release = False
    if not terminal:
        if new_leaves.is_zero():
            new.update(state="FILLED", prev_state=order["state"], terminal_at_ns=ctx.now_ns)
            release = True
        elif order["state"] in ("SUBMIT_PENDING", "UNKNOWN", "ACKNOWLEDGED", "RESERVED"):
            new.update(state="PARTIALLY_FILLED", prev_state=order["state"])
        pending = order.get("broker_terminal_pending")
        if pending and cum >= Fixed.parse(pending["filled"]) and not release:
            target = TERMINAL_MAP.get(pending["status"], "FILLED" if pending["status"] == "FILLED" else "CANCELED")
            if target == "FILLED" and not new_leaves.is_zero():
                target = "CANCELED"
            if target == "REJECTED" and cum.is_positive():
                target = "CANCELED"
            new.update(state=target, prev_state=order["state"], leaves_qty="0", terminal_at_ns=ctx.now_ns,
                       broker_terminal_pending=None, reason=pending.get("reason"),
                       cancel_state="CONFIRMED" if target == "CANCELED" else order["cancel_state"])
            release = True
    exceptions = _resolve_case(ctx, key, f"unknown-order:{order['order_id']}", "execution observed")
    changes["exceptions"] = changes.get("exceptions", []) + exceptions
    if anomalies:
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                             detail={"exec_key": p["exec_key"], "order_id": order["order_id"], "anomalies": anomalies},
                             key=f"exec-anomaly:{p['exec_key']}", session_id=order["session_id"],
                             order_id=order["order_id"], owner_hint=owner)
        if case:
            changes["exceptions"].append(case)
    changes["orders"] = [new]
    changes["holds"] = hold_rows(new, mult, release=release or new["state"] in state.TERMINAL_ORDER_STATES)
    changes["executions"] = [exec_row]
    ctx.emit("ExecutionRecorded", {"exec_key": p["exec_key"], "anomalies": anomalies, "owner": owner},
             changes, account_key=key, session_id=order["session_id"], order_id=order["order_id"],
             instrument_id=order["instrument_id"], event_time_ns=p["exec_time_ns"])
    _check_overcommit(ctx, key, owner, ccy, p["exec_key"], order)
    pending = ctx.tx.one("SELECT row FROM pending_commissions WHERE exec_key=?", (p["exec_key"],))
    if pending is not None:
        c = json.loads(pending["row"])
        _apply_commission(ctx, c, deferred=True)
    if owner.startswith("protected:"):
        _post_completion_notice(ctx, key, order["session_id"], f"execution {p['exec_key']} after completion", blocking=True)
    return {"exec_key": p["exec_key"], "duplicate": False, "anomalies": anomalies}


def _check_overcommit(ctx: Context, key: str, owner: str, ccy: str, exec_key: str, order: dict | None) -> None:
    if ledger.is_system_owner(owner) and ledger.available_cash(ctx.tx, key, owner, ccy).is_negative():
        case = new_exception(ctx, key, OVERCOMMITTED, blocking=True, severity="high",
                             detail={"owner": owner, "exec_key": exec_key,
                                     "available": ledger.available_cash(ctx.tx, key, owner, ccy).to_str()},
                             key=f"overcommitted:{owner}", session_id=order["session_id"] if order else None,
                             owner_hint=owner)
        if case:
            ctx.emit("OwnerOvercommitted", {"owner": owner}, {"exceptions": [case]}, account_key=key)


def _post_completion_notice(ctx: Context, key: str, session_id: str, what: str, *, blocking: bool) -> None:
    case = new_exception(ctx, key, POST_COMPLETION_ADJUSTMENT, blocking=blocking, severity="medium",
                         detail={"session_id": session_id, "what": what}, key=f"post-completion:{session_id}:{ctx.payload_hash[:12]}",
                         session_id=session_id, owner_hint=ledger.protected_owner(session_id))
    if case:
        ctx.emit("PostCompletionAdjustment", {"session_id": session_id, "what": what}, {"exceptions": [case]},
                 account_key=key, session_id=session_id)


def _external_execution(ctx: Context, p: dict, foreign: str | None) -> dict:
    key = p["account_key"]
    if foreign or p.get("order_ref"):
        reason = foreign or "order reference not issued by this deployment"
        case = new_exception(ctx, key, FOREIGN_ACTIVITY, blocking=True, severity="critical",
                             detail={"exec_key": p["exec_key"], "reason": reason}, key=f"foreign-exec:{p['exec_key']}",
                             owner_hint=ledger.EXTERNAL)
        kind = "ForeignExecutionRecorded"
        latch = True
    else:
        case = new_exception(ctx, key, "EXTERNAL_TRADE", blocking=False, severity="medium",
                             detail={"exec_key": p["exec_key"], "note": "manual trade attributed to the external owner"},
                             key=f"external-exec:{p['exec_key']}", owner_hint=ledger.EXTERNAL)
        kind = "ExternalExecutionRecorded"
        latch = False
    if not p.get("instrument_id") or state.instrument(ctx.tx, p["instrument_id"]) is None:
        case = case or new_exception(ctx, key, FOREIGN_ACTIVITY, blocking=True, severity="critical",
                                     detail={"exec_key": p["exec_key"]}, key=f"unmapped-exec:{p['exec_key']}")
        if case:
            case.update(blocking=True, detail={**case["detail"], "issue": "instrument not in master; economic booking deferred",
                                               "raw": p})
        ctx.emit("UnmappedExecutionRecorded", {"payload": p}, {"exceptions": [case] if case else [],
                                                              "control": [_latch_changes(ctx, key, "UNMAPPED_EXECUTION")]},
                 account_key=key)
        return {"exec_key": p["exec_key"], "unmapped": True}
    inst = state.instrument(ctx.tx, p["instrument_id"])
    changes, exec_row, anomalies, _ = _book_execution(ctx, p, owner=ledger.EXTERNAL, order=None,
                                                      settlement_days=inst.settlement_days, mult=inst.multiplier, fee_estimate=None)
    exec_row["fee_status"] = "EXTERNAL"
    if case:
        changes["exceptions"].append(case)
    changes["executions"] = [exec_row]
    if latch:
        changes["control"] = [_latch_changes(ctx, key, "FOREIGN_ACTIVITY")]
    ctx.emit(kind, {"exec_key": p["exec_key"], "anomalies": anomalies}, changes, account_key=key,
             instrument_id=p["instrument_id"], event_time_ns=p["exec_time_ns"])
    # Cover an external purchase from outside cash where possible (ADR-0006 external activity).
    if p["side"] == "BUY":
        ccy = p["currency"]
        need = -ledger.available_cash(ctx.tx, key, ledger.EXTERNAL, ccy)
        if need.is_positive():
            have = ledger.available_cash(ctx.tx, key, ledger.OUTSIDE, ccy)
            if have >= need:
                e = ledger.entry(ctx.new_id("je-"), key, "OWNER_TRANSFER", f"cover external purchase {p['exec_key']}",
                                 ledger.transfer(ledger.CASH, ccy, need, ledger.OUTSIDE, ledger.EXTERNAL))
                ctx.emit("ExternalPurchaseCovered", {"amount": need.to_str()}, {"journal": [e]}, account_key=key)
            else:
                c2 = new_exception(ctx, key, OVERCOMMITTED, blocking=True, severity="critical",
                                   detail={"owner": "external", "need": need.to_str(), "outside_available": have.to_str()},
                                   key=f"overcommitted:external:{p['exec_key']}", owner_hint=ledger.EXTERNAL)
                if c2:
                    ctx.emit("ExternalPurchaseUncovered", {}, {"exceptions": [c2]}, account_key=key)
    return {"exec_key": p["exec_key"], "external": True, "foreign": latch}


def _apply_correction(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    orig = state.execution(ctx.tx, p["correction_of"])
    if orig is None or orig["status"] != "ACTIVE":
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                             detail={"exec_key": p["exec_key"], "issue": "correction of unknown or inactive execution",
                                     "correction_of": p["correction_of"], "raw": p}, key=f"orphan-correction:{p['exec_key']}")
        ctx.emit("OrphanCorrectionRecorded", {"payload": p}, {"exceptions": [case] if case else []}, account_key=key)
        return {"exec_key": p["exec_key"], "orphan_correction": True}
    scale = currency(orig["currency"]).ledger_scale
    changes: dict = {"journal": [], "lots": [], "settlements": [], "executions": [], "orders": [], "holds": []}
    # 1. Reverse the original's journal entries.
    for entry_id in orig["effects"]["entries"] + orig.get("fee_entries", []):
        lines = [dict(r) for r in ctx.tx.all("SELECT account_code, owner, currency, instrument_id, amount FROM postings WHERE entry_id=?", (entry_id,))]
        changes["journal"].append(ledger.entry(ctx.new_id("je-"), key, "REVERSAL", f"reverse {entry_id} (correction {p['exec_key']})",
                                               ledger.reverse_lines(lines), reverses=entry_id))
    # 2. Undo lot effects.
    for lot_id in orig["effects"]["lots_opened"]:
        lot = json.loads(ctx.tx.one("SELECT row FROM lots WHERE lot_id=?", (lot_id,))["row"])
        if Fixed.parse(lot["open_qty"]) != Fixed.parse(lot["original_qty"]):
            case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                                 detail={"exec_key": p["exec_key"], "issue": "correction of a buy whose lot was already disposed",
                                         "raw": p}, key=f"correction-after-disposal:{p['exec_key']}", owner_hint=orig["owner"])
            ctx.emit("CorrectionRequiresReview", {"payload": p}, {"exceptions": [case] if case else []}, account_key=key)
            return {"exec_key": p["exec_key"], "requires_review": True}
        changes["lots"].append({**lot, "open_qty": "0", "open_cost": Fixed(0, scale).to_str()})
    for c in orig["effects"]["lots_consumed"]:
        lot = json.loads(ctx.tx.one("SELECT row FROM lots WHERE lot_id=?", (c["lot_id"],))["row"])
        changes["lots"].append({**lot, "open_qty": (Fixed.parse(lot["open_qty"]) + Fixed.parse(c["qty"])).to_str(),
                                "open_cost": (Fixed.parse(lot["open_cost"]) + Fixed.parse(c["cost"])).rescale(scale).to_str()})
    stl = json.loads(ctx.tx.one("SELECT row FROM settlements WHERE settlement_id=?", (orig["settlement_id"],))["row"])
    if stl["status"] != "EXPECTED":
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                             detail={"exec_key": p["exec_key"], "issue": "correction after settlement", "raw": p},
                             key=f"correction-after-settlement:{p['exec_key']}", owner_hint=orig["owner"])
        ctx.emit("CorrectionRequiresReview", {"payload": p}, {"exceptions": [case] if case else []}, account_key=key)
        return {"exec_key": p["exec_key"], "requires_review": True}
    changes["settlements"].append({**stl, "status": "CANCELLED", "cancelled_by": p["exec_key"]})
    changes["executions"].append({**orig, "status": "CORRECTED", "corrected_by": p["exec_key"]})
    order = state.order(ctx.tx, orig["order_id"]) if orig["order_id"] else None
    if order is not None:
        cum = Fixed.parse(order["cum_qty"]) - Fixed.parse(orig["quantity"])
        notional = Fixed.parse(order["notional_filled"]) - Fixed.parse(orig["quantity"]) * Fixed.parse(orig["price"])
        fees = Fixed.parse(order["fees_estimated"]) - Fixed.parse(orig["fee_estimate"])
        fees_final = Fixed.parse(order["fees_final"]) - (Fixed.parse(orig["fee_final"]) if orig.get("fee_final") else Fixed(0))
        order = {**order, "cum_qty": cum.to_str(), "notional_filled": notional.to_str(),
                 "fees_estimated": fees.rescale(scale).to_str(), "fees_final": fees_final.rescale(scale).to_str(),
                 "leaves_qty": order["leaves_qty"] if order["state"] in state.TERMINAL_ORDER_STATES
                 else (Fixed.parse(order["leaves_qty"]) + Fixed.parse(orig["quantity"])).to_str()}
        changes["orders"].append(order)
    ctx.emit("ExecutionCorrectionReversed", {"original": orig["exec_key"], "correction": p["exec_key"]}, changes,
             account_key=key, order_id=orig["order_id"], session_id=orig.get("session_id"), instrument_id=orig["instrument_id"])
    p2 = {**p, "instrument_id": p.get("instrument_id") or orig["instrument_id"], "currency": p.get("currency") or orig["currency"]}
    if order is not None:
        current = state.order(ctx.tx, order["order_id"])
        allowance = Fixed.parse(orig["quantity"]) if current["state"] in state.TERMINAL_ORDER_STATES else None
        result = apply_order_execution(ctx, current, p2, allowance=allowance)
    else:
        result = _external_execution(ctx, {**p2, "order_ref": None}, None)
    return {**result, "corrected": orig["exec_key"]}


# -------------------------------------------------------------- commission
def broker_commission(ctx: Context, p: dict) -> dict:
    return _apply_commission(ctx, p, deferred=False)


def _apply_commission(ctx: Context, p: dict, *, deferred: bool) -> dict:
    key = p["account_key"]
    exec_key = p["exec_key"]
    ex = state.execution(ctx.tx, exec_key)
    if ex is None:
        ctx.emit("CommissionDeferred", {"exec_key": exec_key, "note": "commission arrived before its execution"},
                 {"pending_commissions": [{"exec_key": exec_key, "account_key": key, **{k: p[k] for k in ("amount", "currency")},
                                           "raw": p.get("raw", {})}]}, account_key=key)
        return {"deferred": True}
    ccy = p["currency"]
    if ccy != ex["currency"]:
        raise Rejected("CURRENCY", "commission currency differs from execution currency (FX fees not released)")
    scale = currency(ccy).ledger_scale
    amount = _fx(p["amount"]).rescale(scale, Rounding.HALF_EVEN)
    owner = economic_owner(ctx.tx, ex["session_id"]) if ex["session_id"] else ledger.EXTERNAL
    changes: dict = {"journal": [], "executions": [], "settlements": [], "orders": [], "holds": [], "exceptions": []}
    if deferred:
        changes["pending_commissions"] = [{"exec_key": exec_key, "account_key": key, "delete": True}]
    against = ledger.PAYABLE if ex["side"] == "BUY" else ledger.RECEIVABLE
    prev_final = Fixed.parse(ex["fee_final"]) if ex.get("fee_final") is not None else None
    if prev_final is not None:
        if prev_final == amount:
            return {"duplicate": True}
        delta = amount - prev_final
        lines = [ledger.Line(ledger.COMMISSION, owner, ccy, delta), ledger.Line(against, owner, ccy, -delta)]
        kind, info = "CommissionAdjusted", {"previous": prev_final.to_str(), "new": amount.to_str()}
    else:
        est = Fixed.parse(ex["fee_estimate"])
        lines = []
        if est.is_positive():
            lines += [ledger.Line(ledger.FEES_ACCRUED, owner, ccy, est), ledger.Line(ledger.FEE_ESTIMATE, owner, ccy, -est)]
        lines += [ledger.Line(ledger.COMMISSION, owner, ccy, amount), ledger.Line(against, owner, ccy, -amount)]
        delta = amount
        kind, info = "CommissionFinalized", {"estimate": est.to_str(), "final": amount.to_str()}
    e = ledger.entry(ctx.new_id("je-"), key, "COMMISSION", f"commission {exec_key}", lines)
    changes["journal"].append(e)
    changes["executions"].append({**ex, "fee_final": amount.to_str(), "fee_status": "FINAL",
                                  "fee_entries": ex.get("fee_entries", []) + [e["entry_id"]]})
    stl = json.loads(ctx.tx.one("SELECT row FROM settlements WHERE settlement_id=?", (ex["settlement_id"],))["row"])
    if stl["status"] == "EXPECTED":
        new_amount = Fixed.parse(stl["amount"]) + (delta if ex["side"] == "BUY" else -delta)
        changes["settlements"].append({**stl, "amount": new_amount.rescale(scale).to_str()})
    else:
        changes["settlements"].append(_settlement_row(ctx, key, owner, ccy, "PAY" if delta.is_positive() else "RECEIVE",
                                                      abs(delta), f"{exec_key}:fee:{ctx.payload_hash[:8]}", ctx.now_ns, 1))
    if ex.get("order_id"):
        order = state.order(ctx.tx, ex["order_id"])
        if order is not None:
            new = {**order, "fees_final": (Fixed.parse(order["fees_final"]) + delta).rescale(scale).to_str()}
            changes["orders"].append(new)
            mult = multiplier_of(ctx, order["instrument_id"])
            changes["holds"] = hold_rows(new, mult, release=new["state"] in state.TERMINAL_ORDER_STATES)
    ctx.emit(kind, {"exec_key": exec_key, **info, "owner": owner}, changes, account_key=key,
             order_id=ex.get("order_id"), session_id=ex.get("session_id"))
    if owner.startswith("protected:") and ex.get("session_id"):
        _post_completion_notice(ctx, key, ex["session_id"], f"{kind} for {exec_key}",
                                blocking=ledger.available_cash(ctx.tx, key, owner, ccy).is_negative())
    _check_overcommit(ctx, key, owner, ccy, exec_key, None)
    return {"exec_key": exec_key, "status": "FINAL", "amount": amount.to_str()}


# ------------------------------------------------------------ connectivity
def broker_connectivity(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    c = state.control(ctx.tx, key)
    connected, authenticated = bool(p.get("connected")), bool(p.get("authenticated"))
    if c["broker_connected"] == connected and c["broker_authenticated"] == authenticated:
        return {"changed": False}
    new = {**c, "broker_connected": connected, "broker_authenticated": authenticated}
    if not connected or not authenticated:
        # Activity may have occurred while disconnected: require a fresh reconciliation.
        new["needs_reconciliation"] = True
    ctx.emit("BrokerConnectivityChanged", {"connected": connected, "authenticated": authenticated,
                                           "detail": p.get("detail", "")}, {"control": [new]}, account_key=key)
    return {"changed": True}
