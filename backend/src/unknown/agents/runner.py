"""Run one bounded job end to end (ADR-0016; CAP-AGT-01/02/03/06, CAP-MODEL-02/04/05/06/09/12).

Order of operations for each model turn:
  routability (adapter facts, catalog approval, approved tariff) -> data-use gate (count)
  -> token count (provider endpoint, else conservative local estimate) -> context fit and
  token cap -> worst-case cost (tariffs; plus any server-side fallback model) -> data-use
  gate (generate) -> ledger reservation -> provider call -> settle (or keep the reservation
  as UNCONFIRMED when cost cannot be computed exactly) -> local schema validation ->
  tool requests through the default-deny gateway (next turn) or evidence verification
  -> ResearchProposal (or abstention) -> ``submit_research_proposal`` as AGENT.

Fallback to the next candidate happens only after TRANSIENT errors (rate limit, overload,
server error, timeout, network) once retries are exhausted, or when a candidate is not
routable or not permitted by the gate. QUOTA_OR_SPEND_EXHAUSTED, AUTH, INVALID_REQUEST and
UNKNOWN end the job without trying another provider. A local budget refusal ends the job
``budget_exhausted``. Outputs produced after the deadline or after cancellation are
discarded and never submitted.

Model output is untrusted: it is parsed with exact decimals, validated against the job's
output schema, and its claims are verified against the supplied spans before any of it
reaches the proposal. Unverified claims are carried only as labelled assumptions.

The proposal is research_proposal schema version 2 (``build_proposal``). Its typed fields (as-of
instant, instrument and entity ids, evidence references, observation window, horizon) come from
the job and from the provenance of the spans that verified claims cite, never from model output
or span text; the model supplies only text. Agent answers carry no forecast block.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..domain.canonical import content_hash
from ..domain.ids import InvalidIdentifier, validate_id
from ..domain.money import Money
from .budget import BudgetError, Reservation, SpendLedger
from .catalog import Catalog
from .common import CONFIG_MODELS, CONTRACTS
from .config import EstimateConfig
from .envelope import JobEnvelope, JobState
from .evidence import Claim, ClaimStatus, EvidenceSpan, NumericClaim, TypedFact, render_data_block, verify_claims
from .gate import DataUseGate
from .prompts import INSTRUCTIONS_V1, PROMPT_TEMPLATE_HASH, proposal_command_id
from .providers.base import (CircuitBreaker, GenerationRequest, GenerationResult, OutputStatus, ProviderAdapter,
                             ProviderError, RetryPolicy, SendRefused, TokenCount, local_estimate)
from .schema import SchemaError, SchemaRejected, Validator, compile_for, parse_model_json
from .tariffs import TariffBook, TariffError
from .tools import ToolBudgetExceeded, ToolGateway, ToolRegistry

PROPOSAL_SCHEMA_PATH = CONTRACTS / "research_proposal.schema.json"
PROPOSAL_SCHEMA_VERSION = 2
MAX_EVIDENCE_REFS = 50
MAX_CLAIMS = 20
MAX_CLAIM_REFS = 10          # evidence_indexes per claim (the core's limit)
MAX_SUBJECT_IDS = 20
MAX_HORIZON_S = 31_536_000
MAX_TIME_NS = 2 ** 63 - 1
EVIDENCE_REF_TYPES = frozenset({"observation", "document_span"})
# The same syntax the core enforces on evidence references (core/handlers/control.py).
_HEX64 = re.compile(r"[0-9a-f]{64}")
_DATASET_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SPAN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_REF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+#=-]{0,199}")


class CoreClientLike(Protocol):
    def command(self, command_id: str, kind: str, payload: dict, principal: dict) -> dict: ...


def load_output_schema(schema_id: str, version: int, directory: Path | None = None) -> dict:
    path = (directory or CONFIG_MODELS / "schemas") / f"{schema_id}.v{version}.json"
    return json.loads(path.read_text())


@dataclass(frozen=True)
class Candidate:
    provider: str
    model_id: str


@dataclass(frozen=True)
class JobInputs:
    task: str
    spans: tuple[EvidenceSpan, ...]
    horizon_seconds: int
    max_output_tokens: int
    facts: tuple[TypedFact, ...] = ()
    # The job's point-in-time instant (the evidence selection's as_of); None means the envelope's created_ns.
    as_of_ns: int | None = None
    # Owner-supplied instrument ids (checked against the instrument master at submission and again by the core)
    # and the research-store entity ids the evidence was selected for. Never taken from model output or span text.
    instrument_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()


@dataclass
class RunnerDeps:
    adapters: Mapping[str, ProviderAdapter]
    ledger: SpendLedger
    tariffs: TariffBook
    gate: DataUseGate
    catalog: Catalog
    core: CoreClientLike
    tools: ToolRegistry
    clock_ns: Callable[[], int]
    sleep_ns: Callable[[int], None]
    retry: RetryPolicy
    breaker: CircuitBreaker
    estimate: EstimateConfig
    is_cancelled: Callable[[str], bool] = lambda job_id: False
    schema_dir: Path | None = None
    principal_id: str = "agent.research"
    # Set by the model worker: the runner still validates the proposal and applies the discard rules, then
    # records ``proposal_ready`` and leaves delivery to the worker's durable outbox instead of calling the core.
    defer_submission: bool = False
    # Called with every record as it is made (the worker appends it to the durable audit trail). An exception
    # here ends the job, so nothing is sent to a provider after a record that could not be audited.
    on_record: Callable[[dict], None] | None = None


@dataclass
class JobResult:
    job_id: str
    status: JobState
    reason: str
    proposal: dict | None = None
    submitted: dict | None = None
    submission_error: str | None = None
    discarded_output_hash: str | None = None
    verdicts: list = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    turns_used: int = 0
    tool_calls: int = 0

    @property
    def abstained(self) -> bool:
        return bool(self.proposal and self.proposal["abstain"])


class _Terminal(Exception):
    def __init__(self, status: JobState, reason: str, proposal: dict | None = None):
        super().__init__(reason)
        self.status, self.reason, self.proposal = status, reason, proposal


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 12] + " [truncated]"


@dataclass(frozen=True)
class Subject:
    """The typed fields of a version-2 proposal that come from the job, never from the model: the as-of instant,
    the instrument and research-entity ids, and the horizon."""
    as_of_ns: int
    instrument_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    horizon_seconds: int | None = None


def _ids_problem(ids: Any, what: str) -> str | None:
    if not isinstance(ids, (tuple, list)) or len(ids) > MAX_SUBJECT_IDS:
        return f"{what} must be at most {MAX_SUBJECT_IDS} identifiers"
    try:
        for i in ids:
            validate_id(i, what)
    except InvalidIdentifier as exc:
        return str(exc)
    return f"{what} are not unique" if len(set(ids)) != len(ids) else None


def job_subject(env: JobEnvelope, inputs: JobInputs, now_ns: int) -> tuple[Subject, str | None]:
    """The job's subject, plus a problem when the inputs are malformed. The subject then keeps only well-formed
    values, so even the abstention that reports the problem matches the contract."""
    problems = []
    as_of = env.created_ns if inputs.as_of_ns is None else inputs.as_of_ns
    if type(as_of) is not int or not 0 <= as_of <= MAX_TIME_NS:
        problems.append("as_of_ns must be a non-negative integer")
        as_of = env.created_ns
    if as_of > now_ns:
        problems.append("as_of_ns is in the future")
        as_of = max(0, now_ns)
    ids = {}
    for what, value in (("instrument_ids", inputs.instrument_ids), ("entity_ids", inputs.entity_ids)):
        problem = _ids_problem(value, what)
        if problem:
            problems.append(problem)
        ids[what] = () if problem else tuple(value)
    horizon = inputs.horizon_seconds
    if type(horizon) is not int or not 1 <= horizon <= MAX_HORIZON_S:
        problems.append("horizon_seconds out of range")
        horizon = None
    return Subject(as_of, ids["instrument_ids"], ids["entity_ids"], horizon), "; ".join(problems) or None


def abstention(job_id: str, subject: Subject, reason: str) -> dict:
    return {"schema_version": PROPOSAL_SCHEMA_VERSION, "job_id": job_id, "as_of_ns": subject.as_of_ns, "hypothesis": "",
            "claims": [], "evidence": [], "instrument_ids": list(subject.instrument_ids),
            "entity_ids": list(subject.entity_ids), "observation_window": None, "horizon_seconds": subject.horizon_seconds,
            "assumptions": [], "forecast": None, "abstain": True,
            "reason": _cut(reason, 2000) if reason.strip() else "abstained without a stated reason"}


def evidence_ref(span: EvidenceSpan, as_of_ns: int) -> dict | None:
    """The typed reference of a cited span, built only from the span's provenance fields (which the evidence
    builder took from the store row), never from its text. None when the span has no usable provenance or when
    any time is after the job's as-of instant. Mirrors the core's checks, so one odd span cannot get a whole
    proposal rejected."""
    if not span.typed or span.ref_type not in EVIDENCE_REF_TYPES:
        return None
    observed, available = span.observed_ns, span.available_ns
    if not (_SPAN_ID.fullmatch(span.span_id) and _DATASET_ID.fullmatch(span.dataset_id) and _REF_ID.fullmatch(span.ref_id)
            and _HEX64.fullmatch(span.content_hash)):
        return None
    if not 0 <= available <= as_of_ns:
        return None
    if observed is not None and (type(observed) is not int or not 0 <= observed <= as_of_ns):
        return None
    return {"span_id": span.span_id, "dataset_id": span.dataset_id, "ref_type": span.ref_type, "ref_id": span.ref_id,
            "content_hash": span.content_hash, "observed_at_ns": observed, "available_at_ns": available}


def build_proposal(job_id: str, parsed: Mapping[str, Any], verdicts, subject: Subject,
                   spans: Sequence[EvidenceSpan] = (), notes: Sequence[str] = ()) -> dict:
    """A research_proposal v2. The model contributes only text (hypothesis, claim text and quote, assumptions,
    reason); every typed field comes from ``subject`` and from the provenance of the spans its verified claims
    cite. A claim becomes evidence only when it is verified AND every span it cites yields a typed reference
    (provenance present, no time after the as-of, room under the reference cap). Verification needs the quote in
    only one cited span, so keeping a claim with some of its spans dropped could attribute the quote to a span
    that does not contain it, or hide that it rests on a span from after the as-of."""
    by_id = {s.span_id: s for s in spans}
    refs: list[dict] = []
    index_of: dict[tuple, int] = {}
    claims: list[dict] = []
    assumptions = [_cut(a, 500) for a in parsed.get("assumptions", [])]
    for v in verdicts:
        text = v.claim.text
        if v.status is not ClaimStatus.VERIFIED:
            assumptions.append(_cut(f"[{v.status.value} claim, not evidence] {text}", 500))
            continue
        if len(claims) >= MAX_CLAIMS or not text.strip():
            assumptions.append(_cut(f"[verified claim not carried as evidence] {text}", 500))
            continue
        cited_refs = []
        for sid in dict.fromkeys(v.claim.span_ids):
            span = by_id.get(sid)
            cited_refs.append(evidence_ref(span, subject.as_of_ns) if span is not None else None)
        keys = [None if r is None else (r["dataset_id"], r["ref_type"], r["ref_id"]) for r in cited_refs]
        new = {k for k in keys if k is not None and k not in index_of}
        if not 1 <= len(cited_refs) <= MAX_CLAIM_REFS or None in keys or len(refs) + len(new) > MAX_EVIDENCE_REFS:
            assumptions.append(_cut(f"[verified claim without typed evidence provenance, not evidence] {text}", 500))
            continue
        cited = []
        for ref, key in zip(cited_refs, keys):
            if key not in index_of:
                index_of[key] = len(refs)
                refs.append(ref)
            if index_of[key] not in cited:
                cited.append(index_of[key])
        quote = v.claim.quote if v.claim.quote and v.claim.quote.strip() else None
        claims.append({"text": _cut(text, 1000), "quote": _cut(quote, 1000) if quote else None,
                       "evidence_indexes": cited})
    assumptions.extend(_cut(n, 500) for n in notes)
    times = [r["available_at_ns"] if r["observed_at_ns"] is None else r["observed_at_ns"] for r in refs]
    window = {"from_ns": min(times), "to_ns": max(times)} if times else None
    hypothesis = parsed.get("hypothesis", "")
    reason = parsed.get("reason", "")
    if parsed.get("abstain"):
        abstain = True
    elif not claims:
        abstain, reason = True, "no claim had verified evidence with typed provenance; " + reason
    elif not hypothesis.strip() or subject.horizon_seconds is None:
        abstain, reason = True, "no hypothesis or horizon was stated; " + reason
    else:
        abstain = False
    if abstain and not reason.strip():
        reason = "the model abstained without giving a reason"
    return {"schema_version": PROPOSAL_SCHEMA_VERSION, "job_id": job_id, "as_of_ns": subject.as_of_ns,
            "hypothesis": hypothesis, "claims": claims, "evidence": refs, "instrument_ids": list(subject.instrument_ids),
            "entity_ids": list(subject.entity_ids), "observation_window": window,
            "horizon_seconds": subject.horizon_seconds, "assumptions": assumptions[:30],
            # Model answers carry no calibration record (book Ch. 4), so the agent layer never emits a forecast block.
            "forecast": None, "abstain": abstain, "reason": _cut(reason, 2000)}


def _claims(parsed: Mapping[str, Any]) -> list[Claim]:
    out = []
    for c in parsed.get("claims", []):
        out.append(Claim(c["text"], tuple(c["span_ids"]), c["quote"],
                         tuple(NumericClaim(n["concept"], n["period"], n["unit"], n["value"]) for n in c["numbers"])))
    return out


class JobRunner:
    def __init__(self, deps: RunnerDeps):
        self.d = deps
        self._proposal_validator = Validator(json.loads(PROPOSAL_SCHEMA_PATH.read_text()))

    # ------------------------------------------------------------------ helpers
    def _rec(self, records: list, kind: str, **detail: Any) -> None:
        rec = {"kind": kind, "at_ns": self.d.clock_ns(), **detail}
        records.append(rec)
        if self.d.on_record is not None:
            self.d.on_record(rec)

    def _check_live(self, env: JobEnvelope) -> None:
        if self.d.is_cancelled(env.job_id):
            raise _Terminal(JobState.CANCELLED_BY_OWNER, "cancelled by owner")
        if self.d.clock_ns() >= env.deadline_ns:
            raise _Terminal(JobState.DEADLINE_EXPIRED, "deadline reached")

    def _settle(self, res: Reservation, result: GenerationResult, records: list) -> Money:
        if result.usage_complete and result.usages:
            try:
                cost = self.d.tariffs.cost_all(result.usages, at_ns=res.created_ns)
                self.d.ledger.settle(res, cost, evidence=result.request_id or result.raw_sha256)
                self._rec(records, "settled", reservation=res.reservation_id, actual=str(cost), reserved=str(res.amount))
                return cost
            except (TariffError, BudgetError) as exc:
                note = f"cost not computable exactly: {exc}"
        else:
            note = "usage object unavailable or incomplete"
        self.d.ledger.mark_unconfirmed(res, note=note)
        self._rec(records, "booked_at_reservation", reservation=res.reservation_id, amount=str(res.amount), note=note)
        return res.amount

    def _submit(self, env: JobEnvelope, proposal: dict, result: JobResult) -> None:
        errors = self._proposal_validator.errors(proposal)
        if errors:
            raise AssertionError(f"runner built an invalid proposal: {errors}")
        if self.d.is_cancelled(env.job_id) or self.d.clock_ns() >= env.deadline_ns:
            result.discarded_output_hash = content_hash(proposal)
            result.status = JobState.CANCELLED_BY_OWNER if self.d.is_cancelled(env.job_id) else JobState.DEADLINE_EXPIRED
            result.reason = "output discarded: " + result.status.value
            self._rec(result.records, "discarded", output_hash=result.discarded_output_hash)
            return
        if self.d.defer_submission:
            self._rec(result.records, "proposal_ready", command_id=proposal_command_id(env.job_id),
                      output_hash=content_hash(proposal))
            return
        try:
            result.submitted = self.d.core.command(
                proposal_command_id(env.job_id), "submit_research_proposal", {"proposal": proposal},
                {"id": self.d.principal_id, "role": "AGENT"})
            self._rec(result.records, "submitted", output_hash=content_hash(proposal))
        except Exception as exc:  # the core is the authority; record and surface, never retry blindly
            result.submission_error = f"{type(exc).__name__}: {exc}"
            self._rec(result.records, "submission_failed", error=result.submission_error)

    def _routable(self, env: JobEnvelope, c: Candidate, records: list) -> ProviderAdapter | None:
        adapter = self.d.adapters.get(c.provider)
        reason = None
        if adapter is None:
            reason = "no_adapter"
        else:
            reason = adapter.admission_problem()
            if reason is None:
                missing = adapter.unverified_requirements("generate")
                if missing:
                    reason = "unverified_protocol_facts:" + ",".join(missing)
            if reason is None:
                ok, why = self.d.catalog.routable(c.provider, c.model_id, env.role)
                reason = None if ok else f"catalog:{why}"
            if reason is None and not self.d.tariffs.is_routable(c.provider, c.model_id, at_ns=self.d.clock_ns(),
                                                                 region=adapter.requested_region() or "global"):
                reason = "tariff:no_approved_verified_row"
        if reason is not None:
            self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason=reason)
            return None
        return adapter

    def _count(self, env: JobEnvelope, adapter: ProviderAdapter, req: GenerationRequest, dataset_ids, records) -> TokenCount:
        if not adapter.unverified_requirements("count_tokens"):
            decision = self.d.gate.evaluate(provider=adapter.provider_id, model_id=req.model_id, feature="count_tokens",
                                            dataset_ids=dataset_ids, request_hash=req.request_hash())
            self._rec(records, "gate", feature="count_tokens", allow=decision.allow, reasons=list(decision.reasons))
            if decision.allow:
                try:
                    adm = self.d.ledger.reserve(job_id=env.job_id, role=env.role, provider=adapter.provider_id,
                                                model_id=req.model_id, amount=Money.zero(self.d.ledger.currency),
                                                job_cap=env.spend_cap, purpose="count_tokens")
                except BudgetError as exc:
                    raise _Terminal(JobState.BUDGET_EXHAUSTED, f"count admission refused: {exc}") from None
                try:
                    tc = adapter.count_tokens(req, admission=adm, gate=decision)
                    self.d.ledger.settle(adm, Money.zero(self.d.ledger.currency), evidence="count_tokens")
                    self._rec(records, "counted", method=tc.method, input_tokens=tc.input_tokens)
                    return tc
                except ProviderError as exc:
                    if exc.sent:
                        self.d.ledger.mark_unconfirmed(adm, note=f"count failed: {exc.cls.value}")
                    else:
                        self.d.ledger.release(adm)
                    self._rec(records, "count_failed", error=exc.record())
                except (SendRefused, NotImplementedError) as exc:
                    self.d.ledger.release(adm, note=f"count not sent: {exc}")
                    self._rec(records, "count_not_sent", reason=str(exc))
                except Exception:
                    # Not classified by the adapter: the request may have left, so the admission stays UNCONFIRMED.
                    if self.d.ledger.is_open(adm):
                        self.d.ledger.mark_unconfirmed(adm, note="unclassified error during token counting; outcome unknown")
                    raise
        tc = local_estimate(req, self.d.estimate)
        self._rec(records, "counted", method=tc.method, input_tokens=tc.input_tokens)
        return tc

    def _worst_case(self, adapter: ProviderAdapter, req: GenerationRequest, prompt_tokens: int, max_out: int) -> Money:
        region = adapter.requested_region() or "global"
        at = self.d.clock_ns()
        total = self.d.tariffs.worst_case(adapter.provider_id, req.model_id, at_ns=at, prompt_tokens=prompt_tokens,
                                          max_output_tokens=max_out, region=region,
                                          cache_write_ttls=adapter.cache_write_ttls(req))
        extra = [self.d.tariffs.worst_case(adapter.provider_id, m, at_ns=at, prompt_tokens=prompt_tokens,
                                           max_output_tokens=max_out, region=region)
                 for m in adapter.additional_billed_models(req)]
        if extra:
            total = total + max(extra, key=lambda m: m.amount)
        return total

    # ------------------------------------------------------------------ main entry
    def run(self, env: JobEnvelope, inputs: JobInputs, candidates: Sequence[Candidate]) -> JobResult:
        result = JobResult(env.job_id, JobState.PROVIDER_UNAVAILABLE, "")
        subject, problem = job_subject(env, inputs, self.d.clock_ns())
        try:
            proposal = self._run(env, inputs, candidates, result, subject, problem)
            result.status, result.reason, result.proposal = JobState.SUCCEEDED, "ok", proposal
        except _Terminal as t:
            result.status, result.reason = t.status, t.reason
            result.proposal = t.proposal
            if result.proposal is None and t.status not in (JobState.CANCELLED_BY_OWNER, JobState.DEADLINE_EXPIRED):
                result.proposal = abstention(env.job_id, subject, f"{t.status.value}: {t.reason}")
        self._rec(result.records, "terminal", status=result.status.value, reason=result.reason)
        if result.proposal is not None:
            self._submit(env, result.proposal, result)
        return result

    def _run(self, env: JobEnvelope, inputs: JobInputs, candidates: Sequence[Candidate], result: JobResult,
             subject: Subject, subject_problem: str | None) -> dict:
        d, records = self.d, result.records
        self._check_live(env)
        if env.prompt_template_hash is not None and env.prompt_template_hash != PROMPT_TEMPLATE_HASH:
            raise _Terminal(JobState.DENIED_BY_POLICY, "envelope prompt template hash does not match the runner template")
        try:
            schema = load_output_schema(env.output_schema, env.schema_version, d.schema_dir)
        except (OSError, ValueError) as exc:
            raise _Terminal(JobState.DENIED_BY_POLICY, f"unknown output schema {env.output_schema} v{env.schema_version}: {exc}") from None
        if not d.ledger.paid_calls_enabled():
            raise _Terminal(JobState.BUDGET_EXHAUSTED, "paid_calls_disabled: no owner budget policy (~/.unknown/config/agents.json)")
        if subject_problem is not None:
            raise _Terminal(JobState.DENIED_BY_POLICY, subject_problem)
        span_hashes = {s.content_hash for s in inputs.spans if s.content_hash}
        if span_hashes and not span_hashes <= set(env.input_hashes):
            raise _Terminal(JobState.DENIED_BY_POLICY, "evidence spans carry content hashes not listed in the envelope")
        dataset_ids = {s.dataset_id for s in inputs.spans}
        blocks = []
        for s in inputs.spans:
            b = render_data_block(kind="evidence_span", block_id=s.span_id, dataset_id=s.dataset_id, body=s.text)
            if b.flags:
                self._rec(records, "injection_screen", span_id=s.span_id, flags=list(b.flags))
            blocks.append(b.text)
        gateway = ToolGateway(d.tools, env, rights_ok=d.gate.dataset_allowed)
        tool_lines = [f"- {n}" for n in env.allowed_tools if d.tools.get(n)]
        tool_results: list[str] = []
        turns_used = tokens_committed = retries_used = 0
        any_routable = False
        policy_causes: list[bool] = []  # per routable candidate: did it end on a policy refusal (gate/tariff/circuit)?
        for c in candidates:
            adapter = self._routable(env, c, records)
            if adapter is None:
                continue
            try:
                compiled = compile_for(c.provider, schema)
            except (SchemaRejected, SchemaError) as exc:
                self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason=f"schema:{exc}")
                continue
            any_routable = True
            key = f"{c.provider}/{c.model_id}"
            max_out = inputs.max_output_tokens
            truncation_retry_used = False
            while True:
                self._check_live(env)
                if turns_used >= env.max_turns:
                    raise _Terminal(JobState.EXCEEDED_TURNS, f"max_turns={env.max_turns} reached")
                user = "\n\n".join([
                    "TASK:\n" + inputs.task,
                    "AVAILABLE TOOLS:\n" + ("\n".join(tool_lines) if tool_lines else "none"),
                    "EVIDENCE:\n" + ("\n".join(blocks) if blocks else "none"),
                    *(["TOOL RESULTS:\n" + "\n".join(tool_results)] if tool_results else []),
                ])
                effort = adapter.effort_for(env.role) if hasattr(adapter, "effort_for") else None
                req = GenerationRequest(env.job_id, env.role, c.model_id, INSTRUCTIONS_V1, user, compiled, max_out, effort)
                tc = self._count(env, adapter, req, dataset_ids, records)
                row = d.catalog.last_good.get(c.provider)
                limit = row.rows[c.model_id].max_input_tokens if row and c.model_id in row.rows else None
                if limit is not None and tc.input_tokens + max_out > limit:
                    raise _Terminal(JobState.DENIED_BY_POLICY, f"context_overflow: {tc.input_tokens}+{max_out} > {limit}")
                if tokens_committed + tc.input_tokens + max_out > env.token_cap:
                    raise _Terminal(JobState.BUDGET_EXHAUSTED, f"token_cap {env.token_cap} would be exceeded")
                try:
                    worst = self._worst_case(adapter, req, tc.input_tokens, max_out)
                except TariffError as exc:
                    self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason=f"tariff:{exc}")
                    policy_causes.append(True)
                    break
                decision = d.gate.evaluate(provider=c.provider, model_id=c.model_id, feature="generate",
                                           dataset_ids=dataset_ids, request_hash=req.request_hash())
                self._rec(records, "gate", feature="generate", allow=decision.allow, reasons=list(decision.reasons),
                          decision_id=decision.decision_id, request_hash=decision.request_hash)
                if not decision.allow:
                    self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason="gate_denied")
                    policy_causes.append(True)
                    break
                if not d.breaker.allow(key, d.clock_ns()):
                    self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason="circuit_open")
                    policy_causes.append(False)
                    break
                try:
                    res = d.ledger.reserve(job_id=env.job_id, role=env.role, provider=c.provider, model_id=c.model_id,
                                           amount=worst, job_cap=env.spend_cap, purpose="generate")
                except BudgetError as exc:
                    raise _Terminal(JobState.BUDGET_EXHAUSTED, str(exc)) from None
                try:
                    self._rec(records, "reserved", reservation=res.reservation_id, amount=str(worst),
                              prompt_tokens=tc.input_tokens, max_output_tokens=max_out, count_method=tc.method)
                except BaseException:
                    d.ledger.release(res, note="not sent: the reservation could not be recorded in the audit trail")
                    raise
                turns_used += 1
                result.turns_used = turns_used
                try:
                    gen = adapter.generate_structured(req, reservation=res, gate=decision)
                except (SendRefused, NotImplementedError) as exc:
                    d.ledger.release(res, note=f"not sent: {exc}")
                    self._rec(records, "not_sent", provider=c.provider, reason=str(exc))
                    turns_used -= 1
                    result.turns_used = turns_used
                    policy_causes.append(False)
                    break
                except ProviderError as exc:
                    if exc.sent:
                        d.ledger.mark_unconfirmed(res, note=f"{exc.cls.value}: billing of errors is not documented (CAP-MODEL-06)")
                    else:
                        d.ledger.release(res, note=f"not sent: {exc.cls.value}")
                    tokens_committed += tc.input_tokens + max_out
                    d.breaker.record_failure(key, exc.cls, d.clock_ns())
                    self._rec(records, "provider_error", error=exc.record())
                    if exc.cls.value == "QUOTA_OR_SPEND_EXHAUSTED":
                        raise _Terminal(JobState.BUDGET_EXHAUSTED, f"provider spend/quota limit: {exc.message}") from None
                    if not exc.transient:
                        raise _Terminal(JobState.PROVIDER_UNAVAILABLE, f"{exc.cls.value}: {exc.message}") from None
                    delay = None
                    if retries_used < env.retry_budget:
                        delay = d.retry.delay_ns(retries_used, exc, now_ns=d.clock_ns(), deadline_ns=env.deadline_ns)
                    if delay is not None and d.breaker.allow(key, d.clock_ns() + delay):
                        retries_used += 1
                        self._rec(records, "retry", attempt=retries_used, delay_ns=delay)
                        d.sleep_ns(delay)
                        continue
                    self._rec(records, "fallback", from_provider=c.provider, cause=exc.cls.value)
                    policy_causes.append(False)
                    break
                except Exception:
                    # An error the adapter could not classify (e.g. an unexpected SDK exception). The request may have
                    # been sent, so the reservation stays consumed as UNCONFIRMED until reconciled (unknown outcomes
                    # stay reserved), and the exception propagates to the caller.
                    if d.ledger.is_open(res):
                        d.ledger.mark_unconfirmed(res, note="unclassified error during the provider call; outcome unknown")
                    raise
                d.breaker.record_success(key)
                cost = self._settle(res, gen, records)
                used = sum(u.prompt_tokens + u.output_tokens for u in gen.usages) if gen.usage_complete else tc.input_tokens + max_out
                tokens_committed += used
                self._rec(records, "generated", status=gen.status.value, request_id=gen.request_id, returned_model=gen.returned_model_id,
                          served_by_fallback=gen.served_by_fallback, cost=str(cost), raw_sha256=gen.raw_sha256)
                notes = []
                if gen.returned_model_id is not None and gen.returned_model_id != c.model_id:
                    if gen.served_by_fallback and gen.returned_model_id in adapter.additional_billed_models(req):
                        notes.append(f"served by server-side refusal fallback model {gen.returned_model_id} (not {c.model_id})")
                    else:
                        raise _Terminal(JobState.DENIED_BY_POLICY,
                                        f"returned model {gen.returned_model_id} differs from pinned {c.model_id}; output quarantined")
                requested_geo = adapter.requested_region()
                if gen.inference_geo is not None and requested_geo is not None and gen.inference_geo != requested_geo:
                    raise _Terminal(JobState.DENIED_BY_POLICY,
                                    f"inference_geo {gen.inference_geo} differs from requested {requested_geo}; output quarantined")
                if gen.status is OutputStatus.REFUSED:
                    raise _Terminal(JobState.SUCCEEDED, f"model_refused:{gen.refusal_category}",
                                    abstention(env.job_id, subject, f"model refused (category {gen.refusal_category})"))
                if gen.status is OutputStatus.TRUNCATED:
                    if not truncation_retry_used:
                        truncation_retry_used = True
                        max_out = max_out * 2
                        self._rec(records, "truncated_retry", new_max_output_tokens=max_out)
                        continue
                    raise _Terminal(JobState.SCHEMA_INVALID, "output truncated twice")
                try:
                    parsed = parse_model_json(gen.text or "")
                except (ValueError, TypeError) as exc:
                    raise _Terminal(JobState.SCHEMA_INVALID, f"output is not JSON: {exc}") from None
                errors = compiled.local.errors(parsed)
                if errors:
                    self._rec(records, "schema_invalid", errors=errors[:20])
                    raise _Terminal(JobState.SCHEMA_INVALID, "; ".join(errors[:5]))
                requests = parsed.get("tool_requests") or []
                if requests:
                    for i, tr in enumerate(requests):
                        try:
                            args = json.loads(tr["arguments_json"], parse_float=Decimal)
                        except ValueError:
                            args = None
                        try:
                            out = gateway.call(tr["name"], args, call_id=f"{env.job_id}.t{turns_used}.{i}")
                        except ToolBudgetExceeded as exc:
                            result.tool_calls = gateway.attempts
                            raise _Terminal(JobState.EXCEEDED_TOOL_CALLS, str(exc)) from None
                        result.tool_calls = gateway.attempts
                        self._rec(records, "tool_call", tool=tr["name"], decision=out.decision.value, rule_id=out.rule_id)
                        tool_results.append(out.content)
                    continue
                verdicts = verify_claims(_claims(parsed), inputs.spans, inputs.facts)
                result.verdicts = verdicts
                self._rec(records, "verified", statuses=[v.status.value for v in verdicts])
                return build_proposal(env.job_id, parsed, verdicts, subject, inputs.spans, notes)
        if not any_routable:
            raise _Terminal(JobState.DENIED_BY_POLICY, "no candidate is routable (see candidate_skipped records)")
        if policy_causes and all(policy_causes):
            raise _Terminal(JobState.DENIED_BY_POLICY, "every routable candidate was refused by the gate or tariff policy")
        raise _Terminal(JobState.PROVIDER_UNAVAILABLE, "every routable candidate failed or was refused")
