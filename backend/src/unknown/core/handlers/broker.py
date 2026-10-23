"""Broker observations (ADR-0006 observation path, ADR-0009).

Observations always record the economic fact. When a fact violates an
expected invariant (unknown order, overfill, price outside limit, late fill,
changed content under one execution id, foreign activity, unexpected fee
currency) the handler records it, opens an exception that blocks new exposure,
and never discards it. Observation handlers do not raise ``Rejected``.

Ledger conventions used here (fixes from the 2026-10-22 review):
* every settlement row records the account code it relieves (PAYABLE or
  RECEIVABLE) and its owner; a trade touching several owners gets one row per
  owner, so settlement moves cash to exactly the owners whose claims it clears;
* buy fee estimates accrue to FEES_ACCRUED (covered by the order's fee
  envelope hold); sell fee estimates accrue against the sale's RECEIVABLE, so
  an exit never reduces settled-cash availability;
* after completion every later effect (commission, correction, restatement)
  posts to the owner that holds the session's balances now, protected:<id> or
  unallocated, so the change is a visible delta there (ADR-0008)."""
from __future__ import annotations

import json
from datetime import date

from ...domain.calendar import local_date
from ...domain.fixed import Fixed, Rounding
from ...domain.money import Money, currency
from ...risk import fees as fee_model
from .. import ledger, state
from ..desk import Context
from .common import (EXECUTION_ANOMALY, FOREIGN_ACTIVITY, OVERCOMMITTED, POST_COMPLETION_ADJUSTMENT, UNKNOWN_ORDER,
                     hold_rows, multiplier_of, new_exception, order_owner)

TERMINAL_MAP = {"CANCELED": "CANCELED", "EXPIRED": "EXPIRED", "REJECTED": "REJECTED"}
WORKING_STATUSES = ("PENDING_SUBMIT", "ACKNOWLEDGED", "PARTIALLY_FILLED", "PENDING_CANCEL")
# Not working and not final (IBKR Inactive): the order is held as UNKNOWN with every reservation (order_machine.py).
HELD_STATUSES = ("INACTIVE",)
ECONOMIC_FIELDS = ("side", "quantity", "price", "instrument_id", "currency")


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


def current_owner(tx, owner: str) -> str:
    """Map an original owner to whoever holds its balances now (session -> protected/unallocated)."""
    if owner.startswith("session:"):
        return economic_owner(tx, owner.split(":", 1)[1])
    return owner


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


def latch(ctx: Context, account_key: str, reason: str) -> None:
    """Latch the stop through the one shared path, which also cancels open buys (ADR-0010)."""
    from .control import latch_stop
    latch_stop(ctx, account_key, reason)


# ----------------------------------------------------------------- status
def broker_order_status(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    order, foreign = _find_order(ctx, key, p.get("client_order_id"), p.get("order_ref"), p.get("broker_order_id"))
    if order is None:
        case = new_exception(ctx, key, FOREIGN_ACTIVITY, blocking=True, severity="critical",
                             detail={"observation": "order status", "payload": p, "reason": foreign or "not our order"},
                             key=f"foreign-order:{p.get('broker_order_id')}")
        ctx.emit("ForeignOrderObserved", {"payload": p}, {"exceptions": [case] if case else []}, account_key=key)
        latch(ctx, key, "FOREIGN_ACTIVITY")
        return {"foreign": True}
    return apply_status(ctx, order, p["status"], _fx(p.get("filled_quantity", "0")), p.get("reason", ""),
                        p.get("broker_order_id"), via="status")


def apply_status(ctx: Context, order: dict, status: str, broker_filled: Fixed, reason: str,
                 broker_order_id: str | None, via: str) -> dict:
    key = order["account_key"]
    refs = dict(account_key=key, session_id=order["session_id"], order_id=order["order_id"])
    local = Fixed.parse(order["cum_qty"])
    new = {**order}
    if broker_order_id and not order.get("broker_order_id"):
        new["broker_order_id"] = broker_order_id
    # A working report says nothing about a cancel whose outcome is unknown (it may never have reached the broker):
    # only a terminal or PENDING_CANCEL report resolves that case (review finding 35). A held (Inactive) report
    # resolves nothing: it is itself an outcome the core cannot rely on.
    if (order.get("cancel_state") == "UNKNOWN" and status in ("PENDING_SUBMIT", "ACKNOWLEDGED", "PARTIALLY_FILLED")) \
            or status in HELD_STATUSES:
        resolved = []
    else:
        resolved = _resolve_case(ctx, key, f"unknown-order:{order['order_id']}", f"broker reported {status}")
    if status not in WORKING_STATUSES and status != "FILLED" and status not in TERMINAL_MAP and status not in HELD_STATUSES:
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="medium",
                             detail={"order_id": order["order_id"], "issue": f"unrecognised broker status {status!r}"},
                             key=f"status:{order['order_id']}:{status}", session_id=order["session_id"], order_id=order["order_id"])
        ctx.emit("UnrecognisedOrderStatus", {"status": status}, {"exceptions": [case] if case else []}, **refs)
        return {"state": order["state"], "anomaly": True}
    if order["state"] in state.TERMINAL_ORDER_STATES:
        if broker_filled > local:
            pending = {"status": status, "filled": broker_filled.to_str(), "reason": reason}
            ctx.emit("TerminalOrderAwaitingExecutions", {"status": status},
                     {"orders": [{**new, "broker_terminal_pending": pending}], "exceptions": resolved}, **refs)
        if (status in WORKING_STATUSES or status in HELD_STATUSES) and via == "reconciliation":
            # The broker's open-order list still shows an order we consider finished (its reservation is
            # released): block, and pull it if it adds exposure. Streamed statuses can arrive stale, so only
            # the reconciliation snapshot is evidence here (review finding 36). A held (Inactive) order counts:
            # it is not final and may work again (order_machine.py), e.g. one the owner closed on evidence.
            case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                                 detail={"order_id": order["order_id"], "issue": "broker lists a locally terminal order as working",
                                         "local_state": order["state"], "broker_status": status},
                                 key=f"working-after-terminal:{order['order_id']}", session_id=order["session_id"],
                                 order_id=order["order_id"])
            if case:
                ctx.emit("TerminalOrderStillWorking", {"status": status}, {"exceptions": [case]}, **refs)
            if order["side"] == "BUY" and status != "PENDING_CANCEL":
                from .orders import enqueue_cancel
                enqueue_cancel(ctx, state.order(ctx.tx, order["order_id"]), "broker still working a locally terminal buy")
        return {"state": order["state"], "noop": True}
    if status in HELD_STATUSES:
        return _hold_at_broker(ctx, new, status, broker_filled, reason, via)
    if status in WORKING_STATUSES:
        if order["state"] in ("SUBMIT_PENDING", "UNKNOWN", "RESERVED"):
            new.update(state="PARTIALLY_FILLED" if local.is_positive() else "ACKNOWLEDGED", prev_state=order["state"])
        if new.get("broker_held"):
            new["broker_held"] = None   # working again: no longer held
        if status == "PENDING_CANCEL" and new["state"] != "CANCEL_PENDING":
            new.update(state="CANCEL_PENDING", prev_state=new["state"])
        if new != order or resolved:
            ctx.emit("OrderStatusObserved", {"status": status, "via": via}, {"orders": [new], "exceptions": resolved}, **refs)
        return {"state": new["state"]}
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


