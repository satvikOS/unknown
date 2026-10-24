"""Order intents, cancellation and the outbox protocol (ADR-0009).

Command path: an intent passes pretrade risk, then reservation (holds), the
order row and an outbox SUBMIT row commit atomically. The sender commits
SUBMIT_PENDING *before* calling the broker; a timeout is UNKNOWN and keeps the
reservation. A cancel request never releases anything.

Disposal (ADR-0008 after completion; BR-LED-03, BR-SES-02). A late fill, a bust or a correction after a session
ended is booked to whoever holds the session's balances now: ``protected:<id>`` after completion, ``unallocated``
after close. Inventory that lands there cannot be sold by the session (it never re-opens, and pretrade refuses
orders for a terminal session), so the owner exits it with ``submit_intent`` purpose ``DISPOSAL``: a SELL that names
the ended session, reserves and sells only that owner's lots, and books its proceeds to the same owner. It runs the
ordinary pretrade sequence, in which the owner's disposal stands in for the session-state check (the decision
record says so); every other check, including owned unreserved quantity, quotes, collar and position-related
exceptions, still applies. Reconciliation then confirms the position and the settlement like any other trade."""
from __future__ import annotations

from ...domain.errors import Rejected
from ...domain.fixed import Fixed, Rounding
from ...domain.ids import validate_id
from ...domain.money import currency
from ...domain.principal import Role
from ...risk.fee_schedule import FeeEnvelopeUnavailable
from ...risk.pretrade import Decision, PretradeInput, evaluate
from .. import ledger, state
from ..desk import Context
from .common import UNKNOWN_ORDER, hold_rows, multiplier_of, new_exception, order_owner

ENTRY = "ENTRY"
EXIT = "EXIT"
WIND_DOWN = "WIND_DOWN"
DISPOSAL = "DISPOSAL"
MAX_ACTIVE_SESSIONS = 1


def _fixed(value: object, what: str) -> Fixed:
    if not isinstance(value, str):
        raise Rejected("INVALID_FIELD", f"{what} must be a decimal string")
    try:
        return Fixed.parse(value)
    except ValueError as exc:
        raise Rejected("INVALID_FIELD", f"{what}: {exc}") from None


def submit_intent(ctx: Context, p: dict) -> dict:
    side = p.get("side")
    purpose = p.get("purpose") or (ENTRY if side == "BUY" else EXIT)
    if purpose not in (ENTRY, EXIT, DISPOSAL):
        raise Rejected("INVALID_FIELD", "purpose must be ENTRY, EXIT or DISPOSAL")
    if purpose == DISPOSAL:
        if ctx.principal.role is not Role.OWNER:
            raise Rejected("UNAUTHORIZED", "only the owner disposes of inventory an ended session left behind")
        if side != "SELL":
            raise Rejected("INVALID_FIELD", "a disposal is a SELL")
    strategy_id = p.get("strategy_id")
    if ctx.principal.role is Role.STRATEGY:
        if not strategy_id:
            raise Rejected("INVALID_FIELD", "strategy intents must name strategy_id")
        if strategy_id != ctx.principal.id:
            # A strategy acts only under its own authenticated identity, never another mandated id.
            raise Rejected("STRATEGY_IDENTITY_MISMATCH", "strategy_id must equal the authenticated strategy principal")
    deadline = p.get("deadline_ns")
    if type(deadline) is not int:
        raise Rejected("INVALID_FIELD", "deadline_ns must be an integer")
    return create_order(
        ctx, account_key=p.get("account_key"), session_id=p.get("session_id"),
        order_id=validate_id(p.get("order_id"), "order id"), instrument_id=p.get("instrument_id"), side=side,
        quantity=_fixed(p.get("quantity"), "quantity"), limit_price=_fixed(p.get("limit_price"), "limit_price"),
        order_type=p.get("order_type", "LIMIT"), tif=p.get("time_in_force", "DAY"), deadline_ns=deadline,
        policy_hash=p.get("policy_hash"), strategy_id=strategy_id or f"{ctx.principal.role.value.lower()}:{ctx.principal.id}",
        purpose=purpose, decision_ref=p.get("decision_ref"))


