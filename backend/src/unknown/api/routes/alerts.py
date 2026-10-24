"""Alert rules, inbox, audit and live delivery (CAP-MKT-09, CAP-NEWS-13). The alert store is local
(``<UNKNOWN_HOME>/alerts/alerts.sqlite``); nothing here opens the core database for writing or touches orders.

  GET  /api/alerts/rules                       rules with status, snooze, evaluation state and trigger counts
  GET  /api/alerts/inbox?unread=1&limit=&before=
  GET  /api/alerts/audit?limit=&rule=
  GET  /api/alerts/status                      engine heartbeat, subscribed keys, notification availability
  GET  /api/alerts/stream                      SSE: status, alert, rules (alerts/stream.py)
  POST /api/alerts/rules                       {rule}: create (validated by alerts/rules.py)
  POST /api/alerts/rules/<id>/pause | resume | delete | snooze {minutes | until (ISO 8601) | clear: true}
  POST /api/alerts/inbox/ack                   {event_ids: [..]} or {all: true}
"""
from __future__ import annotations

import json
import time

from ...alerts import engine as alert_engine
from ...alerts import stream as alert_stream
from ...alerts.notify import MacNotifier
from ...alerts.rules import RuleError, parse_instant, validate
from ...alerts.store import AlertStore, store_path
from . import RouteConflict

MAX_SNOOZE_MIN = 7 * 24 * 60


def _int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = q.get(key)
    if v in (None, ""):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None
    if not lo <= n <= hi:
        raise ValueError(f"{key} must be in [{lo}, {hi}]")
    return n


def _store(ctx, *, readonly: bool) -> AlertStore | None:
    try:
        return AlertStore(store_path(ctx.paths.root), readonly=readonly)
    except FileNotFoundError:
        return None


def _status(ctx, store: AlertStore | None) -> dict:
    engine = store.meta("engine") if store else None
    beat = store.meta("heartbeat_ns") if store else None
    state, detail = MacNotifier(ctx.paths.root).status()
    return {"engine": json.loads(engine) if engine else None, "heartbeat_ns": int(beat) if beat else None,
            "engine_reason": None if engine else "NO_DATA",
            "engine_detail": None if engine else "the alert engine has not run yet (it starts with the API server)",
            "notifications": {"state": state, "detail": detail}, "now_ns": time.time_ns()}


def get_alerts(ctx, parts: list[str], q: dict, account_key: str, conn) -> dict:
    sub = parts[1:]
    store = _store(ctx, readonly=True)
    try:
        if sub == ["rules"]:
            return {"rules": store.rules() if store else [], "version": store.rules_version() if store else 0}
        if sub == ["inbox"]:
            if store is None:
                return {"events": [], "unread": 0}
            before = _int(q, "before", 0, 0, 2 ** 62) or None
            return {"events": store.events(before_id=before, unread_only=q.get("unread") in ("1", "true"),
                                           limit=_int(q, "limit", 100, 1, 1000)), "unread": store.unread()}
        if sub == ["audit"]:
            return {"entries": store.audit_log(limit=_int(q, "limit", 200, 1, 5000), rule_id=q.get("rule") or None)
                    if store else []}
        if sub == ["status"]:
            return _status(ctx, store)
    finally:
        if store is not None:
            store.close()
    raise LookupError(f"unknown alerts endpoint /{'/'.join(parts)}")


def _snooze_until(body: dict) -> int | None:
    if body.get("clear") is True:
        return None
    if "minutes" in body:
        m = body["minutes"]
        if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= MAX_SNOOZE_MIN:
            raise ValueError(f"minutes must be an integer in [1, {MAX_SNOOZE_MIN}]")
        return time.time_ns() + m * 60 * 1_000_000_000
    if "until" in body:
        until = parse_instant(body["until"], "until")
        if until <= time.time_ns():
            raise ValueError("until must be in the future")
        return until
    raise ValueError("snooze takes minutes, until or clear: true")


def post_alerts(ctx, parts: list[str], body: dict, session) -> dict:
    sub = parts[1:]
    store = _store(ctx, readonly=False)
    try:
        if sub == ["rules"]:
            try:
                rule = validate(body.get("rule"))
            except RuleError as exc:
                raise ValueError(str(exc)) from None
            if len(store.rules()) >= 500:
                raise RouteConflict("at most 500 alert rules")
            created = store.create_rule(rule, actor="owner")
            alert_engine.ensure_started(ctx)
            return created
        if len(sub) == 3 and sub[0] == "rules":
            rid, action = sub[1], sub[2]
            if action == "pause":
                return store.set_status(rid, "paused", actor="owner", reason=body.get("reason"))
            if action == "resume":
                if store.rule(rid)["status"] == "deleted":
                    raise LookupError(f"alert rule {rid} is deleted")
                out = store.set_status(rid, "active", actor="owner")
                alert_engine.ensure_started(ctx)
                return out
            if action == "delete":
                return store.set_status(rid, "deleted", actor="owner")
            if action == "snooze":
                return store.snooze(rid, _snooze_until(body), actor="owner")
            raise LookupError(f"unknown rule action {action!r}")
        if sub == ["inbox", "ack"]:
            if body.get("all") is True:
                return {"acknowledged": store.acknowledge(None, actor="owner"), "unread": store.unread()}
            ids = body.get("event_ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or not all(
                    isinstance(i, int) and not isinstance(i, bool) for i in ids):
                raise ValueError("event_ids must be a list of 1..1000 integers (or all: true)")
            return {"acknowledged": store.acknowledge(ids, actor="owner"), "unread": store.unread()}
    finally:
        store.close()
    raise LookupError(f"unknown alerts endpoint /{'/'.join(parts)}")


def stream(ctx, q: dict):
    path = store_path(ctx.paths.root)
    alert_engine.ensure_started(ctx)

    def run(last_event_id: int | None, write, flush, stopping) -> None:
        alert_stream.serve(path, last_event_id, write, flush, stopping)
    return run


ROUTES = [(("alerts", sub), get_alerts) for sub in ("rules", "inbox", "audit", "status")]
POST_ROUTES = [(("alerts", "rules"), post_alerts), (("alerts", "inbox"), post_alerts)]
STREAM_ROUTES = [(("alerts", "stream"), stream)]
