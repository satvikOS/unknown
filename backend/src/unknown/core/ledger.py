"""Economic journal with an owner dimension, holds and invariant verification
(ADR-0006). Pure helpers: building entries never touches the database;
reading balances does, inside the caller's transaction."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from ..domain.errors import InvariantViolation
from ..domain.fixed import Fixed, Rounding
from ..domain.money import currency
from ..store.db import Tx

# Account codes -------------------------------------------------------------
CASH = "CASH"
RECEIVABLE = "RECEIVABLE"
PAYABLE = "PAYABLE"
FEES_ACCRUED = "FEES_ACCRUED"
INVENTORY = "INVENTORY"
OPENING = "OPENING"
CONTRIBUTED = "CONTRIBUTED"
REALIZED = "REALIZED"
COMMISSION = "COMMISSION"
FEE_ESTIMATE = "FEE_ESTIMATE"
OTHER_FEES = "OTHER_FEES"
DIVIDEND = "DIVIDEND"
WITHHOLDING = "WITHHOLDING"
INTEREST = "INTEREST"
ROUNDING = "ROUNDING"
SUSPENSE = "SUSPENSE"

ACCOUNT_CODES = frozenset({CASH, RECEIVABLE, PAYABLE, FEES_ACCRUED, INVENTORY, OPENING, CONTRIBUTED, REALIZED,
                           COMMISSION, FEE_ESTIMATE, OTHER_FEES, DIVIDEND, WITHHOLDING, INTEREST, ROUNDING, SUSPENSE})
# Codes whose balance is part of an owner's liquid value (cash-like, signed).
LIQUID_CODES = (CASH, RECEIVABLE, PAYABLE, FEES_ACCRUED)

# Owners ------------------------------------------------------------------------
OUTSIDE = "outside"
UNALLOCATED = "unallocated"
BUFFER = "buffer"
EXTERNAL = "external"


def session_owner(session_id: str) -> str:
    return f"session:{session_id}"


def protected_owner(session_id: str) -> str:
    return f"protected:{session_id}"


def exception_owner(case_id: str) -> str:
    return f"exception:{case_id}"


def is_system_owner(owner: str) -> bool:
    """Owners whose value belongs to the system's authorized capital."""
    return owner in (UNALLOCATED, BUFFER) or owner.startswith(("session:", "protected:"))


def valid_owner(owner: str) -> bool:
    return owner in (OUTSIDE, UNALLOCATED, BUFFER, EXTERNAL) or owner.startswith(("session:", "protected:", "exception:"))


@dataclass(frozen=True, slots=True)
class Line:
    account_code: str
    owner: str
    currency: str
    amount: Fixed            # signed: debit positive, credit negative
    instrument_id: str = ""

    def to_json(self) -> dict:
        return {"account_code": self.account_code, "owner": self.owner, "currency": self.currency,
                "amount": self.amount.to_str(), "instrument_id": self.instrument_id}


def entry(entry_id: str, account_key: str, kind: str, memo: str, lines: list[Line], reverses: str | None = None) -> dict:
    """Validate and serialize a journal entry; balanced per currency at ledger scale."""
    if not lines:
        raise InvariantViolation("journal entry without lines")
    totals: dict[str, Fixed] = defaultdict(lambda: Fixed(0))
    out = []
    for line in lines:
        if line.account_code not in ACCOUNT_CODES:
            raise InvariantViolation(f"unknown account code {line.account_code}")
        if not valid_owner(line.owner):
            raise InvariantViolation(f"invalid owner {line.owner}")
        if line.account_code == INVENTORY and not line.instrument_id:
            raise InvariantViolation("inventory posting requires instrument_id")
        scale = currency(line.currency).ledger_scale
        amount = line.amount.rescale(scale, Rounding.UNNECESSARY)  # raises if not exact at ledger scale
        if amount.is_zero():
            continue
        totals[line.currency] = totals[line.currency] + amount
        out.append({**line.to_json(), "amount": amount.to_str()})
    for ccy, total in totals.items():
        if not total.is_zero():
            raise InvariantViolation(f"unbalanced journal entry {kind}: {ccy} {total}")
    if not out:
        raise InvariantViolation("journal entry with only zero lines")
    return {"entry_id": entry_id, "account_key": account_key, "kind": kind, "memo": memo,
            "reverses": reverses, "lines": out}


