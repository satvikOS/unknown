"""Provider adapter interface, error taxonomy, retry policy, circuit breaker and transport.

Interface (ADR-0016 section 3): ``list_models``, ``count_tokens``, ``generate_structured``,
usage normalization and error classification. Adapters never retry on their own; the
runner applies one owner-configured retry policy so the spend ledger sees every attempt.

Send preconditions (enforced by ``HttpProviderAdapter._admit`` and by the SDK adapter):
  (a) the provider is enabled in the owner policy;
  (b) an API key is available from the configured environment variable (or key file);
  (c) for paid or content-bearing calls, an OPEN spend-ledger reservation exists;
  (d) for content-bearing calls, the data-use gate allowed this exact request hash.
Nothing is sent otherwise (``SendRefused``).

Error taxonomy (CAP-MODEL-06): TRANSIENT_RATE_LIMIT, QUOTA_OR_SPEND_EXHAUSTED, OVERLOADED,
SERVER_ERROR, TIMEOUT, AUTH, INVALID_REQUEST, UNKNOWN, plus NETWORK (connection failures;
not named in the record, treated as transient). QUOTA/AUTH/INVALID/UNKNOWN are never retried
and never trigger a fallback to another provider.

Protocol facts: each HTTP adapter declares a table of facts, each either ``Verified`` with
the register record that states it, or ``Unverified`` with the record that leaves it open.
Reading an unverified fact raises ``UnverifiedFact`` (a ``NotImplementedError``) naming
the record, so no request is ever built from a guessed field.
"""
from __future__ import annotations

import abc
import enum
import random
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from ...domain.canonical import canonical_json, content_hash, sha256_hex
from ...domain.fixed import Fixed
from ..budget import Reservation, SpendLedger
from ..catalog import CatalogSnapshot
from ..config import EstimateConfig, OwnerPolicy, OwnerPolicyError, ProviderConfig, resolve_api_key
from ..gate import GateDecision
from ..schema import CompiledSchema
from ..tariffs import Usage


# ============================================================================ errors
class ErrorClass(enum.Enum):
    TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"
    QUOTA_OR_SPEND_EXHAUSTED = "QUOTA_OR_SPEND_EXHAUSTED"
    OVERLOADED = "OVERLOADED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    AUTH = "AUTH"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN = "UNKNOWN"


TRANSIENT = frozenset({ErrorClass.TRANSIENT_RATE_LIMIT, ErrorClass.OVERLOADED, ErrorClass.SERVER_ERROR,
                       ErrorClass.TIMEOUT, ErrorClass.NETWORK})
BREAKER_CLASSES = frozenset({ErrorClass.OVERLOADED, ErrorClass.SERVER_ERROR})


class ProviderError(Exception):
    """A classified provider failure. ``sent`` is False only if the request provably never left."""

    def __init__(self, provider: str, cls: ErrorClass, message: str, *, model_id: str | None = None,
                 http_status: int | None = None, provider_type: str | None = None, provider_code: str | None = None,
                 retry_after_s: int | None = None, retry_not_before_ns: int | None = None,
                 request_id: str | None = None, sent: bool = True):
        super().__init__(f"{provider} {cls.value} (HTTP {http_status}): {message}")
        self.provider, self.cls, self.message, self.model_id = provider, cls, message, model_id
        self.http_status, self.provider_type, self.provider_code = http_status, provider_type, provider_code
        self.retry_after_s, self.retry_not_before_ns, self.request_id, self.sent = retry_after_s, retry_not_before_ns, request_id, sent

    @property
    def transient(self) -> bool:
        return self.cls in TRANSIENT

    def record(self) -> dict:
        return {"provider": self.provider, "model_id": self.model_id, "request_id": self.request_id,
                "http_status": self.http_status, "provider_type": self.provider_type, "provider_code": self.provider_code,
                "class": self.cls.value, "retryable": self.transient, "retry_after_s": self.retry_after_s}


class SendRefused(RuntimeError):
    """A send precondition failed; nothing left this process."""

    def __init__(self, provider: str, reason: str):
        self.provider, self.reason = provider, reason
        super().__init__(f"{provider}: refusing to send ({reason})")


