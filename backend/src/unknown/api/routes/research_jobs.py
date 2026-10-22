"""Research jobs for the terminal (ADR-0016): list, detail, submit and cancel.

The API process never holds a provider key and imports no provider adapter, runner or worker code
(a test enforces this). It only writes the research job store
(``<UNKNOWN_HOME>/agents/jobs.sqlite``); the model worker (``python -m unknown.agents.worker``)
runs the jobs and delivers each proposal to the core. Reads: the job store, the audit trail
(read-only), the spend ledger (read-only) and the proposal the core recorded (read-only core
connection).

  GET  /api/research/jobs[?state=QUEUED|RUNNING|SUCCEEDED|FAILED|CANCELLED|EXPIRED&limit=100]
  GET  /api/research/jobs/<job_id>
  POST /api/research/jobs                  body: see ``unknown.agents.submission``
  POST /api/research/jobs/<job_id>/cancel  body: {"reason": "..."} (optional)

POSTs reach these handlers only with an authenticated owner session and its CSRF token.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from ...agents.audit import AuditTrail, default_audit_path
from ...agents.budget import default_ledger_path, read_job_spend
from ...agents.config import OwnerPolicyError, default_policy_path, load_owner_policy
from ...agents.jobstore import JobStore, JobStoreError, StateConflict, default_jobs_path
from ...agents.submission import SubmissionRefused, build_research_job, research_store_path
from . import RouteConflict

_lock = threading.Lock()
_stores: dict[str, JobStore] = {}
_BULKY = ("envelope", "inputs", "proposal", "core_result")
NOW = time.time_ns          # wall clock; tests substitute a manual clock


def _now() -> int:
    return NOW()


def _store(ctx) -> JobStore:
    path = default_jobs_path(ctx.paths.root)
    with _lock:
        store = _stores.get(str(path))
        if store is None:
            store = _stores[str(path)] = JobStore(path, clock_ns=_now)
        return store


def _recorded_proposal(conn: sqlite3.Connection, proposal_id: str | None) -> dict | None:
    if not proposal_id:
        return None
    try:
        row = conn.execute("SELECT proposal_id, author, row FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    except sqlite3.Error as exc:
        return {"proposal_id": proposal_id, "error": f"core database: {exc}"}
    if row is None:
        return {"proposal_id": proposal_id, "missing_from_core_db": True}
    return {"proposal_id": row["proposal_id"], "author": row["author"], **json.loads(row["row"])}


def _detail(ctx, store: JobStore, job_id: str, conn: sqlite3.Connection) -> dict:
    job = store.get(job_id)
    if job is None:
        raise LookupError(f"no research job {job_id}")
    trail = AuditTrail(default_audit_path(ctx.paths.root), clock_ns=_now, readonly=True)
    try:
        audit = trail.records(job_id)
        problems = trail.verify()
    finally:
        trail.close()
    core_result = job.get("core_result") or {}
    inputs = job["inputs"]
    return {
        "job": {k: v for k, v in job.items() if k not in _BULKY},
        "request": inputs.get("request"),
        "task": inputs.get("task"),
        "evidence": inputs.get("evidence"),
        "envelope": job["envelope"],
        "proposal": job["proposal"],
        "core_result": job["core_result"],
        "recorded_proposal": _recorded_proposal(conn, (core_result.get("body") or {}).get("proposal_id")),
        "events": store.events(job_id),
        "audit": audit,
        "audit_chain": {"ok": not problems, "problems": problems[:20]},
        "spend": read_job_spend(default_ledger_path(ctx.paths.root), job_id),
    }


def get_jobs(ctx, parts: list[str], q: dict, account_key: str, conn: sqlite3.Connection) -> dict:
    store = _store(ctx)
    if parts == ["research", "jobs"]:
        return {"jobs": store.list(state=q.get("state"), limit=int(q.get("limit", 100)))}
    if len(parts) == 3:
        return _detail(ctx, store, parts[2], conn)
    raise LookupError("unknown research jobs endpoint")


def _submit(ctx, store: JobStore, body: dict) -> dict:
    try:
        policy = load_owner_policy(default_policy_path(ctx.paths.root))
    except (OwnerPolicyError, OSError) as exc:
        raise RouteConflict(f"the owner policy is invalid: {exc}") from None
    try:
        env, inputs = build_research_job(body, policy=policy, research_path=research_store_path(ctx.paths.root),
                                         now_ns=_now())
    except SubmissionRefused as exc:
        raise RouteConflict(str(exc)) from None
    try:
        res = store.submit(env, inputs, submitted_by="owner")
    except JobStoreError as exc:
        raise RouteConflict(str(exc)) from None
    job = store.get(res.job_id)
    return {"job_id": res.job_id, "created": res.created, "duplicate_of": res.duplicate_of, "state": res.state,
            "evidence_spans": len(job["inputs"]["evidence"]["spans"]), "spend_cap": job["envelope"]["spend_cap"],
            "deadline_ns": job["deadline_ns"]}


def post_jobs(ctx, parts: list[str], body: dict, session) -> dict:
    store = _store(ctx)
    if parts == ["research", "jobs"]:
        return _submit(ctx, store, body)
    if len(parts) == 4 and parts[3] == "cancel":
        if set(body) - {"reason"}:
            raise ValueError("cancel accepts only an optional reason")
        reason = body.get("reason", "owner cancel")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
            raise ValueError("reason must be a string of 1..500 characters")
        try:
            job = store.cancel(parts[2], by="owner", reason=reason)
        except StateConflict as exc:
            raise RouteConflict(str(exc)) from None
        return {"job_id": job["job_id"], "state": job["state"], "cancel_requested": job["cancel_requested"]}
    raise LookupError("unknown research jobs endpoint")


ROUTES = [(("research", "jobs"), get_jobs)]
POST_ROUTES = [(("research", "jobs"), post_jobs)]
