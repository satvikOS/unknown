"""Operational stress scenarios on the actual positions (CAP-PORT-11; book Ch. 6 'loss tails and path dependence').

Each scenario revalues the real holdings exactly (formula stress.revalue@1) and then runs
the core's own reaction on a private sandbox copy (``whatif.Sandbox``): the valuation
command under the shocked or aged quotes, and dry-run exits/entries through the live
pretrade code. Nothing assumes that a stop or a drawdown trigger executes at its
threshold: a trigger *requests* reduction; the scenario loss is never capped at it.

Scenarios
  overnight_gap      every (or a named) instrument opens gapped by s; resting buy limits above
                     the gapped ask fill at their limit; resting sell limits above the gapped bid
                     do not fill
  correlated_selloff s_i = beta_i * s_m for every position (beta 1 unless supplied): the
                     diversification of the book is removed
  stale_quote        no new quote arrives for ``delay_s``: which marks go stale, what the core's
                     valuation does, and which orders the pretrade gate then refuses
  fee_revision       the broker's final fees come in at f times the estimate model; pending estimates
                     are finalised at the revised amount in the sandbox, exit costs and open-buy
                     envelopes are recomputed

All results are labelled hypothetical and never feed trading (CAP-PORT-11 behaviour)."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from fractions import Fraction

from ..core import ledger
from ..core.handlers.common import order_owner
from ..domain.calendar import local_date
from ..domain.fixed import Fixed, Rounding, fmax
from ..domain.money import Money, currency
from ..market.quotes import Quote
from ..risk import fees as fee_model
from .common import ACCOUNT, SYSTEM, Scope, fixed_of_fraction, lineage, parse_fraction
from .holdings import holdings, load_book
from .marks import normalize_quotes
from .whatif import HYPOTHETICAL, Sandbox

SHOCK_SCALE = 10


def _keep_scale(value: Fixed, like: Fixed) -> Fixed:
    """Drop the zeros a shock factor appends, but never show fewer decimals than the original price."""
    n = value.normalized()
    return n.rescale(max(n.scale, like.scale))


def _shocked_quotes(book, quotes: dict[str, Quote], shocks: dict[str, Fraction], now_ns: int) -> dict[str, Quote]:
    """Exact shocked quotes p' = p * (1 + s) (no tick rounding, so the analytic P&L and the core's reaction use the
    same prices); stamped at the scenario instant and labelled as scenario data."""
    out = {}
    for iid, q in quotes.items():
        s = shocks.get(iid)
        if s is None:
            out[iid] = q
            continue
        factor = Fixed(1) + fixed_of_fraction(s, SHOCK_SCALE)

        def shock(p: Fixed | None) -> Fixed | None:
            if p is None:
                return None
            v = _keep_scale(p * factor, p)
            return v if v.is_positive() else None
        out[iid] = dataclasses.replace(q, bid=shock(q.bid), ask=shock(q.ask), last=shock(q.last), event_time_ns=now_ns,
                                       receipt_time_ns=now_ns, source=f"{q.source}+scenario")
    return out


def _revalue(h: dict, book, shocked: dict[str, Quote], shocks: dict[str, Fraction]) -> tuple[list[dict], dict, list[dict]]:
    rows, unmapped = [], []
    per_owner: dict[tuple[str, str], Fixed] = {}
    for p in h["positions"]:
        iid = p["instrument_id"]
        if p["mark"]["price"] is None:
            unmapped.append({"instrument_id": iid, "owner": p["owner"], "reason": "unpriced: cannot be revalued",
                             "cost_basis": p["cost_basis"]})
            continue
        if iid not in shocks:
            unmapped.append({"instrument_id": iid, "owner": p["owner"], "reason": "no shock mapped to this instrument",
                             "market_value": p["market_value"]})
            continue
        scale = currency(p["currency"]).ledger_scale
        price = Fixed.parse(p["mark"]["price"])
        new_price = _keep_scale(price * (Fixed(1) + fixed_of_fraction(shocks[iid], SHOCK_SCALE)), price)
        qty, mult = Fixed.parse(p["quantity"]), Fixed.parse(p["multiplier"])
        pnl = (qty * mult * (new_price - price)).rescale(scale, Rounding.FLOOR)
        key = (p["owner"], p["currency"])
        per_owner[key] = per_owner.get(key, Fixed(0, scale)) + pnl
        rows.append({"owner": p["owner"], "instrument_id": iid, "currency": p["currency"], "quantity": p["quantity"],
                     "shock": str(float(shocks[iid])), "price_before": price.to_str(), "price_after": new_price.to_str(),
                     "market_value_before": p["market_value"],
                     "market_value_after": (qty * new_price * mult).rescale(scale, Rounding.FLOOR).to_str(),
                     "pnl": pnl.to_str(), "mark_status_before": p["mark"]["status"]})
    return rows, per_owner, unmapped


def _gap_orders(book, scope: Scope, quotes: dict[str, Quote], shocked: dict[str, Quote]) -> tuple[list[dict], dict]:
    """Resting orders through the gap (formula stress.gap_fills@1)."""
    out, extra = [], {}
    for o in book.open_orders:
        owner = order_owner(o) if (o.get("owner") or o.get("session_id")) else None   # DISPOSAL: its fixed owner
        if owner is None or not scope.includes(owner):
            continue
        iid = o["instrument_id"]
        q1 = shocked.get(iid)
        if q1 is None or quotes.get(iid) is q1:
            out.append({"order_id": o["order_id"], "side": o["side"], "effect": "instrument not shocked"})
            continue
        leaves, limit = Fixed.parse(o["leaves_qty"]), Fixed.parse(o["limit_price"])
        mult = book.multiplier(iid)
        scale = currency(o["currency"]).ledger_scale
        if o["side"] == "BUY":
            if q1.ask is not None and q1.ask <= limit and q1.bid is not None:
                loss = (leaves * mult * (q1.bid - limit)).rescale(scale, Rounding.FLOOR)
                key = (owner, o["currency"])
                extra[key] = extra.get(key, Fixed(0, scale)) + loss
                out.append({"order_id": o["order_id"], "side": "BUY", "limit": limit.to_str(), "gapped_ask": q1.ask.to_str(),
                            "effect": "fills at its limit through the gap (adds exposure into the fall)",
                            "quantity": leaves.to_str(), "immediate_marked_pnl": loss.to_str(),
                            "cash": "paid from the existing reservation; no new cash is created or released"})
            else:
                out.append({"order_id": o["order_id"], "side": "BUY", "effect": "does not fill"})
        else:
            fills = q1.bid is not None and q1.bid >= limit
            row = {"order_id": o["order_id"], "side": "SELL", "limit": limit.to_str(),
                   "gapped_bid": q1.bid.to_str() if q1.bid is not None else None,
                   "effect": "fills at its limit" if fills else
                   "does not fill: the exit limit is above the gapped market; the position stays exposed"}
            if fills:
                # The sold quantity earns its limit, not the gapped bid the position revaluation applied to it
                # (review fix: a gap up through a resting sell overstated the scenario gain by q*(bid' - L)).
                adj = (leaves * mult * (limit - q1.bid)).rescale(scale, Rounding.FLOOR)
                key = (owner, o["currency"])
                extra[key] = extra.get(key, Fixed(0, scale)) + adj
                row.update(quantity=leaves.to_str(), immediate_marked_pnl=adj.to_str(),
                           note="sold at the limit instead of being marked at the gapped bid")
            out.append(row)
    return out, extra


def _account_thresholds(book, system_h: dict, pnl: dict[tuple[str, str], Fixed], now_ns: int, consequence: str) -> list[dict]:
    """Account loss budgets measured the way core/handlers/valuation.py measures them (review fix).

    System P&L = marked value of every system-owned bucket minus ``authorized_total``, taken from the CURRENT marks
    (not the P&L of the last core valuation, which misses every move since), plus the scenario P&L of the system
    owners in this scope. The daily window follows the core's rule: if the core has not valued yet on the scenario's
    risk day, the shocked valuation is the first of that day and sets the day start, so the daily loss is zero; the
    earlier code compared against the first valuation of an older day and reported a breach the core does not have.
    The loss against the last core valuation (e.g. the previous close) is shown separately as what a window starting
    there would measure; that window is an owner decision, not the core's rule."""
    ccy = book.base_currency
    lim = book.policy.limits
    risk = book.risk
    sys_pnl = sum((v for (o, c), v in pnl.items() if c == ccy and ledger.is_system_owner(o)), Fixed(0))
    rows = [s for s in system_h["owners"] if s["currency"] == ccy and ledger.is_system_owner(s["owner"])]
    unpriced = sorted({u["instrument_id"] for s in rows for u in s["unpriced_positions"]})
    stale = sorted({i for s in rows for i in s["stale_instruments"]})
    base = {"basis": "system P&L at current marks (every system-owned bucket minus authorized capital) plus the scenario "
                     "P&L of the system owners in this scope; the core reaction below covers the whole account",
            "consequence": consequence}
    if unpriced:
        why = f"system value unavailable: unpriced {', '.join(unpriced)}"
        return [{"threshold": t, "level": lvl, "crossed": None, "reason": why, **base}
                for t, lvl in (("daily_loss_trigger", lim.daily_loss_trigger.amount.to_str()),
                               ("cumulative_loss_allowance", lim.cumulative_loss_allowance.amount.to_str()))]
    system_value = sum((Fixed.parse(s["nav_marked"]) for s in rows), Fixed(0))
    now_pnl = system_value - Fixed.parse(risk["authorized_total"])
    after = now_pnl + sys_pnl
    today = local_date(now_ns, lim.risk_day_timezone).isoformat()
    same_day = risk.get("risk_day") == today
    day_start = Fixed.parse(risk["day_start_pnl"]) if same_day else after
    hwm = fmax(Fixed.parse(risk["pnl_high_water"]), after)
    last = Fixed.parse(risk["cumulative_pnl"]) if risk.get("last_valuation_ns") else None
    marks_note = f"includes stale last marks for {', '.join(stale)}" if stale else None
    return [
        {"threshold": "daily_loss_trigger", "level": lim.daily_loss_trigger.amount.to_str(),
         "system_pnl_before": now_pnl.to_str(), "system_pnl_after": after.to_str(),
         "risk_day": today, "day_start_pnl": day_start.to_str(),
         "day_start_source": "first core valuation of this risk day" if same_day else
         "none yet on this risk day: the scenario valuation would be the first and sets the day start",
         "loss_after": (day_start - after).to_str(), "crossed": day_start - after >= lim.daily_loss_trigger.amount,
         "loss_since_last_core_valuation": (last - after).to_str() if last is not None else None,
         "crossed_if_window_started_at_last_core_valuation":
             (last - after >= lim.daily_loss_trigger.amount) if last is not None else None,
         "marks_note": marks_note,
         "caveat": "the core measures daily loss from its first valuation of each risk day, so a gap that is already in "
                   "the marks at that first valuation counts only against the cumulative allowance", **base},
        {"threshold": "cumulative_loss_allowance", "level": lim.cumulative_loss_allowance.amount.to_str(),
         "system_pnl_after": after.to_str(), "pnl_high_water": hwm.to_str(),
         "loss_after": (hwm - after).to_str(), "crossed": hwm - after >= lim.cumulative_loss_allowance.amount,
         "marks_note": marks_note, **base},
    ]


