"""Model worker end to end, offline (ADR-0016; CAP-AGT-01/02/09, AI-05).

The provider side is the real ``AnthropicAdapter`` over a fake SDK client, so "no network call"
means the client factory was never invoked and no client method ran. The core side is a fake
with the core's command-id semantics (same id + payload + principal = duplicate; a different
payload = CommandConflict); ``test_agents_worker_core.py`` runs the same path against the real
core service over its socket."""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import (FIX_TICKER, MODEL, SECOND, TempDirCase, owner, owner_doc,  # noqa: E402
                                  research_store, tariff_row, write_private)
from unknown.agents.audit import AuditTrail, default_audit_path  # noqa: E402
from unknown.agents.budget import SpendLedger, default_ledger_path  # noqa: E402
from unknown.agents.common import CONFIG_MODELS  # noqa: E402
from unknown.agents.jobstore import MAX_START_ATTEMPTS  # noqa: E402
from unknown.agents.providers.anthropic import AnthropicAdapter  # noqa: E402
from unknown.agents.submission import build_research_job  # noqa: E402
from unknown.agents.worker import Worker, WorkerError, readiness  # noqa: E402
from unknown.domain.canonical import content_hash  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.money import Money  # noqa: E402
from unknown.service.ipc import IpcError  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402

KEY_ENV = {"UNKNOWN_TEST_ANTHROPIC_KEY": "fixture-not-a-key"}
SRC = Path(__file__).resolve().parents[1] / "src"
FIELDS = ["us-gaap:Revenues", "us-gaap:Assets"]
QUESTION = "Did Fixture Industries grow revenue in fiscal 2025?"


class Crash(BaseException):
    """Stands in for the process dying at a chosen instant."""


# ---------------------------------------------------------------------------- fakes
class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls, self.count_calls = [], []
        self.before_return = None

    def create(self, **kw):
        self.calls.append(kw)
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        if self.before_return:
            self.before_return()
        return item

    def count_tokens(self, **kw):
        self.count_calls.append(kw)
        return SimpleNamespace(input_tokens=1200)


class FakeSDKClient:
    def __init__(self, outcomes=()):
        self.messages = FakeMessages(outcomes)
        self.list_calls = 0
        info = SimpleNamespace(id=MODEL, display_name="fixture model", created_at=None, lifecycle="active", deprecated_at=None,
                               retires_at=None, max_input_tokens=1_000_000, max_tokens=128_000,
                               capabilities=SimpleNamespace(structured_outputs=SimpleNamespace(supported=True)))

        def listing():
            self.list_calls += 1
            return iter([info])
        self.models = SimpleNamespace(list=listing)

    def network_calls(self) -> int:
        return self.list_calls + len(self.messages.calls) + len(self.messages.count_calls)


def message(text: str, *, input_tokens=1100, output_tokens=400, stop="end_turn"):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, cache_creation=None, inference_geo="global", iterations=None)
    m = SimpleNamespace(stop_reason=stop, stop_details=None, model=MODEL, usage=usage,
                        content=[SimpleNamespace(type="text", text=text)])
    m._request_id = "req_fixture"
    m.to_json = lambda: json.dumps({"stop_reason": stop, "model": MODEL})
    return m


def answer(spans) -> str:
    """A model answer whose two claims quote and cite the job's own spans (so they verify)."""
    by_period = {}
    for s in spans:
        for part in s["text"].split(" "):
            if part.startswith("period="):
                by_period[part[len("period="):]] = s
    claims = []
    for period in ("2024-09-29..2025-09-27", "2023-10-01..2024-09-28"):
        s = by_period[period]
        value = s["text"].split(" value=", 1)[1].split(".", 1)[0]
        quote = f"concept=us-gaap:Revenues period={period} (annual) unit=USD value={value}"
        assert quote in s["text"]
        claims.append({"text": f"Revenue was {value} USD for {period}.", "span_ids": [s["span_id"]], "quote": quote,
                       "numbers": [{"concept": "us-gaap:Revenues", "period": period, "unit": "USD", "value": value}]})
    return json.dumps({"hypothesis": "Revenue grew in fiscal 2025.", "claims": claims, "assumptions": [],
                       "abstain": False, "reason": "both annual values are in the evidence", "tool_requests": []})