def create_order(ctx: Context, *, account_key: str, session_id: str, order_id: str, instrument_id: str, side: str,
                 quantity: Fixed, limit_price: Fixed, order_type: str, tif: str, deadline_ns: int, policy_hash: str,
                 strategy_id: str, purpose: str, decision_ref: object) -> dict:
    """Evaluate and, if approved, reserve + persist order + outbox. Raises Rejected before any event."""
    tx = ctx.tx
    acct = state.account(tx, account_key)
    ctl = state.control(tx, account_key)
    if state.order(tx, order_id) is not None:
        raise Rejected("DUPLICATE_ORDER_ID", order_id)
    sess = state.session(tx, session_id)
    if sess["account_key"] != account_key:
        raise Rejected("SESSION_ACCOUNT_MISMATCH", session_id)
    mandate = state.mandate(tx, sess["mandate_hash"])
    policy = state.active_policy(tx, account_key)
    inst = state.instrument(tx, instrument_id) if isinstance(instrument_id, str) else None
    ccy = mandate.currency
    scale = currency(ccy).ledger_scale
    mult = inst.multiplier if inst else Fixed(1)
    owner = ledger.session_owner(session_id)
    if purpose == DISPOSAL:
        from .broker import economic_owner
        owner = economic_owner(tx, session_id)
        if owner == ledger.session_owner(session_id):
            raise Rejected("SESSION_NOT_ENDED", f"session {session_id} is {sess['state']}: it sells its own inventory "
                                                "with an EXIT intent; DISPOSAL is for what an ended session left in "
                                                "protected:<id> or unallocated")
    quote = ctx.desk.quotes.get(instrument_id) if inst else None

    notional = (quantity * limit_price * mult).rescale(scale, Rounding.CEILING) if quantity.is_positive() and limit_price.is_positive() else Fixed(0, scale)
    # A BUY reserves the fee envelope of the account broker's schedule for this venue and size (ADR-0006 amendment
    # 2026-10-24); without one the FEE_SCHEDULE check refuses the entry with the reason. A SELL reserves no envelope.
    fee_env, fee_error = None, None
    if side == "BUY":
        try:
            fee_env = ctx.desk.fee_schedules.buy_envelope(
                broker=acct["provider"], policy_fees=policy.fees, instrument=inst, quantity=quantity,
                limit_price=limit_price, notional=notional, ccy=ccy, now_ns=ctx.now_ns)
        except FeeEnvelopeUnavailable as exc:
            fee_error = str(exc)
    envelope_amount = fee_env.amount if fee_env is not None else Fixed(0, scale)
    available = ledger.available_cash(tx, account_key, owner, ccy)
    owned = ledger.owned_quantity(tx, account_key, owner, instrument_id) if inst else Fixed(0)
    held = ledger.active_quantity_holds(tx, account_key, owner, instrument_id) if inst else Fixed(0)

    session_open_buys = [o for o in state.orders(tx, account_key, session_id=session_id, open_only=True) if o["side"] == "BUY"]
    pending_buy_qty = sum((Fixed.parse(o["leaves_qty"]) for o in session_open_buys if o["instrument_id"] == instrument_id), Fixed(0))
    position_after = ((owned + pending_buy_qty + quantity) * limit_price * mult).rescale(scale, Rounding.CEILING)
    session_equity = _session_equity_estimate(ctx, account_key, owner, ccy)
    gross_after = _gross_exposure(ctx, account_key, ccy) + notional
    since = ctx.now_ns - 60 * 1_000_000_000
    recent = tx.one("SELECT COUNT(*) AS n FROM orders WHERE account_key=? AND accepted_at_ns>=?", (account_key, since))["n"]
    own = state.ownership(tx, account_key)
    risk = state.risk_state(tx, account_key)
    # The owner may trade any session by hand; the system only liquidates (wind-down); everyone else must be mandated.
    strategy_ok = (ctx.principal.role is Role.OWNER or (ctx.principal.role is Role.SYSTEM and purpose == WIND_DOWN)
                   or strategy_id in mandate.strategy_eligibility)
    blocking_kinds = tuple(e["kind"] for e in state.open_exceptions(tx, account_key, blocking_only=True))
    loss = risk.get("breach") or (None if risk.get("valuation_ok", True) else "VALUATION_STALE")

    decision = evaluate(PretradeInput(
        now_ns=ctx.now_ns, side=side, order_type=order_type, time_in_force=tif, quantity=quantity,
        limit_price=limit_price, order_currency=ccy, deadline_ns=deadline_ns, intent_policy_hash=policy_hash,
        account_mode=acct["mode"], stop_latched=ctl["stop_latched"], resume_requires_owner=ctl["resume_requires_owner"],
        # Buys need a clean, current reconciliation. Risk-reducing sells need only a reconciliation in this
        # process (positions confirmed since restart); position-related exceptions still block them (REC-03).
        reconciled_this_epoch=(ctl["reconciled_epoch"] == ctx.epoch and (side == "SELL" or not ctl["needs_reconciliation"])),
        would_self_cross=_self_cross(ctx, account_key, instrument_id, side, limit_price),
        ownership_held=bool(own and own["status"] == "HELD" and own["deployment_id"] == ctx.desk.deployment_id),
        blocking_exception_kinds=blocking_kinds, loss_budget_breached=loss,
        session_state=sess["state"], mandate_policy_hash=mandate.policy_hash, strategy_permitted=strategy_ok,
        instrument=inst, eligibility=policy.eligibility, quote=quote,
        quote_max_age_ms=policy.limits.quote_max_age_ms, price_collar_bps=policy.limits.price_collar_bps,
        required_cash=(notional + envelope_amount), available_cash=available,
        fee_envelope_error=fee_error, fee_envelope_detail=fee_env.detail() if fee_env is not None else "",
        unreserved_quantity=owned - held, order_notional=notional,
        max_order_notional=policy.limits.max_order_notional.amount, position_notional_after=position_after,
        session_equity=session_equity, max_position_bps=policy.limits.max_position_bps_of_session_equity,
        gross_exposure_after=gross_after, max_gross_exposure=policy.limits.max_gross_exposure.amount,
        orders_last_minute=recent, max_orders_per_minute=policy.limits.max_orders_per_minute,
        average_daily_volume=None, max_participation_bps_of_adv=policy.limits.max_participation_bps_of_adv,
        require_liquidity_data=policy.limits.require_liquidity_data))
    if purpose == DISPOSAL:
        decision = _disposal_decision(decision, sess["state"], owner)
    quote_json = quote.to_json(ctx.now_ns) if quote else None
    if not decision.approved:
        raise Rejected(decision.code, "pretrade check failed", decision=decision.to_json(), quote=quote_json)

    order = {
        "order_id": order_id, "account_key": account_key, "session_id": session_id, "instrument_id": instrument_id,
        "side": side, "order_type": order_type, "time_in_force": tif, "quantity": quantity.to_str(),
        "limit_price": limit_price.to_str(), "currency": ccy, "state": "RESERVED", "prev_state": None,
        "cancel_state": "NONE", "cum_qty": "0", "leaves_qty": quantity.to_str(), "notional_filled": Fixed(0, scale).to_str(),
        "avg_price": None, "fees_estimated": Fixed(0, scale).to_str(), "fees_final": Fixed(0, scale).to_str(),
        "fee_envelope": envelope_amount.to_str(), "broker_order_id": None,
        "order_ref": f"{ctx.desk.deployment_id}:{own['epoch']}:{order_id}",
        "accepted_at_ns": ctx.now_ns, "submitted_at_ns": None, "terminal_at_ns": None, "deadline_ns": deadline_ns,
        "policy_hash": policy_hash, "principal_id": ctx.principal.id, "principal_role": ctx.principal.role.value,
        "strategy_id": strategy_id, "purpose": purpose, "decision_ref": decision_ref, "reason": None,
        "broker_terminal_pending": None, "command_id": ctx.command.command_id,
    }
    if purpose == DISPOSAL:
        order["owner"] = owner   # reserves, sells and books for this owner, never for the ended session
    outbox = {"outbox_id": f"{order_id}:submit", "order_id": order_id, "account_key": account_key, "action": "SUBMIT",
              "state": "PENDING", "attempts": 0, "last_attempt_ns": None, "result": None}
    ctx.emit("OrderAccepted", {"decision": decision.to_json(), "quote": quote_json, "purpose": purpose,
                               "required_cash": (notional + envelope_amount).to_str() if side == "BUY" else None,
                               "fee_envelope": fee_env.to_json() if fee_env is not None else None},
             {"orders": [order], "holds": hold_rows(order, mult, release=False), "outbox": [outbox]},
             account_key=account_key, session_id=session_id, order_id=order_id, instrument_id=instrument_id)
    return {"order_id": order_id, "state": "RESERVED", "decision": decision.code}


