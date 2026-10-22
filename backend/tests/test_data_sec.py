"""Provider registry, polite fetcher and SEC EDGAR ingestion (DAT-01..DAT-04).

Network is never used: a fake opener returns SYNTHETIC payloads shaped like the
documented SEC JSON (field names per the SEC pages cited in
config/providers/sec-edgar.json). Values are fixtures, not real filings."""
import io
import json
import unittest
import urllib.error

from unknown.data.http import Fetcher, FetchError, TokenBucket
from unknown.data.pit import ResearchStore
from unknown.data.registry import RateLimit, RegistryError, dataset, load_registry
from unknown.data.sec import acceptance_times, end_of_filed_day_ns, entity_id, ingest
from unknown.domain.clock import utc_ns

CIK = 1234567
SUBMISSIONS = {
    "cik": str(CIK), "name": "Fixture Industries Inc (synthetic)", "tickers": ["FXI"], "exchanges": ["Nasdaq"],
    "filings": {"recent": {
        "accessionNumber": ["0000000000-26-000002", "0000000000-26-000001"],
        "acceptanceDateTime": ["2026-05-10T20:01:02.000Z", "2026-02-20T21:05:00.000Z"],
        "form": ["10-K/A", "10-K"], "filingDate": ["2026-05-10", "2026-02-20"]}}}
COMPANYFACTS = """{"cik": %d, "entityName": "Fixture Industries Inc (synthetic)", "facts": {"us-gaap": {
  "Revenues": {"units": {"USD": [
     {"start": "2025-01-01", "end": "2025-12-31", "val": 1000000, "accn": "0000000000-26-000001", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-20"},
     {"start": "2025-01-01", "end": "2025-12-31", "val": 990000, "accn": "0000000000-26-000002", "fy": 2025, "fp": "FY", "form": "10-K/A", "filed": "2026-05-10"},
     {"start": "2024-01-01", "end": "2024-12-31", "val": 800000, "accn": "0000000000-25-000009", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-21"}]}},
  "EarningsPerShareBasic": {"units": {"USD/shares": [
     {"start": "2025-01-01", "end": "2025-12-31", "val": 0.1, "accn": "0000000000-26-000001", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-20"}]}},
  "Assets": {"units": {"USD": [
     {"end": "2025-12-31", "val": 5000000.5, "accn": "0000000000-26-000001", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-20"}]}}}}}""" % CIK


class FakeResp:
    def __init__(self, body: bytes, status: int = 200, ctype: str = "application/json"):
        self.status, self._body, self.headers = status, body, {"Content-Type": ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, routes: dict, fail_first: dict | None = None):
        self.routes, self.calls, self.fail_first = routes, [], dict(fail_first or {})

    def __call__(self, req, timeout):
        self.calls.append((req.full_url, req.get_header("User-agent")))
        url = req.full_url
        if self.fail_first.get(url):
            self.fail_first[url] -= 1
            raise urllib.error.HTTPError(url, 503, "busy", {}, io.BytesIO(b""))
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))
        return FakeResp(self.routes[url])


class RegistryTests(unittest.TestCase):
    def test_sec_registered_with_rights_and_limit(self):
        reg = load_registry()
        self.assertEqual(reg["sec-edgar"].rate_limit.max_requests, 10)
        self.assertTrue(dataset(reg, "sec.companyfacts").permits("storage"))
        self.assertFalse(dataset(reg, "sec.companyfacts").permits("redistribution"))
        with self.assertRaises(RegistryError):
            dataset(reg, "unregistered.dataset")
        with self.assertRaises(RegistryError):
            dataset(reg, "sec.companyfacts").permits("resell")