def _thresholds(book, h: dict, pnl: dict[tuple[str, str], Fixed], system_h: dict, now_ns: int) -> list[dict]:
    out = []
    summaries = {(s["owner"], s["currency"]): s for s in h["owners"]}
    consequence = ("the core requests reduction (suspend, wind-down or stop latch); execution at the threshold is not "
                   "guaranteed, so the scenario loss is not capped")
    for (owner, ccy), d in sorted(pnl.items()):
        if not owner.startswith("session:") or owner not in book.mandates:
            continue
        m = book.mandates[owner]
        s = book.sessions.get(owner.split(":", 1)[1], {})
        nav = summaries.get((owner, ccy), {}).get("nav_marked")
        if nav is None:
            continue
        before = Fixed.parse(nav)
        after = before + d
        hw = Fixed.parse(s.get("high_water") or nav)
        floor = (hw * (10_000 - int(m["session_drawdown_trigger_bps"]))).div(10_000, hw.scale, Rounding.CEILING)
        out.append({"threshold": "session_drawdown_trigger", "owner": owner, "level": floor.to_str(),
                    "equity_before": before.to_str(), "equity_after": after.to_str(), "crossed": after <= floor,
                    "loss_beyond_threshold": (floor - after).to_str() if after < floor else "0",
                    "consequence": consequence})
        seed, max_loss = Fixed.parse(m["seed"]), Fixed.parse(m["session_max_loss"])
        out.append({"threshold": "session_max_loss", "owner": owner, "level": (seed - max_loss).to_str(),
                    "equity_after": after.to_str(), "crossed": seed - after >= max_loss,
                    "loss_beyond_threshold": (max_loss - (seed - after)).to_str() if seed - after > max_loss else "0",
                    "consequence": consequence})
    if book.risk and book.policy:
        out += _account_thresholds(book, system_h, pnl, now_ns, consequence)
    return out


