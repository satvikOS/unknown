"""The AI analyst ("ASK") for the terminal (ADR-0016): ask, stream, cancel, threads and cited tool results.

The API only relays: it writes the analyst job store (``<UNKNOWN_HOME>/agents/analyst_jobs.sqlite``) and the thread
store (``agents/analyst.sqlite``), and streams the events the analyst worker (``python -m unknown.agents.analyst``,
agents venv) appends there. It holds no provider key and imports no provider, loop, worker or data-source module (a
test checks every module the API process loads). Owner session required; POSTs also need the CSRF token.

  GET  /api/ai/status                         paid calls on/off and why, model and effort, caps, spend today
  GET  /api/ai/threads[?limit=]               conversation threads, newest first
  GET  /api/ai/threads/<thread_id>            questions and answers (citations, checks, tokens, cost, model, effort)
  GET  /api/ai/jobs/<job_id>                  the job's state, answer, spend (ledger, read-only) and cited results
  GET  /api/ai/results/<job_id>/<ref>         one tool result exactly as the model saw it (opened from a citation)
  GET  /api/ai/stream?job=<job_id>            SSE: status, turn, thinking, text, tool, tool_result, usage, answer, done
  POST /api/ai/ask                            {"question", "thread_id"?, "security"?, ...}: see agents/analyst/submission.py
  POST /api/ai/jobs/<job_id>/cancel           stops the job (queued: at once; running: at the next stream event)

Answers are advisory and labelled AI. Nothing here can place, change or cancel an order, start a session or move money.
"""
from __future__ import annotations

import json
import re
import threading
import time

from ...agents.analyst.status import ai_status
from ...agents.analyst.store import AnalystStore, ThreadBusy, default_analyst_jobs_path, default_analyst_path, new_ask_job_id
from ...agents.analyst.submission import SubmissionError, SubmissionRefused, ask_job, parse_ask
from ...agents.budget import default_ledger_path, read_job_spend
from ...agents.config import OwnerPolicyError, default_policy_path, load_owner_policy
from ...agents.jobstore import TERMINAL, JobStore, JobStoreError, StateConflict
from . import RouteConflict

_lock = threading.Lock()
_stores: dict[str, object] = {}
_JOB_RE = re.compile(r"^ask-[0-9a-f]{32}$")
_REF_RE = re.compile(r"^R[0-9]{1,4}$")
NOW = time.time_ns          # tests substitute a manual clock
MODELS_DIR = None           # config/models of the repository; tests substitute their own approvals
POLL_S = 0.2
KEEPALIVE_S = 15.0
RETRY_MS = 2000


def _now() -> int:
    return NOW()


def _cached(key: str, make):
    with _lock:
        store = _stores.get(key)
        if store is None:
            store = _stores[key] = make()
        return store


def _threads(ctx) -> AnalystStore:
    path = default_analyst_path(ctx.paths.root)
    return _cached(str(path), lambda: AnalystStore(path, clock_ns=_now))


def _jobs(ctx) -> JobStore:
    path = default_analyst_jobs_path(ctx.paths.root)
    return _cached(str(path), lambda: JobStore(path, clock_ns=_now))


def _job_id(text: str) -> str:
    if not _JOB_RE.match(text):
        raise ValueError("job id must look like ask-<32 hex>")
    return text


