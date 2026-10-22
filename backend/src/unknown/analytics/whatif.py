"""What-if sandbox for hypothetical intents and scenarios (CAP-PORT-12; book Ch. 9, 13; ADR-0006, ADR-0009).

``Sandbox`` copies the core database from a READ-ONLY connection into a private in-memory
database with SQLite's online backup and runs the core's own command path on the copy
(``Desk.execute``: authorization -> handler -> ledger verification). A dry-run intent
therefore goes through ``submit_intent`` -> ``create_order`` -> ``risk.pretrade.evaluate`` ->
reservation exactly as a live intent would: the decision, every check and the reservation
come from the code the live risk gate runs, so the what-if cannot drift from the gate.

It never writes to the core. The source is opened with ``mode=ro``; every write lands in
the private copy, which is discarded. The copy keeps the source's writer epoch and lease
holder, so the recovery-gate and fencing checks see what the live core would see. The
deployment evaluated as is the current execution owner unless one is named.

Results are labelled hypothetical, carry the core event sequence they were computed on,
and become stale when that sequence advances (``is_stale``). A what-if cannot authorize an
order: a real order is a new intent through the core and passes the gate again (book R08).
"""
from __future__ import annotations

import json
import sqlite3

from ..core import ledger
from ..domain.clock import ManualClock
from ..domain.fixed import Fixed, Rounding
from ..domain.ids import IdGenerator
from ..domain.money import Money, currency
from ..domain.principal import Principal, Role
from ..market.quotes import QuoteBook
from ..risk import fees as fee_model
from .common import frac, latest_seq, lineage, weight
from .holdings import book_marks, load_book, owner_summary, positions
from .marks import normalize_quotes

HYPOTHETICAL = "HYPOTHETICAL what-if: computed on a private copy; it reserves nothing, places nothing and authorizes nothing"


class SandboxFault(RuntimeError):
    """The core raised a fault (not a domain rejection) inside the sandbox; reported, never masked."""


