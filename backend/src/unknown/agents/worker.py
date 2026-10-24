"""``python -m unknown.agents.worker``: the model-worker process (ADR-0016; CAP-AGT-01/02/09, AI-05).

The worker is the only process that holds a provider key and the only one that calls a
provider. It runs research jobs from the durable job store (``jobstore.py``) one at a time:

  1. Owner policy (``<UNKNOWN_HOME>/config/agents.json``), re-read before every job so an edit takes
     effect without a restart. Absent or invalid: the job fails closed with the reason, no network call.
  2. Evidence: re-derived from the point-in-time research store (``<UNKNOWN_HOME>/research/research.sqlite``)
     at the instant pinned at submission; it must equal what the owner submitted (``pit_evidence.py``).
  3. Candidates: enabled providers x models approved for the job's role in ``config/models/approved.json``
     (the owner's default model first). Unapproved models are never candidates; the runner re-checks the
     catalog approval, lifecycle and freshness.
  4. Admission without the network: the adapter must be enabled and hold a key, and an owner-approved
     tariff row must exist. Then a budget probe reserves (and releases) the cheapest call the job could
     make, ``max_output_tokens`` at the output rate with zero prompt tokens, against every owner cap. A
     job that could not afford even that fails ``budget_exhausted`` before any provider call.
  5. Model catalog: refreshed from the provider's list endpoint only when the last snapshot is older than
     ``catalog.max_age_hours`` (snapshots persist under ``agents/catalog`` across restarts); a failed
     refresh is retried no sooner than 5 minutes later.
  6. The runner (``runner.py``) runs the job with the ledger, gate, tariffs and catalog, appending every
     record to the hash-chained audit trail (``audit.py``) as it is made.
  7. The job's terminal state and its proposal (or abstention) are stored in one transaction; the proposal
     is then delivered to the core as ``submit_research_proposal`` with command id
     ``agent.proposal.<job_id>`` over the core socket with the AGENT token. If the core is unreachable the
     proposal stays PENDING and is retried with backoff (1 s doubling to 5 min); it is never dropped. A
     resend after a crash is byte-identical, so the core's command-id deduplication records it once.

Crash recovery at start: a RUNNING job left by a dead worker is FAILED "worker restarted" when the spend
ledger shows any reservation that may have been sent (its OPEN reservations become UNCONFIRMED: the
outcome is unknown, so the money stays reserved until reconciled; an OPEN admission probe is released,
since a probe is never sent). A job whose cancel was requested becomes CANCELLED. If nothing was sent
the job is queued again, at most ``MAX_START_ATTEMPTS`` times. A job end that cannot be stored is kept
in memory and retried every cycle; one failing delivery never blocks the others or new jobs.

Concurrency: one job at a time per worker, and one worker per ``UNKNOWN_HOME`` (an exclusive lock on
``agents/worker.lock``). A role absent from ``concurrency.per_role`` in the owner policy never runs; its
jobs fail with that reason.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import logging
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..data.pit import ResearchStore
from ..data.registry import load_registry
from ..domain.canonical import canonical_json, sha256_hex
from ..domain.clock import Clock, SystemClock
from ..service.ipc import CoreClient, IpcError
from ..service.paths import Paths
from .audit import AuditTrail, default_audit_path, sanitize
from .budget import BudgetError, SpendLedger, default_ledger_path
from .catalog import Catalog, CatalogError, load_approvals
from .common import CONFIG_MODELS
from .config import BreakerConfig, OwnerPolicy, OwnerPolicyError, default_policy_path, load_owner_policy, resolve_api_key
from .envelope import EnvelopeError, JobEnvelope
from .gate import DataUseGate, GateError, load_provider_policies
from .jobstore import MAX_START_ATTEMPTS, JobStore, StateConflict, default_jobs_path, store_state_for
from .pit_evidence import EMPTY, Evidence, EvidenceError, EvidenceSelection, derive, manifest_matches, task_names_instruments
from .prompts import agent_principal_id, proposal_command_id
from .providers.anthropic import AnthropicAdapter
from .providers.base import CircuitBreaker, ProviderAdapter, RetryPolicy, SendRefused
from .runner import Candidate, JobInputs, JobRunner, RunnerDeps
from .tariffs import TariffBook, TariffError
from .tools import ToolRegistry

log = logging.getLogger("unknown.agents.worker")

NS = 1_000_000_000
CATALOG_RETRY_NS = 300 * NS
DELIVERY_MAX_BACKOFF_S = 300


class WorkerError(RuntimeError):
    pass


class AdapterUnavailable(RuntimeError):
    """A provider adapter cannot be built in this process (for example its SDK is not installed)."""


@dataclass(frozen=True)
class AdapterContext:
    policy: OwnerPolicy
    ledger: SpendLedger
    provider_policies: Mapping[str, Any]
    paths: Paths
    environ: Mapping[str, str]


def anthropic_factory(ctx: AdapterContext) -> ProviderAdapter:
    if importlib.util.find_spec("anthropic") is None:
        raise AdapterUnavailable("the anthropic SDK is not installed in this interpreter; run the worker with the agents "
                                 "venv (pip install -r backend/requirements-agents.txt)")
    return AnthropicAdapter(owner=ctx.policy, ledger=ctx.ledger, provider_policy=ctx.provider_policies["providers"]["anthropic"],
                            environ=ctx.environ, secrets_dir=ctx.paths.secrets)


DEFAULT_ADAPTERS: Mapping[str, Callable[[AdapterContext], ProviderAdapter]] = {"anthropic": anthropic_factory}


class _NoCore:
    """The runner defers submission to the worker's outbox; it must never call the core itself."""

    def command(self, command_id, kind, payload, principal):  # pragma: no cover - guarded by defer_submission
        raise AssertionError("deferred submission: the runner must not call the core")


