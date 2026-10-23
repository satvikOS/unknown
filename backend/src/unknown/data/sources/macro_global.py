"""Cross-country macro comparison and series over the point-in-time store (CAP-MAC-05, CAP-MAC-06, CAP-MAC-09).

Read-only: every function takes a store (read-only in the API) and an as-of time and returns only
observations available at that time (``ResearchStore.as_of``, current knowledge). A column that was
never ingested, an area a source does not cover and a value not yet available at the as-of time
are three different explicit reasons; none is shown as zero.

* ``comparison`` — one row per venue country, one cell per column (latest period available at the
  as-of time), each cell with its period, availability time, observation id, flags, source and
  attribution. Euro-area countries take the euro-area (``XM``) policy rate, labelled as such.
* ``series``     — the points of one column for chosen countries, oldest first.
* ``blocked_sources`` — providers the registry holds with no permitted use (e.g. IMF, RBI website),
  with the recorded reason, so the terminal can say why a source is absent.
"""
from __future__ import annotations

import json

from ...domain.clock import ns_to_iso
from ..pit import OPEN_END
from ..registry import load_registry
from . import fred
from . import macro_common as mc

INGEST = {"worldbank.wdi": "python -m unknown.data.sources.worldbank ingest --from 2015",
          "oecd.sdmx_macro": "python -m unknown.data.sources.oecd ingest --from 2024-01",
          "bis.cbpol": "python -m unknown.data.sources.bis ingest --from 2015-01",
          "fred.series": "python -m unknown.data.sources.fred ingest --from 2015-01-01"}


def _areas_for(col: mc.Column, country: mc.Country) -> str | None:
    if col.areas is None:
        return country.code
    if country.code in col.areas:
        return country.code
    if country.euro_area and mc.EURO_AREA in col.areas:
        return mc.EURO_AREA
    return None


def _point(r) -> dict:
    return {"period_start": r["period_start"], "period_end": r["period_end"], "period_kind": r["period_kind"],
            "value": r["value"], "missing_reason": r["missing_reason"], "unit": r["unit"], "obs_id": r["obs_id"],
            "available_time": ns_to_iso(r["available_time_ns"]), "flags": json.loads(r["quality"] or "[]"),
            "source_ref": r["source_ref"]}


def _held(store) -> dict[str, dict]:
    out = {}
    for r in store.conn.execute("SELECT entity_id, name, attributes FROM entity WHERE kind='macro_series'"):
        out[r["entity_id"]] = {"name": r["name"], **json.loads(r["attributes"] or "{}")}
    return out


def columns_held(store) -> list[mc.Column]:
    cols = list(mc.COLUMNS)
    fred_ids = sorted({a["series_code"] for a in _held(store).values() if a.get("dataset") == fred.DATASET})
    return cols + [mc.fred_column(s) for s in fred_ids]


def cell(store, col: mc.Column, country: mc.Country, as_of_ns: int, held: dict) -> dict:
    area = _areas_for(col, country)
    base = {"column_id": col.column_id, "dataset": col.dataset, "provider": col.provider}
    if area is None:
        return {**base, "status": "NOT_COVERED", "value": None,
                "reason": f"{col.label} does not cover {country.name}"}
    eid = mc.entity(col.column_id, area)
    if eid not in held:
        return {**base, "status": "NOT_INGESTED", "value": None, "area": area, "reason": f"{col.dataset} not ingested for {area}",
                "command": INGEST.get(col.dataset)}
    rows = [r for r in store.as_of(eid, "value", as_of_ns, dataset=col.dataset) if r["value"] is not None]
    if not rows:
        return {**base, "status": "NOT_AVAILABLE_AT_AS_OF", "value": None, "area": area, "entity_id": eid,
                "reason": "no value of this series available at the as-of time (published later or not yet compiled)"}
    r = rows[-1]
    out = {**base, "status": "OK", "area": area, "entity_id": eid, **_point(r)}
    if area != country.code:
        out["flags"] = out["flags"] + [f"area_rate:{area}"]
        out["note"] = f"{mc.NAMES.get(area, area)} rate (applies to {country.name})"
    return out


