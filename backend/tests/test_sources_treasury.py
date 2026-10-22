"""US Treasury par yield curve ingestion (CAP-DATA-06, CAP-MAC-07). No network: a FIXTURE feed shaped like the
documented Atom/OData XML (field names as observed in the October 2026 feed, CAP-MAC-07). Values are synthetic."""
from __future__ import annotations

import unittest
from datetime import date

from unknown.data.http import Response
from unknown.data.pit import ResearchStore
from unknown.data.registry import dataset, load_registry
from unknown.data.sources import treasury
from unknown.data.sources.common import capture_availability, end_of_day_ns
from unknown.domain.clock import utc_ns
from unknown.domain.fixed import Fixed

def entry(day: str, values: dict) -> str:
    fields = []
    for f in treasury.TENORS:
        v = values.get(f)
        fields.append(f'<d:{f} m:type="Edm.Double" m:null="true" />' if v is None else f'<d:{f} m:type="Edm.Double">{v}</d:{f}>')
    fields.append(f'<d:BC_30YEARDISPLAY m:type="Edm.Double">{values.get("BC_30YEAR", "")}</d:BC_30YEARDISPLAY>')
    return (f'<entry><id>fixture</id><content type="application/xml"><m:properties>'
            f'<d:Id m:type="Edm.Int32">1</d:Id><d:NEW_DATE m:type="Edm.DateTime">{day}T00:00:00</d:NEW_DATE>{"".join(fields)}'
            f'</m:properties></content></entry>')


BASE = {f: f"4.{i:02d}" for i, f in enumerate(treasury.TENORS)}
BASE["BC_10YEAR"] = "5.13"
BASE["BC_2YEAR"] = "4.50"


def feed(entries: list[str]) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8" standalone="yes"?><!-- FIXTURE (synthetic values) -->'
            '<feed xml:base="https://home.treasury.gov/" xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" '
            'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"><title type="text">FIXTURE</title>'
            + "".join(entries) + "</feed>").encode()


FEED = feed([entry("2026-10-20", BASE), entry("2026-10-21", {**BASE, "BC_1_5MONTH": None})])


class StubFetcher:
    def __init__(self, body: bytes, receipt_ns: int):
        self.body, self.receipt_ns, self.urls = body, receipt_ns, []

    def get(self, url):
        self.urls.append(url)
        return Response(url, 200, self.body, "application/xml", self.receipt_ns)


