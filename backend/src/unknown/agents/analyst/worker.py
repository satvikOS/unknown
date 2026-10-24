"""``python -m unknown.agents.analyst``: the AI analyst worker (agents venv; holds the provider key, never a broker one).

It is the research model worker (``agents/worker.py``) with its own job store (``agents/analyst_jobs.sqlite``) and lock
(``agents/analyst_worker.lock``), so it inherits, unchanged: the owner policy re-read before every job, per-role
concurrency (a role absent from ``concurrency.per_role`` never runs), deadline expiry, cancellation, crash recovery
against the spend ledger (a request that may have been sent keeps its reservation UNCONFIRMED), the shared spend ledger
(every owner cap counts research and analyst spend together) and the hash-chained audit trail. What differs:

  * a job is one question (``submission.py``), run by ``loop.AnalystRunner`` with read-only tools (``sources.py``);
  * the answer, the stream events and every cited tool result go to the analyst store (``store.py``), which the API
    streams to the terminal; nothing is delivered to the core (an answer is advisory, never a proposal or a command);
  * every question's turn gets an answer record, also when the job ends before running (refused, expired, cancelled,
    recovered after a crash), so a thread never stays blocked.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import signal
import threading
from pathlib import Path
from typing import Any, Mapping

from ...data.registry import load_registry
from ...domain.canonical import canonical_json, sha256_hex
from ...domain.clock import ns_to_iso
from ...service.paths import Paths
from ..budget import BudgetError
from ..catalog import CatalogError, load_approvals
from ..config import OwnerPolicyError, default_policy_path, load_owner_policy
from ..envelope import EnvelopeError, JobEnvelope
from ..gate import DataUseGate, GateError, load_provider_policies
from ..jobstore import TERMINAL, store_state_for
from ..providers.anthropic import AnthropicAdapter
from ..providers.base import RetryPolicy
from ..runner import Candidate
from ..tariffs import TariffBook, TariffError
from ..worker import AdapterContext, AdapterUnavailable, Worker, WorkerError, _fail, _Outcome, readiness
from .loop import AnalystRunner, AskContext, LoopDeps
from .prompt import PROMPT_HASH
from .sources import PlatformData
from .store import AnalystStore, default_analyst_jobs_path, default_analyst_path
from .stream_client import StreamingClaude
from .submission import ROLE
from .toolspec import TOOL_NAMES

log = logging.getLogger("unknown.agents.analyst")

_ANSWER_FOR_STATE = {"CANCELLED": "cancelled", "EXPIRED": "expired", "FAILED": "failed", "SUCCEEDED": "failed"}


def anthropic_factory(ctx: AdapterContext) -> AnthropicAdapter:
    if importlib.util.find_spec("anthropic") is None:
        raise AdapterUnavailable("the anthropic SDK is not installed in this interpreter; run the analyst worker with the "
                                 "agents venv (pip install -r backend/requirements-agents.txt)")
    return AnthropicAdapter(owner=ctx.policy, ledger=ctx.ledger, provider_policy=ctx.provider_policies["providers"]["anthropic"],
                            environ=ctx.environ, secrets_dir=ctx.paths.secrets)


class AnalystWorker(Worker):
    def __init__(self, paths: Paths, **kw: Any):
        kw.setdefault("adapter_factories", {"anthropic": anthropic_factory})
        super().__init__(paths, jobs_path=default_analyst_jobs_path(paths.root), lock_name="analyst_worker.lock", **kw)
        self.store = AnalystStore(default_analyst_path(paths.root), clock_ns=self.clock.now_ns)

    def close(self) -> None:
        try:
            self.store.close()
        finally:
            super().close()

    # ------------------------------------------------------------------ lifecycle overrides
    def _deliver_due(self) -> bool:
        return False            # answers are advisory: nothing is ever delivered to the core

    def recover(self) -> list[dict]:
        actions = super().recover()
        self._settle_turns()
        return actions

    def step(self) -> bool:
        worked = super().step()
        return self._settle_turns() or worked

    def _settle_turns(self) -> bool:
        """Give an answer record to every question whose job ended without one."""
        done = False
        for job_id in self.store.unanswered_jobs():
            job = self.jobs.get(job_id)
            if job is not None and job["state"] not in TERMINAL:
                continue
            state = job["state"] if job is not None else "FAILED"
            reason = job["reason"] if job is not None else "the job was never queued"
            answer = {"status": _ANSWER_FOR_STATE.get(state, "failed"), "label": None, "text": None, "reason": reason,
                      "outcome": job["outcome"] if job is not None else None}
            self.store.record_answer(job_id, answer)
            self.store.append_event(job_id, "done", {"state": state, "outcome": answer["outcome"], "reason": reason})
            done = True
        return done

    def _finish(self, jid: str, out: _Outcome) -> bool:
        ok = super()._finish(jid, out)
        if ok:
            job = self.jobs.get(jid)
            self.store.append_event(jid, "done", {"state": job["state"], "outcome": job["outcome"], "reason": job["reason"]})
        return ok

    # ------------------------------------------------------------------ one question
    def _execute(self, job: dict, policy, problem: str | None) -> _Outcome:
        jid = job["job_id"]
        self.store.append_event(jid, "status", {"state": "RUNNING", "attempt": job["start_attempts"]})
        out = self._execute_ask(job, policy, problem)
        turn = self.store.turn_for_job(jid)
        if turn is not None and turn["answer"] is None:
            self.store.record_answer(jid, {"status": "failed", "label": None, "text": None, "reason": out.reason,
                                           "outcome": out.outcome})
        return out

    def _execute_ask(self, job: dict, policy, problem: str | None) -> _Outcome:
        jid, inputs = job["job_id"], job["inputs"]
        try:
            env = JobEnvelope.from_dict(job["envelope"])
        except EnvelopeError as exc:
            return _fail("denied_by_policy", f"the stored envelope is invalid: {exc}")
        if problem is not None:
            return _fail("denied_by_policy", problem)
        if policy is None:
            return _fail("denied_by_policy", f"no owner policy at {self.policy_path}: paid model calls are disabled (AI-05)")
        ledger = self._ledger_for(policy)
        if not ledger.paid_calls_enabled():
            return _fail("budget_exhausted", "the owner's per-day or per-job cap is zero: paid model calls are disabled")
        context = inputs.get("context")
        if not isinstance(context, dict) or sha256_hex(canonical_json(context)) not in env.input_hashes:
            return _fail("denied_by_policy", "the stored question does not match the envelope's input hashes")
        if env.role != ROLE or env.prompt_template_hash != PROMPT_HASH:
            return _fail("denied_by_policy", "the analyst prompt or tool definitions changed since the question was asked; "
                                             "ask again")
        if not set(env.allowed_tools) <= set(TOOL_NAMES):
            return _fail("denied_by_policy", "the envelope allows tools the analyst does not have")
        try:
            approvals = load_approvals(self.models_dir / "approved.json")
            tariffs = TariffBook.load(self.models_dir / "tariffs.json")
            provider_policies = load_provider_policies(self.models_dir / "provider_policies.json")
        except (CatalogError, TariffError, GateError, OSError, ValueError, KeyError) as exc:
            return _fail("denied_by_policy", f"model configuration: {exc}")
        self.catalog.approvals = approvals
        self.catalog.max_age_ns = policy.catalog_max_age_hours * 3600 * 1_000_000_000
        candidates, why = self._candidates(policy, approvals, env.role)
        if not candidates:
            return _fail("denied_by_policy", why)
        adapters, usable, skipped = self._usable(candidates, policy, ledger, provider_policies, tariffs)
        self._audit(jid, {"kind": "candidates", "usable": [f"{c.provider}/{c.model_id}" for c in usable], "skipped": skipped})
        if not usable:
            return _fail("denied_by_policy", "no candidate model can be sent: " + "; ".join(skipped))
        max_out = inputs["max_output_tokens"]
        now = self.clock.now_ns()
        refusal: Exception | None = None
        for c in usable:
            try:
                floor = tariffs.worst_case(c.provider, c.model_id, at_ns=now, prompt_tokens=0, max_output_tokens=max_out,
                                           region=adapters[c.provider].requested_region() or "global")
                probe = ledger.reserve(job_id=jid, role=env.role, provider=c.provider, model_id=c.model_id, amount=floor,
                                       job_cap=env.spend_cap, purpose="admission_probe",
                                       note="cheapest possible turn of this question; released at once")
                ledger.release(probe, note="admission probe: headroom confirmed before any provider call")
            except (BudgetError, TariffError) as exc:
                refusal = exc
                continue
            self._audit(jid, {"kind": "budget_admitted", "provider": c.provider, "model_id": c.model_id, "floor": str(floor)})
            break
        else:
            self._audit(jid, {"kind": "budget_refused", "error": str(refusal)})
            return _fail("budget_exhausted", f"budget exhausted before any provider call: {refusal}")
        providers = sorted({c.provider for c in usable})
        for p in providers:
            self._ensure_catalog(jid, p, adapters[p])
        gate = DataUseGate(self._registry if self._registry is not None else load_registry(), policy, provider_policies,
                           self.clock.now_ns)
        data = PlatformData(self.paths.root, gate.dataset_allowed, self.clock.now_ns, account=inputs.get("account"))
        r = policy.retry
        deps = LoopDeps(
            clients={p: StreamingClaude(adapters[p]) for p in providers}, ledger=ledger, tariffs=tariffs, gate=gate,
            catalog=self.catalog, tools=data.registry(), clock_ns=self.clock.now_ns, sleep_ns=self.sleep_ns,
            retry=RetryPolicy(r.max_attempts, r.base_ms, r.max_ms, r.jitter_ms), breaker=self._breaker,
            estimate=policy.estimate, is_cancelled=self.jobs.cancel_requested,
            on_record=lambda rec: self._audit(jid, rec, actor="analyst"),
            emit=lambda kind, data_: self.store.append_event(jid, kind, data_), save_result=self.store.put_result)
        ctx = AskContext(context["question"], context.get("security"), tuple(context.get("history") or ()),
                         tuple(context.get("history_datasets") or ()), max_out, ns_to_iso(now))
        result = AnalystRunner(deps).run(env, ctx, usable)
        self.store.record_answer(jid, result.answer)
        self.store.append_event(jid, "answer", result.answer)
        return _Outcome(store_state_for(result.status), result.status.value, result.reason)

    def _usable(self, candidates: list[Candidate], policy, ledger, provider_policies: Mapping, tariffs: TariffBook):
        ctx = AdapterContext(policy, ledger, provider_policies, self.paths, self.environ)
        adapters: dict[str, AnthropicAdapter] = {}
        usable: list[Candidate] = []
        skipped: list[str] = []
        now = self.clock.now_ns()
        for c in candidates:
            if c.provider not in adapters:
                factory = self.adapter_factories.get(c.provider)
                if factory is None:
                    skipped.append(f"{c.provider}/{c.model_id}: the analyst streams with the Anthropic SDK only")
                    continue
                try:
                    adapters[c.provider] = factory(ctx)
                except AdapterUnavailable as exc:
                    skipped.append(f"{c.provider}/{c.model_id}: {exc}")
                    continue
            adapter = adapters[c.provider]
            issue = adapter.admission_problem()
            if issue is None and not tariffs.is_routable(c.provider, c.model_id, at_ns=now,
                                                         region=adapter.requested_region() or "global"):
                issue = "no owner-approved, verified tariff row in config/models/tariffs.json"
            if issue is not None:
                skipped.append(f"{c.provider}/{c.model_id}: {issue}")
                continue
            usable.append(c)
        return adapters, usable, skipped


def analyst_readiness(paths: Paths, *, models_dir: Path | None = None, environ: Mapping[str, str] | None = None) -> list[tuple[bool, str]]:
    """The research worker's local checks (no network, no key printed) plus the analyst role's own."""
    out = [item for item in readiness(paths, models_dir=models_dir, environ=environ) if "role 'research'" not in item[1]]
    from ..common import CONFIG_MODELS
    models = Path(models_dir) if models_dir else CONFIG_MODELS
    try:
        approved = sorted(a.model_id for a in load_approvals(models / "approved.json") if ROLE in a.roles)
        out.append((bool(approved), f"approved.json: models approved for role '{ROLE}': {approved or 'none'}"))
    except (CatalogError, OSError, ValueError) as exc:
        out.append((False, f"approved.json invalid: {exc}"))
    try:
        policy = load_owner_policy(default_policy_path(paths.root))
    except (OwnerPolicyError, OSError, ValueError):
        policy = None
    if policy is not None:
        n = dict(policy.concurrency.per_role).get(ROLE, 0)
        out.append((n >= 1, f"concurrency.per_role.{ROLE} = {n}" + ("" if n >= 1 else " (questions of this role never run)")))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Project Unknown AI analyst worker: answers ASK questions (ADR-0016)")
    ap.add_argument("--home", default=None, help="runtime root (default ~/.unknown or $UNKNOWN_HOME)")
    ap.add_argument("--poll-seconds", type=float, default=1.0)
    ap.add_argument("--once", action="store_true", help="exit when no question is queued")
    ap.add_argument("--check", action="store_true", help="print what a live question needs on this machine, then exit")
    ap.add_argument("--worker-id", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()
    if args.check:
        items = analyst_readiness(paths)
        for ok, text in items:
            print(("ok   " if ok else "MISS ") + text)
        raise SystemExit(0 if all(ok for ok, _ in items) else 1)
    worker = AnalystWorker(paths, worker_id=args.worker_id)
    try:
        worker.start()
    except WorkerError as exc:
        log.error("%s", exc)
        worker.close()
        raise SystemExit(2) from None
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("analyst worker %s started (home %s)", worker.worker_id, paths.root)
    try:
        worker.run(stop, poll_s=args.poll_seconds, once=args.once)
    finally:
        worker.close()
