"""Peer groups (CAP-CO-10), company/instrument search (CAP-INS-01, CAP-CO-01) and filings (CAP-CO-02). FIXTURE data."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import CIK, INGESTED_AT, accepted, fixture_home, ns  # noqa: E402

from unknown.data.fundamentals.filings import filings, identity, load_submissions  # noqa: E402
from unknown.data.fundamentals.metrics import PriceInput  # noqa: E402
from unknown.data.fundamentals.peers import comps, peer_group, percentile_inc, statistics  # noqa: E402
from unknown.data.fundamentals.search import search  # noqa: E402
from unknown.data.sources import sec_tickers  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

E = f"SEC-CIK-{CIK:010d}"
T = ns("2026-08-15T00:00:00Z")
TICKERS_JSON = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
    [CIK, "Fixture Widgets Inc (synthetic)", "FXW", "Nasdaq"], [9000002, "Fixture Peer Two (synthetic)", "FXP2", "NYSE"],
    [9100001, "Fixture Not Ingested Corp (synthetic)", "FXNI", "Nasdaq"], [9100002, "Widgetry Holdings (synthetic)", "WDGT", "NYSE"]]}).encode()


class FakeFetcher:
    def __init__(self, body: bytes, t: int):
        self.body, self.t = body, t

    def get(self, url):
        from unknown.data.http import Response
        return Response(url, 200, self.body, "application/json", self.t)


class PeersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.store = fixture_home()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_default_peers_are_deterministic_by_sic(self):
        a = peer_group(self.store, E, T)
        b = peer_group(self.store, E, T)
        self.assertEqual(a, b)
        self.assertEqual(a["rule"]["id"], "peers_sic4@1")
        self.assertEqual([m["cik"] for m in a["members"]], [9000002, 9000003, 9000004])   # SIC 3571 only, by CIK
        self.assertTrue(any("no SIC history" in c for c in a["caveats"]))
        self.assertTrue(any("survivorship" in c for c in a["caveats"]))
        self.assertIn("not applied", a["rule"]["size_band"])

    def test_size_band_with_market_caps(self):
        caps = {E: Fixed(1000), "SEC-CIK-0009000002": Fixed(2000), "SEC-CIK-0009000003": Fixed(5000)}
        g = peer_group(self.store, E, T, market_caps=caps)
        self.assertEqual([m["cik"] for m in g["members"]], [9000002])
        self.assertEqual({x["entity_id"] for x in g["excluded"]}, {"SEC-CIK-0009000003", "SEC-CIK-0009000004"})

    def test_owner_list_and_comps_statistics(self):
        g = peer_group(self.store, E, T, explicit=["SEC-CIK-0009000002", "SEC-CIK-0009000005", "SEC-CIK-0001234567"])
        self.assertEqual(g["rule"]["id"], "owner_list@1")
        self.assertEqual(g["rule"]["not_ingested"], ["SEC-CIK-0001234567"])
        default = peer_group(self.store, E, T)
        price = PriceInput(Fixed.parse("50"), "USD", "FIXTURE price", T - 10**12)
        table = comps(self.store, E, default, T, prices={E: price, "SEC-CIK-0009000004": PriceInput(Fixed.parse("5"), "USD", "FIXTURE", T - 10**12)})
        nm = table["peer_statistics"]["net_margin"]
        self.assertEqual((nm["n_used"], nm["n_nm"], nm["suppressed"]), (3, 0, False))
        pe = table["peer_statistics"]["pe_ttm"]
        self.assertEqual(pe["n_nm"], 1)                       # the loss maker's P/E is NM and excluded
        self.assertTrue(pe["suppressed"])                     # only peers with prices count; fewer than three
        target = table["rows"][0]
        self.assertEqual(target["role"], "target")
        self.assertEqual(target["values"]["pe_ttm"]["status"], "OK")

    def test_comps_values_carry_lineage_and_flag_members_without_fundamentals(self):
        """Regression (review fix): every comps value carries formula, input observation ids and source dates; a member
        with no fundamentals at the as-of time is labelled and excluded from statistics (CAP-CO-10 degraded behaviour)."""
        late = "SEC-CIK-0009000077"
        self.store.upsert_entity(late, "issuer", "Fixture Registered Later (synthetic)", {"cik": 9000077, "sic": "9999"})
        g = peer_group(self.store, E, T, explicit=["SEC-CIK-0009000002", late])
        table = comps(self.store, E, g, T, metrics=("net_margin", "revenue_ttm"))
        rows = {r["entity_id"]: r for r in table["rows"]}
        nm = rows["SEC-CIK-0009000002"]["values"]["net_margin"]
        self.assertEqual((nm["status"], nm["formula"]), ("OK", "net_margin@1"))
        self.assertTrue(nm["input_obs_ids"])
        self.assertTrue(all(d <= "2026-08-15" for d in nm["source_dates"]))
        self.assertEqual(rows["SEC-CIK-0009000002"]["fundamentals"]["status"], "OK")
        self.assertEqual(rows[late]["fundamentals"]["status"], "NO_FUNDAMENTALS_AT_AS_OF")
        self.assertEqual(rows[late]["values"]["net_margin"]["input_obs_ids"], [])
        self.assertEqual(table["peer_statistics"]["net_margin"]["n_missing"], 1)
        self.assertEqual(table["peer_statistics"]["net_margin"]["n_used"], 1)

    def test_percentiles_exact(self):
        v = [Fixed(1), Fixed(2), Fixed(3), Fixed(4)]
        self.assertEqual(percentile_inc(v, 1), Fixed.parse("1.75"))
        self.assertEqual(percentile_inc(v, 2), Fixed.parse("2.5"))
        self.assertEqual(percentile_inc(v, 3), Fixed.parse("3.25"))
        s = statistics(v, 1, 0)
        self.assertEqual((s["median"], s["mean"], s["n_nm"]), ("2.50", "2.5000000000", 1))
        self.assertTrue(statistics(v[:2], 0, 0)["suppressed"])


class SearchAndFilingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.store = fixture_home()
        cls.snap_t = ns("2026-08-01T00:00:00Z")
        sec_tickers.ingest(cls.store, FakeFetcher(TICKERS_JSON, cls.snap_t))
        cls.core = sqlite3.connect(":memory:")
        cls.core.row_factory = sqlite3.Row
        cls.core.executescript("CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY, symbol TEXT, body TEXT, metadata_version INTEGER);"
                               "CREATE TABLE provider_mappings (provider TEXT, external_id TEXT, version INTEGER, instrument_id TEXT, row TEXT);")
        cls.core.execute("INSERT INTO instruments VALUES ('FIX-EQ-1','FXW',?,1)",
                         (json.dumps({"name": "Fixture Widgets common (test only)", "asset_class": "EQUITY", "venue_mic": "XNAS",
                                      "currency": "USD", "status": "ACTIVE"}),))
        cls.core.execute("INSERT INTO provider_mappings VALUES ('ibkr','999001',1,'FIX-EQ-1','{}')")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.core.close()
        cls.tmp.cleanup()

    def test_exact_ticker_ranks_first_and_never_selects(self):
        r = search("FXW", as_of_ns=T, store=self.store, core_conn=self.core)
        self.assertIsNone(r["selected"])
        first = r["candidates"][0]
        self.assertEqual(first["match"], "EXACT_TICKER")
        kinds = {c["kind"] for c in r["candidates"]}
        self.assertEqual(kinds, {"issuer", "instrument"})
        issuer = next(c for c in r["candidates"] if c["kind"] == "issuer")
        self.assertTrue(issuer["ingested"])
        # submissions ticker aliases are recorded from retrieval (INGESTED_AT) and never backdated
        self.assertEqual({li["source"] for li in issuer["listings"]}, {"sec.company_tickers_exchange"})
        later = search("FXW", as_of_ns=INGESTED_AT, store=self.store)
        issuer_later = next(c for c in later["candidates"] if c["kind"] == "issuer")
        self.assertEqual({li["source"] for li in issuer_later["listings"]}, {"sec.company_tickers_exchange", "sec.submissions"})

    def test_cik_name_and_former_name_matches(self):
        self.assertEqual(search(str(CIK), as_of_ns=T, store=self.store)["candidates"][0]["match"], "EXACT_ID")
        widg = search("widget", as_of_ns=T, store=self.store)
        names = [(c["name"], c["match"]) for c in widg["candidates"]]
        self.assertIn(("Widgetry Holdings (synthetic)", "NAME_PREFIX"), names)
        self.assertIn(("Fixture Widgets Inc (synthetic)", "NAME_PREFIX"), names)
        not_ing = next(c for c in search("FXNI", as_of_ns=T, store=self.store)["candidates"])
        self.assertFalse(not_ing["ingested"])
        old = search("OLD NAME", as_of_ns=T, store=self.store)
        self.assertTrue(old["candidates"])
        self.assertTrue(all(c["match"] == "FORMER_NAME" for c in old["candidates"]))

    def test_point_in_time_snapshot_and_licensed_ids(self):
        before = search("FXNI", as_of_ns=self.snap_t - 1, store=self.store)
        self.assertEqual(before["candidates"], [])
        src = next(s for s in before["sources"] if s["source"] == "sec.company_tickers_exchange")
        self.assertEqual(src["status"], "NOT_AVAILABLE")
        self.assertEqual(search("037833100", as_of_ns=T, store=self.store)["query_type"], "CIK")    # digits read as a CIK
        refused = search("B0YBKJ7", as_of_ns=T, store=self.store)
        self.assertEqual(refused["sources"][0]["status"], "REFUSED")
        self.assertEqual(search("ibkr-none", as_of_ns=T, store=self.store, core_conn=None)["sources"][-1]["status"], "NOT_AVAILABLE")
        self.assertEqual(search("999001", as_of_ns=T, store=self.store, core_conn=self.core)["candidates"][0]["kind"], "instrument")
        with self.assertRaises(ValueError):
            search("", as_of_ns=T, store=self.store)

    def test_openfigi_outcomes_are_searchable_as_of_receipt(self):
        """The OpenFIGI branch of search (previously untested): stored outcomes match by ISIN / FIGI as EXACT_ID, never
        auto-select, and are invisible before their receipt time. FIXTURE responses via the real ingest path."""
        from test_sources_openfigi import UA, Clock, FigiOpener

        from unknown.data.sources import openfigi
        clock = Clock()
        client = openfigi.OpenFigiClient(UA, None, opener=FigiOpener(clock), monotonic=clock.monotonic, sleep=clock.sleep,
                                         clock_ns=lambda: T - 10**9)
        openfigi.ingest_mapping(self.store, client, [{"idType": "ID_ISIN", "idValue": "US0378331005"}], lambda: T - 10**9)
        r = search("US0378331005", as_of_ns=T, store=self.store)
        figi = [c for c in r["candidates"] if c["kind"] == "figi_listing"]
        self.assertEqual(sorted(c["figi"] for c in figi), ["BBG000B9XRY4", "BBG000B9Y5X2"])
        self.assertTrue(all(c["match"] == "EXACT_ID" and c["mapping_status"] == "MULTIPLE_CANDIDATES" for c in figi))
        self.assertIsNone(r["selected"])
        self.assertEqual(search("BBG000B9Y5X2", as_of_ns=T, store=self.store)["candidates"][0]["figi"], "BBG000B9Y5X2")
        before = search("US0378331005", as_of_ns=T - 10**9 - 1, store=self.store)
        self.assertEqual([c for c in before["candidates"] if c["kind"] == "figi_listing"], [])

    def test_snapshot_dedup_and_changes(self):
        later = ns("2026-08-02T00:00:00Z")
        out = sec_tickers.ingest(self.store, FakeFetcher(TICKERS_JSON, later))
        self.assertEqual(out["outcome"], "UNCHANGED")
        changed = json.loads(TICKERS_JSON)
        changed["data"][3][2] = "WDG"
        out2 = sec_tickers.ingest(self.store, FakeFetcher(json.dumps(changed).encode(), ns("2026-08-03T00:00:00Z")))
        diff = sec_tickers.changes(self.store.conn, 1, out2["snapshot_id"])
        self.assertEqual([a["ticker"] for a in diff["added"]], ["WDG"])
        self.assertEqual([a["ticker"] for a in diff["removed"]], ["WDGT"])
        snap = sec_tickers.snapshot_at(self.store.conn, later)
        self.assertEqual(snap["rows_snapshot_id"], 1)

    def test_filings_point_in_time_with_deep_links(self):
        subs, meta = load_submissions(self.store, E)
        self.assertEqual(meta["status"], "OK")
        t = accepted(CIK, "10-K/A", 2025, None)
        fl = filings(subs, t - 1)
        accns = [f["accession"] for f in fl["filings"]]
        self.assertNotIn(f"{CIK:010d}-26-000005", accns)            # the 10-K/A is not yet accepted
        self.assertGreater(fl["not_yet_accepted_at_as_of"], 0)
        eight_k = next(f for f in fl["filings"] if f["form"] == "8-K")
        self.assertTrue(any("NON_RELIANCE" in x for x in eight_k["flags"]))
        self.assertEqual(eight_k["links"]["filing_index"],
                         f"https://www.sec.gov/Archives/edgar/data/{CIK}/{eight_k['accession'].replace('-', '')}/{eight_k['accession']}-index.htm")
        only_k = filings(subs, INGESTED_AT, forms={"10-K"})
        self.assertTrue(all(f["form"] == "10-K" for f in only_k["filings"]))
        ident = identity(subs, meta, ns("2020-06-01T00:00:00Z"))
        self.assertEqual(ident["name_as_of"], "Fixture Widgets Inc (synthetic) OLD NAME (synthetic)")
        self.assertEqual(ident["sic"], "3571")
        self.assertTrue(any("retroactively" in c for c in ident["caveats"]))


if __name__ == "__main__":
    unittest.main()
