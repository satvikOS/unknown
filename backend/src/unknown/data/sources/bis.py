"""BIS central bank policy rates (CAP-MAC-06, CAP-MAC-09; registry ``bis``, dataset ``bis.cbpol``).

Source: BIS Data Portal SDMX API v2, dataflow ``WS_CBPOL`` monthly, end of period:
``https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/M.US+IN+XM?startPeriod=YYYY-MM&format=csv``.
Areas: ``US`` (mid-point of the Federal Reserve target range since 1985), ``IN`` (Reserve Bank of
India policy repo rate; the RBI website itself is blocked by its terms, see config/providers/rbi.json)
and ``XM`` (ECB, used for every euro-area venue country). The series' own compilation note
(``COMPILATION``) and source (``SOURCE_REF``) are kept in the entity attributes.

Availability: the rate in force on the last day of a month is public by then, so a backfilled
month is available at 23:59:59 UTC on its last day (flag ``availability_estimated_backfill``); a
month captured before it ends is available at receipt. Changed values on re-fetch are new
publications flagged ``revised_after_first_seen``.

  python -m unknown.data.sources.bis ingest --from 2015-01
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date
from urllib.parse import urlencode

from ...domain.clock import ns_to_iso
from . import macro_common as mc
from . import sdmx_csv
from .common import capture_availability, data_config, make_fetcher

PROVIDER = "bis"
DATASET = "bis.cbpol"
BASE = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/M.{areas}"
COLUMN = mc.column("bis_policy_rate")


def url(areas: list[str], start_period: str) -> str:
    mc.period_bounds(start_period)
    return BASE.format(areas="+".join(areas)) + "?" + urlencode({"startPeriod": start_period, "format": "csv"})


def month_end_availability(receipt_ns: int, period_end: date) -> tuple[int, tuple[str, ...]]:
    return capture_availability(receipt_ns, period_end, "UTC")


def ingest(store, fetcher, now_ns, *, start_period: str) -> dict:
    mc.require_storage(DATASET)
    areas = list(COLUMN.areas)
    u = url(areas, start_period)
    resp = fetcher.get(u)
    sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url},
                        receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
    rows, skipped = sdmx_csv.parse(resp.body)
    counts: dict = {"NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for area in sorted({r["area"] for r in rows}):
        if area not in areas:
            continue
        mine = [r for r in rows if r["area"] == area]
        first = mine[0]["raw"]
        pts = [{"period": r["period"], "value": r["value"], "missing_reason": "NOT_REPORTED", "key": f"WS_CBPOL/M.{area}",
                "quality": (f"obs_status:{r['obs_status']}",) if r["obs_status"] else ()} for r in mine]
        mc.merge_counts(counts, mc.record_points(
            store, COLUMN, area, pts, raw_sha=sha, receipt_ns=resp.receipt_time_ns, now_ns=now_ns, availability=month_end_availability,
            title=mine[0]["title"] or None,
            extra_attrs={"compilation": (first.get("COMPILATION") or "").strip(), "source_ref": mine[0]["source_ref"]}))
    return {"provider": PROVIDER, "dataset": DATASET, "url": u, "raw_sha256": sha, "receipt_time": ns_to_iso(resp.receipt_time_ns),
            "rows": len(rows), "areas": sorted({r["area"] for r in rows}), "skipped": len(skipped),
            "not_returned": sorted(set(areas) - {r["area"] for r in rows}), **counts}


def main(argv: list[str] | None = None) -> None:
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Ingest BIS central bank policy rates into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--from", dest="start", default="2015-01", help="YYYY-MM")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    store, home = open_store(a.home)
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns, start_period=a.start), indent=2))


if __name__ == "__main__":
    main()
