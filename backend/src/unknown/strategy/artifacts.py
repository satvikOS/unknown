"""Strategy artifact identity and run eligibility (ADR-0017 decisions 1 and 2).

Identity
--------
There is one artifact hash in the system: the one ``research.promotion.PromotionService.register_artifact``
computes, ``stable_hash`` (SHA-256 over canonical JSON) of the bundle

    {spec_hash, strategy_id, parameters, features, code_hash, dependency_lock, schema}

where ``spec_hash`` covers the whole strategy specification, including its ``data_requirements`` (dataset ids).
This module does not define a second scheme. The ADR-0017 artifact id, which mandates list and which strategy
tokens and principals carry, is derived from that hash:

    <strategy_id>@<first 16 hex of the artifact hash>       e.g. TREND_DAILY_V1@0123456789abcdef

The class is the spec's ``strategy_id`` (the value the registry stores), so the id matches
``domain.principal.ARTIFACT_ID``.

Eligibility
-----------
A runner loads the artifact from the research registry (read-only) on every cycle and refuses to run unless:

* the stored bundle still hashes to the artifact hash and names the stored spec (tamper evidence);
* the registry status permits the requested mode (``MODE_MIN_STATUS`` in the ADR-0017 order
  proposed < reproducible < validated < shadow < paper < eligible < active; quarantined and retired never run);
* the artifact has not expired (``expires_on``);
* the code that would run is the code that was registered: the bundle's ``code_hash`` equals
  ``research.manifest.code_hash()`` of this checkout (research/, domain/, data/pit.py);
* an implementation exists for the spec, the spec is schema v2 (it declares the submission window, the risk budget
  and the feature validity windows the runtime applies) and it reads exactly one daily point-in-time dataset.

``shadow`` is a status of the research registry (``research.promotion``): the owner approves it, or the SYSTEM promotes
a validated artifact inside an owner-approved envelope (``PromotionService.auto_shadow``). Shadow mode needs ``shadow``
or later; live mode needs ``paper`` or later."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..domain.principal import ARTIFACT_ID
from ..research import manifest as research_manifest
from ..research.common import RegistryError, stable_hash
from ..research.promotion import LADDER, PromotionService
from ..research.registry import ResearchRegistry
from ..research.spec import StrategySpec
from ..research.strategies import STRATEGIES

# ADR-0017 #2 order, the research registry's own ladder (``research.promotion.LADDER``).
STATUS_ORDER = LADDER
NEVER_RUN = frozenset({"quarantined", "retired"})
MODES = ("shadow", "live")
MODE_MIN_STATUS = {"shadow": "shadow", "live": "paper"}


class ArtifactRefused(Exception):
    """The runner may not run this artifact in this mode. ``code`` is stable for tests and logs."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def artifact_id(strategy_id: str, artifact_hash: str) -> str:
    """The ADR-0017 id of a registered artifact hash (64 lowercase hex)."""
    if len(artifact_hash) != 64 or any(c not in "0123456789abcdef" for c in artifact_hash):
        raise ValueError("artifact hash must be 64 lowercase hex characters")
    aid = f"{strategy_id}@{artifact_hash[:16]}"
    if not ARTIFACT_ID.match(aid):
        raise ValueError(f"{aid!r} is not a valid ADR-0017 artifact id")
    return aid


def split_id(aid: str) -> tuple[str, str]:
    if not isinstance(aid, str) or not ARTIFACT_ID.match(aid):
        raise ArtifactRefused("INVALID_ARTIFACT_ID", f"{aid!r} is not <CLASS>@<16 lowercase hex> (ADR-0017)")
    cls, prefix = aid.split("@")
    return cls, prefix


def status_permits(status: str, mode: str) -> bool:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if status in NEVER_RUN or status not in STATUS_ORDER:
        return False
    return STATUS_ORDER.index(status) >= STATUS_ORDER.index(MODE_MIN_STATUS[mode])


def current_code_hash() -> str:
    """The registry's code-hash convention for this checkout (``research.manifest.code_hash``)."""
    return research_manifest.code_hash()["sha256"]


