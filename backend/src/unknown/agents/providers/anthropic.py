"""Anthropic adapter on the official ``anthropic`` Python SDK (pinned in backend/requirements-agents.txt).

The SDK is imported lazily (only by the default client factory) so the core and every
other test run without it. Unit tests inject a fake client with the same method shape.

Request shape (Claude API reference, cached 2026-10-06; SDK 1.12.1 signatures checked):
  client.messages.create(model, max_tokens, system, messages=[{"role": "user", ...}],
                         output_config={"effort": <explicit>, "format": {"type": "json_schema", "schema": ...}},
                         inference_geo=<explicit>)
  or, with the owner's ``refusal_fallback: "default"``,
  client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default").
Never sent: ``thinking`` (always on for the default model; disabled/budget forms are a 400),
``temperature``/``top_p``/``top_k`` (400 since 2026-10-21), ``tool_choice`` (forced choice is a
400 on the default model; CAP-MODEL-03), ``output_format`` (deprecated; CAP-MODEL-02), an
assistant prefill, or ``cache_control`` (so no cache writes are billed).
``inference_geo`` is always explicit because an omitted value falls back to the workspace
default, whose price uplift could differ (CAP-MODEL-05/10). The client is built with
``max_retries=0``: the runner's own retry policy decides, so the ledger sees every attempt.

Response handling: ``stop_reason`` is checked before content ("refusal" -> typed refusal
with ``stop_details.category``; "max_tokens" -> truncated; anything but "end_turn" is not
trusted). Usage: top-level ``input_tokens``, ``cache_creation_input_tokens`` (split by
``cache_creation`` 5m/1h when present), ``cache_read_input_tokens``, ``output_tokens``; when a
server-side fallback ran, ``usage.iterations`` is the per-attempt billing record and every
attempt is priced at its own model's tariff. ``usage.inference_geo`` is reported so the runner
can quarantine a mismatch (CAP-MODEL-10). ``_request_id`` is recorded.

Errors (CAP-MODEL-06): 429 with ``error.details.error_code == enforced_spend_limit_reached`` or
without retry-after -> QUOTA (never retried; the tier spend-cap 429 has no retry-after);
429 with retry-after -> TRANSIENT; 400 whose message starts "You have reached your specified
(workspace) API usage limits" -> QUOTA; 402 -> QUOTA; 401/403 -> AUTH; 400/404/409/413/422 ->
INVALID; 529/503 -> OVERLOADED; 500 -> SERVER_ERROR; 504 and client timeouts -> TIMEOUT;
connection errors -> NETWORK.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from ...domain.canonical import sha256_hex
from ..budget import Reservation, SpendLedger
from ..catalog import Capability, CatalogSnapshot, ModelRow
from ..config import OwnerPolicy, OwnerPolicyError, ProviderConfig, resolve_api_key
from ..gate import GateDecision
from ..tariffs import Usage
from .base import (ErrorClass, GenerationRequest, GenerationResult, OutputStatus, ProviderAdapter, ProviderError,
                   SendRefused, TokenCount, body_strings)

PROVIDER_ID = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SPEND_LIMIT_PREFIXES = ("You have reached your specified API usage limits",
                        "You have reached your specified workspace API usage limits")
FORBIDDEN_PARAMS = frozenset({"thinking", "temperature", "top_p", "top_k", "tool_choice", "output_format", "cache_control"})
MAX_REQUEST_BYTES = 32_000_000  # Messages and Token Counting requests are limited to 32 MB (CAP-MODEL-06)
CAPABILITY_FIELDS = ("batch", "citations", "code_execution", "image_input", "pdf_input", "structured_outputs")


def default_client_factory(timeout_s: float) -> Callable[[str], Any]:
    def build(api_key: str):
        import anthropic  # lazy: research-side dependency only
        return anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout_s)
    return build


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def normalize_model(info: Any, verified_at: str) -> ModelRow:
    caps = {}
    capabilities = _attr(info, "capabilities")
    if capabilities is not None:
        for name in CAPABILITY_FIELDS:
            support = _attr(capabilities, name)
            supported = _attr(support, "supported") if support is not None else None
            if isinstance(supported, bool):
                caps[name] = Capability(supported, "api", verified_at)
    lifecycle = _attr(info, "lifecycle") or "unknown"
    return ModelRow(provider=PROVIDER_ID, model_id=_attr(info, "id"), display_name=_attr(info, "display_name"),
                    created=_iso(_attr(info, "created_at")), lifecycle=lifecycle,
                    deprecated_at=_iso(_attr(info, "deprecated_at")), retires_at=_iso(_attr(info, "retires_at")),
                    max_input_tokens=_attr(info, "max_input_tokens"), max_output_tokens=_attr(info, "max_tokens"),
                    capabilities=caps)


def _usage_from(u: Any, model_id: str, region: str) -> Usage:
    cache_write = {}
    created = _attr(u, "cache_creation_input_tokens") or 0
    split = _attr(u, "cache_creation")
    if created:
        five = _attr(split, "ephemeral_5m_input_tokens") if split is not None else None
        hour = _attr(split, "ephemeral_1h_input_tokens") if split is not None else None
        if isinstance(five, int) and isinstance(hour, int) and five + hour == created:
            cache_write = {k: v for k, v in (("5m", five), ("1h", hour)) if v}
        else:
            cache_write = {"unknown": created}
    return Usage(provider=PROVIDER_ID, model_id=model_id, input_tokens=_attr(u, "input_tokens") or 0,
                 output_tokens=_attr(u, "output_tokens") or 0, cached_read_tokens=_attr(u, "cache_read_input_tokens") or 0,
                 cache_write_tokens=cache_write, region=region)


def total_input_tokens(u: Any) -> int:
    """CAP-MODEL-04/08: Anthropic total input = cache_read + cache_creation + input_tokens."""
    return (_attr(u, "cache_read_input_tokens") or 0) + (_attr(u, "cache_creation_input_tokens") or 0) + (_attr(u, "input_tokens") or 0)


def normalize_usage(resp: Any, region: str) -> tuple[tuple[Usage, ...], bool, bool]:
    """Return (usages per attempt, complete, served_by_fallback)."""
    usage = _attr(resp, "usage")
    if usage is None:
        return (), False, False
    iterations = _attr(usage, "iterations")
    if iterations:
        out, complete, fallback = [], True, False
        for it in iterations:
            kind = _attr(it, "type")
            model = _attr(it, "model")
            if kind == "fallback_message":
                fallback = True
            if kind not in ("message", "fallback_message") or not isinstance(model, str):
                complete = False
                continue
            out.append(_usage_from(it, model, region))
        return tuple(out), complete and bool(out), fallback
    model = _attr(resp, "model")
    if not isinstance(model, str):
        return (), False, False
    return (_usage_from(usage, model, region),), True, False


def classify_status(status: int, headers: Mapping[str, str], body: Any, message: str = "",
                    request_id: str | None = None, model_id: str | None = None) -> ProviderError:
    strings = body_strings(body) + ([message] if message else [])
    err = body.get("error") if isinstance(body, dict) else None
    etype = err.get("type") if isinstance(err, dict) else None
    details = err.get("details") if isinstance(err, dict) else None
    code = details.get("error_code") if isinstance(details, dict) else None
    ra = headers.get("retry-after") if headers else None
    retry_after = int(ra) if isinstance(ra, str) and ra.strip().isdigit() else None
    if status == 429:
        if code == "enforced_spend_limit_reached" or ra is None:
            cls = ErrorClass.QUOTA_OR_SPEND_EXHAUSTED
        else:
            cls = ErrorClass.TRANSIENT_RATE_LIMIT
    elif status == 400 and any(s.strip().startswith(SPEND_LIMIT_PREFIXES) for s in strings):
        cls = ErrorClass.QUOTA_OR_SPEND_EXHAUSTED
    elif status == 402:
        cls = ErrorClass.QUOTA_OR_SPEND_EXHAUSTED
    elif status in (401, 403):
        cls = ErrorClass.AUTH
    elif status in (400, 404, 405, 409, 413, 415, 422):
        cls = ErrorClass.INVALID_REQUEST
    elif status in (529, 503):
        cls = ErrorClass.OVERLOADED
    elif status == 504:
        cls = ErrorClass.TIMEOUT
    elif status >= 500:
        cls = ErrorClass.SERVER_ERROR
    else:
        cls = ErrorClass.UNKNOWN
    return ProviderError(PROVIDER_ID, cls, message or "; ".join(strings[:2]) or f"HTTP {status}", model_id=model_id,
                         http_status=status, provider_type=etype, provider_code=code, retry_after_s=retry_after,
                         request_id=request_id)


def map_exception(exc: BaseException, model_id: str | None = None) -> ProviderError | None:
    """Map an SDK exception (duck-typed, so tests need not import the SDK) to the taxonomy."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        response = getattr(exc, "response", None)
        headers = {}
        if response is not None and getattr(response, "headers", None) is not None:
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        return classify_status(status, headers, getattr(exc, "body", None), getattr(exc, "message", "") or "",
                               getattr(exc, "request_id", None), model_id)
    names = {c.__name__ for c in type(exc).__mro__}
    if "APITimeoutError" in names:
        return ProviderError(PROVIDER_ID, ErrorClass.TIMEOUT, "client timeout", model_id=model_id, sent=True)
    if "APIConnectionError" in names:
        return ProviderError(PROVIDER_ID, ErrorClass.NETWORK, "connection error", model_id=model_id, sent=True)
    return None


