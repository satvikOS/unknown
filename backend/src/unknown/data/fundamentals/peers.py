"""Peer groups by SIC and trading comparables (CAP-CO-10).

Default rule ``peers_sic4@1`` (deterministic, displayed with the result): candidates are issuers in
the research store whose SEC SIC code (from their submissions payload) equals the target's 4-digit
SIC, ordered by CIK. When market caps are supplied for the target and peers, the size band
0.25x-4x of the target is applied; without prices the band is reported as not applied. The
liquidity filter needs traded-value data the research store does not hold, so it is reported as
not applied. An explicit owner list replaces the rule (``owner_list@1``) and is echoed back.

Point-in-time caveats (always returned): SIC is the current classification from the submissions
snapshot (the feed carries no SIC history), applied to every as-of time; membership can only include
issuers that were ingested into this store (survivorship: deregistered issuers appear only if they
were ingested); the snapshot may postdate the as-of time.

Comps are computed through ``MetricEngine`` as of the same decision time for every member
(fundamentals with availability <= as-of; prices only if supplied with source and time). NM values
are excluded from statistics and counted; statistics are suppressed below three valid peers.
Percentiles are inclusive linear interpolation (PERCENTILE.INC), computed exactly in decimal.
"""
from __future__ import annotations

import json

from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed, Rounding
from .metrics import RATIO_SCALE, MetricEngine, PriceInput
from .statements import Fundamentals, cik_of

RULE = "peers_sic4@1"
DEFAULT_COMPS = ("revenue_ttm", "gross_margin", "operating_margin", "net_margin", "revenue_growth_yoy", "roe", "roic",
                 "debt_to_equity", "net_debt_to_ebitda", "fcf_margin")
PRICE_COMPS = ("market_cap", "pe_ttm", "price_to_sales", "ev_to_ebitda", "ev_to_sales", "fcf_yield")
MIN_PEERS = 3


def issuers(store) -> list[dict]:
    out = []
    for r in store.conn.execute("SELECT entity_id, name, attributes FROM entity WHERE kind='issuer' ORDER BY entity_id"):
        attrs = json.loads(r["attributes"] or "{}")
        out.append({"entity_id": r["entity_id"], "name": r["name"], "cik": attrs.get("cik") or cik_of(r["entity_id"]),
                    "sic": str(attrs["sic"]) if attrs.get("sic") else None, "submissions_ref": attrs.get("submissions_ref")})
    return out


def _snapshot_time(store, sha: str | None) -> int | None:
    if not sha:
        return None
    r = store.conn.execute("SELECT receipt_time_ns FROM raw_object WHERE sha256=?", (sha,)).fetchone()
    return r["receipt_time_ns"] if r else None


def peer_group(store, entity_id: str, as_of_ns: int, *, explicit: list[str] | None = None,
               market_caps: dict[str, Fixed] | None = None, band: tuple[str, str] = ("0.25", "4")) -> dict:
    universe = {i["entity_id"]: i for i in issuers(store)}
    target = universe.get(entity_id)
    if target is None:
        raise LookupError(f"{entity_id} is not an ingested issuer")
    caveats = ["SIC is the current classification from the SEC submissions snapshot; no SIC history is available, so it is "
               "applied to every as-of time",
               f"candidates are limited to the {len(universe)} issuers ingested into this research store (survivorship: "
               "deregistered issuers appear only if ingested)",
               "liquidity filter not applied: no traded-value data in the research store"]
    snap = _snapshot_time(store, target.get("submissions_ref"))
    if snap is not None and snap > as_of_ns:
        caveats.append(f"classification snapshot retrieved {ns_to_iso(snap)}, after the as-of time")
    if explicit is not None:
        unknown = [e for e in explicit if e not in universe]
        members = [universe[e] for e in explicit if e in universe and e != entity_id]
        rule = {"id": "owner_list@1", "description": "explicit owner-supplied peer list", "requested": explicit,
                "not_ingested": unknown}
    else:
        members = [i for i in universe.values() if i["entity_id"] != entity_id and target["sic"] and i["sic"] == target["sic"]]
        rule = {"id": RULE, "description": "same 4-digit SEC SIC code as the target, ordered by CIK; size band 0.25x-4x "
                "of target market cap when prices are supplied", "sic": target["sic"]}
        if not target["sic"]:
            caveats.append("target has no SIC code on record: no default peers")
    excluded = []
    if market_caps and entity_id in market_caps and explicit is None:
        lo, hi = Fixed.parse(band[0]), Fixed.parse(band[1])
        t_mc = market_caps[entity_id]
        kept = []
        for m in members:
            mc = market_caps.get(m["entity_id"])
            if mc is None:
                excluded.append({"entity_id": m["entity_id"], "reason": "no market cap (no price supplied)"})
                continue
            ratio = mc.div(t_mc, RATIO_SCALE, Rounding.HALF_EVEN)
            if lo <= ratio <= hi:
                kept.append(m)
            else:
                excluded.append({"entity_id": m["entity_id"], "reason": f"size ratio {ratio} outside [{band[0]}, {band[1]}]"})
        members = kept
        rule["size_band"] = "applied"
    else:
        rule["size_band"] = "not applied (market caps for the target and peers were not supplied)"
    members.sort(key=lambda m: (m["cik"] or 0, m["entity_id"]))
    return {"target": {k: target[k] for k in ("entity_id", "name", "cik", "sic")}, "as_of": ns_to_iso(as_of_ns), "rule": rule,
            "members": [{k: m[k] for k in ("entity_id", "name", "cik", "sic")} for m in members], "excluded": excluded,
            "caveats": caveats}


