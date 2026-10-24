"""Local alert store: rules, their evaluation state, fired alerts (the inbox) and an append-only audit trail.

SQLite at ``<UNKNOWN_HOME>/alerts/alerts.sqlite`` (outside the repository, never the core database). Each API request
and the engine open their own connection; WAL and a busy timeout serialise writers.

Rule status: ``active`` (evaluated), ``paused`` (owner paused), ``triggered`` (a ``repeat: once`` rule that fired),
``expired`` (past ``expires_at``), ``deleted`` (kept for the audit trail, never evaluated). Snooze is separate: a
snoozed rule is still evaluated, but a trigger inside the snooze window is recorded in the audit as suppressed and
neither enters the inbox nor notifies.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_rule (
  rule_id TEXT PRIMARY KEY, created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL, status TEXT NOT NULL,
  snooze_until_ns INTEGER, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}', eval_status TEXT NOT NULL DEFAULT '{}',
  triggers INTEGER NOT NULL DEFAULT 0, last_trigger_ns INTEGER
);
CREATE TABLE IF NOT EXISTS alert_event (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL, fired_ns INTEGER NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, delayed INTEGER NOT NULL, ack_ns INTEGER,
  notify_status TEXT NOT NULL DEFAULT 'pending', notify_detail TEXT
);
CREATE TABLE IF NOT EXISTS alert_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ns INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  rule_id TEXT, event_id INTEGER, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alert_event_unread ON alert_event(ack_ns, event_id);
"""
LIVE = ("active",)
STATUSES = ("active", "paused", "triggered", "expired", "deleted")


def store_path(root: Path) -> Path:
    return Path(root) / "alerts" / "alerts.sqlite"