@dataclass
class _Outcome:
    state: str
    outcome: str
    reason: str
    proposal: dict | None = None
    discarded_output_hash: str | None = None


def _fail(outcome: str, reason: str) -> _Outcome:
    return _Outcome("FAILED", outcome, reason)


class Worker:
    def __init__(self, paths: Paths, *, clock: Clock | None = None, sleep_ns: Callable[[int], None] | None = None,
                 core_factory: Callable[[], Any] | None = None,
                 adapter_factories: Mapping[str, Callable[[AdapterContext], ProviderAdapter]] | None = None,
                 models_dir: Path | None = None, registry: Mapping | None = None, environ: Mapping[str, str] | None = None,
                 worker_id: str | None = None):
        self.paths = paths
        self.clock = clock or SystemClock()
        self.sleep_ns = sleep_ns or (lambda ns: time.sleep(ns / NS))
        self.models_dir = Path(models_dir) if models_dir else CONFIG_MODELS
        self.environ = os.environ if environ is None else environ
        self.worker_id = worker_id or f"worker-{socket.gethostname()}-{os.getpid()}"
        self.adapter_factories = dict(DEFAULT_ADAPTERS if adapter_factories is None else adapter_factories)
        self._core_factory = core_factory or self._default_core
        self._registry = registry
        root = paths.root
        self.policy_path = default_policy_path(root)
        self.research_path = root / "research" / "research.sqlite"
        self.jobs = JobStore(default_jobs_path(root), clock_ns=self.clock.now_ns)
        self.audit = AuditTrail(default_audit_path(root), clock_ns=self.clock.now_ns)
        self.catalog = Catalog((), self.clock.now_ns, 24 * 3600 * NS, store_dir=root / "agents" / "catalog")
        self._catalog_attempt_ns: dict[str, int] = {}
        self._ledger: SpendLedger | None = None
        self._ledger_key: object = object()
        self._breaker: CircuitBreaker | None = None
        self._lock_fd: int | None = None
        self._delivery_errors: dict[str, str] = {}
        self._unfinished: dict[str, _Outcome] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> list[dict]:
        """Take the per-home worker lock, then recover jobs a dead worker left RUNNING."""
        lock = self.paths.root / "agents" / "worker.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise WorkerError(f"another model worker holds {lock}; one worker runs per UNKNOWN_HOME") from None
        self._lock_fd = fd
        return self.recover()

    def close(self) -> None:
        for closer in (self.jobs.close, self.audit.close, self._ledger.close if self._ledger else None):
            if closer is not None:
                try:
                    closer()
                except Exception:  # pragma: no cover - best effort on shutdown
                    log.exception("close failed")
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def run(self, stop: threading.Event, *, poll_s: float = 2.0, once: bool = False) -> None:
        while not stop.is_set():
            try:
                worked = self.step()
            except Exception:
                log.exception("worker step failed; retrying after the poll interval")
                worked = False
            if once and not worked:
                return
            if not worked:
                stop.wait(poll_s)

    # ------------------------------------------------------------------ helpers
    def _audit(self, job_id: str, record: Mapping[str, Any], actor: str = "worker") -> None:
        rec = dict(record)
        rec.setdefault("at_ns", self.clock.now_ns())
        self.audit.append(job_id, rec, actor=actor)

    def _load_policy(self) -> tuple[OwnerPolicy | None, str | None]:
        try:
            return load_owner_policy(self.policy_path), None
        except (OwnerPolicyError, OSError, ValueError) as exc:
            return None, f"the owner policy {self.policy_path} is invalid: {exc}"

    def _ledger_for(self, policy: OwnerPolicy | None) -> SpendLedger:
        key = policy.policy_hash if policy is not None else None
        if self._ledger is None or self._ledger_key != key:
            if self._ledger is not None:
                self._ledger.close()
            self._ledger = SpendLedger(policy, default_ledger_path(self.paths.root), clock_ns=self.clock.now_ns)
            self._ledger_key = key
            b = policy.breaker if policy is not None else BreakerConfig()
            self._breaker = CircuitBreaker(b.failures, b.window_s * NS, b.cooldown_s * NS)
        return self._ledger

    def _default_core(self) -> CoreClient:
        return CoreClient(self.paths.socket, self.paths.token("agent"))

    @staticmethod
    def _close_open_rows(ledger: SpendLedger, rows: list[dict], note: str) -> tuple[list[str], list[str]]:
        """OPEN rows of a job whose run was interrupted. An admission probe is never sent (it is reserved and
        released around no call), so it is released; any other OPEN row may have been sent and becomes UNCONFIRMED."""
        unconfirmed, released = [], []
        for r in rows:
            if r["state"] != "OPEN":
                continue
            if r["purpose"] == "admission_probe":
                ledger.release(r["reservation_id"], note="admission probe interrupted; never sent")
                released.append(r["reservation_id"])
            else:
                ledger.mark_unconfirmed(r["reservation_id"], note=note)
                unconfirmed.append(r["reservation_id"])
        return unconfirmed, released

    def _unconfirm_open(self, job_id: str, note: str) -> list[str]:
        if self._ledger is None:
            return []
        return self._close_open_rows(self._ledger, self._ledger.reservations(job_id), note)[0]

    # ------------------------------------------------------------------ recovery
    def recover(self) -> list[dict]:
        policy, _ = self._load_policy()
        ledger = self._ledger_for(policy)
        actions = []
        for job in self.jobs.running_jobs():
            jid = job["job_id"]
            rows = ledger.reservations(jid)
            sent = any(r["state"] in ("SETTLED", "UNCONFIRMED") or (r["state"] == "OPEN" and r["purpose"] != "admission_probe")
                       for r in rows)
            marked, released = self._close_open_rows(
                ledger, rows, "worker restarted while this request may have been in flight; outcome unknown, kept reserved "
                              "until reconciled")
            if job["cancel_requested"]:
                action, reason = "cancelled", "cancelled by the owner while running; the worker restarted before the job ended"
                self.jobs.finish(jid, job["worker_id"], state="CANCELLED", outcome="cancelled_by_owner", reason=reason)
            elif sent:
                action, reason = "failed", "worker restarted"
                self.jobs.fail(jid, from_states=("RUNNING",), outcome="worker_restarted", reason=reason)
            elif job["start_attempts"] >= MAX_START_ATTEMPTS:
                action, reason = "failed", f"worker restarted {job['start_attempts']} times before any provider call"
                self.jobs.fail(jid, from_states=("RUNNING",), outcome="worker_restarted", reason=reason)
            else:
                action, reason = "requeued", "worker restarted before any provider call; requeued"
                self.jobs.requeue(jid, reason)
            self._audit(jid, {"kind": "recovered", "action": action, "reason": reason, "previous_worker": job["worker_id"],
                              "reservations": [{"reservation_id": r["reservation_id"], "state": r["state"], "purpose": r["purpose"]}
                                               for r in rows],
                              "marked_unconfirmed": marked, "released_probes": released})
            log.warning("recovered job %s: %s (%s)", jid, action, reason)
            actions.append({"job_id": jid, "action": action, "reason": reason, "marked_unconfirmed": marked})
        return actions

    # ------------------------------------------------------------------ one cycle
    def step(self) -> bool:
        """Record any job end that could not be stored earlier, deliver due proposals, expire overdue jobs, then
        run at most one job. Returns whether anything happened."""
        worked = False
        for jid, out in list(self._unfinished.items()):
            worked = self._finish(jid, out) or worked
        worked = self._deliver_due() or worked
        for jid in self.jobs.expire_overdue():
            self._audit(jid, {"kind": "expired", "reason": "deadline passed while queued"})
            worked = True
        policy, problem = self._load_policy()
        if policy is not None:
            limits = dict(policy.concurrency.per_role)
            for job in self.jobs.queued_jobs():
                if limits.get(job["role"], 0) < 1:
                    reason = (f"role {job['role']!r} has no concurrency limit in the owner policy "
                              f"(concurrency.per_role.{job['role']}); jobs of this role never run")
                    try:
                        self.jobs.fail(job["job_id"], from_states=("QUEUED",), outcome="denied_by_policy", reason=reason)
                    except StateConflict:
                        continue
                    self._audit(job["job_id"], {"kind": "refused", "outcome": "denied_by_policy", "reason": reason})
                    worked = True
            job = self.jobs.claim_next(self.worker_id, max_global=policy.concurrency.global_limit, max_per_role=limits)
        else:
            job = self.jobs.claim_next(self.worker_id, max_global=1, max_per_role=None)
        if job is None:
            return worked
        self._run_claimed(job, policy, problem)
        self._deliver_due()
        return True

    def _run_claimed(self, job: dict, policy: OwnerPolicy | None, problem: str | None) -> None:
        jid = job["job_id"]
        try:
            ev = (job["inputs"].get("evidence") or {}).get("spans") or []
            self._audit(jid, {"kind": "job_started", "worker_id": self.worker_id, "attempt": job["start_attempts"],
                              "submitted_by": job["submitted_by"], "submitted_ns": job["submitted_ns"],
                              "envelope": job["envelope"], "task_hash": job["inputs"].get("task_hash"),
                              "evidence": [{"span_id": s.get("span_id"), "dataset_id": s.get("dataset_id"),
                                            "content_hash": s.get("content_hash")} for s in ev],
                              "policy_hash": policy.policy_hash if policy is not None else None})
            out = self._execute(job, policy, problem)
        except Exception as exc:
            log.exception("job %s failed inside the worker", jid)
            marked = self._unconfirm_open(jid, f"worker error during the job ({type(exc).__name__}); outcome unknown")
            out = _fail("worker_error", f"{type(exc).__name__}: {exc}"[:2000])
            try:
                self._audit(jid, {"kind": "worker_error", "error": out.reason, "marked_unconfirmed": marked})
            except Exception:
                log.exception("could not audit the worker error of job %s", jid)
        self._finish(jid, out)

    def _finish(self, jid: str, out: _Outcome) -> bool:
        """Store a job's end (and its proposal). If the store write fails, the outcome, which may hold a paid
        proposal, is kept in memory and retried at the start of every step instead of being lost."""
        try:
            done = self.jobs.finish(jid, self.worker_id, state=out.state, outcome=out.outcome, reason=out.reason,
                                    proposal=out.proposal, discarded_output_hash=out.discarded_output_hash)
        except StateConflict:
            log.error("job %s is no longer running under this worker; its outcome %s (%s) was not recorded",
                      jid, out.state, out.outcome)
            self._unfinished.pop(jid, None)
            return False
        except Exception:
            log.exception("could not record the end of job %s; kept in memory and retried", jid)
            self._unfinished[jid] = out
            return False
        self._unfinished.pop(jid, None)
        self._audit(jid, {"kind": "job_finished", "state": done["state"], "outcome": done["outcome"], "reason": done["reason"],
                          "output_hash": done["output_hash"], "discarded_output_hash": done["discarded_output_hash"],
                          "delivery": done["delivery"]})
        log.info("job %s finished: %s (%s) %s", jid, done["state"], done["outcome"], done["reason"])
        return True

    # ------------------------------------------------------------------ one job
    def _candidates(self, policy: OwnerPolicy, approvals, role: str) -> tuple[list[Candidate], str]:
        enabled = sorted(pid for pid, cfg in policy.providers.items() if cfg.enabled)
        if not enabled:
            disabled = sorted(policy.providers)
            return [], (f"no provider is enabled in the owner policy {self.policy_path}"
                        + (f" (disabled: {', '.join(disabled)})" if disabled else "") + "; paid model calls are off")
        out: list[Candidate] = []
        for pid in enabled:
            approved = sorted({a.model_id for a in approvals if a.provider == pid and role in a.roles})
            default = policy.providers[pid].default_model
            if default in approved:
                approved.remove(default)
                approved.insert(0, default)
            out.extend(Candidate(pid, m) for m in approved)
        if not out:
            return [], (f"no model is approved for role {role!r} for an enabled provider ({', '.join(enabled)}); "
                        f"the owner approves one in {self.models_dir / 'approved.json'} (see docs/agents/README.md)")
        return out, ""

    def _evidence(self, inputs: Mapping[str, Any]) -> Evidence:
        ev = inputs.get("evidence") or {}
        stored = ev.get("spans") or []
        if ev.get("selection") is None:
            if stored:
                raise EvidenceError("spans were recorded without a selection")
            return EMPTY
        sel = EvidenceSelection.from_dict(ev["selection"])
        try:
            store = ResearchStore.open_readonly(self.research_path)
        except FileNotFoundError:
            raise EvidenceError(f"the research store {self.research_path} is missing") from None
        try:
            evidence = derive(store, sel)
        finally:
            store.close()
        diff = manifest_matches(stored, evidence)
        if diff is not None:
            raise EvidenceError(f"the evidence no longer matches the submission ({diff}); resubmit the question")
        return evidence

    @staticmethod
    def _subject(inputs: Mapping[str, Any], env: JobEnvelope, task: str) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        """The typed subject of the job's proposal (research_proposal v2): the evidence selection's pinned instant
        (else the submission time), the selection's entity ids, and the owner's instrument ids, which must be the
        ones the task names (the task is in the input hashes)."""
        raw = (inputs.get("evidence") or {}).get("selection")
        sel = EvidenceSelection.from_dict(raw) if raw is not None else None
        request = inputs.get("request") or {}
        instruments = request.get("instrument_ids") or []
        if not isinstance(instruments, list) or not all(isinstance(i, str) for i in instruments):
            raise EvidenceError("the stored instrument ids are malformed")
        question = request.get("question")
        if not isinstance(question, str) or not task_names_instruments(task, question, instruments):
            raise EvidenceError("the stored instrument ids are not the ones named in the job's task")
        return (sel.as_of_ns if sel is not None else env.created_ns, tuple(instruments),
                sel.entity_ids if sel is not None else ())

    def _ensure_catalog(self, job_id: str, provider: str, adapter: ProviderAdapter) -> None:
        now = self.clock.now_ns()
        snap = self.catalog.last_good.get(provider)
        if snap is None:
            snap = self.catalog.restore_persisted(provider)
            if snap is not None:
                self._audit(job_id, {"kind": "catalog_restored", "provider": provider, "fetched_at_ns": snap.fetched_at_ns,
                                     "models": len(snap.rows)})
        if snap is not None and now - snap.fetched_at_ns <= self.catalog.max_age_ns:
            return
        last = self._catalog_attempt_ns.get(provider)
        if last is not None and now - last < CATALOG_RETRY_NS:
            return
        self._catalog_attempt_ns[provider] = now
        try:
            fresh = adapter.list_models(now_ns=now)
        except (SendRefused, NotImplementedError) as exc:
            self._audit(job_id, {"kind": "catalog_refresh_refused", "provider": provider, "reason": str(exc)})
            return
        except Exception as exc:
            self._audit(job_id, {"kind": "catalog_refresh_failed", "provider": provider, "error": f"{type(exc).__name__}: {exc}"})
            return
        before = len(self.catalog.alerts)
        d = self.catalog.ingest(fresh)
        self._audit(job_id, {"kind": "catalog_refreshed" if fresh.ok else "catalog_refresh_failed", "provider": provider,
                             "ok": fresh.ok, "http_status": fresh.http_status, "error": fresh.error, "models": len(fresh.rows),
                             "added": list(d.added) if d else [], "removed": list(d.removed) if d else [],
                             "alerts": self.catalog.alerts[before:]})

    def _execute(self, job: dict, policy: OwnerPolicy | None, problem: str | None) -> _Outcome:
        jid = job["job_id"]
        inputs = job["inputs"]
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
        task = inputs.get("task")
        if not isinstance(task, str) or sha256_hex(task) not in env.input_hashes:
            return _fail("denied_by_policy", "the stored task does not match the envelope's input hashes")
        try:
            evidence = self._evidence(inputs)
            as_of_ns, instrument_ids, entity_ids = self._subject(inputs, env, task)
        except EvidenceError as exc:
            return _fail("denied_by_policy", f"evidence: {exc}")
        prepared = self._prepare(jid, role=env.role, spend_cap=env.spend_cap, policy=policy, ledger=ledger,
                                 max_out=inputs["max_output_tokens"], is_cancelled=self.jobs.cancel_requested)
        if isinstance(prepared, _Outcome):
            return prepared
        deps, usable = prepared
        max_out = inputs["max_output_tokens"]
        result = JobRunner(deps).run(env, JobInputs(task, evidence.spans, inputs["horizon_seconds"], max_out, evidence.facts,
                                                    as_of_ns=as_of_ns, instrument_ids=instrument_ids, entity_ids=entity_ids),
                                     usable)
        state = store_state_for(result.status)
        deliver = result.proposal is not None and result.discarded_output_hash is None and state in ("SUCCEEDED", "FAILED")
        return _Outcome(state, result.status.value, result.reason, result.proposal if deliver else None,
                        result.discarded_output_hash)

    def _prepare(self, jid: str, *, role: str, spend_cap, policy: OwnerPolicy, ledger, max_out: int,
                 is_cancelled: Callable[[str], bool]) -> "tuple[RunnerDeps, list[Candidate]] | _Outcome":
        """Admission without the network, shared by research jobs and the AI processes (``agents/processes``):
        approved candidates, adapters with keys and SDKs, owner-approved tariffs, a budget probe against every
        cap (``ledger`` may add a process's own day cap), the catalog refresh, then the runner's dependencies.
        Returns the failure outcome instead when nothing can be sent."""
        try:
            approvals = load_approvals(self.models_dir / "approved.json")
        except (CatalogError, OSError, ValueError) as exc:
            return _fail("denied_by_policy", f"approved.json: {exc}")
        self.catalog.approvals = approvals
        self.catalog.max_age_ns = policy.catalog_max_age_hours * 3600 * NS
        candidates, why = self._candidates(policy, approvals, role)
        if not candidates:
            return _fail("denied_by_policy", why)
        try:
            tariffs = TariffBook.load(self.models_dir / "tariffs.json")
            provider_policies = load_provider_policies(self.models_dir / "provider_policies.json")
        except (TariffError, GateError, OSError, ValueError, KeyError) as exc:
            return _fail("denied_by_policy", f"model configuration: {exc}")
        ctx = AdapterContext(policy, ledger, provider_policies, self.paths, self.environ)
        adapters: dict[str, ProviderAdapter] = {}
        skipped: list[str] = []
        usable: list[Candidate] = []
        now = self.clock.now_ns()
        for c in candidates:
            if c.provider not in adapters:
                factory = self.adapter_factories.get(c.provider)
                if factory is None:
                    skipped.append(f"{c.provider}/{c.model_id}: this worker has no sending adapter for {c.provider}")
                    continue
                try:
                    adapters[c.provider] = factory(ctx)
                except AdapterUnavailable as exc:
                    skipped.append(f"{c.provider}/{c.model_id}: {exc}")
                    continue
            adapter = adapters[c.provider]
            issue = adapter.admission_problem()
            if issue is None:
                missing = adapter.unverified_requirements("generate")
                issue = f"unverified_protocol_facts:{','.join(missing)}" if missing else None
            if issue is None and not tariffs.is_routable(c.provider, c.model_id, at_ns=now,
                                                         region=adapter.requested_region() or "global"):
                issue = "no owner-approved, verified tariff row in config/models/tariffs.json"
            if issue is not None:
                skipped.append(f"{c.provider}/{c.model_id}: {issue}")
                continue
            usable.append(c)
        self._audit(jid, {"kind": "candidates", "usable": [f"{c.provider}/{c.model_id}" for c in usable], "skipped": skipped})
        if not usable:
            return _fail("denied_by_policy", "no candidate model can be sent: " + "; ".join(skipped))
        refusal: Exception | None = None
        for c in usable:
            adapter = adapters[c.provider]
            try:
                floor = tariffs.worst_case(c.provider, c.model_id, at_ns=now, prompt_tokens=0, max_output_tokens=max_out,
                                           region=adapter.requested_region() or "global")
                probe = ledger.reserve(job_id=jid, role=role, provider=c.provider, model_id=c.model_id, amount=floor,
                                       job_cap=spend_cap, purpose="admission_probe",
                                       note="cheapest possible call of this job; released at once")
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
        r = policy.retry
        deps = RunnerDeps(
            adapters={p: adapters[p] for p in providers}, ledger=ledger, tariffs=tariffs,
            gate=DataUseGate(self._registry if self._registry is not None else load_registry(), policy, provider_policies,
                             self.clock.now_ns),
            catalog=self.catalog, core=_NoCore(), tools=ToolRegistry(), clock_ns=self.clock.now_ns, sleep_ns=self.sleep_ns,
            retry=RetryPolicy(r.max_attempts, r.base_ms, r.max_ms, r.jitter_ms), breaker=self._breaker,
            estimate=policy.estimate, is_cancelled=is_cancelled, principal_id=agent_principal_id(role),
            defer_submission=True, on_record=lambda rec: self._audit(jid, rec, actor="runner"))
        return deps, usable

    # ------------------------------------------------------------------ proposal outbox
    def _deliver_due(self) -> bool:
        due = self.jobs.due_deliveries()
        for job in due:
            try:
                self._deliver(job)
            except Exception:  # one bad delivery never blocks the others, expiry or new jobs
                log.exception("delivery of job %s failed unexpectedly; it stays pending", job["job_id"])
        return bool(due)

    def _deliver(self, job: dict) -> None:
        jid = job["job_id"]
        cid = proposal_command_id(jid)
        try:
            core = self._core_factory()
            res = core.command(cid, "submit_research_proposal", {"proposal": job["proposal"]},
                               {"id": agent_principal_id(job["role"]), "role": "AGENT"})
        except IpcError as exc:
            message = str(exc)
            if message.startswith("CommandConflict"):
                self.jobs.mark_delivery_failed(jid, message, final="CONFLICT")
                self._audit(jid, {"kind": "delivery_conflict", "command_id": cid, "error": message})
                log.error("proposal of job %s conflicts with a different payload under %s; kept, not retried", jid, cid)
                return
            self._retry_delivery(job, f"core refused the request: {message}")
            return
        except (OSError, ValueError) as exc:
            self._retry_delivery(job, f"core unreachable: {type(exc).__name__}: {exc}")
            return
        if not isinstance(res, Mapping) or "accepted" not in res:
            self._retry_delivery(job, "unexpected core response")
            return
        body = res.get("body") if isinstance(res.get("body"), Mapping) else {}
        if res.get("accepted"):
            self.jobs.mark_delivered(jid, res)
            self._delivery_errors.pop(jid, None)
            self._audit(jid, {"kind": "proposal_delivered", "command_id": cid, "duplicate": bool(res.get("duplicate")),
                              "proposal_id": body.get("proposal_id"), "attempt": job["delivery_attempts"] + 1})
            log.info("proposal of job %s recorded by the core as %s%s", jid, body.get("proposal_id"),
                     " (duplicate delivery)" if res.get("duplicate") else "")
            return
        error = f"core rejected the proposal: {canonical_json(sanitize(body))}"
        self.jobs.mark_delivery_failed(jid, error, final="REJECTED", core_result=res)
        self._audit(jid, {"kind": "delivery_rejected", "command_id": cid, "error": error})

    def _retry_delivery(self, job: dict, error: str) -> None:
        jid = job["job_id"]
        attempts = job["delivery_attempts"] + 1
        retry_at = self.clock.now_ns() + min(DELIVERY_MAX_BACKOFF_S, 2 ** min(attempts - 1, 16)) * NS
        n = self.jobs.mark_delivery_failed(jid, error, retry_at_ns=retry_at)
        if self._delivery_errors.get(jid) != error:  # audit the first failure and every change of cause
            self._audit(jid, {"kind": "delivery_failed", "command_id": proposal_command_id(jid), "error": error,
                              "attempt": n, "retry_at_ns": retry_at})
            self._delivery_errors[jid] = error
        log.warning("proposal of job %s not delivered (attempt %d): %s; kept and retried", jid, n, error)

    def readiness(self) -> list[tuple[bool, str]]:
        return readiness(self.paths, models_dir=self.models_dir, environ=self.environ, now_ns=self.clock.now_ns())


