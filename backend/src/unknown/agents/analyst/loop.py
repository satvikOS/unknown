"""The analyst's tool-use loop: one job = one owner question, answered in at most ``max_turns`` streamed model turns.

Every turn goes through the agent layer's controls in the research runner's order (``runner.py``):
  routability (adapter admission, catalog approval for the role, approved tariff) -> data-use gate (count) -> token count
  (provider endpoint, else the conservative local estimate) -> context fit and the envelope's token cap -> worst case
  (tariffs, including the 5-minute cache-write multiplier) -> data-use gate (generate, bound to the request hash and
  covering every dataset already in the context) -> circuit breaker -> ledger reservation against every owner cap
  (per job, day, month, lifetime, role, provider) -> streamed call -> settlement at the exact tariff cost (or the full
  reservation kept UNCONFIRMED when usage is incomplete, the stream was cancelled or the error may have been billed).
Tool calls go through the default-deny ``ToolGateway`` (envelope allowlist, tool-call budget, argument schema, dataset
rights); tool results enter the context only as untrusted data blocks behind a random nonce, whose ids (R1, R2, ...)
are the answer's citation refs. A cancel or the deadline stops the job at the next stream event; output produced after
a cancel is discarded. The final answer is checked by ``citations.check_answer`` and labelled AI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ...domain.fixed import Fixed
from ...domain.money import Money
from ..budget import BudgetError, Reservation, SpendLedger
from ..catalog import Catalog
from ..config import EstimateConfig
from ..envelope import JobEnvelope, JobState
from ..gate import DataUseGate
from ..providers.base import CircuitBreaker, ProviderError, RetryPolicy, SendRefused
from ..runner import Candidate
from ..tariffs import TariffBook, TariffError
from ..tools import Decision, ToolBudgetExceeded, ToolGateway, ToolOutput, ToolRegistry, ToolSpec
from .citations import check_answer
from .prompt import SYSTEM_PROMPT
from .stream_client import CACHE_TTL, AnalystRequest, StreamingClaude, TurnResult
from .toolspec import PLATFORM_DATASET, TOOL_DEFS

TEXT_FLUSH_CHARS = 400
TEXT_FLUSH_NS = 150_000_000
STOP_POLL_NS = 250_000_000
LABEL = "AI-generated, advisory only: no order, session or money movement can follow from it."


class _Stop(Exception):
    def __init__(self, status: JobState, reason: str, answer_status: str = "failed"):
        super().__init__(reason)
        self.status, self.reason, self.answer_status = status, reason, answer_status


@dataclass(frozen=True)
class AskContext:
    question: str
    security: str | None
    history: tuple[dict, ...]
    history_datasets: tuple[str, ...]
    max_output_tokens: int
    now_iso: str


@dataclass
class LoopDeps:
    clients: Mapping[str, StreamingClaude]
    ledger: SpendLedger
    tariffs: TariffBook
    gate: DataUseGate
    catalog: Catalog
    tools: ToolRegistry
    clock_ns: Callable[[], int]
    sleep_ns: Callable[[int], None]
    retry: RetryPolicy
    breaker: CircuitBreaker
    estimate: EstimateConfig
    is_cancelled: Callable[[str], bool]
    on_record: Callable[[dict], None]
    emit: Callable[[str, dict], None]
    save_result: Callable[..., None]


@dataclass
class AskResult:
    status: JobState
    reason: str
    answer: dict
    records: list[dict] = field(default_factory=list)


def user_message(ctx: AskContext) -> str:
    lines = [f"Question: {ctx.question}"]
    if ctx.security:
        lines.append(f"Linked security in the terminal: {ctx.security} (symbol before the dot, market identifier after it)")
    lines.append(f"Current time (UTC): {ctx.now_iso}")
    return "\n".join(lines)


def conversation(ctx: AskContext) -> list[dict]:
    msgs: list[dict] = []
    for turn in ctx.history:
        msgs.append({"role": "user", "content": f"Question: {turn['question']}"})
        msgs.append({"role": "assistant", "content": turn["answer"]})
    msgs.append({"role": "user", "content": user_message(ctx)})
    return msgs


def _local_estimate(req: AnalystRequest, est: EstimateConfig) -> int:
    scaled = Fixed(req.payload_bytes()) * est.bytes_per_token_factor
    return -((-scaled.mantissa) // (10 ** scaled.scale)) + est.overhead_tokens


class AnalystRunner:
    def __init__(self, deps: LoopDeps):
        self.d = deps

    # ------------------------------------------------------------------ helpers
    def _rec(self, records: list, kind: str, **detail: Any) -> None:
        rec = {"kind": kind, "at_ns": self.d.clock_ns(), **detail}
        records.append(rec)
        self.d.on_record(rec)

    def _check_live(self, env: JobEnvelope) -> None:
        if self.d.is_cancelled(env.job_id):
            raise _Stop(JobState.CANCELLED_BY_OWNER, "cancelled by owner", "cancelled")
        if self.d.clock_ns() >= env.deadline_ns:
            raise _Stop(JobState.DEADLINE_EXPIRED, "deadline reached", "failed")

    def _routable(self, env: JobEnvelope, c: Candidate, records: list) -> StreamingClaude | None:
        client = self.d.clients.get(c.provider)
        reason = None
        if client is None:
            reason = "no_adapter"
        else:
            reason = client.admission_problem()
            if reason is None:
                ok, why = self.d.catalog.routable(c.provider, c.model_id, env.role)
                reason = None if ok else f"catalog:{why}"
            if reason is None and not self.d.tariffs.is_routable(c.provider, c.model_id, at_ns=self.d.clock_ns(),
                                                                 region=client.requested_region() or "global"):
                reason = "tariff:no_approved_verified_row"
        if reason is not None:
            self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason=reason)
            return None
        return client

    def _count(self, env: JobEnvelope, client: StreamingClaude, req: AnalystRequest, datasets, records) -> tuple[int, str]:
        decision = self.d.gate.evaluate(provider=client.provider_id, model_id=req.model_id, feature="count_tokens",
                                        dataset_ids=datasets, request_hash=req.request_hash())
        self._rec(records, "gate", feature="count_tokens", allow=decision.allow, reasons=list(decision.reasons))
        if decision.allow:
            try:
                adm = self.d.ledger.reserve(job_id=env.job_id, role=env.role, provider=client.provider_id,
                                            model_id=req.model_id, amount=Money.zero(self.d.ledger.currency),
                                            job_cap=env.spend_cap, purpose="count_tokens")
            except BudgetError as exc:
                raise _Stop(JobState.BUDGET_EXHAUSTED, f"count admission refused: {exc}") from None
            try:
                tokens = client.count_tokens(req, admission=adm, gate=decision)
                self.d.ledger.settle(adm, Money.zero(self.d.ledger.currency), evidence="count_tokens")
                self._rec(records, "counted", method="provider_endpoint", input_tokens=tokens)
                return tokens, "provider_endpoint"
            except ProviderError as exc:
                if exc.sent:
                    self.d.ledger.mark_unconfirmed(adm, note=f"count failed: {exc.cls.value}")
                else:
                    self.d.ledger.release(adm)
                self._rec(records, "count_failed", error=exc.record())
            except SendRefused as exc:
                self.d.ledger.release(adm, note=f"count not sent: {exc}")
                self._rec(records, "count_not_sent", reason=str(exc))
            except Exception:
                if self.d.ledger.is_open(adm):
                    self.d.ledger.mark_unconfirmed(adm, note="unclassified error during token counting; outcome unknown")
                raise
        tokens = _local_estimate(req, self.d.estimate)
        self._rec(records, "counted", method="local_estimate", input_tokens=tokens)
        return tokens, "local_estimate"

    def _settle(self, res: Reservation, turn: TurnResult, records: list) -> tuple[Money, bool]:
        if turn.usage_complete and turn.usages:
            try:
                cost = self.d.tariffs.cost_all(turn.usages, at_ns=res.created_ns)
                self.d.ledger.settle(res, cost, evidence=turn.request_id or turn.raw_sha256)
                self._rec(records, "settled", reservation=res.reservation_id, actual=str(cost), reserved=str(res.amount))
                return cost, True
            except (TariffError, BudgetError) as exc:
                note = f"cost not computable exactly: {exc}"
        else:
            note = "usage object unavailable or incomplete"
        self.d.ledger.mark_unconfirmed(res, note=note)
        self._rec(records, "booked_at_reservation", reservation=res.reservation_id, amount=str(res.amount), note=note)
        return res.amount, False

    def _registry_capturing(self) -> tuple[ToolRegistry, dict]:
        """The job's registry with each handler wrapped so the loop learns which datasets a result came from."""
        seen: dict = {}
        reg = ToolRegistry()
        for name in sorted(self.d.tools.names()):
            spec, _ = self.d.tools.get(name)

            def handler(args: dict, _h=spec.handler) -> ToolOutput:
                out = _h(args)
                seen["last"] = out
                return out
            reg.register(ToolSpec(spec.name, spec.description, spec.input_schema, handler, spec.read_only))
        return reg, seen

    # ------------------------------------------------------------------ entry
    def run(self, env: JobEnvelope, ctx: AskContext, candidates: Sequence[Candidate]) -> AskResult:
        state = _State(ctx)
        try:
            status, reason = self._run(env, ctx, candidates, state)
        except _Stop as s:
            status, reason = s.status, s.reason
            state.answer_status = s.answer_status
            if s.status is JobState.CANCELLED_BY_OWNER:
                state.final_text = None            # output after a cancel is discarded, never shown as an answer
        self._rec(state.records, "terminal", status=status.value, reason=reason)
        return AskResult(status, reason, self._answer(state, status, reason), state.records)

    def _answer(self, s: "_State", status: JobState, reason: str) -> dict:
        text = s.final_text
        check = check_answer(text, {r: v["content"] for r, v in s.results.items()}) if text else None
        cost = s.cost
        return {"status": s.answer_status,
                "label": LABEL, "text": text, "reason": None if status is JobState.SUCCEEDED else reason,
                "check": check, "citations": [{k: v for k, v in r.items() if k != "content"} for r in s.results.values()],
                "model": s.model_id, "effort": s.effort, "turns": s.turns, "tool_calls": s.tool_calls,
                "usage": dict(s.usage), "cost": cost.amount.to_str() if cost is not None else None,
                "currency": cost.currency if cost is not None else None, "cost_exact": s.cost_exact,
                "datasets": sorted(s.datasets)}

    def _run(self, env: JobEnvelope, ctx: AskContext, candidates: Sequence[Candidate], s: "_State") -> tuple[JobState, str]:
        d, records = self.d, s.records
        self._check_live(env)
        if not d.ledger.paid_calls_enabled():
            raise _Stop(JobState.BUDGET_EXHAUSTED, "paid_calls_disabled: no owner budget policy (~/.unknown/config/agents.json)")
        registry, seen = self._registry_capturing()
        gateway = ToolGateway(registry, env, rights_ok=lambda ds: ds == PLATFORM_DATASET or d.gate.dataset_allowed(ds))
        tools = tuple(t for t in TOOL_DEFS if env.allows_tool(t["name"]) and registry.get(t["name"]))
        messages = conversation(ctx)
        tokens_committed = retries_used = 0
        any_routable = False
        for c in candidates:
            client = self._routable(env, c, records)
            if client is None:
                continue
            any_routable = True
            key = f"{c.provider}/{c.model_id}"
            effort = client.effort_for(env.role)
            s.model_id, s.effort = c.model_id, effort
            while True:
                self._check_live(env)
                if s.turns >= env.max_turns:
                    raise _Stop(JobState.EXCEEDED_TURNS, f"max_turns={env.max_turns} reached before a final answer")
                req = AnalystRequest(env.job_id, env.role, c.model_id, SYSTEM_PROMPT, tools, tuple(messages),
                                     ctx.max_output_tokens, effort)
                prompt_tokens, method = self._count(env, client, req, sorted(s.datasets), records)
                snap = d.catalog.last_good.get(c.provider)
                limit = snap.rows[c.model_id].max_input_tokens if snap and c.model_id in snap.rows else None
                if limit is not None and prompt_tokens + req.max_output_tokens > limit:
                    raise _Stop(JobState.DENIED_BY_POLICY, f"context_overflow: {prompt_tokens}+{req.max_output_tokens} > {limit}")
                if tokens_committed + prompt_tokens + req.max_output_tokens > env.token_cap:
                    raise _Stop(JobState.BUDGET_EXHAUSTED, f"token_cap {env.token_cap} would be exceeded")
                try:
                    worst = d.tariffs.worst_case(c.provider, c.model_id, at_ns=d.clock_ns(), prompt_tokens=prompt_tokens,
                                                 max_output_tokens=req.max_output_tokens,
                                                 region=client.requested_region() or "global", cache_write_ttls=(CACHE_TTL,))
                except TariffError as exc:
                    self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason=f"tariff:{exc}")
                    break
                decision = d.gate.evaluate(provider=c.provider, model_id=c.model_id, feature="generate",
                                           dataset_ids=sorted(s.datasets), request_hash=req.request_hash())
                self._rec(records, "gate", feature="generate", allow=decision.allow, reasons=list(decision.reasons),
                          decision_id=decision.decision_id, request_hash=decision.request_hash)
                if not decision.allow:
                    self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason="gate_denied")
                    raise _Stop(JobState.DENIED_BY_POLICY, "the data-use gate refused the request: " + "; ".join(decision.reasons))
                if not d.breaker.allow(key, d.clock_ns()):
                    self._rec(records, "candidate_skipped", provider=c.provider, model_id=c.model_id, reason="circuit_open")
                    break
                try:
                    res = d.ledger.reserve(job_id=env.job_id, role=env.role, provider=c.provider, model_id=c.model_id,
                                           amount=worst, job_cap=env.spend_cap, purpose="generate")
                except BudgetError as exc:
                    raise _Stop(JobState.BUDGET_EXHAUSTED, str(exc)) from None
                try:
                    self._rec(records, "reserved", reservation=res.reservation_id, amount=str(worst),
                              prompt_tokens=prompt_tokens, max_output_tokens=req.max_output_tokens, count_method=method)
                except BaseException:
                    d.ledger.release(res, note="not sent: the reservation could not be recorded in the audit trail")
                    raise
                s.turns += 1
                d.emit("turn", {"turn": s.turns, "model": c.model_id, "effort": effort, "reserved": str(worst),
                                "prompt_tokens": prompt_tokens, "count_method": method})
                try:
                    turn = client.stream(req, reservation=res, gate=decision, on_text=s.text_sink(d),
                                         on_block=s.block_sink(d), should_stop=s.stop_poll(d, env))
                except SendRefused as exc:
                    d.ledger.release(res, note=f"not sent: {exc}")
                    self._rec(records, "not_sent", provider=c.provider, reason=str(exc))
                    s.turns -= 1
                    break
                except ProviderError as exc:
                    s.flush(d)
                    if exc.sent:
                        d.ledger.mark_unconfirmed(res, note=f"{exc.cls.value}: billing of errors is not documented (CAP-MODEL-06)")
                    else:
                        d.ledger.release(res, note=f"not sent: {exc.cls.value}")
                    tokens_committed += prompt_tokens + req.max_output_tokens
                    d.breaker.record_failure(key, exc.cls, d.clock_ns())
                    self._rec(records, "provider_error", error=exc.record())
                    if getattr(exc, "streamed_text", False):
                        d.emit("reset", {"turn": s.turns, "reason": exc.cls.value})
                    if exc.cls.value == "QUOTA_OR_SPEND_EXHAUSTED":
                        raise _Stop(JobState.BUDGET_EXHAUSTED, f"provider spend/quota limit: {exc.message}") from None
                    if not exc.transient:
                        raise _Stop(JobState.PROVIDER_UNAVAILABLE, f"{exc.cls.value}: {exc.message}") from None
                    delay = d.retry.delay_ns(retries_used, exc, now_ns=d.clock_ns(), deadline_ns=env.deadline_ns) \
                        if retries_used < env.retry_budget else None
                    if delay is not None and d.breaker.allow(key, d.clock_ns() + delay):
                        retries_used += 1
                        self._rec(records, "retry", attempt=retries_used, delay_ns=delay)
                        d.sleep_ns(delay)
                        continue
                    self._rec(records, "fallback", from_provider=c.provider, cause=exc.cls.value)
                    break
                except Exception:
                    if d.ledger.is_open(res):
                        d.ledger.mark_unconfirmed(res, note="unclassified error during the provider call; outcome unknown")
                    raise
                s.flush(d)
                if turn.cancelled:
                    d.ledger.mark_unconfirmed(res, note="stream closed on the owner's cancel; usage unknown, kept reserved "
                                                        "until reconciled")
                    self._rec(records, "cancelled_mid_stream", reservation=res.reservation_id, request_id=turn.request_id)
                    self._check_live(env)
                    raise _Stop(JobState.CANCELLED_BY_OWNER, "cancelled", "cancelled")
                d.breaker.record_success(key)
                cost, exact = self._settle(res, turn, records)
                s.add_usage(turn, cost, exact)
                tokens_committed += sum(u.prompt_tokens + u.output_tokens for u in turn.usages) if turn.usage_complete \
                    else prompt_tokens + req.max_output_tokens
                self._rec(records, "generated", stop_reason=turn.stop_reason, request_id=turn.request_id,
                          returned_model=turn.returned_model_id, cost=str(cost), raw_sha256=turn.raw_sha256)
                d.emit("usage", s.usage_event(turn))
                if turn.returned_model_id is not None and turn.returned_model_id != c.model_id:
                    raise _Stop(JobState.DENIED_BY_POLICY,
                                f"returned model {turn.returned_model_id} differs from pinned {c.model_id}; output quarantined")
                geo = client.requested_region()
                if turn.inference_geo is not None and geo is not None and turn.inference_geo != geo:
                    raise _Stop(JobState.DENIED_BY_POLICY, f"inference_geo {turn.inference_geo} differs from requested {geo}; "
                                                           "output quarantined")
                messages.append({"role": "assistant", "content": turn.content})
                text = "".join(b.get("text", "") for b in turn.content if b.get("type") == "text")
                tool_uses = [b for b in turn.content if b.get("type") == "tool_use"]
                if turn.stop_reason == "refusal":
                    s.final_text, s.answer_status = text or None, "refused"
                    return JobState.SUCCEEDED, "model_refused"
                if turn.stop_reason == "pause_turn":
                    continue
                if turn.stop_reason == "tool_use" and tool_uses:
                    d.emit("segment_end", {"turn": s.turns})
                    messages.append({"role": "user", "content": self._run_tools(env, gateway, seen, tool_uses, s)})
                    continue
                if turn.stop_reason == "max_tokens":
                    if tool_uses or not text:
                        raise _Stop(JobState.SCHEMA_INVALID, "the turn was truncated at max_output_tokens before an answer")
                    s.final_text, s.answer_status = text, "truncated"
                    return JobState.SUCCEEDED, "answer truncated at max_output_tokens"
                if turn.stop_reason != "end_turn":
                    raise _Stop(JobState.SCHEMA_INVALID, f"unexpected stop_reason {turn.stop_reason!r}")
                self._check_live(env)
                s.final_text, s.answer_status = text, "answered"
                return JobState.SUCCEEDED, "ok"
        if not any_routable:
            raise _Stop(JobState.DENIED_BY_POLICY, "no candidate is routable (see candidate_skipped records)")
        raise _Stop(JobState.PROVIDER_UNAVAILABLE, "every routable candidate failed or was refused")

    def _run_tools(self, env: JobEnvelope, gateway: ToolGateway, seen: dict, tool_uses: list[dict], s: "_State") -> list[dict]:
        blocks = []
        for b in tool_uses:
            s.tool_calls += 1
            ref = f"R{s.tool_calls}"
            seen.pop("last", None)
            self.d.emit("tool", {"ref": ref, "tool": b.get("name"), "arguments": b.get("input")})
            try:
                out = gateway.call(b.get("name"), b.get("input"), call_id=ref)
            except ToolBudgetExceeded as exc:
                raise _Stop(JobState.EXCEEDED_TOOL_CALLS, str(exc)) from None
            produced: ToolOutput | None = seen.get("last")
            datasets: list[str] = []
            status = "denied"
            if out.decision is Decision.EXECUTED and out.rule_id == "allowed" and produced is not None:
                datasets = [x for x in (produced.dataset_id, *produced.also_from) if x != PLATFORM_DATASET]
                s.datasets.update(datasets)
                try:
                    status = json.loads(produced.text).get("status") or "ok"
                except (ValueError, AttributeError):
                    status = "ok"
            elif out.rule_id == "rights_redacted":
                status = "withheld"
            self._rec(s.records, "tool_call", tool=b.get("name"), ref=ref, decision=out.decision.value, rule_id=out.rule_id,
                      datasets=datasets, status=status)
            entry = {"ref": ref, "tool": b.get("name"), "arguments": b.get("input"), "decision": out.decision.value,
                     "rule_id": out.rule_id, "datasets": datasets, "status": status, "content": out.content}
            s.results[ref] = entry
            self.d.save_result(env.job_id, ref, tool=str(b.get("name")), arguments=b.get("input"), decision=out.decision.value,
                               rule_id=out.rule_id, datasets=datasets, content=out.content)
            self.d.emit("tool_result", {k: v for k, v in entry.items() if k != "content"})
            blocks.append({"type": "tool_result", "tool_use_id": b.get("id"), "content": out.content,
                           **({"is_error": True} if out.decision is Decision.DENIED else {})})
        return blocks