def transfer(code: str, ccy: str, amount: Fixed, from_owner: str, to_owner: str, instrument_id: str = "") -> list[Line]:
    """Owner transfer: same account code and currency, opposite signs (moves a balance of sign(amount))."""
    return [Line(code, to_owner, ccy, amount, instrument_id), Line(code, from_owner, ccy, -amount, instrument_id)]


def reverse_lines(lines: list[dict]) -> list[Line]:
    return [Line(l["account_code"], l["owner"], l["currency"], -Fixed.parse(l["amount"]), l.get("instrument_id", ""))
            for l in lines]


# ----------------------------------------------------------------- reads
def balance(tx: Tx, account_key: str, code: str, owner: str, ccy: str, instrument_id: str = "") -> Fixed:
    row = tx.one("SELECT amount FROM balances WHERE account_key=? AND account_code=? AND owner=? AND currency=? AND instrument_id=?",
                 (account_key, code, owner, ccy, instrument_id))
    return Fixed.parse(row["amount"]) if row else Fixed(0, currency(ccy).ledger_scale)


def owner_balances(tx: Tx, account_key: str, owner: str) -> dict[tuple[str, str, str], Fixed]:
    return {(r["account_code"], r["currency"], r["instrument_id"]): Fixed.parse(r["amount"])
            for r in tx.all("SELECT account_code, currency, instrument_id, amount FROM balances WHERE account_key=? AND owner=?",
                            (account_key, owner))}


def code_total(tx: Tx, account_key: str, code: str, ccy: str, instrument_id: str | None = None) -> Fixed:
    total = Fixed(0, currency(ccy).ledger_scale)
    sql = "SELECT amount FROM balances WHERE account_key=? AND account_code=? AND currency=?"
    params: tuple = (account_key, code, ccy)
    if instrument_id is not None:
        sql += " AND instrument_id=?"
        params += (instrument_id,)
    for r in tx.all(sql, params):
        total = total + Fixed.parse(r["amount"])
    return total


def active_cash_holds(tx: Tx, account_key: str, owner: str, ccy: str) -> Fixed:
    total = Fixed(0, currency(ccy).ledger_scale)
    for r in tx.all("SELECT amount FROM holds WHERE account_key=? AND owner=? AND kind='CASH' AND unit=? AND status='ACTIVE'",
                    (account_key, owner, ccy)):
        total = total + Fixed.parse(r["amount"])
    return total


def liquid_value(tx: Tx, account_key: str, owner: str, ccy: str) -> Fixed:
    """CASH + RECEIVABLE + PAYABLE + FEES_ACCRUED (payables/fees carry negative balances)."""
    total = Fixed(0, currency(ccy).ledger_scale)
    for code in LIQUID_CODES:
        total = total + balance(tx, account_key, code, owner, ccy)
    return total


def available_cash(tx: Tx, account_key: str, owner: str, ccy: str) -> Fixed:
    """Spendable settled cash for an owner: CASH + PAYABLE + FEES_ACCRUED - active cash holds.

    Receivables are excluded (LED-03). Payables and accrued fees are commitments
    against settled cash and are subtracted (they carry credit balances)."""
    return (balance(tx, account_key, CASH, owner, ccy)
            + balance(tx, account_key, PAYABLE, owner, ccy)
            + balance(tx, account_key, FEES_ACCRUED, owner, ccy)
            - active_cash_holds(tx, account_key, owner, ccy))


def owner_lots(tx: Tx, account_key: str, owner: str, instrument_id: str | None = None) -> list[dict]:
    sql = "SELECT row FROM lots WHERE account_key=? AND owner=?"
    params: tuple = (account_key, owner)
    if instrument_id is not None:
        sql += " AND instrument_id=?"
        params += (instrument_id,)
    rows = [json.loads(r["row"]) for r in tx.all(sql, params)]
    rows.sort(key=lambda lot: (lot["opened_at_ns"], lot["lot_id"]))  # FIFO by acquisition, never by storage order
    return [r for r in rows if not Fixed.parse(r["open_qty"]).is_zero()]


def owned_quantity(tx: Tx, account_key: str, owner: str, instrument_id: str) -> Fixed:
    total = Fixed(0)
    for lot in owner_lots(tx, account_key, owner, instrument_id):
        total = total + Fixed.parse(lot["open_qty"])
    return total


def active_quantity_holds(tx: Tx, account_key: str, owner: str, instrument_id: str) -> Fixed:
    total = Fixed(0)
    for r in tx.all("SELECT amount FROM holds WHERE account_key=? AND owner=? AND kind='QUANTITY' AND unit=? AND status='ACTIVE'",
                    (account_key, owner, instrument_id)):
        total = total + Fixed.parse(r["amount"])
    return total


