"""Holdings, cash ladder and NAV by owner (policy bucket) and currency (CAP-PORT-01; ADR-0006; book Ch. 9, 10).

Read model over the core database opened READ-ONLY. The journal is authoritative:
balances come from the ``balances`` projection (sum of postings), positions from open
lots, reservations from active holds. Policy buckets are a dimension of real balances,
so summing every owner gives the account and nothing is counted twice: allocating,
protecting or reserving cash never changes NAV.

Marks come from a supplied quote snapshot (``marks.mark_for``). A position without a
usable quote is *unpriced*: it is listed with its cost basis and is never valued at
zero; any NAV that would need it is null with the reason. A position with a stale mark
is valued at its last mark in ``nav_marked`` (flagged) and excluded from ``nav_fresh``.

Every monetary figure is an exact decimal string with its currency; every derived
figure names its formula ``id@version`` in the lineage block."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from ..core import ledger
from ..domain.fixed import Fixed, Rounding
from ..domain.instruments import Instrument
from ..domain.money import currency
from ..policy.model import Policy
from .common import (ACCOUNT, Scope, latest_seq, lineage, owner_kind, read_snapshot, weight)
from .marks import Mark, mark_for, normalize_quotes

SUSPENSE = ledger.SUSPENSE
_TRACKED = (ledger.CASH, ledger.RECEIVABLE, ledger.PAYABLE, ledger.FEES_ACCRUED, ledger.INVENTORY, SUSPENSE)


@dataclass
class Book:
    """Everything the analytics need from one consistent read of the core projections."""
    account_key: str
    account: dict
    base_currency: str
    policy: Policy | None
    risk: dict | None
    control: dict | None
    core_seq: int
    instruments: dict[str, Instrument]
    balances: dict[tuple[str, str], dict[str, Fixed]]
    lots: list[dict]
    cash_holds: dict[tuple[str, str], Fixed]
    qty_holds: dict[tuple[str, str], Fixed]
    settlement_status: dict[str, str]
    seeds: dict[str, tuple[str, Fixed]]
    sessions: dict[str, dict]
    open_orders: list[dict]
    owners: list[str] = field(default_factory=list)
    mandates: dict[str, dict] = field(default_factory=dict)

    def multiplier(self, instrument_id: str) -> Fixed:
        inst = self.instruments.get(instrument_id)
        return inst.multiplier if inst is not None else Fixed(1)


def load_book(conn: sqlite3.Connection, account_key: str) -> Book:
    """Read the projections needed for valuation inside one read transaction."""
    from ..store.db import Tx
    from ..core import state
    from ..domain.errors import Rejected
    with read_snapshot(conn):
        r = conn.execute("SELECT row FROM accounts WHERE account_key=?", (account_key,)).fetchone()
        if r is None:
            raise LookupError(f"unknown account {account_key}")
        account = json.loads(r["row"])
        try:
            policy = state.active_policy(Tx(conn), account_key)
        except Rejected:
            policy = None
        rr = conn.execute("SELECT row FROM risk_state WHERE account_key=?", (account_key,)).fetchone()
        cr = conn.execute("SELECT row FROM control WHERE account_key=?", (account_key,)).fetchone()
        seq = latest_seq(conn)
        instruments = {x["instrument_id"]: Instrument.from_json(json.loads(x["body"]))
                       for x in conn.execute("SELECT instrument_id, body FROM instruments")}
        balances: dict[tuple[str, str], dict[str, Fixed]] = defaultdict(dict)
        for b in conn.execute("SELECT account_code, owner, currency, amount FROM balances WHERE account_key=?", (account_key,)):
            if b["account_code"] not in _TRACKED:
                continue
            slot = balances[(b["owner"], b["currency"])]
            slot[b["account_code"]] = slot.get(b["account_code"], Fixed(0)) + Fixed.parse(b["amount"])
        lots = [json.loads(x["row"]) for x in conn.execute("SELECT row FROM lots WHERE account_key=? ORDER BY lot_id", (account_key,))]
        cash_holds: dict[tuple[str, str], Fixed] = defaultdict(lambda: Fixed(0))
        qty_holds: dict[tuple[str, str], Fixed] = defaultdict(lambda: Fixed(0))
        for h in conn.execute("SELECT owner, kind, unit, amount FROM holds WHERE account_key=? AND status='ACTIVE'", (account_key,)):
            if h["kind"] == "CASH":
                cash_holds[(h["owner"], h["unit"])] += Fixed.parse(h["amount"])
            else:
                qty_holds[(h["owner"], h["unit"])] += Fixed.parse(h["amount"])
        settlement_status = {x["settlement_id"]: x["status"] for x in
                             conn.execute("SELECT settlement_id, status FROM settlements WHERE account_key=?", (account_key,))}
        seeds, sessions, mandates = {}, {}, {}
        for s in conn.execute("SELECT s.session_id, s.row, m.body FROM sessions s LEFT JOIN mandates m ON m.mandate_hash = s.mandate_hash "
                              "WHERE s.account_key=?", (account_key,)):
            row = json.loads(s["row"])
            sessions[s["session_id"]] = row
            if s["body"]:
                body = json.loads(s["body"])
                mandates[ledger.session_owner(s["session_id"])] = body
                seed = body.get("seed")
                if isinstance(seed, dict):
                    seeds[ledger.session_owner(s["session_id"])] = (seed["currency"], Fixed.parse(seed["amount"]))
                elif isinstance(seed, str):
                    seeds[ledger.session_owner(s["session_id"])] = (body.get("currency", account["base_currency"]), Fixed.parse(seed))
        open_orders = [o for o in (json.loads(x["row"]) for x in conn.execute(
            "SELECT row FROM orders WHERE account_key=? ORDER BY accepted_at_ns, order_id", (account_key,)))
            if o["state"] in ("RESERVED", "SUBMIT_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING", "UNKNOWN")]
    owners = sorted({o for o, _ in balances} | {lot["owner"] for lot in lots if not Fixed.parse(lot["open_qty"]).is_zero()})
    return Book(account_key, account, account["base_currency"], policy, json.loads(rr["row"]) if rr else None,
                json.loads(cr["row"]) if cr else None, seq, instruments, dict(balances), lots, dict(cash_holds),
                dict(qty_holds), settlement_status, seeds, sessions, open_orders, owners, mandates)


# --------------------------------------------------------------------- valuation
@dataclass
class Position:
    owner: str
    instrument_id: str
    currency: str
    quantity: Fixed
    cost: Fixed
    settled_quantity: Fixed
    lots: int
    multiplier: Fixed
    mark: Mark
    market_value: Fixed | None
    settlement_unknown_quantity: Fixed = Fixed(0)

    @property
    def priced(self) -> bool:
        return self.market_value is not None


def positions(book: Book, marks: dict[str, Mark]) -> list[Position]:
    agg: dict[tuple[str, str], dict] = {}
    for lot in book.lots:
        qty = Fixed.parse(lot["open_qty"])
        if qty.is_zero():
            continue
        key = (lot["owner"], lot["instrument_id"])
        a = agg.setdefault(key, {"currency": lot["currency"], "qty": Fixed(0), "cost": Fixed(0), "settled": Fixed(0), "lots": 0,
                                 "unknown": Fixed(0)})
        a["qty"] += qty
        a["cost"] += Fixed.parse(lot["open_cost"])
        a["lots"] += 1
        opened_by = lot.get("opened_by") or ""
        status = "SETTLED" if opened_by == "opening-import" else book.settlement_status.get(f"stl:{opened_by}")
        if status == "SETTLED":
            a["settled"] += qty
        elif status is None:
            # No settlement record for the opening execution: reported as unknown and never counted as settled
            # (review fix: a missing record used to default to SETTLED).
            a["unknown"] += qty
    out = []
    for (owner, iid), a in sorted(agg.items()):
        m = marks[iid]
        mult = book.multiplier(iid)
        mv = None
        if m.price is not None:
            mv = (a["qty"] * m.price * mult).rescale(currency(a["currency"]).ledger_scale, Rounding.FLOOR)
        out.append(Position(owner, iid, a["currency"], a["qty"], a["cost"].rescale(currency(a["currency"]).ledger_scale),
                            a["settled"], a["lots"], mult, m, mv, a["unknown"]))
    return out


def book_marks(book: Book, quotes, *, now_ns: int, basis: str, max_age_ms: int) -> dict[str, Mark]:
    qs = normalize_quotes(quotes)
    ids = sorted({lot["instrument_id"] for lot in book.lots if not Fixed.parse(lot["open_qty"]).is_zero()})
    return {iid: mark_for(iid, qs.get(iid), basis=basis, max_age_ms=max_age_ms, now_ns=now_ns) for iid in ids}


def owner_currencies(book: Book, pos: list[Position]) -> list[tuple[str, str]]:
    keys = set(book.balances) | {(p.owner, p.currency) for p in pos}
    return sorted(keys)


def owner_summary(book: Book, owner: str, ccy: str, pos: list[Position]) -> dict:
    scale = currency(ccy).ledger_scale
    b = book.balances.get((owner, ccy), {})

    def g(code: str) -> Fixed:
        return b.get(code, Fixed(0)).rescale(scale)

    liquid = g(ledger.CASH) + g(ledger.RECEIVABLE) + g(ledger.PAYABLE) + g(ledger.FEES_ACCRUED)
    holds = book.cash_holds.get((owner, ccy), Fixed(0)).rescale(scale)
    available = g(ledger.CASH) + g(ledger.PAYABLE) + g(ledger.FEES_ACCRUED) - holds
    mine = [p for p in pos if p.owner == owner and p.currency == ccy]
    mv_fresh = sum((p.market_value for p in mine if p.mark.status == "fresh"), Fixed(0, scale))
    mv_stale = sum((p.market_value for p in mine if p.mark.status == "stale"), Fixed(0, scale))
    unpriced = [p for p in mine if not p.priced]
    stale = [p for p in mine if p.mark.status == "stale"]
    status = "unpriced_positions" if unpriced else ("stale_marks" if stale else "complete")
    out = {
        "owner": owner, "kind": owner_kind(owner), "system_owned": ledger.is_system_owner(owner), "currency": ccy,
        "cash": {"settled_cash": g(ledger.CASH).to_str(), "receivable_unsettled": g(ledger.RECEIVABLE).to_str(),
                 "payable_unsettled": g(ledger.PAYABLE).to_str(), "fees_accrued": g(ledger.FEES_ACCRUED).to_str(),
                 "cash_holds": holds.to_str(), "available_cash": available.to_str(), "liquid_value": liquid.to_str()},
        "suspense": g(SUSPENSE).to_str(),
        "inventory_cost": g(ledger.INVENTORY).to_str(),
        "market_value_fresh": mv_fresh.to_str(),
        "market_value_stale_last_mark": mv_stale.to_str(),
        "unpriced_positions": [{"instrument_id": p.instrument_id, "quantity": p.quantity.to_str(), "cost_basis": p.cost.to_str(),
                                "reason": p.mark.reason} for p in unpriced],
        "stale_instruments": sorted(p.instrument_id for p in stale),
        "nav_marked": None if unpriced else (liquid + mv_fresh + mv_stale).to_str(),
        "nav_fresh": (liquid + mv_fresh).to_str() if status == "complete" else None,
        "valuation_status": status,
    }
    if owner in book.seeds and book.seeds[owner][0] == ccy:
        out["session_seed"] = book.seeds[owner][1].rescale(scale).to_str()
    return out


def _sum_str(values: list[str | None], scale: int) -> str | None:
    if any(v is None for v in values):
        return None
    return sum((Fixed.parse(v) for v in values), Fixed(0, scale)).to_str()


def scope_totals(summaries: list[dict], scope: Scope) -> dict[str, dict]:
    """Per-currency totals over the owners in ``scope`` (buckets partition, so these are plain sums)."""
    per: dict[str, list[dict]] = defaultdict(list)
    for s in summaries:
        if scope.includes(s["owner"]):
            per[s["currency"]].append(s)
    out = {}
    for ccy, rows in sorted(per.items()):
        scale = currency(ccy).ledger_scale
        liquid = sum((Fixed.parse(r["cash"]["liquid_value"]) for r in rows), Fixed(0, scale))
        mv_f = sum((Fixed.parse(r["market_value_fresh"]) for r in rows), Fixed(0, scale))
        mv_s = sum((Fixed.parse(r["market_value_stale_last_mark"]) for r in rows), Fixed(0, scale))
        statuses = {r["valuation_status"] for r in rows}
        status = "unpriced_positions" if "unpriced_positions" in statuses else ("stale_marks" if "stale_marks" in statuses else "complete")
        out[ccy] = {"currency": ccy, "owners": len(rows), "liquid_value": liquid.to_str(),
                    "market_value_fresh": mv_f.to_str(), "market_value_stale_last_mark": mv_s.to_str(),
                    "inventory_cost": sum((Fixed.parse(r["inventory_cost"]) for r in rows), Fixed(0, scale)).to_str(),
                    "available_cash": sum((Fixed.parse(r["cash"]["available_cash"]) for r in rows), Fixed(0, scale)).to_str(),
                    "nav_marked": _sum_str([r["nav_marked"] for r in rows], scale),
                    "nav_fresh": _sum_str([r["nav_fresh"] for r in rows], scale),
                    "valuation_status": status,
                    "stale_instruments": sorted({i for r in rows for i in r["stale_instruments"]}),
                    "unpriced_instruments": sorted({u["instrument_id"] for r in rows for u in r["unpriced_positions"]})}
    return out


def base_currency_total(totals: dict[str, dict], base: str, fx: dict | None) -> dict:
    """Optional reporting conversion; never creates spendable cash (LED-12). ``fx[c] = {rate, source, as_of_ns}``
    gives base units per one unit of c. Missing rates make the total unavailable, never partial."""
    missing = [c for c in totals if c != base and not (fx and c in fx)]
    if missing:
        return {"currency": base, "nav_marked": None, "nav_fresh": None,
                "reason": f"no FX rate supplied for {', '.join(sorted(missing))}"}
    scale = currency(base).ledger_scale
    out = {"currency": base, "rates": {c: fx[c] for c in totals if c != base} if fx else {},
           "note": "reporting conversion only; ECB reference rates are informational and never size transactions"}
    for k in ("nav_marked", "nav_fresh"):
        total = Fixed(0, scale)
        for c, t in totals.items():
            if t[k] is None:
                total = None
                break
            v = Fixed.parse(t[k])
            if c != base:
                v = (v * Fixed.parse(str(fx[c]["rate"]))).rescale(scale, Rounding.HALF_EVEN)
            total = total + v
        out[k] = total.to_str() if total is not None else None
    return out


def account_cash_ladder(summaries: list[dict]) -> list[dict]:
    """Per-currency cash ladder across buckets (CAP-PORT-01 step 4). Buckets are disjoint claims on the same cash."""
    per: dict[str, dict] = {}
    for s in summaries:
        ccy = s["currency"]
        scale = currency(ccy).ledger_scale
        z = Fixed(0, scale)
        d = per.setdefault(ccy, {"currency": ccy, "ledger_settled_cash": z, "receivable_unsettled": z, "payable_unsettled": z,
                                 "fees_accrued": z, "cash_holds": z, "by_bucket": defaultdict(lambda: Fixed(0, scale))})
        c = s["cash"]
        d["ledger_settled_cash"] += Fixed.parse(c["settled_cash"])
        d["receivable_unsettled"] += Fixed.parse(c["receivable_unsettled"])
        d["payable_unsettled"] += Fixed.parse(c["payable_unsettled"])
        d["fees_accrued"] += Fixed.parse(c["fees_accrued"])
        d["cash_holds"] += Fixed.parse(c["cash_holds"])
        d["by_bucket"][s["kind"]] += Fixed.parse(c["liquid_value"])
        if s["owner"] == ledger.UNALLOCATED:
            d["unallocated_available"] = c["available_cash"]
    out = []
    for ccy, d in sorted(per.items()):
        out.append({"currency": ccy, "ledger_settled_cash": d["ledger_settled_cash"].to_str(),
                    "receivable_unsettled": d["receivable_unsettled"].to_str(),
                    "payable_unsettled": d["payable_unsettled"].to_str(), "fees_accrued": d["fees_accrued"].to_str(),
                    "reserved_cash_holds": d["cash_holds"].to_str(),
                    "liquid_by_bucket": {k: v.to_str() for k, v in sorted(d["by_bucket"].items())},
                    "unallocated_available": d.get("unallocated_available"),
                    "broker_settled_cash": None,
                    "broker_settled_cash_reason": "broker cash is evidence held by reconciliation reports, not by the "
                                                  "valuation projections; eq. 9.1 min(C_broker, C_ledger) is applied by the core"})
    return out


def holdings(conn: sqlite3.Connection, account_key: str, quotes=None, *, now_ns: int, scope: Scope = ACCOUNT,
             mark_basis: str = "bid", max_age_ms: int | None = None, fx: dict | None = None,
             book: Book | None = None) -> dict:
    """CAP-PORT-01 read model: positions, owner summaries, scope totals, cash ladder, weights, lineage."""
    book = book or load_book(conn, account_key)
    if max_age_ms is None:
        if book.policy is None:
            raise ValueError("no active policy: supply max_age_ms (freshness budget) explicitly")
        max_age_ms = book.policy.limits.quote_max_age_ms
    marks = book_marks(book, quotes, now_ns=now_ns, basis=mark_basis, max_age_ms=max_age_ms)
    pos = positions(book, marks)
    summaries = [owner_summary(book, o, c, pos) for o, c in owner_currencies(book, pos)]
    totals = scope_totals(summaries, scope)
    account_totals = scope_totals(summaries, ACCOUNT)
    authorized = Fixed.parse(book.risk["authorized_total"]) if book.risk else None
    by_owner = {(s["owner"], s["currency"]): s for s in summaries}
    rows = []
    for p in pos:
        if not scope.includes(p.owner):
            continue
        scale = currency(p.currency).ledger_scale
        denominators = {
            "scope_nav": totals.get(p.currency, {}).get("nav_marked"),
            "owner_nav": by_owner[(p.owner, p.currency)]["nav_marked"],
            "account_nav": account_totals.get(p.currency, {}).get("nav_marked"),
            "authorized_capital": authorized.to_str() if (authorized is not None and p.currency == book.base_currency
                                                          and ledger.is_system_owner(p.owner)) else None,
            "session_seed": by_owner[(p.owner, p.currency)].get("session_seed"),
        }
        weights = {k: (weight(p.market_value, Fixed.parse(v)) if (p.market_value is not None and v is not None) else None)
                   for k, v in denominators.items()}
        held = book.qty_holds.get((p.owner, p.instrument_id), Fixed(0))
        inst = book.instruments.get(p.instrument_id)
        rows.append({
            "owner": p.owner, "bucket_kind": owner_kind(p.owner), "system_owned": ledger.is_system_owner(p.owner),
            "instrument_id": p.instrument_id, "symbol": inst.symbol if inst else None, "venue": inst.venue_mic if inst else None,
            "currency": p.currency, "quantity": p.quantity.to_str(), "settled_quantity": p.settled_quantity.to_str(),
            "unsettled_quantity": (p.quantity - p.settled_quantity - p.settlement_unknown_quantity).to_str(),
            "settlement_unknown_quantity": p.settlement_unknown_quantity.to_str(), "reserved_quantity": held.to_str(),
            "cost_basis": p.cost.to_str(),
            "average_cost": p.cost.div(p.quantity * p.multiplier, max(scale, 4) + 2, Rounding.HALF_EVEN).to_str()
            if not p.quantity.is_zero() else None,
            "lots": p.lots, "multiplier": p.multiplier.to_str(), "mark": p.mark.to_json(),
            "market_value": p.market_value.to_str() if p.market_value is not None else None,
            "unrealized": (p.market_value - p.cost).to_str() if p.market_value is not None else None,
            "valuation_note": None if p.priced else "unpriced: never valued at zero; excluded from every NAV figure",
            "weights": weights, "weight_denominators": denominators,
            "fixture": bool(p.mark.fixture or (inst and inst.attributes.get("fixture"))),
            "instrument_fixture": bool(inst and inst.attributes.get("fixture")),
            "sec_cik": inst.attributes.get("sec.cik") if inst else None,
        })
    fixture = any(r["fixture"] for r in rows)
    return {
        "kind": "holdings", "account_key": account_key, "scope": scope.label, "as_of_ns": now_ns,
        "core_seq": book.core_seq, "base_currency": book.base_currency,
        "mark_policy": {"basis": mark_basis, "max_age_ms": max_age_ms,
                        "source": "supplied quote snapshot (core quote book)"},
        "positions": rows,
        "owners": [s for s in summaries if scope.includes(s["owner"])],
        "totals": totals,
        "base_currency_total": base_currency_total(totals, book.base_currency, fx),
        "cash_ladder": account_cash_ladder(summaries),
        "authorized_total": authorized.to_str() if authorized is not None else None,
        "fixture_data": fixture,
        "fixture_note": "contains FIXTURE quotes or instruments: test data, not market data" if fixture else None,
        "lineage": lineage(["port.mark", "port.market_value", "port.nav", "port.available_cash", "port.weight"],
                           inputs={"core_seq": book.core_seq, "quotes": {i: m.to_json() for i, m in marks.items()}},
                           units="currency amounts as decimal strings; weights dimensionless",
                           source_dates={"valuation_ns": now_ns},
                           missing=[{"instrument_id": i, "reason": m.reason} for i, m in marks.items() if m.status != "fresh"]),
    }
