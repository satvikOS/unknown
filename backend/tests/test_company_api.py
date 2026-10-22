"""Read-only company and macro API routes (route module ``company``). FIXTURE data only.

Handlers are called exactly as ``api/server.py`` dispatches plugin routes:
handler(ctx, parts, query, account_key, conn); LookupError -> 404, ValueError -> 400.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import CIK, fixture_home, ns  # noqa: E402

from unknown.api.routes import company as routes  # noqa: E402
from unknown.data.fundamentals.readonly import ReadOnlyResearchStore  # noqa: E402
from unknown.data.pit import Observation  # noqa: E402
from unknown.data.sources import ecb, treasury  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

HANDLERS = {prefix[0]: fn for prefix, fn in routes.ROUTES}


def get(ctx, path: str, **q):
    """Dispatch like api/server.py and require a strictly JSON-serializable body (no Decimal, date or Fixed)."""
    parts = [p for p in path.split("/") if p][1:]
    body = HANDLERS[parts[0]](ctx, parts, q, "SIM-1", None)
    return json.loads(json.dumps(body, allow_nan=False))


class CompanyApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.store = fixture_home()
        t = ns("2026-08-14T22:30:00Z")
        for tenor, v in (("2Y", "3.90"), ("10Y", "4.25"), ("30Y", "4.60")):
            cls.store.record(Observation(dataset=treasury.DATASET, entity_id=treasury.ENTITY, field=f"par_yield:{tenor}", unit="percent",
                                         value=Fixed.parse(v), period_end="2026-08-14", period_kind="DAY", receipt_time_ns=t,
                                         available_time_ns=t, publication_id="ust-par:2026-08-14", source_ref="FIXTURE"), t)
        for ccy, v in (("USD", "1.1000"), ("JPY", "160.00")):
            cls.store.record(Observation(dataset=ecb.DATASET, entity_id=ecb.entity(ccy), field=ecb.FIELD, unit=f"{ccy}/EUR", currency=ccy,
                                         value=Fixed.parse(v), period_end="2026-08-14", period_kind="DAY", receipt_time_ns=t,
                                         available_time_ns=t, publication_id=f"ecb-exr:{ccy}:2026-08-14", source_ref="FIXTURE"), t)
        cls.ctx = SimpleNamespace(paths=SimpleNamespace(root=cls.root))
        cls.db = cls.root / "research" / "research.sqlite"

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_routes_declared_for_registration(self):
        self.assertEqual([p for p, _ in routes.ROUTES], [("company",), ("macro",)])

    def test_statements_and_metrics_as_of(self):
        s = get(self.ctx, f"/api/company/{CIK}/statements", as_of="2026-08-15", basis="Q", periods="2")
        self.assertEqual(s["as_of"], "2026-08-15T23:59:59.999999999Z")
        self.assertEqual([c["label"] for c in s["columns"]], ["FY2026 Q3", "FY2026 Q2"])
        m = get(self.ctx, f"/api/company/{CIK}/metrics", as_of="2026-08-15", price="50.00", price_currency="USD",
                price_source="FIXTURE price (test only)", price_time="2026-08-14T20:00:00Z")
        pe = next(x for x in m["metrics"] if x["metric"] == "pe_ttm")
        self.assertEqual(pe["status"], "OK")
        self.assertEqual(m["price_input"]["persisted"], False)
        calc = get(self.ctx, f"/api/company/{CIK}/metrics/gross_margin", as_of="2026-08-15")
        self.assertEqual(calc["formula"], "gross_margin@1")
        self.assertTrue(calc["inputs"][0]["cell"]["components"])

    def test_bad_inputs_map_to_400_and_404(self):
        with self.assertRaises(ValueError):
            get(self.ctx, f"/api/company/{CIK}/metrics", price="50", price_currency="USD")            # no source / time
        with self.assertRaises(ValueError):
            get(self.ctx, f"/api/company/{CIK}/metrics", as_of="2026-08-15", price="50", price_currency="USD",
                price_source="x", price_time="2026-08-16T00:00:00Z")                              # look-ahead
        with self.assertRaises(ValueError):
            get(self.ctx, f"/api/company/{CIK}/metrics", as_of="2026-08-15T10:00:00")              # no zone
        with self.assertRaises(ValueError):
            get(self.ctx, "/api/company/AAPL/statements")
        with self.assertRaises(LookupError):
            get(self.ctx, "/api/company/1234/statements")
        with self.assertRaises(LookupError):
            get(self.ctx, f"/api/company/{CIK}/metrics/not_a_metric")
        with self.assertRaises(LookupError):
            get(self.ctx, f"/api/company/{CIK}/nonsense")

    def test_tearsheet_blocks(self):
        t = get(self.ctx, f"/api/company/{CIK}", as_of="2026-08-15")
        b = t["blocks"]
        self.assertEqual(b["identity"]["status"], "OK")
        self.assertEqual(b["statements"]["status"], "OK")
        self.assertEqual(b["quote"]["status"], "NOT_CONNECTED")
        self.assertEqual(b["events"]["status"], "NOT_LICENSED")
        self.assertEqual(b["peers"]["status"], "OK")
        self.assertIn("age_seconds", b["statements"])
        self.assertTrue(b["filings"]["content"]["filings"][0]["links"]["filing_index"].startswith("https://www.sec.gov/Archives/"))
        self.assertEqual(b["description"]["status"], "NOT_AVAILABLE")

    def test_description_link_found_beyond_the_displayed_filings_page(self):
        """Regression (review fix): the latest annual report is looked up over all filings accepted by t. On live SEC data
        insider forms filled the first 20 rows and the block wrongly said no annual report existed."""
        from unknown.data.fundamentals.tearsheet import tearsheet
        t = tearsheet(self.store, f"SEC-CIK-{CIK:010d}", ns("2026-08-15T00:00:00Z"), filings_limit=2)
        shown = [f["form"] for f in t["blocks"]["filings"]["content"]["filings"]]
        self.assertEqual(shown, ["10-Q", "10-Q"])                               # no annual report on the displayed page
        d = t["blocks"]["description"]
        self.assertEqual(d["content"]["accession"], f"{CIK:010d}-25-000010")    # the FY2025 10-K, accepted 2025-10-31
        self.assertTrue(d["content"]["link"].endswith(f"fixture-{CIK:010d}-25-000010.htm"))
        early = tearsheet(self.store, f"SEC-CIK-{CIK:010d}", ns("2024-11-01T00:00:00Z"))
        self.assertEqual(early["blocks"]["description"]["status"], "NOT_AVAILABLE")
        self.assertIn("no annual report", early["blocks"]["description"]["reason"])

    def test_filings_peers_dcf_and_search(self):
        f = get(self.ctx, f"/api/company/{CIK}/filings", as_of="2026-08-15", forms="10-K,10-K/A", limit="5")
        self.assertEqual({x["form"] for x in f["filings"]}, {"10-K", "10-K/A"})
        p = get(self.ctx, f"/api/company/{CIK}/peers", as_of="2026-08-15")
        self.assertEqual(len(p["group"]["members"]), 3)
        d = get(self.ctx, f"/api/company/{CIK}/dcf", as_of="2026-08-15", g="0.02", fcff="100,110,120", rf_tenor="10Y", beta="1.0",
                erp="0.05", kd="0.05", we="0.8", wd="0.2", tax="0.21")
        self.assertEqual(d["inputs"]["rf"]["value"], "0.0425")
        refused = get(self.ctx, f"/api/company/{CIK}/dcf", as_of="2026-08-15", g="0.10", fcff="1", wacc="0.09")
        self.assertEqual(refused["status"], "REFUSED")
        s = get(self.ctx, "/api/company/search", q="FXW", as_of="2026-12-02")
        self.assertIsNone(s["selected"])
        self.assertEqual(s["candidates"][0]["cik"], CIK)

    def test_macro_routes(self):
        curve = get(self.ctx, "/api/macro/yield-curve", as_of="2026-08-15")
        self.assertEqual(curve["curve_date"], "2026-08-14")
        self.assertEqual(curve["status"], "PARTIAL")
        self.assertIn("1M", curve["missing_tenors"])
        self.assertEqual(curve["slope_10y_2y"]["value_bp"], "35.00")
        early = get(self.ctx, "/api/macro/yield-curve", as_of="2026-08-14T22:29:59Z")
        self.assertEqual(early["status"], "UNAVAILABLE")                       # not yet available: no look-ahead
        fx = get(self.ctx, "/api/macro/fx", as_of="2026-08-15", base="USD")
        self.assertEqual(fx["rates"]["USD"]["rate"], "1.1000")
        self.assertEqual(fx["normalized"]["rates"]["JPY"]["value"], "0.0068750000")   # USD per JPY = 1.1 / 160
        self.assertEqual(fx["normalized"]["rates"]["EUR"]["value"], "1.1000000000")
        cat = get(self.ctx, "/api/macro", as_of="2026-08-15")
        self.assertEqual({s["status"] for s in cat["series"]}, {"OK"})
        self.assertEqual({s["last_date"] for s in cat["series"]}, {"2026-08-14"})
        # Regression (review fix): a past as_of never reveals data that became available later.
        past = get(self.ctx, "/api/macro", as_of="2026-08-14T22:29:59Z")
        self.assertEqual([(s["observations"], s["last_date"], s["status"]) for s in past["series"]],
                         [(0, None, "NOT_AVAILABLE_AT_AS_OF"), (0, None, "NOT_AVAILABLE_AT_AS_OF")])
        self.assertTrue(all(s["reason"] for s in past["series"]))
        self.assertEqual(curve["slope_10y_2y"]["inputs"], [next(p["obs_id"] for p in curve["points"] if p["tenor"] == t)
                                                          for t in ("10Y", "2Y")])

    def logical_digest(self) -> str:
        """Digest of the database *content* through a fresh read-only connection. Hashing the main file alone is
        vacuous: the store runs in WAL mode, so a committed write lands in research.sqlite-wal and leaves the main
        file byte-identical (review finding)."""
        conn = sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            h = hashlib.sha256()
            for line in conn.iterdump():
                h.update(line.encode())
            return h.hexdigest()
        finally:
            conn.close()

    def test_routes_never_write(self):
        before = self.logical_digest()
        get(self.ctx, f"/api/company/{CIK}", as_of="2026-08-15")
        get(self.ctx, f"/api/company/{CIK}/metrics", as_of="2026-08-15", price="50.00", price_currency="USD",
            price_source="FIXTURE price (test only)", price_time="2026-08-14T20:00:00Z")
        get(self.ctx, f"/api/company/{CIK}/peers", as_of="2026-08-15")
        get(self.ctx, f"/api/company/{CIK}/dcf", as_of="2026-08-15", g="0.02", fcff="100,110", wacc="0.09")
        get(self.ctx, "/api/company/search", q="FXW", as_of="2026-08-15")
        get(self.ctx, "/api/macro/fx", as_of="2026-08-15", base="USD")
        self.assertEqual(self.logical_digest(), before)
        # control: the digest does see a committed write (a WAL-mode write that the old main-file hash could miss)
        t = ns("2026-08-14T22:30:00Z")
        obs_id, _ = self.store.record(Observation(dataset="fixture.control", entity_id="FIXTURE-CONTROL", field="x", unit="u",
                                                  value=Fixed(1), receipt_time_ns=t, available_time_ns=t, source_ref="FIXTURE"), t)
        try:
            self.assertNotEqual(self.logical_digest(), before)
        finally:
            self.store.conn.execute("DELETE FROM observation WHERE obs_id=?", (obs_id,))
        self.assertEqual(self.logical_digest(), before)
        ro = ReadOnlyResearchStore(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            ro.upsert_entity("X", "issuer", "x")
        ro.close()

    def test_unavailable_without_research_store(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = SimpleNamespace(paths=SimpleNamespace(root=Path(d)))
            self.assertEqual(get(ctx, f"/api/company/{CIK}/statements")["status"], "UNAVAILABLE")
            self.assertEqual(get(ctx, "/api/macro/fx")["status"], "UNAVAILABLE")
            r = get(ctx, "/api/company/search", q="FXW")
            self.assertEqual(r["candidates"], [])


if __name__ == "__main__":
    unittest.main()
