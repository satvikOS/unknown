"""Read models for the terminal, computed from a read-only connection.

Every monetary value is returned as a decimal string with its currency; every
row says which bucket (owner) it belongs to; derived figures state their
basis (e.g. a mark's quote source and age). Nothing here writes.

Nanosecond timestamps (API convention, applied to every JSON response by
``with_ns_strings``): each key ending in ``_ns`` whose value is an integer or
null gets a sibling ``<key>_str`` holding the same value as a decimal string
(or null). The integer stays for existing readers; it exceeds 2**53, so a
JavaScript client that needs the exact value reads ``<key>_str``."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from ..core import ledger
from ..domain.errors import Rejected
from ..domain.fixed import Fixed, Rounding
from ..domain.money import currency
from ..store.db import Tx

NS_SUFFIX = "_ns"
NS_STRING_SUFFIX = "_str"


def with_ns_strings(value):
    """A copy of ``value`` in which every ``*_ns`` integer (or null) has a ``*_ns_str`` decimal-string sibling.

    Recurses through dicts, lists and tuples; never overwrites a ``*_str`` key the value already has; leaves
    every other value untouched (``bool`` is not an integer here)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = with_ns_strings(v)
            if isinstance(k, str) and k.endswith(NS_SUFFIX) and k + NS_STRING_SUFFIX not in value and (
                    v is None or (isinstance(v, int) and not isinstance(v, bool))):
                out[k + NS_STRING_SUFFIX] = None if v is None else str(v)
        return out
    if isinstance(value, (list, tuple)):
        return [with_ns_strings(v) for v in value]
    return value


def stored_command(conn: sqlite3.Connection, command_id: str, *, principal_id: str, principal_role: str) -> dict | None:
    """The stored result of one command (a primary-key read), if ``(principal_id, principal_role)`` issued it.

    Another principal's command reads as absent, as the core's own dedup never discloses it."""
    r = conn.execute("SELECT command_id, kind, principal_id, principal_role, payload_hash, status, result, recorded_time_ns "
                     "FROM commands WHERE command_id=?", (command_id,)).fetchone()
    if r is None or (r["principal_id"], r["principal_role"]) != (principal_id, principal_role):
        return None
    return {"command_id": r["command_id"], "kind": r["kind"], "status": r["status"], "accepted": r["status"] == "ACCEPTED",
            "body": json.loads(r["result"]), "payload_hash": r["payload_hash"], "principal_id": r["principal_id"],
            "recorded_time_ns": r["recorded_time_ns"]}


def command_events(conn: sqlite3.Connection, command_id: str, *, principal_id: str) -> list[int]:
    """Sequence numbers of the events the stored execution of ``command_id`` recorded, ascending.

    A command runs in one write transaction of the single writer, so its events are consecutive. The events
    table has no command_id index: this scans back from the end of the log and stops where that block ends,
    which is cheap for the recent commands whose outcome a client needs to resolve. An authorization refusal
    under the same id is a separate, unstored decision (``stored: false``) and is never part of the block."""
    block: list[int] = []
    for e in conn.execute("SELECT seq, type, payload FROM events WHERE command_id=? AND principal_id=? ORDER BY seq DESC",
                          (command_id, principal_id)):
        if e["type"] == "CommandRejected" and json.loads(e["payload"])["info"].get("stored") is False:
            continue
        if block and e["seq"] != block[-1] - 1:
            break
        block.append(e["seq"])
    return block[::-1]


def reauth_window(conn: sqlite3.Connection, account_key: str) -> tuple[int | None, str | None]:
    """``(reauth_max_age_s, policy_hash)`` of the account's active policy, read exactly as the core reads it
    (``core.state.active_policy``); ``(None, None)`` when the account has no active policy."""
    from ..core import state
    try:
        policy = state.active_policy(Tx(conn), account_key)
    except Rejected:
        return None, None
    return policy.limits.reauth_max_age_s, policy.policy_hash


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [json.loads(r["row"]) for r in conn.execute(sql, params)]


