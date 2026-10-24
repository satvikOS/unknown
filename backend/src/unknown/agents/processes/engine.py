"""One AI process run inside the model worker: evidence -> model call through the runner -> cited note.

The model call goes through exactly the research-job path (``Worker._prepare`` and ``JobRunner``): approved
candidates only, owner-approved tariffs, a budget probe, the data-use gate on every span's dataset, a ledger
reservation before the call, settlement, the hash-chained audit trail, local schema validation and claim
verification against the spans. The process's own day cap is added to every reservation (``ProcessLedger``).

Evidence is store rows rendered as untrusted data blocks (the runner adds the nonce boundaries): only items whose
dataset the registry permits for ``model_processing_cloud`` are sent; the rest are listed in the note as not sent,
with the reason. Price bars are never sent (no bar dataset in the registry grants model processing); they appear
as data lines computed here, each citing its bars. Lines from the model keep the verifier's status: ``verified``
(cited span exists and the quote is in it), ``uncertain`` (cited, quote not checked) or ``unsupported``
(no citation, unknown span or quote not found), and the terminal flags every line that is not verified.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ...data.registry import RegistryError, dataset as registry_dataset, load_registry
from ...domain.canonical import sha256_hex
from ...domain.fixed import Fixed
from ...domain.money import Money
from ..budget import BudgetError
from ..envelope import EnvelopeError, JobEnvelope
from ..evidence import ClaimStatus, EvidenceSpan
from ..pit_evidence import span_hash
from ..prompts import PROMPT_TEMPLATE_HASH
from ..runner import JobInputs, JobRunner
from .config import ProcessSettings
from .sources import DailyMove, NewsItem, iso

log = logging.getLogger("unknown.agents.processes")

NS = 1_000_000_000
OUTPUT_SCHEMA, SCHEMA_VERSION = "research_answer", 1
DEADLINE_S = 900
MAX_TURNS = 3            # one answer, one truncation retry, one transient retry
RETRY_BUDGET = 1
TOKEN_CAP = 400_000
SUMMARY_CHARS = 1500
ADVISORY = ("AI-generated advisory note. It cannot place, change or cancel orders, start sessions, move money or "
            "change policy, and it is not investment advice.")


class ProcessLedger:
    """The worker's SpendLedger with one more cap: the process's own per-day cap, checked inside the same
    reservation transaction as every owner cap. Everything else is delegated unchanged."""

    def __init__(self, inner, role: str, day_cap: Money):
        self._inner, self._role, self._cap = inner, role, day_cap

    def reserve(self, **kw):
        if kw.get("role") != self._role:
            raise BudgetError(f"process ledger for role {self._role} used for role {kw.get('role')}")
        return self._inner.reserve(**kw, role_day_cap=self._cap)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@dataclass
class Evidence:
    spans: list[EvidenceSpan] = field(default_factory=list)
    citations: dict[str, dict] = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)          # every item considered: the non-AI headline list
    data_lines: list[dict] = field(default_factory=list)     # computed here from stored data (not AI)
    unavailable: list[dict] = field(default_factory=list)    # what could not be shown, and why
    symbols: set[str] = field(default_factory=set)
    ciks: set[str] = field(default_factory=set)


def cloud_permission(registry: Mapping, dataset_id: str) -> str | None:
    """None when the registry permits sending this dataset to a cloud model; otherwise the reason."""
    if not dataset_id:
        return "the item's source has no registered dataset"
    try:
        ds = registry_dataset(registry, dataset_id)
    except RegistryError:
        return f"dataset {dataset_id} is not in the data registry"
    if not ds.permits("model_processing_cloud"):
        return f"dataset {dataset_id} does not permit cloud model processing (allowed: {', '.join(sorted(ds.allowed_uses))})"
    return None


def item_text(it: NewsItem) -> str:
    issuers = "; ".join(f"{e.get('name') or 'unnamed'} (CIK {e['cik']}"
                        + (f", tickers {' '.join(e['tickers'])}" if e.get("tickers") else "") + f", role {e.get('role')})"
                        for e in it.entities) or "none tagged"
    summary = it.summary if len(it.summary) <= SUMMARY_CHARS else it.summary[:SUMMARY_CHARS] + " [truncated]"
    lines = [f"source: {it.source_name} ({it.source_id}); kind: {it.kind}" + (f"; form: {it.form}" if it.form else "")
             + (f"; items: {', '.join(it.items)}" if it.items else ""),
             f"issuers: {issuers}",
             f"published: {iso(it.published_ns) or 'not stated'}; received: {iso(it.first_seen_ns)}",
             f"title: {it.title}"]
    if summary:
        lines.append(f"summary: {summary}")
    if it.link:
        lines.append(f"link: {it.link}")
    return "\n".join(lines)


def add_items(ev: Evidence, items: list[NewsItem], registry: Mapping, *, why: str) -> None:
    for it in items:
        if it.citation_id in ev.citations:
            continue
        ev.citations[it.citation_id] = it.citation()
        refusal = cloud_permission(registry, it.dataset_id)
        ev.items.append({"citation": it.citation_id, "title": it.title, "kind": it.kind, "form": it.form,
                         "published": iso(it.published_ns), "why": why, "sent_to_model": refusal is None,
                         "not_sent_reason": refusal})
        ev.ciks.update(it.ciks)
        if refusal is not None:
            continue
        text = item_text(it)
        ev.spans.append(EvidenceSpan(it.citation_id, it.citation_id, it.dataset_id, text,
                                     content_hash=span_hash(it.citation_id, it.dataset_id, text), source=it.link or "",
                                     ref_type="document_span", ref_id=it.citation_id,
                                     observed_ns=it.published_ns, available_ns=it.first_seen_ns))


def add_moves(ev: Evidence, moves: list[DailyMove], reasons: Mapping[str, str]) -> None:
    for m in moves:
        ev.citations[m.citation_id] = m.citation()
        ev.data_lines.append({"kind": "price_move", "symbol": m.symbol, "text": m.line(), "citations": [m.citation_id],
                              "change_pct": m.change_pct, "label": "data (not AI): stored real daily bars; not sent to the model"})
    for symbol, why in sorted(reasons.items()):
        ev.unavailable.append({"what": f"price move for {symbol}", "reason": why})


@dataclass
class ModelOutcome:
    status: str                     # ok | abstained | ai_unavailable
    reason: str
    headline: str | None = None
    lines: list[dict] = field(default_factory=list)
    model_ids: list[str] = field(default_factory=list)
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    cost: str | None = None
    currency: str | None = None
    runner_status: str | None = None


def _spend(ledger, run_id: str) -> tuple[str | None, str | None]:
    rows = ledger.reservations(run_id) if ledger is not None else []
    if not rows:
        return None, None
    total = Fixed(0)
    for r in rows:
        if r["state"] in ("OPEN", "UNCONFIRMED"):
            total = total + Fixed.parse(r["reserved"])
        elif r["state"] == "SETTLED" and r["actual"]:
            total = total + Fixed.parse(r["actual"])
    return total.to_str(), rows[0]["currency"]


def run_model(worker, *, run_id: str, settings: ProcessSettings, task: str, spans: list[EvidenceSpan],
              now_ns: int, horizon_s: int = 86_400) -> ModelOutcome:
    """Send one task with its spans through the worker's admission and the runner. Never raises for a refusal:
    every refusal comes back as ``ai_unavailable`` with the reason (the note then shows the non-AI list)."""
    policy, problem = worker._load_policy()
    if policy is None:
        return ModelOutcome("ai_unavailable", problem or f"no owner policy at {worker.policy_path}: paid model calls are disabled")
    base = worker._ledger_for(policy)
    if not base.paid_calls_enabled():
        return ModelOutcome("ai_unavailable", "the owner's per-day or per-job cap is zero: paid model calls are disabled")
    if settings.per_day.currency != policy.caps.currency:
        return ModelOutcome("ai_unavailable", f"ai_processes.json currency {settings.per_day.currency} differs from the "
                                              f"owner policy currency {policy.caps.currency}")
    if not settings.per_run.is_positive() or not settings.per_day.is_positive():
        return ModelOutcome("ai_unavailable", f"the {settings.process} caps are zero: this process makes no paid calls")
    ledger = ProcessLedger(base, settings.role, settings.per_day)
    try:
        env = JobEnvelope(job_id=run_id, role=settings.role, schema_version=SCHEMA_VERSION,
                          input_hashes=(sha256_hex(task), *dict.fromkeys(s.content_hash for s in spans)), allowed_tools=(),
                          deadline_ns=now_ns + DEADLINE_S * NS, max_turns=MAX_TURNS, max_tool_calls=0,
                          retry_budget=RETRY_BUDGET, token_cap=TOKEN_CAP, spend_cap=settings.per_run,
                          output_schema=OUTPUT_SCHEMA, created_ns=now_ns, prompt_template_hash=PROMPT_TEMPLATE_HASH)
    except EnvelopeError as exc:
        return ModelOutcome("ai_unavailable", f"envelope refused: {exc}")
    worker._audit(run_id, {"kind": "process_started", "process": settings.process, "role": settings.role,
                           "envelope": env.to_dict(),
                           "evidence": [{"span_id": s.span_id, "dataset_id": s.dataset_id, "content_hash": s.content_hash}
                                        for s in spans],
                           "caps": {"per_run": str(settings.per_run.amount.to_str()), "per_day": settings.per_day.amount.to_str()}})
    prepared = worker._prepare(run_id, role=settings.role, spend_cap=settings.per_run, policy=policy, ledger=ledger,
                               max_out=settings.max_output_tokens, is_cancelled=lambda _jid: False)
    if not isinstance(prepared, tuple):
        cost, ccy = _spend(base, run_id)
        return ModelOutcome("ai_unavailable", f"{prepared.outcome}: {prepared.reason}", cost=cost, currency=ccy,
                            runner_status=prepared.outcome)
    deps, usable = prepared
    result = JobRunner(deps).run(env, JobInputs(task, tuple(spans), horizon_s, settings.max_output_tokens, (), as_of_ns=now_ns),
                                 usable)
    gens = [r for r in result.records if r.get("kind") == "generated"]
    cost, ccy = _spend(base, run_id)
    out = ModelOutcome("ai_unavailable", f"{result.status.value}: {result.reason}", cost=cost, currency=ccy,
                       runner_status=result.status.value,
                       model_ids=sorted({g["returned_model"] for g in gens if g.get("returned_model")}),
                       prompt_tokens=sum(g["prompt_tokens"] for g in gens if g.get("prompt_tokens") is not None) if gens else None,
                       output_tokens=sum(g["output_tokens"] for g in gens if g.get("output_tokens") is not None) if gens else None)
    proposal = result.proposal
    if result.status.value != "succeeded" or proposal is None:
        return out
    known = {s.span_id for s in spans}
    for v in result.verdicts:
        cited = [sid for sid in v.claim.span_ids if sid in known]
        out.lines.append({"text": v.claim.text, "status": v.status.value, "citations": cited, "quote": v.claim.quote,
                          "checks": list(v.notes), "label": "AI"})
    if proposal.get("abstain"):
        out.status, out.reason = "abstained", proposal.get("reason") or "the model abstained"
        out.headline = proposal.get("reason") or None
    else:
        out.status, out.reason = "ok", "ok"
        out.headline = proposal.get("hypothesis") or None
    if not any(ln["status"] == ClaimStatus.VERIFIED.value for ln in out.lines) and out.status == "ok":
        out.reason = "no line could be verified against its cited span"
    return out


def note_body(*, process: str, title: str, ev: Evidence, mo: ModelOutcome | None, window: tuple[int, int] | None,
              now_ns: int, extra: Mapping[str, Any] | None = None) -> dict:
    """The stored note: AI label, cited lines (each keeps its verification status), data lines, the non-AI list of
    every item considered (sent or not, and why), what is unavailable, and the run's cost."""
    ai = mo is not None and mo.status in ("ok", "abstained")
    label = {"kind": "AI" if ai else "data", "advisory": ADVISORY,
             "model_ids": mo.model_ids if mo else [], "prompt_template_hash": PROMPT_TEMPLATE_HASH if ai else None,
             "generated_ns": now_ns, "generated": iso(now_ns)}
    body = {"process": process, "title": title, "label": label,
            "headline": mo.headline if mo and ai else None,
            "headline_status": "uncited" if mo and ai and mo.headline else None,
            "lines": mo.lines if mo else [],
            "data_lines": ev.data_lines, "items": ev.items, "unavailable": ev.unavailable,
            "citations": ev.citations,
            "window": {"from_ns": window[0], "to_ns": window[1], "from": iso(window[0]), "to": iso(window[1])} if window else None,
            "ai": {"status": mo.status if mo else "not_run", "reason": mo.reason if mo else None,
                   "runner_status": mo.runner_status if mo else None},
            "cost": {"amount": mo.cost if mo else None, "currency": mo.currency if mo else None,
                     "prompt_tokens": mo.prompt_tokens if mo else None, "output_tokens": mo.output_tokens if mo else None}}
    if extra:
        body.update(extra)
    return body


def note_status(ev: Evidence, mo: ModelOutcome | None) -> str:
    if mo is None:
        return "no_items" if not ev.items else "data_only"
    return mo.status