def _hold_at_broker(ctx: Context, order: dict, status: str, broker_filled: Fixed, reason: str, via: str) -> dict:
    """The broker reports the order exists but is not working and does not say it is final (IBKR Inactive).

    Decision (BR-ORD-07, order_machine.py): such an order is an outcome the core cannot rely on, like a timeout.
    It becomes UNKNOWN, keeps every hold, opens the blocking ``unknown-order:<id>`` case and, for a buy, queues a
    cancel so it cannot come back to life unattended. A later broker terminal report or the owner's
    ``close_unknown_order`` resolution on evidence ends it; nothing is released here."""
    key = order["account_key"]
    refs = dict(account_key=key, session_id=order["session_id"], order_id=order["order_id"])
    held = {"status": status, "reason": reason, "broker_filled": broker_filled.to_str(), "via": via}
    prior = order.get("broker_held") or {}
    if order["state"] == "UNKNOWN" and {k: prior.get(k) for k in held} == held and not _reconciled_since(ctx, key, prior):
        return {"state": "UNKNOWN", "held": True, "noop": True}   # the same report again, no reconciliation since
    # ``after_seq``: the record follows every event up to this seq. The same report after a later reconciliation is
    # recorded again, so close_unknown_order never relies on a run older than the broker's last word on the order.
    after_seq = ctx.tx.one("SELECT MAX(seq) AS s FROM events")["s"] or 0
    new = {**order, "broker_held": {**held, "at_ns": ctx.now_ns, "after_seq": after_seq}}
    if order["state"] != "UNKNOWN":
        new.update(state="UNKNOWN", prev_state=order["state"])
    case = new_exception(ctx, key, UNKNOWN_ORDER, blocking=True, severity="high",
                         detail={"order_id": order["order_id"], "cause": status, "reason": reason,
                                 "broker_filled": broker_filled.to_str(),
                                 "rule": "held at the broker and not working: every hold is kept until a broker terminal "
                                         "report, or the owner's close_unknown_order on broker evidence"},
                         key=f"unknown-order:{order['order_id']}", session_id=order["session_id"], order_id=order["order_id"])
    ctx.emit("OrderHeldAtBroker", {"status": status, "reason": reason, "via": via,
                                   "note": "not working and not final: reservation kept, order treated as UNKNOWN"},
             {"orders": [new], "exceptions": [case] if case else []}, **refs)
    if order["side"] == "BUY":
        from .orders import cancel_order
        cancel_order(ctx, state.order(ctx.tx, order["order_id"]), f"broker reports the buy {status}")
    return {"state": "UNKNOWN", "held": True}


def _reconciled_since(ctx: Context, key: str, held: dict) -> bool:
    """Whether the account's latest reconciliation completed after ``held`` (a broker_held record) was written."""
    rec = state.latest_reconciliation(ctx.tx, key)
    if rec is None:
        return False
    return rec.get("event_seq_at_completion", rec.get("event_seq_at_start", 0)) >= held.get("after_seq", 0)


# -------------------------------------------------------------- executions
def broker_execution(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    exec_key = p["exec_key"]
    raw_economic = {k: p.get(k) for k in ECONOMIC_FIELDS}
    existing = state.execution(ctx.tx, exec_key)
    if existing is not None:
        if existing.get("raw_economic") == raw_economic or {k: existing.get(k) for k in ECONOMIC_FIELDS} == raw_economic:
            return {"exec_key": exec_key, "duplicate": True}
        # Decision (BR-ORD-05, BR-REF-21): the booked original stands and nothing of the amended content is booked.
        # The received payload is recorded in full and a blocking critical case, attributed to the execution's
        # order, session and current owner, holds new exposure until the owner resolves it. A genuine amendment
        # reaches the ledger as a broker correction (a new execution id) or as a reconciliation difference; the
        # same id is never silently re-booked, and a redelivery of the amended payload adds no second case.
        holder = current_owner(ctx.tx, existing["owner"]) if existing.get("owner") else None
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="critical",
                             detail={"exec_key": exec_key, "issue": "same execution id with changed content",
                                     "stored": existing.get("raw_economic"), "received": raw_economic,
                                     "decision": "the original stays booked; the amended content is recorded, not booked, "
                                                 "until the owner resolves this case"},
                             key=f"exec-changed:{exec_key}:{ctx.payload_hash[:12]}", session_id=existing.get("session_id"),
                             order_id=existing.get("order_id"), owner_hint=holder)
        ctx.emit("ExecutionContentConflict", {"exec_key": exec_key, "received": p}, {"exceptions": [case] if case else []},
                 account_key=key, session_id=existing.get("session_id"), order_id=existing.get("order_id"))
        return {"exec_key": exec_key, "conflict": True}
    p = {**p, "_raw_economic": raw_economic}
    if not state.control(ctx.tx, key).get("imported"):
        # Already reflected in the opening snapshot about to be imported: record, never book twice.
        ctx.emit("PreImportExecutionRecorded", {"exec_key": exec_key, "note": "covered by the opening balance import"},
                 {"executions": [{"exec_key": exec_key, "account_key": key, "order_id": None, "session_id": None,
                                  "owner": ledger.OUTSIDE, "instrument_id": p.get("instrument_id"), "side": p.get("side"),
                                  "quantity": p.get("quantity"), "price": p.get("price"), "currency": p.get("currency"),
                                  "exec_time_ns": p.get("exec_time_ns"), "status": "PRE_IMPORT", "fee_status": "PRE_IMPORT",
                                  "raw_economic": raw_economic, "effects": {"entries": [], "lots_opened": [], "lots_consumed": []},
                                  "settlement_ids": [], "fee_estimate": "0", "fee_final": None, "corrected_by": None,
                                  "correction_of": p.get("correction_of")}]}, account_key=key)
        return {"exec_key": exec_key, "pre_import": True}
    if p.get("correction_of"):
        return _apply_correction(ctx, p)
    order, foreign = _find_order(ctx, key, p.get("client_order_id"), p.get("order_ref"), p.get("broker_order_id"))
    if order is None:
        return _external_execution(ctx, p, foreign)
    return apply_order_execution(ctx, order, p)