def _core_reaction(conn, account_key: str, quotes: dict[str, Quote], *, now_ns: int, deployment_id: str | None) -> dict:
    """Run the core valuation command on a sandbox copy with the scenario quotes; report what the core did."""
    with Sandbox(conn, now_ns=now_ns, quotes=quotes, deployment_id=deployment_id, account_key=account_key) as sb:
        seq = sb.seq
        result = sb.execute("valuation", {"account_key": account_key})
        events = sb.events_since(seq)
        control = sb.row("control", "account_key", account_key)
        risk = sb.row("risk_state", "account_key", account_key)
    # The copy can be newer than the read that produced the analytic P&L if the live core moved in between; the
    # sequence it was taken at is reported so the two can be compared (core_seq at the top level).
    return {"computed_on_core_seq": seq, "valuation_result": result.body,
            "events": [{"type": e["type"], "session_id": e["session_id"], "info": e["info"]} for e in events],
            "stop_latched_after": control["stop_latched"] if control else None,
            "latch_reason_after": control.get("latch_reason") if control else None,
            "valuation_ok_after": risk.get("valuation_ok") if risk else None, "breach_after": risk.get("breach") if risk else None}


def _shock_scenario(name: str, conn, account_key: str, quotes, shocks_for, *, now_ns: int, scope: Scope,
                    deployment_id: str | None, params: dict) -> dict:
    qs = normalize_quotes(quotes)
    book = load_book(conn, account_key)
    h = holdings(conn, account_key, qs, now_ns=now_ns, scope=scope, book=book)
    system_h = h if scope == SYSTEM else holdings(conn, account_key, qs, now_ns=now_ns, scope=SYSTEM, book=book)
    shocks = shocks_for(h)
    shocked = _shocked_quotes(book, qs, shocks, now_ns)
    rows, per_owner, unmapped = _revalue(h, book, shocked, shocks)
    orders, extra = _gap_orders(book, scope, qs, shocked)
    total: dict[tuple[str, str], Fixed] = dict(per_owner)
    for k, v in extra.items():
        total[k] = total.get(k, Fixed(0)) + v
    by_ccy: dict[str, Fixed] = {}
    for (_, c), v in total.items():
        by_ccy[c] = by_ccy.get(c, Fixed(0, currency(c).ledger_scale)) + v
    reaction = _core_reaction(conn, account_key, shocked, now_ns=now_ns, deployment_id=deployment_id)
    return {"kind": "stress", "scenario": name, "label": HYPOTHETICAL, "account_key": account_key, "scope": scope.label,
            "as_of_ns": now_ns, "core_seq": book.core_seq, "parameters": params,
            "positions": rows, "resting_orders": orders, "unmapped": unmapped,
            "pnl_by_owner": [{"owner": o, "currency": c, "pnl": v.to_str()} for (o, c), v in sorted(total.items())],
            "pnl_by_currency": {c: v.to_str() for c, v in sorted(by_ccy.items())},
            "thresholds": _thresholds(book, h, total, system_h, now_ns), "core_reaction": reaction,
            "fixture_data": h["fixture_data"], "fixture_note": h["fixture_note"],
            "lineage": lineage(["stress.revalue", "stress.gap_fills", "port.market_value"],
                               inputs={"core_seq": book.core_seq, "quotes": sorted(qs)}, units="currency (P&L signed)",
                               source_dates={"as_of_ns": now_ns}, missing=unmapped,
                               notes=["shocked prices are exact p * (1 + s), not rounded to a tick; shocks are held to "
                                      f"{SHOCK_SCALE} decimal places"])}