class _State:
    """Per-job mutable state: usage totals, tool results by ref, datasets in the context, and the text buffer."""

    def __init__(self, ctx: AskContext):
        self.records: list[dict] = []
        self.results: dict[str, dict] = {}
        self.datasets: set[str] = set(ctx.history_datasets)
        self.turns = 0
        self.tool_calls = 0
        self.final_text: str | None = None
        self.answer_status = "failed"
        self.model_id: str | None = None
        self.effort: str | None = None
        self.cost: Money | None = None
        self.cost_exact = True
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        self._buf: list[str] = []
        self._buf_ns = 0
        self._poll_ns = 0

    def text_sink(self, d: LoopDeps) -> Callable[[str], None]:
        def sink(text: str) -> None:
            if not self._buf:
                self._buf_ns = d.clock_ns()
            self._buf.append(text)
            if sum(len(x) for x in self._buf) >= TEXT_FLUSH_CHARS or d.clock_ns() - self._buf_ns >= TEXT_FLUSH_NS:
                self.flush(d)
        return sink

    def flush(self, d: LoopDeps) -> None:
        if self._buf:
            d.emit("text", {"turn": self.turns, "text": "".join(self._buf)})
            self._buf = []

    def block_sink(self, d: LoopDeps) -> Callable[[str, str | None], None]:
        def sink(kind: str, name: str | None) -> None:
            if kind in ("thinking", "redacted_thinking"):
                self.flush(d)
                d.emit("thinking", {"turn": self.turns})
            elif kind == "tool_use":
                self.flush(d)
                d.emit("tool_pending", {"turn": self.turns, "tool": name})
        return sink

    def stop_poll(self, d: LoopDeps, env: JobEnvelope) -> Callable[[], bool]:
        def poll() -> bool:
            now = d.clock_ns()
            if now >= env.deadline_ns:
                return True
            if now - self._poll_ns < STOP_POLL_NS:
                return False
            self._poll_ns = now
            return d.is_cancelled(env.job_id)
        return poll

    def add_usage(self, turn: TurnResult, cost: Money, exact: bool) -> None:
        for u in turn.usages:
            self.usage["input_tokens"] += u.input_tokens
            self.usage["output_tokens"] += u.output_tokens
            self.usage["cache_read_tokens"] += u.cached_read_tokens
            self.usage["cache_write_tokens"] += sum(u.cache_write_tokens.values())
        self.cost = cost if self.cost is None else self.cost + cost
        self.cost_exact = self.cost_exact and exact

    def usage_event(self, turn: TurnResult) -> dict:
        return {"turn": self.turns, "stop_reason": turn.stop_reason, "model": self.model_id, "effort": self.effort,
                **self.usage, "cost": self.cost.amount.to_str() if self.cost else None,
                "currency": self.cost.currency if self.cost else None, "cost_exact": self.cost_exact}