class DedupCore:
    """Core command semantics (core/desk.py execute): same id + payload + principal returns the stored result
    as a duplicate; a different payload under the same id is a CommandConflict. Records proposals once."""

    def __init__(self):
        self.recorded: dict[str, tuple] = {}
        self.proposals: list[dict] = []
        self.calls: list[str] = []
        self.unreachable = False
        self.reject = False

    def command(self, command_id, kind, payload, principal):
        self.calls.append(command_id)
        if self.unreachable:
            raise ConnectionRefusedError(61, "Connection refused")
        assert kind == "submit_research_proposal" and principal["role"] == "AGENT"
        h = content_hash({"kind": kind, "payload": payload})
        if command_id in self.recorded:
            prev_hash, prev_principal, result = self.recorded[command_id]
            if (prev_hash, prev_principal) == (h, principal["id"]):
                return {**result, "duplicate": True, "events": []}
            raise IpcError(f"CommandConflict: command id {command_id} reused with a different payload")
        if self.reject:
            result = {"accepted": False, "duplicate": False, "body": {"code": "PROPOSAL_INVALID", "message": "fixture"}, "events": [1]}
        else:
            self.proposals.append({"proposal_id": f"prop-{len(self.proposals) + 1}", "author": principal["id"],
                                   "body": copy.deepcopy(payload["proposal"])})
            result = {"accepted": True, "duplicate": False, "body": {"proposal_id": self.proposals[-1]["proposal_id"]},
                      "events": [len(self.proposals)]}
        self.recorded[command_id] = (h, principal["id"], result)
        return result


# ---------------------------------------------------------------------------- harness
class WorkerHarness(TempDirCase):
    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)            # expected warnings (retries, recoveries) are asserted, not printed
        self.addCleanup(logging.disable, logging.NOTSET)
        self.home = self.tmp / "home"
        self.paths = Paths(self.home)
        self.models = self.tmp / "models"
        self.models.mkdir()
        shutil.copy(CONFIG_MODELS / "provider_policies.json", self.models / "provider_policies.json")
        (self.models / "tariffs.json").write_text(json.dumps({"schema_version": 1, "rows": [tariff_row()]}))
        self.approve()
        self.write_policy()
        research_store(self.home / "research" / "research.sqlite").close()
        self.client = FakeSDKClient()
        self.keys_seen: list[str] = []
        self.core = DedupCore()
        self.workers: list[Worker] = []
        self.environ = dict(KEY_ENV)

    def tearDown(self):
        for w in self.workers:
            w.close()
        super().tearDown()

    # configuration -------------------------------------------------------------
    def write_policy(self, **overrides):
        doc = owner_doc(**overrides)
        doc.setdefault("concurrency", {"global": 1, "per_role": {"research": 1}})
        write_private(self.home / "config" / "agents.json", json.dumps(doc))

    def approve(self, roles=("research",)):
        approvals = [{"provider": "anthropic", "model_id": MODEL, "roles": list(roles), "approved_by": "owner-test",
                      "approved_at": "2026-10-22", "policy_version": "test"}] if roles else []
        (self.models / "approved.json").write_text(json.dumps({"schema_version": 1, "approvals": approvals}))

    def factory(self, ctx):
        def build(key):
            self.keys_seen.append(key)
            return self.client
        return AnthropicAdapter(owner=ctx.policy, ledger=ctx.ledger, provider_policy=ctx.provider_policies["providers"]["anthropic"],
                                client_factory=build, environ=self.environ, secrets_dir=ctx.paths.secrets)

    def worker(self, **kw) -> Worker:
        w = Worker(self.paths, clock=self.clock, sleep_ns=self.clock.advance, core_factory=kw.pop("core_factory", lambda: self.core),
                   adapter_factories={"anthropic": self.factory}, models_dir=self.models, environ=self.environ,
                   worker_id=kw.pop("worker_id", f"w{len(self.workers) + 1}"), **kw)
        self.workers.append(w)
        return w

    def submit(self, w: Worker, question=QUESTION, **body) -> str:
        req = {"question": question, "issuer_ids": [FIX_TICKER], "fields": FIELDS, "max_output_tokens": 2000, **body}
        env, inputs = build_research_job(req, policy=owner(), research_path=w.research_path, now_ns=self.clock.now_ns())
        res = w.jobs.submit(env, inputs, submitted_by="owner")
        self.assertTrue(res.created)
        return res.job_id

    def ledger(self) -> SpendLedger:
        led = SpendLedger(owner(), default_ledger_path(self.home), clock_ns=self.clock.now_ns)
        self.addCleanup(led.close)
        return led

    def audit_kinds(self, w: Worker, job_id: str) -> list[str]:
        return [r["kind"] for r in w.audit.records(job_id)]

    def assert_no_network(self):
        self.assertEqual(self.keys_seen, [], "no SDK client may be built")
        self.assertEqual(self.client.network_calls(), 0)


