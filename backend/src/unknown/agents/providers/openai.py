"""OpenAI adapter (urllib), restricted to facts in the verified register.

Verified (implemented and tested):
  * base URL https://api.openai.com/v1 and GET /models; model object fields id, object,
    created, owned_by, shutdown_date (-> retires_at)                          CAP-MODEL-01
  * POST /responses/input_tokens takes the Responses payload and returns
    {object: "response.input_tokens", input_tokens}                           CAP-MODEL-04
  * generation endpoint /v1/responses                                          CAP-MODEL-04, CAP-MODEL-09
  * structured output text.format {type: json_schema, strict}; max_output_tokens;
    store=false sent on every request                                          CAP-MODEL-02/04/09
  * error taxonomy by status, error.type and error.code; x-ratelimit-* and
    Retry-After headers                                                        CAP-MODEL-06

Not recorded, so building a full request or reading a full response raises
``UnverifiedFact`` (NotImplementedError) with the record id: the auth header, the list
response field holding model objects, the request fields carrying the model id and the
input, the field holding the schema inside text.format (and any required name), where
the generated text, refusal and truncation signal live in a response, the usage field
names and whether cached tokens are a subset of input tokens, regional path prefixes.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from ..catalog import CatalogSnapshot, ModelRow
from ..gate import GateDecision
from ..budget import Reservation
from .base import (ErrorClass, GenerationRequest, GenerationResult, HttpProviderAdapter, HttpRequest, HttpResponse,
                   ProtocolFacts, ProviderError, TokenCount, Unverified, Verified, body_strings, parse_retry_after)

PROVIDER_ID = "openai"
QUOTA_CODES = frozenset({"credit_balance_exhausted", "organization_spend_limit_exceeded",
                         "project_spend_limit_exceeded", "organization_usage_limit_exceeded"})
MODEL_OBJECT_FIELDS = ("id", "object", "created", "owned_by", "shutdown_date")

FACTS = ProtocolFacts(PROVIDER_ID, {
    "base_url.global": Verified("https://api.openai.com/v1", "CAP-MODEL-01", "list endpoint GET https://api.openai.com/v1/models"),
    "base_url.us": Unverified("CAP-MODEL-10", "the regional host us.api.openai.com is recorded, its path prefix is not"),
    "base_url.eu": Unverified("CAP-MODEL-10", "the regional host eu.api.openai.com is recorded, its path prefix is not (EU also needs ZDR/MAM)"),
    "auth": Unverified("CAP-MODEL-01", "the request authentication header and scheme are not recorded"),
    "list_models.endpoint": Verified(("GET", "/models"), "CAP-MODEL-01"),
    "list_models.items_field": Unverified("CAP-MODEL-01", "the list response field that holds the model objects is not recorded"),
    "model_object.fields": Verified(MODEL_OBJECT_FIELDS, "CAP-MODEL-01"),
    "count_tokens.endpoint": Verified(("POST", "/responses/input_tokens"), "CAP-MODEL-04"),
    "count_tokens.response": Verified({"object": "response.input_tokens", "field": "input_tokens"}, "CAP-MODEL-04"),
    "generate.endpoint": Verified(("POST", "/responses"), "CAP-MODEL-04, CAP-MODEL-09"),
    "request.model_field": Unverified("CAP-MODEL-02", "the request field naming the model is not recorded"),
    "request.input_shape": Unverified("CAP-MODEL-04", "the Responses instructions/input field shapes are not recorded"),
    "structured.text_format": Verified({"type": "json_schema", "strict": True}, "CAP-MODEL-02"),
    "structured.schema_field": Unverified("CAP-MODEL-02", "text.format carries type json_schema and strict; the field holding the schema (and any required name) is not recorded"),
    "request.max_output_tokens": Verified("max_output_tokens", "CAP-MODEL-04"),
    "request.store": Verified(False, "CAP-MODEL-09"),
    "response.output_text": Unverified("CAP-MODEL-02", "where the Responses result carries the generated JSON text is not recorded"),
    "response.refusal": Unverified("CAP-MODEL-02", "the refusal field is checked before parsing, but its location is not recorded"),
    "response.truncation": Unverified("CAP-MODEL-02", "the truncation signal of a Responses result is not recorded"),
    "response.model": Unverified("CAP-MODEL-11", "the response field reporting the serving model id is not recorded"),
    "usage.fields": Unverified("CAP-MODEL-04, CAP-MODEL-08", "only cached_tokens and input_tokens_details.cache_write_tokens_details are recorded; the input/output token field names and whether cached and write tokens are subsets of input tokens are not"),
    "errors.body": Verified(("error.type", "error.code"), "CAP-MODEL-06"),
    "ratelimit.headers": Verified("x-ratelimit-*", "CAP-MODEL-06"),
    "tools": Unverified("CAP-MODEL-03", "function_call output items and argument parsing are not recorded"),
})


def normalize_model(obj: Mapping[str, Any]) -> ModelRow:
    """Normalize one documented model object. OpenAI exposes no limits, capabilities, prices or
    lifecycle; those stay null/unknown and must come from a dated docs snapshot or probes."""
    if not isinstance(obj, Mapping) or not isinstance(obj.get("id"), str):
        raise ValueError("model object must carry a string id")
    shutdown = obj.get("shutdown_date")
    if shutdown is not None and not isinstance(shutdown, str):
        raise ValueError("shutdown_date must be a string or null")
    created = obj.get("created")
    return ModelRow(provider=PROVIDER_ID, model_id=obj["id"], created=None if created is None else str(created),
                    lifecycle="unknown", retires_at=shutdown)


def structured_output_fragment(max_output_tokens: int) -> dict:
    """The verified parameters of a structured-output request (the schema field is not recorded)."""
    fmt = FACTS.require("structured.text_format")
    return {"text": {"format": dict(fmt)}, FACTS.require("request.max_output_tokens"): max_output_tokens,
            "store": FACTS.require("request.store")}


def parse_count_response(body: bytes) -> int:
    spec = FACTS.require("count_tokens.response")
    doc = json.loads(body)
    if not isinstance(doc, dict) or doc.get("object") != spec["object"] or type(doc.get(spec["field"])) is not int:
        raise ValueError("unexpected input_tokens response shape")
    return doc[spec["field"]]


def rate_limit_observation(headers: Mapping[str, str]) -> dict:
    return {k: v for k, v in headers.items() if k.startswith("x-ratelimit-")}


def classify_error(resp: HttpResponse, *, model_id: str | None = None) -> ProviderError:
    try:
        body = json.loads(resp.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    etype = err.get("type") if isinstance(err, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    strings = body_strings(body)
    retry_after = parse_retry_after(resp.headers)
    s = resp.status
    if s == 429:
        if code in QUOTA_CODES or etype == "insufficient_quota":
            cls = ErrorClass.QUOTA_OR_SPEND_EXHAUSTED
        elif code == "slow_down" or any(x.startswith("Rate limit reached for requests") for x in strings):
            cls = ErrorClass.TRANSIENT_RATE_LIMIT
        else:
            cls = ErrorClass.UNKNOWN
    elif s == 503:
        cls = ErrorClass.OVERLOADED if (etype == "service_unavailable_error" or code == "server_is_overloaded") else ErrorClass.UNKNOWN
    elif s == 500:
        cls = ErrorClass.SERVER_ERROR
    elif s in (401, 403):
        cls = ErrorClass.AUTH
    elif s in (400, 404, 405, 409, 413, 415, 422):
        cls = ErrorClass.INVALID_REQUEST
    else:
        cls = ErrorClass.UNKNOWN
    return ProviderError(PROVIDER_ID, cls, "; ".join(strings[:3]) or f"HTTP {s}", model_id=model_id, http_status=s,
                         provider_type=etype, provider_code=code, retry_after_s=retry_after)


class OpenAIAdapter(HttpProviderAdapter):
    provider_id = PROVIDER_ID
    facts = FACTS
    list_requires = ("auth", "list_models.items_field")
    count_requires = ("auth", "request.model_field", "request.input_shape", "structured.schema_field")
    generate_requires = ("auth", "request.model_field", "request.input_shape", "structured.schema_field",
                         "response.output_text", "response.refusal", "response.truncation", "usage.fields")

    def _headers(self, key: str) -> dict:
        self.facts.require("auth")  # raises: the scheme is not recorded
        raise AssertionError("unreachable")  # pragma: no cover

    def _body(self, request: GenerationRequest) -> bytes:
        body = structured_output_fragment(request.max_output_tokens)
        self.facts.require("request.model_field")
        self.facts.require("request.input_shape")
        self.facts.require("structured.schema_field")
        return json.dumps(body).encode()  # pragma: no cover

    def list_models(self, *, now_ns: int) -> CatalogSnapshot:
        key = self._admit(reservation=None, gate=None, request_hash=None, model_id=None, feature=None)
        method, path = self.facts.require("list_models.endpoint")
        headers = self._headers(key)
        resp = self._send(HttpRequest(method, self.base_url() + path, headers))  # pragma: no cover
        if resp.status != 200:  # pragma: no cover
            return CatalogSnapshot.failure(PROVIDER_ID, now_ns, path, resp.status, classify_error(resp).message)
        items = json.loads(resp.body)[self.facts.require("list_models.items_field")]  # pragma: no cover
        return CatalogSnapshot.success(PROVIDER_ID, now_ns, path, resp.body, (normalize_model(o) for o in items))  # pragma: no cover

    def count_tokens(self, request: GenerationRequest, *, admission: Reservation, gate: GateDecision) -> TokenCount:
        key = self._admit(reservation=admission, gate=gate, request_hash=request.request_hash(),
                          model_id=request.model_id, feature="count_tokens")
        headers = self._headers(key)
        method, path = self.facts.require("count_tokens.endpoint")  # pragma: no cover
        resp = self._send(HttpRequest(method, self.base_url() + path, headers, self._body(request)))  # pragma: no cover
        if resp.status != 200:  # pragma: no cover
            raise classify_error(resp, model_id=request.model_id)
        return TokenCount(request.model_id, parse_count_response(resp.body), "provider_endpoint")  # pragma: no cover

    def generate_structured(self, request: GenerationRequest, *, reservation: Reservation, gate: GateDecision) -> GenerationResult:
        key = self._admit(reservation=reservation, gate=gate, request_hash=request.request_hash(),
                          model_id=request.model_id, feature="generate")
        headers = self._headers(key)
        method, path = self.facts.require("generate.endpoint")  # pragma: no cover
        resp = self._send(HttpRequest(method, self.base_url() + path, headers, self._body(request)))  # pragma: no cover
        if resp.status != 200:  # pragma: no cover
            raise classify_error(resp, model_id=request.model_id)
        self.facts.require("response.output_text")  # pragma: no cover
        raise AssertionError("unreachable")  # pragma: no cover