def _job_view(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {k: job.get(k) for k in ("job_id", "state", "outcome", "reason", "submitted_ns", "started_ns", "finished_ns",
                                    "deadline_ns", "cancel_requested")}


# ---------------------------------------------------------------------------- GET
def handle(ctx, parts: list[str], q: dict, account_key: str, conn) -> dict:
    if parts == ["ai", "status"]:
        return ai_status(ctx.paths.root, now_ns=_now(), models_dir=MODELS_DIR)
    if parts == ["ai", "threads"]:
        return {"threads": _threads(ctx).threads(int(q.get("limit", 100)))}
    if len(parts) == 3 and parts[1] == "threads":
        thread = _threads(ctx).thread(parts[2])
        if thread is None:
            raise LookupError(f"no thread {parts[2]}")
        jobs = _jobs(ctx)
        for turn in thread["turns"]:
            turn["job"] = _job_view(jobs.get(turn["job_id"]))
        return thread
    if len(parts) == 3 and parts[1] == "jobs":
        jid = _job_id(parts[2])
        job = _jobs(ctx).get(jid)
        if job is None:
            raise LookupError(f"no analyst job {jid}")
        store = _threads(ctx)
        return {"job": _job_view(job), "envelope": job["envelope"], "turn": store.turn_for_job(jid),
                "results": [{k: v for k, v in r.items() if k != "content"} for r in store.results(jid)],
                "events": _jobs(ctx).events(jid), "spend": read_job_spend(default_ledger_path(ctx.paths.root), jid)}
    if len(parts) == 4 and parts[1] == "results":
        jid = _job_id(parts[2])
        if not _REF_RE.match(parts[3]):
            raise ValueError("a citation ref looks like R1")
        result = _threads(ctx).result(jid, parts[3])
        if result is None:
            raise LookupError(f"no tool result {parts[3]} in job {jid}")
        return result
    raise LookupError("unknown AI endpoint")


# ---------------------------------------------------------------------------- POST
def _ask(ctx, body: dict) -> dict:
    try:
        policy = load_owner_policy(default_policy_path(ctx.paths.root))
    except (OwnerPolicyError, OSError) as exc:
        raise RouteConflict(f"the owner policy is invalid: {exc}") from None
    try:
        req = parse_ask(body, policy=policy)
    except SubmissionRefused as exc:
        raise RouteConflict(str(exc)) from None
    except SubmissionError as exc:
        raise ValueError(str(exc)) from None
    store, jobs = _threads(ctx), _jobs(ctx)
    job_id, now = new_ask_job_id(), _now()
    try:
        thread = store.add_question(job_id=job_id, question=req.question, security=req.security, thread_id=req.thread_id)
    except ThreadBusy as exc:
        raise RouteConflict(str(exc)) from None
    try:
        env, inputs = ask_job(req, job_id=job_id, now_ns=now, thread=thread, account=ctx.account_key)
        res = jobs.submit(env, inputs, submitted_by="owner")
    except (SubmissionError, JobStoreError) as exc:
        store.drop_question(job_id)
        raise RouteConflict(str(exc)) from None
    return {"job_id": res.job_id, "thread_id": thread["thread_id"], "seq": thread["seq"], "state": res.state,
            "spend_cap": env.spend_cap.amount.to_str(), "currency": env.spend_cap.currency, "deadline_ns": env.deadline_ns}


def post(ctx, parts: list[str], body: dict, session) -> dict:
    if parts == ["ai", "ask"]:
        return _ask(ctx, body)
    if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "cancel":
        jid = _job_id(parts[2])
        if set(body) - {"reason"}:
            raise ValueError("cancel accepts only an optional reason")
        reason = body.get("reason", "owner cancel")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
            raise ValueError("reason must be a string of 1..500 characters")
        try:
            job = _jobs(ctx).cancel(jid, by="owner", reason=reason)
        except StateConflict as exc:
            raise RouteConflict(str(exc)) from None
        if job["state"] == "CANCELLED":
            store = _threads(ctx)
            store.record_answer(jid, {"status": "cancelled", "label": None, "text": None, "reason": reason,
                                      "outcome": job["outcome"]})
            store.append_event(jid, "done", {"state": job["state"], "outcome": job["outcome"], "reason": reason})
        return {"job_id": jid, "state": job["state"], "cancel_requested": job["cancel_requested"]}
    raise LookupError("unknown AI endpoint")


# ---------------------------------------------------------------------------- SSE
def _frame(name: str, payload, event_id: int | None = None) -> bytes:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {name}\ndata: {json.dumps(payload, separators=(',', ':'), default=str)}\n\n".encode()


def stream(ctx, q: dict):
    """Validates the request (400/404 before any header) and returns the runner. Events are replayed from the store
    after ``Last-Event-ID``, so a reconnect resumes exactly; the stream ends after the job's ``done`` event."""
    jid = _job_id(q.get("job") or "")
    jobs = _jobs(ctx)
    if jobs.get(jid) is None:
        raise LookupError(f"no analyst job {jid}")
    path = default_analyst_path(ctx.paths.root)

    def run(last_event_id: int | None, write, flush, stopping) -> None:
        store = AnalystStore(path, clock_ns=_now, readonly=True)
        try:
            write(f"retry: {RETRY_MS}\n\n".encode())
            job = jobs.get(jid)
            write(_frame("job", _job_view(job)))
            flush()
            last, quiet = last_event_id or 0, 0.0
            while not stopping.is_set():
                events = store.events_after(jid, last)
                for e in events:
                    write(_frame(e["kind"], {**e["data"], "at_ns": e["at_ns"]}, e["seq"]))
                    last = e["seq"]
                    if e["kind"] == "done":
                        flush()
                        return
                if events:
                    flush()
                    quiet = 0.0
                    continue
                job = jobs.get(jid)
                if job is not None and job["state"] in TERMINAL and not store.events_after(jid, last):
                    # finished before any done event could be written (e.g. cancelled by another tab): say so and end
                    write(_frame("done", {"state": job["state"], "outcome": job["outcome"], "reason": job["reason"]}))
                    flush()
                    return
                stopping.wait(POLL_S)
                quiet += POLL_S
                if quiet >= KEEPALIVE_S:
                    write(b": keepalive\n\n")
                    flush()
                    quiet = 0.0
        finally:
            store.close()
    return run


ROUTES = [(("ai",), handle)]
POST_ROUTES = [(("ai",), post)]
STREAM_ROUTES = [(("ai", "stream"), stream)]
