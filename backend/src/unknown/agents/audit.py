"""Append-only, hash-chained audit trail for agent jobs (CAP-AGT-09; ADR-0016).

Every runner record (reservation, settlement, generation, gate decision, tool call, schema
failure, discard, ...) and every worker action (start, recovery, catalog refresh, delivery) is
appended here as it happens, at ``<UNKNOWN_HOME>/agents/audit.sqlite``. The chain follows the
core event log (``core/replay.py``): each row stores the SHA-256 of its canonical body and

    chain_hash = SHA-256(prev_hash || canonical_json({seq, job_id, kind, actor, at_ns, recorded_ns, body_hash}))

starting from 64 zeros, so editing, deleting, reordering or re-attributing any row breaks
verification of every later row. SQLite triggers refuse UPDATE and DELETE. Like the core chain
this is a diagnostic, not a security boundary: someone who can rewrite the file can rebuild the
chain, and truncating the newest rows is detectable only against a head recorded elsewhere
(``head()``; the job store keeps each job's terminal state independently).

Bodies are canonical JSON. Records are sanitised first: floats become decimal text (never a JSON
float), tuples become lists, enums their value, and anything else its ``str()``.
"""
from __future__ import annotations

import enum
import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed
from ..domain.money import Money
from ..service.paths import Paths
from .common import connect_durable

GENESIS = "0" * 64
CHAINED_COLUMNS = ("seq", "job_id", "kind", "actor", "at_ns", "recorded_ns", "body_hash")
MAX_KIND = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit(
  seq INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  at_ns INTEGER NOT NULL,
  recorded_ns INTEGER NOT NULL,
  body TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  chain_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_job ON audit(job_id, seq);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
"""


class AuditError(RuntimeError):
    pass


def default_audit_path(home: Path | None = None) -> Path:
    root = Paths(home).root if home else Paths.default().root
    return root / "agents" / "audit.sqlite"


def chain_hash(prev_hash: str, fields: Mapping[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json({c: fields[c] for c in CHAINED_COLUMNS}))


def sanitize(value: Any) -> Any:
    """A JSON value that ``canonical_json`` accepts, never losing a number's exact text."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Fixed):
        return value.to_str()
    if isinstance(value, Money):
        return value.to_json()
    if isinstance(value, enum.Enum):
        return sanitize(value.value)
    if isinstance(value, Mapping):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [sanitize(v) for v in value]
        return sorted(items, key=canonical_json) if isinstance(value, (set, frozenset)) else items
    return str(value)


class AuditTrail:
    def __init__(self, path: Path | None = None, *, clock_ns: Callable[[], int], readonly: bool = False):
        self.path = Path(path) if path else default_audit_path()
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

    # ------------------------------------------------------------------ append
    def append(self, job_id: str, record: Mapping[str, Any], *, actor: str) -> int:
        """Append one record durably; returns its sequence number. ``record['kind']`` names it."""
        if self.readonly or self._db is None:
            raise AuditError("audit trail opened read-only")
        if not isinstance(record, Mapping):
            raise AuditError("an audit record must be a mapping")
        kind = record.get("kind")
        if not isinstance(kind, str) or not kind or len(kind) > MAX_KIND:
            raise AuditError("an audit record needs a short string 'kind'")
        if not isinstance(job_id, str) or not job_id or not isinstance(actor, str) or not actor:
            raise AuditError("job_id and actor are required")
        now = self._clock_ns()
        at = record.get("at_ns")
        at_ns = at if type(at) is int else now
        body = canonical_json(sanitize(record))
        body_hash = sha256_hex(body)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                last = self._db.execute("SELECT seq, chain_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
                seq = (last["seq"] + 1) if last else 1
                prev = last["chain_hash"] if last else GENESIS
                fields = {"seq": seq, "job_id": job_id, "kind": kind, "actor": actor, "at_ns": at_ns, "recorded_ns": now,
                          "body_hash": body_hash}
                self._db.execute("INSERT INTO audit(seq, job_id, kind, actor, at_ns, recorded_ns, body, body_hash, prev_hash,"
                                 " chain_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (seq, job_id, kind, actor, at_ns, now, body, body_hash, prev, chain_hash(prev, fields)))
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return seq

    # ------------------------------------------------------------------ queries
    def records(self, job_id: str) -> list[dict]:
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute("SELECT seq, kind, actor, at_ns, recorded_ns, body, chain_hash FROM audit"
                                    " WHERE job_id = ? ORDER BY seq", (job_id,)).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "actor": r["actor"], "at_ns": r["at_ns"], "recorded_ns": r["recorded_ns"],
                 "record": json.loads(r["body"]), "chain_hash": r["chain_hash"]} for r in rows]

    def head(self) -> tuple[int, str]:
        if self._db is None:
            return 0, GENESIS
        with self._lock:
            last = self._db.execute("SELECT seq, chain_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return (last["seq"], last["chain_hash"]) if last else (0, GENESIS)

    def verify(self) -> list[str]:
        """Recompute the whole chain; return problems (empty means intact)."""
        if self._db is None:
            return []
        problems: list[str] = []
        prev, expected_seq = GENESIS, 1
        cols = ", ".join(CHAINED_COLUMNS)
        with self._lock:
            rows = self._db.execute(f"SELECT {cols}, body, prev_hash, chain_hash FROM audit ORDER BY seq").fetchall()
        for r in rows:
            if r["seq"] != expected_seq:
                problems.append(f"audit seq {r['seq']}: expected {expected_seq} (row missing or inserted)")
            if sha256_hex(r["body"]) != r["body_hash"]:
                problems.append(f"audit seq {r['seq']}: body hash mismatch")
            if r["prev_hash"] != prev:
                problems.append(f"audit seq {r['seq']}: prev_hash mismatch")
            if r["chain_hash"] != chain_hash(prev, dict(r)):
                problems.append(f"audit seq {r['seq']}: chain hash mismatch")
            prev = r["chain_hash"]
            expected_seq = r["seq"] + 1
        return problems