class Sandbox:
    """A private, disposable copy of the core on which core commands can be executed."""

    def __init__(self, conn: sqlite3.Connection, *, now_ns: int, quotes=None, deployment_id: str | None = None,
                 account_key: str | None = None):
        from ..core.desk import Desk
        from ..store.db import Store
        self.mem = sqlite3.connect(":memory:", isolation_level=None)
        conn.backup(self.mem)
        self.mem.row_factory = sqlite3.Row
        # The sequence of the state actually copied (read from the copy, not from the source before the backup, which
        # could be older than the copy if the live core committed in between).
        self.source_seq = latest_seq(self.mem)
        self.mem.execute("PRAGMA foreign_keys=ON")
        lease = self.mem.execute("SELECT holder, epoch FROM writer_lease WHERE id=1").fetchone()
        self.clock = ManualClock(now_ns)
        store = object.__new__(Store)  # store facade over the private copy: same lease holder and epoch as the source
        store.path, store.clock, store.host, store.conn = ":memory:", self.clock, "whatif-sandbox", self.mem
        store.holder = lease["holder"] if lease else "whatif-sandbox"
        store.epoch = lease["epoch"] if lease else 0
        if deployment_id is None:
            # The execution owner of THIS account (review fix: the first account's row was taken whatever account
            # was being evaluated).
            r = (self.mem.execute("SELECT row FROM execution_ownership WHERE account_key=?", (account_key,)).fetchone()
                 if account_key is not None else
                 self.mem.execute("SELECT row FROM execution_ownership ORDER BY account_key LIMIT 1").fetchone())
            own = json.loads(r["row"]) if r else None
            deployment_id = own["deployment_id"] if own and own.get("status") == "HELD" else "whatif-sandbox"
        self.quotes = QuoteBook()
        self.desk = Desk(store, self.clock, IdGenerator(lambda: self.clock.now_ns() // 1_000_000), deployment_id=deployment_id,
                         quotes=self.quotes)
        self.put_quotes(quotes)
        self.n = 0

    # ------------------------------------------------------------ control
    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.mem.close()

    def put_quotes(self, quotes) -> None:
        for q in normalize_quotes(quotes).values():
            if q.receipt_time_ns <= self.clock.now_ns():  # point-in-time guard
                self.quotes.put(q)

    def set_time(self, now_ns: int) -> None:
        self.clock.set(now_ns)

    @property
    def seq(self) -> int:
        return latest_seq(self.mem)

    # ------------------------------------------------------------ commands
    def execute(self, kind: str, payload: dict, principal: Principal | None = None):
        from ..core.desk import Command
        self.n += 1
        principal = principal or self.desk.system()
        try:
            return self.desk.execute(Command(f"whatif.{kind}.{self.n}", kind, principal, payload))
        except Exception as exc:
            raise SandboxFault(f"{kind}: {type(exc).__name__}: {exc}") from exc

    def events_since(self, seq: int) -> list[dict]:
        out = []
        for r in self.mem.execute("SELECT seq, type, session_id, order_id, payload FROM events WHERE seq>? ORDER BY seq", (seq,)):
            info = json.loads(r["payload"])["info"]
            out.append({"seq": r["seq"], "type": r["type"], "session_id": r["session_id"], "order_id": r["order_id"],
                        "info": info})
        return out

    def row(self, table: str, key_col: str, key: str) -> dict | None:
        r = self.mem.execute(f"SELECT row FROM {table} WHERE {key_col}=?", (key,)).fetchone()
        return json.loads(r["row"]) if r else None

    def intent(self, account_key: str, intent: dict, *, principal_id: str = "owner", principal_role: str = "OWNER") -> dict:
        """Run ``submit_intent`` on the copy; return the decision record and the reservation preview."""
        now = self.clock.now_ns()
        session_id = intent.get("session_id")
        payload = {"account_key": account_key, "session_id": session_id,
                   "order_id": intent.get("order_id") or f"whatif-intent-{self.n + 1}",
                   "instrument_id": intent.get("instrument_id"), "side": intent.get("side"),
                   "quantity": intent.get("quantity"), "limit_price": intent.get("limit_price"),
                   "order_type": intent.get("order_type", "LIMIT"), "time_in_force": intent.get("time_in_force", "DAY"),
                   "deadline_ns": intent.get("deadline_ns", now + 60 * 1_000_000_000)}
        srow = self.mem.execute("SELECT m.body FROM sessions s JOIN mandates m ON m.mandate_hash=s.mandate_hash "
                                "WHERE s.session_id=?", (session_id,)).fetchone() if session_id else None
        mandate = json.loads(srow["body"]) if srow else None
        payload["policy_hash"] = intent.get("policy_hash") or (mandate["policy_hash"] if mandate else None)
        for k in ("strategy_id", "purpose", "decision_ref"):
            if intent.get(k) is not None:
                payload[k] = intent[k]
        principal = Principal(principal_id, Role[principal_role], authenticated_at_ns=now)
        result = self.execute("submit_intent", payload, principal)
        if result.accepted:
            ev = self.mem.execute("SELECT payload FROM events WHERE type='OrderAccepted' AND order_id=? ORDER BY seq DESC LIMIT 1",
                                  (payload["order_id"],)).fetchone()
            info = json.loads(ev["payload"])["info"]
            decision, quote_used, required = info["decision"], info.get("quote"), info.get("required_cash")
            holds = [dict(h) for h in self.mem.execute("SELECT kind, unit, amount, status FROM holds WHERE order_id=?",
                                                       (payload["order_id"],))]
        else:
            details = result.body.get("details", {})
            decision, quote_used, required, holds = details.get("decision"), details.get("quote"), None, []
        return {"intent": payload, "mandate": mandate, "approved": bool(result.accepted),
                "code": "APPROVED" if result.accepted else result.body.get("code"),
                "message": None if result.accepted else result.body.get("message"),
                "decision": decision, "quote_used": quote_used, "required_cash": required, "reservation_preview": holds}


# ------------------------------------------------------------------ dry run
def _owner_view(mem: sqlite3.Connection, account_key: str, owner: str, ccy: str, quotes, now_ns: int, max_age_ms: int,
                instrument_id: str) -> dict:
    from ..store.db import Tx
    book = load_book(mem, account_key)
    marks = book_marks(book, quotes, now_ns=now_ns, basis="bid", max_age_ms=max_age_ms)
    pos = positions(book, marks)
    summary = owner_summary(book, owner, ccy, pos)
    tx = Tx(mem)
    mine = [p for p in pos if p.owner == owner and p.currency == ccy]
    return {"available_cash": ledger.available_cash(tx, account_key, owner, ccy),
            "owned_quantity": ledger.owned_quantity(tx, account_key, owner, instrument_id),
            "reserved_quantity": ledger.active_quantity_holds(tx, account_key, owner, instrument_id),
            "nav_marked": summary["nav_marked"], "positions": mine,
            "target": next((p for p in mine if p.instrument_id == instrument_id), None), "policy": book.policy,
            "multiplier": book.multiplier(instrument_id)}


def _hhi(values: list[Fixed]) -> float | None:
    gross = sum((abs(v) for v in values), Fixed(0))
    if not gross.is_positive():
        return None
    return float(sum((frac(abs(v)) / frac(gross)) ** 2 for v in values))


def dry_run(conn: sqlite3.Connection, account_key: str, intent: dict, quotes=None, *, now_ns: int,
            deployment_id: str | None = None, principal_id: str = "owner", principal_role: str = "OWNER") -> dict:
    """Evaluate a hypothetical intent exactly as the core would, without writing to the core (CAP-PORT-12)."""
    qs = normalize_quotes(quotes)
    with Sandbox(conn, now_ns=now_ns, quotes=qs, deployment_id=deployment_id, account_key=account_key) as sb:
        session_id = intent.get("session_id")
        instrument_id = intent.get("instrument_id")
        srow = sb.mem.execute("SELECT m.body FROM sessions s JOIN mandates m ON m.mandate_hash=s.mandate_hash "
                              "WHERE s.session_id=?", (session_id,)).fetchone() if session_id else None
        ccy = json.loads(srow["body"])["currency"] if srow else None
        owner = ledger.session_owner(session_id) if session_id else None
        before = after = None
        impact_unavailable = None
        book0 = load_book(sb.mem, account_key) if (owner and ccy and instrument_id) else None
        if book0 is not None and book0.policy is None:
            # No silent freshness default: without a policy there is no quote budget (and the core refuses the intent).
            impact_unavailable = "no active policy: no quote freshness budget to mark positions; impact not computed"
        elif book0 is not None:
            max_age = book0.policy.limits.quote_max_age_ms
            before = _owner_view(sb.mem, account_key, owner, ccy, qs, now_ns, max_age, instrument_id)
        elif not (owner and ccy):
            impact_unavailable = "unknown session or mandate: impact not computed"
        res = sb.intent(account_key, intent, principal_id=principal_id, principal_role=principal_role)
        if before is not None:
            after = _owner_view(sb.mem, account_key, owner, ccy, qs, now_ns, max_age, instrument_id)
        evaluated_as = sb.desk.deployment_id
        snapshot_seq = sb.source_seq
    out = {"kind": "whatif", "label": HYPOTHETICAL, "account_key": account_key, "as_of_ns": now_ns,
           "snapshot_seq": snapshot_seq, "principal": {"id": principal_id, "role": principal_role},
           "evaluated_as_deployment": evaluated_as, **{k: v for k, v in res.items() if k != "mandate"}}
    if before is not None:
        out["impact"] = _impact(res["intent"], before, after, res["required_cash"], ccy, qs)
    else:
        out["impact"] = {"reason": impact_unavailable or "impact not computed"}
    out["lineage"] = lineage(["whatif.cash", "whatif.headroom", "port.available_cash", "port.nav", "port.weight"],
                             inputs={"core_seq": snapshot_seq, "quotes": sorted(qs)}, units="currency amounts as decimal strings",
                             source_dates={"as_of_ns": now_ns},
                             notes=["decision and reservation come from the core command path run on a private copy",
                                    "post-trade figures assume a complete fill at the limit price"])
    return out


def _impact(payload: dict, before: dict, after: dict, required: str | None, ccy: str, qs: dict) -> dict:
    scale = currency(ccy).ledger_scale
    policy, mult = before["policy"], before["multiplier"]
    try:
        qty, limit = Fixed.parse(str(payload["quantity"])), Fixed.parse(str(payload["limit_price"]))
    except ValueError:
        return {"reason": "quantity or limit price is not a decimal string"}
    buy = payload["side"] == "BUY"
    valid = qty.is_positive() and limit.is_positive()
    notional = (qty * limit * mult).rescale(scale, Rounding.CEILING) if valid else Fixed(0, scale)
    envelope = fee_model.envelope(policy.fees, qty, Money(ccy, notional)).amount if (policy and buy and valid) else Fixed(0, scale)
    est_fee = fee_model.estimate(policy.fees, qty, Money(ccy, notional)).amount if (policy and valid) else Fixed(0, scale)
    need = Fixed.parse(required) if required else (notional + envelope if buy else Fixed(0, scale))
    avail = before["available_cash"]
    cash = {"available_before": avail.to_str(), "required_reservation": need.to_str() if buy else "0",
            "available_after_reservation": (avail - need).to_str() if buy else avail.to_str(),
            "shortfall": (need - avail).to_str() if buy and need > avail else None,
            "available_after_in_sandbox": after["available_cash"].to_str(),
            "note": "unsettled sale proceeds add nothing to available cash (ch09)"}
    owned, reserved = before["owned_quantity"], before["reserved_quantity"]
    quantity = {"owned_before": owned.to_str(), "reserved_before": reserved.to_str(),
                "unreserved_before": (owned - reserved).to_str(),
                "reserved_after_in_sandbox": after["reserved_quantity"].to_str(),
                "excess_over_unreserved": (qty - (owned - reserved)).to_str() if not buy and qty > owned - reserved else None}
    q = qs.get(payload["instrument_id"])
    mark = q.bid if q is not None and q.bid is not None else None
    filled = {"assumption": "complete fill at the limit price; marked at the current bid",
              "note": "for a rejected intent these figures only explain the rejection; that order cannot exist"}
    nav = Fixed.parse(before["nav_marked"]) if before["nav_marked"] is not None else None
    if mark is not None and nav is not None and valid:
        signed = qty if buy else -qty
        mv_before = before["target"].market_value if (before["target"] is not None and before["target"].market_value is not None) \
            else Fixed(0, scale)
        mv_after = ((owned + signed) * mark * mult).rescale(scale, Rounding.FLOOR)
        nav_after = nav - est_fee + ((-qty if buy else qty) * (limit - mark) * mult).rescale(scale, Rounding.FLOOR)
        others = [p.market_value for p in before["positions"]
                  if p.instrument_id != payload["instrument_id"] and p.market_value is not None]
        filled.update(position_value_before=mv_before.to_str(), position_value_after=mv_after.to_str(),
                      owner_nav_before=nav.to_str(), owner_nav_after=nav_after.to_str(),
                      weight_before=weight(mv_before, nav), weight_after=weight(mv_after, nav_after),
                      hhi_before=_hhi(others + [mv_before]), hhi_after=_hhi(others + [mv_after]),
                      estimated_fee=est_fee.to_str())
    else:
        filled["reason"] = "no bid mark, no owner NAV or an invalid order: concentration impact not computed"
    headroom = {}
    if policy is not None:
        headroom["max_order_notional"] = (policy.limits.max_order_notional.amount - notional).to_str() if buy else None
        headroom["cash"] = (avail - need).to_str() if buy else None
        headroom["owned_quantity"] = ((owned - reserved) - qty).to_str() if not buy else None
    return {"order_notional_at_limit": notional.to_str(), "cash": cash, "quantity": quantity, "if_filled": filled,
            "headroom": headroom, "currency": ccy}


def is_stale(conn: sqlite3.Connection, result: dict) -> bool:
    """A what-if is stale once the core has recorded any event after the snapshot it was computed on."""
    return latest_seq(conn) != result["snapshot_seq"]
