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
    """Spendable settled cash for an owner: CASH + PAYABLE + FEES_ACCRUED - active cash holds - net payments due
    on sales.

    Receivables are excluded (LED-03). Payables and accrued fees are commitments
    against settled cash and are subtracted (they carry credit balances). A sale
    whose fees exceed its proceeds settles as a payment out of the owner's cash
    (an EXPECTED settlement row that relieves RECEIVABLE with direction PAY); that
    amount is committed too, so it can never be spent first (review finding 25)."""
    return (balance(tx, account_key, CASH, owner, ccy)
            + balance(tx, account_key, PAYABLE, owner, ccy)
            + balance(tx, account_key, FEES_ACCRUED, owner, ccy)
            - active_cash_holds(tx, account_key, owner, ccy)
            - sale_payments_due(tx, account_key, owner, ccy))


def sale_payments_due(tx: Tx, account_key: str, owner: str, ccy: str) -> Fixed:
    total = Fixed(0)
    # The literal predicates match the partial index settlements_sale_payments (schema migration 2).
    for r in tx.all("SELECT json_extract(row, '$.amount') AS amount, json_extract(row, '$.currency') AS ccy FROM settlements "
                    "WHERE account_key=? AND owner=? AND status = 'EXPECTED' AND json_extract(row, '$.direction') = 'PAY' "
                    "AND json_extract(row, '$.relieves') = 'RECEIVABLE'", (account_key, owner)):
        if r["ccy"] == ccy:
            total = total + Fixed.parse(r["amount"])
    return total


def owner_lots(tx: Tx, account_key: str, owner: str, instrument_id: str | None = None) -> list[dict]:
    # Closed lots stay in the table forever; skipping the exact text '0' in SQL (a string comparison, no
    # arithmetic) avoids decoding them. Any other zero spelling is still removed by the exact check below.
    sql = "SELECT row FROM lots WHERE account_key=? AND owner=? AND open_qty != '0'"
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
    for r in tx.all("SELECT open_qty FROM lots WHERE account_key=? AND instrument_id=? AND open_qty != '0'",
                    (account_key, instrument_id)):
        total = total + Fixed.parse(r["open_qty"])
    return total


# ----------------------------------------------------------- verification
# The full check (``verify``) and the per-command check (``verify_touched``) word every problem through
# these helpers, so a test can require that both flag exactly the same things (review finding 39).
_BALANCE_KEY = "account_key=? AND account_code=? AND owner=? AND currency=? AND instrument_id=?"


def _entry_problem(entry_id: str, ccy: str, total: Fixed) -> str:
    return f"entry {entry_id} unbalanced in {ccy}: {total}"


def _balance_problem(key: tuple, stored: Fixed | None, postings: Fixed | None) -> str:
    return f"balance {key} stored {stored} != postings {postings}"


def _transfer_problem(entry_id: str, key: tuple) -> str:
    return f"owner transfer {entry_id} changes economic total of {key}"


def _inventory_problem(key: tuple, postings: Fixed | None, lots: Fixed | None) -> str:
    return f"inventory {key} postings {postings} != open lot cost {lots}"


def _cash_problem(account_key: str, owner: str, ccy: str) -> str:
    return f"available cash negative for {account_key}/{owner}/{ccy}"


def _quantity_problem(account_key: str, owner: str, unit: str) -> str:
    return f"quantity holds exceed owned lots for {account_key}/{owner}/{unit}"


def _differs(a: Fixed | None, b: Fixed | None) -> bool:
    return (Fixed(0) if a is None else a) != (Fixed(0) if b is None else b)


def _lot_key(lot: dict) -> tuple[str, str, str, str]:
    """(account, owner, currency, instrument): the key inventory cost is compared on."""
    return (lot["account_key"], lot["owner"], lot["currency"], lot["instrument_id"])


def _stored_balance(tx: Tx, key: tuple) -> Fixed | None:
    row = tx.one(f"SELECT amount FROM balances WHERE {_BALANCE_KEY}", key)
    return Fixed.parse(row["amount"]) if row else None


