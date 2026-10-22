"""Marked valuation: target observation, session drawdown and account loss budgets
(SES-04, RSK-02; book-review resolutions on loss measurement).

Equity marks long inventory at a fresh executable bid. If any held position
lacks a fresh bid, the valuation is incomplete and the account fails closed:
``valuation_ok`` becomes false and exposure-increasing orders are refused.

Loss budgets (resolution of the book's net/gross ambiguity):
* cumulative loss = high-water mark of system P&L minus current system P&L,
  so earlier gains (including protected proceeds) never refill the allowance;
* daily loss = system P&L at the first valuation of the risk day minus current
  P&L, with the day defined in the policy's ``risk_day_timezone``.
System P&L = marked value of every system-owned bucket minus net capital
authorized into the system (``authorized_total``)."""
from __future__ import annotations

from ...domain.calendar import local_date
from ...domain.fixed import Fixed, Rounding, fmax
from ...domain.money import currency
from .. import ledger, state
from ..desk import Context
from .common import multiplier_of
from .control import latch_stop
from .sessions import begin_wind_down, suspend

VALUATION_MAX_AGE_NS = 120 * 1_000_000_000


def _marked(ctx: Context, key: str, owner: str, ccy: str, max_age_ns: int) -> tuple[Fixed, list[str], dict]:
    value = ledger.liquid_value(ctx.tx, key, owner, ccy)
    stale: list[str] = []
    marks: dict = {}
    scale = currency(ccy).ledger_scale
    for lot in ledger.owner_lots(ctx.tx, key, owner):
        q = ctx.desk.quotes.get(lot["instrument_id"])
        if q is None or q.bid is None or q.delayed or q.age_ns(ctx.now_ns) > max_age_ns:
            stale.append(lot["instrument_id"])
            continue
        qty = Fixed.parse(lot["open_qty"])
        value = value + (qty * q.bid * multiplier_of(ctx, lot["instrument_id"])).rescale(scale, Rounding.FLOOR)
        marks[lot["instrument_id"]] = q.to_json(ctx.now_ns)
    return value, sorted(set(stale)), marks


def valuation(ctx: Context, p: dict) -> dict:
    key = p["account_key"]
    policy = state.active_policy(ctx.tx, key)
    ccy = policy.currency
    max_age = policy.limits.quote_max_age_ms * 1_000_000
    out: dict = {"sessions": {}}
    for s in state.sessions(ctx.tx, key, ("RUNNING", "SUSPENDED", "WINDING_DOWN")):
        s = state.session(ctx.tx, s["session_id"])
        m = state.mandate(ctx.tx, s["mandate_hash"])
        equity, stale, marks = _marked(ctx, key, ledger.session_owner(s["session_id"]), ccy, max_age)
        out["sessions"][s["session_id"]] = {"equity": equity.to_str(), "stale": stale}
        if stale:
            continue  # cannot observe a target or drawdown on incomplete marks
        evidence = {"equity": equity.to_str(), "marks": marks, "target": m.target_terminal_equity.amount.to_str(),
                    "high_water": s["high_water"]}
        hw = Fixed.parse(s["high_water"])
        if equity > hw:
            s = {**s, "high_water": equity.to_str()}
            ctx.emit("SessionValued", {"equity": equity.to_str(), "new_high_water": True}, {"sessions": [s]},
                     account_key=key, session_id=s["session_id"])
            hw = equity
        if s["state"] != "RUNNING":
            continue
        if equity >= m.target_terminal_equity.amount:
            begin_wind_down(ctx, s, "TARGET_OBSERVED", evidence)
            continue
        dd_floor = (hw * (10_000 - m.session_drawdown_trigger_bps)).div(10_000, hw.scale, Rounding.CEILING)
        if equity <= dd_floor:
            suspend(ctx, s, "SESSION_DRAWDOWN", {**evidence, "drawdown_floor": dd_floor.to_str()})
            continue
        if m.seed.amount - equity >= m.session_max_loss.amount:
            suspend(ctx, s, "SESSION_MAX_LOSS", {**evidence, "session_max_loss": m.session_max_loss.amount.to_str()})

    # Account loss budgets over every system-owned bucket.
    owners = {r["owner"] for r in ctx.tx.all("SELECT DISTINCT owner FROM balances WHERE account_key=?", (key,))}
    owners |= {r["owner"] for r in ctx.tx.all("SELECT DISTINCT owner FROM lots WHERE account_key=?", (key,))}
    system_value = Fixed(0, currency(ccy).ledger_scale)
    stale_all: list[str] = []
    for owner in sorted(o for o in owners if ledger.is_system_owner(o)):
        v, stale, _ = _marked(ctx, key, owner, ccy, max_age)
        system_value = system_value + v
        stale_all += stale
    r = state.risk_state(ctx.tx, key)
    new = dict(r)
    valuation_ok = not stale_all
    new["valuation_ok"] = valuation_ok
    new["stale_instruments"] = sorted(set(stale_all))
    breach = None
    if valuation_ok:
        pnl = system_value - Fixed.parse(r["authorized_total"])
        hwm = fmax(Fixed.parse(r["pnl_high_water"]), pnl)
        day = local_date(ctx.now_ns, policy.limits.risk_day_timezone).isoformat()
        day_start = Fixed.parse(r["day_start_pnl"]) if r.get("risk_day") == day else pnl
        new.update(cumulative_pnl=pnl.to_str(), pnl_high_water=hwm.to_str(), risk_day=day, day_start_pnl=day_start.to_str())
        if hwm - pnl >= policy.limits.cumulative_loss_allowance.amount:
            breach = "CUMULATIVE_LOSS_FROM_HIGH_WATER"
        elif day_start - pnl >= policy.limits.daily_loss_trigger.amount:
            breach = "DAILY_LOSS"
        new["breach"] = breach
        if breach and not r.get("breach"):
            new["breach_at_ns"] = ctx.now_ns
    else:
        new["breach"] = r.get("breach")
    new["last_valuation_ns"] = ctx.now_ns
    decision_changed = any(new.get(k) != r.get(k) for k in ("valuation_ok", "breach", "risk_day", "pnl_high_water", "stale_instruments"))
    if decision_changed:
        ctx.emit("RiskStateUpdated", {"system_value": system_value.to_str(), "breach": new.get("breach"),
                                      "valuation_ok": valuation_ok, "stale": new["stale_instruments"]},
                 {"risk_state": [new]}, account_key=key)
    if breach and not r.get("breach"):
        latch_stop(ctx, key, f"LOSS_BUDGET:{breach}")
    out.update(system_value=system_value.to_str(), breach=new.get("breach"), valuation_ok=valuation_ok)
    return out
