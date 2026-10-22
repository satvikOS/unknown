"""US Treasury daily par yield curve (CAP-DATA-06, CAP-MAC-07; registry ``us-treasury``).

Source: the Treasury daily interest rate XML feed
``home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve``
(by month ``field_tdr_date_value_month=YYYYMM`` or year ``field_tdr_date_value=YYYY``). Each Atom
entry carries ``d:NEW_DATE`` and tenor fields ``BC_1MONTH`` .. ``BC_30YEAR`` (incl. ``BC_1_5MONTH``),
values in percent. ``BC_30YEARDISPLAY`` is kept only in the raw payload, never stored as a tenor
(CAP-MAC-07). Values are parsed from text into exact decimals; a missing tenor stays missing and is
never interpolated into storage.

Storage: dataset ``ust.par_yield_curve``, entity ``UST-PAR-NOMINAL``, field ``par_yield:<tenor>``,
unit ``percent``, ``period_end`` = curve date. Availability follows the capture rule in
``common`` with time zone America/New_York (live capture = first seen; backfill = 23:59:59 ET on the
curve date, flagged). A later fetch with a different value for a stored date is a new, flagged
publication (Treasury "reserves the option to make changes to the yield curve").

Compounding / day-count convention of par yields is unverified (CAP-MAC-07 open question), so no
bootstrapping is done; the only derived quantity is the labelled 10Y-2Y slope in basis points.

  python -m unknown.data.sources.treasury ingest --month 202610 [--home ~/.unknown]
"""
from __future__ import annotations

import argparse
import json
import time
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed
from ..pit import Observation, ResearchStore
from ..registry import dataset, load_registry
from .common import capture_availability, data_config, guard_xml, make_fetcher, record_if_changed

PROVIDER = "us-treasury"
DATASET = "ust.par_yield_curve"
ENTITY = "UST-PAR-NOMINAL"
FEED = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
TZ = "America/New_York"
TENORS = {"BC_1MONTH": "1M", "BC_1_5MONTH": "1.5M", "BC_2MONTH": "2M", "BC_3MONTH": "3M", "BC_4MONTH": "4M",
          "BC_6MONTH": "6M", "BC_1YEAR": "1Y", "BC_2YEAR": "2Y", "BC_3YEAR": "3Y", "BC_5YEAR": "5Y", "BC_7YEAR": "7Y",
          "BC_10YEAR": "10Y", "BC_20YEAR": "20Y", "BC_30YEAR": "30Y"}
TENOR_YEARS = {"1M": "1/12", "1.5M": "1.5/12", "2M": "2/12", "3M": "0.25", "4M": "4/12", "6M": "0.5", "1Y": "1", "2Y": "2",
               "3Y": "3", "5Y": "5", "7Y": "7", "10Y": "10", "20Y": "20", "30Y": "30"}
ORDER = list(TENORS.values())
ATTRIBUTION = "Source: U.S. Department of the Treasury, Daily Treasury Par Yield Curve Rates"
METHOD_NOTE = ("Par yields from indicative bid-side quotations at or near 3:30 PM, monotone convex method since 2021-12-06 "
               "(Treasury methodology page); compounding convention unverified, so no bootstrapped curves are produced.")


def month_url(yyyymm: str) -> str:
    if not (len(yyyymm) == 6 and yyyymm.isdigit()):
        raise ValueError("month must be YYYYMM")
    return f"{FEED}?data=daily_treasury_yield_curve&field_tdr_date_value_month={yyyymm}"


def year_url(yyyy: str) -> str:
    if not (len(yyyy) == 4 and yyyy.isdigit()):
        raise ValueError("year must be YYYY")
    return f"{FEED}?data=daily_treasury_yield_curve&field_tdr_date_value={yyyy}"


def parse_number(text: str) -> Fixed:
    try:
        return Fixed.parse(text)
    except ValueError:
        try:
            d = Decimal(text.strip())
        except InvalidOperation:
            raise ValueError(f"not a number: {text!r}") from None
        return Fixed.from_decimal(d.normalize() if d == d.to_integral_value() else d)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(body: bytes) -> list[dict]:
    """[{date, values: {tenor: Fixed|None}, raw: {field: text}}] in feed order."""
    guard_xml(body)
    root = ET.fromstring(body)
    out = []
    for props in root.iter():
        if _local(props.tag) != "properties":
            continue
        raw = {}
        for child in props:
            nil = any(_local(k) == "null" and v == "true" for k, v in child.attrib.items())
            raw[_local(child.tag)] = None if nil else (child.text or "").strip() or None
        if not raw.get("NEW_DATE"):
            continue
        d = date.fromisoformat(raw["NEW_DATE"][:10])
        values = {tenor: (parse_number(raw[f]) if raw.get(f) else None) for f, tenor in TENORS.items()}
        out.append({"date": d, "values": values, "raw": raw})
    return out


def observations(parsed: list[dict], raw_sha: str, receipt_ns: int) -> list[Observation]:
    out = []
    for entry in parsed:
        available, flags = capture_availability(receipt_ns, entry["date"], TZ)
        for field_name, tenor in TENORS.items():
            v = entry["values"][tenor]
            if v is None:
                continue  # a missing tenor stays missing; never interpolated into storage
            out.append(Observation(
                dataset=DATASET, entity_id=ENTITY, field=f"par_yield:{tenor}", unit="percent", value=v,
                period_end=entry["date"].isoformat(), period_kind="DAY", receipt_time_ns=receipt_ns, available_time_ns=available,
                publication_id=f"ust-par:{entry['date'].isoformat()}", source_ref=f"raw:{raw_sha}#{entry['date']}/{field_name}",
                quality=flags))
    return out


