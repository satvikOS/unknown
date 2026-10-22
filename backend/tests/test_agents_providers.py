"""Provider adapters: documented request fragments, response parsing, error taxonomy, send
preconditions, retry policy and circuit breaker (CAP-MODEL-01..06, CAP-MODEL-10/11).

Fixtures are shaped from the verified record text; no network, no real keys. The Anthropic
adapter is exercised with an injected fake client (the SDK itself is not needed here)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import MODEL, SECOND, T0, TempDirCase, owner, registry, sha, write_private  # noqa: E402
from unknown.agents.budget import SpendLedger  # noqa: E402
from unknown.agents.gate import DataUseGate, load_provider_policies  # noqa: E402
from unknown.agents.providers import anthropic as anth  # noqa: E402
from unknown.agents.providers import gemini, openai, xai  # noqa: E402
from unknown.agents.providers.base import (CircuitBreaker, ErrorClass, GenerationRequest, HttpResponse,  # noqa: E402
                                           OutputStatus, ProviderError, RetryPolicy, SendRefused, UnverifiedFact,
                                           local_estimate)
from unknown.agents.config import EstimateConfig  # noqa: E402
from unknown.agents.runner import INSTRUCTIONS_V1, load_output_schema  # noqa: E402
from unknown.agents.schema import compile_for  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.money import Money  # noqa: E402


class FakeTransport:
    def __init__(self, responses=()):
        self.calls, self.responses = [], list(responses)

    def send(self, request, *, timeout_s):
        self.calls.append(request)
        return self.responses.pop(0)


def resp(status, body=None, headers=None):
    return HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, json.dumps(body or {}).encode())


def request(provider="anthropic", model=MODEL, effort="low"):
    return GenerationRequest("job-1", "research", model, INSTRUCTIONS_V1, "TASK: x", compile_for(provider, load_output_schema("research_answer", 1)),
                             300, effort)


def provider_owner(pid, region="global", **account):
    return owner(**{f"providers__{pid}": {"enabled": True, "api_key_env": "UNKNOWN_TEST_KEY", "region": region,
                                          "account": account or {"training_permission_granted": False}}})


class HttpAdapterPreconditionsTest(TempDirCase, unittest.TestCase):
    """The adapter refuses to send unless (a) enabled, (b) key present, (c) reservation, (d) gate decision."""

    def setUp(self):
        super().setUp()
        self.pol = provider_owner("openai", training_opt_in=False)
        self.ledger = SpendLedger(self.pol, self.tmp / "l.sqlite", clock_ns=self.clock.now_ns)
        self.addCleanup(self.ledger.close)
        self.gate = DataUseGate(registry(), self.pol, load_provider_policies(), self.clock.now_ns)

    def adapter(self, pol="default", env=None):
        return openai.OpenAIAdapter(owner=self.pol if pol == "default" else pol, ledger=self.ledger, transport=self.t,
                                    environ={} if env is None else env)

    def test_refusals_send_nothing(self):
        self.t = FakeTransport()
        req = request("openai", "gpt-6.1-sol")
        with self.assertRaises(SendRefused) as c:
            self.adapter(pol=None).list_models(now_ns=T0)
        self.assertEqual(c.exception.reason, "provider_disabled")
        with self.assertRaises(SendRefused) as c:
            self.adapter().list_models(now_ns=T0)
        self.assertEqual(c.exception.reason, "api_key_missing")
        a = self.adapter(env={"UNKNOWN_TEST_KEY": "sk-test"})
        dec = self.gate.evaluate(provider="openai", model_id="gpt-6.1-sol", feature="generate", dataset_ids=["fixture.public"],
                                 request_hash=req.request_hash())
        with self.assertRaises(SendRefused) as c:
            a.generate_structured(req, reservation=None, gate=dec)
        self.assertEqual(c.exception.reason, "no_open_reservation")
        res = self.ledger.reserve(job_id="job-1", role="research", provider="openai", model_id="gpt-6.1-sol",
                                  amount=Money("USD", Fixed.parse("0.01")))
        other = self.gate.evaluate(provider="openai", model_id="gpt-6.1-sol", feature="generate", dataset_ids=[], request_hash="b" * 64)
        with self.assertRaises(SendRefused) as c:
            a.generate_structured(req, reservation=res, gate=other)
        self.assertEqual(c.exception.reason, "gate_decision_mismatch")
        denied = self.gate.evaluate(provider="openai", model_id="gpt-6.1-sol", feature="generate",
                                    dataset_ids=["fixture.restricted"], request_hash=req.request_hash())
        with self.assertRaises(SendRefused) as c:
            a.generate_structured(req, reservation=res, gate=denied)
        self.assertEqual(c.exception.reason, "gate_denied")
        # all preconditions hold: the adapter still refuses to guess the unrecorded auth scheme
        with self.assertRaises(NotImplementedError) as c:
            a.generate_structured(req, reservation=res, gate=dec)
        self.assertIsInstance(c.exception, UnverifiedFact)
        self.assertEqual(c.exception.record_id, "CAP-MODEL-01")
        self.assertEqual(self.t.calls, [])

    def test_unverified_requirements_are_reported(self):
        self.t = FakeTransport()
        for a in (openai.OpenAIAdapter, xai.XAIAdapter, gemini.GeminiAdapter):
            adapter = a(owner=None, ledger=None, transport=self.t)
            missing = adapter.unverified_requirements("generate")
            self.assertIn("auth", missing)
            for key in missing:
                with self.assertRaises(UnverifiedFact) as c:
                    adapter.facts.require(key)
                self.assertTrue(c.exception.record_id.startswith("CAP-MODEL-"))


class OpenAIDocumentedShapesTest(unittest.TestCase):
    def test_model_object(self):
        row = openai.normalize_model({"id": "gpt-6.1-sol", "object": "model", "created": 1767225600, "owned_by": "system",
                                      "shutdown_date": "2027-01-15"})
        self.assertEqual(row.retires_at, "2027-01-15")
        self.assertEqual(row.lifecycle, "unknown")
        self.assertIsNone(row.max_input_tokens)
        self.assertIsNone(openai.normalize_model({"id": "m", "object": "model", "created": 1, "owned_by": "o",
                                                  "shutdown_date": None}).retires_at)

    def test_structured_fragment_and_count_response(self):
        frag = openai.structured_output_fragment(300)
        self.assertEqual(frag, {"text": {"format": {"type": "json_schema", "strict": True}}, "max_output_tokens": 300, "store": False})
        self.assertEqual(openai.parse_count_response(b'{"object": "response.input_tokens", "input_tokens": 1234}'), 1234)
        with self.assertRaises(ValueError):
            openai.parse_count_response(b'{"object": "response", "input_tokens": 1}')
        with self.assertRaises(UnverifiedFact):
            openai.FACTS.require("structured.schema_field")

    def test_error_taxonomy(self):
        cases = [
            (resp(429, {"error": {"type": "insufficient_quota", "code": "organization_spend_limit_exceeded"}}), ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (resp(429, {"error": {"type": "requests", "code": "project_spend_limit_exceeded"}}), ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (resp(429, {"error": {"type": "insufficient_quota", "code": None}}), ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (resp(429, {"error": {"message": "Rate limit reached for requests", "type": "requests"}}, {"Retry-After": "7"}), ErrorClass.TRANSIENT_RATE_LIMIT),
            (resp(429, {"error": {"code": "slow_down"}}), ErrorClass.TRANSIENT_RATE_LIMIT),
            (resp(429, {"error": {"code": "mystery"}}), ErrorClass.UNKNOWN),
            (resp(503, {"error": {"type": "service_unavailable_error", "code": "server_is_overloaded"}}), ErrorClass.OVERLOADED),
            (resp(500, {}), ErrorClass.SERVER_ERROR), (resp(401, {}), ErrorClass.AUTH), (resp(403, {}), ErrorClass.AUTH),
            (resp(400, {}), ErrorClass.INVALID_REQUEST), (resp(413, {}), ErrorClass.INVALID_REQUEST), (resp(418, {}), ErrorClass.UNKNOWN),
        ]
        for r, cls in cases:
            with self.subTest(status=r.status, body=r.body):
                self.assertEqual(openai.classify_error(r).cls, cls)
        e = openai.classify_error(cases[3][0])
        self.assertEqual(e.retry_after_s, 7)
        self.assertTrue(e.transient)
        self.assertFalse(openai.classify_error(cases[0][0]).transient)
        self.assertEqual(openai.rate_limit_observation({"x-ratelimit-remaining-requests": "9", "other": "x"}),
                         {"x-ratelimit-remaining-requests": "9"})


class XAIDocumentedShapesTest(TempDirCase, unittest.TestCase):
    def test_language_model_prices_are_stored_unchanged_and_converted_only_for_cross_check(self):
        row = xai.normalize_language_model({"id": "grok-4.7", "aliases": ["grok-4.7-stable"], "fingerprint": "fp_1",
                                            "version": "1.0", "input_modalities": ["text"], "output_modalities": ["text"],
                                            "prompt_text_token_price": 20000, "long_context_threshold": 200000,
                                            "capabilities": {}})
        self.assertEqual(row.provider_prices["prompt_text_token_price"], 20000)
        self.assertEqual(row.provider_price_unit, "usd_cents_per_100m_tokens")
        self.assertEqual(xai.usd_per_1m_from_catalog(20000), Fixed.parse("2.00"))
        self.assertEqual(row.aliases, ("grok-4.7-stable",))

    def test_tokenize_shapes_and_usage_locations(self):
        self.assertEqual(json.loads(xai.tokenize_body("grok-4.7", "hello")), {"model": "grok-4.7", "text": "hello"})
        body = {"token_ids": [{"string_token": "he", "token_bytes": [104, 101], "token_id": 1},
                              {"string_token": "llo", "token_bytes": [108, 108, 111], "token_id": 2}]}
        self.assertEqual(xai.parse_tokenize_response(json.dumps(body).encode()), 2)
        self.assertEqual(xai.cached_tokens({"prompt_tokens_details": {"cached_tokens": 40}}, "chat_completions"), 40)
        self.assertEqual(xai.cached_tokens({"input_tokens_details": {"cached_tokens": 7}}, "responses"), 7)

    def test_errors(self):
        self.assertEqual(xai.classify_error(resp(429, {"error": "Too many requests"})).cls, ErrorClass.TRANSIENT_RATE_LIMIT)
        self.assertEqual(xai.classify_error(resp(502, {})).cls, ErrorClass.OVERLOADED)
        self.assertEqual(xai.classify_error(resp(400, {})).cls, ErrorClass.INVALID_REQUEST)

    def test_us_endpoint_rejects_other_models_locally(self):
        pol = provider_owner("xai", region="us", training_permission_granted=False)
        a = xai.XAIAdapter(owner=pol, ledger=None, transport=FakeTransport(), environ={"UNKNOWN_TEST_KEY": "k"},
                           us_endpoint_models=tuple(load_provider_policies()["providers"]["xai"]["regions"]["us"]["models"]))
        with self.assertRaises(ProviderError) as c:
            a.generate_structured(request("xai", "grok-9"), reservation=None, gate=None)
        self.assertFalse(c.exception.sent)
        self.assertEqual(c.exception.cls, ErrorClass.INVALID_REQUEST)
        self.assertEqual(a.base_url(), "https://us.api.x.ai")


class GeminiDocumentedShapesTest(unittest.TestCase):
    def test_pagination_parameters(self):
        self.assertEqual(gemini.page_params(None), {"pageSize": "1000"})
        self.assertEqual(gemini.page_params("tok", 1000), {"pageSize": "1000", "pageToken": "tok"})
        with self.assertRaises(ValueError):
            gemini.page_params(None, 1001)
        self.assertEqual(gemini.next_page_token({"nextPageToken": "n2"}), "n2")
        self.assertIsNone(gemini.next_page_token({"nextPageToken": ""}))
        self.assertEqual(gemini.model_limits({"inputTokenLimit": 1048576, "outputTokenLimit": 65536,
                                              "supportedGenerationMethods": ["generateContent"], "thinking": True}),
                         {"inputTokenLimit": 1048576, "outputTokenLimit": 65536,
                          "supportedGenerationMethods": ["generateContent"], "thinking": True})
        with self.assertRaises(UnverifiedFact):
            gemini.normalize_model({"inputTokenLimit": 1})
        self.assertEqual(gemini.structured_output_fragment(), {"response_format": {"mime_type": "application/json"}, "store": False})

    def test_errors_and_pacific_midnight(self):
        from unknown.domain.clock import utc_ns
        now = utc_ns(2026, 10, 23, 14, 0, 0)  # 07:00 Pacific (PDT)
        q = gemini.classify_error(resp(429, {"error": {"status": "quota_exceeded", "message": "You have exceeded your daily quota"}}), now_ns=now)
        self.assertEqual(q.cls, ErrorClass.QUOTA_OR_SPEND_EXHAUSTED)
        self.assertEqual(q.retry_not_before_ns, utc_ns(2026, 10, 24, 7, 0, 0))
        cases = [({"error": {"status": "rate_limit_exceeded"}}, 429, ErrorClass.TRANSIENT_RATE_LIMIT),
                 ({"error": {"status": "RESOURCE_EXHAUSTED"}}, 429, ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
                 ({}, 402, ErrorClass.QUOTA_OR_SPEND_EXHAUSTED), ({}, 503, ErrorClass.OVERLOADED),
                 ({}, 500, ErrorClass.SERVER_ERROR), ({}, 504, ErrorClass.TIMEOUT), ({}, 429, ErrorClass.UNKNOWN)]
        for body, status, cls in cases:
            with self.subTest(status=status, body=body):
                self.assertEqual(gemini.classify_error(resp(status, body), now_ns=now).cls, cls)


# ============================================================================ Anthropic (SDK adapter, fake client)
class FakeStatusError(Exception):
    def __init__(self, status, body=None, headers=None, message="", request_id="req_err"):
        super().__init__(message)
        self.status_code, self.body, self.message, self.request_id = status, body, message, request_id
        self.response = SimpleNamespace(headers=headers or {})


class APITimeoutError(Exception):
    pass


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes, self.calls, self.count_calls = list(outcomes), [], []

    def create(self, **kw):
        self.calls.append(kw)
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def count_tokens(self, **kw):
        self.count_calls.append(kw)
        return SimpleNamespace(input_tokens=321)


class FakeClient:
    def __init__(self, outcomes=(), beta_outcomes=(), models=()):
        self.messages = FakeMessages(outcomes)
        self.beta = SimpleNamespace(messages=FakeMessages(beta_outcomes))
        self.models = SimpleNamespace(list=lambda: iter(models))


def message(text='{"ok": true}', stop="end_turn", model=MODEL, usage=None, content=None, details=None):
    usage = usage or SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0, cache_creation=None, inference_geo="global", iterations=None)
    m = SimpleNamespace(stop_reason=stop, stop_details=details, model=model, usage=usage,
                        content=content if content is not None else [SimpleNamespace(type="thinking", thinking=""),
                                                                     SimpleNamespace(type="text", text=text)])
    m._request_id = "req_018fixture"
    m.to_json = lambda: json.dumps({"stop_reason": stop, "model": model})
    return m


class AnthropicAdapterTest(TempDirCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.pol = owner(providers__anthropic__api_key_file="anthropic_api_key")
        self.ledger = SpendLedger(self.pol, self.tmp / "l.sqlite", clock_ns=self.clock.now_ns)
        self.addCleanup(self.ledger.close)
        self.gate = DataUseGate(registry(), self.pol, load_provider_policies(), self.clock.now_ns)
        self.policy = load_provider_policies()["providers"]["anthropic"]
        self.keys = []

    def adapter(self, client, pol=None, env=None):
        def factory(key):
            self.keys.append(key)
            return client
        return anth.AnthropicAdapter(owner=pol or self.pol, ledger=self.ledger, provider_policy=self.policy,
                                     client_factory=factory, environ={"UNKNOWN_TEST_ANTHROPIC_KEY": "sk-ant-test"} if env is None else env,
                                     secrets_dir=self.tmp)

    def admitted(self, req, feature="generate"):
        res = self.ledger.reserve(job_id="job-1", role="research", provider="anthropic", model_id=req.model_id,
                                  amount=Money("USD", Fixed.parse("0.05")))
        dec = self.gate.evaluate(provider="anthropic", model_id=req.model_id, feature=feature, dataset_ids=["fixture.public"],
                                 request_hash=req.request_hash())
        return res, dec

    def test_request_shape(self):
        client = FakeClient([message()])
        a = self.adapter(client)
        req = request()
        res, dec = self.admitted(req)
        out = a.generate_structured(req, reservation=res, gate=dec)
        kw = client.messages.calls[0]
        self.assertEqual(set(kw), {"model", "max_tokens", "system", "messages", "output_config", "inference_geo"})
        self.assertEqual(kw["model"], MODEL)
        self.assertEqual(kw["messages"], [{"role": "user", "content": "TASK: x"}])
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("maxLength", json.dumps(kw["output_config"]["format"]["schema"]))
        self.assertEqual(kw["inference_geo"], "global")
        for forbidden in ("thinking", "temperature", "top_p", "top_k", "tool_choice", "output_format"):
            self.assertNotIn(forbidden, kw)
        self.assertEqual(out.status, OutputStatus.COMPLETE)
        self.assertEqual(out.text, '{"ok": true}')
        self.assertEqual(out.request_id, "req_018fixture")
        self.assertEqual(out.usages[0].prompt_tokens, 1000)
        self.assertEqual(self.keys, ["sk-ant-test"])

    def test_effort_must_be_explicit(self):
        a = self.adapter(FakeClient([message()]))
        req = request(effort=None)
        res, dec = self.admitted(req)
        with self.assertRaises(SendRefused) as c:
            a.generate_structured(req, reservation=res, gate=dec)
        self.assertEqual(c.exception.reason, "effort_not_configured_for_role")

    def test_preconditions(self):
        client = FakeClient([message()])
        req = request()
        res, dec = self.admitted(req)
        with self.assertRaises(SendRefused) as c:
            self.adapter(client, env={}).generate_structured(req, reservation=res, gate=dec)
        self.assertEqual(c.exception.reason, "api_key_missing")
        write_private(self.tmp / "anthropic_api_key", "sk-file\n", 0o644)
        self.assertTrue(self.adapter(client, env={}).admission_problem().startswith("api_key_unsafe"))
        write_private(self.tmp / "anthropic_api_key", "sk-file\n", 0o600)
        a = self.adapter(client, env={})
        self.assertIsNone(a.admission_problem())
        with self.assertRaises(SendRefused):
            a.generate_structured(req, reservation=None, gate=dec)
        disabled = owner(providers__anthropic__enabled=False)
        with self.assertRaises(SendRefused):
            self.adapter(client, pol=disabled).generate_structured(req, reservation=res, gate=dec)
        self.assertEqual(client.messages.calls, [])
        a.generate_structured(req, reservation=res, gate=dec)
        self.assertEqual(self.keys[-1], "sk-file")

    def test_refusal_truncation_and_fallback_beta(self):
        details = SimpleNamespace(type="refusal", category="cyber", explanation="x")
        client = FakeClient([message(stop="refusal", content=[], details=details), message(stop="max_tokens")])
        a = self.adapter(client)
        req = request()
        res, dec = self.admitted(req)
        r = a.generate_structured(req, reservation=res, gate=dec)
        self.assertEqual((r.status, r.refusal_category, r.text), (OutputStatus.REFUSED, "cyber", None))
        r = a.generate_structured(req, reservation=res, gate=dec)
        self.assertEqual(r.status, OutputStatus.TRUNCATED)
        # opt-in server-side fallback: beta endpoint, iterations price every attempt at its own model
        usage = SimpleNamespace(input_tokens=1000, output_tokens=150, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                                cache_creation=None, inference_geo="global", iterations=[
                                    SimpleNamespace(type="message", model=MODEL, input_tokens=1000, output_tokens=20,
                                                    cache_creation_input_tokens=0, cache_read_input_tokens=0, cache_creation=None),
                                    SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=1000,
                                                    output_tokens=150, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                                                    cache_creation=None)])
        content = [SimpleNamespace(type="text", text='{"partial'), SimpleNamespace(type="fallback"),
                   SimpleNamespace(type="text", text='{"ok": 1}')]
        fb_client = FakeClient(beta_outcomes=[message(model="claude-opus-4-8", usage=usage, content=content)])
        fb = self.adapter(fb_client, pol=owner(providers__anthropic__refusal_fallback="default"))
        self.assertEqual(fb.additional_billed_models(req), ("claude-opus-4-8", "claude-opus-5"))
        r = fb.generate_structured(req, reservation=res, gate=dec)
        kw = fb_client.beta.messages.calls[0]
        self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kw["fallbacks"], "default")
        self.assertTrue(r.served_by_fallback)
        self.assertEqual(r.text, '{"ok": 1}')
        self.assertEqual([u.model_id for u in r.usages], [MODEL, "claude-opus-4-8"])
        self.assertTrue(r.usage_complete)

    def test_usage_normalization(self):
        u = SimpleNamespace(input_tokens=100, output_tokens=5, cache_creation_input_tokens=30, cache_read_input_tokens=70,
                            cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=10, ephemeral_1h_input_tokens=20))
        self.assertEqual(anth.total_input_tokens(u), 200)
        usage = anth._usage_from(u, MODEL, "global")
        self.assertEqual(usage.prompt_tokens, 200)
        self.assertEqual(dict(usage.cache_write_tokens), {"5m": 10, "1h": 20})
        u.cache_creation = None
        self.assertEqual(dict(anth._usage_from(u, MODEL, "global").cache_write_tokens), {"unknown": 30})
        usages, complete, _ = anth.normalize_usage(SimpleNamespace(usage=None), "global")
        self.assertEqual((usages, complete), ((), False))

    def test_error_mapping(self):
        cases = [
            (FakeStatusError(429, {"type": "error", "error": {"type": "rate_limit_error", "details": {"error_code": "enforced_spend_limit_reached"}}}),
             ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (FakeStatusError(429, {"type": "error", "error": {"type": "rate_limit_error"}}), ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (FakeStatusError(429, {"error": {"type": "rate_limit_error"}}, {"retry-after": "7"}), ErrorClass.TRANSIENT_RATE_LIMIT),
            (FakeStatusError(400, {"error": {"type": "invalid_request_error", "message": "You have reached your specified API usage limits. You will regain access on 2026-11-01"}}),
             ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (FakeStatusError(400, {"error": {"type": "invalid_request_error", "message": "You have reached your specified workspace API usage limits."}}),
             ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (FakeStatusError(400, {"error": {"type": "invalid_request_error", "message": "bad schema"}}), ErrorClass.INVALID_REQUEST),
            (FakeStatusError(402, {"error": {"type": "billing_error"}}), ErrorClass.QUOTA_OR_SPEND_EXHAUSTED),
            (FakeStatusError(401), ErrorClass.AUTH), (FakeStatusError(403), ErrorClass.AUTH), (FakeStatusError(404), ErrorClass.INVALID_REQUEST),
            (FakeStatusError(413), ErrorClass.INVALID_REQUEST), (FakeStatusError(529), ErrorClass.OVERLOADED),
            (FakeStatusError(503), ErrorClass.OVERLOADED), (FakeStatusError(500), ErrorClass.SERVER_ERROR),
            (FakeStatusError(504), ErrorClass.TIMEOUT), (APITimeoutError("t"), ErrorClass.TIMEOUT),
        ]
        for exc, cls in cases:
            with self.subTest(exc=type(exc).__name__, status=getattr(exc, "status_code", None), body=getattr(exc, "body", None)):
                err = anth.map_exception(exc, MODEL)
                self.assertEqual(err.cls, cls)
        e = anth.map_exception(cases[2][0])
        self.assertEqual((e.retry_after_s, e.request_id), (7, "req_err"))
        self.assertIsNone(anth.map_exception(ValueError("not an SDK error")))
        client = FakeClient([cases[0][0]])
        a = self.adapter(client)
        req = request()
        res, dec = self.admitted(req)
        with self.assertRaises(ProviderError) as c:
            a.generate_structured(req, reservation=res, gate=dec)
        self.assertEqual(c.exception.cls, ErrorClass.QUOTA_OR_SPEND_EXHAUSTED)

    def test_count_tokens_and_list_models(self):
        caps = SimpleNamespace(**{n: SimpleNamespace(supported=True) for n in anth.CAPABILITY_FIELDS})
        info = SimpleNamespace(id=MODEL, display_name="Claude Opus 5.5", created_at=None, lifecycle="active", deprecated_at=None,
                               retires_at=None, max_input_tokens=1_000_000, max_tokens=128_000, capabilities=caps)
        client = FakeClient(models=[info])
        a = self.adapter(client)
        snap = a.list_models(now_ns=T0)
        row = snap.rows[MODEL]
        self.assertEqual((row.lifecycle, row.max_input_tokens, row.max_output_tokens), ("active", 1_000_000, 128_000))
        self.assertEqual(row.capabilities["structured_outputs"].provenance, "api")
        req = request()
        res, dec = self.admitted(req, feature="count_tokens")
        tc = a.count_tokens(req, admission=res, gate=dec)
        self.assertEqual((tc.input_tokens, tc.method), (321, "provider_endpoint"))
        kw = client.messages.count_calls[0]
        self.assertEqual(kw["model"], MODEL)
        self.assertNotIn("tool_choice", kw)


@unittest.skipUnless(__import__("importlib").util.find_spec("anthropic"), "anthropic SDK not installed in this interpreter")
class AnthropicSDKConformanceTest(unittest.TestCase):
    """Run with the research venv: our parameters exist in the pinned SDK and real SDK
    exceptions map to the taxonomy."""

    def test_parameters_exist_in_sdk_signatures(self):
        import inspect
        from anthropic.resources.beta.messages import Messages as BetaMessages
        from anthropic.resources.messages import Messages
        params = set(inspect.signature(Messages.create).parameters)
        self.assertLessEqual({"model", "max_tokens", "system", "messages", "output_config", "inference_geo"}, params)
        self.assertLessEqual({"betas", "fallbacks"}, set(inspect.signature(BetaMessages.create).parameters))
        self.assertLessEqual({"model", "system", "messages", "output_config"}, set(inspect.signature(Messages.count_tokens).parameters))

    def test_real_exceptions_map(self):
        import anthropic
        import httpx2
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        def err(cls, status, body, headers=None):
            return cls("x", response=httpx2.Response(status, headers=headers or {}, request=req), body=body)
        self.assertEqual(anth.map_exception(err(anthropic.RateLimitError, 429, {"error": {"type": "rate_limit_error"}},
                                                {"retry-after": "3"})).cls, ErrorClass.TRANSIENT_RATE_LIMIT)
        self.assertEqual(anth.map_exception(err(anthropic.RateLimitError, 429, {"error": {"type": "rate_limit_error",
                                                "details": {"error_code": "enforced_spend_limit_reached"}}})).cls,
                         ErrorClass.QUOTA_OR_SPEND_EXHAUSTED)
        self.assertEqual(anth.map_exception(err(anthropic.OverloadedError, 529, {})).cls, ErrorClass.OVERLOADED)
        self.assertEqual(anth.map_exception(err(anthropic.AuthenticationError, 401, {})).cls, ErrorClass.AUTH)
        self.assertEqual(anth.map_exception(anthropic.APITimeoutError(request=req)).cls, ErrorClass.TIMEOUT)
        self.assertEqual(anth.map_exception(anthropic.APIConnectionError(request=req)).cls, ErrorClass.NETWORK)


class RetryAndBreakerTest(unittest.TestCase):
    def test_retry_policy(self):
        p = RetryPolicy(max_attempts=2, base_ms=1000, max_ms=30_000, jitter_ms=0)
        transient = ProviderError("x", ErrorClass.TRANSIENT_RATE_LIMIT, "rl", retry_after_s=7)
        self.assertEqual(p.delay_ns(0, transient, now_ns=0, deadline_ns=60 * SECOND), 7 * SECOND)  # never before retry-after
        self.assertEqual(p.delay_ns(1, ProviderError("x", ErrorClass.OVERLOADED, "o"), now_ns=0, deadline_ns=60 * SECOND), 2 * SECOND)
        self.assertIsNone(p.delay_ns(2, transient, now_ns=0, deadline_ns=60 * SECOND))           # attempts exhausted
        self.assertIsNone(p.delay_ns(0, transient, now_ns=0, deadline_ns=5 * SECOND))            # past the deadline
        for cls in (ErrorClass.QUOTA_OR_SPEND_EXHAUSTED, ErrorClass.AUTH, ErrorClass.INVALID_REQUEST, ErrorClass.UNKNOWN):
            self.assertIsNone(p.delay_ns(0, ProviderError("x", cls, "e"), now_ns=0, deadline_ns=10**18))
        nb = ProviderError("x", ErrorClass.TRANSIENT_RATE_LIMIT, "q", retry_not_before_ns=30 * SECOND)
        self.assertEqual(p.delay_ns(0, nb, now_ns=0, deadline_ns=60 * SECOND), 30 * SECOND)

    def test_no_retry_after_deadline_property(self):
        import random
        rng = random.Random(3)
        p = RetryPolicy(max_attempts=10, base_ms=100, max_ms=20_000, jitter_ms=500)
        for _ in range(2000):
            now, deadline = rng.randint(0, 10**12), rng.randint(0, 10**12)
            err = ProviderError("x", ErrorClass.OVERLOADED, "o", retry_after_s=rng.choice([None, 1, 30]))
            d = p.delay_ns(rng.randint(0, 9), err, now_ns=now, deadline_ns=deadline, rand_ms=lambda hi: rng.randint(0, hi))
            if d is not None:
                self.assertLess(now + d, deadline)

    def test_breaker(self):
        b = CircuitBreaker(failures=2, window_ns=60 * SECOND, cooldown_ns=300 * SECOND)
        b.record_failure("k", ErrorClass.OVERLOADED, 0)
        self.assertTrue(b.allow("k", SECOND))
        b.record_failure("k", ErrorClass.SERVER_ERROR, SECOND)
        self.assertFalse(b.allow("k", 2 * SECOND))
        self.assertEqual(b.state("k", 2 * SECOND), "open")
        self.assertTrue(b.allow("k", 302 * SECOND))     # half-open: one probe
        self.assertFalse(b.allow("k", 303 * SECOND))
        b.record_failure("k", ErrorClass.OVERLOADED, 303 * SECOND)
        self.assertFalse(b.allow("k", 304 * SECOND))
        self.assertTrue(b.allow("k", 700 * SECOND))
        b.record_success("k")
        self.assertEqual(b.state("k", 701 * SECOND), "closed")
        b.record_failure("k", ErrorClass.TRANSIENT_RATE_LIMIT, 702 * SECOND)  # rate limits do not trip the breaker
        b.record_failure("k", ErrorClass.TRANSIENT_RATE_LIMIT, 703 * SECOND)
        self.assertTrue(b.allow("k", 704 * SECOND))

    def test_local_estimate_is_conservative(self):
        req = request()
        tc = local_estimate(req, EstimateConfig(Fixed.parse("1.5"), 100))
        self.assertEqual(tc.method, "local_estimate")
        self.assertEqual(tc.input_tokens, -(-req.payload_bytes() * 3 // 2) + 100)


if __name__ == "__main__":
    unittest.main()
