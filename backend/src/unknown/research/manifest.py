"""Reproducibility manifests and replay comparison (CAP-RES-11; book ch. 7).

A manifest is written *before* a run starts (the intent) and the trial row
records the result hash after it finishes. It holds:

* git commit and whether the research/domain sources differ from it (dirty);
* a code hash: SHA-256 over every source file the engine depends on
  (``research/``, ``domain/``, ``data/pit.py``), path-sorted;
* dataset fingerprints (content hashes of the exact bar versions the run may
  read) and, for SYNTHETIC fixtures, the regeneration recipe;
* the configuration hash (cost model, execution model, sizing, calendar,
  clock), the strategy spec hash and parameters;
* seeds, simulator version, Python implementation and version.

``manifest_hash`` = SHA-256 over the canonical manifest without that field.
``compare_replay`` re-runs and reports ``replay_exact`` (event-log SHA-256
equality) plus a list of manifest fields that differ.

Evidence retention (BR-DAT-16). Each dataset entry states ``retention_until``:
the last UTC day a result resting on the data may be used, i.e. the day before
the earliest instant at which the licence requires the observations the run
read to be deleted (``retention_date``), or ``"unbounded"`` when the dataset
has no retention. ``research.bars.pit_manifest_entry`` derives it from the
point-in-time store and the provider registry, with the ``scope`` (entities and
availability cutoff) the run read, which the research registry later matches
against the store's deletion events. Synthetic fixtures are ``"unbounded"``. An
entry without the field (written before this rule) is reported as unrecorded.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import stable_hash

PACKAGE_ROOT = Path(__file__).resolve().parents[1]          # backend/src/unknown
REPO_ROOT = Path(__file__).resolve().parents[4]
CODE_PATHS = ("research", "domain", "data/pit.py")
UNBOUNDED = "unbounded"                                     # dataset entry retention: the data has no deletion deadline


def retention_date(deadline_ns: int) -> date:
    """The last UTC day on which a result resting on data that must be deleted at ``deadline_ns`` may still be used:
    the day before the deadline's UTC date. Day-granular expiry (``expires_on < today`` is expired) then never
    reaches the deadline instant."""
    if type(deadline_ns) is not int or deadline_ns < 0:
        raise ValueError("a retention deadline is a non-negative integer of nanoseconds since the epoch")
    return datetime.fromtimestamp(deadline_ns // 1_000_000_000, tz=timezone.utc).date() - timedelta(days=1)


def check_retention(name: str, entry: Any) -> None:
    """A dataset entry's ``retention_until`` is an ISO date or ``"unbounded"`` (absent or null: not recorded)."""
    value = entry.get("retention_until") if isinstance(entry, dict) else None
    if value is None or value == UNBOUNDED:
        return
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"dataset {name}: retention_until {value!r} is neither an ISO date nor {UNBOUNDED!r}") from None


def code_hash(root: Path = PACKAGE_ROOT) -> dict:
    files = []
    for rel in CODE_PATHS:
        p = root / rel
        paths = [p] if p.is_file() else sorted(p.rglob("*.py"))
        for f in paths:
            files.append((f.relative_to(root).as_posix(), hashlib.sha256(f.read_bytes()).hexdigest()))
    files.sort()
    return {"sha256": stable_hash(files), "files": len(files), "scope": list(CODE_PATHS)}


_GIT_CACHE: dict[str, dict] = {}


def git_state(repo: Path = REPO_ROOT, *, refresh: bool = False) -> dict:
    """Commit and dirty flag via local git (no network), captured once per process unless ``refresh``.
    Unavailable is reported, never guessed."""
    key = str(repo)
    if key in _GIT_CACHE and not refresh:
        return dict(_GIT_CACHE[key])
    _GIT_CACHE[key] = _git_state(repo)
    return dict(_GIT_CACHE[key])


def _git_state(repo: Path) -> dict:
    try:
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=10, check=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", "backend/src/unknown/research",
                                 "backend/src/unknown/domain", "backend/src/unknown/data/pit.py"],
                                capture_output=True, text=True, timeout=10, check=True).stdout
        return {"commit": commit, "dirty": bool(status.strip()), "dirty_scope": "research, domain, data/pit.py"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"commit": None, "dirty": None, "unavailable": f"git not available: {type(exc).__name__}"}


def build_manifest(*, run_kind: str, strategy_id: str, spec_hash: str, parameters: dict[str, str], config: dict,
                   datasets: dict[str, dict], seeds: dict[str, int], extra: dict[str, Any] | None = None,
                   include_environment: bool = True) -> dict:
    for name, entry in datasets.items():
        check_retention(name, entry)
    m: dict[str, Any] = {
        "manifest_version": 1, "run_kind": run_kind, "strategy_id": strategy_id, "spec_hash": spec_hash,
        "parameters": dict(sorted(parameters.items())), "config_hash": stable_hash(config), "config": config,
        "datasets": datasets, "seeds": seeds, "simulator_version": config.get("simulator_version"),
        "code": code_hash(), "extra": extra or {},
    }
    if include_environment:
        m["git"] = git_state()
        m["python"] = {"implementation": platform.python_implementation(), "version": platform.python_version()}
    m["manifest_hash"] = stable_hash(m)
    return m


def verify_manifest(m: dict) -> bool:
    body = {k: v for k, v in m.items() if k != "manifest_hash"}
    return stable_hash(body) == m.get("manifest_hash")


REPLAY_RELEVANT = ("spec_hash", "parameters", "config_hash", "datasets", "seeds", "simulator_version", "code")
# The runtime is the only dependency (standard library only): CAP-RES-11 asks the replay diff to name a changed
# dependency version, so the interpreter and the commit are compared too when both manifests record them.
ENVIRONMENT = (("python", "implementation"), ("python", "version"), ("git", "commit"), ("git", "dirty"))


def diff_manifests(a: dict, b: dict) -> list[str]:
    """Names of replay-relevant fields that differ (dataset entries are compared per name)."""
    out = []
    for k in REPLAY_RELEVANT:
        if k == "datasets":
            for name in sorted(set(a.get(k, {})) | set(b.get(k, {}))):
                if a.get(k, {}).get(name) != b.get(k, {}).get(name):
                    out.append(f"datasets.{name}")
        elif k == "code":
            if (a.get(k) or {}).get("sha256") != (b.get(k) or {}).get("sha256"):
                out.append("code.sha256")
        elif a.get(k) != b.get(k):
            out.append(k)
    for sect, key in ENVIRONMENT:
        if sect in a and sect in b and (a[sect] or {}).get(key) != (b[sect] or {}).get(key):
            out.append(f"{sect}.{key}")
    return out


def compare_replay(original_manifest: dict, original_event_hash: str, replay_manifest: dict, replay_event_hash: str) -> dict:
    diffs = diff_manifests(original_manifest, replay_manifest)
    return {"replay_exact": int(original_event_hash == replay_event_hash), "event_log_hash": original_event_hash,
            "replay_event_log_hash": replay_event_hash, "manifest_differences": diffs,
            "formula": "replay_exact = 1 iff SHA-256(event log of replay) == stored event-log hash (CAP-RES-11)"}
