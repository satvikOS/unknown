"""Read helpers over projections, used by handlers inside the transaction."""
from __future__ import annotations

import json

from ..domain.errors import Rejected
from ..domain.fixed import Fixed
from ..domain.instruments import Instrument
from ..policy.model import Mandate, Policy
from ..store.db import Tx
from .order_machine import OPEN_STATES as OPEN_ORDER_STATES, TERMINAL_STATES as TERMINAL_ORDER_STATES  # noqa: F401
ACTIVE_SESSION_STATES = ("RUNNING", "SUSPENDED", "WINDING_DOWN", "RECONCILING")


def _load(tx: Tx, table: str, key_col: str, key: str) -> dict | None:
    r = tx.one(f"SELECT row FROM {table} WHERE {key_col}=?", (key,))
    return json.loads(r["row"]) if r else None


def account(tx: Tx, account_key: str) -> dict:
    a = _load(tx, "accounts", "account_key", account_key)
    if a is None:
        raise Rejected("UNKNOWN_ACCOUNT", account_key)
    return a


def control(tx: Tx, account_key: str) -> dict:
    c = _load(tx, "control", "account_key", account_key)
    if c is None:
        raise Rejected("UNKNOWN_ACCOUNT", account_key)
    return c


def ownership(tx: Tx, account_key: str) -> dict | None:
    return _load(tx, "execution_ownership", "account_key", account_key)


def risk_state(tx: Tx, account_key: str) -> dict:
    r = _load(tx, "risk_state", "account_key", account_key)
    if r is None:
        raise Rejected("UNKNOWN_ACCOUNT", account_key)
    return r


def policy_by_hash(tx: Tx, policy_hash: str) -> Policy:
    r = tx.one("SELECT body FROM policies WHERE policy_hash=?", (policy_hash,))
    if r is None:
        raise Rejected("UNKNOWN_POLICY", policy_hash)
    return Policy.parse(json.loads(r["body"]))


def active_policy(tx: Tx, account_key: str) -> Policy:
    r = tx.one("SELECT policy_hash FROM policy_active WHERE account_key=?", (account_key,))
    if r is None:
        raise Rejected("NO_ACTIVE_POLICY", f"account {account_key} has no active policy")
    return policy_by_hash(tx, r["policy_hash"])


def instrument(tx: Tx, instrument_id: str) -> Instrument | None:
    r = tx.one("SELECT body FROM instruments WHERE instrument_id=?", (instrument_id,))
    return Instrument.from_json(json.loads(r["body"])) if r else None


def session(tx: Tx, session_id: str) -> dict:
    s = _load(tx, "sessions", "session_id", session_id)
    if s is None:
        raise Rejected("UNKNOWN_SESSION", session_id)
    return s


def mandate(tx: Tx, mandate_hash: str) -> Mandate:
    r = tx.one("SELECT body FROM mandates WHERE mandate_hash=?", (mandate_hash,))
    if r is None:
        raise Rejected("UNKNOWN_MANDATE", mandate_hash)
    return Mandate.from_json(json.loads(r["body"]))


def sessions(tx: Tx, account_key: str, states: tuple[str, ...] | None = None) -> list[dict]:
    rows = [json.loads(r["row"]) for r in tx.all("SELECT row FROM sessions WHERE account_key=? ORDER BY rowid", (account_key,))]
    return [s for s in rows if states is None or s["state"] in states]


def order(tx: Tx, order_id: str) -> dict | None:
    return _load(tx, "orders", "order_id", order_id)


def order_by_broker_id(tx: Tx, account_key: str, broker_order_id: str) -> dict | None:
    r = tx.one("SELECT row FROM orders WHERE account_key=? AND broker_order_id=?", (account_key, broker_order_id))
    return json.loads(r["row"]) if r else None


def orders(tx: Tx, account_key: str, *, session_id: str | None = None, open_only: bool = False) -> list[dict]:
    sql, params = "SELECT row FROM orders WHERE account_key=?", [account_key]
    if session_id is not None:
        sql += " AND session_id=?"
        params.append(session_id)
    rows = [json.loads(r["row"]) for r in tx.all(sql + " ORDER BY accepted_at_ns, order_id", tuple(params))]
    return [o for o in rows if not open_only or o["state"] in OPEN_ORDER_STATES]


def holds_for_order(tx: Tx, order_id: str) -> list[dict]:
    return [dict(r) for r in tx.all("SELECT * FROM holds WHERE order_id=?", (order_id,))]


def outbox_rows(tx: Tx, account_key: str, states: tuple[str, ...]) -> list[dict]:
    rows = [json.loads(r["row"]) for r in tx.all(
        "SELECT row FROM outbox WHERE account_key=? ORDER BY created_seq, outbox_id", (account_key,))]
    return [r for r in rows if r["state"] in states]


def outbox_for_order(tx: Tx, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in tx.all("SELECT row FROM outbox WHERE order_id=? ORDER BY created_seq", (order_id,))]


def execution(tx: Tx, exec_key: str) -> dict | None:
    return _load(tx, "executions", "exec_key", exec_key)


def executions_for_order(tx: Tx, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in tx.all("SELECT row FROM executions WHERE order_id=? ORDER BY rowid", (order_id,))]


def settlements(tx: Tx, account_key: str, *, owner: str | None = None, status: str | None = None) -> list[dict]:
    rows = [json.loads(r["row"]) for r in tx.all("SELECT row FROM settlements WHERE account_key=? ORDER BY rowid", (account_key,))]
    return [r for r in rows if (owner is None or r["owner"] == owner) and (status is None or r["status"] == status)]


def open_exceptions(tx: Tx, account_key: str, *, blocking_only: bool = False) -> list[dict]:
    rows = [json.loads(r["row"]) for r in tx.all(
        "SELECT row FROM exceptions WHERE account_key=? AND status='OPEN' ORDER BY rowid", (account_key,))]
    return [r for r in rows if r["blocking"] or not blocking_only]


def exception_by_key(tx: Tx, account_key: str, key: str) -> dict | None:
    for r in open_exceptions(tx, account_key):
        if r.get("key") == key:
            return r
    return None


def latest_reconciliation(tx: Tx, account_key: str) -> dict | None:
    """The run recorded last. Runs are append-only rows inserted in event order (replay preserves the
    rowid), so insertion order is the truth; wall-clock ``completed_at_ns`` can step back and is kept
    for display and age checks only (review finding 51)."""
    r = tx.one("SELECT row FROM reconciliations WHERE account_key=? ORDER BY rowid DESC LIMIT 1", (account_key,))
    return json.loads(r["row"]) if r else None


def fx(value: str | None) -> Fixed | None:
    return Fixed.parse(value) if value is not None else None