def _settle_calendar(ctx: Context, account_key: str):
    policy = state.active_policy(ctx.tx, account_key)
    return policy.limits.risk_day_timezone, ctx.desk.calendars.get(policy.settlement_calendar, ctx.desk.calendars["WEEKDAYS"])


def settlement_row(ctx: Context, account_key: str, owner: str, ccy: str, direction: str, amount: Fixed, relieves: str,
                   settlement_id: str, exec_key: str | None, trade_time_ns: int, settlement_days: int) -> dict:
    tz, cal = _settle_calendar(ctx, account_key)
    trade = local_date(trade_time_ns, tz)
    expected = cal.add_business_days(trade, settlement_days)
    return {"settlement_id": settlement_id, "account_key": account_key, "owner": owner, "currency": ccy,
            "direction": direction, "amount": amount.to_str(), "relieves": relieves, "trade_date": trade.isoformat(),
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
    settlements: list[dict] = []
    memo = f"{p['side']} {qty} {inst_id} @ {price} ({exec_key})"
    if p["side"] == "BUY":
        lot_id = f"lot:{exec_key}"
        changes["lots"].append({"lot_id": lot_id, "account_key": key, "instrument_id": inst_id, "owner": owner,
                                "currency": ccy, "open_qty": qty.to_str(), "open_cost": notional.to_str(),
                                "original_qty": qty.to_str(), "opened_by": exec_key, "opened_at_ns": p["exec_time_ns"]})
        lines = [ledger.Line(ledger.INVENTORY, owner, ccy, notional, inst_id), ledger.Line(ledger.PAYABLE, owner, ccy, -notional)]
        effects["lots_opened"].append(lot_id)
        settlements.append(settlement_row(ctx, key, owner, ccy, "PAY", notional, ledger.PAYABLE, f"stl:{exec_key}", exec_key,
                                          p["exec_time_ns"], settlement_days))
    else:
        rows, consumed, cost, uncovered = _consume_lots(ctx, key, owner, inst_id, qty, scale)
        changes["lots"].extend(rows)
        effects["lots_consumed"] = consumed
        covered_qty = qty - uncovered
        covered_proceeds = (covered_qty * price * mult).rescale(scale, Rounding.HALF_EVEN) if uncovered.is_positive() else notional
        lines = [ledger.Line(ledger.RECEIVABLE, owner, ccy, covered_proceeds),
                 ledger.Line(ledger.INVENTORY, owner, ccy, -cost, inst_id),
                 ledger.Line(ledger.REALIZED, owner, ccy, -(covered_proceeds - cost))]
        if covered_proceeds.is_positive():
            settlements.append(settlement_row(ctx, key, owner, ccy, "RECEIVE", covered_proceeds, ledger.RECEIVABLE,
                                              f"stl:{exec_key}", exec_key, p["exec_time_ns"], settlement_days))
        if uncovered.is_positive():
            case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="critical",
                                 detail={"exec_key": exec_key, "issue": "sell exceeds owned quantity", "uncovered": uncovered.to_str(),
                                         "owner": owner}, key=f"uncovered-sell:{exec_key}")
            case_id = case["case_id"] if case else f"case-uncovered-{exec_key}"
            ex_owner = ledger.exception_owner(case_id)
            excess = notional - covered_proceeds
            lines += [ledger.Line(ledger.RECEIVABLE, ex_owner, ccy, excess), ledger.Line(ledger.SUSPENSE, ex_owner, ccy, -excess)]
            short_id = f"lot:{exec_key}:short"
            changes["lots"].append({"lot_id": short_id, "account_key": key, "instrument_id": inst_id,
                                    "owner": ex_owner, "currency": ccy, "open_qty": (-uncovered).to_str(),
                                    "open_cost": Fixed(0, scale).to_str(), "original_qty": (-uncovered).to_str(),
                                    "opened_by": exec_key, "opened_at_ns": p["exec_time_ns"]})
            effects["lots_opened"].append(short_id)
            # The excess proceeds settle to the quarantine owner, never to the selling owner (review finding 6).
            settlements.append(settlement_row(ctx, key, ex_owner, ccy, "RECEIVE", excess, ledger.RECEIVABLE,
                                              f"stl:{exec_key}:excess", exec_key, p["exec_time_ns"], settlement_days))
            if case:
                case["owner_hint"] = owner
                changes["exceptions"].append(case)
            anomalies.append({"issue": "uncovered sell", "qty": uncovered.to_str()})
    trade_entry = ledger.entry(ctx.new_id("je-"), key, "TRADE", memo, lines)
    changes["journal"].append(trade_entry)
    effects["entries"].append(trade_entry["entry_id"])
    estimate_code = None
    if fee_estimate is not None and fee_estimate.is_positive():
        estimate_code = ledger.FEES_ACCRUED if p["side"] == "BUY" else ledger.RECEIVABLE
        fe = ledger.entry(ctx.new_id("je-"), key, "FEE_ESTIMATE", f"estimated fee {exec_key}",
                          [ledger.Line(ledger.FEE_ESTIMATE, owner, ccy, fee_estimate.amount),
                           ledger.Line(estimate_code, owner, ccy, -fee_estimate.amount)])
        changes["journal"].append(fe)
        effects["entries"].append(fe["entry_id"])
        if estimate_code == ledger.RECEIVABLE:
            # The sale's expected cash is net of the estimated fee, matching its receivable.
            main = next((s for s in settlements if s["settlement_id"] == f"stl:{exec_key}"), None)
            if main is None:
                main = settlement_row(ctx, key, owner, ccy, "RECEIVE", Fixed(0, scale), ledger.RECEIVABLE, f"stl:{exec_key}",
                                      exec_key, p["exec_time_ns"], settlement_days)
                settlements.insert(0, main)
            signed = Fixed.parse(main["amount"]) - fee_estimate.amount
            main.update(direction="RECEIVE" if not signed.is_negative() else "PAY", amount=abs(signed).rescale(scale).to_str())
    changes["settlements"].extend(settlements)
    exec_row = {"exec_key": exec_key, "account_key": key, "order_id": order["order_id"] if order else None,
                "session_id": order["session_id"] if order else None, "owner": owner, "instrument_id": inst_id,
                "side": p["side"], "quantity": qty.to_str(), "price": price.to_str(), "currency": ccy,
                "notional": notional.to_str(), "raw_notional": raw_notional.to_str(), "exec_time_ns": p["exec_time_ns"],
                "provider_exec_id": p.get("provider_exec_id"), "correction_of": p.get("correction_of"),
                "corrected_by": None, "status": "ACTIVE",
                "fee_estimate": fee_estimate.amount.to_str() if fee_estimate else Fixed(0, scale).to_str(),
                "fee_estimate_code": estimate_code,
                "fee_final": None, "fee_status": "ESTIMATED", "effects": effects,
                "settlement_id": settlements[0]["settlement_id"] if settlements else None,
                "settlement_ids": [s["settlement_id"] for s in settlements],
                "raw_economic": p.get("_raw_economic") or {k: p.get(k) for k in ECONOMIC_FIELDS},
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
    # A disposal books to the owner it was accepted for; any other order to whoever holds its session's balances now.
    owner = order.get("owner") or economic_owner(ctx.tx, order["session_id"])
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
    check_overcommit(ctx, key, owner, ccy, p["exec_key"], order["session_id"])
    pending = ctx.tx.one("SELECT row FROM pending_commissions WHERE exec_key=?", (p["exec_key"],))
    if pending is not None:
        _apply_commission(ctx, json.loads(pending["row"]), deferred=True)
    if _after_session_end(ctx, order) and (order.get("purpose") != "DISPOSAL" or p.get("correction_of")):
        # A late fill or a correction for an ended session: booked to protected:<id> or unallocated (above), and a
        # blocking case for the owner (ADR-0008). An owner disposal's own fills are expected activity, not a case;
        # a broker correction of one is not.
        _post_completion_notice(ctx, key, order["session_id"], f"execution {p['exec_key']} after the session ended",
                                blocking=True, ref=p["exec_key"])
    s = state.session(ctx.tx, order["session_id"]) if order["session_id"] else None
    if s is not None and s["state"] == "RECONCILING":
        _late_activity_notice(ctx, key, s, f"execution {p['exec_key']} while reconciling")
    return {"exec_key": p["exec_key"], "duplicate": False, "anomalies": anomalies}


def check_overcommit(ctx: Context, key: str, owner: str, ccy: str, ref: str, session_id: str | None) -> None:
    """A system owner whose available cash went negative (a fill or fee beyond its reservation) hands the
    deficit to ``exception:<case>`` so the owner stays non-negative and the shortfall is visible and
    blocking until the owner funds it (``cover_shortfall``, ADR-0006)."""
    if not ledger.is_system_owner(owner):
        return
    available = ledger.available_cash(ctx.tx, key, owner, ccy)
    if not available.is_negative():
        return
    case = new_exception(ctx, key, OVERCOMMITTED, blocking=True, severity="high",
                         detail={"owner": owner, "ref": ref, "currency": ccy, "shortfall": (-available).to_str(),
                                 "resolution": "cover_shortfall funds the exception owner from unallocated, buffer or outside cash"},
                         key=f"overcommitted:{owner}", session_id=session_id, owner_hint=owner)
    open_case = case or state.exception_by_key(ctx.tx, key, f"overcommitted:{owner}")
    ex_owner = ledger.exception_owner(open_case["case_id"])
    e = ledger.entry(ctx.new_id("je-"), key, "OWNER_TRANSFER", f"quarantine shortfall of {owner} ({ref})",
                     ledger.transfer(ledger.CASH, ccy, -available, ex_owner, owner))
    if case is None:
        detail = open_case["detail"]
        prior = Fixed.parse(detail.get("shortfall", "0"))
        case = {**open_case, "detail": {**detail, "shortfall": (prior + -available).to_str(), "ref": ref}}
    ctx.emit("OwnerOvercommitted", {"owner": owner, "shortfall": (-available).to_str(), "case_id": case["case_id"]},
             {"exceptions": [case], "journal": [e]}, account_key=key, session_id=session_id)


def _after_session_end(ctx: Context, order: dict | None) -> bool:
    """Whether the order's session has ended (COMPLETED_*, CLOSED or CANCELLED): its balances live elsewhere now."""
    sid = (order or {}).get("session_id")
    return bool(sid) and economic_owner(ctx.tx, sid) != ledger.session_owner(sid)


def _post_completion_notice(ctx: Context, key: str, session_id: str, what: str, *, blocking: bool, ref: str) -> None:
    """One case per execution with a late effect on an ended session (``ref``: its execution key), naming the owner
    that bore it: protected:<id> after completion, unallocated after close (ADR-0008). Later effects of the same
    execution (its commission, an adjustment) join the open case, escalating it to blocking when they must block.
    The session's state, outcome and successor never change."""
    s = state.session(ctx.tx, session_id)
    holder = economic_owner(ctx.tx, session_id)
    case_key = f"post-completion:{session_id}:{ref}"
    existing = state.exception_by_key(ctx.tx, key, case_key)
    if existing is not None:
        if blocking and not existing["blocking"]:
            ctx.emit("PostCompletionAdjustment", {"session_id": session_id, "what": what, "booked_to": holder, "escalated": True},
                     {"exceptions": [{**existing, "blocking": True, "detail": {**existing["detail"], "escalated_by": what}}]},
                     account_key=key, session_id=session_id)
        return
    case = new_exception(ctx, key, POST_COMPLETION_ADJUSTMENT, blocking=blocking, severity="medium",
                         detail={"session_id": session_id, "what": what, "session_state": s["state"], "booked_to": holder,
                                 "resolution": "review the effect; dispose of any inventory it left with a DISPOSAL intent"},
                         key=case_key, session_id=session_id, owner_hint=holder)
    ctx.emit("PostCompletionAdjustment", {"session_id": session_id, "what": what, "booked_to": holder},
             {"exceptions": [case]}, account_key=key, session_id=session_id)


def _late_activity_notice(ctx: Context, key: str, session: dict, what: str) -> None:
    case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="medium",
                         detail={"session_id": session["session_id"], "what": what,
                                 "note": "economic activity after the session was flat; wind-down resumes"},
                         key=f"late-activity:{session['session_id']}:{ctx.payload_hash[:12]}",
                         session_id=session["session_id"], owner_hint=ledger.session_owner(session["session_id"]))
    if case:
        ctx.emit("LateSessionActivity", {"what": what}, {"exceptions": [case]}, account_key=key, session_id=session["session_id"])