class AlertStore:
    def __init__(self, path: str | Path, *, readonly: bool = False, clock_ns=time.time_ns):
        self.path = Path(path)
        self.clock_ns = clock_ns
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(str(self.path))
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ meta
    def meta(self, k: str) -> str | None:
        try:
            r = self.conn.execute("SELECT v FROM alert_meta WHERE k=?", (k,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return r["v"] if r else None

    def set_meta(self, k: str, v: str) -> None:
        self.conn.execute("INSERT INTO alert_meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def rules_version(self) -> int:
        return int(self.meta("rules_version") or 0)

    def _bump(self) -> None:
        self.conn.execute("INSERT INTO alert_meta(k, v) VALUES ('rules_version', '1') ON CONFLICT(k) DO UPDATE SET "
                          "v=CAST(CAST(v AS INTEGER) + 1 AS TEXT)")

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, *, rule_id: str | None = None, event_id: int | None = None,
              detail: dict | None = None, ts_ns: int | None = None) -> None:
        self.conn.execute("INSERT INTO alert_audit(ts_ns, actor, action, rule_id, event_id, detail) VALUES (?,?,?,?,?,?)",
                          (ts_ns or self.clock_ns(), actor, action, rule_id, event_id,
                           json.dumps(detail or {}, sort_keys=True, default=str)))

    def audit_log(self, *, limit: int = 200, rule_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM alert_audit", []
        if rule_id:
            sql, args = sql + " WHERE rule_id=?", [rule_id]
        rows = self.conn.execute(sql + " ORDER BY audit_id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ------------------------------------------------------------------ rules
    @staticmethod
    def _rule(r) -> dict:
        return {"rule_id": r["rule_id"], "created_ns": r["created_ns"], "updated_ns": r["updated_ns"],
                "status": r["status"], "snooze_until_ns": r["snooze_until_ns"], "rule": json.loads(r["body"]),
                "state": json.loads(r["state"]), "evaluation": json.loads(r["eval_status"]),
                "triggers": r["triggers"], "last_trigger_ns": r["last_trigger_ns"]}

    def create_rule(self, rule: dict, *, actor: str) -> dict:
        now = self.clock_ns()
        rid = "alr-" + uuid.uuid4().hex[:16]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT INTO alert_rule(rule_id, created_ns, updated_ns, status, body) VALUES (?,?,?,?,?)",
                              (rid, now, now, "active", json.dumps(rule, sort_keys=True)))
            self.audit(actor, "create", rule_id=rid, detail={"rule": rule}, ts_ns=now)
            self._bump()
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.rule(rid)

    def rule(self, rule_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM alert_rule WHERE rule_id=?", (rule_id,)).fetchone()
        if r is None:
            raise LookupError(f"no alert rule {rule_id!r}")
        return self._rule(r)

    def rules(self, *, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM alert_rule" + ("" if include_deleted else " WHERE status != 'deleted'")
        return [self._rule(r) for r in self.conn.execute(sql + " ORDER BY created_ns DESC").fetchall()]

    def live_rules(self) -> list[dict]:
        return [self._rule(r) for r in self.conn.execute(
            "SELECT * FROM alert_rule WHERE status='active' ORDER BY created_ns").fetchall()]

    def set_status(self, rule_id: str, status: str, *, actor: str, reason: str | None = None) -> dict:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        cur = self.rule(rule_id)
        if cur["status"] == "deleted":
            raise LookupError(f"alert rule {rule_id} is deleted")
        now = self.clock_ns()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # resuming starts from a clean edge state so a level crossed while paused is not reported late
            state_sql = ", state='{}'" if status == "active" and cur["status"] != "active" else ""
            self.conn.execute(f"UPDATE alert_rule SET status=?, updated_ns=?{state_sql} WHERE rule_id=?",
                              (status, now, rule_id))
            self.audit(actor, status if status != "active" else "resume", rule_id=rule_id,
                       detail={"from": cur["status"], "reason": reason}, ts_ns=now)
            self._bump()
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.rule(rule_id)

    def snooze(self, rule_id: str, until_ns: int | None, *, actor: str) -> dict:
        cur = self.rule(rule_id)
        if cur["status"] == "deleted":
            raise LookupError(f"alert rule {rule_id} is deleted")
        now = self.clock_ns()
        self.conn.execute("UPDATE alert_rule SET snooze_until_ns=?, updated_ns=? WHERE rule_id=?", (until_ns, now, rule_id))
        self.audit(actor, "snooze" if until_ns else "unsnooze", rule_id=rule_id, detail={"until_ns": until_ns}, ts_ns=now)
        self._bump()
        return self.rule(rule_id)

    def save_state(self, rule_id: str, state: dict, evaluation: dict) -> None:
        self.conn.execute("UPDATE alert_rule SET state=?, eval_status=? WHERE rule_id=?",
                          (json.dumps(state, sort_keys=True, default=str), json.dumps(evaluation, sort_keys=True), rule_id))

    # ------------------------------------------------------------------ events
    def fire(self, rule: dict, trigger: dict, *, title: str, now_ns: int, retire: bool) -> int:
        """Record a trigger in the inbox (and retire a ``once`` rule) atomically; returns the event id."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO alert_event(rule_id, fired_ns, kind, title, body, delayed) VALUES (?,?,?,?,?,?)",
                (rule["rule_id"], now_ns, rule["rule"]["kind"], title,
                 json.dumps({"rule": rule["rule"], "trigger": trigger}, sort_keys=True, default=str),
                 int(bool(trigger.get("delayed")))))
            eid = cur.lastrowid
            self.conn.execute("UPDATE alert_rule SET triggers=triggers+1, last_trigger_ns=?" +
                              (", status='triggered'" if retire else "") + " WHERE rule_id=?", (now_ns, rule["rule_id"]))
            self.audit("engine", "trigger", rule_id=rule["rule_id"], event_id=eid,
                       detail={k: trigger.get(k) for k in ("observed", "threshold", "source", "data_type", "delayed")},
                       ts_ns=now_ns)
            if retire:
                self.audit("engine", "triggered", rule_id=rule["rule_id"], detail={"repeat": "once"}, ts_ns=now_ns)
                self._bump()
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return eid

    def set_delivery(self, event_id: int, status: str, detail: str | None) -> None:
        self.conn.execute("UPDATE alert_event SET notify_status=?, notify_detail=? WHERE event_id=?",
                          (status, detail, event_id))
        self.audit("engine", "notify", event_id=event_id, detail={"status": status, "detail": detail})

    @staticmethod
    def _event(r) -> dict:
        body = json.loads(r["body"])
        return {"event_id": r["event_id"], "rule_id": r["rule_id"], "fired_ns": r["fired_ns"], "kind": r["kind"],
                "title": r["title"], "rule": body["rule"], "trigger": body["trigger"], "delayed": bool(r["delayed"]),
                "ack_ns": r["ack_ns"], "notify_status": r["notify_status"], "notify_detail": r["notify_detail"]}

    def events(self, *, after_id: int | None = None, before_id: int | None = None, unread_only: bool = False,
               limit: int = 100) -> list[dict]:
        where, args = [], []
        if after_id is not None:
            where.append("event_id > ?")
            args.append(after_id)
        if before_id is not None:
            where.append("event_id < ?")
            args.append(before_id)
        if unread_only:
            where.append("ack_ns IS NULL")
        order = "ASC" if after_id is not None else "DESC"
        sql = "SELECT * FROM alert_event" + (" WHERE " + " AND ".join(where) if where else "") + \
              f" ORDER BY event_id {order} LIMIT ?"
        return [self._event(r) for r in self.conn.execute(sql, (*args, limit)).fetchall()]

    def max_event_id(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM alert_event").fetchone()[0]

    def unread(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM alert_event WHERE ack_ns IS NULL").fetchone()[0]

    def acknowledge(self, event_ids: list[int] | None, *, actor: str) -> int:
        now = self.clock_ns()
        if event_ids is None:
            n = self.conn.execute("UPDATE alert_event SET ack_ns=? WHERE ack_ns IS NULL", (now,)).rowcount
        else:
            marks = ",".join("?" * len(event_ids))
            n = self.conn.execute(f"UPDATE alert_event SET ack_ns=? WHERE ack_ns IS NULL AND event_id IN ({marks})",
                                  (now, *event_ids)).rowcount
        self.audit(actor, "acknowledge", detail={"event_ids": event_ids if event_ids is not None else "all", "count": n},
                   ts_ns=now)
        return n
