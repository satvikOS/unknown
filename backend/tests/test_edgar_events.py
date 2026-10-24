"""Events calendar from SEC filings (CAP-NEWS-10, partial). FIXTURE data only.

Occurred entries come from accepted 8-K Item 2.02 and 10-Q/10-K filings, announced ones from a date stated in an
8-K, projected ones are labelled estimated (internal) and give way to announcements and occurrences.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import RouteOpener, ns  # noqa: E402

from unknown.api.routes import calendar as route  # noqa: E402
from unknown.data.http import Fetcher  # noqa: E402
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.data.registry import RateLimit  # noqa: E402
from unknown.data.sources import edgar_events as ev  # noqa: E402
from unknown.data.sources import edgar_ownership as eo  # noqa: E402

CIK = 9100002
NOW = ns("2026-10-20T00:00:00Z")
ANNOUNCE = (b"<html><body><p>FIXTURE Corp. will report its fiscal 2027 first quarter results on Tuesday, November 3, 2026, after the "
            b"market closes, and will host a conference call at 4:30 p.m. ET. Revenue for the quarter ended September 30, 2026 "
            b"is not included here.</p></body></html>")
FILINGS = [  # accession, form, acceptance, filing date, items, primary document
    ("0009100002-25-000010", "8-K", "2025-11-04T21:05:00.000Z", "2025-11-04", "2.02,9.01", "q4-25.htm"),
    ("0009100002-25-000011", "10-K", "2025-11-14T21:00:00.000Z", "2025-11-14", "", "10k.htm"),
    ("0009100002-26-000001", "8-K", "2026-02-03T21:05:00.000Z", "2026-02-03", "2.02,9.01", "q1.htm"),
    ("0009100002-26-000002", "10-Q", "2026-02-06T21:00:00.000Z", "2026-02-06", "", "10q1.htm"),
    ("0009100002-26-000003", "8-K", "2026-05-05T21:05:00.000Z", "2026-05-05", "2.02,9.01", "q2.htm"),
    ("0009100002-26-000004", "8-K", "2026-10-13T12:00:00.000Z", "2026-10-13", "7.01,9.01", "date.htm"),
    ("0009100002-26-000005", "8-K", "2026-10-30T21:05:00.000Z", "2026-10-30", "2.02", "future.htm"),
]


def submissions() -> dict:
    r = FILINGS
    return {"cik": str(CIK), "name": "Fixture Calendar Corp (synthetic)", "sic": "7372", "tickers": ["FXCAL"], "exchanges": ["NYSE"],
            "filings": {"recent": {"accessionNumber": [x[0] for x in r], "form": [x[1] for x in r], "acceptanceDateTime": [x[2] for x in r],
                                   "filingDate": [x[3] for x in r], "reportDate": ["" for _ in r], "items": [x[4] for x in r],
                                   "primaryDocument": [x[5] for x in r]}}}


class AnnouncementParserTests(unittest.TestCase):
    def test_future_date_with_time_and_no_period_end(self):
        found = ev.find_announced_dates(ev.html_to_text(ANNOUNCE), date(2026, 10, 13))
        self.assertEqual([(f["date"], f["event_type"], f["time_text"]) for f in found], [("2026-11-03", "results_release", "4:30 p.m. ET")])
        self.assertTrue(found[0]["call_mentioned"])

    def test_year_inferred_and_past_or_far_dates_ignored(self):
        f = ev.find_announced_dates("The Company will announce earnings on January 28 before the open.", date(2026, 12, 20))
        self.assertEqual([(x["date"], x["year_inferred"]) for x in f], [("2027-01-28", True)])
        self.assertEqual(ev.find_announced_dates("The Company will report results on March 3, 2026.", date(2026, 10, 1)), [])
        self.assertEqual(ev.find_announced_dates("Results for the quarter ended December 31, 2026 will follow.", date(2026, 10, 1)), [])
        self.assertEqual(ev.find_announced_dates("The board will meet on November 3, 2026.", date(2026, 10, 1)), [])


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "research").mkdir()
        self.store = ResearchStore(self.root / "research" / "research.sqlite")
        routes = {eo.SUBMISSIONS.format(cik=CIK): json.dumps(submissions()).encode(),
                  f"{eo.folder_url(CIK, '0009100002-26-000004')}/date.htm": ANNOUNCE}
        fetcher = Fetcher("Fixture Owner owner@example.test", RateLimit(10, 1, "fixture"), opener=RouteOpener(routes), clock_ns=lambda: NOW,
                          sleep=lambda s: None)
        subs = eo.load_or_fetch_submissions(self.store, CIK, fetcher)
        self.ingest = ev.ingest_announcements(self.store, CIK, subs, fetcher, lambda: NOW)
        self.eid = eo.entity_id(CIK)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def cal(self, as_of: str, start="2026-01-01", end="2027-03-31", **kw):
        return ev.calendar(self.store, [{"entity_id": self.eid}], date.fromisoformat(start), date.fromisoformat(end), ns(as_of), **kw)

    def events(self, out):
        return [(e["status"], e["event_type"], e["date"]) for w in out["weeks"] for e in w["events"]]

    def test_ingested_announcement_and_statuses(self):
        self.assertEqual((self.ingest["candidates"], self.ingest["announced"]), (1, 1))
        got = self.events(self.cal("2026-10-20T00:00:00Z"))
        self.assertIn(("occurred", "results_release", "2026-02-03"), got)
        self.assertIn(("occurred", "periodic_report", "2026-02-06"), got)
        self.assertIn(("announced", "results_release", "2026-11-03"), got)
        # The projection from 2025-11-04 (+364 d = 2026-11-03) gives way to the announcement; the 10-K projection stays, labelled.
        self.assertNotIn(("projected", "results_release", "2026-11-03"), got)
        proj = [e for w in self.cal("2026-10-20T00:00:00Z")["weeks"] for e in w["events"] if e["status"] == "projected"]
        self.assertIn(("periodic_report", "2026-11-13"), [(e["event_type"], e["date"]) for e in proj])
        self.assertTrue(all(e["basis"] == "derived" and e["label"].startswith("estimated (internal)") for e in proj))
        # The 2026-02-03 release projects one year (364 days) ahead
        self.assertIn(("projected", "results_release", "2027-02-02"), got)

    def test_as_of_hides_later_filings_and_announcements(self):
        got = self.events(self.cal("2026-10-12T00:00:00Z"))
        self.assertNotIn(("announced", "results_release", "2026-11-03"), got)
        self.assertIn(("projected", "results_release", "2026-11-03"), got)          # before the 8-K, only the estimate exists
        self.assertNotIn(("occurred", "results_release", "2026-10-30"), got)
        later = self.cal("2026-11-01T00:00:00Z")
        ann = [e for w in later["weeks"] for e in w["events"] if e["status"] == "announced"][0]
        self.assertEqual(ann["evidence"][:20], "FIXTURE Corp. will r")
        self.assertIsNone(ann["confirmed_by"])
        self.assertEqual(self.events(later).count(("occurred", "results_release", "2026-10-30")), 1)
        self.assertNotIn(("projected", "results_release", "2026-11-03"), self.events(later))

    def test_filters_and_weeks(self):
        out = self.cal("2026-10-20T00:00:00Z", statuses={"occurred"}, types={"periodic_report"})
        self.assertEqual({(s, t) for s, t, _ in self.events(out)}, {("occurred", "periodic_report")})
        self.assertTrue(all(date.fromisoformat(w["week_start"]).weekday() == 0 for w in out["weeks"]))

    def test_route(self):
        ctx = SimpleNamespace(paths=SimpleNamespace(root=self.root))
        body = json.loads(json.dumps(route.handle(ctx, ["calendar"], {"issuers": "FXCAL.XNYS,NOPE", "as_of": "2026-10-20",
                                                                      "from": "2026-10-01", "to": "2026-11-30"}, "SIM", None)))
        self.assertEqual(body["unresolved"], ["NOPE"])
        self.assertEqual([i["status"] for i in body["issuers"]], ["OK"])
        self.assertTrue(all(e["status"] in ("occurred", "announced", "projected") for w in body["weeks"] for e in w["events"]))
        with self.assertRaises(ValueError):
            route.handle(ctx, ["calendar"], {"status": "rumoured"}, "SIM", None)
        with self.assertRaises(ValueError):
            route.handle(ctx, ["calendar"], {"from": "2026-10-01", "to": "2025-10-01"}, "SIM", None)


if __name__ == "__main__":
    unittest.main()