def _disposal_decision(decision: Decision, session_state: str, owner: str) -> Decision:
    """An owner DISPOSAL names a session that has ended; the session-state check (which only admits sessions that
    may trade) is replaced by the owner's disposal authority, stated in the decision record. Every other check keeps
    its result, and the first failing one is still the decision (BR-LED-03, BR-SES-02)."""
    checks = [{**c, "passed": True, "detail": f"owner disposal of inventory owned by {owner}; the session stays "
                                              f"{session_state} (ADR-0008: an ended session never re-opens)"}
              if c["check"] == "SESSION_STATE" else c for c in decision.checks]
    failed = next((c for c in checks if not c["passed"]), None)
    return Decision(approved=failed is None, code=failed["check"] if failed else "APPROVED", checks=checks)


def _self_cross(ctx: Context, account_key: str, instrument_id: str, side: str, limit: Fixed) -> bool:
    """True if the new order could trade against one of our own working orders (wash-trade prevention, book Ch. 25)."""
    for o in state.orders(ctx.tx, account_key, open_only=True):
        if o["instrument_id"] != instrument_id or o["side"] == side or o["cancel_state"] == "CONFIRMED":
            continue
        other = Fixed.parse(o["limit_price"])
        if (side == "BUY" and limit >= other) or (side == "SELL" and limit <= other):
            return True
    return False


