"""The runner's write-ahead journal: one row per (eligible session, instrument) decision (ADR-0005 applied to a
strategy runner).

A decision is written PLANNED, with the exact ``submit_intent`` and ``record_strategy_decision`` payloads and
their command ids, *before* anything is sent to the core. Command ids and the order id are derived from
(artifact, session date, instrument) only (``decision_ids``), so they are the same in every process that ever
makes this decision. After a crash the runner re-sends the stored payload under the stored id: the core's
command-id dedup returns the original result (``duplicate: true``) and the order is never submitted twice
(the deterministic order id would also be refused as ``DUPLICATE_ORDER_ID``).

States: PLANNED -> SUBMITTED (submit result stored; skipped in shadow mode or without an intent)
-> RECORDED (the core recorded the decision) or REFUSED (the core refused the record, e.g. an artifact that the
session mandate does not list). RECORDED and REFUSED are final for that session and instrument.

The journal lives beside the runtime state (``<UNKNOWN_HOME>/strategy/<account>/<artifact>.journal.sqlite``, 0700
dir; one journal per account and artifact). If it is lost, ``CoreView.commands`` is consulted before deciding again
(see ``runner.StrategyRunner``)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

FINAL = ("RECORDED", "REFUSED")
STATES = ("PLANNED", "SUBMITTED") + FINAL

SCHEMA = """
CREATE TABLE IF NOT EXISTS decision (
  decision_key TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, session_date TEXT NOT NULL, instrument_id TEXT NOT NULL,
  mode TEXT NOT NULL, state TEXT NOT NULL, submit_command_id TEXT NOT NULL, order_id TEXT NOT NULL,
  submit_payload TEXT, submit_result TEXT, decision_command_id TEXT NOT NULL, decision_payload TEXT NOT NULL,
  decision_result TEXT, detail TEXT NOT NULL, created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS decision_date ON decision(session_date);
"""


def decision_ids(artifact_id: str, session_date: date, instrument_id: str) -> dict[str, str]:
    """Deterministic ids of one decision. Readable prefix, hash suffix (instrument ids can be long)."""
    h = hashlib.sha256(f"{artifact_id}|{session_date.isoformat()}|{instrument_id}".encode()).hexdigest()
    day = session_date.strftime("%Y%m%d")
    return {"submit_command_id": f"strat.{artifact_id}.{day}.{h[:16]}.intent",
            "decision_command_id": f"strat.{artifact_id}.{day}.{h[:16]}.decision",
            "order_id": f"sr-{day}-{h[:16]}"}


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
        # A runner is used by one thread at a time, but not always the one that opened it (``run_loop`` in a
        # worker thread); SQLite itself is serialized, so the per-thread guard is lifted.
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def key(session_date: date, instrument_id: str) -> str:
        return f"{session_date.isoformat()}|{instrument_id}"

    def get(self, session_date: date, instrument_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM decision WHERE decision_key=?", (self.key(session_date, instrument_id),)).fetchone()
        return self._row(r) if r else None

    def rows(self, session_date: date | None = None) -> list[dict]:
        if session_date is None:
            return [self._row(r) for r in self.conn.execute("SELECT * FROM decision ORDER BY session_date, instrument_id")]
        return [self._row(r) for r in self.conn.execute("SELECT * FROM decision WHERE session_date=? ORDER BY instrument_id",
                                                       (session_date.isoformat(),))]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("submit_payload", "submit_result", "decision_payload", "decision_result", "detail"):
            d[k] = json.loads(d[k]) if d[k] is not None else None
        return d

    def plan(self, rows: list[dict], now_ns: int) -> None:
        """Write PLANNED rows atomically; a row that already exists is left as it is (first plan wins)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                self.conn.execute(
                    "INSERT OR IGNORE INTO decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.key(r["session_date"], r["instrument_id"]), r["artifact_id"], r["session_date"].isoformat(),
                     r["instrument_id"], r["mode"], r.get("state", "PLANNED"), r["submit_command_id"], r["order_id"],
                     _dump(r.get("submit_payload")), _dump(r.get("submit_result")), r["decision_command_id"],
                     _dump(r["decision_payload"]), _dump(r.get("decision_result")), _dump(r.get("detail") or {}), now_ns, now_ns))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def update(self, session_date: date, instrument_id: str, now_ns: int, **fields) -> None:
        allowed = {"state", "submit_result", "decision_payload", "decision_result", "detail"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"journal update fields must be in {sorted(allowed)}")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError(f"unknown journal state {fields['state']}")
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_ns=?"
        vals = [fields[k] if k == "state" else _dump(fields[k]) for k in fields] + [now_ns, self.key(session_date, instrument_id)]
        cur = self.conn.execute(f"UPDATE decision SET {sets} WHERE decision_key=?", vals)
        if cur.rowcount != 1:
            raise KeyError(f"no journal row for {session_date} {instrument_id}")


def _dump(v) -> str | None:
    return None if v is None else json.dumps(v, sort_keys=True, separators=(",", ":"))
