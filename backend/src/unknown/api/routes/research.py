"""Read-only research routes (I5; CAP-RES-10/-11/-12; DAT-05, DAT-06, TRM-03).

Mounted under ``/api/research``. Every response is ``{"available", "data",
"provenance"}``; provenance names the registry file, its schema version, the
read time, the formulas behind derived numbers and whether the content comes
from SYNTHETIC fixtures. When the research registry does not exist the route
answers ``available: false`` with the reason, never a placeholder.

The registry is opened with SQLite ``mode=ro`` per request; these handlers
cannot write research state, change a status or approve anything (promotion
transitions exist only in ``unknown.research.promotion`` and need the owner).

Endpoints::

    GET /api/research                              index and counts
    GET /api/research/formulas                     formula registry (formula id@version)
    GET /api/research/strategies                   registered strategy specs
    GET /api/research/strategies/<spec_hash>       one spec (hash re-verified)
    GET /api/research/experiments                  pre-registered experiments with trial counts
    GET /api/research/experiments/<id>             pre-registration, amendments, budget, holdout log
    GET /api/research/experiments/<id>/trials      every trial of the experiment, all statuses
    GET /api/research/trials/<trial_id>            one trial with metrics, returns and manifest
    GET /api/research/reports                      report index
    GET /api/research/reports/<report_id>          one report body
    GET /api/research/artifacts                    strategy artifacts with authoritative status
    GET /api/research/artifacts/<artifact_hash>    status history, evidence, chain check
"""
from __future__ import annotations

import time
from pathlib import Path

from ...research.common import FORMULAS, ResearchError
from ...research.promotion import PromotionService
from ...research.registry import SEGMENTS, ResearchRegistry

SOURCE = "research registry (SQLite, opened read-only)"


def registry_path(ctx) -> Path:
    override = getattr(ctx, "research_registry_path", None)
    if override:
        return Path(override)
    return Path(ctx.paths.root) / "research" / "experiments.sqlite"


def _prov(path: Path, reg: ResearchRegistry | None, **extra) -> dict:
    return {"source": SOURCE, "path": str(path), "schema_version": reg.schema_version() if reg else None,
            "read_at_ns": time.time_ns(), **extra}


def _fixture_label(fixture: bool | None) -> dict:
    """DAT-06/TRM-03: anything derived from a FIXTURE experiment is labelled as such in provenance."""
    if fixture is None:
        return {"fixture": None, "label": "UNKNOWN_ORIGIN"}
    return {"fixture": fixture, "label": "FIXTURE" if fixture else None}


def _experiment_fixture(reg: ResearchRegistry, experiment_id: str) -> bool:
    return bool(reg.experiment(experiment_id)["fixture"])


def _artifact_fixture(reg: ResearchRegistry, art: dict) -> bool | None:
    """An artifact inherits the label of the experiment whose trial produced its evaluation manifest."""
    row = reg.conn.execute("SELECT experiment_id FROM trial WHERE manifest_hash=? ORDER BY started_at_ns LIMIT 1",
                           (art["body"]["evaluation_manifest_hash"],)).fetchone()
    return _experiment_fixture(reg, row["experiment_id"]) if row else None


def handle(ctx, parts: list[str], q: dict, account_key: str, conn) -> dict:
    path = registry_path(ctx)
    rest = parts[1:]
    if rest == ["formulas"]:
        return {"available": True, "data": [{"formula_id": k, **v} for k, v in sorted(FORMULAS.items())],
                "provenance": {"source": "unknown.research.common.FORMULAS (code)", "read_at_ns": time.time_ns()}}
    try:
        reg = ResearchRegistry.open_readonly(path)
    except FileNotFoundError:
        return {"available": False, "data": None,
                "unavailable_reason": f"research registry not found at {path}; record an experiment or run "
                                      "`python -m unknown.research.study` first",
                "provenance": _prov(path, None)}
    try:
        return _route(reg, path, rest, q)
    except ResearchError as exc:
        raise LookupError(str(exc)) from None
    finally:
        reg.close()