def cover_external(ctx: Context, key: str, ccy: str, ref: str) -> None:
    """External (manual) activity draws on the owner's unauthorized cash first (ADR-0006)."""
    need = -ledger.available_cash(ctx.tx, key, ledger.EXTERNAL, ccy)
    if not need.is_positive():
        return
    have = ledger.available_cash(ctx.tx, key, ledger.OUTSIDE, ccy)
    if have >= need:
        e = ledger.entry(ctx.new_id("je-"), key, "OWNER_TRANSFER", f"cover external activity {ref}",
                         ledger.transfer(ledger.CASH, ccy, need, ledger.OUTSIDE, ledger.EXTERNAL))
        ctx.emit("ExternalActivityCovered", {"amount": need.to_str(), "ref": ref}, {"journal": [e]}, account_key=key)
        return
    case = new_exception(ctx, key, OVERCOMMITTED, blocking=True, severity="critical",
                         detail={"owner": "external", "currency": ccy, "need": need.to_str(), "outside_available": have.to_str(),
                                 "ref": ref}, key="overcommitted:external", owner_hint=ledger.EXTERNAL)
    existing = None if case else state.exception_by_key(ctx.tx, key, "overcommitted:external")
    if existing is not None:
        case = {**existing, "detail": {**existing["detail"], "need": need.to_str(), "outside_available": have.to_str(), "ref": ref}}
    ctx.emit("ExternalActivityUncovered", {"ref": ref, "need": need.to_str()}, {"exceptions": [case]}, account_key=key)