def position_quantity(tx: Tx, account_key: str, instrument_id: str) -> Fixed:
    total = Fixed(0)
    for r in tx.all("SELECT open_qty FROM lots WHERE account_key=? AND instrument_id=?", (account_key, instrument_id)):
        total = total + Fixed.parse(r["open_qty"])
    return total


# ----------------------------------------------------------- verification
def verify(tx: Tx) -> list[str]:
    """Recompute every derived ledger fact from postings and lots; return problems.

    Checks: each entry balances per currency; materialized balances equal the
    sum of postings; owner-transfer entries balance per account code; inventory
    cost per (account, owner, instrument) equals open lot cost; system-owner
    available cash and unreserved quantity are non-negative unless an open
    exception covers the owner (command-path invariant, ADR-0006)."""
    problems: list[str] = []
    entry_totals: dict[tuple[str, str], Fixed] = defaultdict(lambda: Fixed(0))
    recomputed: dict[tuple, Fixed] = defaultdict(lambda: Fixed(0))
    for p in tx.all("SELECT entry_id, account_key, account_code, owner, currency, instrument_id, amount FROM postings"):
        amt = Fixed.parse(p["amount"])
        entry_totals[(p["entry_id"], p["currency"])] += amt
        recomputed[(p["account_key"], p["account_code"], p["owner"], p["currency"], p["instrument_id"])] += amt
    for (eid, ccy), total in entry_totals.items():
        if not total.is_zero():
            problems.append(f"entry {eid} unbalanced in {ccy}: {total}")
    stored = {(r["account_key"], r["account_code"], r["owner"], r["currency"], r["instrument_id"]): Fixed.parse(r["amount"])
              for r in tx.all("SELECT * FROM balances")}
    for key in set(stored) | set(recomputed):
        if stored.get(key, Fixed(0)) != recomputed.get(key, Fixed(0)):
            problems.append(f"balance {key} stored {stored.get(key)} != postings {recomputed.get(key)}")
    for e in tx.all("SELECT entry_id FROM journal_entries WHERE kind='OWNER_TRANSFER'"):
        per_code: dict[tuple[str, str, str], Fixed] = defaultdict(lambda: Fixed(0))
        for p in tx.all("SELECT account_code, currency, instrument_id, amount FROM postings WHERE entry_id=?", (e["entry_id"],)):
            per_code[(p["account_code"], p["currency"], p["instrument_id"])] += Fixed.parse(p["amount"])
        for key, total in per_code.items():
            if not total.is_zero():
                problems.append(f"owner transfer {e['entry_id']} changes economic total of {key}")
    lot_cost: dict[tuple, Fixed] = defaultdict(lambda: Fixed(0))
    for r in tx.all("SELECT row FROM lots"):
        lot = json.loads(r["row"])
        lot_cost[(lot["account_key"], lot["owner"], lot["currency"], lot["instrument_id"])] += Fixed.parse(lot["open_cost"])
    inventory = {(k[0], k[2], k[3], k[4]): v for k, v in recomputed.items() if k[1] == INVENTORY}
    for key in set(lot_cost) | set(inventory):
        if lot_cost.get(key, Fixed(0)) != inventory.get(key, Fixed(0)):
            problems.append(f"inventory {key} postings {inventory.get(key)} != open lot cost {lot_cost.get(key)}")
    covered = {r["owner_hint"] for r in tx.all(
        "SELECT json_extract(row,'$.owner_hint') AS owner_hint FROM exceptions WHERE status='OPEN'") if r["owner_hint"]}
    owners = {(r["account_key"], r["owner"], r["currency"]) for r in tx.all(
        "SELECT DISTINCT account_key, owner, currency FROM balances")}
    for account_key, owner, ccy in owners:
        if not is_system_owner(owner) or owner in covered:
            continue
        if available_cash(tx, account_key, owner, ccy).is_negative():
            problems.append(f"available cash negative for {account_key}/{owner}/{ccy}")
    for h in tx.all("SELECT DISTINCT account_key, owner, unit FROM holds WHERE kind='QUANTITY' AND status='ACTIVE'"):
        if h["owner"] in covered:
            continue
        if active_quantity_holds(tx, h["account_key"], h["owner"], h["unit"]) > owned_quantity(tx, h["account_key"], h["owner"], h["unit"]):
            problems.append(f"quantity holds exceed owned lots for {h['account_key']}/{h['owner']}/{h['unit']}")
    return problems