def readiness(paths: Paths, *, models_dir: Path | None = None, environ: Mapping[str, str] | None = None,
              now_ns: int | None = None) -> list[tuple[bool, str]]:
    """What a live research job needs on this machine, checked locally: no network call, no file written,
    and the key is only tested for presence, never printed."""
    models_dir = Path(models_dir) if models_dir else CONFIG_MODELS
    environ = os.environ if environ is None else environ
    now_ns = time.time_ns() if now_ns is None else now_ns
    policy_path = default_policy_path(paths.root)
    out: list[tuple[bool, str]] = []
    try:
        policy, problem = load_owner_policy(policy_path), None
    except (OwnerPolicyError, OSError, ValueError) as exc:
        policy, problem = None, f"invalid: {exc}"
    out.append((policy is not None, f"owner policy {policy_path}: "
                + ("present" if policy is not None else (problem or "absent (paid model calls are disabled)"))))
    cfg = policy.provider("anthropic") if policy is not None else None
    out.append((bool(cfg and cfg.enabled), "providers.anthropic enabled" if cfg and cfg.enabled else "providers.anthropic not enabled"))
    if cfg is not None:
        try:
            key_ok = resolve_api_key(cfg, environ=environ, secrets_dir=paths.secrets) is not None
            key_msg = "API key available" if key_ok else (f"no API key: set {cfg.api_key_env} for the worker or write "
                                                         f"{paths.secrets / (cfg.api_key_file or 'anthropic_api_key')} (mode 600)")
        except OwnerPolicyError as exc:
            key_ok, key_msg = False, f"API key file refused: {exc}"
        out.append((key_ok, key_msg))
    out.append((importlib.util.find_spec("anthropic") is not None,
                "anthropic SDK importable in this interpreter (use the agents venv: backend/requirements-agents.txt)"))
    try:
        approvals = load_approvals(models_dir / "approved.json")
        research = sorted(a.model_id for a in approvals if a.provider == "anthropic" and "research" in a.roles)
        out.append((bool(research), f"approved.json: anthropic models approved for role 'research': {research or 'none'}"))
    except (CatalogError, OSError, ValueError) as exc:
        out.append((False, f"approved.json invalid: {exc}"))
    if cfg is not None and cfg.default_model:
        try:
            ok = TariffBook.load(models_dir / "tariffs.json").is_routable("anthropic", cfg.default_model, at_ns=now_ns)
        except (TariffError, OSError, ValueError):
            ok = False
        out.append((ok, f"tariff row for {cfg.default_model}: {'owner-approved' if ok else 'missing or not approved'}"))
    token = paths.secrets / "agent_token"
    out.append((token.exists(), f"agent token {token}: " + ("present" if token.exists() else "missing (the core creates it at start)")))
    research_db = paths.root / "research" / "research.sqlite"
    out.append((research_db.exists(), f"research store {research_db}: "
                + ("present" if research_db.exists() else "missing (questions without issuer_ids still run)")))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Project Unknown model worker: runs queued research jobs (ADR-0016)")
    ap.add_argument("--home", default=None, help="runtime root (default ~/.unknown or $UNKNOWN_HOME)")
    ap.add_argument("--poll-seconds", type=float, default=2.0)
    ap.add_argument("--once", action="store_true", help="exit when no job is queued and no delivery is due")
    ap.add_argument("--check", action="store_true", help="print what a live job needs on this machine, then exit (no network)")
    ap.add_argument("--worker-id", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()
    if args.check:
        items = readiness(paths)
        for ok, text in items:
            print(("ok   " if ok else "MISS ") + text)
        raise SystemExit(0 if all(ok for ok, _ in items) else 1)
    worker = Worker(paths, worker_id=args.worker_id)
    try:
        worker.start()
    except WorkerError as exc:
        log.error("%s", exc)
        worker.close()
        raise SystemExit(2) from None
    for ok, text in worker.readiness():
        (log.info if ok else log.warning)("readiness: %s", text)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("model worker %s started (home %s)", worker.worker_id, paths.root)
    try:
        worker.run(stop, poll_s=args.poll_seconds, once=args.once)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