class AnthropicAdapter(ProviderAdapter):
    provider_id = PROVIDER_ID

    def __init__(self, *, owner: OwnerPolicy | None, ledger: SpendLedger | None, provider_policy: Mapping[str, Any],
                 client_factory: Callable[[str], Any] | None = None, environ: Mapping[str, str] | None = None,
                 secrets_dir=None, timeout_s: float = 120.0):
        self.owner, self.ledger, self.policy = owner, ledger, provider_policy
        self.client_factory = client_factory or default_client_factory(timeout_s)
        self.environ, self.secrets_dir = environ, secrets_dir
        self._client = None
        self.request_log: list[dict] = []

    # ------------------------------------------------------------------ configuration
    def _config(self) -> ProviderConfig | None:
        return self.owner.provider(PROVIDER_ID) if self.owner else None

    def admission_problem(self) -> str | None:
        cfg = self._config()
        if cfg is None or not cfg.enabled:
            return "provider_disabled"
        try:
            if resolve_api_key(cfg, environ=self.environ, secrets_dir=self.secrets_dir) is None:
                return "api_key_missing"
        except OwnerPolicyError as exc:
            return f"api_key_unsafe:{exc}"
        if self.requested_region() is None:
            return f"region_{cfg.region}_not_recorded"
        return None

    def requested_region(self) -> str | None:
        cfg = self._config()
        region = self.policy.get("regions", {}).get(cfg.region if cfg else "global")
        return region.get("inference_geo") if isinstance(region, Mapping) else None

    def unverified_requirements(self, operation: str) -> tuple[str, ...]:
        return ()

    def fallback_enabled(self) -> bool:
        cfg = self._config()
        return cfg is not None and cfg.refusal_fallback == "default"

    def additional_billed_models(self, request: GenerationRequest) -> tuple[str, ...]:
        if not self.fallback_enabled():
            return ()
        return tuple(self.policy.get("fallback_targets", {}).get("model_ids", ()))

    def cache_write_ttls(self, request: GenerationRequest) -> tuple[str, ...]:
        return ()  # no cache_control is ever sent

    def effort_for(self, role: str) -> str | None:
        cfg = self._config()
        return cfg.effort.for_role(role) if cfg and cfg.effort else None

    def _client_for_send(self, *, reservation: Reservation | None, gate: GateDecision | None,
                         request: GenerationRequest | None, feature: str | None):
        problem = self.admission_problem()
        if problem:
            raise SendRefused(PROVIDER_ID, problem)
        if feature is not None:
            if reservation is None or self.ledger is None or not self.ledger.is_open(reservation):
                raise SendRefused(PROVIDER_ID, "no_open_reservation")
            if reservation.provider != PROVIDER_ID or reservation.model_id != request.model_id:
                raise SendRefused(PROVIDER_ID, "reservation_mismatch")
            if gate is None or not gate.allow:
                raise SendRefused(PROVIDER_ID, "gate_denied")
            if (gate.provider, gate.model_id, gate.feature, gate.request_hash) != (PROVIDER_ID, request.model_id, feature, request.request_hash()):
                raise SendRefused(PROVIDER_ID, "gate_decision_mismatch")
            if request.payload_bytes() > MAX_REQUEST_BYTES:
                raise SendRefused(PROVIDER_ID, "request_too_large")
        if self._client is None:
            key = resolve_api_key(self._config(), environ=self.environ, secrets_dir=self.secrets_dir)
            self._client = self.client_factory(key)
        return self._client

    # ------------------------------------------------------------------ request building
    def build_params(self, request: GenerationRequest) -> dict:
        if not request.effort:
            raise SendRefused(PROVIDER_ID, "effort_not_configured_for_role")
        params = {
            "model": request.model_id,
            "max_tokens": request.max_output_tokens,
            "system": request.instructions,
            "messages": [{"role": "user", "content": request.user_content}],
            "output_config": {"effort": request.effort,
                              "format": {"type": "json_schema", "schema": request.output.provider_schema}},
            "inference_geo": self.requested_region(),
        }
        assert not FORBIDDEN_PARAMS & set(params)
        return params

    # ------------------------------------------------------------------ operations
    def list_models(self, *, now_ns: int) -> CatalogSnapshot:
        client = self._client_for_send(reservation=None, gate=None, request=None, feature=None)
        from datetime import datetime, timezone
        verified_at = datetime.fromtimestamp(now_ns // 1_000_000_000, tz=timezone.utc).date().isoformat()
        try:
            rows, raw = [], []
            for info in client.models.list():  # the SDK paginates (has_more / last_id) to exhaustion
                rows.append(normalize_model(info, verified_at))
                raw.append(info.to_json() if hasattr(info, "to_json") else json.dumps(info, default=str, sort_keys=True))
        except Exception as exc:
            err = map_exception(exc)
            if err is None:
                raise
            return CatalogSnapshot.failure(PROVIDER_ID, now_ns, "/v1/models", err.http_status, err.message)
        return CatalogSnapshot.success(PROVIDER_ID, now_ns, "/v1/models", "\n".join(raw).encode(), rows)

    def count_tokens(self, request: GenerationRequest, *, admission: Reservation, gate: GateDecision) -> TokenCount:
        client = self._client_for_send(reservation=admission, gate=gate, request=request, feature="count_tokens")
        try:
            resp = client.messages.count_tokens(
                model=request.model_id, system=request.instructions,
                messages=[{"role": "user", "content": request.user_content}],
                output_config={"format": {"type": "json_schema", "schema": request.output.provider_schema}})
        except Exception as exc:
            err = map_exception(exc, request.model_id)
            if err is None:
                raise
            raise err from None
        tokens = _attr(resp, "input_tokens")
        if type(tokens) is not int or tokens < 0:
            raise ProviderError(PROVIDER_ID, ErrorClass.UNKNOWN, "count_tokens returned no input_tokens", model_id=request.model_id)
        return TokenCount(request.model_id, tokens, "provider_endpoint")

    def generate_structured(self, request: GenerationRequest, *, reservation: Reservation, gate: GateDecision) -> GenerationResult:
        client = self._client_for_send(reservation=reservation, gate=gate, request=request, feature="generate")
        params = self.build_params(request)
        region = params["inference_geo"]
        try:
            if self.fallback_enabled():
                resp = client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                resp = client.messages.create(**params)
        except Exception as exc:
            err = map_exception(exc, request.model_id)
            if err is None:
                raise
            self.request_log.append({"model": request.model_id, "max_tokens": params["max_tokens"], "error": err.cls.value,
                                     "request_id": err.request_id})
            raise err from None
        request_id = getattr(resp, "_request_id", None)
        raw = resp.to_json() if hasattr(resp, "to_json") else json.dumps(resp, default=str, sort_keys=True)
        stop = _attr(resp, "stop_reason")
        refusal_category = None
        if stop == "refusal":
            status = OutputStatus.REFUSED
            details = _attr(resp, "stop_details")
            refusal_category = _attr(details, "category") if details is not None else None
        elif stop == "end_turn":
            status = OutputStatus.COMPLETE
        else:
            status = OutputStatus.TRUNCATED  # max_tokens, pause_turn, context window, ...: not trusted
        text = None
        if status is OutputStatus.COMPLETE:
            blocks = list(_attr(resp, "content") or [])
            last_fallback = max((i for i, b in enumerate(blocks) if _attr(b, "type") == "fallback"), default=-1)
            text = "".join(_attr(b, "text") or "" for b in blocks[last_fallback + 1:] if _attr(b, "type") == "text")
        usages, complete, fallback_ran = normalize_usage(resp, region)
        returned = _attr(resp, "model")
        geo = _attr(_attr(resp, "usage"), "inference_geo")
        self.request_log.append({"model": request.model_id, "max_tokens": params["max_tokens"], "returned_model": returned,
                                 "stop_reason": stop, "request_id": request_id, "served_by_fallback": fallback_ran})
        notes = (f"stop_reason={stop}",) + (("served_by_fallback",) if fallback_ran else ())
        return GenerationResult(status, text, usages, complete, returned, request_id, sha256_hex(raw),
                                served_by_fallback=fallback_ran, refusal_category=refusal_category,
                                inference_geo=geo, notes=notes)