def latest_seq(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()["s"]


def accounts(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn, "SELECT row FROM accounts ORDER BY rowid")


def buckets(conn: sqlite3.Connection, account_key: str) -> list[dict]:
    """Per-owner liquid balances and availability (ADR-0006). Buckets partition real balances; they never add."""
    tx = Tx(conn)
    per: dict[tuple[str, str], dict] = defaultdict(dict)
    for r in conn.execute("SELECT account_code, owner, currency, instrument_id, amount FROM balances WHERE account_key=?", (account_key,)):
        key = (r["owner"], r["currency"])
        code = r["account_code"]
        if code in ledger.LIQUID_CODES or code == ledger.INVENTORY:
            per[key][code] = (Fixed.parse(per[key].get(code, "0")) + Fixed.parse(r["amount"])).to_str()
    out = []
    for (owner, ccy), codes in sorted(per.items()):
        scale = currency(ccy).ledger_scale
        def g(c):
            return Fixed.parse(codes.get(c, "0"))
        liquid = g(ledger.CASH) + g(ledger.RECEIVABLE) + g(ledger.PAYABLE) + g(ledger.FEES_ACCRUED)
        out.append({"owner": owner, "currency": ccy, "settled_cash": g(ledger.CASH).rescale(scale).to_str(),
                    "receivable_unsettled": g(ledger.RECEIVABLE).rescale(scale).to_str(),
                    "payable_unsettled": g(ledger.PAYABLE).rescale(scale).to_str(),
                    "fees_accrued": g(ledger.FEES_ACCRUED).rescale(scale).to_str(),
                    "liquid_value": liquid.rescale(scale).to_str(),
                    "available_cash": ledger.available_cash(tx, account_key, owner, ccy).to_str(),
                    "holds": ledger.active_cash_holds(tx, account_key, owner, ccy).to_str(),
                    "inventory_cost": g(ledger.INVENTORY).rescale(scale).to_str(),
                    "system_owned": ledger.is_system_owner(owner)})
    return out


def positions(conn: sqlite3.Connection, account_key: str, quotes: dict) -> list[dict]:
    agg: dict[tuple[str, str], dict] = {}
    for lot in _rows(conn, "SELECT row FROM lots WHERE account_key=?", (account_key,)):
        qty = Fixed.parse(lot["open_qty"])
        if qty.is_zero():
            continue
        key = (lot["owner"], lot["instrument_id"])
        a = agg.setdefault(key, {"owner": lot["owner"], "instrument_id": lot["instrument_id"], "currency": lot["currency"],
                                 "quantity": Fixed(0), "cost": Fixed(0), "lots": 0})
        a["quantity"] = a["quantity"] + qty
        a["cost"] = a["cost"] + Fixed.parse(lot["open_cost"])
        a["lots"] += 1
    symbols = {r["instrument_id"]: r["symbol"] for r in conn.execute("SELECT instrument_id, symbol FROM instruments")}
    out = []
    for (owner, inst), a in sorted(agg.items()):
        q = quotes.get(inst)
        scale = currency(a["currency"]).ledger_scale
        row = {"owner": owner, "instrument_id": inst, "symbol": symbols.get(inst), "currency": a["currency"],
               "quantity": a["quantity"].to_str(), "cost_basis": a["cost"].to_str(), "lots": a["lots"], "mark": None,
               "market_value": None, "unrealized": None, "mark_basis": "unavailable: no quote"}
        if q and q.get("bid") is not None:
            mv = (a["quantity"] * Fixed.parse(q["bid"])).rescale(scale, Rounding.FLOOR)
            row.update(mark=q["bid"], market_value=mv.to_str(), unrealized=(mv - a["cost"]).to_str(),
                       mark_basis=f"bid from {q['source']} age {q['age_ms']} ms" + (" FIXTURE" if q.get("fixture") else ""),
                       mark_fixture=bool(q.get("fixture")))
        out.append(row)
    return out


def sessions(conn: sqlite3.Connection, account_key: str) -> list[dict]:
    out = []
    for s in _rows(conn, "SELECT row FROM sessions WHERE account_key=? ORDER BY rowid", (account_key,)):
        m = conn.execute("SELECT body FROM mandates WHERE mandate_hash=?", (s["mandate_hash"],)).fetchone()
        s["mandate"] = json.loads(m["body"]) if m else None
        out.append(s)
    return out


def session_detail(conn: sqlite3.Connection, session_id: str) -> dict | None:
    r = conn.execute("SELECT row FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if r is None:
        return None
    s = json.loads(r["row"])
    m = conn.execute("SELECT body FROM mandates WHERE mandate_hash=?", (s["mandate_hash"],)).fetchone()
    s["mandate"] = json.loads(m["body"]) if m else None
    owners = [ledger.session_owner(session_id), ledger.protected_owner(session_id)]
    s["balances"] = [dict(x) for x in conn.execute(
        "SELECT account_code, owner, currency, instrument_id, amount FROM balances WHERE owner IN (?,?) ORDER BY owner, account_code",
        tuple(owners))]
    s["orders"] = _rows(conn, "SELECT row FROM orders WHERE session_id=? ORDER BY accepted_at_ns", (session_id,))
    s["executions"] = _rows(conn, "SELECT row FROM executions WHERE json_extract(row,'$.session_id')=? ORDER BY rowid", (session_id,))
    s["settlements"] = [x for x in _rows(conn, "SELECT row FROM settlements WHERE owner IN (?,?)", tuple(owners))]
    s["timeline"] = events(conn, session_id=session_id, limit=500)
    succ = conn.execute("SELECT successor_id FROM successors WHERE predecessor_id=?", (session_id,)).fetchone()
    s["successor_id"] = succ["successor_id"] if succ else None
    return s


def orders(conn: sqlite3.Connection, account_key: str, *, open_only: bool = False, session_id: str | None = None,
           limit: int = 500) -> list[dict]:
    sql, params = "SELECT row FROM orders WHERE account_key=?", [account_key]
    if session_id:
        sql += " AND session_id=?"
        params.append(session_id)
    if open_only:
        sql += " AND state IN ('RESERVED','SUBMIT_PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_PENDING','UNKNOWN')"
    sql += " ORDER BY accepted_at_ns DESC LIMIT ?"
    params.append(limit)
    out = _rows(conn, sql, tuple(params))
    for o in out:
        o["holds"] = [dict(h) for h in conn.execute("SELECT kind, unit, amount, status FROM holds WHERE order_id=?", (o["order_id"],))]
    return out


def order_detail(conn: sqlite3.Connection, order_id: str) -> dict | None:
    r = conn.execute("SELECT row FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if r is None:
        return None
    o = json.loads(r["row"])
    o["holds"] = [dict(h) for h in conn.execute("SELECT * FROM holds WHERE order_id=?", (order_id,))]
    o["outbox"] = _rows(conn, "SELECT row FROM outbox WHERE order_id=? ORDER BY created_seq", (order_id,))
    o["executions"] = _rows(conn, "SELECT row FROM executions WHERE order_id=? ORDER BY rowid", (order_id,))
    o["timeline"] = events(conn, order_id=order_id, limit=500)
    return o


def executions(conn: sqlite3.Connection, account_key: str, limit: int = 500) -> list[dict]:
    return _rows(conn, "SELECT row FROM executions WHERE account_key=? ORDER BY rowid DESC LIMIT ?", (account_key, limit))


def exceptions(conn: sqlite3.Connection, account_key: str, *, open_only: bool = True) -> list[dict]:
    sql = "SELECT row FROM exceptions WHERE account_key=?" + (" AND status='OPEN'" if open_only else "") + " ORDER BY rowid DESC"
    return _rows(conn, sql, (account_key,))


def reconciliations(conn: sqlite3.Connection, account_key: str, limit: int = 50) -> list[dict]:
    return _rows(conn, "SELECT row FROM reconciliations WHERE account_key=? ORDER BY completed_at_ns DESC LIMIT ?", (account_key, limit))


def journal(conn: sqlite3.Connection, account_key: str, *, owner: str | None = None, limit: int = 200,
            before_seq: int | None = None) -> list[dict]:
    sql = "SELECT * FROM journal_entries WHERE account_key=?"
    params: list = [account_key]
    if before_seq:
        sql += " AND event_seq<?"
        params.append(before_seq)
    if owner:
        sql += " AND entry_id IN (SELECT entry_id FROM postings WHERE owner=?)"
        params.append(owner)
    sql += " ORDER BY event_seq DESC LIMIT ?"
    params.append(limit)
    out = []
    for e in conn.execute(sql, tuple(params)):
        d = dict(e)
        d["lines"] = [dict(p) for p in conn.execute("SELECT line, account_code, owner, currency, instrument_id, amount FROM postings WHERE entry_id=? ORDER BY line", (e["entry_id"],))]
        out.append(d)
    return out


def events(conn: sqlite3.Connection, *, after: int = 0, limit: int = 200, type_: str | None = None,
           session_id: str | None = None, order_id: str | None = None, account_key: str | None = None) -> list[dict]:
    sql = "SELECT seq, event_id, type, schema_version, command_id, correlation_id, principal_id, account_key, session_id, order_id, instrument_id, event_time_ns, receipt_time_ns, recorded_time_ns, writer_epoch, payload, payload_hash, chain_hash FROM events WHERE seq>?"
    params: list = [after]
    for col, val in (("type", type_), ("session_id", session_id), ("order_id", order_id), ("account_key", account_key)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    sql += " ORDER BY seq LIMIT ?"
    params.append(min(limit, 2000))
    out = []
    for r in conn.execute(sql, tuple(params)):
        d = dict(r)
        payload = json.loads(d.pop("payload"))
        d["info"] = payload["info"]
        d["changed_sections"] = sorted(payload["changes"].keys())
        out.append(d)
    return out


def control(conn: sqlite3.Connection, account_key: str) -> dict | None:
    r = conn.execute("SELECT row FROM control WHERE account_key=?", (account_key,)).fetchone()
    return json.loads(r["row"]) if r else None


def risk(conn: sqlite3.Connection, account_key: str) -> dict | None:
    r = conn.execute("SELECT row FROM risk_state WHERE account_key=?", (account_key,)).fetchone()
    return json.loads(r["row"]) if r else None


def policies(conn: sqlite3.Connection) -> list[dict]:
    return [{"policy_hash": r["policy_hash"], "name": r["name"], "published_by": r["published_by"],
             "published_at_ns": r["published_at_ns"], "body": json.loads(r["body"])}
            for r in conn.execute("SELECT * FROM policies ORDER BY published_at_ns")]


def active_policy(conn: sqlite3.Connection, account_key: str) -> dict | None:
    r = conn.execute("SELECT p.policy_hash, p.name, p.body, a.row FROM policy_active a JOIN policies p ON p.policy_hash=a.policy_hash WHERE a.account_key=?",
                     (account_key,)).fetchone()
    return {"policy_hash": r["policy_hash"], "name": r["name"], "body": json.loads(r["body"]), "activation": json.loads(r["row"])} if r else None


def instruments(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT body FROM instruments ORDER BY symbol"):
        i = json.loads(r["body"])
        i["mappings"] = _rows(conn, "SELECT row FROM provider_mappings WHERE instrument_id=? ORDER BY provider, version", (i["instrument_id"],))
        out.append(i)
    return out


def ownership(conn: sqlite3.Connection, account_key: str) -> dict | None:
    r = conn.execute("SELECT row FROM execution_ownership WHERE account_key=?", (account_key,)).fetchone()
    return json.loads(r["row"]) if r else None


def overview(conn: sqlite3.Connection, account_key: str, quotes: dict) -> dict:
    acct = conn.execute("SELECT row FROM accounts WHERE account_key=?", (account_key,)).fetchone()
    open_orders = orders(conn, account_key, open_only=True)
    return {
        "account": json.loads(acct["row"]) if acct else None,
        "control": control(conn, account_key),
        "risk": risk(conn, account_key),
        "ownership": ownership(conn, account_key),
        "active_policy": (active_policy(conn, account_key) or {}).get("policy_hash"),
        "buckets": buckets(conn, account_key),
        "positions": positions(conn, account_key, quotes),
        "sessions": [{k: s.get(k) for k in ("session_id", "state", "outcome", "seed", "target", "currency", "funding",
                                           "blockers", "predecessor_id", "protected_value")} for s in sessions(conn, account_key)],
        "open_orders": len(open_orders),
        "unknown_orders": sum(1 for o in open_orders if o["state"] == "UNKNOWN"),
        "exceptions_open": exceptions(conn, account_key),
        "last_reconciliation": (reconciliations(conn, account_key, 1) or [None])[0],
        "latest_seq": latest_seq(conn),
    }
