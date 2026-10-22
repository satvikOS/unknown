"""Build a research job (envelope and inputs) from an owner request (ADR-0016 section 2).

Used by the terminal API, which only writes the job store: this module imports no provider
adapter, no runner and no credential code. The envelope is complete at submission (role,
output schema, prompt template hash, input hashes, deadline, turn/tool/retry/token caps and the
spend cap), so the dedupe key and everything the worker may spend are fixed before any model
call. The worker re-derives the evidence from the research store and refuses the job if it no
longer matches the input hashes.

Request fields (unknown fields are refused):
  question           required, 1..4000 characters
  role               default "research"
  issuer_ids         up to 5 PIT entity ids or tickers known to the research store (optional)
  instrument_ids     up to 20 internal instrument ids (optional). Each must be in the core's instrument master:
                     checked here through ``instrument_known`` and again by the core when it records the
                     proposal. They are named in the task text, so they are part of the input hashes.
  fields            XBRL concepts (taxonomy:Concept); default ``pit_evidence.DEFAULT_FIELDS``
  max_output_tokens  default 8000, 256..32000
  deadline_seconds   default 3600, 60..86400 (covers queueing and running)
  horizon_seconds    default 7776000 (90 days), 1..31536000
  spend_cap          decimal string in the owner's currency; default and maximum: the owner's per-job cap
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ..data.pit import ResearchStore
from ..domain.canonical import sha256_hex
from ..domain.fixed import Fixed
from ..domain.ids import InvalidIdentifier, validate_id
from ..domain.money import Money
from .config import OwnerPolicy
from .envelope import ROLE_RE, EnvelopeError, JobEnvelope
from .jobstore import new_job_id
from .pit_evidence import EMPTY, EvidenceError, derive, selection, task_text
from .prompts import PROMPT_TEMPLATE_HASH

OUTPUT_SCHEMA = "research_answer"
SCHEMA_VERSION = 1
REQUEST_FIELDS = frozenset({"question", "role", "issuer_ids", "instrument_ids", "fields", "max_output_tokens",
                            "deadline_seconds", "horizon_seconds", "spend_cap"})
MAX_INSTRUMENTS = 20
DEFAULTS = {"role": "research", "max_output_tokens": 8000, "deadline_seconds": 3600, "horizon_seconds": 7_776_000}
LIMITS = {"max_output_tokens": (256, 32_000), "deadline_seconds": (60, 86_400), "horizon_seconds": (1, 31_536_000)}
MAX_TURNS = 4          # one answer, one truncation retry, two transient-error retries
RETRY_BUDGET = 2
TOKEN_CAP = 500_000
MAX_QUESTION = 4000
NS = 1_000_000_000


class SubmissionError(ValueError):
    """The request is malformed (HTTP 400)."""


class SubmissionRefused(RuntimeError):
    """The request is well formed but cannot be queued now (HTTP 409), e.g. paid calls are disabled."""


def research_store_path(home: Path) -> Path:
    return Path(home) / "research" / "research.sqlite"


def _int(body: Mapping[str, Any], name: str) -> int:
    value = body.get(name, DEFAULTS[name])
    lo, hi = LIMITS[name]
    if type(value) is not int or not lo <= value <= hi:
        raise SubmissionError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def instrument_ids(body: Mapping[str, Any], instrument_known: Callable[[str], bool] | None) -> list[str]:
    """The request's instrument ids, each checked against the core's instrument master (``instrument_known``).
    With no master to check against, a request that names instruments is refused, never accepted unchecked."""
    raw = body.get("instrument_ids", [])
    if not isinstance(raw, list) or len(raw) > MAX_INSTRUMENTS:
        raise SubmissionError(f"instrument_ids must be a list of at most {MAX_INSTRUMENTS} instrument ids")
    out: list[str] = []
    for v in raw:
        try:
            validate_id(v, "instrument id")
        except InvalidIdentifier as exc:
            raise SubmissionError(str(exc)) from None
        if v in out:
            raise SubmissionError(f"instrument id {v} is listed twice")
        out.append(v)
    if out and instrument_known is None:
        raise SubmissionRefused("instrument_ids cannot be checked: the core's instrument master is not available")
    unknown = [v for v in out if not instrument_known(v)]
    if unknown:
        raise SubmissionError(f"not in the instrument master: {unknown}; register the instrument first")
    return out


def build_research_job(body: Mapping[str, Any], *, policy: OwnerPolicy | None, research_path: Path, now_ns: int,
                       job_id: str | None = None,
                       instrument_known: Callable[[str], bool] | None = None) -> tuple[JobEnvelope, dict]:
    if not isinstance(body, Mapping):
        raise SubmissionError("request must be a JSON object")
    unknown = set(body) - REQUEST_FIELDS
    if unknown:
        raise SubmissionError(f"unknown fields {sorted(unknown)}; allowed: {sorted(REQUEST_FIELDS)}")
    question = body.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise SubmissionError(f"question must be a non-empty string of at most {MAX_QUESTION} characters")
    role = body.get("role", DEFAULTS["role"])
    if not isinstance(role, str) or not ROLE_RE.match(role):
        raise SubmissionError("role must match ^[a-z][a-z0-9_]{0,63}$")
    max_out, deadline_s, horizon = _int(body, "max_output_tokens"), _int(body, "deadline_seconds"), _int(body, "horizon_seconds")
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
    instruments = instrument_ids(body, instrument_known)
    issuers = body.get("issuer_ids") or []
    fields = body.get("fields")
    if fields is not None and not issuers:
        raise SubmissionError("fields need issuer_ids")
    sel, evidence = None, EMPTY
    if issuers:
        try:
            store = ResearchStore.open_readonly(research_path)
        except FileNotFoundError:
            raise SubmissionRefused(f"no research store at {research_path}; ingest SEC data first "
                                    "(python -m unknown.data.sec ingest --cik ...)") from None
        try:
            sel = selection(store, issuers, fields, now_ns)
            evidence = derive(store, sel)
        except EvidenceError as exc:
            raise SubmissionError(str(exc)) from None
        finally:
            store.close()
    task = task_text(question, sel, evidence, instruments)
    task_hash = sha256_hex(task)
    try:
        env = JobEnvelope(
            job_id=job_id or new_job_id(), role=role, schema_version=SCHEMA_VERSION,
            input_hashes=(task_hash, *(s.content_hash for s in evidence.spans)), allowed_tools=(),
            deadline_ns=now_ns + deadline_s * NS, max_turns=MAX_TURNS, max_tool_calls=0, retry_budget=RETRY_BUDGET,
            token_cap=TOKEN_CAP, spend_cap=cap, output_schema=OUTPUT_SCHEMA, created_ns=now_ns,
            prompt_template_hash=PROMPT_TEMPLATE_HASH)
    except EnvelopeError as exc:
        raise SubmissionError(str(exc)) from None
    inputs = {
        "schema": 1,
        "task": task,
        "task_hash": task_hash,
        "horizon_seconds": horizon,
        "max_output_tokens": max_out,
        "request": {"question": question, "role": role, "issuer_ids": list(issuers), "instrument_ids": instruments,
                    "fields": list(fields) if fields is not None else None},
        "evidence": {"selection": sel.to_dict() if sel else None, "spans": evidence.manifest()},
    }
    return env, inputs
