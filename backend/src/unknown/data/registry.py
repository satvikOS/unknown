"""Provider and dataset registry (ADR-0015; DAT-01).

Registry entries are versioned JSON under ``config/providers/``. They hold no
secrets (keys and contact strings live in ~/.unknown/config/data.json). A
dataset without recorded rights is refused for production ingestion, and the
agent layer checks ``model_processing_cloud`` before sending content to a model."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ALLOWED_USES = frozenset({"display", "non_display", "storage", "derived_storage", "redistribution",
                          "model_processing_cloud", "model_processing_local"})
REPO = Path(__file__).resolve().parents[4]


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class RateLimit:
    max_requests: int
    per_seconds: float
    source: str


@dataclass(frozen=True)
class Dataset:
    dataset_id: str
    provider_id: str
    description: str
    endpoint: str
    coverage: str
    history_depth: str
    update_frequency: str
    availability_rule: str
    allowed_uses: frozenset[str]
    retention_days: int | None
    vintage_support: bool
    rights_source: str
    rights_verified_on: str

    def permits(self, use: str) -> bool:
        if use not in ALLOWED_USES:
            raise RegistryError(f"unknown use {use}")
        return use in self.allowed_uses


@dataclass(frozen=True)
class Provider:
    provider_id: str
    name: str
    access: str
    cost_model: str
    budget: str
    rate_limit: RateLimit
    requires_local_config: tuple[str, ...]
    failure_behavior: str
    sources: tuple[str, ...]
    verified_on: str
    datasets: tuple[Dataset, ...]


def _req(d: dict, key: str, where: str):
    if key not in d:
        raise RegistryError(f"{where}.{key} is required")
    return d[key]


def parse_provider(d: dict) -> Provider:
    pid = _req(d, "provider_id", "provider")
    rl = _req(d, "rate_limit", pid)
    datasets = []
    for ds in _req(d, "datasets", pid):
        uses = frozenset(_req(ds, "allowed_uses", ds.get("dataset_id", "?")))
        if not uses <= ALLOWED_USES:
            raise RegistryError(f"{ds.get('dataset_id')}: unknown uses {sorted(uses - ALLOWED_USES)}")
        datasets.append(Dataset(
            dataset_id=_req(ds, "dataset_id", pid), provider_id=pid, description=_req(ds, "description", pid),
            endpoint=_req(ds, "endpoint", pid), coverage=_req(ds, "coverage", pid), history_depth=_req(ds, "history_depth", pid),
            update_frequency=_req(ds, "update_frequency", pid), availability_rule=_req(ds, "availability_rule", pid),
            allowed_uses=uses, retention_days=ds.get("retention_days"), vintage_support=bool(_req(ds, "vintage_support", pid)),
            rights_source=_req(ds, "rights_source", pid), rights_verified_on=_req(ds, "rights_verified_on", pid)))
    return Provider(
        provider_id=pid, name=_req(d, "name", pid), access=_req(d, "access", pid), cost_model=_req(d, "cost_model", pid),
        budget=_req(d, "budget", pid),
        rate_limit=RateLimit(int(_req(rl, "max_requests", pid)), float(_req(rl, "per_seconds", pid)), _req(rl, "source", pid)),
        requires_local_config=tuple(d.get("requires_local_config", [])), failure_behavior=_req(d, "failure_behavior", pid),
        sources=tuple(_req(d, "sources", pid)), verified_on=_req(d, "verified_on", pid), datasets=tuple(datasets))


def load_registry(directory: Path | None = None) -> dict[str, Provider]:
    directory = directory or REPO / "config" / "providers"
    providers = {}
    for f in sorted(directory.glob("*.json")):
        p = parse_provider(json.loads(f.read_text()))
        if p.provider_id in providers:
            raise RegistryError(f"duplicate provider {p.provider_id}")
        providers[p.provider_id] = p
    return providers


def dataset(registry: dict[str, Provider], dataset_id: str) -> Dataset:
    for p in registry.values():
        for d in p.datasets:
            if d.dataset_id == dataset_id:
                return d
    raise RegistryError(f"dataset {dataset_id} is not registered; unregistered data cannot be ingested")


def local_config(name: str, home: Path | None = None) -> dict:
    """Owner-provided settings outside the repository (e.g. SEC User-Agent contact string)."""
    from ..service.paths import Paths
    paths = Paths(home) if home else Paths.default()
    path = paths.root / "config" / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; see docs/integrations/data-sources.md")
    return json.loads(path.read_text())
