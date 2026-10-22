"""Generic projector: applies the decided ``changes`` carried by an event.

Handlers decide everything (ids, postings, lot rows, new order rows); the
projector only writes. Replaying the event log through this function must
reproduce every projection exactly (ADR-0005)."""
from __future__ import annotations

import json
from collections import defaultdict

from ..domain.canonical import canonical_json
from ..domain.errors import InvariantViolation
from ..domain.fixed import Fixed
from ..domain.money import currency
from ..store.db import Tx
from .ledger import Touched

SECTION_ORDER = (
    "accounts", "control", "ownership", "policies", "policy_active", "instruments", "mappings",
    "mandates", "sessions", "successors", "journal", "lots", "orders", "holds", "outbox",
    "executions", "pending_commissions", "settlements", "exceptions", "reconciliations", "risk_state", "proposals",
)


def _row(value: dict) -> str:
    return canonical_json(value)


PRIMARY_KEYS = {
    "accounts": ("account_key",), "control": ("account_key",), "execution_ownership": ("account_key",),
    "policy_active": ("account_key",), "instruments": ("instrument_id",),
    "balances": ("account_key", "account_code", "owner", "currency", "instrument_id"),
    "lots": ("lot_id",), "sessions": ("session_id",), "orders": ("order_id",), "holds": ("hold_id",), "outbox": ("outbox_id",),
    "executions": ("exec_key",), "pending_commissions": ("exec_key",), "settlements": ("settlement_id",),
    "exceptions": ("case_id",), "risk_state": ("account_key",),
}


def _upsert(tx: Tx, table: str, cols: tuple[str, ...], values: tuple) -> None:
    """INSERT ... ON CONFLICT DO UPDATE: unlike INSERT OR REPLACE it keeps the rowid,
    so creation order (used for FIFO and display) survives updates."""
    pk = PRIMARY_KEYS[table]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    sql = (f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
           f"ON CONFLICT({', '.join(pk)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING"))
    tx.run(sql, values)


def apply_changes(tx: Tx, seq: int, changes: dict, touched: Touched | None = None) -> None:
    """Write one event's changes. ``touched`` (the command path passes one; replay does not) records
    each row's pre-image before it is written, for the per-command ledger check (review finding 39)."""
    unknown = set(changes) - set(SECTION_ORDER)
    if unknown:
        raise InvariantViolation(f"unknown change sections {sorted(unknown)}")
    for section in SECTION_ORDER:
        for item in changes.get(section, ()):
            if touched is not None:
                touched.before(tx, section, item)
            _APPLIERS[section](tx, seq, item)


def _accounts(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "accounts", ("account_key", "provider", "mode", "base_currency", "row"), (r["account_key"], r["provider"], r["mode"], r["base_currency"], _row(r)))


def _control(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "control", ("account_key", "stop_latched", "row"), (r["account_key"], 1 if r["stop_latched"] else 0, _row(r)))


def _ownership(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "execution_ownership", ("account_key", "deployment_id", "epoch", "status", "row"), (r["account_key"], r["deployment_id"], r["epoch"], r["status"], _row(r)))


def _policies(tx: Tx, seq: int, r: dict) -> None:
    tx.run("INSERT INTO policies(policy_hash, name, body, published_by, published_at_ns) VALUES (?,?,?,?,?)",
           (r["policy_hash"], r["name"], canonical_json(r["body"]), r["published_by"], r["published_at_ns"]))


def _policy_active(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "policy_active", ("account_key", "policy_hash", "row"), (r["account_key"], r["policy_hash"], _row(r)))


def _instruments(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "instruments", ("instrument_id", "symbol", "body", "metadata_version"),
            (r["instrument_id"], r["symbol"], _row(r), r["metadata_version"]))


def _mappings(tx: Tx, seq: int, r: dict) -> None:
    tx.run("INSERT INTO provider_mappings(provider, external_id, version, instrument_id, row) VALUES (?,?,?,?,?)",
           (r["provider"], r["external_id"], r["version"], r["instrument_id"], _row(r)))


def _mandates(tx: Tx, seq: int, r: dict) -> None:
    tx.run("INSERT INTO mandates(mandate_hash, session_id, body) VALUES (?,?,?)",
           (r["mandate_hash"], r["session_id"], canonical_json(r["body"])))


def _sessions(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "sessions", ("session_id", "account_key", "state", "mandate_hash", "predecessor_id", "row"), (r["session_id"], r["account_key"], r["state"], r["mandate_hash"], r.get("predecessor_id"), _row(r)))


def _successors(tx: Tx, seq: int, r: dict) -> None:
    tx.run("INSERT INTO successors(predecessor_id, successor_id) VALUES (?,?)", (r["predecessor_id"], r["successor_id"]))


