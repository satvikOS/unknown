"""The AI process store: ``<UNKNOWN_HOME>/agents/processes.sqlite`` (WAL, synchronous=FULL, mode 0600).

Tables
  runs       one row per process run: trigger (schedule, event, request), a unique trigger key that makes every
             trigger run at most once (``brief:2026-10-23``, ``summary:news:138``, ``explain:<request id>``),
             state QUEUED -> RUNNING -> DONE | FAILED, outcome, reason, model id, tokens, cost and latency.
  notes      advisory notes: immutable (UPDATE and DELETE are refused by triggers), each with its content hash.
  approvals  the owner approval queue (CAP-AGT-04). A proposal's content is immutable; the decision is bound
             to the SHA-256 of its canonical content, recomputed at decision time, and the audit record is
             appended inside the decision transaction (no decision without an audit record). Expired
             proposals cannot be decided; a rejected proposal with the same content is not queued again for
             ``REJECT_COOLDOWN_NS``. A decision changes this row only: nothing reads it to place an order,
             start a session, move money or write to the core.
  subjects   holdings snapshots the terminal API takes from the core's read-only connection (symbols and
             instrument ids only, never quantities), so the worker never opens the core database.
  cursors    per-process high-water marks (for example the last news item id the summaries process saw).

Written by the model worker (runs, notes, proposals, cursors) and by the terminal API (explainer requests,
owner decisions, holdings snapshots). Both use short ``BEGIN IMMEDIATE`` transactions.
"""
from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from ...domain.canonical import canonical_json, content_hash
from ...service.paths import Paths
from ..common import connect_durable

