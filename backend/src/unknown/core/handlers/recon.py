"""Reconciliation result handling and evidence-based settlement (REC-01..REC-04).

The ``Reconciler`` (reconcile/reconciler.py) first ingests executions,
commissions and order statuses through the ordinary observation commands,
then sends one ``reconciliation_result`` carrying the broker snapshot, the
open-order list and lookups for local orders the broker no longer lists. This
handler compares them with projections inside one transaction (a consistent
cut on our side), applies conclusive outcomes, opens exceptions for
everything else, confirms due settlements only when broker settled cash
agrees exactly, and advances ``reconciled_epoch`` only on a clean result."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from ...domain.calendar import local_date
from ...domain.errors import Rejected
from ...domain.fixed import Fixed, Rounding
from ...domain.money import currency
from .. import ledger, state
from ..desk import Context
from .broker import WORKING_STATUSES, _find_order, apply_status, finalize
from .control import latch_stop
from .orders import cancel_order
from .common import (CASH_MISMATCH, FOREIGN_ACTIVITY, POSITION_MISMATCH, RECONCILIATION_INCOMPLETE, SETTLEMENT_LATE,
                     UNKNOWN_ORDER, control_update, new_exception)

SETTLEMENT_GRACE_BUSINESS_DAYS = 2


def reconciliation_result(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    acct = state.account(ctx.tx, key)
    run_id = p.get("run_id") or ctx.new_id("rec-")
    seq_row = ctx.tx.one("SELECT MAX(seq) AS s FROM events")
    seq_at_start = seq_row["s"] or 0
    snap = p["snapshot"]
    oo = p["open_orders"]
    complete = bool(snap.get("complete")) and bool(oo.get("complete")) and bool(p.get("executions_complete"))
    differences: list[dict] = []
    if not complete:
        case = new_exception(ctx, key, RECONCILIATION_INCOMPLETE, blocking=False, severity="medium",
                             detail={"run_id": run_id, "snapshot_complete": snap.get("complete"),
                                     "orders_complete": oo.get("complete"), "executions_complete": p.get("executions_complete")},
                             key=f"recon-incomplete:{ctx.epoch}")
        # An incomplete run proves nothing: entries stay blocked until a complete clean run (REC-01).
        ctx.emit("ReconciliationIncomplete", {"run_id": run_id, "note": "partial broker data never proves absence"},
                 {"reconciliations": [_run_row(ctx, key, run_id, "INCOMPLETE", [], [], p, seq_at_start)],
                  "exceptions": [case] if case else [],
                  "control": [control_update(ctx, key, needs_reconciliation=True)]}, account_key=key)
        return {"run_id": run_id, "status": "INCOMPLETE"}

    c = state.control(ctx.tx, key)
    if not c.get("imported"):
        _import_opening(ctx, key, acct, snap)

    # 1. Orders --------------------------------------------------------------
    broker_open = {}
    for row in oo["rows"]:
        order, foreign = _find_order(ctx, key, row.get("client_order_id"), row.get("order_ref"), row.get("broker_order_id"))
        if order is None:
            differences.append({"kind": FOREIGN_ACTIVITY, "blocking": True, "detail": {"open_order": row, "reason": foreign}})
            case = new_exception(ctx, key, FOREIGN_ACTIVITY, blocking=True, severity="critical",
                                 detail={"open_order": row, "reason": foreign or "order not placed by this deployment"},
                                 key=f"foreign-order:{row.get('broker_order_id')}")
            if case:
                ctx.emit("ForeignOrderObserved", {"open_order": row}, {"exceptions": [case]}, account_key=key)
            latch_stop(ctx, key, "FOREIGN_ACTIVITY")
            continue
        broker_open[order["order_id"]] = row
        before = state.order(ctx.tx, order["order_id"])
        if before["state"] in state.TERMINAL_ORDER_STATES and row["status"] in WORKING_STATUSES:
            differences.append({"kind": "TERMINAL_ORDER_WORKING", "blocking": True,
                                "detail": {"order_id": order["order_id"], "local_state": before["state"], "broker_status": row["status"]}})
        apply_status(ctx, before, row["status"], Fixed.parse(row["filled_quantity"]), "",
                     row.get("broker_order_id"), via="reconciliation")
        after = state.order(ctx.tx, order["order_id"])
        if (after["state"] not in state.TERMINAL_ORDER_STATES and after["cancel_state"] in ("SENT", "ACKED", "UNKNOWN")
                and row["status"] != "PENDING_CANCEL" and not _live_cancel(ctx, after["order_id"])):
            # Our cancel did not take effect at the broker (lost in a crash or a timeout): send it again.
            differences.append({"kind": "CANCEL_NOT_EFFECTIVE", "blocking": False,
                                "detail": {"order_id": after["order_id"], "cancel_state": after["cancel_state"]}})
            cancel_order(ctx, after, "reconciliation: earlier cancel not effective at the broker")
        local = Fixed.parse(state.order(ctx.tx, order["order_id"])["cum_qty"])
        if Fixed.parse(row["filled_quantity"]) > local:
            differences.append({"kind": "EXECUTIONS_MISSING", "blocking": True,
                                "detail": {"order_id": order["order_id"], "broker_filled": row["filled_quantity"], "local": local.to_str()}})
    lookups = p.get("lookups", {})
    for o in state.orders(ctx.tx, key, open_only=True):
        if o["order_id"] in broker_open:
            continue
        submit = next((b for b in state.outbox_for_order(ctx.tx, o["order_id"]) if b["action"] == "SUBMIT"), None)
        if o["state"] == "RESERVED" and submit is not None and submit["state"] == "PENDING":
            continue  # never transmitted; nothing to compare
        lk = lookups.get(o["order_id"])
        if lk is None or lk.get("found") is None:
            differences.append({"kind": "ORDER_UNRESOLVED", "blocking": True,
                                "detail": {"order_id": o["order_id"], "state": o["state"], "lookup": lk}})
            continue
        if lk["found"]:
            status = lk.get("status")
            if status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "INACTIVE"):
                apply_status(ctx, o, status, Fixed.parse(lk.get("filled_quantity") or "0"), "reconciled",
                             lk.get("broker_order_id"), via="reconciliation")
            else:
                differences.append({"kind": "ORDER_STATUS_INCONSISTENT", "blocking": True,
                                    "detail": {"order_id": o["order_id"], "lookup": lk}})
            continue
        # Conclusively never received by the broker: the UNKNOWN exit rule (ADR-0009).
        if Fixed.parse(o["cum_qty"]).is_zero() and o["state"] in ("UNKNOWN", "SUBMIT_PENDING", "RESERVED", "CANCEL_PENDING"):
            case = state.exception_by_key(ctx.tx, key, f"unknown-order:{o['order_id']}")
            extra = [{**case, "status": "RESOLVED", "resolution": {"by": ctx.principal.id, "at_ns": ctx.now_ns,
                                                                    "note": "broker conclusively reports order not received"}}] if case else []
            out = [{**b, "state": "DONE", "result": {"status": "NOT_RECEIVED", "at_ns": ctx.now_ns}}
                   for b in state.outbox_for_order(ctx.tx, o["order_id"]) if b["state"] not in ("DONE", "SKIPPED")]
            finalize(ctx, {**o}, "REJECTED", "broker conclusively reports the order was never received", extra_exceptions=extra,
                     via="reconciliation")
            if out:
                ctx.emit("OutboxResolvedByReconciliation", {"order_id": o["order_id"]}, {"outbox": out}, account_key=key, order_id=o["order_id"])
        else:
            differences.append({"kind": "ORDER_MISSING_AT_BROKER", "blocking": True, "detail": {"order_id": o["order_id"], "state": o["state"]}})

    # 2. Positions -------------------------------------------------------------
    broker_pos: dict[str, Fixed] = defaultdict(lambda: Fixed(0))
    for row in snap["positions"]:
        if row.get("instrument_id") is None:
            differences.append({"kind": POSITION_MISMATCH, "blocking": True, "detail": {"unmapped_position": row}})
            continue
        broker_pos[row["instrument_id"]] += Fixed.parse(row["quantity"])
    ledger_pos: dict[str, Fixed] = defaultdict(lambda: Fixed(0))
    for r in ctx.tx.all("SELECT instrument_id, open_qty FROM lots WHERE account_key=?", (key,)):
        ledger_pos[r["instrument_id"]] += Fixed.parse(r["open_qty"])
    for inst in sorted(set(broker_pos) | set(ledger_pos)):
        if broker_pos[inst] != ledger_pos[inst]:
            differences.append({"kind": POSITION_MISMATCH, "blocking": True,
                                "detail": {"instrument_id": inst, "broker": broker_pos[inst].to_str(), "ledger": ledger_pos[inst].to_str()}})

    # 3. Cash and settlement ---------------------------------------------------
    policy = state.active_policy(ctx.tx, key)
    tz = policy.limits.risk_day_timezone
    as_of_date = local_date(snap["as_of_ns"], tz)
    cal = ctx.desk.calendars.get(policy.settlement_calendar, ctx.desk.calendars["WEEKDAYS"])
    settled_rows: list[dict] = []
    for cash in snap["cash"]:
        ccy = cash["currency"]
        scale = currency(ccy).ledger_scale
        # Broker amounts finer than the ledger scale are rounded explicitly (ADR-0004 #3); the sub-unit
        # residual is reported, never silently dropped and never treated as unexplained cash.
        raw_settled = Fixed.parse(cash["settled"])
        broker_settled = raw_settled.rescale(scale, Rounding.HALF_EVEN)
        if raw_settled != broker_settled:
            differences.append({"kind": "SUB_LEDGER_SCALE_RESIDUAL", "blocking": False, "detail": {
                "currency": ccy, "raw_broker_settled": raw_settled.to_str(), "rounded": broker_settled.to_str(),
                "residual": (raw_settled - broker_settled).to_str()}})
        ledger_cash = ledger.code_total(ctx.tx, key, ledger.CASH, ccy)
        expected = [r for r in state.settlements(ctx.tx, key, status="EXPECTED") if r["currency"] == ccy]
        due = [r for r in expected if date.fromisoformat(r["expected_date"]) <= as_of_date]
        due_net = sum((_signed(r) for r in due), Fixed(0, scale))
        gap = broker_settled - (ledger_cash + due_net)
        allowance = _rounding_allowance(ctx, due, scale)
        if gap.is_zero():
            if due:
                settled_rows.extend(_confirm(ctx, key, due))
        elif due and not allowance.is_zero() and abs(gap) <= allowance:
            # The difference is fully explained by recorded per-trade rounding of broker notionals:
            # book it to ROUNDING for the owner of the largest due item, then confirm.
            target = max(due, key=lambda r: abs(Fixed.parse(r["amount"])))
            e = ledger.entry(ctx.new_id("je-"), key, "ROUNDING", f"broker rounding on settlement ({gap})",
                             [ledger.Line(ledger.CASH, target["owner"], ccy, gap),
                              ledger.Line(ledger.ROUNDING, target["owner"], ccy, -gap)])
            ctx.emit("SettlementRoundingBooked", {"gap": gap.to_str(), "allowance": allowance.to_str(), "owner": target["owner"]},
                     {"journal": [e]}, account_key=key)
            differences.append({"kind": "ROUNDING_BOOKED", "blocking": False, "detail": {
                "currency": ccy, "gap": gap.to_str(), "allowance": allowance.to_str()}})
            settled_rows.extend(_confirm(ctx, key, due))
        elif broker_settled == ledger_cash:
            for r in due:
                late_after = cal.add_business_days(date.fromisoformat(r["expected_date"]), SETTLEMENT_GRACE_BUSINESS_DAYS)
                if as_of_date > late_after:
                    case = new_exception(ctx, key, SETTLEMENT_LATE, blocking=False, severity="medium",
                                         detail={"settlement_id": r["settlement_id"], "expected_date": r["expected_date"],
                                                 "owner": r["owner"], "amount": r["amount"]},
                                         key=f"settlement-late:{r['settlement_id']}", owner_hint=r["owner"])
                    if case:
                        ctx.emit("SettlementLate", {"settlement_id": r["settlement_id"]}, {"exceptions": [case]}, account_key=key)
        else:
            differences.append({"kind": CASH_MISMATCH, "blocking": True, "detail": {
                "currency": ccy, "broker_settled": broker_settled.to_str(), "ledger_cash": ledger_cash.to_str(),
                "due_settlements_net": due_net.to_str(),
                "unexplained": (broker_settled - ledger_cash - due_net).to_str()}})
        unsettled_broker = (Fixed.parse(cash["total"]) - raw_settled).rescale(scale, Rounding.HALF_EVEN)
        unsettled_ledger = sum((_signed(r) for r in state.settlements(ctx.tx, key, status="EXPECTED") if r["currency"] == ccy), Fixed(0, scale))
        if unsettled_broker != unsettled_ledger:
            differences.append({"kind": "UNSETTLED_DIFFERENCE", "blocking": False, "detail": {
                "currency": ccy, "broker_unsettled": unsettled_broker.to_str(), "ledger_unsettled": unsettled_ledger.to_str()}})

    # 4. Outcome ---------------------------------------------------------------
    exceptions = []
    for d in differences:
        if not d["blocking"] or d["kind"] == FOREIGN_ACTIVITY:
            continue
        kind = d["kind"] if d["kind"] in (POSITION_MISMATCH, CASH_MISMATCH) else "RECONCILIATION_DIFFERENCE"
        case = new_exception(ctx, key, kind, blocking=True, severity="high", detail=d["detail"],
                             key=f"recon:{d['kind']}:{_dkey(d)}")
        if case:
            exceptions.append(case)
    blocking = any(d["blocking"] for d in differences)
    status = "DIFFERENCES" if blocking else "CLEAN"
    ctl = state.control(ctx.tx, key)
    new_ctl = dict(ctl)
    if status == "CLEAN":
        new_ctl.update(reconciled_epoch=ctx.epoch, needs_reconciliation=False, last_clean_reconciliation_ns=ctx.now_ns, imported=True)
        if ctl["resume_requires_owner"] and ctl.get("resume_reason") == "PROCESS_RESTART" and policy.auto_resume_after_clean_reconciliation:
            new_ctl.update(resume_requires_owner=False, resume_reason=None)
    else:
        new_ctl.update(needs_reconciliation=True, imported=True)
    resolved = []
    if status == "CLEAN":
        case = state.exception_by_key(ctx.tx, key, f"recon-incomplete:{ctx.epoch}")
        if case:
            resolved.append({**case, "status": "RESOLVED", "resolution": {"by": ctx.principal.id, "at_ns": ctx.now_ns,
                                                                           "note": f"clean reconciliation {run_id}"}})
    ctx.emit("ReconciliationCompleted", {"run_id": run_id, "status": status, "differences": differences,
                                         "settlements_confirmed": [r["settlement_id"] for r in settled_rows]},
             {"reconciliations": [_run_row(ctx, key, run_id, status, differences, settled_rows, p, seq_at_start)],
              "exceptions": exceptions + resolved, "control": [new_ctl]}, account_key=key)
    if status != "CLEAN":
        # Unexplained differences need an owner green start, not just an acknowledgement (ADR-0010).
        latch_stop(ctx, key, "RECONCILIATION_FAILURE")
    from .sessions import mark_settled_sessions, retry_waiting_funding
    mark_settled_sessions(ctx, key)
    if status == "CLEAN":
        retry_waiting_funding(ctx, key)
    return {"run_id": run_id, "status": status, "differences": differences}


def _live_cancel(ctx: Context, order_id: str) -> bool:
    return any(b["action"] == "CANCEL" and b["state"] in ("PENDING", "SENDING") for b in state.outbox_for_order(ctx.tx, order_id))


def _rounding_allowance(ctx: Context, rows: list[dict], scale: int) -> Fixed:
    """Sum of |raw broker notional - booked notional| for the executions behind these settlement rows,
    rounded up to whole ledger units: the most rounding that recorded trades can explain."""
    total = Fixed(0)
    for r in rows:
        ex = state.execution(ctx.tx, r["exec_key"]) if r.get("exec_key") else None
        if ex and ex.get("raw_notional") and ex.get("notional"):
            total = total + abs(Fixed.parse(ex["raw_notional"]) - Fixed.parse(ex["notional"]))
    return total.rescale(scale, Rounding.CEILING)


def _dkey(d: dict) -> str:
    det = d["detail"]
    return str(det.get("order_id") or det.get("instrument_id") or det.get("currency") or "x")


def _signed(r: dict) -> Fixed:
    a = Fixed.parse(r["amount"])
    return a if r["direction"] == "RECEIVE" else -a


def _confirm(ctx: Context, key: str, rows: list[dict]) -> list[dict]:
    out = []
    lines: list[ledger.Line] = []
    for r in rows:
        # Cash moves by the signed amount; the claim the row records (``relieves``) moves the other way.
        cash = _signed(r)
        relieves = r.get("relieves") or (ledger.RECEIVABLE if r["direction"] == "RECEIVE" else ledger.PAYABLE)
        if not cash.is_zero():
            lines += [ledger.Line(ledger.CASH, r["owner"], r["currency"], cash), ledger.Line(relieves, r["owner"], r["currency"], -cash)]
        out.append({**r, "status": "SETTLED", "settled_at_ns": ctx.now_ns})
    journal = [ledger.entry(ctx.new_id("je-"), key, "SETTLEMENT", f"settlement confirmed by broker settled cash ({len(rows)} items)",
                            lines)] if lines else []
    ctx.emit("SettlementConfirmed", {"items": [r["settlement_id"] for r in rows],
                                     "note": "settlement changes availability, not P&L"},
             {"journal": journal, "settlements": out}, account_key=key)
    return out


def _import_opening(ctx: Context, key: str, acct: dict, snap: dict) -> None:
    """First reconciliation: record what the account already holds, owned by ``outside`` (ADR-0006)."""
    if ctx.tx.one("SELECT 1 FROM journal_entries WHERE account_key=?", (key,)):
        return
    lines: list[ledger.Line] = []
    lots, settlements, problems = [], [], []
    policy = state.active_policy(ctx.tx, key)
    tz = policy.limits.risk_day_timezone
    cal = ctx.desk.calendars.get(policy.settlement_calendar, ctx.desk.calendars["WEEKDAYS"])
    for cash in snap["cash"]:
        ccy = cash["currency"]
        scale = currency(ccy).ledger_scale
        raw_settled = Fixed.parse(cash["settled"])
        raw_unsettled = Fixed.parse(cash["total"]) - raw_settled
        # Explicit rounding (ADR-0004 #3): settled cash never rounds up (FLOOR); the residual is recorded.
        settled = raw_settled.rescale(scale, Rounding.FLOOR)
        unsettled = raw_unsettled.rescale(scale, Rounding.HALF_EVEN)
        if settled != raw_settled or unsettled != raw_unsettled:
            problems.append({"currency": ccy, "raw_settled": raw_settled.to_str(), "raw_unsettled": raw_unsettled.to_str(),
                             "booked_settled": settled.to_str(), "booked_unsettled": unsettled.to_str(),
                             "note": "sub-ledger-scale residual not booked"})
        if not settled.is_zero():
            lines += [ledger.Line(ledger.CASH, ledger.OUTSIDE, ccy, settled), ledger.Line(ledger.OPENING, ledger.OUTSIDE, ccy, -settled)]
        if not unsettled.is_zero():
            code = ledger.RECEIVABLE if unsettled.is_positive() else ledger.PAYABLE
            lines += [ledger.Line(code, ledger.OUTSIDE, ccy, unsettled), ledger.Line(ledger.OPENING, ledger.OUTSIDE, ccy, -unsettled)]
            settlements.append({"settlement_id": f"stl:opening:{ccy}", "account_key": key, "owner": ledger.OUTSIDE, "currency": ccy,
                                "direction": "RECEIVE" if unsettled.is_positive() else "PAY", "amount": abs(unsettled).to_str(),
                                "trade_date": local_date(snap["as_of_ns"], tz).isoformat(),
                                "expected_date": cal.add_business_days(local_date(snap["as_of_ns"], tz), 1).isoformat(),
                                "calendar": cal.calendar_id, "status": "EXPECTED", "exec_key": None, "settled_at_ns": None})
    for row in snap["positions"]:
        if row.get("instrument_id") is None or state.instrument(ctx.tx, row["instrument_id"]) is None:
            problems.append(row)
            continue
        ccy = row["currency"]
        scale = currency(ccy).ledger_scale
        qty = Fixed.parse(row["quantity"])
        inst = state.instrument(ctx.tx, row["instrument_id"])
        avg = Fixed.parse(row["average_cost"]) if row.get("average_cost") else Fixed(0)
        cost = (qty * avg * inst.multiplier).rescale(scale, rounding=_HALF_EVEN)
        lot_id = f"lot:opening:{row['instrument_id']}"
        lots.append({"lot_id": lot_id, "account_key": key, "instrument_id": row["instrument_id"], "owner": ledger.OUTSIDE,
                     "currency": ccy, "open_qty": qty.to_str(), "open_cost": cost.to_str(), "original_qty": qty.to_str(),
                     "opened_by": "opening-import", "opened_at_ns": snap["as_of_ns"]})
        if not cost.is_zero():
            lines += [ledger.Line(ledger.INVENTORY, ledger.OUTSIDE, ccy, cost, row["instrument_id"]),
                      ledger.Line(ledger.OPENING, ledger.OUTSIDE, ccy, -cost)]
    changes: dict = {"lots": lots, "settlements": settlements}
    if lines:
        changes["journal"] = [ledger.entry(ctx.new_id("je-"), key, "OPENING", "opening balance import (owner: outside)", lines)]
    ctx.emit("OpeningBalancesImported", {"positions": len(lots), "unmapped_positions": problems,
                                         "note": "pre-existing holdings are owned by 'outside' until the owner authorizes capital"},
             changes, account_key=key)


def _run_row(ctx: Context, key: str, run_id: str, status: str, differences: list, settled: list, p: dict, seq_at_start: int) -> dict:
    return {"run_id": run_id, "account_key": key, "status": status, "epoch": ctx.epoch, "completed_at_ns": ctx.now_ns,
            "snapshot_as_of_ns": p["snapshot"].get("as_of_ns"), "watermark": p.get("watermark"),
            "differences": differences, "settlements_confirmed": [r["settlement_id"] for r in settled],
            "event_seq_at_start": seq_at_start}


_HALF_EVEN = Rounding.HALF_EVEN