def covered_owners(tx: Tx) -> set[tuple[str, str]]:
    """(account, owner) pairs named by an OPEN *blocking* exception. A non-blocking case does not halt new
    exposure, so it cannot excuse a negative balance; and a case in one account never covers another
    account's owner of the same name (review finding 53)."""
    return {(r["account_key"], r["owner_hint"]) for r in tx.all(
        "SELECT account_key, json_extract(row,'$.owner_hint') AS owner_hint FROM exceptions "
        "WHERE status='OPEN' AND blocking=1") if r["owner_hint"]}


def is_covered(tx: Tx, account_key: str, owner: str) -> bool:
    return tx.one("SELECT 1 FROM exceptions WHERE account_key=? AND status='OPEN' AND blocking=1 "
                  "AND json_extract(row,'$.owner_hint')=? LIMIT 1", (account_key, owner)) is not None


def verify(tx: Tx) -> list[str]:
    """Recompute every derived ledger fact from postings and lots; return problems.

    Checks: each entry balances per currency; materialized balances equal the
    sum of postings; owner-transfer entries balance per account code; inventory
    cost per (account, owner, instrument) equals open lot cost; system-owner
    available cash and unreserved quantity are non-negative unless an open
    blocking exception in the same account covers the owner (command-path
    invariant, ADR-0006).

    Cost grows with the whole history. The command path runs it only where a
    whole-ledger check is the point (process start, reconciliation results,
    green start, writing a ledger-invariant case); every other command runs
    ``verify_touched``. ``Desk.verify``, the audit endpoint and tests use this."""
    problems: list[str] = []
    entry_totals: dict[tuple[str, str], Fixed] = defaultdict(lambda: Fixed(0))
    transfer_totals: dict[tuple[str, tuple], Fixed] = defaultdict(lambda: Fixed(0))
    recomputed: dict[tuple, Fixed] = defaultdict(lambda: Fixed(0))
    # One scan: per-entry totals, balances, and per-code totals of owner transfers (no query per transfer entry).
    for p in tx.all("SELECT p.entry_id, p.account_key, p.account_code, p.owner, p.currency, p.instrument_id, p.amount, "
                    "j.kind FROM postings p LEFT JOIN journal_entries j ON j.entry_id = p.entry_id"):
        amt = Fixed.parse(p["amount"])
        entry_totals[(p["entry_id"], p["currency"])] += amt
        recomputed[(p["account_key"], p["account_code"], p["owner"], p["currency"], p["instrument_id"])] += amt
        if p["kind"] == "OWNER_TRANSFER":
            transfer_totals[(p["entry_id"], (p["account_code"], p["currency"], p["instrument_id"]))] += amt
    for (eid, ccy), total in entry_totals.items():
        if not total.is_zero():
            problems.append(_entry_problem(eid, ccy, total))
    stored = {(r["account_key"], r["account_code"], r["owner"], r["currency"], r["instrument_id"]): Fixed.parse(r["amount"])
              for r in tx.all("SELECT * FROM balances")}
    for key in set(stored) | set(recomputed):
        if _differs(stored.get(key), recomputed.get(key)):
            problems.append(_balance_problem(key, stored.get(key), recomputed.get(key)))
    for (eid, key), total in transfer_totals.items():
        if not total.is_zero():
            problems.append(_transfer_problem(eid, key))
    lot_cost: dict[tuple, Fixed] = defaultdict(lambda: Fixed(0))
    for r in tx.all("SELECT row FROM lots"):
        lot = json.loads(r["row"])
        lot_cost[_lot_key(lot)] += Fixed.parse(lot["open_cost"])
    inventory = {(k[0], k[2], k[3], k[4]): v for k, v in recomputed.items() if k[1] == INVENTORY}
    for key in set(lot_cost) | set(inventory):
        if _differs(lot_cost.get(key), inventory.get(key)):
            problems.append(_inventory_problem(key, inventory.get(key), lot_cost.get(key)))
    covered = covered_owners(tx)
    owners = {(r["account_key"], r["owner"], r["currency"]) for r in tx.all(
        "SELECT DISTINCT account_key, owner, currency FROM balances")}
    for account_key, owner, ccy in owners:
        if not is_system_owner(owner) or (account_key, owner) in covered:
            continue
        if available_cash(tx, account_key, owner, ccy).is_negative():
            problems.append(_cash_problem(account_key, owner, ccy))
    for h in tx.all("SELECT DISTINCT account_key, owner, unit FROM holds WHERE kind='QUANTITY' AND status='ACTIVE'"):
        if (h["account_key"], h["owner"]) in covered:
            continue
        if active_quantity_holds(tx, h["account_key"], h["owner"], h["unit"]) > owned_quantity(tx, h["account_key"], h["owner"], h["unit"]):
            problems.append(_quantity_problem(h["account_key"], h["owner"], h["unit"]))
    return problems