def _external_execution(ctx: Context, p: dict, foreign: str | None) -> dict:
    key = p["account_key"]
    if foreign or p.get("order_ref"):
        reason = foreign or "order reference not issued by this deployment"
        case = new_exception(ctx, key, FOREIGN_ACTIVITY, blocking=True, severity="critical",
                             detail={"exec_key": p["exec_key"], "reason": reason}, key=f"foreign-exec:{p['exec_key']}",
                             owner_hint=ledger.EXTERNAL)
        kind, is_foreign = "ForeignExecutionRecorded", True
    else:
        case = new_exception(ctx, key, "EXTERNAL_TRADE", blocking=False, severity="medium",
                             detail={"exec_key": p["exec_key"], "note": "manual trade attributed outside the system's sessions"},
                             key=f"external-exec:{p['exec_key']}", owner_hint=ledger.EXTERNAL)
        kind, is_foreign = "ExternalExecutionRecorded", False
    if not p.get("instrument_id") or state.instrument(ctx.tx, p["instrument_id"]) is None:
        case = case or new_exception(ctx, key, FOREIGN_ACTIVITY, blocking=True, severity="critical",
                                     detail={"exec_key": p["exec_key"]}, key=f"unmapped-exec:{p['exec_key']}")
        if case:
            case.update(blocking=True, detail={**case["detail"], "issue": "instrument not in master; economic booking deferred",
                                               "raw": p})
        ctx.emit("UnmappedExecutionRecorded", {"payload": {k: v for k, v in p.items() if k != "_raw_economic"}},
                 {"exceptions": [case] if case else []}, account_key=key)
        latch(ctx, key, "UNMAPPED_EXECUTION")
        return {"exec_key": p["exec_key"], "unmapped": True}
    inst = state.instrument(ctx.tx, p["instrument_id"])
    owner = ledger.EXTERNAL
    if p["side"] == "SELL":
        qty = _fx(p["quantity"])
        # A manual sale relieves the owner's own (outside) holdings first, then earlier external buys.
        # Session-held shares are never taken implicitly (review finding 30).
        if ledger.owned_quantity(ctx.tx, key, ledger.OUTSIDE, p["instrument_id"]) >= qty:
            owner = ledger.OUTSIDE
        elif ledger.owned_quantity(ctx.tx, key, ledger.EXTERNAL, p["instrument_id"]) < qty:
            held = [r["owner"] for r in ctx.tx.all("SELECT DISTINCT owner FROM lots WHERE account_key=? AND instrument_id=? "
                                                   "AND owner LIKE 'session:%'", (key, p["instrument_id"]))]
            if held:
                c2 = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="critical",
                                   detail={"exec_key": p["exec_key"], "issue": "manual sale may have used session-held shares",
                                           "session_owners": held}, key=f"manual-sale-session:{p['exec_key']}")
                if c2:
                    ctx.emit("ManualSaleOfSessionShares", {"exec_key": p["exec_key"]}, {"exceptions": [c2]}, account_key=key)
    changes, exec_row, anomalies, _ = _book_execution(ctx, p, owner=owner, order=None,
                                                      settlement_days=inst.settlement_days, mult=inst.multiplier, fee_estimate=None)
    exec_row["fee_status"] = "EXTERNAL"
    if case:
        changes["exceptions"].append(case)
    changes["executions"] = [exec_row]
    ctx.emit(kind, {"exec_key": p["exec_key"], "anomalies": anomalies, "owner": owner}, changes, account_key=key,
             instrument_id=p["instrument_id"], event_time_ns=p["exec_time_ns"])
    if is_foreign:
        latch(ctx, key, "FOREIGN_ACTIVITY")
    pending = ctx.tx.one("SELECT row FROM pending_commissions WHERE exec_key=?", (p["exec_key"],))
    if pending is not None:
        _apply_commission(ctx, json.loads(pending["row"]), deferred=True)   # it arrived before its execution
    if owner == ledger.EXTERNAL:
        cover_external(ctx, key, p["currency"], p["exec_key"])
    return {"exec_key": p["exec_key"], "external": True, "foreign": is_foreign}


def _active_revision(ctx: Context, exec_key: str) -> dict | None:
    ex = state.execution(ctx.tx, exec_key)
    seen = set()
    while ex is not None and ex.get("status") == "CORRECTED" and ex.get("corrected_by") and ex["exec_key"] not in seen:
        seen.add(ex["exec_key"])
        ex = state.execution(ctx.tx, ex["corrected_by"])
    return ex