def comparison(store, as_of_ns: int, *, country_codes: list[str] | None = None) -> dict:
    cs = mc.countries()
    if country_codes:
        unknown = sorted(set(country_codes) - {c.code for c in cs})
        if unknown:
            raise ValueError(f"unknown countries {unknown}; one of {', '.join(c.code for c in cs)}")
        cs = [c for c in cs if c.code in country_codes]
    held = _held(store)
    cols = columns_held(store)
    rows = [{"country": c.to_json(), "cells": {col.column_id: cell(store, col, c, as_of_ns, held) for col in cols}} for c in cs]
    status = {d: _dataset_status(store, d, as_of_ns) for d in sorted({c.dataset for c in cols})}
    return {"as_of": ns_to_iso(as_of_ns), "columns": [c.to_json() for c in cols], "rows": rows, "datasets": status,
            "fred": fred.status(), "blocked_sources": blocked_sources(),
            "note": "Each column is one source and one definition; figures from different sources are never merged. "
                    "Values without upstream vintages are available from the time this system first captured them."}


def _dataset_status(store, ds: str, as_of_ns: int) -> dict:
    r = store.conn.execute("SELECT COUNT(*) AS n, MIN(period_end) AS first, MAX(period_end) AS last, MAX(available_time_ns) AS newest "
                           "FROM observation WHERE dataset=? AND available_time_ns<=? AND known_to_ns=?",
                           (ds, as_of_ns, OPEN_END)).fetchone()
    return {"dataset": ds, "observations": r["n"], "first_period_end": r["first"], "last_period_end": r["last"],
            "newest_available_time": ns_to_iso(r["newest"]) if r["newest"] is not None else None,
            "status": "OK" if r["n"] else "NOT_AVAILABLE_AT_AS_OF", "command": INGEST.get(ds)}


def series(store, column_id: str, as_of_ns: int, *, country_codes: list[str] | None = None, start: str | None = None) -> dict:
    col = mc.column(column_id)
    cs = mc.countries()
    if country_codes:
        cs = [c for c in cs if c.code in country_codes]
        if not cs:
            raise ValueError("no known country requested")
    held = _held(store)
    out, seen = [], set()
    for c in cs:
        area = _areas_for(col, c)
        if area is None:
            out.append({"country": c.code, "status": "NOT_COVERED", "reason": f"{col.label} does not cover {c.name}", "points": []})
            continue
        if area in seen:
            out.append({"country": c.code, "status": "SAME_AS", "area": area, "reason": f"uses the {mc.NAMES.get(area, area)} series",
                        "points": []})
            continue
        seen.add(area)
        eid = mc.entity(column_id, area)
        if eid not in held:
            out.append({"country": c.code, "area": area, "status": "NOT_INGESTED", "reason": f"{col.dataset} not ingested for {area}",
                        "command": INGEST.get(col.dataset), "points": []})
            continue
        pts = [_point(r) for r in store.as_of(eid, "value", as_of_ns, dataset=col.dataset) if not start or (r["period_end"] or "") >= start]
        out.append({"country": c.code, "area": area, "entity_id": eid, "title": held[eid].get("title"),
                    "status": "OK" if pts else "NOT_AVAILABLE_AT_AS_OF", "points": pts,
                    **({} if pts else {"reason": "no value of this series available at the as-of time"})})
    return {"as_of": ns_to_iso(as_of_ns), "column": col.to_json(), "series": out}


def blocked_sources() -> list[dict]:
    out = []
    for p in load_registry().values():
        for d in p.datasets:
            if not d.allowed_uses:
                out.append({"provider_id": p.provider_id, "name": p.name, "dataset": d.dataset_id, "reason": d.rights_source,
                            "verified_on": d.rights_verified_on})
    return sorted(out, key=lambda x: x["provider_id"])
