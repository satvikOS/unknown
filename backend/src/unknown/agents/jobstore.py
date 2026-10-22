"""Durable research job store (ADR-0016 section 2; CAP-AGT-01).

SQLite at ``<UNKNOWN_HOME>/agents/jobs.sqlite`` with the core store's durability settings
(WAL, ``synchronous=FULL``, ``fullfsync`` on macOS). It replaces the in-memory ``JobRegistry``
for jobs that must survive a restart. Writers: the terminal API (submit, cancel) and the
model worker (claim, finish, expiry, delivery); every write is one ``BEGIN IMMEDIATE``
transaction, so the two processes never interleave a check with its update.

Job states::

    QUEUED --claim--> RUNNING --finish--> SUCCEEDED | FAILED | CANCELLED | EXPIRED
    QUEUED --cancel--> CANCELLED      QUEUED --deadline--> EXPIRED      QUEUED --refuse--> FAILED
    RUNNING --worker restart--> QUEUED (nothing was sent) or FAILED "worker restarted"

``outcome`` keeps the runner's fine-grained status (``budget_exhausted``, ``schema_invalid``,
``denied_by_policy``, ...) or a worker code (``worker_restarted``, ``worker_error``).

Submission is idempotent: the same job id with the same envelope and inputs returns the
existing job, and an equivalent job (CAP-AGT-01 dedupe key: role, schema, prompt template and
input hashes) that is queued, running or succeeded within the dedupe window is returned instead
of a new one. A failed, cancelled or expired job does not block a resubmission.

Proposal outbox: a finished job's proposal (or abstention) is stored in the same transaction as
its terminal state, with delivery ``PENDING``. Delivery to the core is retried until the core
answers; the payload is stored once and resent byte-identical under the same command id, so the
core's command-id deduplication records it once even if the worker dies after the core accepted
it. Delivery states: NONE (nothing to deliver), PENDING, DELIVERED, REJECTED (the core refused
it; kept, not retried) and CONFLICT (the command id already holds a different payload; kept).
Nothing in this module deletes a job or a proposal.
"""
from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..domain.canonical import canonical_json, content_hash
from ..service.paths import Paths
from .common import connect_durable
from .envelope import JobEnvelope, JobState

STATES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED")
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"})
DEDUPE_LIVE = ("QUEUED", "RUNNING", "SUCCEEDED")
DELIVERY_STATES = ("NONE", "PENDING", "DELIVERED", "REJECTED", "CONFLICT")
DEFAULT_DEDUPE_WINDOW_NS = 24 * 3600 * 1_000_000_000
MAX_START_ATTEMPTS = 3
MAX_TEXT = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY,
  dedupe_key TEXT NOT NULL,
  role TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED','EXPIRED')),
  outcome TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  envelope TEXT NOT NULL,
  inputs TEXT NOT NULL,
  submitted_by TEXT NOT NULL,
  submitted_ns INTEGER NOT NULL,
  deadline_ns INTEGER NOT NULL,
  updated_ns INTEGER NOT NULL,
  started_ns INTEGER,
  finished_ns INTEGER,
  start_attempts INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  proposal TEXT,
  output_hash TEXT,
  discarded_output_hash TEXT,
  delivery TEXT NOT NULL DEFAULT 'NONE' CHECK (delivery IN ('NONE','PENDING','DELIVERED','REJECTED','CONFLICT')),
  delivery_attempts INTEGER NOT NULL DEFAULT 0,
  delivery_error TEXT NOT NULL DEFAULT '',
  next_delivery_ns INTEGER,
  core_result TEXT,
  delivered_ns INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, submitted_ns);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key, submitted_ns);
