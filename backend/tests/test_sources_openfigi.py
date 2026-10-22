"""OpenFIGI mapping client and identifier validation (CAP-DATA-04, CAP-INS-02, CAP-INS-01). No network: a fake
opener returns FIXTURE responses shaped like the documented API. Identifiers used are public examples
(Apple's ISIN, CUSIP and FIGI; a textbook SEDOL) for check-digit tests only."""
from __future__ import annotations

import io
import json
import unittest
import urllib.error

from unknown.data.http import FetchError
from unknown.data.pit import ResearchStore
from unknown.data.registry import dataset, load_registry
from unknown.data.sources import openfigi
from unknown.data.sources.identifiers import classify, cusip_valid, figi_format_ok, isin_valid, sedol_valid
from unknown.domain.clock import utc_ns

UA = "Fixture Owner owner@example.test"


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Resp:
    def __init__(self, body):
        self.body, self.status, self.headers = body, 200, {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FigiOpener:
    """Answers each job: ISIN US0378331005 -> two listings, TICKER FXW -> one listing, anything else -> no match."""

    def __init__(self, clock: Clock, fail: list | None = None, max_jobs: int | None = None):
        self.clock, self.fail, self.max_jobs = clock, list(fail or []), max_jobs
        self.requests = []

    def __call__(self, req, timeout):
        jobs = json.loads(req.data)
        self.requests.append({"t": self.clock.t, "jobs": jobs, "headers": dict(req.header_items())})
        if self.fail:
            code, headers = self.fail.pop(0)
            raise urllib.error.HTTPError(req.full_url, code, "fixture", headers, io.BytesIO(b""))
        if self.max_jobs is not None and len(jobs) > self.max_jobs:
            raise urllib.error.HTTPError(req.full_url, 413, "too large", {}, io.BytesIO(b""))
        out = []
        for j in jobs:
            if j["idValue"] == "US0378331005":
                out.append({"data": [{"figi": "BBG000B9XRY4", "compositeFIGI": "BBG000B9XRY4", "name": "FIXTURE LISTING A", "ticker": "FXA",
                                      "exchCode": "US", "marketSector": "Equity", "securityType": "Common Stock"},
                                     {"figi": "BBG000B9Y5X2", "compositeFIGI": "BBG000B9XRY4", "name": "FIXTURE LISTING A", "ticker": "FXA",
                                      "exchCode": "UW", "marketSector": "Equity", "securityType": "Common Stock"}]})
            elif j["idValue"] == "FXW":
                out.append({"data": [{"figi": "BBG00FXTR001", "name": "FIXTURE WIDGETS", "ticker": "FXW", "exchCode": "US"}]})
            elif j["idValue"] == "BADJOB":
                out.append({"error": "Invalid idValue format."})
            else:
                out.append({"warning": "No identifier found."})
        return Resp(json.dumps(out).encode())


class IdentifierTests(unittest.TestCase):
    def test_check_digits(self):
        self.assertTrue(isin_valid("US0378331005"))
        self.assertFalse(isin_valid("US0378331006"))
        self.assertFalse(isin_valid("us0378331005"))
        self.assertTrue(cusip_valid("037833100"))
        self.assertFalse(cusip_valid("037833101"))
        self.assertTrue(sedol_valid("B0YBKJ7"))
        self.assertTrue(sedol_valid("0263494"))
        self.assertFalse(sedol_valid("0263495"))
        self.assertTrue(figi_format_ok("BBG000B9XRY4"))
        self.assertFalse(figi_format_ok("BBA000B9XRY4"))      # vowel
        self.assertFalse(figi_format_ok("GGG000B9XRY4"))      # reserved prefix

    def test_classify(self):
        self.assertEqual(classify("US0378331005"), "ISIN")
        self.assertEqual(classify("BBG000B9XRY4"), "FIGI")
        self.assertEqual(classify("0000320193"), "CIK")
        self.assertEqual(classify("B0YBKJ7"), "LICENSED_ID")
        self.assertEqual(classify("BRK.B"), "TICKER")
        self.assertEqual(classify("apple inc"), "NAME")


class OpenFigiTests(unittest.TestCase):
    def client(self, opener, clock, key=None):
        return openfigi.OpenFigiClient(UA, key, opener=opener, monotonic=clock.monotonic, sleep=clock.sleep,
                                       clock_ns=lambda: utc_ns(2026, 10, 22, 12))

    def test_registry_entry(self):
        reg = load_registry()
        self.assertEqual((reg["openfigi"].rate_limit.max_requests, reg["openfigi"].rate_limit.per_seconds), (25, 60.0))
        self.assertTrue(dataset(reg, openfigi.DATASET).permits("model_processing_cloud"))

    def test_invalid_and_licensed_inputs_rejected_before_any_call(self):
        clock = Clock()
        opener = FigiOpener(clock)
        c = self.client(opener, clock)
        for job in ({"idType": "ID_ISIN", "idValue": "US0378331006"}, {"idType": "ID_CUSIP", "idValue": "037833100"},
                    {"idType": "ID_SEDOL", "idValue": "B0YBKJ7"}, {"idType": "NOPE", "idValue": "x"},
                    {"idType": "TICKER", "idValue": "FXW", "bogus": 1}, {"idType": "ID_BB_GLOBAL", "idValue": "BBA000B9XRY4"}):
            with self.assertRaises(openfigi.JobError):
                c.map([{"idType": "TICKER", "idValue": "FXW"}, job])
        self.assertEqual(opener.requests, [])                  # nothing sent, not even the valid job
        ok = c.map([{"idType": "ID_CUSIP", "idValue": "037833100"}], allow_licensed=True)
        self.assertEqual(len(ok), 1)

    def test_batching_and_sliding_window_without_key(self):
        clock = Clock()
        opener = FigiOpener(clock)
        c = self.client(opener, clock)
        jobs = [{"idType": "TICKER", "idValue": f"T{i}"} for i in range(265)]
        batches = c.map(jobs)
        self.assertEqual([len(b.jobs) for b in batches][:3], [10, 10, 10])
        self.assertEqual(len(batches), 27)
        times = [r["t"] for r in opener.requests]
        for i in range(len(times)):                            # never more than 25 sends in any 60 s window
            self.assertLessEqual(sum(1 for t in times if times[i] <= t < times[i] + 60), 25)
        self.assertGreaterEqual(times[25], 60.0)
        self.assertNotIn("X-openfigi-apikey", opener.requests[0]["headers"])

    def test_key_raises_limits_and_is_sent_only_as_header(self):
        clock = Clock()
        opener = FigiOpener(clock)
        c = self.client(opener, clock, key="fixture-key-not-real")
        batches = c.map([{"idType": "TICKER", "idValue": f"T{i}"} for i in range(150)])
        self.assertEqual([len(b.jobs) for b in batches], [100, 50])
        self.assertEqual(opener.requests[0]["headers"].get("X-openfigi-apikey"), "fixture-key-not-real")
        s = ResearchStore(":memory:")
        openfigi.ingest_mapping(s, c, [{"idType": "TICKER", "idValue": "FXW"}], lambda: utc_ns(2026, 10, 22, 12))
        raw = [r["request"] for r in s.conn.execute("SELECT request FROM raw_object")]
        self.assertFalse(any("fixture-key-not-real" in r for r in raw))
        s.close()

    def test_429_waits_retry_after_and_413_splits(self):
        clock = Clock()
        opener = FigiOpener(clock, fail=[(429, {"Retry-After": "7"})])
        c = self.client(opener, clock)
        c.map([{"idType": "TICKER", "idValue": "FXW"}])
        self.assertIn(7.0, clock.slept)
        self.assertGreaterEqual(opener.requests[1]["t"] - opener.requests[0]["t"], 7.0)
        clock2 = Clock()
        opener2 = FigiOpener(clock2, max_jobs=4)
        batches = self.client(opener2, clock2).map([{"idType": "TICKER", "idValue": f"T{i}"} for i in range(10)])
        self.assertEqual([len(b.jobs) for b in batches], [2, 3, 2, 3])      # 10 -> 413 -> 5 + 5 -> 413 -> 2 + 3 each
        self.assertEqual([j["idValue"] for b in batches for j in b.jobs], [f"T{i}" for i in range(10)])
        clock3 = Clock()
        with self.assertRaises(FetchError):
            self.client(FigiOpener(clock3, fail=[(403, {})]), clock3).map([{"idType": "TICKER", "idValue": "FXW"}])

    def test_ingest_stores_every_outcome_and_never_auto_selects(self):
        clock = Clock()
        s = ResearchStore(":memory:")
        c = self.client(FigiOpener(clock), clock)
        jobs = [{"idType": "ID_ISIN", "idValue": "US0378331005", "exchCode": "US"}, {"idType": "TICKER", "idValue": "FXW"},
                {"idType": "TICKER", "idValue": "NOSUCH"}, {"idType": "TICKER", "idValue": "BADJOB"}]
        out = openfigi.ingest_mapping(s, c, jobs, lambda: utc_ns(2026, 10, 22, 12))
        self.assertEqual(out["statuses"], {"MULTIPLE_CANDIDATES": 1, "MAPPED": 1, "NO_MATCH": 1, "ERROR": 1})
        stored = {m["job"]: m["outcome"] for m in openfigi.mappings_as_of(s, utc_ns(2026, 10, 22, 12))}
        multi = stored["OPENFIGI-JOB:ID_ISIN:US0378331005:exchCode=US"]
        self.assertIsNone(multi["selected"])
        self.assertEqual(multi["preferred_by_exchange"], "BBG000B9XRY4")
        self.assertEqual(stored["OPENFIGI-JOB:TICKER:NOSUCH"]["status"], "NO_MATCH")
        self.assertEqual(s.resolve("figi", "BBG000B9XRY4", utc_ns(2026, 10, 22, 12)), "FIGI-BBG000B9XRY4")
        self.assertEqual(openfigi.mappings_as_of(s, utc_ns(2026, 10, 22, 11)), [])        # nothing before receipt
        again = openfigi.ingest_mapping(s, c, jobs, lambda: utc_ns(2026, 10, 23, 12))
        self.assertEqual(again["UNCHANGED"], 4)
        s.close()


if __name__ == "__main__":
    unittest.main()