def _apply_correction(ctx: Context, p: dict) -> dict:
    """Book a broker correction: reverse the original, book the replacement, never park the fact."""
    key = p["account_key"]
    orig = _active_revision(ctx, p["correction_of"])
    if orig is None or orig.get("status") != "ACTIVE":
        # The revision being corrected was never seen: book this one as an ordinary execution and flag it.
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                             detail={"exec_key": p["exec_key"], "issue": "correction whose original was not ingested; booked as new",
                                     "correction_of": p["correction_of"]}, key=f"orphan-correction:{p['exec_key']}")
        ctx.emit("OrphanCorrectionRecorded", {"exec_key": p["exec_key"]}, {"exceptions": [case] if case else []}, account_key=key)
        order, foreign = _find_order(ctx, key, p.get("client_order_id"), p.get("order_ref"), p.get("broker_order_id"))
        if _fx(p["quantity"]).is_zero():
            # A bust of an execution never ingested: nothing to reverse or book; recorded under the open case.
            row = _bust_row(p, owner=current_owner(ctx.tx, order_owner(order)) if order else ledger.EXTERNAL,
                            order_id=order["order_id"] if order else None, session_id=order["session_id"] if order else None,
                            instrument_id=p.get("instrument_id") or (order or {}).get("instrument_id"), side=p.get("side"),
                            ccy=p.get("currency") or (order or {}).get("currency"))
            ctx.emit("OrphanBustRecorded", {"exec_key": p["exec_key"], "correction_of": p["correction_of"]},
                     {"executions": [row]}, account_key=key, order_id=row["order_id"], session_id=row["session_id"])
            return {"exec_key": p["exec_key"], "duplicate": False, "bust": True, "orphan": True}
        plain = {**p, "correction_of": None}
        return apply_order_execution(ctx, order, plain) if order else _external_execution(ctx, plain, foreign)
    scale = currency(orig["currency"]).ledger_scale
    ccy = orig["currency"]
    changes: dict = {"journal": [], "lots": [], "settlements": [], "executions": [], "orders": [], "holds": [], "exceptions": []}
    # 1. Reverse the original's entries into whoever holds the balances now (ADR-0008 after completion).
    for entry_id in orig["effects"]["entries"] + orig.get("fee_entries", []):
        lines = []
        for r in ctx.tx.all("SELECT account_code, owner, currency, instrument_id, amount FROM postings WHERE entry_id=?", (entry_id,)):
            lines.append(ledger.Line(r["account_code"], current_owner(ctx.tx, r["owner"]), r["currency"],
                                     -Fixed.parse(r["amount"]), r["instrument_id"]))
        changes["journal"].append(ledger.entry(ctx.new_id("je-"), key, "REVERSAL", f"reverse {entry_id} (correction {p['exec_key']})",
                                               lines, reverses=entry_id))
    # 2. Undo lot effects. A buy whose lot was partly sold keeps the disposed part: the replacement is
    #    booked in full and the disposed quantity is re-attributed at the corrected price below.
    disposed = Fixed(0)
    disposed_cost_old = Fixed(0, scale)
    for lot_id in orig["effects"]["lots_opened"]:
        lot = json.loads(ctx.tx.one("SELECT row FROM lots WHERE lot_id=?", (lot_id,))["row"])
        open_qty, original_qty = Fixed.parse(lot["open_qty"]), Fixed.parse(lot["original_qty"])
        if open_qty != original_qty and original_qty.is_positive():
            disposed = disposed + (original_qty - open_qty)
            disposed_cost_old = disposed_cost_old + (Fixed.parse(orig["notional"]) - Fixed.parse(lot["open_cost"]))
        changes["lots"].append({**lot, "owner": current_owner(ctx.tx, lot["owner"]), "open_qty": "0",
                                "open_cost": Fixed(0, scale).to_str()})
    for c in orig["effects"]["lots_consumed"]:
        lot = json.loads(ctx.tx.one("SELECT row FROM lots WHERE lot_id=?", (c["lot_id"],))["row"])
        changes["lots"].append({**lot, "owner": current_owner(ctx.tx, lot["owner"]),
                                "open_qty": (Fixed.parse(lot["open_qty"]) + Fixed.parse(c["qty"])).to_str(),
                                "open_cost": (Fixed.parse(lot["open_cost"]) + Fixed.parse(c["cost"])).rescale(scale).to_str()})
    if disposed.is_positive() and _fx(p["quantity"]) < disposed:
        case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="critical",
                             detail={"exec_key": p["exec_key"], "issue": "correction reduces a buy below the quantity already sold",
                                     "disposed": disposed.to_str(), "corrected_qty": p["quantity"]},
                             key=f"correction-below-disposed:{p['exec_key']}", owner_hint=current_owner(ctx.tx, orig["owner"]))
        changes["exceptions"].append(case) if case else None
    # 3. Settlement: cancel unsettled rows; compensate rows already settled (cash moved at the broker).
    for sid in orig.get("settlement_ids") or ([orig["settlement_id"]] if orig.get("settlement_id") else []):
        stl = json.loads(ctx.tx.one("SELECT row FROM settlements WHERE settlement_id=?", (sid,))["row"])
        if stl["status"] == "EXPECTED":
            changes["settlements"].append({**stl, "status": "CANCELLED", "cancelled_by": p["exec_key"]})
        else:
            changes["settlements"].append(settlement_row(
                ctx, key, current_owner(ctx.tx, stl["owner"]), stl["currency"], "RECEIVE" if stl["direction"] == "PAY" else "PAY",
                Fixed.parse(stl["amount"]), stl.get("relieves") or (ledger.PAYABLE if stl["direction"] == "PAY" else ledger.RECEIVABLE),
                f"{sid}:reversal:{p['exec_key']}", orig["exec_key"], ctx.now_ns, 0))
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
    p2 = {**p, "instrument_id": p.get("instrument_id") or orig["instrument_id"], "currency": p.get("currency") or ccy}
    if _fx(p2["quantity"]).is_zero():
        # A bust: the broker says the execution did not happen. The reversal above is the whole economic effect;
        # there is no replacement to book (a zero-quantity booking would be an empty journal entry).
        result = _record_bust(ctx, orig, p2, order)
    elif order is not None:
        current = state.order(ctx.tx, order["order_id"])
        allowance = Fixed.parse(orig["quantity"]) if current["state"] in state.TERMINAL_ORDER_STATES else None
        result = apply_order_execution(ctx, current, p2, allowance=allowance)
    else:
        result = _external_execution(ctx, {**p2, "order_ref": None}, None)
    if disposed.is_positive():
        _reattribute_disposed(ctx, key, orig, p2, disposed, disposed_cost_old, scale)
    return {**result, "corrected": orig["exec_key"]}


def _bust_row(p: dict, *, owner: str, order_id: str | None, session_id: str | None, instrument_id: str | None,
              side: str | None, ccy: str | None) -> dict:
    """The execution row of a bust (a correction to quantity zero): recorded, with no economic effect of its own."""
    zero = Fixed(0, currency(ccy).ledger_scale).to_str() if ccy else "0"
    return {"exec_key": p["exec_key"], "account_key": p["account_key"], "order_id": order_id, "session_id": session_id,
            "owner": owner, "instrument_id": instrument_id, "side": side, "quantity": "0", "price": p.get("price"),
            "currency": ccy, "notional": zero, "raw_notional": "0", "exec_time_ns": p.get("exec_time_ns"),
            "provider_exec_id": p.get("provider_exec_id"), "correction_of": p.get("correction_of"), "corrected_by": None,
            "status": "BUST", "fee_estimate": zero, "fee_estimate_code": None, "fee_final": None, "fee_status": "BUST",
            "effects": {"entries": [], "lots_opened": [], "lots_consumed": []}, "settlement_id": None,
            "settlement_ids": [], "raw_economic": p.get("_raw_economic") or {k: p.get(k) for k in ECONOMIC_FIELDS},
            "raw": p.get("raw", {})}


