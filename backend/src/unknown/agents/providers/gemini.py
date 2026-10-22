"""Google Gemini adapter (urllib), restricted to facts in the verified register.

Verified (implemented and tested):
  * GET https://generativelanguage.googleapis.com/v1beta/models with pageSize (default 50,
    at most 1000) and pageToken (all other parameters unchanged); nextPageToken; model
    fields inputTokenLimit, outputTokenLimit, supportedGenerationMethods, thinking; no
    lifecycle or shutdown field                                                  CAP-MODEL-01
  * Interactions API response_format with mime_type application/json; store=false sent
    on every request                                                             CAP-MODEL-02, CAP-MODEL-09
  * usage.total_cached_tokens                                                    CAP-MODEL-08
  * errors: 429 rate_limit_exceeded / too_many_requests (transient), quota_exceeded (not
    before the next midnight Pacific), RESOURCE_EXHAUSTED spend limit, 402 payment_required,
    503 service_unavailable, 500 api_error, 504 deadline_exceeded               CAP-MODEL-06

Not recorded (``UnverifiedFact``): the auth header, the list field holding models, the
field naming each model, the countTokens path prefix and response field, the Interactions
endpoint, the schema field inside response_format, where the generated text lives, the
prompt/output usage field names. Error labels are matched against any string in the error
body because the field that carries them is not recorded either.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ..budget import Reservation
from ..catalog import CatalogSnapshot, ModelRow
from ..gate import GateDecision
from .base import (ErrorClass, GenerationRequest, GenerationResult, HttpProviderAdapter, HttpResponse, ProtocolFacts,
                   ProviderError, TokenCount, Unverified, Verified, body_strings, parse_retry_after)

PROVIDER_ID = "gemini"
MAX_PAGE_SIZE = 1000
PACIFIC = "America/Los_Angeles"

FACTS = ProtocolFacts(PROVIDER_ID, {
    "base_url.global": Verified("https://generativelanguage.googleapis.com/v1beta", "CAP-MODEL-01"),
    "auth": Unverified("CAP-MODEL-01", "the request authentication header and scheme are not recorded"),
    "list_models.endpoint": Verified(("GET", "/models"), "CAP-MODEL-01"),
    "list_models.pagination": Verified({"page_size": "pageSize", "page_token": "pageToken", "next": "nextPageToken",
                                        "default": 50, "max": MAX_PAGE_SIZE}, "CAP-MODEL-01"),
    "list_models.items_field": Unverified("CAP-MODEL-01", "the list response field that holds the model objects is not recorded"),
    "model.id_field": Unverified("CAP-MODEL-01", "the field naming the model in a models.list entry is not recorded"),
    "model.fields": Verified(("inputTokenLimit", "outputTokenLimit", "supportedGenerationMethods", "thinking"), "CAP-MODEL-01"),
    "count_tokens.endpoint": Unverified("CAP-MODEL-04", "recorded only as POST .../models/{model}:countTokens; the prefix and the response field are not recorded"),
    "generate.endpoint": Unverified("CAP-MODEL-02", "the Interactions API endpoint path is not recorded"),
    "structured.response_format": Verified({"mime_type": "application/json"}, "CAP-MODEL-02"),
    "structured.schema_field": Unverified("CAP-MODEL-02", "the field holding the schema inside response_format is not recorded"),
    "request.store": Verified(False, "CAP-MODEL-09"),
    "response.output_text": Unverified("CAP-MODEL-02", "where an Interaction carries the generated text is not recorded"),
    "usage.cached": Verified(("usage", "total_cached_tokens"), "CAP-MODEL-08"),
    "usage.fields": Unverified("CAP-MODEL-08", "prompt/output token field names are not recorded"),
})


def page_params(page_token: str | None, page_size: int = MAX_PAGE_SIZE) -> dict:
    """Parameters for one models.list page; only the token changes between pages."""
    p = FACTS.require("list_models.pagination")
    if not 1 <= page_size <= p["max"]:
        raise ValueError(f"pageSize must be in [1, {p['max']}]")
    params = {p["page_size"]: str(page_size)}
    if page_token:
        params[p["page_token"]] = page_token
    return params


def next_page_token(body: Mapping[str, Any]) -> str | None:
    tok = body.get(FACTS.require("list_models.pagination")["next"])
    return tok if isinstance(tok, str) and tok else None


def model_limits(obj: Mapping[str, Any]) -> dict:
    """The verified fields of one models.list entry (the id field itself is not recorded)."""
    names = FACTS.require("model.fields")
    return {n: obj.get(n) for n in names}


def normalize_model(obj: Mapping[str, Any]) -> ModelRow:
    FACTS.require("model.id_field")
    raise AssertionError("unreachable")  # pragma: no cover


def structured_output_fragment() -> dict:
    return {"response_format": dict(FACTS.require("structured.response_format")), "store": FACTS.require("request.store")}


def next_pacific_midnight_ns(now_ns: int) -> int:
    tz = ZoneInfo(PACIFIC)
    local = datetime.fromtimestamp(now_ns / 1_000_000_000, tz)
    nxt = datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=tz)
    return int(nxt.timestamp()) * 1_000_000_000


def classify_error(resp: HttpResponse, *, now_ns: int, model_id: str | None = None) -> ProviderError:
    try:
        body = json.loads(resp.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    strings = body_strings(body)
    has = lambda label: any(label in x for x in strings)  # noqa: E731
    s = resp.status
    not_before = None
    if s == 429:
        if has("quota_exceeded"):
            cls, not_before = ErrorClass.QUOTA_OR_SPEND_EXHAUSTED, next_pacific_midnight_ns(now_ns)
        elif has("RESOURCE_EXHAUSTED"):
            cls = ErrorClass.QUOTA_OR_SPEND_EXHAUSTED
        elif has("rate_limit_exceeded") or has("too_many_requests"):
            cls = ErrorClass.TRANSIENT_RATE_LIMIT
        else:
            cls = ErrorClass.UNKNOWN
    elif s == 402:
        cls = ErrorClass.QUOTA_OR_SPEND_EXHAUSTED
    elif s == 503:
        cls = ErrorClass.OVERLOADED
    elif s == 500:
        cls = ErrorClass.SERVER_ERROR
    elif s == 504:
        cls = ErrorClass.TIMEOUT
    elif s in (401, 403):
        cls = ErrorClass.AUTH
    elif s in (400, 404, 405, 409, 413, 415, 422):
        cls = ErrorClass.INVALID_REQUEST
    else:
        cls = ErrorClass.UNKNOWN
    return ProviderError(PROVIDER_ID, cls, "; ".join(strings[:3]) or f"HTTP {s}", model_id=model_id, http_status=s,
                         retry_after_s=parse_retry_after(resp.headers), retry_not_before_ns=not_before)


class GeminiAdapter(HttpProviderAdapter):
    provider_id = PROVIDER_ID
    facts = FACTS
    list_requires = ("auth", "list_models.items_field", "model.id_field")
    count_requires = ("auth", "count_tokens.endpoint")
    generate_requires = ("auth", "generate.endpoint", "structured.schema_field", "response.output_text", "usage.fields")

    def _headers(self, key: str) -> dict:
        self.facts.require("auth")
        raise AssertionError("unreachable")  # pragma: no cover

    def list_models(self, *, now_ns: int) -> CatalogSnapshot:
        key = self._admit(reservation=None, gate=None, request_hash=None, model_id=None, feature=None)
        self._headers(key)
        raise AssertionError("unreachable")  # pragma: no cover

    def count_tokens(self, request: GenerationRequest, *, admission: Reservation, gate: GateDecision) -> TokenCount:
        key = self._admit(reservation=admission, gate=gate, request_hash=request.request_hash(),
                          model_id=request.model_id, feature="count_tokens")
        self._headers(key)
        raise AssertionError("unreachable")  # pragma: no cover

    def generate_structured(self, request: GenerationRequest, *, reservation: Reservation, gate: GateDecision) -> GenerationResult:
        key = self._admit(reservation=reservation, gate=gate, request_hash=request.request_hash(),
                          model_id=request.model_id, feature="generate")
        self._headers(key)
        raise AssertionError("unreachable")  # pragma: no cover