def _session_equity_estimate(ctx: Context, account_key: str, owner: str, ccy: str) -> Fixed:
    """Liquid value plus open lots at a fresh bid if available, else at cost (for concentration only)."""
    value = ledger.liquid_value(ctx.tx, account_key, owner, ccy)
    for lot in ledger.owner_lots(ctx.tx, account_key, owner):
        value = value + _lot_value(ctx, lot)
    return value


def _lot_value(ctx: Context, lot: dict) -> Fixed:
    q = ctx.desk.quotes.get(lot["instrument_id"])
    qty = Fixed.parse(lot["open_qty"])
    if q is not None and q.bid is not None:
        mult = multiplier_of(ctx, lot["instrument_id"])
        return (qty * q.bid * mult).rescale(currency(lot["currency"]).ledger_scale, Rounding.FLOOR)
    return Fixed.parse(lot["open_cost"])


def _gross_exposure(ctx: Context, account_key: str, ccy: str) -> Fixed:
    total = Fixed(0, currency(ccy).ledger_scale)
    for r in ctx.tx.all("SELECT row FROM lots WHERE account_key=?", (account_key,)):
        import json
        lot = json.loads(r["row"])
        if ledger.is_system_owner(lot["owner"]) and lot["currency"] == ccy:
            total = total + abs(_lot_value(ctx, lot))
    for o in state.orders(ctx.tx, account_key, open_only=True):
        if o["side"] == "BUY" and o["currency"] == ccy:
            total = total + (Fixed.parse(o["leaves_qty"]) * Fixed.parse(o["limit_price"]) * multiplier_of(ctx, o["instrument_id"])).rescale(
                currency(ccy).ledger_scale, Rounding.CEILING)
    return total


