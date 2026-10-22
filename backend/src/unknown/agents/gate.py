"""Data-use gate (ADR-0016 section 6; CAP-MODEL-09, CAP-MODEL-10, CAP-AGT-08; ADR-0015).

A payload may enter a provider request (generation, token counting or tokenizing alike,
CAP-MODEL-04) only if EVERY artifact in it is allowed:

  allow(a) = registry dataset exists
             AND Dataset.permits("model_processing_cloud")                    (ADR-0015)
             AND the owner data policy covers the dataset                         (owner)
             AND the provider policy record is fresh (checked_on + max age)       (CAP-MODEL-09)
             AND effective provider retention <= the dataset's retention ceiling  (CAP-MODEL-09 b)
             AND training use is off for this account                             (CAP-MODEL-09 c)
             AND residency_ok(dataset, endpoint region)                           (CAP-MODEL-10)
             AND the paid tier is recorded where the provider requires it         (CAP-MODEL-09 e)

Unknown facts block (a missing account fact, an unclassified model, a retention period
the register does not quantify, an unknown region). Document text never reaches this
module, so it cannot change an outcome. Each evaluation is recorded with the request
hash it authorises; adapters refuse to send unless they hold a matching allow decision.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..data.registry import Provider as DataProvider, RegistryError, dataset as find_dataset
from ..domain.canonical import content_hash
from .common import CONFIG_MODELS
from .config import OwnerPolicy

FEATURES = frozenset({"generate", "count_tokens", "batch"})


class GateError(RuntimeError):
    pass


@dataclass(frozen=True)
class GateDecision:
    decision_id: str
    allow: bool
    reasons: tuple[str, ...]
    provider: str
    model_id: str
    feature: str
    request_hash: str
    datasets: tuple[str, ...]
    policy_version: str
    decided_at_ns: int


def load_provider_policies(path: Path | None = None) -> dict:
    doc = json.loads((path or CONFIG_MODELS / "provider_policies.json").read_text())
    if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("providers"), dict):
        raise GateError("provider_policies.json must be {schema_version: 1, providers: {...}}")
    date.fromisoformat(doc["checked_on"])
    return doc


@dataclass
class DataUseGate:
    registry: Mapping[str, DataProvider]
    owner: OwnerPolicy | None
    provider_policies: dict
    clock_ns: Callable[[], int]
    decisions: list[GateDecision] = field(default_factory=list)

    @property
    def policy_version(self) -> str:
        dp = self.owner.data_policy if self.owner else None
        owner_part = None
        if dp is not None:
            owner_part = {"max_policy_age_days": dp.max_policy_age_days,
                          "default": _dp_json(dp.default),
                          "datasets": {k: _dp_json(v) for k, v in sorted(dp.datasets.items())}}
        return content_hash({"providers": self.provider_policies, "owner_data_policy": owner_part})

    # ------------------------------------------------------------------ provider-level facts
    def _feature(self, prov: dict, feature: str) -> dict | None:
        f = prov.get("features", {}).get(feature)
        seen = set()
        while isinstance(f, dict) and "same_as" in f:
            if f["same_as"] in seen:
                return None
            seen.add(f["same_as"])
            f = prov.get("features", {}).get(f["same_as"])
        return f if isinstance(f, dict) else None

    def _retention(self, provider: str, prov: dict, feature: str, model_id: str, account: Mapping[str, bool],
                   reasons: list[str]) -> int | None:
        f = self._feature(prov, feature)
        if f is None:
            reasons.append(f"provider_policy:{provider}:{feature}:not_recorded")
            return None
        retention = f.get("retention_days")
        if account.get("zdr_enabled") is True and "zdr_retention_days" in f:
            retention = f["zdr_retention_days"]
        if provider == "anthropic" and feature != "batch":
            covered = set(prov.get("covered_models", {}).get("model_ids", []))
            non_covered = set(prov.get("non_covered_model_ids", []))
            if model_id in covered:
                retention = f.get("covered_model_retention_days")
            elif model_id not in non_covered:
                reasons.append(f"provider_policy:anthropic:model_{model_id}_not_classified_as_covered_or_not")
                return None
        return retention

    def _provider_checks(self, provider: str, model_id: str, feature: str, reasons: list[str]) -> tuple[dict, Any] | None:
        prov = self.provider_policies.get("providers", {}).get(provider)
        if prov is None:
            reasons.append(f"provider_policy:{provider}:missing")
            return None
        if self.owner is None:
            reasons.append("owner_policy:absent")
            return None
        cfg = self.owner.provider(provider)
        if cfg is None or not cfg.enabled:
            reasons.append(f"owner_policy:{provider}:not_enabled")
            return None
        dp = self.owner.data_policy
        if dp is None:
            reasons.append("owner_data_policy:absent")
            return None
        checked = date.fromisoformat(self.provider_policies["checked_on"])
        today = datetime.fromtimestamp(self.clock_ns() // 1_000_000_000, tz=timezone.utc).date()
        if today > checked + timedelta(days=dp.max_policy_age_days):
            reasons.append(f"provider_policy:stale:checked_on_{checked.isoformat()}")
        training = prov.get("training", {})
        fact = training.get("account_fact")
        if fact is None or cfg.account.get(fact) is None:
            reasons.append(f"account_fact:{provider}:{fact}:unknown")
        elif cfg.account.get(fact) is not training.get("required_value"):
            reasons.append(f"training:{provider}:{training.get('rule')}:not_satisfied")
        if prov.get("paid_tier_required") and cfg.account.get("paid_tier") is not True:
            reasons.append(f"paid_tier:{provider}:not_recorded")
        region = prov.get("regions", {}).get(cfg.region)
        if region is None:
            reasons.append(f"region:{provider}:{cfg.region}:not_recorded")
        else:
            need = region.get("requires_account_fact")
            if need and cfg.account.get(need) is not True:
                reasons.append(f"region:{provider}:{cfg.region}:requires_{need}")
            allowed_models = region.get("models")
            if allowed_models is not None and model_id not in allowed_models:
                reasons.append(f"region:{provider}:{cfg.region}:model_{model_id}_not_served")
        if feature == "batch" and cfg.account.get("zdr_enabled") is True and self._feature(prov, "batch") and \
                self._feature(prov, "batch").get("zdr_available") is False:
            reasons.append(f"batch:{provider}:unavailable_under_zdr")
        return prov, (cfg, dp, region)

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, *, provider: str, model_id: str, feature: str, dataset_ids: Iterable[str],
                 request_hash: str) -> GateDecision:
        if feature not in FEATURES:
            raise GateError(f"unknown feature {feature}")
        reasons: list[str] = []
        datasets = tuple(sorted(set(dataset_ids)))
        checked = self._provider_checks(provider, model_id, feature, reasons)
        if checked is not None:
            prov, (cfg, dp, region) = checked
            retention = self._retention(provider, prov, feature, model_id, cfg.account, reasons)
            for ds_id in datasets:
                try:
                    ds = find_dataset(self.registry, ds_id)
                except RegistryError:
                    reasons.append(f"rights:{ds_id}:not_registered")
                    continue
                if not ds.permits("model_processing_cloud"):
                    reasons.append(f"rights:{ds_id}:model_processing_cloud_not_granted")
                    continue
                pol = dp.for_dataset(ds_id)
                if pol is None:
                    reasons.append(f"owner_data_policy:{ds_id}:absent")
                    continue
                if pol.max_provider_retention_days is not None:
                    if retention is None:
                        reasons.append(f"retention:{ds_id}:{provider}:{feature}:unknown_vs_ceiling_{pol.max_provider_retention_days}d")
                    elif retention > pol.max_provider_retention_days:
                        reasons.append(f"retention:{ds_id}:{provider}:{feature}:{retention}d>ceiling_{pol.max_provider_retention_days}d")
                if region is not None:
                    if not _region_ok(region.get("processing"), pol.allowed_processing_regions):
                        reasons.append(f"residency:{ds_id}:processing_{region.get('processing')}_not_allowed")
                    if not _region_ok(region.get("storage"), pol.allowed_storage_regions):
                        reasons.append(f"residency:{ds_id}:storage_{region.get('storage')}_not_allowed")
        decision = GateDecision(uuid.uuid4().hex, not reasons, tuple(reasons), provider, model_id, feature,
                                request_hash, datasets, self.policy_version, self.clock_ns())
        self.decisions.append(decision)
        return decision

    def dataset_allowed(self, dataset_id: str) -> bool:
        """Registry-level check used by the tool gateway before a tool result enters model context."""
        try:
            return find_dataset(self.registry, dataset_id).permits("model_processing_cloud")
        except RegistryError:
            return False


def _region_ok(region: str | None, allowed: frozenset[str]) -> bool:
    if "any" in allowed:
        return region is not None
    return region is not None and region != "unspecified" and region in allowed


def _dp_json(p) -> Any:
    if p is None:
        return None
    return {"max_provider_retention_days": p.max_provider_retention_days,
            "allowed_processing_regions": sorted(p.allowed_processing_regions),
            "allowed_storage_regions": sorted(p.allowed_storage_regions)}