def ingest(store: ResearchStore, fetcher, now_ns, *, month: str | None = None, year: str | None = None) -> dict:
    ds = dataset(load_registry(), DATASET)
    if not ds.permits("storage"):
        raise PermissionError(f"{DATASET}: storage not permitted by the registry")
    url = month_url(month) if month else year_url(year or str(time.gmtime().tm_year))
    resp = fetcher.get(url)
    sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url},
                        receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
    parsed = parse_feed(resp.body)
    store.upsert_entity(ENTITY, "series", "US Treasury daily par yield curve (nominal)",
                        {"provider": PROVIDER, "dataset": DATASET, "unit": "percent", "tenors": ORDER})
    counts = {"NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for o in observations(parsed, sha, resp.receipt_time_ns):
        _, outcome = record_if_changed(store, o, now_ns())
        counts[outcome] = counts.get(outcome, 0) + 1
    dates = [e["date"].isoformat() for e in parsed]
    partial = {e["date"].isoformat(): [t for t in ORDER if e["values"][t] is None] for e in parsed if any(v is None for v in e["values"].values())}
    return {"provider": PROVIDER, "dataset": DATASET, "url": url, "raw_sha256": sha, "receipt_time": ns_to_iso(resp.receipt_time_ns),
            "curve_dates": len(dates), "first_date": min(dates) if dates else None, "last_date": max(dates) if dates else None,
            "partial_dates": partial, **counts}


def curve_as_of(store, as_of_ns: int, on_date: str | None = None, *, knowledge_cutoff_ns: int | None = None) -> dict:
    """The curve available at ``as_of_ns`` for ``on_date`` (or the latest curve date). If the
    requested date has no curve yet (holiday, or not yet published at the as-of time) the previous
    available date is returned and labelled, never interpolated (CAP-MAC-07 degraded behaviour)."""
    by_tenor = {t: {r["period_end"]: r for r in store.as_of(ENTITY, f"par_yield:{t}", as_of_ns, dataset=DATASET,
                                                            knowledge_cutoff_ns=knowledge_cutoff_ns)} for t in ORDER}
    dates = sorted({d for rows in by_tenor.values() for d in rows})
    if not dates:
        return {"status": "UNAVAILABLE", "reason": "no Treasury par curve available at the as-of time (not ingested or not yet published)",
                "as_of": ns_to_iso(as_of_ns), "dataset": DATASET}
    if on_date:
        eligible = [d for d in dates if d <= on_date]
        if not eligible:
            return {"status": "UNAVAILABLE", "reason": f"no curve on or before {on_date} available at the as-of time",
                    "as_of": ns_to_iso(as_of_ns), "dataset": DATASET}
        curve_date = eligible[-1]
    else:
        curve_date = dates[-1]
    points, missing = [], []
    for t in ORDER:
        r = by_tenor[t].get(curve_date)
        if r is None:
            missing.append(t)
            continue
        points.append({"tenor": t, "years": TENOR_YEARS[t], "value_pct": r["value"], "obs_id": r["obs_id"],
                       "available_time": ns_to_iso(r["available_time_ns"]), "publication_id": r["publication_id"],
                       "flags": json.loads(r["quality"] or "[]")})
    vals = {p["tenor"]: Fixed.parse(p["value_pct"]) for p in points}
    obs = {p["tenor"]: p["obs_id"] for p in points}
    slope = None
    if "10Y" in vals and "2Y" in vals:
        slope = {"value_bp": ((vals["10Y"] - vals["2Y"]) * 100).to_str(), "formula": "ust_slope_10y_2y@1: 100 x (y_10Y - y_2Y)",
                 "unit": "basis points", "inputs": [obs["10Y"], obs["2Y"]], "curve_date": curve_date, "label": "derived"}
    status = "OK" if not missing else "PARTIAL"
    out = {"status": status, "curve": "UST par nominal", "curve_date": curve_date, "as_of": ns_to_iso(as_of_ns),
           "unit": "percent", "points": points, "missing_tenors": missing, "slope_10y_2y": slope,
           "dataset": DATASET, "entity_id": ENTITY, "attribution": ATTRIBUTION, "method_note": METHOD_NOTE}
    if on_date and curve_date != on_date:
        out["requested_date"] = on_date
        out["fallback"] = f"no curve for {on_date} at the as-of time; showing the previous available date {curve_date}"
    return out


def point(store, tenor: str, as_of_ns: int, on_date: str | None = None) -> dict | None:
    """One tenor of the curve as of a time, with lineage (used for the DCF risk-free rate)."""
    if tenor not in TENOR_YEARS:
        raise ValueError(f"unknown tenor {tenor}; one of {', '.join(ORDER)}")
    curve = curve_as_of(store, as_of_ns, on_date)
    if curve["status"] == "UNAVAILABLE":
        return None
    p = next((p for p in curve["points"] if p["tenor"] == tenor), None)
    if p is None:
        return None
    return {**p, "curve_date": curve["curve_date"], "dataset": DATASET, "currency": "USD", "attribution": ATTRIBUTION,
            "fallback": curve.get("fallback")}


def main(argv: list[str] | None = None) -> None:
    from ...service.paths import Paths
    ap = argparse.ArgumentParser(description="Ingest the US Treasury daily par yield curve into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    g = ing.add_mutually_exclusive_group(required=True)
    g.add_argument("--month", help="YYYYMM")
    g.add_argument("--year", help="YYYY")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    home = Path(a.home).expanduser() if a.home else None
    paths = Paths(home) if home else Paths.default()
    research = paths.root / "research"
    research.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(research / "research.sqlite")
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns, month=a.month, year=a.year), indent=2))


if __name__ == "__main__":
    main()
