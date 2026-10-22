"""xAI adapter (urllib), restricted to facts in the verified register.

Verified (implemented and tested):
  * global base https://api.x.ai and US base https://us.api.x.ai (paths carry /v1); the US
    endpoint serves only the models listed in config/models/provider_policies.json  CAP-MODEL-10
  * GET /v1/models, /v1/language-models, /v1/language-models/{model_id}; language-model
    fields id, aliases, fingerprint, version, input_modalities, output_modalities,
    prompt_text_token_price, long_context_threshold, capabilities; prices in USD cents
    per 100 million tokens, stored unchanged and never used for billing         CAP-MODEL-01
  * POST /v1/tokenize-text {model, text, user} -> token_ids[{string_token, token_bytes,
    token_id}] (plain text only; request overhead is added from owner calibration)  CAP-MODEL-04
  * cached-token usage locations usage.prompt_tokens_details.cached_tokens (Chat
    Completions) and usage.input_tokens_details.cached_tokens (Responses)        CAP-MODEL-08
    (the cache-routing header is recorded too; it is not used because prompt caching is
    not implemented in this increment, see docs/agents/README.md)
  * errors: 429 is the only documented code; any 5xx is treated as overloaded   CAP-MODEL-06

Not recorded (``UnverifiedFact``): the auth header, the list response field holding
models, the generation endpoint path, the inner shape of response_format, where the
generated text lives, the prompt/output usage field names, the shape of 'capabilities'.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from ...domain.fixed import Fixed
from ..budget import Reservation
from ..catalog import CatalogSnapshot, ModelRow
from ..gate import GateDecision
from .base import (ErrorClass, GenerationRequest, GenerationResult, HttpProviderAdapter, HttpRequest, HttpResponse,
                   ProtocolFacts, ProviderError, TokenCount, Unverified, Verified, body_strings, parse_retry_after)

PROVIDER_ID = "xai"
PRICE_UNIT = "usd_cents_per_100m_tokens"
LANGUAGE_MODEL_FIELDS = ("id", "aliases", "fingerprint", "version", "input_modalities", "output_modalities",
                         "prompt_text_token_price", "long_context_threshold", "capabilities")

FACTS = ProtocolFacts(PROVIDER_ID, {
    "base_url.global": Verified("https://api.x.ai", "CAP-MODEL-10", "global endpoint; paths carry /v1"),
    "base_url.us": Verified("https://us.api.x.ai", "CAP-MODEL-10", "recorded as https://us.api.x.ai/v1"),
    "auth": Unverified("CAP-MODEL-01", "the request authentication header and scheme are not recorded"),
    "list_models.endpoints": Verified({"models": "/v1/models", "language_models": "/v1/language-models",
                                       "language_model": "/v1/language-models/{model_id}"}, "CAP-MODEL-01"),
    "list_models.items_field": Unverified("CAP-MODEL-01", "the list response field that holds the model objects is not recorded"),
    "language_model.fields": Verified(LANGUAGE_MODEL_FIELDS, "CAP-MODEL-01", "close paraphrase in the record"),
    "language_model.capabilities_shape": Unverified("CAP-MODEL-01", "the structure of the capabilities field is not recorded"),
    "tokenize.endpoint": Verified(("POST", "/v1/tokenize-text"), "CAP-MODEL-04"),
    "tokenize.request_fields": Verified(("model", "text", "user"), "CAP-MODEL-04"),
    "tokenize.response": Verified({"list": "token_ids", "item_fields": ("string_token", "token_bytes", "token_id")}, "CAP-MODEL-04"),
    "generate.endpoint": Unverified("CAP-MODEL-02, CAP-MODEL-03", "the generation path (Responses or Chat Completions) is not recorded"),
    "structured.response_format": Unverified("CAP-MODEL-02", "response_format is named; its inner shape (and whether strict applies) is not recorded"),
    "response.output_text": Unverified("CAP-MODEL-02", "where the generated text lives is not recorded"),
    "usage.cached_tokens": Verified({"chat_completions": ("prompt_tokens_details", "cached_tokens"),
                                     "responses": ("input_tokens_details", "cached_tokens")}, "CAP-MODEL-08"),
    "usage.fields": Unverified("CAP-MODEL-08", "prompt/output token field names and whether cached tokens are a subset are not recorded"),
})


def usd_per_1m_from_catalog(field_value: int | str) -> Fixed:
    """CAP-MODEL-01 unit conversion (cross-check only, never billing): USD/1M = cents per 100M / 10,000."""
    v = Fixed.of(field_value) if not isinstance(field_value, Fixed) else field_value
    return Fixed(v.mantissa, v.scale + 4)


def normalize_language_model(obj: Mapping[str, Any]) -> ModelRow:
    if not isinstance(obj, Mapping) or not isinstance(obj.get("id"), str):
        raise ValueError("language model object must carry a string id")
    prices = {k: v for k, v in obj.items() if k.endswith("_price")}
    def _tuple(name: str) -> tuple:
        v = obj.get(name)
        return tuple(v) if isinstance(v, list) else ()
    return ModelRow(provider=PROVIDER_ID, model_id=obj["id"], aliases=_tuple("aliases"),
                    fingerprint=obj.get("fingerprint"), version=obj.get("version"),
                    input_modalities=_tuple("input_modalities"), output_modalities=_tuple("output_modalities"),
                    lifecycle="unknown", provider_prices=prices, provider_price_unit=PRICE_UNIT if prices else None)


def tokenize_body(model_id: str, text: str) -> bytes:
    fields = FACTS.require("tokenize.request_fields")
    return json.dumps({fields[0]: model_id, fields[1]: text}).encode()


def parse_tokenize_response(body: bytes) -> int:
    spec = FACTS.require("tokenize.response")
    doc = json.loads(body)
    ids = doc.get(spec["list"]) if isinstance(doc, dict) else None
    if not isinstance(ids, list):
        raise ValueError("tokenize response lacks token_ids")
    return len(ids)


def cached_tokens(usage: Mapping[str, Any], api: str) -> int | None:
    parent, child = FACTS.require("usage.cached_tokens")[api]
    v = usage.get(parent, {}).get(child) if isinstance(usage.get(parent), Mapping) else None
    return v if type(v) is int else None


def classify_error(resp: HttpResponse, *, model_id: str | None = None) -> ProviderError:
    try:
        body = json.loads(resp.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    s = resp.status
    if s == 429:
        cls = ErrorClass.TRANSIENT_RATE_LIMIT
    elif s >= 500:
        cls = ErrorClass.OVERLOADED   # xAI documents no 5xx codes; treat as overloaded (CAP-MODEL-06)
    elif s in (401, 403):
        cls = ErrorClass.AUTH
    elif s in (400, 404, 405, 409, 413, 415, 422):
        cls = ErrorClass.INVALID_REQUEST
    else:
        cls = ErrorClass.UNKNOWN
    return ProviderError(PROVIDER_ID, cls, "; ".join(body_strings(body)[:3]) or f"HTTP {s}", model_id=model_id,
                         http_status=s, retry_after_s=parse_retry_after(resp.headers))


class XAIAdapter(HttpProviderAdapter):
    provider_id = PROVIDER_ID
    facts = FACTS
    list_requires = ("auth", "list_models.items_field")
    count_requires = ("auth",)
    generate_requires = ("auth", "generate.endpoint", "structured.response_format", "response.output_text", "usage.fields")

    def __init__(self, *args, us_endpoint_models: tuple[str, ...] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self.us_endpoint_models = us_endpoint_models

    def check_region_model(self, model_id: str) -> None:
        """CAP-MODEL-10: a US-endpoint request for a model the US endpoint does not serve is refused locally."""
        cfg = self._config()
        if cfg is not None and cfg.region == "us" and model_id not in self.us_endpoint_models:
            raise ProviderError(PROVIDER_ID, ErrorClass.INVALID_REQUEST, f"{model_id} is not served by the US endpoint",
                                model_id=model_id, sent=False)

    def _headers(self, key: str) -> dict:
        self.facts.require("auth")
        raise AssertionError("unreachable")  # pragma: no cover

    def list_models(self, *, now_ns: int) -> CatalogSnapshot:
        key = self._admit(reservation=None, gate=None, request_hash=None, model_id=None, feature=None)
        headers = self._headers(key)
        path = self.facts.require("list_models.endpoints")["language_models"]  # pragma: no cover
        resp = self._send(HttpRequest("GET", self.base_url() + path, headers))  # pragma: no cover
        if resp.status != 200:  # pragma: no cover
            return CatalogSnapshot.failure(PROVIDER_ID, now_ns, path, resp.status, classify_error(resp).message)
        items = json.loads(resp.body)[self.facts.require("list_models.items_field")]  # pragma: no cover
        return CatalogSnapshot.success(PROVIDER_ID, now_ns, path, resp.body, (normalize_language_model(o) for o in items))  # pragma: no cover

    def count_tokens(self, request: GenerationRequest, *, admission: Reservation, gate: GateDecision) -> TokenCount:
        self.check_region_model(request.model_id)
        key = self._admit(reservation=admission, gate=gate, request_hash=request.request_hash(),
                          model_id=request.model_id, feature="count_tokens")
        headers = self._headers(key)
        method, path = self.facts.require("tokenize.endpoint")  # pragma: no cover
        total = 0  # pragma: no cover
        for text in (request.instructions, request.user_content):  # pragma: no cover
            resp = self._send(HttpRequest(method, self.base_url() + path, headers, tokenize_body(request.model_id, text)))
            if resp.status != 200:
                raise classify_error(resp, model_id=request.model_id)
            total += parse_tokenize_response(resp.body)
        return TokenCount(request.model_id, total, "provider_text_tokenizer")  # pragma: no cover

    def generate_structured(self, request: GenerationRequest, *, reservation: Reservation, gate: GateDecision) -> GenerationResult:
        self.check_region_model(request.model_id)
        key = self._admit(reservation=reservation, gate=gate, request_hash=request.request_hash(),
                          model_id=request.model_id, feature="generate")
        self._headers(key)
        raise AssertionError("unreachable")  # pragma: no cover
