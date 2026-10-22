"""Job envelopes and the job scheduler (ADR-0016 section 2; CAP-AGT-01).

Every model-driven unit of work is a job whose envelope fixes, before any provider
call: role, output schema id and version, input artifact hashes, the allowed-tool
list, wall-clock deadline, max turns (model calls), max tool calls, retry budget,
token cap and spend cap (exact ``Money``). A job may name a parent job; the parent
must itself be a root job, so the tree depth is at most 1 and a child can never
widen its parent's tools, deadline, token cap or spend cap.

The scheduler (``JobRegistry``) is deterministic code, never a model. It:
  * deduplicates equivalent jobs by CAP-AGT-01's key
    ``SHA-256(role || schema_version || prompt_template_hash || sorted(input_hashes))``
    (the output schema id is included as well);
  * caps concurrency per role and globally (a role without a configured limit cannot run);
  * expires queued jobs at their deadline and discards outputs that arrive after it,
    recording the discarded output hash;
  * cancels jobs (queued: immediately; running: at the next turn boundary, output discarded).

Workers hold no handle to the registry, so a model cannot create jobs.
"""
from __future__ import annotations

import enum
import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..domain.fixed import Fixed
from ..domain.ids import InvalidIdentifier, validate_id
from ..domain.money import Money

ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JOB_ID_LEN = 120
MAX_TURNS_LIMIT = 50
MAX_TOOL_CALLS_LIMIT = 500
MAX_RETRY_BUDGET = 10
MAX_TOKEN_CAP = 50_000_000
MAX_INPUTS = 1000
MAX_DEPTH = 1

ENVELOPE_FIELDS = frozenset({
    "job_id", "role", "schema_version", "input_hashes", "allowed_tools", "deadline_ns", "max_turns",
    "max_tool_calls", "retry_budget", "token_cap", "spend_cap", "output_schema", "created_ns",
    "parent_job_id", "prompt_template_hash", "relevance_end_ns",
})


class EnvelopeError(ValueError):
    pass


class SchedulerError(RuntimeError):
    pass