class Touched:
    """What one command's events change, recorded by the projector just before each write.

    ``balances`` and ``lots`` keep each key's value from before the command (its pre-image; ``None``
    when the row did not exist), so ``verify_touched`` can check the command's own effects without
    re-reading the history of the keys it touched (review finding 39)."""

    __slots__ = ("entries", "balances", "lots", "holds", "cases", "settlements", "full")

    def __init__(self) -> None:
        self.entries: list[str] = []                                 # journal entry ids, in emission order
        self.balances: dict[tuple, Fixed | None] = {}                # balance key -> amount before the command
        self.lots: dict[str, tuple | None] = {}                      # lot id -> (inventory key, open_cost) before
        self.holds: set[tuple[str, str, str, str]] = set()           # (account, owner, kind, unit), before and after
        self.cases: set[tuple[str, str]] = set()                     # (account, owner_hint) of cases written
        self.settlements: set[tuple[str, str, str]] = set()          # (account, owner, currency), before and after
        self.full = False   # a LEDGER_INVARIANT case was written: clearing one needs the whole ledger clean

    def before(self, tx: Tx, section: str, item: dict) -> None:
        """Record what ``item`` (one row of an event's ``changes[section]``) is about to change."""
        if section == "journal":
            for line in item["lines"]:
                key = (item["account_key"], line["account_code"], line["owner"], line["currency"], line.get("instrument_id", ""))
                if key not in self.balances:
                    self.balances[key] = _stored_balance(tx, key)
            self.entries.append(item["entry_id"])
        elif section == "lots":
            if item["lot_id"] not in self.lots:
                r = tx.one("SELECT row FROM lots WHERE lot_id=?", (item["lot_id"],))
                lot = json.loads(r["row"]) if r else None
                self.lots[item["lot_id"]] = None if lot is None else (_lot_key(lot), Fixed.parse(lot["open_cost"]))
        elif section == "holds":
            r = tx.one("SELECT account_key, owner, kind, unit FROM holds WHERE hold_id=?", (item["hold_id"],))
            if r is not None:
                self.holds.add((r["account_key"], r["owner"], r["kind"], r["unit"]))
            self.holds.add((item["account_key"], item["owner"], item["kind"], item["unit"]))
        elif section == "settlements":
            r = tx.one("SELECT account_key, owner, json_extract(row,'$.currency') AS currency FROM settlements "
                       "WHERE settlement_id=?", (item["settlement_id"],))
            if r is not None:
                self.settlements.add((r["account_key"], r["owner"], r["currency"]))
            self.settlements.add((item["account_key"], item["owner"], item["currency"]))
        elif section == "exceptions":
            r = tx.one("SELECT account_key, kind, json_extract(row,'$.owner_hint') AS owner_hint FROM exceptions "
                       "WHERE case_id=?", (item["case_id"],))
            images = [(r["account_key"], r["kind"], r["owner_hint"])] if r else []
            for account_key, kind, hint in images + [(item["account_key"], item["kind"], item.get("owner_hint"))]:
                if hint:
                    self.cases.add((account_key, hint))
                if kind == "LEDGER_INVARIANT":
                    self.full = True


def _lot_changes(tx: Tx, touched: Touched) -> tuple[dict[tuple, Fixed], list[tuple[str, str, str]]]:
    """Change of open lot cost per inventory key, and the (account, owner, instrument) of every lot written."""
    change: dict[tuple, Fixed] = defaultdict(lambda: Fixed(0))
    owners: dict[tuple[str, str, str], None] = {}
    for lot_id, image in touched.lots.items():
        if image is not None:
            key, cost = image
            change[key] = change[key] - cost
            owners[(key[0], key[1], key[3])] = None
        r = tx.one("SELECT row FROM lots WHERE lot_id=?", (lot_id,))
        if r is not None:
            lot = json.loads(r["row"])
            key = _lot_key(lot)
            change[key] = change[key] + Fixed.parse(lot["open_cost"])
            owners[(key[0], key[1], key[3])] = None
    return change, list(owners)


