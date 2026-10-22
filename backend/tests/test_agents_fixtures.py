"""Shared fixtures for the agent-layer tests (no TestCase here).

Every number, id and document below is a TEST FIXTURE. Model ids appear only in tests and
configuration, never in application source (CAP-MODEL-01 acceptance criterion)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from unknown.agents.budget import SpendLedger
from unknown.agents.catalog import Approval, Capability, Catalog, CatalogSnapshot, ModelRow
from unknown.agents.config import parse_owner_policy
from unknown.agents.envelope import JobEnvelope
from unknown.agents.gate import DataUseGate, load_provider_policies
from unknown.agents.providers.base import (CircuitBreaker, GenerationResult, OutputStatus, ProviderAdapter, RetryPolicy,
                                           TokenCount)
from unknown.agents.tariffs import TariffBook, Usage, parse_row
from unknown.agents.tools import ToolRegistry
from unknown.data.registry import parse_provider
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed
from unknown.domain.money import Money

T0 = utc_ns(2026, 10, 22, 14, 0, 0)
MODEL = "claude-opus-5-5"
SECOND = 1_000_000_000


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def owner_doc(**overrides) -> dict:
    doc = {
        "schema_version": 1,
        "comment": "test fixture",
        "currency": "USD",
        "caps": {"lifetime": "200.00", "per_day": "15.00", "per_job": "2.00",
                 "per_provider": {"anthropic": {"lifetime": "200.00", "per_day": "15.00"}}},
        "providers": {
            "anthropic": {"enabled": True, "api_key_env": "UNKNOWN_TEST_ANTHROPIC_KEY", "region": "global",
                          "default_model": MODEL, "effort": {"default": "medium", "by_role": {"research": "low"}},
                          "refusal_fallback": "off",
                          "account": {"training_permission_granted": False, "zdr_enabled": False}},
        },
        "data_policy": {"max_policy_age_days": 30,
                        "default": {"max_provider_retention_days": 30, "allowed_processing_regions": ["any"],
                                    "allowed_storage_regions": ["any"]}},
        "retry": {"max_attempts": 2, "base_ms": 10, "max_ms": 100, "jitter_ms": 0},
        "circuit_breaker": {"failures": 3, "window_s": 60, "cooldown_s": 300},
        "token_estimate": {"bytes_per_token_factor": "1", "overhead_tokens": 64},
        "catalog": {"max_age_hours": 24},
    }
    for dotted, value in overrides.items():
        target = doc
        parts = dotted.split("__")
        for p in parts[:-1]:
            target = target[p]
        if value is DELETE:
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = value
    return doc


DELETE = object()


def owner(**overrides):
    return parse_owner_policy(owner_doc(**overrides))


def tariff_row(provider="anthropic", model=MODEL, inp="4.00", out="20.00", cached="0.20", lo=0, hi=None,
               status="verified", frm="2026-10-01", until=None, approved="owner-test", cache_write=None):
    return {
        "provider": provider, "model_id": model, "tier": "standard", "region": "global",
        "band": {"measure": "prompt_tokens", "min_tokens": lo, "max_tokens": hi, "status": status,
                 "rule": "all_tokens_at_band_rate" if status == "verified" else None, "note": "fixture"},
        "currency": "USD", "per_million": {"input": inp if status == "verified" else None,
                                           "output": out if status == "verified" else None,
                                           "cached_input": cached if status == "verified" else None},
        "cache_write": cache_write or {"multipliers": {"5m": "1.25", "1h": "2"}, "status": "unverified"},
        "effective_from": frm, "effective_until": until, "source_url": "https://example.invalid/fixture",
        "retrieved_on": "2026-10-22", "records": ["fixture"], "approved_by": approved,
    }


def book(*rows) -> TariffBook:
    return TariffBook(parse_row(r, i) for i, r in enumerate(rows))


def registry():
    return {"fixture": parse_provider({
        "provider_id": "fixture", "name": "Fixture provider", "access": "test", "cost_model": "free", "budget": "none",
        "rate_limit": {"max_requests": 1, "per_seconds": 1.0, "source": "fixture"},
        "failure_behavior": "test", "sources": ["fixture"], "verified_on": "2026-10-22",
        "datasets": [
            {"dataset_id": "fixture.public", "description": "public filings (fixture)", "endpoint": "x", "coverage": "x",
             "history_depth": "x", "update_frequency": "x", "availability_rule": "x",
             "allowed_uses": ["display", "storage", "model_processing_cloud"], "retention_days": None,
             "vintage_support": False, "rights_source": "fixture", "rights_verified_on": "2026-10-22"},
            {"dataset_id": "fixture.restricted", "description": "licensed feed (fixture)", "endpoint": "x", "coverage": "x",
             "history_depth": "x", "update_frequency": "x", "availability_rule": "x",
             "allowed_uses": ["display"], "retention_days": None,
             "vintage_support": False, "rights_source": "fixture", "rights_verified_on": "2026-10-22"},
        ]})}


def envelope(**kw) -> JobEnvelope:
    base = dict(job_id="job-1", role="research", schema_version=1, input_hashes=(sha("span-1"),),
                allowed_tools=(), deadline_ns=T0 + 600 * SECOND, max_turns=3, max_tool_calls=4, retry_budget=2,
                token_cap=200_000, spend_cap=Money("USD", Fixed.parse("2.00")), output_schema="research_answer",
                created_ns=T0)
    base.update(kw)
    return JobEnvelope(**base)


def catalog(clock, *, approvals=None, model=MODEL, provider="anthropic", lifecycle="active", max_input=1_000_000):
    approvals = approvals if approvals is not None else (
        Approval(provider, model, frozenset({"research"}), "owner-test", "2026-10-22", "test"),)
    cat = Catalog(approvals, clock.now_ns, 24 * 3600 * SECOND)
    row = ModelRow(provider, model, lifecycle=lifecycle, max_input_tokens=max_input, max_output_tokens=128_000,
                   capabilities={"structured_outputs": Capability(True, "api", "2026-10-22")})
    cat.ingest(CatalogSnapshot.success(provider, clock.now_ns(), "/v1/models", b"fixture", [row]))
    return cat


class FakeCore:
    """Records commands like CoreClient.command; never mutates anything."""

    def __init__(self, fail: Exception | None = None):
        self.commands = []
        self.fail = fail

    def command(self, command_id, kind, payload, principal):
        self.commands.append({"command_id": command_id, "kind": kind, "payload": copy.deepcopy(payload),
                              "principal": dict(principal)})
        if self.fail:
            raise self.fail
        return {"accepted": True, "duplicate": False, "body": {"proposal_id": "prop-fixture"}, "events": []}


class FakeAdapter(ProviderAdapter):
    """Scripted provider: each script entry is a GenerationResult or an exception to raise."""
    provider_id = "anthropic"

    def __init__(self, script, *, model=MODEL, effort="low", extra_models=(), region="global"):
        self.script = list(script)
        self.calls = []
        self.model = model
        self.effort = effort
        self.extra = tuple(extra_models)
        self.region = region

    def list_models(self, *, now_ns):  # pragma: no cover - not used
        raise NotImplementedError

    def count_tokens(self, request, *, admission, gate):
        return TokenCount(request.model_id, 1000, "provider_endpoint")

    def generate_structured(self, request, *, reservation, gate):
        assert gate.allow and gate.request_hash == request.request_hash()
        self.calls.append({"request": request, "reservation": reservation, "gate": gate})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def unverified_requirements(self, operation):
        return ()

    def effort_for(self, role):
        return self.effort

    def additional_billed_models(self, request):
        return self.extra

    def requested_region(self):
        return self.region


def answer(**kw) -> str:
    body = {"hypothesis": "Fixture revenue grew year over year.",
            "claims": [{"text": "Revenue was 1000000 USD in FY2025.", "span_ids": ["span-1"],
                        "quote": "Total revenue was $1,000,000", "numbers": [
                            {"concept": "Revenues", "period": "FY2025", "unit": "USD", "value": "1000000"}]}],
            "assumptions": ["fixture assumption"], "abstain": False, "reason": "evidence supports it",
            "tool_requests": []}
    body.update(kw)
    return json.dumps(body)


def result(text=None, *, status=OutputStatus.COMPLETE, input_tokens=900, output_tokens=200, model=MODEL,
           usage_complete=True, fallback=False, geo="global", usages=None, category=None):
    us = usages if usages is not None else (Usage("anthropic", model, input_tokens=input_tokens, output_tokens=output_tokens),)
    return GenerationResult(status, text if text is not None else answer(), us, usage_complete, model, "req_fixture",
                            sha("raw"), served_by_fallback=fallback, refusal_category=category, inference_geo=geo)


def deps_kwargs(tmp: Path, clock: ManualClock, *, owner_policy="default", adapters=None, core=None, tariffs=None,
                cat=None, tools=None, sleeps=None):
    from unknown.agents.runner import RunnerDeps
    pol = owner() if owner_policy == "default" else owner_policy
    ledger = SpendLedger(pol, tmp / "ledger.sqlite", clock_ns=clock.now_ns)
    gate = DataUseGate(registry(), pol, load_provider_policies(), clock.now_ns)
    sleeps = sleeps if sleeps is not None else []
    def sleep(ns):
        sleeps.append(ns)
        clock.advance(ns)
    return RunnerDeps(
        adapters=adapters or {}, ledger=ledger, tariffs=tariffs or book(tariff_row()), gate=gate,
        catalog=cat or catalog(clock), core=core or FakeCore(), tools=tools or ToolRegistry(),
        clock_ns=clock.now_ns, sleep_ns=sleep, retry=RetryPolicy(2, 10, 100, 0),
        breaker=CircuitBreaker(3, 60 * SECOND, 300 * SECOND),
        estimate=(pol.estimate if pol else parse_owner_policy(owner_doc()).estimate))


SPAN_OBSERVED = T0 - 30 * 86_400 * SECOND      # fixture: the fact's period end
SPAN_AVAILABLE = T0 - 7 * 86_400 * SECOND       # fixture: when the filing became available


def spans():
    """One span with typed provenance (research_proposal v2): a document span of a fixture filing."""
    from unknown.agents.evidence import EvidenceSpan
    return (EvidenceSpan("span-1", "doc-1", "fixture.public", "Fixture Industries 10-K. Total revenue was $1,000,000 for fiscal 2025.",
                         content_hash=sha("span-1"), ref_type="document_span", ref_id="doc-1#p12",
                         observed_ns=SPAN_OBSERVED, available_ns=SPAN_AVAILABLE),)


def facts():
    from unknown.agents.evidence import TypedFact
    return (TypedFact("Revenues", "FY2025", "USD", Fixed(1000000)),)


class TempDirCase:
    """Mixin: self.tmp is a fresh directory removed after the test."""

    def setUp(self):  # noqa: N802
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = ManualClock(T0)

    def tearDown(self):  # noqa: N802
        self._tmp.cleanup()


def write_private(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, mode)
    return path


# ---------------------------------------------------------------------------- research store fixture
FIX_ENTITY = "SEC-CIK-0000000001"
FIX_TICKER = "FIXA"
INGESTED_AT = T0 - 86_400 * SECOND


def fixture_observation(field, start, end, value, available, accn, *, form="10-K", unit="USD"):
    from unknown.data.pit import Observation
    return Observation(dataset="sec.companyfacts", entity_id=FIX_ENTITY, field=field, unit=unit,
                       currency="USD" if unit == "USD" else None, period_start=start, period_end=end,
                       period_kind="INSTANT" if start is None else "FY", value=Fixed.parse(value),
                       receipt_time_ns=INGESTED_AT, available_time_ns=available, published_time_ns=available,
                       publication_id=accn, source_ref=f"raw:fixture#{field}/{accn}", quality=(f"form:{form}",))


def research_store(path: Path):
    """A tiny point-in-time store (all values are fixtures): two annual revenues, one quarter, one 9-month
    year-to-date value (never a span), one balance-sheet instant, and a 10-K/A restatement that becomes
    available one day after T0."""
    from unknown.data.pit import ResearchStore
    from unknown.domain.clock import utc_ns
    path.parent.mkdir(parents=True, exist_ok=True)
    s = ResearchStore(path)
    s.upsert_entity(FIX_ENTITY, "issuer", "Fixture Industries Inc.", {"cik": 1})
    s.add_alias("ticker:Nasdaq", FIX_TICKER, FIX_ENTITY, INGESTED_AT, "fixture")
    for o in (
        fixture_observation("us-gaap:Revenues", "2023-10-01", "2024-09-28", "1000000", utc_ns(2024, 11, 1, 21), "0000000001-24-000010"),
        fixture_observation("us-gaap:Revenues", "2024-09-29", "2025-09-27", "1200000", utc_ns(2025, 10, 31, 21), "0000000001-25-000010"),
        fixture_observation("us-gaap:Revenues", "2025-03-30", "2025-06-28", "290000", utc_ns(2025, 8, 1, 21), "0000000001-25-000007",
                            form="10-Q"),
        fixture_observation("us-gaap:Revenues", "2024-09-29", "2025-06-28", "880000", utc_ns(2025, 8, 1, 21), "0000000001-25-000007",
                            form="10-Q"),
        fixture_observation("us-gaap:Assets", None, "2025-09-27", "5000000", utc_ns(2025, 10, 31, 21), "0000000001-25-000010"),
        fixture_observation("us-gaap:Revenues", "2024-09-29", "2025-09-27", "1150000", T0 + 86_400 * SECOND, "0000000001-26-000001",
                            form="10-K/A"),
    ):
        s.record(o, INGESTED_AT)
    return s