# ------------------------------------------------------------------ cancel
def request_cancel(ctx: Context, p: dict) -> dict:
    order = state.order(ctx.tx, p.get("order_id", ""))
    if order is None:
        raise Rejected("UNKNOWN_ORDER", str(p.get("order_id")))
    if ctx.principal.role is Role.STRATEGY and (order["principal_id"], order.get("principal_role")) != (
            ctx.principal.id, Role.STRATEGY.value):
        raise Rejected("UNAUTHORIZED", "a strategy may cancel only its own orders")
    return cancel_order(ctx, order, p.get("reason") or "requested")


def _live_cancel_row(outbox: list[dict]) -> bool:
    return any(b["action"] == "CANCEL" and b["state"] in ("PENDING", "SENDING") for b in outbox)


def cancel_order(ctx: Context, order: dict, reason: str) -> dict:
    if order["state"] in state.TERMINAL_ORDER_STATES:
        return {"order_id": order["order_id"], "state": order["state"], "noop": True}
    outbox = state.outbox_for_order(ctx.tx, order["order_id"])
    # A cancel is idempotent at the broker: it is re-issued unless one is queued or in flight, or the broker
    # already acknowledged it (a cancel lost to a crash or a timeout must never make an order uncancelable).
    if _live_cancel_row(outbox) or order["cancel_state"] == "ACKED":
        return {"order_id": order["order_id"], "state": order["state"], "noop": True}
    mult = multiplier_of(ctx, order["instrument_id"])
    submit = next((b for b in outbox if b["action"] == "SUBMIT"), None)
    if order["state"] == "RESERVED" and submit is not None and submit["state"] == "PENDING":
        new = {**order, "state": "CANCELED", "prev_state": "RESERVED", "cancel_state": "CONFIRMED", "leaves_qty": "0",
               "terminal_at_ns": ctx.now_ns, "reason": f"canceled before transmission: {reason}"}
        ctx.emit("OrderCanceledBeforeTransmission", {"reason": reason},
                 {"orders": [new], "holds": hold_rows(new, mult, release=True), "outbox": [{**submit, "state": "SKIPPED"}]},
                 account_key=order["account_key"], session_id=order["session_id"], order_id=order["order_id"])
        return {"order_id": order["order_id"], "state": "CANCELED"}
    new = {**order, "cancel_state": "REQUESTED", "reason": reason}
    if order["state"] not in ("UNKNOWN", "CANCEL_PENDING"):
        new.update(state="CANCEL_PENDING", prev_state=order["state"])
    ctx.emit("OrderCancelRequested", {"reason": reason, "note": "reservation retained until broker confirmation"},
             {"orders": [new], "outbox": [_cancel_row(ctx, order)]},
             account_key=order["account_key"], session_id=order["session_id"], order_id=order["order_id"])
    return {"order_id": order["order_id"], "state": new["state"], "cancel_state": "REQUESTED"}


def _cancel_row(ctx: Context, order: dict, *, force: bool = False) -> dict:
    return {"outbox_id": f"{order['order_id']}:cancel:{ctx.new_id()}", "order_id": order["order_id"],
            "account_key": order["account_key"], "action": "CANCEL", "state": "PENDING", "attempts": 0,
            "last_attempt_ns": None, "result": None, "force": force}


def enqueue_cancel(ctx: Context, order: dict, reason: str) -> dict:
    """Cancel an order the broker still works although it is terminal here (its reservation is gone)."""
    if _live_cancel_row(state.outbox_for_order(ctx.tx, order["order_id"])):
        return {"order_id": order["order_id"], "noop": True}
    ctx.emit("OrderCancelForced", {"reason": reason, "local_state": order["state"]},
             {"outbox": [_cancel_row(ctx, order, force=True)]},
             account_key=order["account_key"], session_id=order["session_id"], order_id=order["order_id"])
    return {"order_id": order["order_id"], "forced": True}