def _route(reg: ResearchRegistry, path: Path, rest: list[str], q: dict) -> dict:
    promo = PromotionService(reg)
    if not rest:
        counts = {t: reg.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("strategy_spec", "experiment", "trial", "report", "artifact", "evidence", "transition")}
        return {"available": True, "data": {"counts": counts, "endpoints": [
            "/api/research/formulas", "/api/research/strategies", "/api/research/experiments", "/api/research/reports",
            "/api/research/artifacts"]}, "provenance": _prov(path, reg)}
    head = rest[0]
    if head == "strategies":
        if len(rest) == 1:
            rows = [dict(r) for r in reg.conn.execute(
                "SELECT spec_hash, strategy_id, version, family, registered_at_ns, registered_by, registered_role "
                "FROM strategy_spec ORDER BY registered_at_ns, spec_hash")]
            return {"available": True, "data": rows, "provenance": _prov(path, reg)}
        if len(rest) == 2:
            spec = reg.spec(rest[1])  # re-parses and re-verifies the hash
            fixture = any(d.get("fixture_allowed") for d in spec.body["data_requirements"])
            return {"available": True, "data": {"spec_hash": spec.spec_hash, "body": spec.body, "hash_verified": True},
                    "provenance": _prov(path, reg, fixture_data_allowed=fixture,
                                        formulas=sorted({e["formula_id"] for e in spec.body["equations"]}))}
    if head == "experiments":
        if len(rest) == 1:
            rows = []
            for e in reg.experiments():
                e["trial_counts"] = {s: reg.trial_count(e["experiment_id"], s) for s in SEGMENTS}
                e["budget_current"] = reg.budget(e["experiment_id"])
                e["holdout_consumed"] = any(a["outcome"] == "GRANTED" for a in reg.holdout_log(e["experiment_id"]))
                e["fixture"] = bool(e["fixture"])
                rows.append(e)
            return {"available": True, "data": rows, "provenance": _prov(path, reg, note="trial counts include every status")}
        exp_id = rest[1]
        if len(rest) == 2:
            e = reg.experiment(exp_id)  # verifies the pre-registration hash
            data = {"experiment_id": exp_id, "prereg": e["prereg"], "prereg_hash": e["prereg_hash"], "prereg_hash_verified": True,
                    "amendments": reg.amendments(exp_id), "search_space": reg.search_space(exp_id), "budget": reg.budget(exp_id),
                    "trial_counts": {s: reg.trial_count(exp_id, s) for s in SEGMENTS},
                    "budget_consumption": reg.budget_consumption(exp_id), "holdout_log": reg.holdout_log(exp_id)}
            return {"available": True, "data": data, "provenance": _prov(path, reg, **_fixture_label(bool(e["fixture"])))}
        if len(rest) == 3 and rest[2] == "trials":
            e = reg.experiment(exp_id)
            segment = q.get("segment")
            if segment and segment not in SEGMENTS:
                raise ValueError(f"unknown segment {segment!r}; expected one of {list(SEGMENTS)}")
            return {"available": True, "data": reg.trials(exp_id, segment),
                    "provenance": _prov(path, reg, note="returns omitted; GET /api/research/trials/<id> for one trial",
                                        **_fixture_label(bool(e["fixture"])))}
    if head == "trials" and len(rest) == 2:
        t = reg.trial(rest[1])
        t["manifest"] = reg.manifest(t["manifest_hash"])
        return {"available": True, "data": t,
                "provenance": _prov(path, reg, formulas=["return.simple@1", "sharpe.per_period@1", "nav@1"],
                                    units={"returns": "simple return per session (float statistic)"},
                                    **_fixture_label(_experiment_fixture(reg, t["experiment_id"])))}
    if head == "reports":
        if len(rest) == 1:
            return {"available": True, "data": reg.reports(), "provenance": _prov(path, reg)}
        if len(rest) == 2:
            r = reg.report(rest[1])
            return {"available": True, "data": r, "provenance": _prov(path, reg, fixture=bool(r["fixture"]),
                                                                      label="FIXTURE" if r["fixture"] else None)}
    if head == "artifacts":
        if len(rest) == 1:
            return {"available": True, "data": promo.artifacts(),
                    "provenance": _prov(path, reg, note="status = last record of the append-only transition log")}
        if len(rest) == 2:
            a = promo.artifact(rest[1])
            data = {**a, "history": promo.history(rest[1]), "evidence": promo.evidence(rest[1]),
                    "chain_problems": promo.verify_chain()}
            return {"available": True, "data": data, "provenance": _prov(path, reg, **_fixture_label(_artifact_fixture(reg, a)))}
    raise LookupError("unknown research endpoint")


ROUTES = [(("research",), handle)]
