"""OECD Data Explorer SDMX API: monthly CPI inflation and unemployment (CAP-MAC-05; registry ``oecd``).

Dataflows (``macro_common.COLUMNS``):

* ``OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0`` key ``<AREAS>.M.N.CPI.PA._T.N.GY``: consumer price
  index, national methodology, total, percent per annum, growth over 1 year, monthly.
* ``OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0`` key ``<AREAS>.UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M``:
  unemployment rate, percent of labour force, seasonally adjusted, age 15+, monthly. India is not
  in this dataflow, so the column covers US and the EU venue countries only.

``format=csvfile`` (SDMX-CSV without labels). The OECD limits data downloads (20 per hour is the
strictest published figure), so the registry paces requests at one every 180 s and one request
covers every country. Availability: first capture (no vintages read).

  python -m unknown.data.sources.oecd ingest --from 2024-01
"""
from __future__ import annotations

import argparse
import json
import time
from urllib.parse import urlencode

from ...domain.clock import ns_to_iso
from . import macro_common as mc
from . import sdmx_csv
from .common import data_config, make_fetcher

PROVIDER = "oecd"
DATASET = "oecd.sdmx_macro"
BASE = "https://sdmx.oecd.org/public/rest/data/"
FLOWS = {
    "oecd_cpi_yoy": ("OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0", "{areas}.M.N.CPI.PA._T.N.GY"),
    "oecd_unemployment": ("OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0", "{areas}.UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M"),
}
OECD_COLUMNS = tuple(c for c in mc.COLUMNS if c.dataset == DATASET)


def url(column_id: str, iso3: list[str], start_period: str) -> str:
    flow, key = FLOWS[column_id]
    mc.period_bounds(start_period)  # validates YYYY or YYYY-MM
    return BASE + flow + "/" + key.format(areas="+".join(iso3)) + "?" + urlencode({"startPeriod": start_period, "format": "csvfile"})


def ingest(store, fetcher, now_ns, *, start_period: str, columns=OECD_COLUMNS) -> dict:
    mc.require_storage(DATASET)
    cs = mc.countries()
    out = {"provider": PROVIDER, "dataset": DATASET, "series": [], "NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for col in columns:
        wanted = [c for c in cs if col.areas is None or c.code in col.areas]
        by_iso3 = {c.iso3: c.code for c in wanted}
        u = url(col.column_id, [c.iso3 for c in wanted], start_period)
        resp = fetcher.get(u)
        sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url},
                            receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
        rows, skipped = sdmx_csv.parse(resp.body)
        counts: dict = {}
        for iso3 in sorted({r["area"] for r in rows}):
            area = by_iso3.get(iso3)
            if area is None:
                skipped.append({"area": iso3, "reason": "area not requested"})
                continue
            pts = [{"period": r["period"], "value": r["value"], "missing_reason": "NOT_REPORTED", "key": f"{col.series_code}/{iso3}",
                    "quality": (f"obs_status:{r['obs_status']}",) if r["obs_status"] else ()}
                   for r in rows if r["area"] == iso3]
            mc.merge_counts(counts, mc.record_points(store, col, area, pts, raw_sha=sha, receipt_ns=resp.receipt_time_ns, now_ns=now_ns))
        mc.merge_counts(out, counts)
        out["series"].append({"column_id": col.column_id, "url": u, "raw_sha256": sha, "receipt_time": ns_to_iso(resp.receipt_time_ns),
                              "rows": len(rows), "areas": sorted({r["area"] for r in rows}), "skipped": len(skipped),
                              "not_returned": sorted(set(by_iso3) - {r["area"] for r in rows}), **counts})
    return out


def main(argv: list[str] | None = None) -> None:
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Ingest OECD monthly CPI inflation and unemployment into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--from", dest="start", default="2020-01", help="YYYY-MM")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    store, home = open_store(a.home)
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns, start_period=a.start), indent=2))


if __name__ == "__main__":
    main()
