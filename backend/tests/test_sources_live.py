"""OPT-IN live smoke tests against the free official endpoints (CAP-DATA-04, CAP-DATA-06, CAP-DATA-07, CAP-INS-01).

Skipped unless ``UNKNOWN_LIVE_SOURCES=1``. Each test makes exactly one request with the declared
User-Agent from ``~/.unknown/config/data.json`` (``data_user_agent`` or ``sec_user_agent``), within the
registry rate limit, and writes only to a temporary store (never the owner's research store).
The OpenFIGI test sends one public ISIN without a key unless ``openfigi_api_key`` is configured.

  UNKNOWN_LIVE_SOURCES=1 PYTHONPATH=src python3 -m unittest tests.test_sources_live -v
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from unknown.data.pit import ResearchStore
from unknown.data.sources import ecb, openfigi, sec_tickers, treasury
from unknown.data.sources.common import data_config, declared_user_agent, make_fetcher
from unknown.data.sources.identifiers import figi_format_ok

LIVE = os.environ.get("UNKNOWN_LIVE_SOURCES") == "1"


@unittest.skipUnless(LIVE, "live smoke tests are opt-in: set UNKNOWN_LIVE_SOURCES=1")
class LiveSmokeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = data_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.tmp.name) / "research.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_treasury_par_curve_current_month(self):
        month = time.strftime("%Y%m", time.gmtime())
        out = treasury.ingest(self.store, make_fetcher(treasury.PROVIDER, self.cfg, max_retries=1), time.time_ns, month=month)
        print("\nTREASURY", {k: out[k] for k in ("url", "curve_dates", "first_date", "last_date", "NEW", "partial_dates")})
        self.assertGreater(out["curve_dates"], 0)
        curve = treasury.curve_as_of(self.store, time.time_ns())
        self.assertIn(curve["status"], ("OK", "PARTIAL"))
        ten = next(p for p in curve["points"] if p["tenor"] == "10Y")
        print("TREASURY 10Y", curve["curve_date"], ten["value_pct"])

    def test_ecb_reference_rates_last_observation(self):
        out = ecb.ingest(self.store, make_fetcher(ecb.PROVIDER, self.cfg, max_retries=1), time.time_ns, last_n=1)
        print("\nECB", {k: out[k] for k in ("url", "currencies", "rows", "skipped", "last_date", "NEW")})
        self.assertGreater(out["currencies"], 10)
        rates = ecb.rates_as_of(self.store, time.time_ns())
        print("ECB USD", rates["rates"]["USD"]["reference_date"], rates["rates"]["USD"]["rate"])

    def test_openfigi_one_isin(self):
        client = openfigi.OpenFigiClient(declared_user_agent(self.cfg), self.cfg.get("openfigi_api_key"), max_retries=1)
        out = openfigi.ingest_mapping(self.store, client, [{"idType": "ID_ISIN", "idValue": "US0378331005"}], time.time_ns)
        print("\nOPENFIGI", {k: out[k] for k in ("requests_sent", "keyed", "statuses")})
        self.assertEqual(out["requests_sent"], 1)
        outcome = openfigi.mappings_as_of(self.store, time.time_ns())[0]["outcome"]
        self.assertIn(outcome["status"], ("MAPPED", "MULTIPLE_CANDIDATES"))
        self.assertTrue(all(figi_format_ok(c["figi"]) for c in outcome["candidates"]))
        print("OPENFIGI candidates", len(outcome["candidates"]), sorted({c["exchCode"] for c in outcome["candidates"]})[:8])

    def test_sec_company_tickers_file(self):
        out = sec_tickers.ingest(self.store, make_fetcher(sec_tickers.PROVIDER, self.cfg, max_retries=1), time.time_ns)
        print("\nSEC TICKERS", {k: out[k] for k in ("rows", "companies", "outcome", "receipt_time")})
        self.assertGreater(out["rows"], 1000)
        row = self.store.conn.execute("SELECT ticker FROM ref_sec_ticker WHERE cik=320193").fetchall()
        self.assertTrue(row)


if __name__ == "__main__":
    unittest.main()