class TreasuryTests(unittest.TestCase):
    def setUp(self):
        self.s = ResearchStore(":memory:")

    def tearDown(self):
        self.s.close()

    def test_registry_entry(self):
        reg = load_registry()
        ds = dataset(reg, treasury.DATASET)
        self.assertTrue(ds.permits("storage"))
        self.assertFalse(ds.permits("model_processing_cloud"))     # provisional rights: not granted
        self.assertFalse(ds.vintage_support)
        self.assertEqual(reg["us-treasury"].rate_limit.max_requests, 1)

    def test_parse_exact_values_missing_tenor_and_display_field(self):
        parsed = treasury.parse_feed(FEED)
        self.assertEqual([e["date"].isoformat() for e in parsed], ["2026-10-20", "2026-10-21"])
        self.assertEqual(parsed[0]["values"]["10Y"], Fixed.parse("5.13"))
        self.assertIsNone(parsed[1]["values"]["1.5M"])
        self.assertNotIn("30YEARDISPLAY", parsed[0]["values"])
        self.assertEqual(len(parsed[0]["values"]), 14)
        self.assertEqual(treasury.parse_number("4.0400000000000000e+000"), Fixed.parse("4.04"))
        with self.assertRaises(ValueError):
            treasury.parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed/>')

    def test_live_capture_and_availability(self):
        receipt = utc_ns(2026, 10, 21, 22, 30)                  # 18:30 New York on the curve date
        out = treasury.ingest(self.s, StubFetcher(FEED, receipt), lambda: receipt, month="202610")
        self.assertEqual((out["NEW"], out["curve_dates"]), (27, 2))
        self.assertEqual(out["partial_dates"], {"2026-10-21": ["1.5M"]})
        ten = self.s.latest_as_of(treasury.ENTITY, "par_yield:10Y", receipt)
        self.assertEqual((ten["value"], ten["period_end"], ten["available_time_ns"]), ("5.13", "2026-10-21", receipt))
        # a backtest clock at 17:00 New York on 2026-10-21 cannot read that day's curve
        five_pm = utc_ns(2026, 10, 21, 21, 0)
        self.assertEqual(self.s.latest_as_of(treasury.ENTITY, "par_yield:10Y", five_pm)["period_end"], "2026-10-20")
        # 2026-10-20 was captured after its own day ended: backfill bound, flagged
        prev = self.s.as_of(treasury.ENTITY, "par_yield:10Y", receipt)[0]
        self.assertEqual(prev["available_time_ns"], end_of_day_ns(date(2026, 10, 20), "America/New_York"))
        self.assertIn("availability_estimated_backfill", prev["quality"])

    def test_refetch_unchanged_and_revision_never_leaks(self):
        r1 = utc_ns(2026, 10, 22, 12)
        treasury.ingest(self.s, StubFetcher(FEED, r1), lambda: r1, month="202610")
        again = treasury.ingest(self.s, StubFetcher(FEED, r1 + 10**9), lambda: r1 + 10**9, month="202610")
        self.assertEqual((again["NEW"], again["UNCHANGED"]), (0, 27))
        revised = feed([entry("2026-10-20", {**BASE, "BC_10YEAR": "5.14"}), entry("2026-10-21", {**BASE, "BC_1_5MONTH": None})])
        r2 = utc_ns(2026, 10, 23, 12)
        out = treasury.ingest(self.s, StubFetcher(revised, r2), lambda: r2, month="202610")
        self.assertEqual(out["REVISED"], 1)
        rows = [r for r in self.s.as_of(treasury.ENTITY, "par_yield:10Y", r2) if r["period_end"] == "2026-10-20"]
        self.assertEqual(rows[-1]["value"], "5.14")
        self.assertIn("revised_after_first_seen", rows[-1]["quality"])
        before = [r for r in self.s.as_of(treasury.ENTITY, "par_yield:10Y", r2 - 1) if r["period_end"] == "2026-10-20"]
        self.assertEqual(before[-1]["value"], "5.13")

    def test_curve_as_of_fallback_and_point(self):
        receipt = utc_ns(2026, 10, 21, 22, 30)
        treasury.ingest(self.s, StubFetcher(FEED, receipt), lambda: receipt, month="202610")
        c = treasury.curve_as_of(self.s, receipt, "2026-10-22")        # holiday / not yet published
        self.assertEqual(c["curve_date"], "2026-10-21")
        self.assertIn("previous available date", c["fallback"])
        self.assertEqual(c["status"], "PARTIAL")
        self.assertEqual(c["slope_10y_2y"]["value_bp"], "63.00")
        by_tenor = {pt["tenor"]: pt["obs_id"] for pt in c["points"]}
        self.assertEqual(c["slope_10y_2y"]["inputs"], [by_tenor["10Y"], by_tenor["2Y"]])     # lineage of the derived slope
        p = treasury.point(self.s, "10Y", receipt)
        self.assertEqual((p["value_pct"], p["currency"]), ("5.13", "USD"))
        with self.assertRaises(ValueError):
            treasury.point(self.s, "11Y", receipt)

    def test_capture_rule_bounds(self):
        d = date(2026, 10, 21)
        eod = end_of_day_ns(d, "America/New_York")
        self.assertEqual(capture_availability(eod - 5, d, "America/New_York"), (eod - 5, ()))
        self.assertEqual(capture_availability(eod + 5, d, "America/New_York"), (eod, ("availability_estimated_backfill",)))
        with self.assertRaises(ValueError):
            treasury.month_url("2026-10")


if __name__ == "__main__":
    unittest.main()