class UnverifiedFact(NotImplementedError):
    def __init__(self, provider: str, key: str, record_id: str, detail: str):
        self.provider, self.key, self.record_id, self.detail = provider, key, record_id, detail
        super().__init__(f"{provider}.{key} is not verified ({record_id}): {detail}")


def parse_retry_after(headers: Mapping[str, str]) -> int | None:
    """Integer seconds from Retry-After; any other form is treated as absent."""
    v = headers.get("retry-after")
    if v is None:
        return None
    v = v.strip()
    return int(v) if v.isdigit() else None


def body_strings(body: Any) -> list[str]:
    """All string values in a decoded JSON error body (used where the field that carries a
    documented error label is itself not recorded)."""
    out: list[str] = []
    stack = [body]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return out


# ============================================================================ protocol facts
@dataclass(frozen=True)
class Verified:
    value: Any
    record_id: str
    note: str = ""


@dataclass(frozen=True)
class Unverified:
    record_id: str
    detail: str


class ProtocolFacts:
    def __init__(self, provider: str, facts: Mapping[str, Verified | Unverified]):
        self.provider = provider
        self._facts = dict(facts)

    def require(self, key: str) -> Any:
        f = self._facts.get(key)
        if f is None:
            raise UnverifiedFact(self.provider, key, "none", "no such fact is recorded")
        if isinstance(f, Unverified):
            raise UnverifiedFact(self.provider, key, f.record_id, f.detail)
        return f.value

    def is_verified(self, key: str) -> bool:
        return isinstance(self._facts.get(key), Verified)

    def unverified(self) -> dict[str, Unverified]:
        return {k: v for k, v in self._facts.items() if isinstance(v, Unverified)}

    def items(self):
        return self._facts.items()


# ============================================================================ retry and breaker
@dataclass(frozen=True)
class RetryPolicy:
    """retry_delay_n = min(D_max, max(retry_after_s, b * 2^n) + U(0, j)) (CAP-MODEL-06), only for
    transient classes, only within the attempt limit, and never past the job deadline."""
    max_attempts: int
    base_ms: int
    max_ms: int
    jitter_ms: int

    def delay_ns(self, attempt: int, err: ProviderError, *, now_ns: int, deadline_ns: int,
                 rand_ms: Callable[[int], int] = lambda hi: random.randint(0, hi) if hi > 0 else 0) -> int | None:
        if not err.transient or attempt >= self.max_attempts:
            return None
        backoff_ms = self.base_ms * (2 ** attempt)
        retry_after_ms = (err.retry_after_s or 0) * 1000
        delay_ms = min(self.max_ms, max(retry_after_ms, backoff_ms) + rand_ms(self.jitter_ms))
        delay_ms = max(delay_ms, retry_after_ms)  # never earlier than the provider asked
        if err.retry_not_before_ns is not None:
            delay_ns = max(delay_ms * 1_000_000, err.retry_not_before_ns - now_ns)
        else:
            delay_ns = delay_ms * 1_000_000
        if now_ns + delay_ns >= deadline_ns:
            return None
        return delay_ns


@dataclass
class CircuitBreaker:
    """Opens after ``failures`` consecutive OVERLOADED/SERVER_ERROR results within ``window_ns``;
    half-opens (one probe allowed) after ``cooldown_ns``."""
    failures: int
    window_ns: int
    cooldown_ns: int
    _state: dict[str, dict] = field(default_factory=dict)

    def _s(self, key: str) -> dict:
        return self._state.setdefault(key, {"fails": [], "opened_at": None, "probe": False})

    def allow(self, key: str, now_ns: int) -> bool:
        s = self._s(key)
        if s["opened_at"] is None:
            return True
        if now_ns - s["opened_at"] >= self.cooldown_ns and not s["probe"]:
            s["probe"] = True
            return True
        return False

    def state(self, key: str, now_ns: int) -> str:
        s = self._s(key)
        if s["opened_at"] is None:
            return "closed"
        return "half_open" if now_ns - s["opened_at"] >= self.cooldown_ns else "open"

    def record_success(self, key: str) -> None:
        self._state[key] = {"fails": [], "opened_at": None, "probe": False}

    def record_failure(self, key: str, cls: ErrorClass, now_ns: int) -> None:
        s = self._s(key)
        if cls not in BREAKER_CLASSES:
            s["fails"] = []
            return
        s["fails"] = [t for t in s["fails"] if now_ns - t <= self.window_ns] + [now_ns]
        if s["probe"] or len(s["fails"]) >= self.failures:
            s["opened_at"], s["probe"] = now_ns, False