def _record_bust(ctx: Context, orig: dict, p: dict, order: dict | None) -> dict:
    """Record a bust after its original was reversed (BR-LED-03, BR-SES-02).

    The reversal already moved every effect to whoever holds the balances now: after completion the shares a busted
    sell had delivered come back to protected:<id> with their cost, after close to unallocated (ADR-0008); a busted
    buy's payable and inventory leave the same way. Here the bust itself is recorded, the order's holds are
    recomputed (a working order keeps its reservation for the quantity the broker may still fill), and cases open:
    a blocking POST_COMPLETION_ADJUSTMENT for an ended session (dispose of returned inventory with a DISPOSAL intent),
    a late-activity case for a session already flat, and an EXECUTION_ANOMALY when the order is still working, since
    the broker's view of its remaining quantity must then be confirmed."""
    key = p["account_key"]
    owner = current_owner(ctx.tx, orig["owner"])
    row = _bust_row(p, owner=owner, order_id=orig.get("order_id"), session_id=orig.get("session_id"),
                    instrument_id=orig["instrument_id"], side=orig["side"], ccy=orig["currency"])
    changes: dict = {"executions": [row], "exceptions": []}
    current = state.order(ctx.tx, order["order_id"]) if order is not None else None
    if current is not None:
        working = current["state"] not in state.TERMINAL_ORDER_STATES
        changes["holds"] = hold_rows(current, multiplier_of(ctx, current["instrument_id"]), release=not working)
        if working:
            case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                                 detail={"exec_key": p["exec_key"], "busted": orig["exec_key"], "order_id": current["order_id"],
                                         "issue": "execution busted while the order is working: confirm its remaining quantity"},
                                 key=f"bust-working:{p['exec_key']}", session_id=current["session_id"],
                                 order_id=current["order_id"], owner_hint=owner)
            if case:
                changes["exceptions"].append(case)
    ctx.emit("ExecutionBusted", {"exec_key": p["exec_key"], "busted": orig["exec_key"], "owner": owner,
                                 "quantity_returned": orig["quantity"] if orig["side"] == "SELL" else None,
                                 "note": "reversal is the whole economic effect; no replacement is booked"},
             changes, account_key=key, session_id=orig.get("session_id"), order_id=orig.get("order_id"),
             instrument_id=orig["instrument_id"], event_time_ns=p.get("exec_time_ns"))
    sid = orig.get("session_id")
    if sid and _after_session_end(ctx, current or {"session_id": sid}):
        # Including a bust of an owner DISPOSAL's fill: the shares it sold come back to protected:<id> or
        # unallocated, and only this case says so (the reconciliation agrees with the broker).
        _post_completion_notice(ctx, key, sid, f"bust {p['exec_key']} of {orig['exec_key']} after the session ended",
                                blocking=True, ref=p["exec_key"])
    elif sid:
        s = state.session(ctx.tx, sid)
        if s["state"] == "RECONCILING":
            _late_activity_notice(ctx, key, s, f"bust {p['exec_key']} while reconciling")
    check_overcommit(ctx, key, owner, orig["currency"], p["exec_key"], sid)
    return {"exec_key": p["exec_key"], "duplicate": False, "bust": True, "owner": owner}


def _reattribute_disposed(ctx: Context, key: str, orig: dict, p: dict, disposed: Fixed, old_cost: Fixed, scale: int) -> None:
    """Shares of a corrected buy that were already sold: remove them from the replacement lot at the
    corrected price and restate the realized result of those sales by the cost difference. After a bust there is
    no replacement lot: the sold shares' whole old cost is restated (the correction-below-disposed case records
    that they were never owned)."""
    new = state.execution(ctx.tx, p["exec_key"])
    opened = ((new or {}).get("effects") or {}).get("lots_opened") or []
    if not opened:
        if (new or {}).get("status") != "BUST" or old_cost.is_zero():
            return
        owner = current_owner(ctx.tx, orig["owner"])
        lines = [ledger.Line(ledger.INVENTORY, owner, orig["currency"], old_cost, orig["instrument_id"]),
                 ledger.Line(ledger.REALIZED, owner, orig["currency"], -old_cost)]
        ctx.emit("CorrectionDisposalRestated", {"disposed": "0", "old_cost": old_cost.to_str(), "new_cost": "0",
                                                "note": f"bust {p['exec_key']}: {disposed} shares sold were never bought"},
                 {"journal": [ledger.entry(ctx.new_id("je-"), key, "CORRECTION_RESTATEMENT",
                                           f"restate cost of {disposed} shares sold before bust {p['exec_key']}", lines)]},
                 account_key=key, instrument_id=orig["instrument_id"])
        return
    lot = json.loads(ctx.tx.one("SELECT row FROM lots WHERE lot_id=?", (opened[0],))["row"])
    open_qty, open_cost = Fixed.parse(lot["open_qty"]), Fixed.parse(lot["open_cost"])
    take = disposed if disposed < open_qty else open_qty
    new_cost = open_cost if take == open_qty else (open_cost * take).div(open_qty, scale, Rounding.HALF_EVEN)
    delta = old_cost - new_cost  # inventory adjustment; realized moves by the opposite amount
    owner = lot["owner"]
    lines = [ledger.Line(ledger.INVENTORY, owner, lot["currency"], delta, lot["instrument_id"]),
             ledger.Line(ledger.REALIZED, owner, lot["currency"], -delta)]
    changes = {"lots": [{**lot, "open_qty": (open_qty - take).to_str(), "open_cost": (open_cost - new_cost).rescale(scale).to_str()}]}
    if not delta.is_zero():
        changes["journal"] = [ledger.entry(ctx.new_id("je-"), key, "CORRECTION_RESTATEMENT",
                                           f"restate cost of {take} shares sold before correction {p['exec_key']}", lines)]
    ctx.emit("CorrectionDisposalRestated", {"disposed": take.to_str(), "old_cost": old_cost.to_str(), "new_cost": new_cost.to_str()},
             changes, account_key=key, instrument_id=lot["instrument_id"])


# -------------------------------------------------------------- commission
def broker_commission(ctx: Context, p: dict) -> dict:
    return _apply_commission(ctx, p, deferred=False)


def _commission_anomaly(ctx: Context, key: str, p: dict, issue: str) -> dict:
    case = new_exception(ctx, key, EXECUTION_ANOMALY, blocking=True, severity="high",
                         detail={"exec_key": p.get("exec_key"), "issue": issue, "commission": {k: p.get(k) for k in ("amount", "currency")}},
                         key=f"commission:{p.get('exec_key')}:{issue[:24]}")
    ctx.emit("CommissionAnomaly", {"issue": issue, "exec_key": p.get("exec_key")}, {"exceptions": [case] if case else []},
             account_key=key)
    return {"exec_key": p.get("exec_key"), "anomaly": issue}