NS = 1_000_000_000
REJECT_COOLDOWN_NS = 7 * 86_400 * NS
RUN_STATES = ("QUEUED", "RUNNING", "DONE", "FAILED")
APPROVAL_STATES = ("PENDING", "APPROVED", "REJECTED", "EXPIRED")
DECISIONS = {"approve": "APPROVED", "reject": "REJECTED"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, process TEXT NOT NULL, trigger TEXT NOT NULL, trigger_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('QUEUED','RUNNING','DONE','FAILED')), request TEXT NOT NULL DEFAULT '{}',
  requested_by TEXT NOT NULL, requested_ns INTEGER NOT NULL, started_ns INTEGER, finished_ns INTEGER,
  outcome TEXT, reason TEXT, note_id TEXT, model_id TEXT, prompt_tokens INTEGER, output_tokens INTEGER,
  cost TEXT, currency TEXT, worker_id TEXT
);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state, requested_ns);
CREATE TABLE IF NOT EXISTS notes(
  note_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, process TEXT NOT NULL, created_ns INTEGER NOT NULL,
  title TEXT NOT NULL, status TEXT NOT NULL, symbols TEXT NOT NULL, ciks TEXT NOT NULL, body TEXT NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_created ON notes(created_ns);
CREATE TRIGGER IF NOT EXISTS notes_no_update BEFORE UPDATE ON notes BEGIN SELECT RAISE(ABORT, 'notes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS notes_no_delete BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT, 'notes are immutable'); END;
CREATE TABLE IF NOT EXISTS approvals(
  approval_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL,
  content TEXT NOT NULL, content_hash TEXT NOT NULL, created_ns INTEGER NOT NULL, expires_ns INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
  decided_ns INTEGER, decided_by TEXT, decision_hash TEXT, decision_reason TEXT, audit_seq INTEGER,
  UNIQUE (source, source_id, content_hash)
);
CREATE TRIGGER IF NOT EXISTS approvals_content_immutable BEFORE UPDATE OF content, content_hash, kind, source, source_id
  ON approvals BEGIN SELECT RAISE(ABORT, 'a proposal is immutable; a change is a new proposal'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT, 'approvals are kept'); END;
CREATE TABLE IF NOT EXISTS subjects(
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, as_of_ns INTEGER NOT NULL, source TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class ProcessStoreError(RuntimeError):
    pass


class ApprovalRefused(ProcessStoreError):
    """A decision the queue refuses; ``code`` is stable for the terminal (payload_hash_mismatch, expired, ...)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def default_store_path(home: Path | None = None) -> Path:
    root = Paths(home).root if home else Paths.default().root
    return root / "agents" / "processes.sqlite"


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _loads(text: str | None, default: Any) -> Any:
    return json.loads(text) if text else default


class ProcessStore:
    def __init__(self, path: Path, *, clock_ns: Callable[[], int], readonly: bool = False):
        self.path = Path(path)
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self.readonly = readonly
        self._db = None
        if readonly:
            if self.path.exists():
                self._db = connect_durable(self.path, readonly=True)
        else:
            self._db = connect_durable(self.path)
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ------------------------------------------------------------------ plumbing
    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        if self._db is None:
            return []
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _tx(self, fn: Callable[[Any], Any]) -> Any:
        if self.readonly or self._db is None:
            raise ProcessStoreError("the process store is open read-only")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self._db)
                self._db.execute("COMMIT")
                return out
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ runs
    @staticmethod
    def _run(r: Mapping[str, Any] | None) -> dict | None:
        if r is None:
            return None
        out = dict(r)
        out["request"] = _loads(out["request"], {})
        return out

    def enqueue(self, process: str, *, trigger: str, trigger_key: str, request: Mapping[str, Any] | None = None,
                requested_by: str = "worker") -> tuple[dict, bool]:
        """Queue one run; the same trigger key returns the existing run instead (at most one run per trigger)."""
        now = self._clock_ns()

        def tx(db):
            row = db.execute("SELECT * FROM runs WHERE trigger_key = ?", (trigger_key,)).fetchone()
            if row is not None:
                return self._run(row), False
            rid = new_id(f"aip-{process}")
            db.execute("INSERT INTO runs(run_id, process, trigger, trigger_key, state, request, requested_by, requested_ns)"
                       " VALUES (?,?,?,?,'QUEUED',?,?,?)",
                       (rid, process, trigger, trigger_key, canonical_json(dict(request or {})), requested_by, now))
            return self._run(db.execute("SELECT * FROM runs WHERE run_id = ?", (rid,)).fetchone()), True
        return self._tx(tx)

    def get_run(self, run_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return self._run(rows[0]) if rows else None

    def run_for(self, trigger_key: str) -> dict | None:
        rows = self._rows("SELECT * FROM runs WHERE trigger_key = ?", (trigger_key,))
        return self._run(rows[0]) if rows else None

    def queued(self, process: str | None = None) -> list[dict]:
        sql = "SELECT * FROM runs WHERE state = 'QUEUED'" + (" AND process = ?" if process else "") + " ORDER BY requested_ns, rowid"
        return [self._run(r) for r in self._rows(sql, (process,) if process else ())]

    def running(self) -> list[dict]:
        return [self._run(r) for r in self._rows("SELECT * FROM runs WHERE state = 'RUNNING' ORDER BY started_ns")]

    def runs(self, *, process: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM runs" + (" WHERE process = ?" if process else "") + " ORDER BY requested_ns DESC, rowid DESC LIMIT ?"
        return [self._run(r) for r in self._rows(sql, ((process,) if process else ()) + (int(limit),))]

    def claim(self, run_id: str, worker_id: str) -> dict:
        now = self._clock_ns()

        def tx(db):
            n = db.execute("UPDATE runs SET state = 'RUNNING', started_ns = ?, worker_id = ? WHERE run_id = ? AND state = 'QUEUED'",
                           (now, worker_id, run_id)).rowcount
            if n != 1:
                raise ProcessStoreError(f"run {run_id} is not queued")
            return self._run(db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
        return self._tx(tx)

    def finish(self, run_id: str, *, state: str, outcome: str, reason: str, note: Mapping[str, Any] | None = None,
               stats: Mapping[str, Any] | None = None) -> dict:
        """End a run, storing its note (if any) in the same transaction."""
        if state not in ("DONE", "FAILED"):
            raise ProcessStoreError(f"invalid end state {state}")
        now = self._clock_ns()
        stats = dict(stats or {})

        def tx(db):
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None or row["state"] not in ("QUEUED", "RUNNING"):
                raise ProcessStoreError(f"run {run_id} cannot end from {row['state'] if row else 'absent'}")
            note_id = None
            if note is not None:
                note_id = self._insert_note(db, row["run_id"], row["process"], note, now)
            db.execute("UPDATE runs SET state = ?, finished_ns = ?, outcome = ?, reason = ?, note_id = ?, model_id = ?,"
                       " prompt_tokens = ?, output_tokens = ?, cost = ?, currency = ? WHERE run_id = ?",
                       (state, now, outcome, reason[:2000], note_id, stats.get("model_id"), stats.get("prompt_tokens"),
                        stats.get("output_tokens"), stats.get("cost"), stats.get("currency"), run_id))
            return self._run(db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
        return self._tx(tx)

    # ------------------------------------------------------------------ notes
    @staticmethod
    def _insert_note(db, run_id: str, process: str, note: Mapping[str, Any], now: int) -> str:
        body = dict(note["body"])
        h = content_hash(body)
        nid = new_id("note")
        db.execute("INSERT INTO notes(note_id, run_id, process, created_ns, title, status, symbols, ciks, body, content_hash)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (nid, run_id, process, now, str(note["title"])[:300], note["status"],
                    canonical_json(sorted(set(note.get("symbols") or ()))), canonical_json(sorted(set(note.get("ciks") or ()))),
                    canonical_json(body), h))
        return nid

    @staticmethod
    def _note(r: Mapping[str, Any], *, full: bool) -> dict:
        out = {k: r[k] for k in ("note_id", "run_id", "process", "created_ns", "title", "status", "content_hash")}
        out["symbols"] = _loads(r["symbols"], [])
        out["ciks"] = _loads(r["ciks"], [])
        body = _loads(r["body"], {})
        if full:
            out["body"] = body
        else:
            out["label"] = body.get("label")
            out["lines"] = len(body.get("lines") or [])
            out["flagged"] = sum(1 for ln in body.get("lines") or [] if ln.get("status") != "verified")
            out["headline"] = body.get("headline")
        return out

    def notes(self, *, process: str | None = None, symbol: str | None = None, limit: int = 100) -> list[dict]:
        where, args = [], []
        if process:
            where.append("process = ?")
            args.append(process)
        if symbol:
            where.append("(symbols LIKE ? OR ciks LIKE ?)")
            args += [f'%"{symbol}"%', f'%"{symbol}"%']
        sql = "SELECT * FROM notes" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_ns DESC, rowid DESC LIMIT ?"
        return [self._note(r, full=False) for r in self._rows(sql, (*args, int(limit)))]

    def note(self, note_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM notes WHERE note_id = ?", (note_id,))
        if not rows:
            return None
        out = self._note(rows[0], full=True)
        out["content_hash_ok"] = content_hash(out["body"]) == out["content_hash"]
        return out

    # ------------------------------------------------------------------ approvals
    @staticmethod
    def _approval(r: Mapping[str, Any] | None) -> dict | None:
        if r is None:
            return None
        out = dict(r)
        out["content"] = _loads(out["content"], {})
        return out

    def propose(self, *, kind: str, source: str, source_id: str, title: str, content: Mapping[str, Any],
                expires_ns: int) -> tuple[dict, str]:
        """Queue a proposal for the owner. Returns (row, how): ``queued``, ``existing`` (same source and content)
        or ``suppressed`` (the owner rejected this exact content within the cool-down)."""
        now = self._clock_ns()
        content = dict(content)
        h = content_hash(content)

        def tx(db):
            same = db.execute("SELECT * FROM approvals WHERE source = ? AND source_id = ? AND content_hash = ?",
                              (source, source_id, h)).fetchone()
            if same is not None:
                return self._approval(same), "existing"
            rejected = db.execute("SELECT * FROM approvals WHERE content_hash = ? AND state = 'REJECTED' AND decided_ns >= ?"
                                  " ORDER BY decided_ns DESC LIMIT 1", (h, now - REJECT_COOLDOWN_NS)).fetchone()
            if rejected is not None:
                return self._approval(rejected), "suppressed"
            aid = new_id("apq")
            db.execute("INSERT INTO approvals(approval_id, kind, source, source_id, title, content, content_hash, created_ns,"
                       " expires_ns, state) VALUES (?,?,?,?,?,?,?,?,?,'PENDING')",
                       (aid, kind, source, source_id, title[:300], canonical_json(content), h, now, expires_ns))
            return self._approval(db.execute("SELECT * FROM approvals WHERE approval_id = ?", (aid,)).fetchone()), "queued"
        return self._tx(tx)

    def has_source(self, source: str, source_id: str) -> bool:
        return bool(self._rows("SELECT 1 FROM approvals WHERE source = ? AND source_id = ? LIMIT 1", (source, source_id)))

    def approval(self, approval_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        return self._approval(rows[0]) if rows else None

    def approvals(self, *, state: str | None = None, limit: int = 200) -> list[dict]:
        if state is not None and state not in APPROVAL_STATES:
            raise ValueError(f"state must be one of {APPROVAL_STATES}")
        sql = "SELECT * FROM approvals" + (" WHERE state = ?" if state else "") + " ORDER BY created_ns DESC, rowid DESC LIMIT ?"
        return [self._approval(r) for r in self._rows(sql, ((state,) if state else ()) + (int(limit),))]

    def expire_due(self) -> list[str]:
        now = self._clock_ns()
        if not self._rows("SELECT 1 FROM approvals WHERE state = 'PENDING' AND expires_ns <= ? LIMIT 1", (now,)):
            return []           # the worker calls this every cycle: take the write lock only when something expires

        def tx(db):
            ids = [r[0] for r in db.execute("SELECT approval_id FROM approvals WHERE state = 'PENDING' AND expires_ns <= ?", (now,))]
            if ids:
                db.execute(f"UPDATE approvals SET state = 'EXPIRED', decided_ns = ?, decided_by = 'system:expiry'"
                           f" WHERE approval_id IN ({','.join('?' * len(ids))})", (now, *ids))
            return ids
        return self._tx(tx)

    def decide(self, approval_id: str, *, decision: str, content_hash_seen: str, by: str, reason: str,
               audit: Callable[[dict], int]) -> dict:
        """Record the owner's decision on the exact content they reviewed. Refused (ApprovalRefused) when the
        proposal is unknown, already decided, expired, or when ``content_hash_seen`` differs from the stored hash
        or from the hash recomputed from the stored content. ``audit`` appends the decision record to the
        hash-chained audit trail inside this transaction and returns its sequence number."""
        if decision not in DECISIONS:
            raise ApprovalRefused("invalid_decision", f"decision must be one of {sorted(DECISIONS)}")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ApprovalRefused("invalid_reason", "reason must be a string of at most 500 characters")
        now = self._clock_ns()

        def tx(db):
            row = db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None:
                raise ApprovalRefused("unknown_proposal", f"no proposal {approval_id}")
            if row["state"] != "PENDING":
                raise ApprovalRefused("already_decided", f"proposal {approval_id} is {row['state']}")
            if row["expires_ns"] <= now:
                db.execute("UPDATE approvals SET state = 'EXPIRED', decided_ns = ?, decided_by = 'system:expiry'"
                           " WHERE approval_id = ?", (now, approval_id))
                return ApprovalRefused("expired", f"proposal {approval_id} expired before the decision")
            recomputed = content_hash(json.loads(row["content"]))
            if content_hash_seen != row["content_hash"] or recomputed != row["content_hash"]:
                raise ApprovalRefused("payload_hash_mismatch",
                                      "the decision does not match the stored proposal content; reload and review it again")
            state = DECISIONS[decision]
            seq = audit({"kind": "approval_decided", "approval_id": approval_id, "decision": state, "content_hash": recomputed,
                         "proposal_kind": row["kind"], "source": row["source"], "source_id": row["source_id"],
                         "by": by, "reason": reason, "at_ns": now,
                         "effect": "decision recorded only; nothing is executed, ordered or sent to the core"})
            db.execute("UPDATE approvals SET state = ?, decided_ns = ?, decided_by = ?, decision_hash = ?, decision_reason = ?,"
                       " audit_seq = ? WHERE approval_id = ? AND state = 'PENDING'",
                       (state, now, by, recomputed, reason, seq, approval_id))
            return self._approval(db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone())
        out = self._tx(tx)
        if isinstance(out, ApprovalRefused):   # the expiry was committed; the decision is refused
            raise out
        return out

    # ------------------------------------------------------------------ subjects and cursors
    def save_subjects(self, entries: list[Mapping[str, Any]], *, source: str) -> int:
        now = self._clock_ns()
        body = canonical_json([{"symbol": e.get("symbol"), "instrument_id": e.get("instrument_id"), "held": bool(e.get("held"))}
                               for e in entries])

        def tx(db):
            last = db.execute("SELECT snapshot_id, body FROM subjects ORDER BY snapshot_id DESC LIMIT 1").fetchone()
            if last is not None and last["body"] == body:
                db.execute("UPDATE subjects SET as_of_ns = ? WHERE snapshot_id = ?", (now, last["snapshot_id"]))
                return last["snapshot_id"]
            return db.execute("INSERT INTO subjects(as_of_ns, source, body) VALUES (?,?,?)", (now, source, body)).lastrowid
        return self._tx(tx)

    def latest_subjects(self) -> dict | None:
        rows = self._rows("SELECT * FROM subjects ORDER BY snapshot_id DESC LIMIT 1")
        if not rows:
            return None
        return {"snapshot_id": rows[0]["snapshot_id"], "as_of_ns": rows[0]["as_of_ns"], "source": rows[0]["source"],
                "entries": _loads(rows[0]["body"], [])}

    def cursor(self, name: str) -> str | None:
        rows = self._rows("SELECT value FROM cursors WHERE name = ?", (name,))
        return rows[0]["value"] if rows else None

    def set_cursor(self, name: str, value: str) -> None:
        self._tx(lambda db: db.execute("INSERT INTO cursors(name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET"
                                       " value = excluded.value", (name, value)))