def percentile_inc(sorted_values: list[Fixed], k: int) -> Fixed:
    """PERCENTILE.INC at k/4 (k in 1..3), exact: h = (n-1)k/4, v[lo] + (v[lo+1]-v[lo]) x frac."""
    n = len(sorted_values)
    num = (n - 1) * k
    lo, rem = divmod(num, 4)
    if rem == 0 or lo + 1 >= n:
        return sorted_values[lo]
    frac = Fixed(rem * 25, 2)
    return sorted_values[lo] + (sorted_values[lo + 1] - sorted_values[lo]) * frac


def statistics(values: list[Fixed], n_nm: int, n_missing: int) -> dict:
    v = sorted(values)
    out = {"n_used": len(v), "n_nm": n_nm, "n_missing": n_missing}
    if len(v) < MIN_PEERS:
        out.update({"suppressed": True, "reason": f"fewer than {MIN_PEERS} valid peers"})
        return out
    total = v[0]
    for x in v[1:]:
        total = total + x
    out.update({"suppressed": False, "mean": total.div(len(v), RATIO_SCALE, Rounding.HALF_EVEN).to_str(),
                "median": percentile_inc(v, 2).to_str(), "p25": percentile_inc(v, 1).to_str(), "p75": percentile_inc(v, 3).to_str(),
                "method": "PERCENTILE.INC (inclusive linear interpolation), exact decimal"})
    return out


def comps(store, entity_id: str, group: dict, as_of_ns: int, *, prices: dict[str, PriceInput] | None = None,
          metrics: tuple[str, ...] | None = None) -> dict:
    prices = prices or {}
    names = tuple(metrics or (DEFAULT_COMPS + (PRICE_COMPS if prices else ())))
    rows = []
    ids = [entity_id] + [m["entity_id"] for m in group["members"]]
    sics = {group["target"]["entity_id"]: group["target"]["sic"], **{m["entity_id"]: m["sic"] for m in group["members"]}}
    for eid in ids:
        f = Fundamentals(store, eid, as_of_ns)
        eng = MetricEngine(f, prices.get(eid), sic=sics.get(eid))
        vals = {}
        for n in names:
            r = eng.compute(n)
            # lineage per value (CAP-CO-07/10): formula id@version, input observation ids and their availability times
            vals[n] = {"value": r.value.to_str() if r.value is not None else None, "status": r.status, "unit": r.unit,
                       "formula": r.formula.ref, "period": r.period, "reason": r.reason, "flags": list(r.flags),
                       "input_obs_ids": r.obs_ids(), "source_dates": r.source_dates()}
        # CAP-CO-10 degraded behaviour: a member without fundamentals at the as-of time (e.g. not yet registered
        # then; membership uses today's SIC) is shown with a reason and contributes nothing to the statistics.
        fundamentals = ({"status": "OK", "facts_at_as_of": f.fact_count} if f.fact_count else
                        {"status": "NO_FUNDAMENTALS_AT_AS_OF", "facts_at_as_of": 0,
                         "reason": "no SEC companyfacts value available at the as-of time; excluded from statistics"})
        rows.append({"entity_id": eid, "role": "target" if eid == entity_id else "peer", "values": vals,
                     "fundamentals": fundamentals, "price": prices[eid].to_json() if eid in prices else None})
    stats = {}
    for n in names:
        used, nm, missing = [], 0, 0
        for row in rows:
            if row["role"] != "peer":
                continue
            v = row["values"][n]
            if v["status"] == "OK" and v["value"] is not None:
                used.append(Fixed.parse(v["value"]))
            elif v["status"] == "NM":
                nm += 1
            else:
                missing += 1
        stats[n] = statistics(used, nm, missing)
    return {"as_of": ns_to_iso(as_of_ns), "metrics": list(names), "rows": rows, "peer_statistics": stats,
            "note": "Statistics use peers only (target excluded); NM, INCOMPLETE and missing values are excluded and counted. "
                    "Forward multiples are absent: no licensed estimates feed (CAP-CO-08)."}
