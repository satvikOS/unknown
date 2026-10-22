"""External cash flows of a scope, read from the core journal (CAP-PORT-04; formula port.external_flow@1).

GIPS-consistent separation of capital movements from performance: a flow is value that
enters or leaves the scope by an owner transfer (capital authorization or
deauthorization, session funding, protection, closure), a recorded external cash flow,
or the opening import. Trading P&L, fees, income and settlement never appear here,
because they post within one owner and to income/expense codes.

Point in time: a flow is known from its event's ``recorded_time_ns``; ``until_ns``
excludes everything recorded later, so a historical computation can never see a flow
that had not been booked yet."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from ..core import ledger
from ..domain.calendar import local_date
from ..domain.fixed import Fixed
from ..domain.money import currency
from .common import Scope

FLOW_ENTRY_KINDS = ("OWNER_TRANSFER", "EXTERNAL_FLOW", "OPENING")
VALUE_CODES = (ledger.CASH, ledger.RECEIVABLE, ledger.PAYABLE, ledger.FEES_ACCRUED, ledger.INVENTORY)


@dataclass(frozen=True)
class Flow:
    entry_id: str
    kind: str
    memo: str
    event_seq: int
    event_type: str
    time_ns: int
    currency: str
    amount: Fixed          # signed: + into the scope, - out of it

    def to_json(self, tz: str | None = None) -> dict:
        d = {"entry_id": self.entry_id, "kind": self.kind, "memo": self.memo, "event_seq": self.event_seq,
             "event_type": self.event_type, "time_ns": self.time_ns, "currency": self.currency,
             "amount": self.amount.to_str()}
        if tz:
            d["date"] = local_date(self.time_ns, tz).isoformat()
        return d


def external_flows(conn: sqlite3.Connection, account_key: str, scope: Scope, *, until_ns: int | None = None,
                   after_ns: int | None = None) -> list[Flow]:
    """Net flow per (journal entry, currency) into ``scope``; zero nets (internal moves) are dropped."""
    marks = ",".join("?" for _ in FLOW_ENTRY_KINDS)
    codes = ",".join("?" for _ in VALUE_CODES)
    sql = (f"SELECT je.entry_id, je.kind, je.memo, je.event_seq, e.type AS event_type, e.recorded_time_ns, "
           f"p.owner, p.currency, p.amount FROM journal_entries je JOIN events e ON e.seq = je.event_seq "
           f"JOIN postings p ON p.entry_id = je.entry_id WHERE je.account_key=? AND je.kind IN ({marks}) "
           f"AND p.account_code IN ({codes})")
    params: list = [account_key, *FLOW_ENTRY_KINDS, *VALUE_CODES]
    if until_ns is not None:
        sql += " AND e.recorded_time_ns <= ?"
        params.append(until_ns)
    if after_ns is not None:
        sql += " AND e.recorded_time_ns > ?"
        params.append(after_ns)
    sql += " ORDER BY je.event_seq, je.entry_id, p.line"
    acc: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for r in conn.execute(sql, tuple(params)):
        if not scope.includes(r["owner"]):
            continue
        key = (r["entry_id"], r["currency"])
        if key not in acc:
            acc[key] = {"row": r, "amount": Fixed(0, currency(r["currency"]).ledger_scale)}
            order.append(key)
        acc[key]["amount"] = acc[key]["amount"] + Fixed.parse(r["amount"])
    out = []
    for key in order:
        a = acc[key]
        if a["amount"].is_zero():
            continue
        r = a["row"]
        out.append(Flow(r["entry_id"], r["kind"], r["memo"], r["event_seq"], r["event_type"], r["recorded_time_ns"],
                        r["currency"], a["amount"]))
    return out


def cumulative(flows: list[Flow], *, until_ns: int | None = None) -> dict[str, Fixed]:
    """Net flows per currency up to and including ``until_ns``."""
    total: dict[str, Fixed] = defaultdict(lambda: Fixed(0))
    for f in flows:
        if until_ns is None or f.time_ns <= until_ns:
            total[f.currency] = total[f.currency] + f.amount
    return {c: v.rescale(currency(c).ledger_scale) for c, v in total.items()}