CREATE INDEX IF NOT EXISTS jobs_delivery ON jobs(delivery, next_delivery_ns);
CREATE TABLE IF NOT EXISTS job_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  at_ns INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, seq);
"""

_JSON_COLUMNS = ("envelope", "inputs", "proposal", "core_result")
_SUMMARY = ("job_id", "role", "state", "outcome", "reason", "submitted_by", "submitted_ns", "deadline_ns", "started_ns",
            "finished_ns", "start_attempts", "cancel_requested", "delivery", "delivery_attempts", "delivery_error",
            "output_hash", "delivered_ns")


class JobStoreError(RuntimeError):
    pass


class StateConflict(JobStoreError):
    """The job is not in a state that allows the requested transition."""


@dataclass(frozen=True)
class SubmitResult:
    job_id: str
    created: bool
    duplicate_of: str | None
    state: str


def default_jobs_path(home: Path | None = None) -> Path:
    root = Paths(home).root if home else Paths.default().root
    return root / "agents" / "jobs.sqlite"


def new_job_id() -> str:
    return "rj-" + secrets.token_hex(10)


def store_state_for(status: JobState) -> str:
    """Map a runner terminal status to the store's coarse state (the fine status is kept as ``outcome``)."""
    if not status.terminal:
        raise JobStoreError(f"{status.value} is not terminal")
    if status is JobState.SUCCEEDED:
        return "SUCCEEDED"
    if status is JobState.CANCELLED_BY_OWNER:
        return "CANCELLED"
    if status is JobState.DEADLINE_EXPIRED:
        return "EXPIRED"
    return "FAILED"


def _cut(text: str) -> str:
    text = str(text)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 12] + " [truncated]"