# ============================================================================ transport
@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None

    def __repr__(self) -> str:  # never print header values (they may hold a key)
        return f"HttpRequest({self.method} {self.url}, headers={sorted(self.headers)})"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-cased names
    body: bytes


class TransportError(Exception):
    def __init__(self, message: str, *, sent: bool, timeout: bool = False):
        super().__init__(message)
        self.sent, self.timeout = sent, timeout


class Transport(Protocol):
    def send(self, request: HttpRequest, *, timeout_s: float) -> HttpResponse: ...


class UrllibTransport:
    """Real HTTPS transport (standard library). Only https:// URLs; HTTP errors are returned
    as responses for classification, network failures raise ``TransportError``."""

    def __init__(self, context: ssl.SSLContext | None = None):
        self._context = context or ssl.create_default_context()

    def send(self, request: HttpRequest, *, timeout_s: float) -> HttpResponse:
        if not request.url.startswith("https://"):
            raise TransportError("only https:// URLs may be sent", sent=False)
        req = urllib.request.Request(request.url, data=request.body, method=request.method, headers=dict(request.headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout_s, context=self._context) as resp:
                return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            finally:
                exc.close()
            return HttpResponse(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
        except TimeoutError as exc:
            raise TransportError(f"timeout: {exc}", sent=True, timeout=True) from None
        except urllib.error.URLError as exc:
            refused = isinstance(exc.reason, ConnectionRefusedError)
            raise TransportError(f"network error: {exc.reason}", sent=not refused) from None


# ============================================================================ requests and results
class OutputStatus(enum.Enum):
    COMPLETE = "complete"
    REFUSED = "refused"
    TRUNCATED = "truncated"


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    role: str
    model_id: str
    instructions: str
    user_content: str
    output: CompiledSchema
    max_output_tokens: int
    effort: str | None = None

    def request_hash(self) -> str:
        return content_hash({"model_id": self.model_id, "instructions": self.instructions, "user": self.user_content,
                             "schema": self.output.schema_hash, "provider": self.output.provider,
                             "max_output_tokens": self.max_output_tokens, "effort": self.effort})

    def payload_bytes(self) -> int:
        """UTF-8 bytes of everything sent: instructions, user content and the output schema."""
        return len(self.instructions.encode()) + len(self.user_content.encode()) + \
            len(canonical_json(self.output.provider_schema).encode())


@dataclass(frozen=True)
class TokenCount:
    model_id: str
    input_tokens: int
    method: str  # provider_endpoint | provider_text_tokenizer | local_estimate


@dataclass(frozen=True)
class GenerationResult:
    status: OutputStatus
    text: str | None
    usages: tuple[Usage, ...]
    usage_complete: bool
    returned_model_id: str | None
    request_id: str | None
    raw_sha256: str
    served_by_fallback: bool = False
    refusal_category: str | None = None
    inference_geo: str | None = None
    notes: tuple[str, ...] = ()


def local_estimate(request: GenerationRequest, cfg: EstimateConfig) -> TokenCount:
    """Conservative local count: ceil(utf8 bytes x factor) + overhead (CAP-MODEL-04 degraded path)."""
    scaled = Fixed(request.payload_bytes()) * cfg.bytes_per_token_factor
    tokens = -((-scaled.mantissa) // (10 ** scaled.scale)) + cfg.overhead_tokens
    return TokenCount(request.model_id, tokens, "local_estimate")


# ============================================================================ adapter interface
class ProviderAdapter(abc.ABC):
    provider_id: str

    @abc.abstractmethod
    def list_models(self, *, now_ns: int) -> CatalogSnapshot: ...

    @abc.abstractmethod
    def count_tokens(self, request: GenerationRequest, *, admission: Reservation, gate: GateDecision) -> TokenCount: ...

    @abc.abstractmethod
    def generate_structured(self, request: GenerationRequest, *, reservation: Reservation,
                            gate: GateDecision) -> GenerationResult: ...

    @abc.abstractmethod
    def unverified_requirements(self, operation: str) -> tuple[str, ...]:
        """Facts an operation needs that the register leaves unverified (empty = can run)."""

    def admission_problem(self) -> str | None:
        """Why this adapter cannot send at all right now (disabled, no key), or None."""
        return None

    def cache_write_ttls(self, request: GenerationRequest) -> tuple[str, ...]:
        """Cache-write TTLs this request can trigger (priced into the worst case)."""
        return ()

    def additional_billed_models(self, request: GenerationRequest) -> tuple[str, ...]:
        """Other models that may bill for this request (e.g. server-side refusal fallbacks)."""
        return ()

    def requested_region(self) -> str:
        return "global"


class HttpProviderAdapter(ProviderAdapter):
    """Shared admission logic for urllib-based adapters."""
    facts: ProtocolFacts
    count_requires: tuple[str, ...] = ()
    generate_requires: tuple[str, ...] = ()
    list_requires: tuple[str, ...] = ()

    def __init__(self, *, owner: OwnerPolicy | None, ledger: SpendLedger | None, transport: Transport,
                 environ: Mapping[str, str] | None = None, secrets_dir=None, timeout_s: float = 60.0):
        self.owner, self.ledger, self.transport = owner, ledger, transport
        self.environ, self.secrets_dir, self.timeout_s = environ, secrets_dir, timeout_s

    def _config(self) -> ProviderConfig | None:
        return self.owner.provider(self.provider_id) if self.owner else None

    def admission_problem(self) -> str | None:
        cfg = self._config()
        if cfg is None or not cfg.enabled:
            return "provider_disabled"
        try:
            if resolve_api_key(cfg, environ=self.environ, secrets_dir=self.secrets_dir) is None:
                return "api_key_missing"
        except OwnerPolicyError as exc:
            return f"api_key_unsafe:{exc}"
        return None

    def _admit(self, *, reservation: Reservation | None, gate: GateDecision | None, request_hash: str | None,
               model_id: str | None, feature: str | None) -> str:
        problem = self.admission_problem()
        if problem:
            raise SendRefused(self.provider_id, problem)
        if feature is not None:
            if reservation is None or self.ledger is None or not self.ledger.is_open(reservation):
                raise SendRefused(self.provider_id, "no_open_reservation")
            if reservation.provider != self.provider_id or reservation.model_id != model_id:
                raise SendRefused(self.provider_id, "reservation_mismatch")
            if gate is None or not gate.allow:
                raise SendRefused(self.provider_id, "gate_denied")
            if (gate.provider, gate.model_id, gate.feature, gate.request_hash) != (self.provider_id, model_id, feature, request_hash):
                raise SendRefused(self.provider_id, "gate_decision_mismatch")
        cfg = self._config()
        return resolve_api_key(cfg, environ=self.environ, secrets_dir=self.secrets_dir)

    def unverified_requirements(self, operation: str) -> tuple[str, ...]:
        keys = {"list_models": self.list_requires, "count_tokens": self.count_requires,
                "generate": self.generate_requires}.get(operation, ())
        return tuple(k for k in keys if not self.facts.is_verified(k))

    def base_url(self) -> str:
        cfg = self._config()
        region = cfg.region if cfg else "global"
        return self.facts.require(f"base_url.{region}")

    def _send(self, request: HttpRequest) -> HttpResponse:
        try:
            return self.transport.send(request, timeout_s=self.timeout_s)
        except TransportError as exc:
            cls = ErrorClass.TIMEOUT if exc.timeout else ErrorClass.NETWORK
            raise ProviderError(self.provider_id, cls, str(exc), sent=exc.sent) from None

    @staticmethod
    def raw_hash(body: bytes) -> str:
        return sha256_hex(body.decode("utf-8", "replace"))


def ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


