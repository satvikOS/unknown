"""Owner policy for the agent layer (ADR-0016 section 5).

The policy lives outside git at ``<UNKNOWN_HOME>/config/agents.json`` (default
``~/.unknown``). Nothing in the agent layer writes it: caps are owner policy and no
agent code path can raise them (CAP-AGT-02). If the file is absent, every provider
is disabled and every paid call is refused (AI-05). Unknown keys fail validation;
missing optional sections are never read as permissive defaults (book Ch 1,
CAP-MODEL-09 "unknown policy facts count as blocking").

Money is decimal text only ("2.00"); JSON numbers are refused for amounts (ADR-0004).
API keys are never stored in this file. ``api_key_env`` names an environment
variable; ``api_key_file`` optionally names a file under ``<UNKNOWN_HOME>/secrets``
that must be mode 0600 and owned by this user.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..domain.canonical import content_hash
from ..domain.fixed import Fixed
from ..domain.money import Money, currency
from ..service.paths import Paths

SCHEMA_VERSION = 1
PROVIDER_IDS = frozenset({"openai", "xai", "anthropic", "gemini"})
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
ACCOUNT_FACTS = frozenset({"training_opt_in", "training_permission_granted", "zdr_enabled", "paid_tier"})
REGION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}$")


class OwnerPolicyError(ValueError):
    """The owner policy file is malformed or unsafe; the agent layer fails closed."""


def default_policy_path(home: Path | None = None) -> Path:
    root = Paths(home).root if home else Paths.default().root
    return root / "config" / "agents.json"


def default_secrets_dir(home: Path | None = None) -> Path:
    root = Paths(home).root if home else Paths.default().root
    return root / "secrets"


# --------------------------------------------------------------------------- parts
@dataclass(frozen=True)
class ProviderCaps:
    lifetime: Money | None
    per_day: Money | None


@dataclass(frozen=True)
class Caps:
    """Spend caps. ``per_day`` and ``per_job`` are required; the rest are optional.

    ``per_role_per_day``: when present, a role it does not list has no budget.
    """
    currency: str
    per_day: Money
    per_job: Money
    lifetime: Money | None = None
    per_month: Money | None = None
    per_role_per_day: Mapping[str, Money] | None = None
    per_provider: Mapping[str, ProviderCaps] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class EffortConfig:
    default: str | None
    by_role: Mapping[str, str]

    def for_role(self, role: str) -> str | None:
        return self.by_role.get(role, self.default)


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    enabled: bool
    api_key_env: str
    region: str
    account: Mapping[str, bool]
    api_key_file: str | None = None
    default_model: str | None = None
    effort: EffortConfig | None = None
    refusal_fallback: str = "off"


@dataclass(frozen=True)
class DatasetPolicy:
    """Owner data policy for one dataset (or the default).

    ``max_provider_retention_days``: ``None`` means the owner accepts any provider
    retention ("unlimited"); 0 means no third-party retention.
    """
    max_provider_retention_days: int | None
    allowed_processing_regions: frozenset[str]
    allowed_storage_regions: frozenset[str]


@dataclass(frozen=True)
class DataPolicy:
    max_policy_age_days: int
    default: DatasetPolicy | None
    datasets: Mapping[str, DatasetPolicy]

    def for_dataset(self, dataset_id: str) -> DatasetPolicy | None:
        return self.datasets.get(dataset_id, self.default)


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 0       # retries after the first attempt; 0 = never retry
    base_ms: int = 1000
    max_ms: int = 30_000
    jitter_ms: int = 250


@dataclass(frozen=True)
class BreakerConfig:
    failures: int = 1
    window_s: int = 60
    cooldown_s: int = 300


@dataclass(frozen=True)
class EstimateConfig:
    """Local token estimate = ceil(utf8_bytes * factor) + overhead (CAP-MODEL-04 fallback)."""
    bytes_per_token_factor: Fixed = Fixed(1)
    overhead_tokens: int = 1024


@dataclass(frozen=True)
class ConcurrencyConfig:
    global_limit: int = 1
    per_role: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class OwnerPolicy:
    schema_version: int
    caps: Caps
    providers: Mapping[str, ProviderConfig]
    data_policy: DataPolicy | None
    retry: RetryConfig
    breaker: BreakerConfig
    estimate: EstimateConfig
    catalog_max_age_hours: int
    concurrency: ConcurrencyConfig
    policy_hash: str

    def provider(self, provider_id: str) -> ProviderConfig | None:
        return self.providers.get(provider_id)


# --------------------------------------------------------------------------- parsing helpers
def _obj(value: Any, where: str, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise OwnerPolicyError(f"{where} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise OwnerPolicyError(f"{where}: unknown keys {sorted(unknown)}")
    missing = set(required) - set(value)
    if missing:
        raise OwnerPolicyError(f"{where}: missing keys {sorted(missing)}")
    return value


def _money(value: Any, ccy: str, where: str) -> Money:
    if not isinstance(value, str):
        raise OwnerPolicyError(f"{where} must be a decimal string (JSON numbers are refused for money)")
    try:
        amount = Fixed.parse(value)
    except ValueError as exc:
        raise OwnerPolicyError(f"{where}: {exc}") from None
    if amount.is_negative():
        raise OwnerPolicyError(f"{where} must not be negative")
    return Money(ccy, amount)


def _int(value: Any, where: str, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise OwnerPolicyError(f"{where} must be an integer in [{lo}, {hi}]")
    return value


def _bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise OwnerPolicyError(f"{where} must be true or false")
    return value


def _regions(value: Any, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not value or not all(isinstance(r, str) and REGION_RE.match(r) for r in value):
        raise OwnerPolicyError(f"{where} must be a non-empty list of region codes (or [\"any\"])")
    return frozenset(value)


def _dataset_policy(value: Any, where: str) -> DatasetPolicy:
    d = _obj(value, where, {"max_provider_retention_days", "allowed_processing_regions", "allowed_storage_regions"},
             {"max_provider_retention_days", "allowed_processing_regions", "allowed_storage_regions"})
    ret = d["max_provider_retention_days"]
    if ret == "unlimited":
        retention = None
    else:
        retention = _int(ret, f"{where}.max_provider_retention_days", 0, 36500)
    return DatasetPolicy(retention, _regions(d["allowed_processing_regions"], f"{where}.allowed_processing_regions"),
                         _regions(d["allowed_storage_regions"], f"{where}.allowed_storage_regions"))


def _caps(value: Any, ccy: str) -> Caps:
    d = _obj(value, "caps", {"per_day", "per_job", "lifetime", "per_month", "per_role_per_day", "per_provider"},
             {"per_day", "per_job"})
    roles = None
    if "per_role_per_day" in d:
        r = d["per_role_per_day"]
        if not isinstance(r, dict):
            raise OwnerPolicyError("caps.per_role_per_day must be an object")
        out = {}
        for role, amount in r.items():
            if not ROLE_RE.match(role):
                raise OwnerPolicyError(f"caps.per_role_per_day: invalid role {role!r}")
            out[role] = _money(amount, ccy, f"caps.per_role_per_day.{role}")
        roles = MappingProxyType(out)
    per_provider = {}
    if "per_provider" in d:
        pp = d["per_provider"]
        if not isinstance(pp, dict):
            raise OwnerPolicyError("caps.per_provider must be an object")
        for pid, body in pp.items():
            if pid not in PROVIDER_IDS:
                raise OwnerPolicyError(f"caps.per_provider: unknown provider {pid!r}")
            b = _obj(body, f"caps.per_provider.{pid}", {"lifetime", "per_day"})
            per_provider[pid] = ProviderCaps(
                _money(b["lifetime"], ccy, f"caps.per_provider.{pid}.lifetime") if "lifetime" in b else None,
                _money(b["per_day"], ccy, f"caps.per_provider.{pid}.per_day") if "per_day" in b else None)
    return Caps(
        currency=ccy,
        per_day=_money(d["per_day"], ccy, "caps.per_day"),
        per_job=_money(d["per_job"], ccy, "caps.per_job"),
        lifetime=_money(d["lifetime"], ccy, "caps.lifetime") if "lifetime" in d else None,
        per_month=_money(d["per_month"], ccy, "caps.per_month") if "per_month" in d else None,
        per_role_per_day=roles,
        per_provider=MappingProxyType(per_provider))


def _provider(pid: str, value: Any) -> ProviderConfig:
    where = f"providers.{pid}"
    d = _obj(value, where, {"enabled", "api_key_env", "api_key_file", "region", "default_model", "effort",
                            "refusal_fallback", "account"}, {"enabled", "api_key_env", "region", "account"})
    if not isinstance(d["api_key_env"], str) or not ENV_RE.match(d["api_key_env"]):
        raise OwnerPolicyError(f"{where}.api_key_env must name an environment variable (A-Z, 0-9, _)")
    key_file = d.get("api_key_file")
    if key_file is not None and (not isinstance(key_file, str) or not FILE_RE.match(key_file)):
        raise OwnerPolicyError(f"{where}.api_key_file must be a plain file name under <UNKNOWN_HOME>/secrets")
    if not isinstance(d["region"], str) or not REGION_RE.match(d["region"]):
        raise OwnerPolicyError(f"{where}.region invalid")
    account_raw = _obj(d["account"], f"{where}.account", set(ACCOUNT_FACTS))
    account = MappingProxyType({k: _bool(v, f"{where}.account.{k}") for k, v in account_raw.items()})
    model = d.get("default_model")
    if model is not None and (not isinstance(model, str) or not MODEL_ID_RE.match(model)):
        raise OwnerPolicyError(f"{where}.default_model invalid")
    effort = None
    if "effort" in d:
        e = _obj(d["effort"], f"{where}.effort", {"default", "by_role"})
        default = e.get("default")
        if default is not None and default not in EFFORT_LEVELS:
            raise OwnerPolicyError(f"{where}.effort.default must be one of {sorted(EFFORT_LEVELS)}")
        by_role_raw = e.get("by_role", {})
        if not isinstance(by_role_raw, dict):
            raise OwnerPolicyError(f"{where}.effort.by_role must be an object")
        by_role = {}
        for role, level in by_role_raw.items():
            if not ROLE_RE.match(role) or level not in EFFORT_LEVELS:
                raise OwnerPolicyError(f"{where}.effort.by_role.{role} invalid")
            by_role[role] = level
        effort = EffortConfig(default, MappingProxyType(by_role))
    fallback = d.get("refusal_fallback", "off")
    if fallback not in ("off", "default"):
        raise OwnerPolicyError(f"{where}.refusal_fallback must be \"off\" or \"default\"")
    return ProviderConfig(pid, _bool(d["enabled"], f"{where}.enabled"), d["api_key_env"], d["region"], account,
                          key_file, model, effort, fallback)


def parse_owner_policy(doc: Any) -> OwnerPolicy:
    d = _obj(doc, "agents.json", {"schema_version", "comment", "currency", "caps", "providers", "data_policy", "retry",
                                  "circuit_breaker", "token_estimate", "catalog", "concurrency"},
             {"schema_version", "currency", "caps", "providers"})
    if type(d["schema_version"]) is not int or d["schema_version"] != SCHEMA_VERSION:
        raise OwnerPolicyError(f"schema_version must be {SCHEMA_VERSION}")
    ccy = d["currency"]
    try:
        currency(ccy)
    except Exception:
        raise OwnerPolicyError(f"unregistered currency {ccy!r}") from None
    caps = _caps(d["caps"], ccy)
    if not isinstance(d["providers"], dict):
        raise OwnerPolicyError("providers must be an object")
    providers = {}
    for pid, body in d["providers"].items():
        if pid not in PROVIDER_IDS:
            raise OwnerPolicyError(f"unknown provider {pid!r}; known: {sorted(PROVIDER_IDS)}")
        providers[pid] = _provider(pid, body)
    data_policy = None
    if "data_policy" in d:
        dp = _obj(d["data_policy"], "data_policy", {"max_policy_age_days", "default", "datasets"}, {"max_policy_age_days"})
        datasets_raw = dp.get("datasets", {})
        if not isinstance(datasets_raw, dict):
            raise OwnerPolicyError("data_policy.datasets must be an object")
        data_policy = DataPolicy(
            _int(dp["max_policy_age_days"], "data_policy.max_policy_age_days", 1, 3650),
            _dataset_policy(dp["default"], "data_policy.default") if "default" in dp else None,
            MappingProxyType({k: _dataset_policy(v, f"data_policy.datasets.{k}") for k, v in datasets_raw.items()}))
    retry = RetryConfig()
    if "retry" in d:
        r = _obj(d["retry"], "retry", {"max_attempts", "base_ms", "max_ms", "jitter_ms"},
                 {"max_attempts", "base_ms", "max_ms", "jitter_ms"})
        retry = RetryConfig(_int(r["max_attempts"], "retry.max_attempts", 0, 10), _int(r["base_ms"], "retry.base_ms", 1, 600_000),
                            _int(r["max_ms"], "retry.max_ms", 1, 3_600_000), _int(r["jitter_ms"], "retry.jitter_ms", 0, 60_000))
    breaker = BreakerConfig()
    if "circuit_breaker" in d:
        b = _obj(d["circuit_breaker"], "circuit_breaker", {"failures", "window_s", "cooldown_s"}, {"failures", "window_s", "cooldown_s"})
        breaker = BreakerConfig(_int(b["failures"], "circuit_breaker.failures", 1, 100),
                                _int(b["window_s"], "circuit_breaker.window_s", 1, 86_400),
                                _int(b["cooldown_s"], "circuit_breaker.cooldown_s", 1, 86_400))
    estimate = EstimateConfig()
    if "token_estimate" in d:
        t = _obj(d["token_estimate"], "token_estimate", {"bytes_per_token_factor", "overhead_tokens"},
                 {"bytes_per_token_factor", "overhead_tokens"})
        if not isinstance(t["bytes_per_token_factor"], str):
            raise OwnerPolicyError("token_estimate.bytes_per_token_factor must be a decimal string")
        factor = Fixed.parse(t["bytes_per_token_factor"])
        if not factor.is_positive():
            raise OwnerPolicyError("token_estimate.bytes_per_token_factor must be positive")
        estimate = EstimateConfig(factor, _int(t["overhead_tokens"], "token_estimate.overhead_tokens", 0, 1_000_000))
    catalog_age = 24
    if "catalog" in d:
        c = _obj(d["catalog"], "catalog", {"max_age_hours"}, {"max_age_hours"})
        catalog_age = _int(c["max_age_hours"], "catalog.max_age_hours", 1, 24 * 365)
    concurrency = ConcurrencyConfig()
    if "concurrency" in d:
        c = _obj(d["concurrency"], "concurrency", {"global", "per_role"}, {"global"})
        per_role_raw = c.get("per_role", {})
        if not isinstance(per_role_raw, dict):
            raise OwnerPolicyError("concurrency.per_role must be an object")
        concurrency = ConcurrencyConfig(_int(c["global"], "concurrency.global", 1, 64),
                                        MappingProxyType({k: _int(v, f"concurrency.per_role.{k}", 0, 64) for k, v in per_role_raw.items()}))
    return OwnerPolicy(SCHEMA_VERSION, caps, MappingProxyType(providers), data_policy, retry, breaker, estimate,
                       catalog_age, concurrency, content_hash(doc))


def _check_private_file(path: Path, *, allow_group_read: bool) -> None:
    st = path.stat()
    if st.st_uid != os.getuid():
        raise OwnerPolicyError(f"{path} is not owned by this user")
    bad = stat.S_IWGRP | stat.S_IWOTH | stat.S_IROTH | (0 if allow_group_read else stat.S_IRGRP)
    if st.st_mode & bad:
        raise OwnerPolicyError(f"{path} has unsafe permissions {oct(st.st_mode & 0o777)}; chmod 600")


def load_owner_policy(path: Path | None = None) -> OwnerPolicy | None:
    """Return the owner policy, or ``None`` when the file does not exist (paid calls disabled).

    A file that exists but is malformed or writable/readable by others raises
    ``OwnerPolicyError``: a broken policy must not silently become "no policy".
    """
    path = path or default_policy_path()
    if not path.exists():
        return None
    _check_private_file(path, allow_group_read=True)
    try:
        doc = json.loads(path.read_text(), parse_float=_refuse_float)
    except json.JSONDecodeError as exc:
        raise OwnerPolicyError(f"{path}: {exc}") from None
    return parse_owner_policy(doc)


def _refuse_float(text: str):
    raise OwnerPolicyError(f"floating-point number {text} in owner policy; use decimal strings")


def resolve_api_key(cfg: ProviderConfig, *, environ: Mapping[str, str] | None = None,
                    secrets_dir: Path | None = None) -> str | None:
    """API key from ``cfg.api_key_env``, else from ``<secrets>/<api_key_file>`` (mode 0600 only).

    Returns ``None`` when no key is available. Never logs or returns the key in an error.
    """
    env = os.environ if environ is None else environ
    value = env.get(cfg.api_key_env, "")
    if value.strip():
        return value.strip()
    if cfg.api_key_file:
        path = (secrets_dir or default_secrets_dir()) / cfg.api_key_file
        if path.exists():
            st = path.stat()
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise OwnerPolicyError(f"{path} must be mode 0600 and owned by this user; refusing to read it")
            text = path.read_text().strip()
            return text or None
    return None