def verify_touched(tx: Tx, touched: Touched) -> list[str]:
    """Per-command ledger check: ``verify`` restricted to what this command changed.

    It checks the entries the command emitted (read back from the postings
    table), every balance key they or the projector touched, the inventory
    cost of every (account, owner, currency, instrument) whose INVENTORY
    balance or lots changed, and available cash and quantity holds of every
    owner whose balances, holds or covering exceptions changed.

    Precondition: the state before the command passed verification. Under it,
    the result is exactly what ``verify`` returns now, and the cost is
    proportional to what the command wrote, not to the ledger's history: a
    balance is checked as its pre-image plus this command's postings, and lot
    cost as the inventory pre-image plus the change of the lots written,
    instead of re-summing every posting and lot of the key. Damage that
    predates the command (an edit made outside the core) is found by the full
    ``verify``, which the command path still runs at process start, at every
    reconciliation result and at green start."""
    problems: list[str] = []
    entry_totals: dict[tuple[str, str], Fixed] = defaultdict(lambda: Fixed(0))
    transfer_totals: dict[tuple[str, tuple], Fixed] = defaultdict(lambda: Fixed(0))
    delta: dict[tuple, Fixed] = defaultdict(lambda: Fixed(0))
    for i in range(0, len(touched.entries), 400):
        chunk = tuple(touched.entries[i:i + 400])
        for p in tx.all("SELECT p.entry_id, p.account_key, p.account_code, p.owner, p.currency, p.instrument_id, p.amount, "
                        "j.kind FROM postings p LEFT JOIN journal_entries j ON j.entry_id = p.entry_id "
                        f"WHERE p.entry_id IN ({', '.join('?' * len(chunk))})", chunk):
            amt = Fixed.parse(p["amount"])
            entry_totals[(p["entry_id"], p["currency"])] += amt
            delta[(p["account_key"], p["account_code"], p["owner"], p["currency"], p["instrument_id"])] += amt
            if p["kind"] == "OWNER_TRANSFER":
                transfer_totals[(p["entry_id"], (p["account_code"], p["currency"], p["instrument_id"]))] += amt
    for (eid, ccy), total in entry_totals.items():
        if not total.is_zero():
            problems.append(_entry_problem(eid, ccy, total))
    # Balances: the pre-image plus this command's postings must equal what is stored now.
    before: dict[tuple, Fixed | None] = {}
    recomputed: dict[tuple, Fixed | None] = {}
    for key in list(touched.balances) + [k for k in delta if k not in touched.balances]:
        stored = _stored_balance(tx, key)
        pre = touched.balances[key] if key in touched.balances else stored
        change = delta.get(key)
        value = None if pre is None and change is None else (
            Fixed(0) + (pre if pre is not None else Fixed(0)) + (change if change is not None else Fixed(0)))
        before[key], recomputed[key] = pre, value
        if _differs(stored, value):
            problems.append(_balance_problem(key, stored, value))
    for (eid, key), total in transfer_totals.items():
        if not total.is_zero():
            problems.append(_transfer_problem(eid, key))
    # Inventory: the open lot cost before the command equalled its INVENTORY balance (precondition), so the
    # open lot cost now is that pre-image plus the change of the lots this command wrote.
    lot_change, lot_owners = _lot_changes(tx, touched)
    inventory_keys = dict.fromkeys([(k[0], k[2], k[3], k[4]) for k in recomputed if k[1] == INVENTORY] + list(lot_change))
    for key in inventory_keys:
        k5 = (key[0], INVENTORY, key[1], key[2], key[3])
        if k5 in recomputed:
            inventory_now, inventory_before = recomputed[k5], before[k5]
        else:
            inventory_now = inventory_before = _stored_balance(tx, k5)
        lots_now = (Fixed(0) + (inventory_before if inventory_before is not None else Fixed(0))
                    + lot_change.get(key, Fixed(0)))
        if _differs(inventory_now, lots_now):
            exists = tx.one("SELECT 1 FROM lots WHERE json_extract(row,'$.account_key')=? AND json_extract(row,'$.owner')=? "
                            "AND json_extract(row,'$.currency')=? AND json_extract(row,'$.instrument_id')=? LIMIT 1", key)
            problems.append(_inventory_problem(key, inventory_now, lots_now if exists else None))
    # Available cash and quantity holds of every owner whose inputs, or covering cases, changed.
    cash_keys = dict.fromkeys((k[0], k[2], k[3]) for k in recomputed)
    quantity_keys = dict.fromkeys(lot_owners)
    for account_key, owner, kind, unit in sorted(touched.holds):
        (cash_keys if kind == "CASH" else quantity_keys)[(account_key, owner, unit)] = None
    for key in sorted(touched.settlements):
        cash_keys[key] = None   # a payment due on a sale is committed cash (available_cash)
    for account_key, owner in sorted(touched.cases):
        for r in tx.all("SELECT DISTINCT currency FROM balances WHERE account_key=? AND owner=?", (account_key, owner)):
            cash_keys[(account_key, owner, r["currency"])] = None
        for r in tx.all("SELECT DISTINCT unit FROM holds WHERE account_key=? AND owner=? AND kind='QUANTITY' "
                        "AND status='ACTIVE'", (account_key, owner)):
            quantity_keys[(account_key, owner, r["unit"])] = None
    for account_key, owner, ccy in cash_keys:
        if not is_system_owner(owner) or not available_cash(tx, account_key, owner, ccy).is_negative():
            continue
        if tx.one("SELECT 1 FROM balances WHERE account_key=? AND owner=? AND currency=? LIMIT 1",
                  (account_key, owner, ccy)) is None:
            continue  # like ``verify``: only owners holding a balance in the currency are checked
        if not is_covered(tx, account_key, owner):
            problems.append(_cash_problem(account_key, owner, ccy))
    for account_key, owner, unit in quantity_keys:
        rows = tx.all("SELECT amount FROM holds WHERE account_key=? AND owner=? AND kind='QUANTITY' AND unit=? "
                      "AND status='ACTIVE'", (account_key, owner, unit))
        if not rows:
            continue
        held = Fixed(0)
        for r in rows:
            held = held + Fixed.parse(r["amount"])
        if held > owned_quantity(tx, account_key, owner, unit) and not is_covered(tx, account_key, owner):
            problems.append(_quantity_problem(account_key, owner, unit))
    return problems