def overnight_gap(conn: sqlite3.Connection, account_key: str, quotes, *, now_ns: int, gap="-0.10",
                  per_instrument: dict | None = None, scope: Scope = ACCOUNT, deployment_id: str | None = None) -> dict:
    g = parse_fraction(gap, "gap")
    overrides = {k: parse_fraction(v, f"gap for {k}") for k, v in (per_instrument or {}).items()}
    if g <= -1 or any(v <= -1 for v in overrides.values()):
        raise ValueError("a gap must be greater than -100%")

    def shocks(h):
        return {p["instrument_id"]: overrides.get(p["instrument_id"], g) for p in h["positions"]} | \
               {iid: overrides.get(iid, g) for iid in normalize_quotes(quotes)}
    return _shock_scenario("overnight_gap", conn, account_key, quotes, shocks, now_ns=now_ns, scope=scope,
                           deployment_id=deployment_id, params={"gap": str(gap), "per_instrument": per_instrument or {}})


def correlated_selloff(conn: sqlite3.Connection, account_key: str, quotes, *, now_ns: int, market_shock="-0.20",
                       betas: dict | None = None, scope: Scope = ACCOUNT, deployment_id: str | None = None) -> dict:
    sm = parse_fraction(market_shock, "market shock")
    bs = {k: parse_fraction(v, f"beta for {k}") for k, v in (betas or {}).items()}

    def shocks(h):
        ids = {p["instrument_id"] for p in h["positions"]} | set(normalize_quotes(quotes))
        out = {iid: bs.get(iid, Fraction(1)) * sm for iid in ids}
        if any(v <= -1 for v in out.values()):
            raise ValueError("beta * market shock must be greater than -100% for every instrument")
        return out
    res = _shock_scenario("correlated_selloff", conn, account_key, quotes, shocks, now_ns=now_ns, scope=scope,
                          deployment_id=deployment_id,
                          params={"market_shock": str(market_shock), "betas": betas or {},
                                  "beta_note": "beta 1.0 assumed where none is supplied (no estimate used)"})
    res["note"] = "every position moves with the market factor: correlation 1, no diversification benefit"
    return res