class FetcherTests(unittest.TestCase):
    def test_user_agent_required(self):
        with self.assertRaises(FetchError):
            Fetcher("", RateLimit(10, 1, "x"))
        with self.assertRaises(FetchError):
            Fetcher("no contact", RateLimit(10, 1, "x"))

    def test_retries_then_succeeds_and_404_is_fatal(self):
        url = "https://example.test/a"
        opener = FakeOpener({url: b"ok"}, fail_first={url: 2})
        sleeps = []
        f = Fetcher("Fixture Owner owner@example.test", RateLimit(10, 1, "x"), opener=opener, sleep=sleeps.append)
        self.assertEqual(f.get(url).body, b"ok")
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(opener.calls[0][1], "Fixture Owner owner@example.test")
        with self.assertRaises(FetchError):
            f.get("https://example.test/missing")

    def test_token_bucket_paces(self):
        t = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            t[0] += s
        b = TokenBucket(RateLimit(10, 1.0, "x"), monotonic=lambda: t[0], sleep=sleep)
        for _ in range(25):
            b.acquire()
        self.assertGreaterEqual(t[0], 1.5 - 1e-9)  # 25 requests at 10/s with burst 10 need >= 1.5 s


class SecIngestTests(unittest.TestCase):
    def setUp(self):
        self.store = ResearchStore(":memory:")
        routes = {f"https://data.sec.gov/submissions/CIK{CIK:010d}.json": json.dumps(SUBMISSIONS).encode(),
                  f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json": COMPANYFACTS.encode()}
        self.fetcher = Fetcher("Fixture Owner owner@example.test", RateLimit(10, 1, "x"), opener=FakeOpener(routes),
                               clock_ns=lambda: utc_ns(2026, 10, 22), sleep=lambda s: None)

    def tearDown(self):
        self.store.close()

    def test_ingest_point_in_time(self):
        result = ingest(CIK, self.store, self.fetcher, lambda: utc_ns(2026, 10, 22))
        self.assertEqual(result["observations"], 5)
        e = entity_id(CIK)
        accepted_10k = utc_ns(2026, 2, 20, 21, 5)
        before = self.store.latest_as_of(e, "us-gaap:Revenues", accepted_10k - 1, period_kind="FY")
        self.assertEqual((before["value"], before["period_end"]), ("800000", "2024-12-31"))  # FY2025 not yet published
        r = self.store.latest_as_of(e, "us-gaap:Revenues", accepted_10k, period_kind="FY")
        self.assertEqual((r["value"], r["period_end"], r["available_time_ns"]), ("1000000", "2025-12-31", accepted_10k))
        restated = self.store.latest_as_of(e, "us-gaap:Revenues", utc_ns(2026, 5, 10, 20, 1, 2), period_kind="FY")
        self.assertEqual(restated["value"], "990000")
        old = self.store.latest_as_of(e, "us-gaap:Revenues", utc_ns(2025, 6, 1), period_kind="FY")
        self.assertEqual(old["value"], "800000")
        self.assertIn("availability_from_filed_date", old["quality"])
        self.assertEqual(old["available_time_ns"], end_of_filed_day_ns("2025-02-21"))

    def test_decimal_values_are_exact(self):
        ingest(CIK, self.store, self.fetcher, lambda: utc_ns(2026, 10, 22))
        e = entity_id(CIK)
        eps = self.store.latest_as_of(e, "us-gaap:EarningsPerShareBasic", utc_ns(2026, 3, 1))
        self.assertEqual((eps["value"], eps["unit"], eps["currency"]), ("0.1", "USD/shares", "USD"))
        assets = self.store.latest_as_of(e, "us-gaap:Assets", utc_ns(2026, 3, 1))
        self.assertEqual((assets["value"], assets["period_kind"]), ("5000000.5", "INSTANT"))

    def test_reingest_is_idempotent_and_ticker_not_backdated(self):
        ingest(CIK, self.store, self.fetcher, lambda: utc_ns(2026, 10, 22))
        again = ingest(CIK, self.store, self.fetcher, lambda: utc_ns(2026, 10, 23))
        self.assertEqual((again["NEW"], again["UNCHANGED"], again["CORRECTED"]), (0, 5, 0))
        self.assertEqual(self.store.resolve("ticker:Nasdaq", "FXI", utc_ns(2026, 10, 23)), entity_id(CIK))
        self.assertIsNone(self.store.resolve("ticker:Nasdaq", "FXI", utc_ns(2026, 1, 1)))

    def test_acceptance_time_parsing(self):
        self.assertEqual(acceptance_times(SUBMISSIONS)["0000000000-26-000001"], utc_ns(2026, 2, 20, 21, 5))


if __name__ == "__main__":
    unittest.main()
