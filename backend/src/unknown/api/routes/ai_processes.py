"""AI processes for the terminal (CAP-AGT-04, CAP-AGT-11, CAP-AGT-13): notes inbox, approval queue, cost dashboard.

The API process holds no model key and loads no runner, worker or adapter code (a test checks every module the
API loads). It reads the process store, the audit trail and the spend ledger read-only, and writes only the
process store: explainer requests (run by the model worker), holdings snapshots and owner decisions on queued
proposals. A decision is bound to the proposal's content hash and appended to the audit trail in the same
transaction; it never places an order, starts a session, moves money or writes to the core.

  GET  /api/ai/notes[?process=brief|summaries|explainer&symbol=AAPL&limit=100]
  GET  /api/ai/notes/<note_id>
  GET  /api/ai/runs[?process=&limit=]            GET /api/ai/runs/<run_id>   (with its audit records)
  GET  /api/ai/approvals[?state=PENDING|APPROVED|REJECTED|EXPIRED]
  GET  /api/ai/approvals/<approval_id>            (with its audit records)
  GET  /api/ai/dashboard[?days=14]
  GET  /api/ai/config                             (settings, defaults and which models the owner approved per role)
  POST /api/ai/explain                 {"symbol": "AAPL", "window_days": 5}
  POST /api/ai/holdings                {}  snapshot of held symbols and watchlist instrument ids (no quantities)
  POST /api/ai/approvals/<id>/decide   {"decision": "approve"|"reject", "content_hash": "<sha256>", "reason": "..."}
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from ...agents.audit import AuditTrail, default_audit_path
from ...agents.common import CONFIG_MODELS
from ...agents.processes.config import (DEFAULTS, PROCESS_IDS, ROLES, TICKER_RE, ProcessConfigError, default_config_path,
                                        load_processes_config)
from ...agents.processes.dashboard import dashboard
from ...agents.processes.store import ApprovalRefused, ProcessStore, default_store_path, new_id
from .. import queries
from . import RouteConflict

_lock = threading.Lock()
_stores: dict[str, ProcessStore] = {}
NOW = time.time_ns          # wall clock; tests substitute a manual clock
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _now() -> int:
    return NOW()


def _store(ctx) -> ProcessStore:
    path = default_store_path(ctx.paths.root)
    with _lock:
        store = _stores.get(str(path))
        if store is None:
            store = _stores[str(path)] = ProcessStore(path, clock_ns=_now)
        return store


def _limit(q: dict, default: int = 100) -> int:
    try:
        n = int(q.get("limit", default))
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None
    if not 1 <= n <= 1000:
        raise ValueError("limit must be 1..1000")
    return n


def _audit_records(ctx, scope: str) -> dict:
    trail = AuditTrail(default_audit_path(ctx.paths.root), clock_ns=_now, readonly=True)
    try:
        return {"records": trail.records(scope), "chain": {"ok": not trail.verify()}}
    finally:
        trail.close()


def _approved_models() -> dict:
    try:
        doc = json.loads((CONFIG_MODELS / "approved.json").read_text())
    except (OSError, ValueError) as exc:
        return {"error": f"approved.json unreadable: {exc}"}
    out = {}
    for proc, role in ROLES.items():
        out[proc] = sorted(f"{a.get('provider')}/{a.get('model_id')}" for a in doc.get("approvals", [])
                           if role in (a.get("roles") or []))
    return out


def _config_view(ctx) -> dict:
    try:
        cfg = load_processes_config(default_config_path(ctx.paths.root))
        view, problem = cfg.view(), None
    except ProcessConfigError as exc:
        view, problem = None, str(exc)
    return {"config": view, "problem": problem, "defaults": DEFAULTS, "roles": ROLES, "approved_models": _approved_models(),
            "path": str(default_config_path(ctx.paths.root))}


def get_ai(ctx, parts: list[str], q: dict, account_key: str, conn) -> dict:
    store = _store(ctx)
    tail = parts[1:]
    if tail == ["notes"]:
        proc = q.get("process") or None
        if proc is not None and proc not in PROCESS_IDS:
            raise ValueError(f"process must be one of {PROCESS_IDS}")
        sym = (q.get("symbol") or "").strip().upper() or None
        return {"notes": store.notes(process=proc, symbol=sym, limit=_limit(q))}
    if len(tail) == 2 and tail[0] == "notes":
        note = store.note(tail[1])
        if note is None:
            raise LookupError(f"no note {tail[1]}")
        run = store.get_run(note["run_id"])
        return {"note": note, "run": run}
    if tail == ["runs"]:
        proc = q.get("process") or None
        return {"runs": store.runs(process=proc, limit=_limit(q, 200))}
    if len(tail) == 2 and tail[0] == "runs":
        run = store.get_run(tail[1])
        if run is None:
            raise LookupError(f"no run {tail[1]}")
        return {"run": run, "audit": _audit_records(ctx, tail[1])}
    if tail == ["approvals"]:
        return {"approvals": store.approvals(state=q.get("state") or None, limit=_limit(q, 200))}
    if len(tail) == 2 and tail[0] == "approvals":
        a = store.approval(tail[1])
        if a is None:
            raise LookupError(f"no proposal {tail[1]}")
        return {"approval": a, "audit": _audit_records(ctx, tail[1])}
    if tail == ["dashboard"]:
        days = int(q.get("days", 14))
        if not 1 <= days <= 90:
            raise ValueError("days must be 1..90")
        return dashboard(ctx.paths.root, now_ns=_now(), days=days)
    if tail == ["config"]:
        return _config_view(ctx)
    raise LookupError("unknown AI endpoint")


def _instrument_for(ctx, symbol: str) -> str | None:
    if not ctx.db_path.exists():
        return None
    conn = ctx.ro()
    try:
        row = conn.execute("SELECT instrument_id FROM instruments WHERE symbol = ? ORDER BY instrument_id LIMIT 1",
                           (symbol,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _holdings(ctx, store: ProcessStore) -> dict:
    """Held symbols and the instrument ids of watchlist symbols, from the core's read-only connection."""
    if not ctx.db_path.exists():
        raise RouteConflict("no core database to read holdings from")
    try:
        watch = load_processes_config(default_config_path(ctx.paths.root)).watchlist
    except ProcessConfigError:
        watch = ()
    conn = ctx.ro()
    try:
        held = queries.positions(conn, ctx.account_key, {})
        symbols = {r["instrument_id"]: r["symbol"] for r in conn.execute("SELECT instrument_id, symbol FROM instruments")}
    finally:
        conn.close()
    entries: dict[str, dict] = {}
    for p in held:
        sym = p.get("symbol")
        if sym:
            entries[sym] = {"symbol": sym, "instrument_id": p["instrument_id"], "held": True}
    for iid, sym in sorted(symbols.items()):
        if sym in watch and sym not in entries:
            entries[sym] = {"symbol": sym, "instrument_id": iid, "held": False}
    snap = store.save_subjects(sorted(entries.values(), key=lambda e: e["symbol"]), source=f"api:{ctx.account_key}")
    return {"snapshot_id": snap, "held": sorted(s for s, e in entries.items() if e["held"]),
            "watchlist_mapped": sorted(s for s, e in entries.items() if not e["held"])}