def stale_quote(conn: sqlite3.Connection, account_key: str, quotes, *, now_ns: int, delay_s: int = 600,
                haircut="-0.05", scope: Scope = ACCOUNT, deployment_id: str | None = None) -> dict:
    """No quote update for ``delay_s`` seconds after ``now_ns``; evaluate marks, NAV and the order gate at that instant."""
    qs = normalize_quotes(quotes)
    later = now_ns + delay_s * 1_000_000_000
    book = load_book(conn, account_key)
    h0 = holdings(conn, account_key, qs, now_ns=now_ns, scope=scope, book=book)
    h1 = holdings(conn, account_key, qs, now_ns=later, scope=scope, book=book)
    hc = parse_fraction(haircut, "haircut")
    became = [p for p, p0 in zip(h1["positions"], h0["positions"]) if p["mark"]["status"] == "stale" and p0["mark"]["status"] == "fresh"]
    conservative = []
    for p in h1["positions"]:
        if p["mark"]["status"] != "stale":
            continue
        scale = currency(p["currency"]).ledger_scale
        last = Fixed.parse(p["mark"]["price"])
        val = (Fixed.parse(p["quantity"]) * last * (Fixed(1) + fixed_of_fraction(hc, SHOCK_SCALE)) * Fixed.parse(p["multiplier"])
               ).rescale(scale, Rounding.FLOOR)
        conservative.append({"owner": p["owner"], "instrument_id": p["instrument_id"], "last_mark": last.to_str(),
                             "age_ms": p["mark"]["age_ms"], "value_at_last_mark": p["market_value"],
                             "value_after_haircut": val.to_str()})
    reaction = _core_reaction(conn, account_key, qs, now_ns=later, deployment_id=deployment_id)
    gate = []
    with Sandbox(conn, now_ns=later, quotes=qs, deployment_id=deployment_id, account_key=account_key) as sb:
        sb.execute("valuation", {"account_key": account_key})
        for p in h1["positions"]:
            if not p["owner"].startswith("session:") or p["mark"]["price"] is None:
                continue
            sid = p["owner"].split(":", 1)[1]
            unreserved = Fixed.parse(p["quantity"]) - Fixed.parse(p["reserved_quantity"])
            q = qs.get(p["instrument_id"])
            for side, qty, px in (("SELL", unreserved, q.bid if q else None), ("BUY", Fixed(1), q.ask if q else None)):
                if px is None or not qty.is_positive():
                    continue
                r = sb.intent(account_key, {"session_id": sid, "instrument_id": p["instrument_id"], "side": side,
                                            "quantity": qty.to_str(), "limit_price": px.to_str(),
                                            "order_id": f"stale-{side.lower()}-{p['instrument_id']}"})
                gate.append({"owner": p["owner"], "instrument_id": p["instrument_id"], "side": side, "quantity": qty.to_str(),
                             "limit_price": px.to_str(), "approved": r["approved"], "code": r["code"],
                             "failed_checks": [c for c in (r["decision"] or {}).get("checks", []) if not c["passed"]]})
    return {"kind": "stress", "scenario": "stale_quote", "label": HYPOTHETICAL, "account_key": account_key,
            "scope": scope.label, "as_of_ns": now_ns, "evaluated_at_ns": later, "core_seq": book.core_seq,
            "parameters": {"delay_s": delay_s, "haircut": str(haircut),
                           "order_freshness_budget_ms": book.policy.limits.quote_max_age_ms if book.policy else None},
            "became_stale": [{"owner": p["owner"], "instrument_id": p["instrument_id"], "age_ms": p["mark"]["age_ms"]} for p in became],
            "fresh_nav_before": {c: t["nav_fresh"] for c, t in h0["totals"].items()},
            "fresh_nav_after": {c: t["nav_fresh"] for c, t in h1["totals"].items()},
            "valuation_status_after": {c: t["valuation_status"] for c, t in h1["totals"].items()},
            "conservative_values": conservative, "core_reaction": reaction, "order_gate": gate,
            "finding": "a stale quote fails QUOTE_FRESH for both sides, so risk-reducing exits are also refused until a fresh "
                       "quote arrives" if any(g["side"] == "SELL" and g["code"] == "QUOTE_FRESH" for g in gate) else None,
            "fixture_data": h1["fixture_data"], "fixture_note": h1["fixture_note"],
            "lineage": lineage(["port.mark", "port.nav", "stress.revalue", "whatif.headroom"],
                               inputs={"core_seq": book.core_seq, "quotes": sorted(qs)}, units="currency",
                               source_dates={"as_of_ns": now_ns, "evaluated_at_ns": later})}


