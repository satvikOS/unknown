"""OPT-IN live smoke test: one tiny structured-output request to Anthropic through the full runner.

Runs only when ALL of these hold (otherwise it is skipped with the reason):
  * environment UNKNOWN_LIVE_ANTHROPIC=1 (the owner's explicit opt-in for this one paid call);
  * the ``anthropic`` SDK is importable (use the research venv; see backend/requirements-agents.txt);
  * <UNKNOWN_HOME>/config/agents.json exists, enables providers.anthropic and has a data_policy;
  * an API key resolves (env named in the policy, else <UNKNOWN_HOME>/secrets/<api_key_file>, mode 0600);
  * the tariff row for the policy's default_model in config/models/tariffs.json is owner-approved (approved_by set).

What it does: refreshes the live model catalog (list endpoint), approves the owner's default
model for this test's role IN MEMORY ONLY (the opt-in above is the owner's authorization for
this single call), and runs one job with max_output_tokens 150 (at most one truncation retry
at 300) and effort "low". The spend is reserved and settled in the REAL ledger
(<UNKNOWN_HOME>/agents/ledger.sqlite), so it counts against the owner's caps. The proposal is
sent to an in-memory fake core, never to the real core.

    UNKNOWN_LIVE_ANTHROPIC=1 PYTHONPATH=src <venv>/bin/python -m unittest tests.test_agents_live_anthropic -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import FakeCore, sha  # noqa: E402
from unknown.agents.budget import SpendLedger, default_ledger_path  # noqa: E402
from unknown.agents.catalog import Approval, Catalog  # noqa: E402
from unknown.agents.config import load_owner_policy, resolve_api_key  # noqa: E402
from unknown.agents.envelope import JobEnvelope, JobState  # noqa: E402
from unknown.agents.evidence import EvidenceSpan  # noqa: E402
from unknown.agents.gate import DataUseGate, load_provider_policies  # noqa: E402
from unknown.agents.providers.anthropic import AnthropicAdapter  # noqa: E402
from unknown.agents.providers.base import CircuitBreaker, RetryPolicy  # noqa: E402
from unknown.agents.runner import Candidate, JobInputs, JobRunner, RunnerDeps  # noqa: E402
from unknown.agents.tariffs import TariffBook  # noqa: E402
from unknown.agents.tools import ToolRegistry  # noqa: E402
from unknown.data.registry import parse_provider  # noqa: E402
from unknown.domain.clock import SystemClock  # noqa: E402

ROLE = os.environ.get("UNKNOWN_LIVE_ROLE", "research")
SPAN_TEXT = "Synthetic smoke-test span written by the test itself: The reference widget is painted blue."


def smoke_registry():
    return {"smoke": parse_provider({
        "provider_id": "smoke", "name": "Synthetic smoke-test text", "access": "in-test", "cost_model": "free",
        "budget": "none", "rate_limit": {"max_requests": 1, "per_seconds": 1.0, "source": "n/a"},
        "failure_behavior": "n/a", "sources": ["tests/test_agents_live_anthropic.py"], "verified_on": "2026-10-22",
        "datasets": [{"dataset_id": "smoke.synthetic", "description": "text authored by this test", "endpoint": "n/a",
                      "coverage": "n/a", "history_depth": "n/a", "update_frequency": "n/a", "availability_rule": "n/a",
                      "allowed_uses": ["model_processing_cloud"], "retention_days": None, "vintage_support": False,
                      "rights_source": "authored in this repository", "rights_verified_on": "2026-10-22"}]})}


class LowEffortAdapter(AnthropicAdapter):
    def effort_for(self, role):
        return "low"


@unittest.skipUnless(os.environ.get("UNKNOWN_LIVE_ANTHROPIC") == "1", "live test: set UNKNOWN_LIVE_ANTHROPIC=1 to opt in")
@unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic SDK not installed in this interpreter")
class LiveAnthropicSmokeTest(unittest.TestCase):
    def test_one_structured_call_end_to_end(self):
        policy = load_owner_policy()
        if policy is None or policy.provider("anthropic") is None or not policy.provider("anthropic").enabled:
            self.skipTest("owner policy absent or providers.anthropic not enabled")
        if policy.data_policy is None:
            self.skipTest("owner policy has no data_policy; the gate would block every call")
        cfg = policy.provider("anthropic")
        if resolve_api_key(cfg) is None:
            self.skipTest("no Anthropic API key available")
        model = cfg.default_model
        if not model:
            self.skipTest("providers.anthropic.default_model is not set")
        clock = SystemClock()
        tariffs = TariffBook.load()
        if not tariffs.is_routable("anthropic", model, at_ns=clock.now_ns()):
            self.skipTest(f"tariff row for {model} is not owner-approved; re-verify the price and set approved_by in config/models/tariffs.json")
        ledger = SpendLedger(policy, default_ledger_path(), clock_ns=clock.now_ns)
        self.addCleanup(ledger.close)
        policies = load_provider_policies()
        adapter = LowEffortAdapter(owner=policy, ledger=ledger, provider_policy=policies["providers"]["anthropic"])
        catalog = Catalog((Approval("anthropic", model, frozenset({ROLE}), "owner (live smoke opt-in)",
                                    time.strftime("%Y-%m-%d"), policy.policy_hash),),
                          clock.now_ns, policy.catalog_max_age_hours * 3600 * 1_000_000_000)
        snap = adapter.list_models(now_ns=clock.now_ns())
        self.assertTrue(snap.ok, snap.error)
        catalog.ingest(snap)
        self.assertIn(model, snap.rows, "the owner's default model is not in the live catalog")
        core = FakeCore()
        deps = RunnerDeps(adapters={"anthropic": adapter}, ledger=ledger, tariffs=tariffs,
                          gate=DataUseGate(smoke_registry(), policy, policies, clock.now_ns), catalog=catalog, core=core,
                          tools=ToolRegistry(), clock_ns=clock.now_ns, sleep_ns=lambda ns: time.sleep(ns / 1e9),
                          retry=RetryPolicy(0, 1000, 1000, 0), breaker=CircuitBreaker(1, 60_000_000_000, 300_000_000_000),
                          estimate=policy.estimate)
        span = EvidenceSpan("smoke-1", "smoke-doc", "smoke.synthetic", SPAN_TEXT, content_hash=sha(SPAN_TEXT))
        now = clock.now_ns()
        job_id = f"smoke-{uuid.uuid4().hex[:12]}"
        env = JobEnvelope(job_id=job_id, role=ROLE, schema_version=1, input_hashes=(sha(SPAN_TEXT),), allowed_tools=(),
                          deadline_ns=now + 300 * 1_000_000_000, max_turns=2, max_tool_calls=0, retry_budget=0,
                          token_cap=60_000, spend_cap=policy.caps.per_job, output_schema="research_answer", created_ns=now)
        inputs = JobInputs("State the widget's colour as a one-sentence hypothesis. Cite span smoke-1 with a verbatim quote. "
                           "Leave numbers empty.", (span,), 3600, 150)
        result = JobRunner(deps).run(env, inputs, [Candidate("anthropic", model)])
        print("\nlive smoke:", result.status.value, result.reason, [r for r in result.records if r["kind"] in ("reserved", "settled", "booked_at_reservation", "generated")])
        self.assertIn(result.status, (JobState.SUCCEEDED, JobState.SCHEMA_INVALID))
        self.assertLessEqual(result.turns_used, 2)
        self.assertEqual(len(core.commands), 1)
        self.assertEqual(core.commands[0]["kind"], "submit_research_proposal")
        self.assertTrue(all(c["max_tokens"] <= 300 for c in adapter.request_log if "max_tokens" in c))
        self.assertLessEqual(ledger.consumed(job_id=job_id), policy.caps.per_job.amount)


if __name__ == "__main__":
    unittest.main()