def _apply_commission(ctx: Context, p: dict, *, deferred: bool) -> dict:
    key = p["account_key"]
    exec_key = p["exec_key"]
    try:
        amount_raw = _fx(p["amount"])
        currency(p["currency"])
    except (ValueError, TypeError, KeyError):
        return _commission_anomaly(ctx, key, p, "malformed commission report")
    ex = state.execution(ctx.tx, exec_key)
    if ex is None:
        ctx.emit("CommissionDeferred", {"exec_key": exec_key, "note": "commission arrived before its execution"},
                 {"pending_commissions": [{"exec_key": exec_key, "account_key": key, **{k: p[k] for k in ("amount", "currency")},
                                           "raw": p.get("raw", {})}]}, account_key=key)
        return {"deferred": True}
    if ex.get("status") == "PRE_IMPORT":
        ctx.emit("PreImportCommissionRecorded", {"exec_key": exec_key, "amount": p["amount"]},
                 {"pending_commissions": [{"exec_key": exec_key, "account_key": key, "delete": True}]} if deferred else {},
                 account_key=key)
        return {"pre_import": True}
    if ex.get("status") == "CORRECTED" and ex.get("fee_final") is not None and Fixed.parse(ex["fee_final"]) == \
            amount_raw.rescale(currency(ex["currency"]).ledger_scale, Rounding.HALF_EVEN):
        if deferred:
            ctx.emit("CommissionDuplicate", {"exec_key": exec_key},
                     {"pending_commissions": [{"exec_key": exec_key, "account_key": key, "delete": True}]}, account_key=key)
        return {"duplicate": True}   # a redelivery of what the corrected revision itself already booked
    if ex.get("status") == "CORRECTED":
        # A late report for a superseded revision: it belongs to the active replacement, once.
        active = _active_revision(ctx, exec_key)
        if active is None or active.get("status") != "ACTIVE":
            return _commission_anomaly(ctx, key, p, "commission for a corrected execution with no active revision")
        if active.get("fee_final") is not None:
            same = Fixed.parse(active["fee_final"]) == amount_raw.rescale(currency(active["currency"]).ledger_scale, Rounding.HALF_EVEN)
            ctx.emit("CommissionForCorrectedExecution", {"exec_key": exec_key, "active": active["exec_key"], "same_amount": same}, {},
                     account_key=key)
            return {"duplicate": True} if same else _commission_anomaly(ctx, key, p, "late commission differs from the active revision's")
        ex = active
        exec_key = active["exec_key"]
    ccy = p["currency"]
    if ccy != ex["currency"]:
        if deferred:
            return _commission_anomaly(ctx, key, p, "commission currency differs from execution currency (kept pending)")
        return _commission_anomaly(ctx, key, p, "commission currency differs from execution currency")
    scale = currency(ccy).ledger_scale
    amount = amount_raw.rescale(scale, Rounding.HALF_EVEN)
    owner = economic_owner(ctx.tx, ex["session_id"]) if ex["session_id"] else current_owner(ctx.tx, ex["owner"])
    changes: dict = {"journal": [], "executions": [], "settlements": [], "orders": [], "holds": [], "exceptions": []}
    if deferred:
        changes["pending_commissions"] = [{"exec_key": p["exec_key"], "account_key": key, "delete": True}]
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
        est_code = ex.get("fee_estimate_code") or ledger.FEES_ACCRUED
        lines = []
        if est.is_positive():
            lines += [ledger.Line(est_code, owner, ccy, est), ledger.Line(ledger.FEE_ESTIMATE, owner, ccy, -est)]
        lines += [ledger.Line(ledger.COMMISSION, owner, ccy, amount), ledger.Line(against, owner, ccy, -amount)]
        delta = amount
        kind, info = "CommissionFinalized", {"estimate": est.to_str(), "final": amount.to_str()}
    entry_ids: list[str] = []
    if any(not line.amount.is_zero() for line in lines):
        e = ledger.entry(ctx.new_id("je-"), key, "COMMISSION", f"commission {exec_key}", lines)
        changes["journal"].append(e)
        entry_ids.append(e["entry_id"])
    # else: a zero fee on an execution with no estimate (e.g. the broker's commission report for a bust, whose row
    # books nothing): recorded as final, with nothing to post. An empty entry would raise and lose the report.
    changes["executions"].append({**ex, "fee_final": amount.to_str(), "fee_status": "FINAL",
                                  "fee_entries": ex.get("fee_entries", []) + entry_ids})
    main = ex.get("settlement_id")
    stl = json.loads(ctx.tx.one("SELECT row FROM settlements WHERE settlement_id=?", (main,))["row"]) if main else None
    # Change in the cash this trade settles for: a higher fee means less cash on either side. A sell's
    # estimate was already netted into its expected settlement, so the first final replaces it.
    cash_effect = -delta
    if prev_final is None and ex.get("fee_estimate_code") == ledger.RECEIVABLE:
        cash_effect = cash_effect + Fixed.parse(ex["fee_estimate"])
    if stl is not None and stl["status"] == "EXPECTED":
        signed = (Fixed.parse(stl["amount"]) if stl["direction"] == "RECEIVE" else -Fixed.parse(stl["amount"])) + cash_effect
        changes["settlements"].append({**stl, "owner": owner, "direction": "RECEIVE" if not signed.is_negative() else "PAY",
                                       "amount": abs(signed).rescale(scale).to_str()})
    elif not cash_effect.is_zero():
        changes["settlements"].append(settlement_row(ctx, key, owner, ccy, "RECEIVE" if cash_effect.is_positive() else "PAY",
                                                     abs(cash_effect), against, f"stl:{exec_key}:fee:{ctx.payload_hash[:8]}",
                                                     exec_key, ctx.now_ns, 1))
    if ex.get("order_id"):
        order = state.order(ctx.tx, ex["order_id"])
        if order is not None:
            new = {**order, "fees_final": (Fixed.parse(order["fees_final"]) + delta).rescale(scale).to_str()}
            changes["orders"].append(new)
            mult = multiplier_of(ctx, order["instrument_id"])
            changes["holds"] = hold_rows(new, mult, release=new["state"] in state.TERMINAL_ORDER_STATES)
    ctx.emit(kind, {"exec_key": exec_key, **info, "owner": owner}, changes, account_key=key,
             order_id=ex.get("order_id"), session_id=ex.get("session_id"))
    ex_order = state.order(ctx.tx, ex["order_id"]) if ex.get("order_id") else None
    disposal_fee = (ex_order or {}).get("purpose") == "DISPOSAL" and kind == "CommissionFinalized"
    if ex.get("session_id") and owner != ledger.session_owner(ex["session_id"]) and not disposal_fee:
        # A disposal's first final commission is its expected activity; a later change of it is a late fee.
        _post_completion_notice(ctx, key, ex["session_id"], f"{kind} for {exec_key}",
                                blocking=ledger.available_cash(ctx.tx, key, owner, ccy).is_negative(), ref=exec_key)
    if owner == ledger.EXTERNAL:
        cover_external(ctx, key, ccy, f"{exec_key}:commission")
    check_overcommit(ctx, key, owner, ccy, exec_key, ex.get("session_id"))
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