def fee_revision(conn: sqlite3.Connection, account_key: str, quotes, *, now_ns: int, factor="2", scope: Scope = ACCOUNT,
                 deployment_id: str | None = None) -> dict:
    """Final fees arrive at ``factor`` times the policy's estimate model (per-share rate, minimum and cap scaled)."""
    qs = normalize_quotes(quotes)
    book = load_book(conn, account_key)
    if book.policy is None:
        raise ValueError("no active policy: the fee model is unknown")
    f = parse_fraction(factor, "factor")
    if f <= 0:
        raise ValueError("factor must be positive")
    fm = book.policy.fees
    ffix = fixed_of_fraction(f, SHOCK_SCALE)
    cap_bps = fm.maximum_bps_of_notional * f
    revised = dataclasses.replace(fm, per_share=fm.per_share * ffix,
                                  minimum=Money(fm.minimum.currency, fm.minimum.amount * ffix),
                                  maximum_bps_of_notional=-(-cap_bps.numerator // cap_bps.denominator))
    h = holdings(conn, account_key, qs, now_ns=now_ns, scope=scope, book=book)
    pending = []
    for r in conn.execute("SELECT row FROM executions WHERE account_key=?", (account_key,)):
        ex = json.loads(r["row"])
        if ex.get("fee_status") != "ESTIMATED" or ex.get("status", "ACTIVE") != "ACTIVE":
            continue
        if not scope.includes(ex.get("owner") or ""):
            continue
        ccy = ex["currency"]
        notional = Money(ccy, Fixed.parse(ex["notional"]))
        new_fee = fee_model.estimate(revised, Fixed.parse(ex["quantity"]), notional)
        old = Fixed.parse(ex["fee_estimate"])
        pending.append({"exec_key": ex["exec_key"], "owner": ex.get("owner"), "currency": ccy, "estimate": old.to_str(),
                        "revised_final": new_fee.amount.to_str(), "additional_cost": (new_fee.amount - old).to_str()})
    exits = []
    for p in h["positions"]:
        if p["market_value"] is None or Fixed.parse(p["quantity"]).is_negative():
            continue
        ccy = p["currency"]
        mv = Money(ccy, Fixed.parse(p["market_value"]))
        qty = Fixed.parse(p["quantity"])
        a, b = fee_model.estimate(fm, qty, mv).amount, fee_model.estimate(revised, qty, mv).amount
        exits.append({"owner": p["owner"], "instrument_id": p["instrument_id"], "exit_fee_current_model": a.to_str(),
                      "exit_fee_revised": b.to_str(), "difference": (b - a).to_str()})
    envelopes = []
    for o in book.open_orders:
        owner = order_owner(o) if (o.get("owner") or o.get("session_id")) else None   # DISPOSAL: its fixed owner
        if o["side"] != "BUY" or owner is None or not scope.includes(owner):
            continue
        ccy = o["currency"]
        scale = currency(ccy).ledger_scale
        leaves, limit = Fixed.parse(o["leaves_qty"]), Fixed.parse(o["limit_price"])
        notional = (leaves * limit * book.multiplier(o["instrument_id"])).rescale(scale, Rounding.CEILING)
        need = fee_model.estimate(revised, leaves, Money(ccy, notional)).amount
        held = Fixed.parse(o["fee_envelope"]) - Fixed.parse(o["fees_estimated"])
        envelopes.append({"order_id": o["order_id"], "fee_envelope_remaining": held.to_str(), "revised_fee": need.to_str(),
                          "under_reserved_by": (need - held).to_str() if need > held else None})
    reaction = None
    if pending:
        with Sandbox(conn, now_ns=now_ns, quotes=qs, deployment_id=deployment_id, account_key=account_key) as sb:
            seq = sb.seq
            for pnd in pending:
                sb.execute("broker_commission", {"account_key": account_key, "exec_key": pnd["exec_key"],
                                                 "amount": pnd["revised_final"], "currency": pnd["currency"]})
            from ..store.db import Tx
            tx = Tx(sb.mem)
            owners = sorted({(p["owner"], p["currency"]) for p in pending if p["owner"]})
            reaction = {"events": [{"type": e["type"], "info": e["info"]} for e in sb.events_since(seq)],
                        "available_cash_after": [{"owner": o, "currency": c,
                                                  "available": ledger.available_cash(tx, account_key, o, c).to_str()}
                                                 for o, c in owners],
                        "open_exceptions_after": [json.loads(r["row"])["kind"] for r in sb.mem.execute(
                            "SELECT row FROM exceptions WHERE account_key=? AND status='OPEN'", (account_key,))]}
    totals: dict[str, Fixed] = {}
    for p in pending:
        totals[p["currency"]] = totals.get(p["currency"], Fixed(0)) + Fixed.parse(p["additional_cost"])
    return {"kind": "stress", "scenario": "fee_revision", "label": HYPOTHETICAL, "account_key": account_key,
            "scope": scope.label, "as_of_ns": now_ns, "core_seq": book.core_seq,
            "parameters": {"factor": str(factor),
                           "model_now": {"per_share": fm.per_share.to_str(), "minimum": fm.minimum.amount.to_str(),
                                         "maximum_bps_of_notional": fm.maximum_bps_of_notional},
                           "model_revised": {"per_share": revised.per_share.to_str(), "minimum": revised.minimum.amount.to_str(),
                                             "maximum_bps_of_notional": revised.maximum_bps_of_notional,
                                             "note": "per-share rate, minimum and percentage cap all scaled by the factor "
                                                     "(cap rounded up)"}},
            "pending_estimates": pending, "additional_cost_by_currency": {c: v.to_str() for c, v in totals.items()},
            "exit_costs": exits, "open_buy_envelopes": envelopes, "core_reaction": reaction,
            "note": "the observation path always books the broker's final fee; an unexpected amount reduces available cash "
                    "and may open an exception, it is never discarded (ADR-0006)",
            "fixture_data": h["fixture_data"], "fixture_note": h["fixture_note"],
            "lineage": lineage(["stress.fee_revision"], inputs={"core_seq": book.core_seq, "fee_source": fm.source,
                                                                 "fee_verified_on": fm.verified_on},
                               units="currency", source_dates={"as_of_ns": now_ns})}


SCENARIOS = {"overnight_gap": overnight_gap, "correlated_selloff": correlated_selloff, "stale_quote": stale_quote,
             "fee_revision": fee_revision}