def _journal(tx: Tx, seq: int, e: dict) -> None:
    totals: dict[str, Fixed] = defaultdict(lambda: Fixed(0))
    tx.run("INSERT INTO journal_entries(entry_id, event_seq, account_key, kind, memo, reverses) VALUES (?,?,?,?,?,?)",
           (e["entry_id"], seq, e["account_key"], e["kind"], e["memo"], e.get("reverses")))
    for i, line in enumerate(e["lines"], start=1):
        amount = Fixed.parse(line["amount"])
        totals[line["currency"]] += amount
        key = (e["account_key"], line["account_code"], line["owner"], line["currency"], line.get("instrument_id", ""))
        tx.run("INSERT INTO postings(entry_id, line, account_key, account_code, owner, currency, instrument_id, amount) VALUES (?,?,?,?,?,?,?,?)",
               (e["entry_id"], i) + key[:4] + (key[4], amount.to_str()))
        current = tx.one("SELECT amount FROM balances WHERE account_key=? AND account_code=? AND owner=? AND currency=? AND instrument_id=?", key)
        scale = currency(line["currency"]).ledger_scale
        new = (Fixed.parse(current["amount"]) if current else Fixed(0, scale)) + amount
        _upsert(tx, "balances", ("account_key", "account_code", "owner", "currency", "instrument_id", "amount"),
                key + (new.rescale(scale).to_str(),))
    for ccy, total in totals.items():
        if not total.is_zero():
            raise InvariantViolation(f"projected entry {e['entry_id']} unbalanced in {ccy}")


def _lots(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "lots", ("lot_id", "account_key", "instrument_id", "owner", "open_qty", "row"), (r["lot_id"], r["account_key"], r["instrument_id"], r["owner"], r["open_qty"], _row(r)))


def _orders(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "orders", ("order_id", "account_key", "session_id", "instrument_id", "side", "state", "broker_order_id",
                           "accepted_at_ns", "row"),
            (r["order_id"], r["account_key"], r.get("session_id"), r["instrument_id"], r["side"], r["state"],
             r.get("broker_order_id"), r["accepted_at_ns"], _row(r)))


def _holds(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "holds", ("hold_id", "order_id", "account_key", "owner", "kind", "unit", "amount", "status"), (r["hold_id"], r["order_id"], r["account_key"], r["owner"], r["kind"], r["unit"], r["amount"], r["status"]))


def _outbox(tx: Tx, seq: int, r: dict) -> None:
    created = r.get("created_seq") or seq
    r = {**r, "created_seq": created}
    _upsert(tx, "outbox", ("outbox_id", "order_id", "account_key", "action", "state", "created_seq", "row"),
            (r["outbox_id"], r["order_id"], r["account_key"], r["action"], r["state"], created, _row(r)))


def _executions(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "executions", ("exec_key", "account_key", "order_id", "row"), (r["exec_key"], r["account_key"], r.get("order_id"), _row(r)))


def _pending_commissions(tx: Tx, seq: int, r: dict) -> None:
    if r.get("delete"):
        tx.run("DELETE FROM pending_commissions WHERE exec_key=?", (r["exec_key"],))
    else:
        _upsert(tx, "pending_commissions", ("exec_key", "account_key", "row"), (r["exec_key"], r["account_key"], _row(r)))


def _settlements(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "settlements", ("settlement_id", "account_key", "owner", "status", "row"),
            (r["settlement_id"], r["account_key"], r["owner"], r["status"], _row(r)))


def _exceptions(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "exceptions", ("case_id", "account_key", "kind", "blocking", "status", "session_id", "order_id", "row"), (r["case_id"], r["account_key"], r["kind"], 1 if r["blocking"] else 0, r["status"], r.get("session_id"),
            r.get("order_id"), _row(r)))


def _reconciliations(tx: Tx, seq: int, r: dict) -> None:
    tx.run("INSERT INTO reconciliations(run_id, account_key, status, completed_at_ns, row) VALUES (?,?,?,?,?)",
           (r["run_id"], r["account_key"], r["status"], r["completed_at_ns"], _row(r)))


def _risk_state(tx: Tx, seq: int, r: dict) -> None:
    _upsert(tx, "risk_state", ("account_key", "row"), (r["account_key"], _row(r)))


def _proposals(tx: Tx, seq: int, r: dict) -> None:
    tx.run("INSERT INTO proposals(proposal_id, author, row) VALUES (?,?,?)", (r["proposal_id"], r["author"], _row(r)))


_APPLIERS = {
    "accounts": _accounts, "control": _control, "ownership": _ownership, "policies": _policies,
    "policy_active": _policy_active, "instruments": _instruments, "mappings": _mappings, "mandates": _mandates,
    "sessions": _sessions, "successors": _successors, "journal": _journal, "lots": _lots, "orders": _orders,
    "holds": _holds, "outbox": _outbox, "executions": _executions, "pending_commissions": _pending_commissions,
    "settlements": _settlements, "exceptions": _exceptions, "reconciliations": _reconciliations,
    "risk_state": _risk_state, "proposals": _proposals,
}


def load_row(tx: Tx, table: str, key_col: str, key: str) -> dict | None:
    r = tx.one(f"SELECT row FROM {table} WHERE {key_col}=?", (key,))
    return json.loads(r["row"]) if r else None