def runtime_code_hash() -> str:
    """SHA-256 over this runtime package's own sources (recorded in every decision's inputs digest; the
    registry's artifact hash does not cover the runner, see docs/strategy/README.md)."""
    import hashlib
    root = Path(__file__).resolve().parent
    files = [(p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(root.glob("*.py"))]
    return stable_hash(files)


@dataclass(frozen=True)
class LoadedArtifact:
    artifact_id: str
    artifact_hash: str
    status: str
    spec: StrategySpec
    bundle: dict
    expires_on: date
    dataset_id: str
    fixture_allowed: bool

    @property
    def strategy_class(self) -> str:
        return self.spec.strategy_id


def resolve(registry: ResearchRegistry, aid: str) -> str:
    """The registered artifact hash behind an ADR-0017 id (exactly one, else refused)."""
    cls, prefix = split_id(aid)
    rows = registry.conn.execute("SELECT artifact_hash FROM artifact WHERE strategy_id=? AND substr(artifact_hash, 1, 16)=?",
                                 (cls, prefix)).fetchall()
    if not rows:
        raise ArtifactRefused("ARTIFACT_UNKNOWN", f"{aid} is not in the research registry")
    if len(rows) > 1:   # a 64-bit prefix collision: the id would not name one thing, so nothing runs
        raise ArtifactRefused("ARTIFACT_AMBIGUOUS", f"{aid} matches {len(rows)} registered artifacts")
    return rows[0]["artifact_hash"]


def load(registry: ResearchRegistry, aid: str, *, mode: str, today: date, code_hash: str | None = None) -> LoadedArtifact:
    """Load and check one artifact for a run in ``mode`` (raises ``ArtifactRefused``)."""
    if mode not in MODES:
        raise ArtifactRefused("INVALID_MODE", f"mode must be one of {MODES}")
    h = resolve(registry, aid)
    promo = PromotionService(registry)
    try:
        art = promo.artifact(h)
        spec = registry.spec(art["spec_hash"])
    except RegistryError as exc:
        raise ArtifactRefused("ARTIFACT_INVALID", str(exc)) from None
    bundle = art["body"]["bundle"]
    if stable_hash(bundle) != h or bundle.get("spec_hash") != art["spec_hash"] or bundle.get("strategy_id") != spec.strategy_id:
        raise ArtifactRefused("ARTIFACT_TAMPERED", "stored bundle does not hash to the artifact id or names another spec")
    if bundle.get("parameters") != {k: v.to_str() for k, v in sorted(spec.parameters.items())}:
        raise ArtifactRefused("ARTIFACT_TAMPERED", "bundle parameters differ from the registered spec")
    status = art["status"]
    if not status_permits(status, mode):
        need = MODE_MIN_STATUS[mode]
        raise ArtifactRefused("STATUS_NOT_PERMITTED",
                              f"{aid} is '{status}'; {mode} mode needs '{need}' or later in {' < '.join(STATUS_ORDER)}"
                              + ("" if status not in NEVER_RUN else " (quarantined and retired artifacts never run)"))
    expires = date.fromisoformat(art["body"]["expires_on"])
    if expires < today:
        raise ArtifactRefused("ARTIFACT_EXPIRED", f"{aid} expired on {expires.isoformat()}")
    running = code_hash if code_hash is not None else current_code_hash()
    if bundle.get("code_hash") != running:
        raise ArtifactRefused("CODE_DRIFT", f"registered code hash {str(bundle.get('code_hash'))[:16]}... differs from this "
                                            f"checkout's {running[:16]}...: the code that would run is not the code that was evaluated")
    if spec.strategy_id not in STRATEGIES:
        raise ArtifactRefused("NO_IMPLEMENTATION", f"no runtime implementation for {spec.strategy_id}")
    if spec.schema_version < 2:
        # A v1 spec declares no submission window (BR-DAT-19), no risk budget (BR-DAT-04) and no feature validity
        # windows (BR-DAT-18): the runtime would have to invent its execution, sizing and feature rules.
        raise ArtifactRefused("SPEC_SCHEMA_V1", f"{aid} is a schema v1 spec: the runner needs v2 (submission window, "
                                                "risk budget, feature validity windows); register a v2 spec")
    reqs = spec.body["data_requirements"]
    daily = [d for d in reqs if d["frequency"] == "DAILY"]
    if len(reqs) != 1 or len(daily) != 1:
        raise ArtifactRefused("DATA_REQUIREMENTS", "the runner reads exactly one DAILY point-in-time bar dataset")
    return LoadedArtifact(artifact_id=aid, artifact_hash=h, status=status, spec=spec, bundle=bundle, expires_on=expires,
                          dataset_id=daily[0]["dataset_id"], fixture_allowed=bool(daily[0]["fixture_allowed"]))