class JobState(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DUPLICATE = "duplicate"
    # terminal statuses (CAP-AGT-01 outputs)
    SUCCEEDED = "succeeded"
    SCHEMA_INVALID = "schema_invalid"
    EXCEEDED_TURNS = "exceeded_turns"
    EXCEEDED_TOOL_CALLS = "exceeded_tool_calls"
    DEADLINE_EXPIRED = "deadline_expired"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED_BY_OWNER = "cancelled_by_owner"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    DENIED_BY_POLICY = "denied_by_policy"

    @property
    def terminal(self) -> bool:
        return self not in (JobState.QUEUED, JobState.RUNNING, JobState.DUPLICATE)


def _int(value: Any, name: str, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise EnvelopeError(f"{name} must be an int in [{lo}, {hi}], got {value!r}")
    return value


@dataclass(frozen=True)
class JobEnvelope:
    job_id: str
    role: str
    schema_version: int
    input_hashes: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    deadline_ns: int
    max_turns: int
    max_tool_calls: int
    retry_budget: int
    token_cap: int
    spend_cap: Money
    output_schema: str
    created_ns: int
    parent_job_id: str | None = None
    prompt_template_hash: str | None = None
    relevance_end_ns: int | None = None

    def __post_init__(self) -> None:
        try:
            validate_id(self.job_id, "job id")
        except InvalidIdentifier as exc:
            raise EnvelopeError(str(exc)) from None
        if len(self.job_id) > MAX_JOB_ID_LEN:
            raise EnvelopeError(f"job id longer than {MAX_JOB_ID_LEN}")
        if not isinstance(self.role, str) or not ROLE_RE.match(self.role):
            raise EnvelopeError(f"invalid role {self.role!r}")
        _int(self.schema_version, "schema_version", 1, 10_000)
        if not isinstance(self.output_schema, str) or not SCHEMA_ID_RE.match(self.output_schema):
            raise EnvelopeError(f"invalid output schema id {self.output_schema!r}")
        if not isinstance(self.input_hashes, tuple) or len(self.input_hashes) > MAX_INPUTS:
            raise EnvelopeError("input_hashes must be a tuple of at most 1000 SHA-256 hex digests")
        if any(not isinstance(h, str) or not HASH_RE.match(h) for h in self.input_hashes):
            raise EnvelopeError("input_hashes must be lowercase SHA-256 hex digests")
        if len(set(self.input_hashes)) != len(self.input_hashes):
            raise EnvelopeError("input_hashes contains duplicates")
        if not isinstance(self.allowed_tools, tuple) or any(not isinstance(t, str) or not TOOL_RE.match(t) for t in self.allowed_tools):
            raise EnvelopeError("allowed_tools must be a tuple of tool names")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise EnvelopeError("allowed_tools contains duplicates")
        _int(self.created_ns, "created_ns", 0, 2**63 - 1)
        _int(self.deadline_ns, "deadline_ns", 0, 2**63 - 1)
        if self.deadline_ns <= self.created_ns:
            raise EnvelopeError("deadline_ns must be after created_ns")
        _int(self.max_turns, "max_turns", 1, MAX_TURNS_LIMIT)
        _int(self.max_tool_calls, "max_tool_calls", 0, MAX_TOOL_CALLS_LIMIT)
        _int(self.retry_budget, "retry_budget", 0, MAX_RETRY_BUDGET)
        _int(self.token_cap, "token_cap", 1, MAX_TOKEN_CAP)
        if not isinstance(self.spend_cap, Money) or not self.spend_cap.is_positive():
            raise EnvelopeError("spend_cap must be a positive Money")
        if self.parent_job_id is not None:
            try:
                validate_id(self.parent_job_id, "parent job id")
            except InvalidIdentifier as exc:
                raise EnvelopeError(str(exc)) from None
            if self.parent_job_id == self.job_id:
                raise EnvelopeError("a job cannot be its own parent")
        if self.prompt_template_hash is not None and (not isinstance(self.prompt_template_hash, str)
                                                      or not HASH_RE.match(self.prompt_template_hash)):
            raise EnvelopeError("prompt_template_hash must be a SHA-256 hex digest")
        if self.relevance_end_ns is not None:
            _int(self.relevance_end_ns, "relevance_end_ns", 0, 2**63 - 1)

    # ------------------------------------------------------------------ helpers
    def dedupe_key(self) -> str:
        """CAP-AGT-01: SHA-256(role || schema_version || prompt_template_hash || sorted(input hashes)),
        extended with the output schema id. Fields are length-prefixed so no concatenation is ambiguous."""
        parts = [self.role, self.output_schema, str(self.schema_version), self.prompt_template_hash or "",
                 *sorted(self.input_hashes)]
        h = hashlib.sha256()
        for p in parts:
            b = p.encode()
            h.update(len(b).to_bytes(4, "big"))
            h.update(b)
        return h.hexdigest()

    def allows_tool(self, name: str) -> bool:
        return name in self.allowed_tools

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id, "role": self.role, "schema_version": self.schema_version,
            "input_hashes": list(self.input_hashes), "allowed_tools": list(self.allowed_tools),
            "deadline_ns": self.deadline_ns, "max_turns": self.max_turns, "max_tool_calls": self.max_tool_calls,
            "retry_budget": self.retry_budget, "token_cap": self.token_cap, "spend_cap": self.spend_cap.to_json(),
            "output_schema": self.output_schema, "created_ns": self.created_ns, "parent_job_id": self.parent_job_id,
            "prompt_template_hash": self.prompt_template_hash, "relevance_end_ns": self.relevance_end_ns,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "JobEnvelope":
        """Strict construction: unknown fields, floats and missing required fields are refused."""
        if not isinstance(d, Mapping):
            raise EnvelopeError("envelope must be an object")
        unknown = set(d) - ENVELOPE_FIELDS
        if unknown:
            raise EnvelopeError(f"unknown envelope fields {sorted(unknown)}")
        required = ENVELOPE_FIELDS - {"parent_job_id", "prompt_template_hash", "relevance_end_ns"}
        missing = required - set(d)
        if missing:
            raise EnvelopeError(f"missing envelope fields {sorted(missing)}")
        cap = d["spend_cap"]
        if not isinstance(cap, Mapping) or set(cap) != {"currency", "amount"} or not isinstance(cap["amount"], str):
            raise EnvelopeError("spend_cap must be {currency, amount} with a decimal-string amount")
        try:
            spend_cap = Money(cap["currency"], Fixed.parse(cap["amount"]))
        except (ValueError, TypeError) as exc:
            raise EnvelopeError(f"spend_cap: {exc}") from None
        for name in ("input_hashes", "allowed_tools"):
            if not isinstance(d[name], (list, tuple)):
                raise EnvelopeError(f"{name} must be a list")
        return cls(
            job_id=d["job_id"], role=d["role"], schema_version=d["schema_version"],
            input_hashes=tuple(d["input_hashes"]), allowed_tools=tuple(d["allowed_tools"]),
            deadline_ns=d["deadline_ns"], max_turns=d["max_turns"], max_tool_calls=d["max_tool_calls"],
            retry_budget=d["retry_budget"], token_cap=d["token_cap"], spend_cap=spend_cap,
            output_schema=d["output_schema"], created_ns=d["created_ns"], parent_job_id=d.get("parent_job_id"),
            prompt_template_hash=d.get("prompt_template_hash"), relevance_end_ns=d.get("relevance_end_ns"))


# ============================================================================ scheduler
@dataclass
class JobRecord:
    envelope: JobEnvelope
    state: JobState
    depth: int
    dedupe_key: str
    submitted_ns: int
    duplicate_of: str | None = None
    started_ns: int | None = None
    finished_ns: int | None = None
    output_hash: str | None = None
    discarded_output_hash: str | None = None
    reason: str = ""
    research_only: bool = False
    cancel_requested: bool = False


@dataclass(frozen=True)
class Submission:
    job_id: str
    accepted: bool
    duplicate_of: str | None = None
    reason: str = ""


@dataclass
class JobRegistry:
    """In-process scheduler. Thread-safe; state is in memory with an append-only event list.

    ``max_per_role``: a role absent from the mapping has a limit of 0 and never runs.
    ``dedupe_window_ns``: an equivalent job submitted within this window of the canonical
    job's submission (and while that job is queued, running or succeeded) is a duplicate.
    """
    clock_ns: Callable[[], int]
    max_global: int
    max_per_role: Mapping[str, int]
    dedupe_window_ns: int
    records: dict[str, JobRecord] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    _by_key: dict[str, str] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if type(self.max_global) is not int or self.max_global < 1:
            raise SchedulerError("max_global must be >= 1")

    def _event(self, kind: str, job_id: str, **extra: Any) -> None:
        self.events.append({"kind": kind, "job_id": job_id, "at_ns": self.clock_ns(), **extra})

    # ------------------------------------------------------------------ submit
    def submit(self, env: JobEnvelope) -> Submission:
        with self._lock:
            now = self.clock_ns()
            existing = self.records.get(env.job_id)
            if existing is not None:
                if existing.envelope == env:
                    return Submission(env.job_id, True, existing.duplicate_of, "already submitted")
                raise SchedulerError(f"job id {env.job_id} reused with a different envelope")
            if env.deadline_ns <= now:
                self._event("refused", env.job_id, reason="deadline_already_passed")
                return Submission(env.job_id, False, reason="deadline already passed")
            depth = 0
            if env.parent_job_id is not None:
                parent = self.records.get(env.parent_job_id)
                if parent is None:
                    self._event("refused", env.job_id, reason="unknown_parent")
                    return Submission(env.job_id, False, reason=f"unknown parent {env.parent_job_id}")
                if parent.depth + 1 > MAX_DEPTH:
                    self._event("refused", env.job_id, reason="depth_exceeded")
                    return Submission(env.job_id, False, reason=f"depth limit {MAX_DEPTH} exceeded")
                p = parent.envelope
                widened = []
                if not set(env.allowed_tools) <= set(p.allowed_tools):
                    widened.append("allowed_tools")
                if env.deadline_ns > p.deadline_ns:
                    widened.append("deadline")
                if env.token_cap > p.token_cap:
                    widened.append("token_cap")
                if env.spend_cap.currency != p.spend_cap.currency or env.spend_cap > p.spend_cap:
                    widened.append("spend_cap")
                if widened:
                    self._event("refused", env.job_id, reason="child_widens_parent", fields=widened)
                    return Submission(env.job_id, False, reason=f"child job widens parent: {widened}")
                depth = parent.depth + 1
            key = env.dedupe_key()
            canonical_id = self._by_key.get(key)
            if canonical_id is not None:
                canon = self.records[canonical_id]
                live = canon.state in (JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED)
                if live and now - canon.submitted_ns <= self.dedupe_window_ns:
                    self.records[env.job_id] = JobRecord(env, JobState.DUPLICATE, depth, key, now, duplicate_of=canonical_id)
                    self._event("deduplicated", env.job_id, duplicate_of=canonical_id)
                    return Submission(env.job_id, True, canonical_id, "equivalent job exists")
            self.records[env.job_id] = JobRecord(env, JobState.QUEUED, depth, key, now)
            self._by_key[key] = env.job_id
            self._event("queued", env.job_id, depth=depth)
            return Submission(env.job_id, True)

    # ------------------------------------------------------------------ run control
    def _running(self, role: str | None = None) -> int:
        return sum(1 for r in self.records.values()
                   if r.state is JobState.RUNNING and (role is None or r.envelope.role == role))

    def expire_overdue(self) -> list[str]:
        """Queued jobs past their deadline become ``deadline_expired`` without running."""
        with self._lock:
            now = self.clock_ns()
            expired = []
            for r in self.records.values():
                if r.state is JobState.QUEUED and now >= r.envelope.deadline_ns:
                    r.state, r.finished_ns, r.reason = JobState.DEADLINE_EXPIRED, now, "expired while queued"
                    self._event("expired", r.envelope.job_id, while_state="queued")
                    expired.append(r.envelope.job_id)
            return expired

    def try_start(self, job_id: str) -> bool:
        with self._lock:
            self.expire_overdue()
            r = self.records.get(job_id)
            if r is None or r.state is not JobState.QUEUED:
                return False
            role = r.envelope.role
            if self._running() >= self.max_global or self._running(role) >= self.max_per_role.get(role, 0):
                return False
            r.state, r.started_ns = JobState.RUNNING, self.clock_ns()
            self._event("started", job_id)
            return True

    def start_next(self, role: str | None = None) -> JobEnvelope | None:
        """Start the oldest queued job (optionally of one role) that concurrency limits admit."""
        with self._lock:
            self.expire_overdue()
            queued = sorted((r for r in self.records.values() if r.state is JobState.QUEUED
                             and (role is None or r.envelope.role == role)),
                            key=lambda r: (r.submitted_ns, r.envelope.job_id))
            for r in queued:
                if self.try_start(r.envelope.job_id):
                    return r.envelope
            return None

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            r = self.records.get(job_id)
            return r is not None and (r.cancel_requested or r.state is JobState.CANCELLED_BY_OWNER)

    def cancel(self, job_id: str, reason: str = "owner cancel") -> JobState:
        with self._lock:
            r = self.records.get(job_id)
            if r is None:
                raise SchedulerError(f"unknown job {job_id}")
            if r.state.terminal:
                return r.state
            if r.state in (JobState.QUEUED, JobState.DUPLICATE):
                r.state, r.finished_ns, r.reason = JobState.CANCELLED_BY_OWNER, self.clock_ns(), reason
                self._event("cancelled", job_id, while_state="queued")
                return r.state
            r.cancel_requested = True
            self._event("cancel_requested", job_id)
            return r.state

    def complete(self, job_id: str, state: JobState, *, output_hash: str | None = None, reason: str = "") -> JobRecord:
        """Record a running job's terminal state. Outputs arriving after the deadline or after a
        cancellation are discarded (their hash is kept as evidence) and the job is marked accordingly."""
        if not state.terminal:
            raise SchedulerError("complete() needs a terminal state")
        with self._lock:
            r = self.records.get(job_id)
            if r is None or r.state is not JobState.RUNNING:
                raise SchedulerError(f"job {job_id} is not running")
            now = self.clock_ns()
            r.finished_ns = now
            if r.cancel_requested:
                r.state, r.discarded_output_hash, r.reason = JobState.CANCELLED_BY_OWNER, output_hash, "cancelled while running; output discarded"
                self._event("discarded", job_id, cause="cancelled", output_hash=output_hash)
                return r
            if now > r.envelope.deadline_ns:
                r.state, r.discarded_output_hash, r.reason = JobState.DEADLINE_EXPIRED, output_hash, "output arrived after deadline; discarded"
                self._event("discarded", job_id, cause="deadline", output_hash=output_hash)
                return r
            r.state, r.output_hash, r.reason = state, output_hash, reason
            if r.envelope.relevance_end_ns is not None and now > r.envelope.relevance_end_ns:
                r.research_only = True
            self._event("completed", job_id, state=state.value, output_hash=output_hash, research_only=r.research_only)
            return r

    # ------------------------------------------------------------------ queries
    def record(self, job_id: str) -> JobRecord:
        with self._lock:
            r = self.records.get(job_id)
            if r is None:
                raise SchedulerError(f"unknown job {job_id}")
            return r

    def output_for(self, job_id: str) -> str | None:
        """The output hash a job refers to; duplicates resolve to their canonical job's output."""
        with self._lock:
            r = self.record(job_id)
            if r.state is JobState.DUPLICATE and r.duplicate_of:
                return self.record(r.duplicate_of).output_hash
            return r.output_hash
