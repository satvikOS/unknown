"""World Bank World Development Indicators (CAP-MAC-05; registry ``world-bank``, dataset ``worldbank.wdi``).

Source: Indicators API v2, one request per indicator for all venue countries:
``https://api.worldbank.org/v2/country/USA;IND;DEU;.../indicator/<ID>?format=json&date=<from>:<to>&per_page=1000``.
The response is ``[meta, rows]``; a row carries ``countryiso3code``, ``date`` (year), ``value``
(JSON number or null) and ``obs_status``. The JSON is parsed with numbers kept as their literal
text, so ``0.239578342117881`` is stored exactly as published, never through a float. A null value
is stored as NOT_YET_PUBLISHED (the latest year is often not yet compiled), never zero.

Indicators (``macro_common.COLUMNS``): NY.GDP.MKTP.KD.ZG (GDP growth), FP.CPI.TOTL.ZG (CPI
inflation), SL.UEM.TOTL.ZS (unemployment, ILO modelled). Availability: first capture (no vintages).

  python -m unknown.data.sources.worldbank ingest --from 2015
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date
from urllib.parse import urlencode

from ...domain.clock import ns_to_iso
from . import macro_common as mc
from .common import data_config, make_fetcher

PROVIDER = "world-bank"
DATASET = "worldbank.wdi"
BASE = "https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"
WB_COLUMNS = tuple(c for c in mc.COLUMNS if c.dataset == DATASET)


def url(indicator: str, iso3: list[str], start_year: int, end_year: int) -> str:
    if not 1960 <= start_year <= end_year <= 2100:
        raise ValueError("years must satisfy 1960 <= from <= to <= 2100")
    return BASE.format(countries=";".join(iso3), indicator=indicator) + "?" + urlencode(
        {"format": "json", "date": f"{start_year}:{end_year}", "per_page": "1000"})


def parse(body: bytes, indicator: str) -> tuple[dict, list[dict], list[dict]]:
    """(meta, rows, skipped). A row is {iso3, year, value (Fixed|None), obs_status}."""
    doc = json.loads(body.decode("utf-8-sig"), parse_float=str, parse_int=str)
    if not isinstance(doc, list) or not doc:
        raise ValueError("unexpected World Bank response shape")
    meta = doc[0] if isinstance(doc[0], dict) else {}
    if "message" in meta:
        raise ValueError(f"World Bank API error: {json.dumps(meta['message'])[:300]}")
    if str(meta.get("pages", "1")) not in ("0", "1"):
        raise ValueError(f"response has {meta.get('pages')} pages; narrow the year range (per_page=1000)")
    rows, skipped = [], []
    for r in (doc[1] if len(doc) > 1 and doc[1] else []):
        if (r.get("indicator") or {}).get("id") != indicator:
            skipped.append({"row": r.get("date"), "reason": "indicator mismatch"})
            continue
        iso3, year, raw = r.get("countryiso3code"), str(r.get("date") or ""), r.get("value")
        if not iso3 or not (len(year) == 4 and year.isdigit()):
            skipped.append({"row": year, "reason": "no country or year"})
            continue
        try:
            value = mc.exact(raw) if raw is not None else None
        except ValueError as exc:
            skipped.append({"country": iso3, "year": year, "reason": str(exc)})
            continue
        rows.append({"iso3": iso3, "year": year, "value": value, "obs_status": r.get("obs_status") or ""})
    return meta, rows, skipped


def ingest(store, fetcher, now_ns, *, start_year: int, end_year: int | None = None, columns=WB_COLUMNS) -> dict:
    mc.require_storage(DATASET)
    end_year = end_year or date.today().year
    cs = mc.countries()
    by_iso3 = {c.iso3: c.code for c in cs}
    out = {"provider": PROVIDER, "dataset": DATASET, "indicators": [], "NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for col in columns:
        u = url(col.series_code, [c.iso3 for c in cs], start_year, end_year)
        resp = fetcher.get(u)
        sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url},
                            receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
        meta, rows, skipped = parse(resp.body, col.series_code)
        counts: dict = {}
        for iso3 in sorted({r["iso3"] for r in rows}):
            area = by_iso3.get(iso3)
            if area is None:
                continue
            pts = [{"period": r["year"], "value": r["value"], "missing_reason": "NOT_YET_PUBLISHED", "key": f"{col.series_code}/{iso3}",
                    "quality": ((f"obs_status:{r['obs_status']}",) if r["obs_status"] else ())
                    + ((f"source_lastupdated:{meta['lastupdated']}",) if meta.get("lastupdated") else ())}
                   for r in rows if r["iso3"] == iso3]
            mc.merge_counts(counts, mc.record_points(store, col, area, pts, raw_sha=sha, receipt_ns=resp.receipt_time_ns, now_ns=now_ns))
        mc.merge_counts(out, counts)
        out["indicators"].append({"column_id": col.column_id, "indicator": col.series_code, "url": u, "raw_sha256": sha,
                                  "receipt_time": ns_to_iso(resp.receipt_time_ns), "rows": len(rows),
                                  "with_value": sum(r["value"] is not None for r in rows), "skipped": len(skipped),
                                  "source_lastupdated": meta.get("lastupdated"), **counts})
    return out


def main(argv: list[str] | None = None) -> None:
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Ingest World Bank WDI indicators into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--from", dest="start", type=int, default=2010)
    ing.add_argument("--to", dest="end", type=int, default=None)
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    store, home = open_store(a.home)
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns, start_year=a.start, end_year=a.end), indent=2))


if __name__ == "__main__":
    main()