def quantity_shortfalls(tx: Tx, touched: Touched) -> list[dict]:
    """Owners whose lots or quantity holds this command changed and whose ACTIVE quantity holds now exceed
    the quantity they own (review finding 16).

    A broker fact can cause this: a correction that shrinks a buy leaves a working SELL reserving shares
    the owner no longer has. The observation path records the fact and opens a blocking case naming the
    owner (ADR-0006) instead of tripping the ledger-invariant alarm, which names no owner."""
    keys: dict[tuple[str, str, str], None] = {}
    for lot_id, image in touched.lots.items():
        if image is not None:
            keys[(image[0][0], image[0][1], image[0][3])] = None
        r = tx.one("SELECT account_key, owner, instrument_id FROM lots WHERE lot_id=?", (lot_id,))
        if r is not None:
            keys[(r["account_key"], r["owner"], r["instrument_id"])] = None
    for account_key, owner, kind, unit in sorted(touched.holds):
        if kind == "QUANTITY":
            keys[(account_key, owner, unit)] = None
    out = []
    for account_key, owner, instrument_id in keys:
        rows = tx.all("SELECT order_id, amount FROM holds WHERE account_key=? AND owner=? AND kind='QUANTITY' AND unit=? "
                      "AND status='ACTIVE' ORDER BY order_id", (account_key, owner, instrument_id))
        held = Fixed(0)
        for r in rows:
            held = held + Fixed.parse(r["amount"])
        if not held.is_positive():
            continue
        owned = owned_quantity(tx, account_key, owner, instrument_id)
        if held > owned:
            out.append({"account_key": account_key, "owner": owner, "instrument_id": instrument_id,
                        "held": held.to_str(), "owned": owned.to_str(),
                        "order_ids": [r["order_id"] for r in rows if not Fixed.parse(r["amount"]).is_zero()]})
    return out
