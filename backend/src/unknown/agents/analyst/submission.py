"""Build an analyst ("ASK") job from an owner request. Used by the terminal API, which writes only the analyst stores:
no provider adapter, no loop, no worker and no data source is imported here.

Request fields (unknown fields are refused):
  question           required, 1..4000 characters
  thread_id          optional: continue this thread (its earlier questions and answers become context)
  security           optional: the terminal's linked security key (e.g. AAPL.XNAS), named to the model as context
  max_output_tokens  default 8000, 1024..32000 per model turn (adaptive thinking counts against it)
  deadline_seconds   default 900, 60..3600 (covers queueing and running)
  spend_cap          decimal string in the owner's currency; default and maximum: the owner's per-job cap

The envelope is complete at submission: role ``analyst``, the read-only tool allowlist, the prompt hash (system prompt
and tool definitions), turn, tool-call, retry and token caps, deadline and spend cap. The daily, monthly, lifetime,
role and provider caps are the owner's (``agents.json``) and are enforced by the spend ledger before every call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from ...domain.canonical import canonical_json, sha256_hex
from ...domain.fixed import Fixed
from ...domain.money import Money
from ..config import OwnerPolicy
from ..envelope import EnvelopeError, JobEnvelope
from .prompt import PROMPT_HASH
from .toolspec import TOOL_NAMES

ROLE = "analyst"
OUTPUT_SCHEMA = "analyst_answer"
SCHEMA_VERSION = 1
REQUEST_FIELDS = frozenset({"question", "thread_id", "security", "max_output_tokens", "deadline_seconds", "spend_cap"})
MAX_QUESTION = 4000
MAX_TURNS = 12
MAX_TOOL_CALLS = 24
RETRY_BUDGET = 2
TOKEN_CAP = 2_000_000
LIMITS = {"max_output_tokens": (1024, 32_000), "deadline_seconds": (60, 3600)}
DEFAULTS = {"max_output_tokens": 8000, "deadline_seconds": 900}
NS = 1_000_000_000
_THREAD_RE = re.compile(r"^thr-[0-9a-f]{32}$")
_SECURITY_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,40}$")


class SubmissionError(ValueError):
    """The request is malformed (HTTP 400)."""


class SubmissionRefused(RuntimeError):
    """Well formed but cannot be queued now (HTTP 409): paid calls disabled, a spend cap above the owner's."""


@dataclass(frozen=True)
class AskRequest:
    question: str
    thread_id: str | None
    security: str | None
    max_output_tokens: int
    deadline_seconds: int
    spend_cap: Money


def _int(body: Mapping[str, Any], name: str) -> int:
    value = body.get(name, DEFAULTS[name])
    lo, hi = LIMITS[name]
    if type(value) is not int or not lo <= value <= hi:
        raise SubmissionError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def parse_ask(body: Any, *, policy: OwnerPolicy | None) -> AskRequest:
    if not isinstance(body, Mapping):
        raise SubmissionError("request must be a JSON object")
    unknown = set(body) - REQUEST_FIELDS
    if unknown:
        raise SubmissionError(f"unknown fields {sorted(unknown)}; allowed: {sorted(REQUEST_FIELDS)}")
    question = body.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise SubmissionError(f"question must be a non-empty string of at most {MAX_QUESTION} characters")
    thread_id = body.get("thread_id")
    if thread_id is not None and (not isinstance(thread_id, str) or not _THREAD_RE.match(thread_id)):
        raise SubmissionError("thread_id must be a thread id from GET /api/ai/threads")
    security = body.get("security")
    if security is not None and (not isinstance(security, str) or not _SECURITY_RE.match(security)):
        raise SubmissionError("security must be a security key such as AAPL.XNAS")
    max_out, deadline = _int(body, "max_output_tokens"), _int(body, "deadline_seconds")
    if policy is None:
        raise SubmissionRefused("paid model calls are disabled: there is no owner policy (<UNKNOWN_HOME>/config/agents.json); "
                                "see docs/agents/README.md")
    per_job = policy.caps.per_job
    if not per_job.is_positive() or not policy.caps.per_day.is_positive():
        raise SubmissionRefused("paid model calls are disabled: the owner's per-day or per-job cap is zero")
    cap = per_job
    if "spend_cap" in body:
        raw = body["spend_cap"]
        if not isinstance(raw, str):
            raise SubmissionError("spend_cap must be a decimal string (JSON numbers are refused for money)")
        try:
            cap = Money(per_job.currency, Fixed.parse(raw))
        except (ValueError, TypeError) as exc:
            raise SubmissionError(f"spend_cap: {exc}") from None
        if not cap.is_positive():
            raise SubmissionError("spend_cap must be positive")
        if cap > per_job:
            raise SubmissionError(f"spend_cap {cap} exceeds the owner's per-job cap {per_job}; a job cannot raise it")
    return AskRequest(question.strip(), thread_id, security, max_out, deadline, cap)


def ask_job(req: AskRequest, *, job_id: str, now_ns: int, thread: Mapping[str, Any], account: str | None) -> tuple[JobEnvelope, dict]:
    """The envelope and stored inputs of one question. ``thread`` is what ``AnalystStore.add_question`` returned."""
    context = {"question": req.question, "security": req.security, "thread_id": thread["thread_id"], "seq": thread["seq"],
               "history": list(thread["history"]), "history_datasets": list(thread["history_datasets"])}
    try:
        env = JobEnvelope(
            job_id=job_id, role=ROLE, schema_version=SCHEMA_VERSION, input_hashes=(sha256_hex(canonical_json(context)),),
            allowed_tools=TOOL_NAMES, deadline_ns=now_ns + req.deadline_seconds * NS, max_turns=MAX_TURNS,
            max_tool_calls=MAX_TOOL_CALLS, retry_budget=RETRY_BUDGET, token_cap=TOKEN_CAP, spend_cap=req.spend_cap,
            output_schema=OUTPUT_SCHEMA, created_ns=now_ns, prompt_template_hash=PROMPT_HASH)
    except EnvelopeError as exc:
        raise SubmissionError(str(exc)) from None
    inputs = {"schema": 1, "context": context, "context_hash": env.input_hashes[0],
              "max_output_tokens": req.max_output_tokens, "account": account}
    return env, inputs