class JobStore:
    def __init__(self, path: Path | None = None, *, clock_ns: Callable[[], int], readonly: bool = False):
        self.path = Path(path) if path else default_jobs_path()
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self.readonly = readonly
        self._db = connect_durable(self.path, readonly=readonly)
        if not readonly:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ plumbing
    def _write(self, fn: Callable[[], Any]) -> Any:
        if self.readonly:
            raise JobStoreError("job store opened read-only")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn()
                self._db.execute("COMMIT")
                return out
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _event(self, job_id: str, kind: str, detail: Any = "") -> None:
        text = detail if isinstance(detail, str) else canonical_json(detail)
        self._db.execute("INSERT INTO job_events(job_id, kind, at_ns, detail) VALUES (?,?,?,?)",
                         (job_id, kind, self._clock_ns(), _cut(text)))

    def _row(self, job_id: str):
        return self._db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    @staticmethod
    def _decode(row) -> dict:
        out = dict(row)
        for col in _JSON_COLUMNS:
            if col in out and out[col] is not None:
                out[col] = json.loads(out[col])
        out["cancel_requested"] = bool(out.get("cancel_requested"))
        return out

    def _transition(self, job_id: str, from_states: tuple[str, ...], sets: Mapping[str, Any], *, event: str,
                    detail: Any = "", extra_where: str = "", extra_args: tuple = ()) -> None:
        cols = ", ".join(f"{k} = ?" for k in sets)
        placeholders = ",".join("?" for _ in from_states)
        cur = self._db.execute(
            f"UPDATE jobs SET {cols}, updated_ns = ? WHERE job_id = ? AND state IN ({placeholders}){extra_where}",
            (*sets.values(), self._clock_ns(), job_id, *from_states, *extra_args))
        if cur.rowcount != 1:
            row = self._row(job_id)
            if row is None:
                raise LookupError(f"unknown job {job_id}")
            raise StateConflict(f"job {job_id} is {row['state']}; cannot {event}")
        self._event(job_id, event, detail)

    # ------------------------------------------------------------------ submission
    def submit(self, env: JobEnvelope, inputs: Mapping[str, Any], *, submitted_by: str,
               dedupe_window_ns: int = DEFAULT_DEDUPE_WINDOW_NS) -> SubmitResult:
        """Queue a job, or return the existing job it repeats. Raises ``JobStoreError`` for a reused job id
        with different content, a deadline already past, or a child job (the store accepts root jobs only)."""
        if not isinstance(env, JobEnvelope):
            raise JobStoreError("submit needs a JobEnvelope")
        if env.parent_job_id is not None:
            raise JobStoreError("the job store accepts root jobs only; child jobs are not supported")
        if not isinstance(submitted_by, str) or not submitted_by:
            raise JobStoreError("submitted_by is required")
        env_text = canonical_json(env.to_dict())
        inputs_text = canonical_json(dict(inputs))
        key = env.dedupe_key()

        def tx() -> SubmitResult:
            now = self._clock_ns()
            row = self._row(env.job_id)
            if row is not None:
                if row["envelope"] == env_text and row["inputs"] == inputs_text:
                    return SubmitResult(env.job_id, False, None, row["state"])
                raise JobStoreError(f"job id {env.job_id} reused with a different envelope or inputs")
            if env.deadline_ns <= now:
                raise JobStoreError("deadline already passed")
            canon = self._db.execute(
                "SELECT job_id, state, submitted_ns FROM jobs WHERE dedupe_key = ? AND state IN ('QUEUED','RUNNING','SUCCEEDED')"
                " ORDER BY submitted_ns DESC, job_id DESC LIMIT 1", (key,)).fetchone()
            if canon is not None and now - canon["submitted_ns"] <= dedupe_window_ns:
                self._event(canon["job_id"], "duplicate_submission", {"by": submitted_by, "refused_job_id": env.job_id})
                return SubmitResult(canon["job_id"], False, canon["job_id"], canon["state"])
            self._db.execute(
                "INSERT INTO jobs(job_id, dedupe_key, role, state, envelope, inputs, submitted_by, submitted_ns, deadline_ns,"
                " updated_ns) VALUES (?,?,?,'QUEUED',?,?,?,?,?,?)",
                (env.job_id, key, env.role, env_text, inputs_text, submitted_by, now, env.deadline_ns, now))
            self._event(env.job_id, "queued", {"by": submitted_by, "dedupe_key": key})
            return SubmitResult(env.job_id, True, None, "QUEUED")

        return self._write(tx)

    # ------------------------------------------------------------------ queries
    def get(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._row(job_id)
        return self._decode(row) if row is not None else None

    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict]:
        if state is not None and state not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        limit = max(1, min(int(limit), 1000))
        sql = f"SELECT {', '.join(_SUMMARY)}, json_extract(inputs, '$.request.question') AS question," \
              " json_extract(core_result, '$.body.proposal_id') AS proposal_id," \
              " json_extract(proposal, '$.abstain') AS abstain FROM jobs"
        args: tuple = ()
        if state is not None:
            sql += " WHERE state = ?"
            args = (state,)
        sql += " ORDER BY submitted_ns DESC, job_id DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, args + (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["cancel_requested"] = bool(d["cancel_requested"])
            d["abstain"] = None if d["abstain"] is None else bool(d["abstain"])
            out.append(d)
        return out

    def events(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT seq, kind, at_ns, detail FROM job_events WHERE job_id = ? ORDER BY seq",
                                    (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def running_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE state = 'RUNNING' ORDER BY started_ns, job_id").fetchall()
        return [self._decode(r) for r in rows]

    def queued_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE state = 'QUEUED' ORDER BY submitted_ns, job_id").fetchall()
        return [self._decode(r) for r in rows]

    def cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT state, cancel_requested FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row is not None and (bool(row["cancel_requested"]) or row["state"] == "CANCELLED")

    # ------------------------------------------------------------------ worker transitions
    def claim_next(self, worker_id: str, *, max_global: int, max_per_role: Mapping[str, int] | None) -> dict | None:
        """Atomically move the oldest admissible QUEUED job to RUNNING. ``max_per_role=None`` means no per-role
        limit; otherwise a role absent from the mapping (or mapped to 0) is never claimed."""
        def tx() -> dict | None:
            now = self._clock_ns()
            counts = {r["role"]: r["n"] for r in self._db.execute(
                "SELECT role, COUNT(*) AS n FROM jobs WHERE state = 'RUNNING' GROUP BY role")}
            if sum(counts.values()) >= max_global:
                return None
            for row in self._db.execute("SELECT job_id, role FROM jobs WHERE state = 'QUEUED' AND deadline_ns > ?"
                                        " ORDER BY submitted_ns, job_id", (now,)).fetchall():
                if max_per_role is not None and counts.get(row["role"], 0) >= max_per_role.get(row["role"], 0):
                    continue
                self._transition(row["job_id"], ("QUEUED",), {"state": "RUNNING", "started_ns": now, "worker_id": worker_id,
                                                              "start_attempts": self._attempts(row["job_id"]) + 1},
                                 event="started", detail={"worker_id": worker_id})
                return self._decode(self._row(row["job_id"]))
            return None

        return self._write(tx)

    def _attempts(self, job_id: str) -> int:
        return self._db.execute("SELECT start_attempts FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]

    def finish(self, job_id: str, worker_id: str, *, state: str, outcome: str, reason: str,
               proposal: Mapping[str, Any] | None = None, discarded_output_hash: str | None = None) -> dict:
        """Record a RUNNING job's terminal state and, in the same transaction, its proposal for delivery."""
        if state not in TERMINAL:
            raise JobStoreError(f"finish needs a terminal state, got {state}")
        proposal_text = canonical_json(dict(proposal)) if proposal is not None else None
        output_hash = content_hash(dict(proposal)) if proposal is not None else None

        def tx() -> dict:
            now = self._clock_ns()
            st, out, why, text, ohash, discarded = state, outcome, reason, proposal_text, output_hash, discarded_output_hash
            row = self._row(job_id)
            if row is not None and row["state"] == "RUNNING" and row["cancel_requested"] and st != "CANCELLED":
                # The owner's cancel arrived after the runner's last check: checked again here, in the same
                # transaction as the terminal state, so an accepted cancel request always discards the output.
                why = f"cancelled while running; the run ended {st} ({out}: {reason}); output discarded"
                discarded = ohash or discarded
                st, out, text, ohash = "CANCELLED", JobState.CANCELLED_BY_OWNER.value, None, None
            sets = {"state": st, "outcome": out, "reason": _cut(why), "finished_ns": now,
                    "proposal": text, "output_hash": ohash, "discarded_output_hash": discarded,
                    "delivery": "PENDING" if text is not None else "NONE",
                    "next_delivery_ns": now if text is not None else None}
            self._transition(job_id, ("RUNNING",), sets, event="finished",
                             detail={"state": st, "outcome": out, "output_hash": ohash, "discarded_output_hash": discarded},
                             extra_where=" AND worker_id = ?", extra_args=(worker_id,))
            return self._decode(self._row(job_id))

        return self._write(tx)

    def requeue(self, job_id: str, reason: str) -> None:
        """Recovery: a RUNNING job whose worker died before anything was sent goes back to the queue."""
        self._write(lambda: self._transition(job_id, ("RUNNING",), {"state": "QUEUED", "worker_id": None, "started_ns": None},
                                             event="requeued", detail=reason))

    def fail(self, job_id: str, *, from_states: tuple[str, ...], outcome: str, reason: str) -> None:
        """Fail a QUEUED or RUNNING job without a proposal (refused before the runner, or recovered after a crash)."""
        if not set(from_states) <= {"QUEUED", "RUNNING"}:
            raise JobStoreError("only QUEUED or RUNNING jobs can be failed")
        self._write(lambda: self._transition(job_id, from_states, {"state": "FAILED", "outcome": outcome,
                                                                   "reason": _cut(reason), "finished_ns": self._clock_ns()},
                                             event="failed", detail={"outcome": outcome, "reason": _cut(reason)}))

    def expire_overdue(self) -> list[str]:
        """QUEUED jobs at or past their deadline become EXPIRED without running."""
        def tx() -> list[str]:
            now = self._clock_ns()
            ids = [r["job_id"] for r in self._db.execute(
                "SELECT job_id FROM jobs WHERE state = 'QUEUED' AND deadline_ns <= ? ORDER BY submitted_ns", (now,))]
            for jid in ids:
                self._transition(jid, ("QUEUED",), {"state": "EXPIRED", "outcome": JobState.DEADLINE_EXPIRED.value,
                                                    "reason": "expired while queued", "finished_ns": now},
                                 event="expired", detail="expired while queued")
            return ids

        return self._write(tx)

    # ------------------------------------------------------------------ owner actions
    def cancel(self, job_id: str, *, by: str, reason: str = "owner cancel") -> dict:
        """QUEUED -> CANCELLED at once; RUNNING -> cancel requested (the runner stops at the next turn boundary
        and discards its output). A finished job raises ``StateConflict``; an unknown job ``LookupError``."""
        def tx() -> dict:
            row = self._row(job_id)
            if row is None:
                raise LookupError(f"unknown job {job_id}")
            if row["state"] == "QUEUED":
                self._transition(job_id, ("QUEUED",), {"state": "CANCELLED", "outcome": JobState.CANCELLED_BY_OWNER.value,
                                                       "reason": _cut(reason), "finished_ns": self._clock_ns(),
                                                       "cancel_requested": 1},
                                 event="cancelled", detail={"by": by, "while": "queued"})
            elif row["state"] == "RUNNING":
                if not row["cancel_requested"]:
                    self._transition(job_id, ("RUNNING",), {"cancel_requested": 1}, event="cancel_requested", detail={"by": by})
            else:
                raise StateConflict(f"job {job_id} already finished ({row['state']})")
            return self._decode(self._row(job_id))

        return self._write(tx)

    # ------------------------------------------------------------------ proposal outbox
    def due_deliveries(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE delivery = 'PENDING' AND next_delivery_ns <= ?"
                                    " ORDER BY next_delivery_ns, job_id LIMIT ?", (self._clock_ns(), limit)).fetchall()
        return [self._decode(r) for r in rows]

    def mark_delivered(self, job_id: str, core_result: Mapping[str, Any]) -> None:
        def tx() -> None:
            cur = self._db.execute("UPDATE jobs SET delivery = 'DELIVERED', core_result = ?, delivered_ns = ?, delivery_error = '',"
                                   " delivery_attempts = delivery_attempts + 1, next_delivery_ns = NULL, updated_ns = ?"
                                   " WHERE job_id = ? AND delivery = 'PENDING'",
                                   (canonical_json(dict(core_result)), self._clock_ns(), self._clock_ns(), job_id))
            if cur.rowcount != 1:
                raise StateConflict(f"job {job_id} has no pending delivery")
            self._event(job_id, "delivered", {"duplicate": bool(core_result.get("duplicate")),
                                              "proposal_id": (core_result.get("body") or {}).get("proposal_id")})
        self._write(tx)

    def mark_delivery_failed(self, job_id: str, error: str, *, retry_at_ns: int | None = None,
                             final: str | None = None, core_result: Mapping[str, Any] | None = None) -> int:
        """Record a failed delivery attempt. With ``final`` (REJECTED or CONFLICT) delivery stops and the proposal
        stays stored; otherwise it is retried at ``retry_at_ns``. Returns the attempt count."""
        if final is not None and final not in ("REJECTED", "CONFLICT"):
            raise JobStoreError("final must be REJECTED or CONFLICT")
        if final is None and retry_at_ns is None:
            raise JobStoreError("a retryable failure needs retry_at_ns")

        def tx() -> int:
            cur = self._db.execute(
                "UPDATE jobs SET delivery = ?, delivery_error = ?, delivery_attempts = delivery_attempts + 1, next_delivery_ns = ?,"
                " core_result = COALESCE(?, core_result), updated_ns = ? WHERE job_id = ? AND delivery = 'PENDING'",
                (final or "PENDING", _cut(error), retry_at_ns if final is None else None,
                 canonical_json(dict(core_result)) if core_result is not None else None, self._clock_ns(), job_id))
            if cur.rowcount != 1:
                raise StateConflict(f"job {job_id} has no pending delivery")
            self._event(job_id, "delivery_failed" if final is None else f"delivery_{final.lower()}", _cut(error))
            return self._db.execute("SELECT delivery_attempts FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]

        return self._write(tx)