# ------------------------------------------------------------------ outbox
def outbox_begin(ctx: Context, p: dict) -> dict:
    """Claim an outbox row for transmission; commits SUBMIT_PENDING before any network call."""
    r = ctx.tx.one("SELECT row FROM outbox WHERE outbox_id=?", (p.get("outbox_id", ""),))
    if r is None:
        raise Rejected("UNKNOWN_OUTBOX", str(p.get("outbox_id")))
    import json
    row = json.loads(r["row"])
    if row["state"] != "PENDING":
        return {"send": False, "reason": f"outbox state {row['state']}"}
    order = state.order(ctx.tx, row["order_id"])
    key = row["account_key"]
    ctl = state.control(ctx.tx, key)
    own = state.ownership(ctx.tx, key)
    if not (own and own["status"] == "HELD" and own["deployment_id"] == ctx.desk.deployment_id):
        return {"send": False, "reason": "EXECUTION_OWNERSHIP_NOT_HELD"}
    if row["action"] == "SUBMIT" and (ctl["reconciled_epoch"] != ctx.epoch
                                      or (order["side"] == "BUY" and ctl["needs_reconciliation"])):
        # Cancels always go out (risk-reducing); sells need positions confirmed in this process.
        return {"send": False, "reason": "RECONCILIATION_REQUIRED"}
    mult = multiplier_of(ctx, order["instrument_id"])
    if row["action"] == "SUBMIT":
        acct = state.account(ctx.tx, key)
        mode = ctx.desk.health().broker_account_mode
        if mode is None or mode != acct["mode"] or mode == "LIVE":
            # Checked at transmission, not only at green start: a reconnect may reach a different account
            # (review finding 21). The order stays RESERVED and is retried once the connection checks out.
            return {"send": False, "reason": f"BROKER_ACCOUNT_MODE:{mode}"}
        if order["state"] != "RESERVED":
            ctx.emit("OutboxSkipped", {"reason": f"order state {order['state']}"}, {"outbox": [{**row, "state": "SKIPPED"}]},
                     account_key=key, order_id=order["order_id"])
            return {"send": False, "reason": f"order state {order['state']}"}
        blocked = None
        if order["side"] == "BUY":
            from .common import entries_permitted
            ok, blockers = entries_permitted(ctx, key)
            blocked = None if ok else ",".join(blockers)
        if blocked is None and ctx.now_ns > order["deadline_ns"]:
            new = {**order, "state": "EXPIRED", "prev_state": "RESERVED", "leaves_qty": "0", "terminal_at_ns": ctx.now_ns,
                   "reason": "intent deadline passed before transmission"}
            ctx.emit("OrderExpiredBeforeTransmission", {}, {"orders": [new], "holds": hold_rows(new, mult, release=True),
                                                           "outbox": [{**row, "state": "SKIPPED"}]},
                     account_key=key, session_id=order["session_id"], order_id=order["order_id"])
            return {"send": False, "reason": "DEADLINE_EXPIRED"}
        if blocked is not None:
            new = {**order, "state": "CANCELED", "prev_state": "RESERVED", "cancel_state": "CONFIRMED", "leaves_qty": "0",
                   "terminal_at_ns": ctx.now_ns, "reason": f"entry blocked before transmission: {blocked}"}
            ctx.emit("OrderCanceledBeforeTransmission", {"reason": blocked},
                     {"orders": [new], "holds": hold_rows(new, mult, release=True), "outbox": [{**row, "state": "SKIPPED"}]},
                     account_key=key, session_id=order["session_id"], order_id=order["order_id"])
            return {"send": False, "reason": blocked}
        new_row = {**row, "state": "SENDING", "attempts": row["attempts"] + 1, "last_attempt_ns": ctx.now_ns}
        new_order = {**order, "state": "SUBMIT_PENDING", "prev_state": "RESERVED", "submitted_at_ns": ctx.now_ns}
        ctx.emit("OrderSubmitStarted", {"attempt": new_row["attempts"]}, {"orders": [new_order], "outbox": [new_row]},
                 account_key=key, session_id=order["session_id"], order_id=order["order_id"])
        inst = state.instrument(ctx.tx, order["instrument_id"])
        provider_ref = _provider_ref(ctx, order["instrument_id"])
        return {"send": True, "action": "SUBMIT", "order": {
            "client_order_id": order["order_id"], "order_ref": order["order_ref"], "instrument_id": order["instrument_id"],
            "provider_ref": provider_ref, "side": order["side"], "quantity": order["quantity"],
            "limit_price": order["limit_price"], "currency": order["currency"], "time_in_force": order["time_in_force"],
            "outside_regular_hours": False, "symbol": inst.symbol if inst else None}}
    # CANCEL
    if order["state"] in state.TERMINAL_ORDER_STATES and not row.get("force"):
        ctx.emit("OutboxSkipped", {"reason": "order already terminal"}, {"outbox": [{**row, "state": "SKIPPED"}]},
                 account_key=key, order_id=order["order_id"])
        return {"send": False, "reason": "order terminal"}
    new_row = {**row, "state": "SENDING", "attempts": row["attempts"] + 1, "last_attempt_ns": ctx.now_ns}
    ctx.emit("OrderCancelSending", {}, {"orders": [] if order["state"] in state.TERMINAL_ORDER_STATES
                                          else [{**order, "cancel_state": "SENT"}], "outbox": [new_row]},
             account_key=key, session_id=order["session_id"], order_id=order["order_id"])
    return {"send": True, "action": "CANCEL", "client_order_id": order["order_id"], "broker_order_id": order["broker_order_id"]}