class WorkerEndToEndTest(WorkerHarness, unittest.TestCase):
    def test_a_job_runs_under_the_caps_and_its_proposal_is_recorded_once(self):
        w = self.worker()
        jid = self.submit(w)
        spans = w.jobs.get(jid)["inputs"]["evidence"]["spans"]
        self.client.messages.outcomes = [message(answer(spans))]
        self.assertTrue(w.step())
        job = w.jobs.get(jid)
        self.assertEqual((job["state"], job["outcome"], job["delivery"]), ("SUCCEEDED", "succeeded", "DELIVERED"), job["reason"])
        self.assertEqual(len(self.core.proposals), 1)
        prop = self.core.proposals[0]
        self.assertEqual(prop["author"], "agent.research")
        self.assertEqual(prop["body"], job["proposal"])
        self.assertFalse(prop["body"]["abstain"])
        self.assertEqual(len(prop["body"]["evidence"]), 2)
        self.assertEqual(job["core_result"]["body"]["proposal_id"], "prop-1")
        self.assertEqual(self.core.calls, [f"agent.proposal.{jid}"])
        # the request that left: pinned model, explicit effort and region, the evidence as untrusted data
        kw = self.client.messages.calls[0]
        self.assertEqual((kw["model"], kw["output_config"]["effort"], kw["inference_geo"], kw["max_tokens"]),
                         (MODEL, "low", "global", 2000))
        self.assertIn("<<<UNTRUSTED evidence_span id=S1 dataset=sec.companyfacts", kw["messages"][0]["content"])
        self.assertEqual(self.client.list_calls, 1)
        # spend: the probe released, the token count admitted at zero, the call settled exactly
        rows = self.ledger().reservations(jid)
        self.assertEqual([(r["purpose"], r["state"]) for r in rows],
                         [("admission_probe", "RELEASED"), ("count_tokens", "SETTLED"), ("generate", "SETTLED")])
        self.assertEqual(Fixed.parse(rows[2]["actual"]), Fixed.parse("0.0124"))     # 1100 x 4.00 + 400 x 20.00 per million
        # audit: every runner record and every worker action, chained
        kinds = self.audit_kinds(w, jid)
        for k in ("job_started", "candidates", "budget_admitted", "catalog_refreshed", "gate", "reserved", "settled",
                  "generated", "verified", "terminal", "proposal_ready", "job_finished", "proposal_delivered"):
            self.assertIn(k, kinds)
        self.assertLess(kinds.index("reserved"), kinds.index("generated"))
        self.assertEqual(w.audit.verify(), [])
        self.assertFalse(w.step())                        # nothing left to do

    def test_catalog_refresh_happens_at_most_once_per_max_age_even_across_restarts(self):
        w = self.worker()
        for i in range(2):
            jid = self.submit(w, question=f"{QUESTION} ({i})")
            self.client.messages.outcomes.append(message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"])))
            w.step()
            self.assertEqual(w.jobs.get(jid)["state"], "SUCCEEDED")
        self.assertEqual(self.client.list_calls, 1)
        w.close()
        self.workers.remove(w)
        w2 = self.worker()
        jid = self.submit(w2, question=f"{QUESTION} (restarted)")
        self.client.messages.outcomes.append(message(answer(w2.jobs.get(jid)["inputs"]["evidence"]["spans"])))
        w2.step()
        self.assertEqual(self.client.list_calls, 1)                      # restored the persisted snapshot
        self.assertIn("catalog_restored", self.audit_kinds(w2, jid))
        self.clock.advance(25 * 3600 * SECOND)
        jid = self.submit(w2, question=f"{QUESTION} (next day)")
        self.client.messages.outcomes.append(message(answer(w2.jobs.get(jid)["inputs"]["evidence"]["spans"])))
        w2.step()
        self.assertEqual(self.client.list_calls, 2)

    def test_core_unreachable_then_reachable_the_proposal_is_kept_and_delivered(self):
        w = self.worker()
        jid = self.submit(w)
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"]))]
        self.core.unreachable = True
        w.step()
        job = w.jobs.get(jid)
        self.assertEqual((job["state"], job["delivery"], job["delivery_attempts"]), ("SUCCEEDED", "PENDING", 1))
        self.assertIn("core unreachable", job["delivery_error"])
        stored = job["proposal"]
        self.assertFalse(w.step())                                   # backoff: not due yet
        self.assertEqual(len(self.core.calls), 1)
        self.clock.advance(SECOND)
        w.step()                                                     # second failure, backoff doubles
        self.assertEqual(w.jobs.get(jid)["delivery_attempts"], 2)
        self.core.unreachable = False
        self.clock.advance(2 * SECOND)
        w.step()
        job = w.jobs.get(jid)
        self.assertEqual((job["delivery"], job["delivery_attempts"]), ("DELIVERED", 3))
        self.assertEqual(self.core.proposals[0]["body"], stored)
        kinds = self.audit_kinds(w, jid)
        self.assertEqual(kinds.count("delivery_failed"), 1)          # one record per distinct cause
        self.assertEqual(kinds.count("proposal_delivered"), 1)
        self.assertEqual(len(self.client.messages.calls), 1)         # the model was never asked twice

    def test_crash_after_the_core_accepted_but_before_marking_delivered_records_once(self):
        w = self.worker()
        jid = self.submit(w)
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"]))]
        original = w.jobs.mark_delivered

        def die(*a, **kw):
            raise Crash()
        w.jobs.mark_delivered = die
        with self.assertRaises(Crash):
            w.step()
        w.jobs.mark_delivered = original
        self.assertEqual(len(self.core.proposals), 1)
        self.assertEqual(w.jobs.get(jid)["delivery"], "PENDING")
        w.close()
        self.workers.remove(w)
        restarted = self.worker()
        self.assertEqual(restarted.recover(), [])                    # the job itself had finished
        restarted.step()
        job = restarted.jobs.get(jid)
        self.assertEqual(job["delivery"], "DELIVERED")
        self.assertTrue(job["core_result"]["duplicate"])
        self.assertEqual(len(self.core.proposals), 1)
        self.assertEqual(self.core.calls, [f"agent.proposal.{jid}"] * 2)
        delivered = [r["record"] for r in restarted.audit.records(jid) if r["kind"] == "proposal_delivered"]
        self.assertEqual([d["duplicate"] for d in delivered], [True])
        self.assertEqual(len(self.client.messages.calls), 1)

    def test_a_failed_store_write_at_the_end_of_a_job_is_retried_and_the_proposal_kept(self):
        w = self.worker()
        jid = self.submit(w)
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"]))]
        original = w.jobs.finish
        calls = []

        def busy_once(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(*a, **kw)
        w.jobs.finish = busy_once
        w.step()
        self.assertEqual(w.jobs.get(jid)["state"], "RUNNING")
        self.assertEqual(self.core.calls, [])
        w.step()
        job = w.jobs.get(jid)
        self.assertEqual((job["state"], job["delivery"]), ("SUCCEEDED", "DELIVERED"))
        self.assertEqual(len(self.core.proposals), 1)
        self.assertEqual(len(self.client.messages.calls), 1)                 # the paid answer was not asked for twice

    def test_one_failing_delivery_never_blocks_other_jobs(self):
        w = self.worker()
        first = self.submit(w)
        self.client.messages.outcomes = [message(answer(w.jobs.get(first)["inputs"]["evidence"]["spans"]))]
        self.core.command = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("unexpected core client failure"))
        w.step()
        self.assertEqual(w.jobs.get(first)["delivery"], "PENDING")
        second = self.submit(w, question="another question")
        self.client.messages.outcomes = [message(answer(w.jobs.get(second)["inputs"]["evidence"]["spans"]))]
        w.step()
        self.assertEqual(w.jobs.get(second)["state"], "SUCCEEDED")
        self.assertEqual(w.jobs.get(first)["delivery"], "PENDING")

    def test_rejected_and_conflicting_deliveries_are_kept_not_retried(self):
        w = self.worker()
        jid = self.submit(w)
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"]))]
        self.core.reject = True
        w.step()
        job = w.jobs.get(jid)
        self.assertEqual(job["delivery"], "REJECTED")
        self.assertIsNotNone(job["proposal"])
        self.assertIn("delivery_rejected", self.audit_kinds(w, jid))
        jid2 = self.submit(w, question="another question")
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid2)["inputs"]["evidence"]["spans"]))]
        self.core.reject = False
        self.core.recorded[f"agent.proposal.{jid2}"] = ("other-payload-hash", "agent.research", {})
        w.step()
        self.assertEqual(w.jobs.get(jid2)["delivery"], "CONFLICT")
        self.clock.advance(3600 * SECOND)
        self.assertFalse(w.step())


