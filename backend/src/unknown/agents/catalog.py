"""Runtime model catalog: snapshots, diff, approvals and routability (CAP-MODEL-01, CAP-MODEL-11).

* Model ids are discovered at runtime from each provider's list endpoint; application
  code holds none. A snapshot keeps fetched_at, HTTP status, the SHA-256 of the raw body
  and normalized rows; it is diffed against the previous successful snapshot.
* New ids are ``unreviewed`` and cannot be routed until the owner approves them for a
  role in ``config/models/approved.json`` (shipped empty).
* An approved id missing from a successful snapshot becomes non-routable at once and
  raises an alert; a failed refresh keeps the last good snapshot and adds nothing.
  A 401/403 marks the provider ``credentials_invalid`` and removes all its models.
* routable(m, role, t) = approved(m, role) AND present(m, latest successful snapshot)
  AND lifecycle(m) = active (or, for providers whose list exposes no lifecycle, an owner
  lifecycle review that has not expired) AND required caps are verified AND staleness <= max age.
* Production pins refuse moving aliases ("-latest", or an id that another row lists as an alias).
* Provider-reported prices (xAI, unit usd_cents_per_100m_tokens) are stored unchanged and
  flagged non-authoritative; the tariff module never reads them.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from ..domain.canonical import sha256_hex
from .common import CONFIG_MODELS

LIFECYCLES = frozenset({"active", "deprecated", "retired", "unknown"})
PROVENANCE = frozenset({"api", "doc_snapshot", "probe"})
DIFF_FIELDS = ("aliases", "display_name", "lifecycle", "deprecated_at", "retires_at", "max_input_tokens",
               "max_output_tokens", "input_modalities", "output_modalities", "capabilities", "fingerprint", "version")


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Capability:
    value: bool
    provenance: str
    verified_at: str

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE:
            raise CatalogError(f"capability provenance must be one of {sorted(PROVENANCE)}")


@dataclass(frozen=True)
class ModelRow:
    provider: str
    model_id: str
    aliases: tuple[str, ...] = ()
    display_name: str | None = None
    created: str | None = None
    lifecycle: str = "unknown"
    deprecated_at: str | None = None
    retires_at: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    capabilities: Mapping[str, Capability] = field(default_factory=lambda: MappingProxyType({}))
    fingerprint: str | None = None
    version: str | None = None
    provider_prices: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    provider_price_unit: str | None = None

    def __post_init__(self) -> None:
        if self.lifecycle not in LIFECYCLES:
            raise CatalogError(f"lifecycle must be one of {sorted(LIFECYCLES)}")

    def comparable(self) -> dict:
        out = {}
        for f in DIFF_FIELDS:
            v = getattr(self, f)
            if f == "capabilities":
                v = {k: (c.value, c.provenance) for k, c in sorted(v.items())}
            out[f] = v
        return out


@dataclass(frozen=True)
class CatalogSnapshot:
    provider: str
    fetched_at_ns: int
    endpoint: str
    http_status: int | None
    ok: bool
    raw_sha256: str | None
    rows: Mapping[str, ModelRow]
    error: str | None = None

    @classmethod
    def success(cls, provider: str, fetched_at_ns: int, endpoint: str, raw_body: bytes, rows: Iterable[ModelRow],
                http_status: int = 200) -> "CatalogSnapshot":
        by_id = {}
        for r in rows:
            if r.provider != provider:
                raise CatalogError("row provider mismatch")
            if r.model_id in by_id:
                raise CatalogError(f"duplicate model id {r.model_id} in snapshot")
            by_id[r.model_id] = r
        return cls(provider, fetched_at_ns, endpoint, http_status, True, sha256_hex(raw_body.decode("utf-8", "replace")),
                   MappingProxyType(by_id))

    @classmethod
    def failure(cls, provider: str, fetched_at_ns: int, endpoint: str, http_status: int | None, error: str) -> "CatalogSnapshot":
        return cls(provider, fetched_at_ns, endpoint, http_status, False, None, MappingProxyType({}), error)


@dataclass(frozen=True)
class CatalogDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: Mapping[str, Mapping[str, tuple[Any, Any]]]


def diff(prev: CatalogSnapshot | None, new: CatalogSnapshot) -> CatalogDiff:
    old_rows = prev.rows if prev else {}
    added = tuple(sorted(set(new.rows) - set(old_rows)))
    removed = tuple(sorted(set(old_rows) - set(new.rows)))
    changed = {}
    for mid in sorted(set(new.rows) & set(old_rows)):
        a, b = old_rows[mid].comparable(), new.rows[mid].comparable()
        delta = {k: (a[k], b[k]) for k in DIFF_FIELDS if a[k] != b[k]}
        if delta:
            changed[mid] = MappingProxyType(delta)
    return CatalogDiff(added, removed, MappingProxyType(changed))


# ============================================================================ approvals
@dataclass(frozen=True)
class Approval:
    provider: str
    model_id: str
    roles: frozenset[str]
    approved_by: str
    approved_at: str
    policy_version: str
    lifecycle_reviewed_until: date | None = None


def load_approvals(path: Path | None = None) -> tuple[Approval, ...]:
    doc = json.loads((path or CONFIG_MODELS / "approved.json").read_text())
    if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("approvals"), list):
        raise CatalogError("approved.json must be {schema_version: 1, approvals: [...]}")
    out = []
    keys = {"provider", "model_id", "roles", "approved_by", "approved_at", "policy_version", "lifecycle_reviewed_until", "note"}
    for i, a in enumerate(doc["approvals"]):
        if not isinstance(a, dict) or set(a) - keys:
            raise CatalogError(f"approvals[{i}] malformed")
        for k in ("provider", "model_id", "approved_by", "approved_at", "policy_version"):
            if not isinstance(a.get(k), str) or not a[k]:
                raise CatalogError(f"approvals[{i}].{k} required")
        if a["model_id"].endswith("-latest"):
            raise CatalogError(f"approvals[{i}]: '{a['model_id']}' is a moving alias; pin a specific id (CAP-MODEL-11)")
        roles = a.get("roles")
        if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles):
            raise CatalogError(f"approvals[{i}].roles must be a non-empty list")
        until = a.get("lifecycle_reviewed_until")
        out.append(Approval(a["provider"], a["model_id"], frozenset(roles), a["approved_by"], a["approved_at"],
                            a["policy_version"], date.fromisoformat(until) if until else None))
    return tuple(out)


# ============================================================================ catalog state
@dataclass
class Catalog:
    approvals: tuple[Approval, ...]
    clock_ns: Callable[[], int]
    max_age_ns: int
    store_dir: Path | None = None
    last_good: dict[str, CatalogSnapshot] = field(default_factory=dict)
    credentials_invalid: set[str] = field(default_factory=set)
    alerts: list[dict] = field(default_factory=list)

    def _alert(self, kind: str, **detail: Any) -> None:
        self.alerts.append({"kind": kind, "at_ns": self.clock_ns(), **detail})

    def ingest(self, snap: CatalogSnapshot) -> CatalogDiff | None:
        """Apply one refresh result. Returns the diff for a successful snapshot, else None."""
        if not snap.ok:
            if snap.http_status in (401, 403):
                self.credentials_invalid.add(snap.provider)
                self._alert("credentials_invalid", provider=snap.provider, http_status=snap.http_status)
            else:
                self._alert("refresh_failed", provider=snap.provider, error=snap.error)
            return None
        self.credentials_invalid.discard(snap.provider)
        prev = self.last_good.get(snap.provider)
        d = diff(prev, snap)
        self.last_good[snap.provider] = snap
        for a in self.approvals:
            if a.provider == snap.provider and a.model_id not in snap.rows:
                self._alert("approved_model_missing", provider=a.provider, model_id=a.model_id)
        for mid, delta in d.changed.items():
            if "fingerprint" in delta:
                self._alert("fingerprint_changed", provider=snap.provider, model_id=mid, change=list(delta["fingerprint"]))
        if self.store_dir is not None:
            self._persist(snap)
        return d

    def _persist(self, snap: CatalogSnapshot) -> None:
        d = self.store_dir / snap.provider
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.store_dir, 0o700)
        body = {"provider": snap.provider, "fetched_at_ns": snap.fetched_at_ns, "endpoint": snap.endpoint,
                "http_status": snap.http_status, "raw_sha256": snap.raw_sha256,
                "rows": {k: {**r.comparable(), "capabilities": {c: [v.value, v.provenance, v.verified_at] for c, v in r.capabilities.items()},
                             "provider_prices": dict(r.provider_prices), "provider_price_unit": r.provider_price_unit}
                         for k, r in snap.rows.items()}}
        (d / f"{snap.fetched_at_ns}.json").write_text(json.dumps(body, sort_keys=True, default=list))

    def _approval(self, provider: str, model_id: str, role: str) -> Approval | None:
        for a in self.approvals:
            if a.provider == provider and a.model_id == model_id and role in a.roles:
                return a
        return None

    def status(self, provider: str, model_id: str) -> str:
        if provider in self.credentials_invalid:
            return "credentials_invalid"
        snap = self.last_good.get(provider)
        approved = any(a.provider == provider and a.model_id == model_id for a in self.approvals)
        if snap is None:
            return "no_snapshot"
        if model_id not in snap.rows:
            return "missing" if approved else "absent"
        return "approved" if approved else "unreviewed"

    def routable(self, provider: str, model_id: str, role: str, *, required_caps: Iterable[str] = ()) -> tuple[bool, str]:
        if provider in self.credentials_invalid:
            return False, "credentials_invalid"
        snap = self.last_good.get(provider)
        if snap is None:
            return False, "no_successful_snapshot"
        if self.clock_ns() - snap.fetched_at_ns > self.max_age_ns:
            return False, "catalog_stale"
        row = snap.rows.get(model_id)
        approval = self._approval(provider, model_id, role)
        if row is None:
            return False, "missing_from_snapshot" if approval else "unknown_model"
        if approval is None:
            return False, "unreviewed" if self.status(provider, model_id) == "unreviewed" else "not_approved_for_role"
        if model_id.endswith("-latest") or any(model_id in r.aliases for r in snap.rows.values() if r.model_id != model_id):
            return False, "alias_not_pinnable"
        today = datetime.fromtimestamp(self.clock_ns() // 1_000_000_000, tz=timezone.utc).date()
        if row.lifecycle == "unknown":
            if approval.lifecycle_reviewed_until is None or today > approval.lifecycle_reviewed_until:
                return False, "lifecycle_unknown_without_owner_review"
        elif row.lifecycle != "active":
            return False, f"lifecycle_{row.lifecycle}"
        if row.retires_at and row.retires_at[:10] <= today.isoformat():
            return False, "past_retirement_date"
        for cap in required_caps:
            c = row.capabilities.get(cap)
            if c is None or not c.value:
                return False, f"capability_unverified:{cap}"
        return True, "ok"