def _provider_ref(ctx: Context, instrument_id: str) -> str:
    acct_provider = None
    r = ctx.tx.one("SELECT provider, external_id FROM provider_mappings WHERE instrument_id=? ORDER BY version DESC", (instrument_id,))
    if r is not None:
        acct_provider = r["external_id"]
    return acct_provider or instrument_id


def outbox_result(ctx: Context, p: dict) -> dict:
    import json
    r = ctx.tx.one("SELECT row FROM outbox WHERE outbox_id=?", (p.get("outbox_id", ""),))
    if r is None:
        raise Rejected("UNKNOWN_OUTBOX", str(p.get("outbox_id")))
    row = json.loads(r["row"])
    status = p.get("status")
    if status not in ("ACCEPTED", "REJECTED", "UNKNOWN"):
        raise Rejected("INVALID_FIELD", "status must be ACCEPTED, REJECTED or UNKNOWN")
    if row["state"] != "SENDING":
        return {"applied": False, "reason": f"outbox state {row['state']}"}
    order = state.order(ctx.tx, row["order_id"])
    key = row["account_key"]
    mult = multiplier_of(ctx, order["instrument_id"])
    reason = p.get("reason", "")
    result = {"status": status, "reason": reason, "broker_order_id": p.get("broker_order_id"), "at_ns": ctx.now_ns}
    refs = dict(account_key=key, session_id=order["session_id"], order_id=order["order_id"])
    if row["action"] == "SUBMIT":
        if status == "ACCEPTED":
            new = {**order, "broker_order_id": p.get("broker_order_id") or order["broker_order_id"]}
            if order["state"] in ("SUBMIT_PENDING", "UNKNOWN"):
                new.update(state="PARTIALLY_FILLED" if Fixed.parse(order["cum_qty"]).is_positive() else "ACKNOWLEDGED",
                           prev_state=order["state"])
            ctx.emit("OrderAcknowledged", {"via": "submit_result"}, {"orders": [new], "outbox": [{**row, "state": "DONE", "result": result}]}, **refs)
        elif status == "REJECTED":
            if order["state"] in state.TERMINAL_ORDER_STATES and not Fixed.parse(order["cum_qty"]).is_positive():
                # A broker report already ended the order (e.g. CANCELED) before the submit result was recorded: a
                # terminal state is never left or relabelled (order transition table); the result is kept on the row.
                ctx.emit("SubmitResultAfterTerminal", {"reason": reason, "local_state": order["state"]},
                         {"outbox": [{**row, "state": "DONE", "result": result}]}, **refs)
            elif Fixed.parse(order["cum_qty"]).is_positive():
                case = new_exception(ctx, key, "EXECUTION_ANOMALY", blocking=True, severity="high",
                                     detail={"order_id": order["order_id"], "issue": "rejection after fills", "reason": reason},
                                     key=f"reject-after-fill:{order['order_id']}", session_id=order["session_id"],
                                     order_id=order["order_id"], owner_hint=order_owner(order))
                ctx.emit("OrderRejectContradictsFills", {"reason": reason},
                         {"outbox": [{**row, "state": "DONE", "result": result}], "exceptions": [case] if case else []}, **refs)
            else:
                new = {**order, "state": "REJECTED", "prev_state": order["state"], "leaves_qty": "0",
                       "terminal_at_ns": ctx.now_ns, "reason": reason}
                ctx.emit("OrderRejected", {"reason": reason, "via": "submit_result"},
                         {"orders": [new], "holds": hold_rows(new, mult, release=True),
                          "outbox": [{**row, "state": "DONE", "result": result}]}, **refs)
        else:
            new = {**order, "state": "UNKNOWN", "prev_state": order["state"], "reason": reason or "submission outcome unknown"}
            case = new_exception(ctx, key, UNKNOWN_ORDER, blocking=True, severity="high",
                                 detail={"order_id": order["order_id"], "reason": reason, "rule": "lookup before any retry"},
                                 key=f"unknown-order:{order['order_id']}", session_id=order["session_id"], order_id=order["order_id"])
            ctx.emit("OrderOutcomeUnknown", {"reason": reason, "note": "reservation retained; reconcile before retry"},
                     {"orders": [new] if order["state"] == "SUBMIT_PENDING" else [],
                      "outbox": [{**row, "state": "UNKNOWN", "result": result}], "exceptions": [case] if case else []}, **refs)
        return {"applied": True, "status": status}
    # CANCEL results: acceptance is not confirmation (ORD-04)
    if status == "ACCEPTED":
        ctx.emit("OrderCancelAcknowledged", {"note": "awaiting broker cancellation confirmation"},
                 {"orders": [{**order, "cancel_state": "ACKED"}] if order["state"] not in state.TERMINAL_ORDER_STATES else [],
                  "outbox": [{**row, "state": "DONE", "result": result}]}, **refs)
    elif status == "REJECTED":
        new = {**order, "cancel_state": "REJECTED", "reason": reason}
        if order["state"] == "CANCEL_PENDING":
            restored = order["prev_state"] or "ACKNOWLEDGED"
            if restored in ("ACKNOWLEDGED", "SUBMIT_PENDING", "UNKNOWN", "RESERVED") and Fixed.parse(order["cum_qty"]).is_positive():
                restored = "PARTIALLY_FILLED"  # fills arrived while the cancel was pending
            new.update(state=restored, prev_state="CANCEL_PENDING")
        ctx.emit("OrderCancelRejected", {"reason": reason},
                 {"orders": [new] if order["state"] not in state.TERMINAL_ORDER_STATES else [],
                  "outbox": [{**row, "state": "DONE", "result": result}]}, **refs)
    else:
        live = order["state"] not in state.TERMINAL_ORDER_STATES
        # Like an unknown submission: the order may still be working, so it blocks new exposure until the
        # broker reports its state; a later stop or reconciliation re-issues the cancel.
        case = new_exception(ctx, key, UNKNOWN_ORDER, blocking=True, severity="high",
                             detail={"order_id": order["order_id"], "reason": reason or "cancel outcome unknown",
                                     "rule": "the order may still be working; look up before relying on the cancel"},
                             key=f"unknown-order:{order['order_id']}", session_id=order["session_id"],
                             order_id=order["order_id"]) if live else None
        ctx.emit("OrderCancelOutcomeUnknown", {"reason": reason},
                 {"orders": [{**order, "cancel_state": "UNKNOWN"}] if live else [],
                  "outbox": [{**row, "state": "UNKNOWN", "result": result}], "exceptions": [case] if case else []}, **refs)
    return {"applied": True, "status": status}