class WorkerFailsClosedTest(WorkerHarness, unittest.TestCase):
    def run_one(self, w: Worker, jid: str) -> dict:
        w.step()
        return w.jobs.get(jid)

    def test_budget_exhaustion_fails_the_job_without_any_network_call(self):
        led = self.ledger()
        for i in range(7):                                           # 7 x 2.00 + 1.00 = the 15.00 daily cap
            led.reserve(job_id=f"earlier-{i}", role="research", provider="anthropic", model_id=MODEL, amount=Money("USD", Fixed.parse("2.00")))
        led.reserve(job_id="earlier-7", role="research", provider="anthropic", model_id=MODEL, amount=Money("USD", Fixed.parse("1.00")))
        w = self.worker()
        jid = self.submit(w)
        job = self.run_one(w, jid)
        self.assertEqual((job["state"], job["outcome"]), ("FAILED", "budget_exhausted"))
        self.assertIn("before any provider call", job["reason"])
        self.assertIn("day", job["reason"])
        self.assert_no_network()
        self.assertEqual(self.core.calls, [])
        self.assertIn("budget_refused", self.audit_kinds(w, jid))
        self.assertEqual(led.reservations(jid), [])                  # the refused probe left no row

    def test_absent_or_invalid_owner_policy_fails_closed_without_network(self):
        w = self.worker()
        jid = self.submit(w)
        (self.home / "config" / "agents.json").unlink()
        job = self.run_one(w, jid)
        self.assertEqual((job["state"], job["outcome"]), ("FAILED", "denied_by_policy"))
        self.assertIn("no owner policy", job["reason"])
        self.write_policy()
        os.chmod(self.home / "config" / "agents.json", 0o666)
        jid2 = self.submit(w, question="second")
        job = self.run_one(w, jid2)
        self.assertEqual(job["state"], "FAILED")
        self.assertIn("is invalid", job["reason"])
        self.assert_no_network()
        self.assertEqual(self.core.calls, [])

    def test_disabled_provider_fails_closed_without_network(self):
        w = self.worker()
        jid = self.submit(w)
        self.write_policy(providers__anthropic__enabled=False)
        job = self.run_one(w, jid)
        self.assertEqual((job["state"], job["outcome"]), ("FAILED", "denied_by_policy"))
        self.assertIn("no provider is enabled", job["reason"])
        self.assertIn("anthropic", job["reason"])
        self.assert_no_network()

    def test_an_unapproved_model_is_never_called(self):
        self.approve(roles=())
        w = self.worker()
        jid = self.submit(w)
        job = self.run_one(w, jid)
        self.assertEqual(job["state"], "FAILED")
        self.assertIn("no model is approved for role 'research'", job["reason"])
        self.approve(roles=("quant",))
        jid2 = self.submit(w, question="second")
        self.assertIn("no model is approved", self.run_one(w, jid2)["reason"])
        self.assert_no_network()

    def test_missing_key_or_unapproved_tariff_fails_without_network(self):
        self.environ.clear()
        w = self.worker()
        jid = self.submit(w)
        job = self.run_one(w, jid)
        self.assertIn("api_key_missing", job["reason"])
        self.environ.update(KEY_ENV)
        (self.models / "tariffs.json").write_text(json.dumps({"schema_version": 1, "rows": [tariff_row(approved=None)]}))
        jid2 = self.submit(w, question="second")
        self.assertIn("tariff", self.run_one(w, jid2)["reason"])
        self.assert_no_network()

    def test_a_role_without_a_concurrency_limit_never_runs(self):
        self.write_policy(concurrency={"global": 1, "per_role": {}})
        w = self.worker()
        jid = self.submit(w)
        job = self.run_one(w, jid)
        self.assertEqual((job["state"], job["outcome"]), ("FAILED", "denied_by_policy"))
        self.assertIn("concurrency.per_role.research", job["reason"])
        self.assert_no_network()

    def test_evidence_that_no_longer_matches_the_submission_is_refused(self):
        w = self.worker()
        jid = self.submit(w)
        conn = sqlite3.connect(w.jobs.path, isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("UPDATE jobs SET inputs = replace(inputs, 'value=1200000', 'value=9999999') WHERE job_id = ?", (jid,))
        job = self.run_one(w, jid)
        self.assertEqual((job["state"], job["outcome"]), ("FAILED", "denied_by_policy"))
        self.assertIn("evidence", job["reason"])
        self.assert_no_network()

    def test_queued_job_past_its_deadline_expires_without_running(self):
        w = self.worker()
        jid = self.submit(w, deadline_seconds=60)
        self.clock.advance(61 * SECOND)
        job = self.run_one(w, jid)
        self.assertEqual(job["state"], "EXPIRED")
        self.assertIn("expired", self.audit_kinds(w, jid))
        self.assert_no_network()

    def test_cancel_while_running_discards_the_output(self):
        w = self.worker()
        jid = self.submit(w)
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"]))]
        self.client.messages.before_return = lambda: w.jobs.cancel(jid, by="owner")
        job = self.run_one(w, jid)
        self.assertEqual((job["state"], job["outcome"], job["delivery"]), ("CANCELLED", "cancelled_by_owner", "NONE"))
        self.assertIsNotNone(job["discarded_output_hash"])
        self.assertIsNone(job["proposal"])
        self.assertEqual(self.core.calls, [])
        self.assertIn("discarded", self.audit_kinds(w, jid))
        generate = [r for r in self.ledger().reservations(jid) if r["purpose"] == "generate"]
        self.assertEqual([r["state"] for r in generate], ["SETTLED"])     # the spend is real and stays booked

    def test_an_unclassified_sdk_exception_keeps_the_reservation_unconfirmed(self):
        w = self.worker()
        jid = self.submit(w)
        self.client.messages.outcomes = [RuntimeError("unexpected SDK failure")]
        job = self.run_one(w, jid)
        self.assertEqual((job["state"], job["outcome"]), ("FAILED", "worker_error"))
        generate = [r for r in self.ledger().reservations(jid) if r["purpose"] == "generate"]
        self.assertEqual([r["state"] for r in generate], ["UNCONFIRMED"])
        self.assertIn("worker_error", self.audit_kinds(w, jid))


class WorkerReadinessTest(WorkerHarness, unittest.TestCase):
    def test_check_lists_what_a_live_job_needs_without_side_effects(self):
        items = readiness(self.paths, models_dir=self.models, environ={}, now_ns=self.clock.now_ns())
        by_ok = {text.split(":")[0]: ok for ok, text in items}
        self.assertTrue(by_ok[f"owner policy {self.home / 'config' / 'agents.json'}"])
        self.assertFalse([ok for ok, text in items if text.startswith("no API key")][0])
        self.assertTrue([ok for ok, text in items if text.startswith("approved.json")][0])
        with_key = readiness(self.paths, models_dir=self.models, environ=KEY_ENV, now_ns=self.clock.now_ns())
        self.assertIn((True, "API key available"), with_key)
        self.assertFalse(any("fixture-not-a-key" in text for _, text in with_key))
        self.assertFalse((self.home / "agents").exists())
        out = subprocess.run([sys.executable, "-m", "unknown.agents.worker", "--check", "--home", str(self.home)],
                             capture_output=True, text=True, timeout=120, cwd=str(SRC.parent),
                             env={k: v for k, v in {**os.environ, "PYTHONPATH": str(SRC)}.items() if k != "ANTHROPIC_API_KEY"})
        self.assertEqual(out.returncode, 1, out.stderr)                 # the repository's approved.json approves no research model
        self.assertIn("MISS approved.json", out.stdout)
        self.assertFalse((self.home / "agents").exists())


class WorkerRecoveryTest(WorkerHarness, unittest.TestCase):
    def claimed_by_a_dead_worker(self, w: Worker, question=QUESTION, attempts=1) -> str:
        jid = self.submit(w, question=question)
        w.jobs.claim_next("dead-worker", max_global=5, max_per_role=None)
        for _ in range(attempts - 1):
            w.jobs.requeue(jid, "fixture")
            w.jobs.claim_next("dead-worker", max_global=5, max_per_role=None)
        return jid

    def test_a_job_that_may_have_sent_is_failed_and_its_open_reservation_kept(self):
        w = self.worker()
        jid = self.claimed_by_a_dead_worker(w)
        led = self.ledger()
        res = led.reserve(job_id=jid, role="research", provider="anthropic", model_id=MODEL, amount=Money("USD", Fixed.parse("0.05")))
        actions = w.recover()
        self.assertEqual([(a["job_id"], a["action"]) for a in actions], [(jid, "failed")])
        job = w.jobs.get(jid)
        self.assertEqual((job["state"], job["outcome"], job["reason"]), ("FAILED", "worker_restarted", "worker restarted"))
        self.assertEqual(led.state(res.reservation_id)["state"], "UNCONFIRMED")
        self.assertEqual(led.consumed(job_id=jid), Fixed.parse("0.05"))
        rec = [r["record"] for r in w.audit.records(jid) if r["kind"] == "recovered"][0]
        self.assertEqual(rec["marked_unconfirmed"], [res.reservation_id])

    def test_a_job_that_never_sent_is_requeued_and_then_runs(self):
        w = self.worker()
        jid = self.claimed_by_a_dead_worker(w)
        led = self.ledger()
        probe = led.reserve(job_id=jid, role="research", provider="anthropic", model_id=MODEL, amount=Money("USD", Fixed.parse("0.04")),
                            purpose="admission_probe")
        led.release(probe)
        self.assertEqual([a["action"] for a in w.recover()], ["requeued"])
        self.assertEqual(w.jobs.get(jid)["state"], "QUEUED")
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"]))]
        w.step()
        self.assertEqual(w.jobs.get(jid)["state"], "SUCCEEDED")
        self.assertEqual(len(self.core.proposals), 1)

    def test_an_interrupted_admission_probe_is_released_not_counted_as_sent(self):
        w = self.worker()
        jid = self.claimed_by_a_dead_worker(w)
        led = self.ledger()
        probe = led.reserve(job_id=jid, role="research", provider="anthropic", model_id=MODEL, amount=Money("USD", Fixed.parse("0.04")),
                            purpose="admission_probe")
        self.assertEqual([a["action"] for a in w.recover()], ["requeued"])
        self.assertEqual(led.state(probe.reservation_id)["state"], "RELEASED")
        self.assertEqual(led.consumed(job_id=jid), Fixed(0))

    def test_a_job_cancelled_while_running_is_not_requeued_after_a_restart(self):
        w = self.worker()
        never_sent = self.claimed_by_a_dead_worker(w)
        w.jobs.cancel(never_sent, by="owner")
        sent = self.claimed_by_a_dead_worker(w, question="second")
        w.jobs.cancel(sent, by="owner")
        led = self.ledger()
        res = led.reserve(job_id=sent, role="research", provider="anthropic", model_id=MODEL, amount=Money("USD", Fixed.parse("0.05")))
        self.assertEqual(sorted(a["action"] for a in w.recover()), ["cancelled", "cancelled"])
        for jid in (never_sent, sent):
            self.assertEqual((w.jobs.get(jid)["state"], w.jobs.get(jid)["outcome"]), ("CANCELLED", "cancelled_by_owner"))
        self.assertEqual(led.state(res.reservation_id)["state"], "UNCONFIRMED")      # possibly sent: stays reserved
        self.assertFalse(w.step())
        self.assert_no_network()

    def test_repeated_restarts_before_sending_eventually_fail(self):
        w = self.worker()
        jid = self.claimed_by_a_dead_worker(w, attempts=MAX_START_ATTEMPTS)
        self.assertEqual([a["action"] for a in w.recover()], ["failed"])
        self.assertIn("before any provider call", w.jobs.get(jid)["reason"])

    def test_one_worker_per_home(self):
        a, b = self.worker(), self.worker()
        a.start()
        with self.assertRaises(WorkerError):
            b.start()
        a.close()
        self.workers.remove(a)
        b.start()

    def test_audit_trail_is_shared_and_verifiable_after_restart(self):
        w = self.worker()
        jid = self.claimed_by_a_dead_worker(w)
        w.recover()
        w.close()
        self.workers.remove(w)
        trail = AuditTrail(default_audit_path(self.home), clock_ns=self.clock.now_ns, readonly=True)
        self.addCleanup(trail.close)
        self.assertEqual([r["kind"] for r in trail.records(jid)], ["recovered"])
        self.assertEqual(trail.verify(), [])


if __name__ == "__main__":
    unittest.main()