def _explain(ctx, store: ProcessStore, body: dict) -> dict:
    if set(body) - {"symbol", "window_days"}:
        raise ValueError("explain accepts only symbol and window_days")
    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not TICKER_RE.match(symbol.strip().upper()):
        raise ValueError("symbol must be a ticker such as AAPL")
    symbol = symbol.strip().upper()
    days = body.get("window_days")
    if days is not None and (type(days) is not int or not 1 <= days <= 30):
        raise ValueError("window_days must be an integer 1..30")
    try:
        cfg = load_processes_config(default_config_path(ctx.paths.root))
    except ProcessConfigError as exc:
        raise RouteConflict(f"AI processes are off: {exc}") from None
    if not cfg.get("explainer").enabled:
        raise RouteConflict(f"the explainer is switched off in {cfg.path} (explainer.enabled)")
    req = {"symbol": symbol, "window_days": days, "instrument_id": _instrument_for(ctx, symbol)}
    run, _ = store.enqueue("explainer", trigger="request", trigger_key=f"explain:{new_id('req')}", request=req,
                           requested_by="owner")
    return {"run_id": run["run_id"], "state": run["state"], "request": run["request"]}


def _decide(ctx, store: ProcessStore, approval_id: str, body: dict, session) -> dict:
    if set(body) - {"decision", "content_hash", "reason"}:
        raise ValueError("decide accepts only decision, content_hash and reason")
    decision, seen, reason = body.get("decision"), body.get("content_hash"), body.get("reason", "")
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    if not isinstance(seen, str) or not HASH_RE.match(seen):
        raise ValueError("content_hash must be the 64-character hash shown with the proposal")
    sid = getattr(session, "session_id", "") or ""
    trail = AuditTrail(default_audit_path(ctx.paths.root), clock_ns=_now)

    def audit(record: dict) -> int:
        rec = {**record, "auth": {"method": "owner_session", "session": hashlib.sha256(sid.encode()).hexdigest()[:16],
                                  "authenticated_at_ns": getattr(session, "authenticated_at_ns", None)}}
        return trail.append(approval_id, rec, actor="owner")
    try:
        try:
            row = store.decide(approval_id, decision=decision, content_hash_seen=seen, by="owner", reason=reason, audit=audit)
        except ApprovalRefused as exc:
            trail.append(approval_id, {"kind": "approval_refused", "code": exc.code, "decision": decision,
                                       "content_hash_seen": seen}, actor="owner")
            if exc.code == "unknown_proposal":
                raise LookupError(str(exc)) from None
            raise RouteConflict(str(exc)) from None
    finally:
        trail.close()
    return {"approval": row, "effect": "decision recorded only; nothing was executed, ordered or sent to the core"}


def post_ai(ctx, parts: list[str], body: dict, session) -> dict:
    store = _store(ctx)
    tail = parts[1:]
    if tail == ["explain"]:
        return _explain(ctx, store, body)
    if tail == ["holdings"]:
        if body:
            raise ValueError("holdings takes an empty body")
        return _holdings(ctx, store)
    if len(tail) == 3 and tail[0] == "approvals" and tail[2] == "decide":
        return _decide(ctx, store, tail[1], body, session)
    raise LookupError("unknown AI endpoint")


ROUTES = [(("ai",), get_ai)]
POST_ROUTES = [(("ai",), post_ai)]
