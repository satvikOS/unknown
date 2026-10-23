"""EU issuers (CAP-CO-03, CAP-DATA-03): ESEF facts from filings.xbrl.org, the esef-ifrs-map@1 statements, the
ESEF/SEC side-by-side comparison, ESMA FIRDS listings, search and the /api/company/eu routes. No network: the
JSON:API, xBRL-JSON and auth.017 documents are SYNTHETIC with the documented shapes; the issuer, LEI, ISIN and
figures are invented."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from unknown.api.routes import company as routes
from unknown.data.fundamentals.ifrs import IFRS_ITEMS, EsefStatements, compare, validate_ifrs_mapping
from unknown.data.fundamentals.mapping import ITEMS
from unknown.data.fundamentals.search import search
from unknown.data.http import Response
from unknown.data.pit import ResearchStore
from unknown.data.sources import esef, firds
from unknown.data.sources.identifiers import isin_valid
from unknown.domain.clock import utc_ns


def with_check_digits(base18: str) -> str:
    n = int("".join(str(int(c, 36)) for c in base18 + "00"))
    return base18 + f"{98 - n % 97:02d}"


LEI = with_check_digits("FIXTURE00000000001")
OTHER_LEI = with_check_digits("FIXTURE00000000002")
ISIN = "NL0000000006" if isin_valid("NL0000000006") else next(f"NL000000000{d}" for d in range(10) if isin_valid(f"NL000000000{d}"))


def fact(concept, period, value, unit="iso4217:EUR", **dims):
    return {"value": value, "decimals": -5, "dimensions": {"concept": concept, "entity": f"scheme:{LEI}", "period": period, "unit": unit, **dims}}


FY24, FY25 = "2024-01-01T00:00:00/2025-01-01T00:00:00", "2025-01-01T00:00:00/2026-01-01T00:00:00"


def oim(facts: dict) -> bytes:
    return json.dumps({"documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"}, "facts": facts}).encode()


REPORT_2024 = oim({
    "f1": fact("ifrs-full:Revenue", FY24, "1000.0"),
    "f2": fact("ifrs-full:ProfitLoss", FY24, "100.0"),
    "f3": fact("ifrs-full:Assets", "2025-01-01T00:00:00", "5000.0"),
    "f4": fact("ifrs-full:Revenue", FY24, "1000.0"),                                     # duplicate, same value
    "f5": fact("ifrs-full:Revenue", FY24, "600.0", **{"ifrs-full:SegmentsAxis": "x:A"}),  # dimensional: excluded
    "f6": fact("fix:NetSystemSales", FY24, "700.0"),                                      # extension: excluded
    "f7": fact("ifrs-full:CashFlowsFromUsedInOperatingActivities", FY24, "150.0"),
    "f8": fact("ifrs-full:NumberOfSharesOutstanding", "2025-01-01T00:00:00", "10.0", unit="xbrli:shares"),
})
REPORT_2025 = oim({
    "g1": fact("ifrs-full:Revenue", FY25, "1200.0"),
    "g2": fact("ifrs-full:Revenue", FY24, "1010.0"),                                      # restated comparative
    "g3": fact("ifrs-full:ProfitLossAttributableToOwnersOfParent", FY25, "130.0"),
    "g4": fact("ifrs-full:ProfitLoss", FY25, "135.0"),
    "g5": fact("ifrs-full:Assets", "2026-01-01T00:00:00", "5600.0"),
    "g6": fact("ifrs-full:EquityAttributableToOwnersOfParent", "2026-01-01T00:00:00", "2500.0"),
    "g7": fact("ifrs-full:BasicEarningsLossPerShare", FY25, "13.00", unit="iso4217:EUR/xbrli:shares"),
})


def filing(fid, period_end, country, published, json_url):
    return {"type": "filing", "id": fid, "attributes": {
        "fxo_id": f"{LEI}-{period_end}-ESEF-{country}-0", "period_end": period_end, "country": country, "published": published,
        "date_added": "2026-04-01 00:00:00", "json_url": json_url, "viewer_url": f"/{LEI}/{period_end}/viewer.html", "error_count": 0}}


FILINGS = json.dumps({"data": [
    filing("1", "2024-12-31", "NL", "2025-03-01 09:00:00", "/r2024.json"),
    filing("2", "2025-12-31", "NL", None, "/r2025.json"),
    filing("3", "2025-12-31", "GB", "2026-03-02 09:00:00", "/r2025gb.json")], "links": {}}).encode()
ENTITY = json.dumps({"data": {"attributes": {"identifier": LEI, "name": "FIXTURE EUROPA N.V."}}}).encode()
EMPTY = json.dumps({"data": [], "meta": {"count": 0}, "links": {}}).encode()
OTHER_ENTITY = json.dumps({"data": {"attributes": {"identifier": OTHER_LEI, "name": "FIXTURE DEUTSCHLAND SE"}}}).encode()


def auth017(*records) -> bytes:
    ns = "urn:iso:std:iso:20022:tech:xsd:auth.017.001.02"
    body = "".join(
        f"<RefData><FinInstrmGnlAttrbts><Id>{i}</Id><FullNm>{n}</FullNm><ShrtNm>{s}</ShrtNm><ClssfctnTp>ESVUFR</ClssfctnTp>"
        f"<NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts><Issr>{lei}</Issr><TradgVnRltdAttrbts><Id>{mic}</Id>"
        f"<FrstTradDt>2001-01-02T00:00:00Z</FrstTradDt></TradgVnRltdAttrbts></RefData>" for i, n, s, lei, mic in records)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><BizData><Pyld><Document xmlns="{ns}"><FinInstrmRptgRefDataRpt>{body}'
    xml += "</FinInstrmRptgRefDataRpt></Document></Pyld></BizData>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("FULINS_E_20261018_01of01.xml", xml)
    return buf.getvalue()


class Stub:
    def __init__(self, routes_: dict[str, bytes], receipt_ns: int):
        self.routes, self.receipt_ns, self.urls = routes_, receipt_ns, []

    def get(self, url):
        self.urls.append(url)
        for k in sorted(self.routes, key=len, reverse=True):
            if url.endswith(k) or k in url:
                return Response(url, 200, self.routes[k], "application/json", self.receipt_ns)
        raise AssertionError(f"unexpected url {url}")


RECEIPT = utc_ns(2026, 10, 23, 12)


def esef_stub():
    return Stub({f"/api/entities/{LEI}": ENTITY, f"/api/entities/{LEI}/filings": FILINGS, "/r2024.json": REPORT_2024,
                 "/r2025.json": REPORT_2025, f"/api/entities/{OTHER_LEI}": OTHER_ENTITY, f"/api/entities/{OTHER_LEI}/filings": EMPTY}, RECEIPT)


def tick():
    t = [RECEIPT]

    def now():
        t[0] += 1
        return t[0]
    return now


class EsefTests(unittest.TestCase):
    def setUp(self):
        self.s = ResearchStore(":memory:")
        self.addCleanup(self.s.close)

    def test_lei_check_digits(self):
        self.assertTrue(esef.lei_valid(LEI))
        self.assertTrue(esef.lei_valid("724500Y6DUVHQD6OXN27"))   # ASML Holding N.V., public LEI record
        self.assertFalse(esef.lei_valid(LEI[:-1] + str((int(LEI[-1]) + 1) % 10)))

    def test_mapping_keys_line_up_with_the_sec_mapping(self):
        self.assertEqual(validate_ifrs_mapping(), [])
        for i in IFRS_ITEMS:
            self.assertIn(i.key, ITEMS)
        self.assertTrue({"revenue", "operating_income", "net_income", "total_assets", "equity_parent", "cfo",
                         "shares_outstanding_balance"} <= {i.key for i in IFRS_ITEMS})

    def test_ingest_keeps_eu_filings_and_plain_ifrs_facts_exactly(self):
        stub = esef_stub()
        out = esef.ingest_issuer(self.s, stub, tick(), LEI)
        self.assertEqual((out["filings_indexed"], out["filings_eu"], out["filings_ingested"]), (3, 2, 2))
        self.assertNotIn("/r2025gb.json", " ".join(stub.urls))                      # the UK copy is not EU data
        self.assertEqual(out["filings"][0]["dimensional"], 1)
        self.assertEqual(out["filings"][0]["non_ifrs"], 1)
        self.assertEqual(out["filings"][0]["duplicates"], 1)
        rows = self.s.as_of(esef.entity_id(LEI), "ifrs-full:Assets", RECEIPT, dataset=esef.DATASET)
        self.assertEqual([(r["period_start"], r["period_end"], r["value"]) for r in rows],
                         [(None, "2024-12-31", "5000.0"), (None, "2025-12-31", "5600.0")])
        q = json.loads(rows[0]["quality"])
        self.assertIn("published_time_read_as_utc", q)
        self.assertEqual(rows[0]["available_time_ns"], utc_ns(2025, 3, 1, 9))
        self.assertEqual(rows[1]["available_time_ns"], utc_ns(2026, 4, 1))           # no 'published': date_added
        self.assertIn("availability_from_index_date_added", json.loads(rows[1]["quality"]))
        again = esef.ingest_issuer(self.s, esef_stub(), tick(), LEI)
        self.assertEqual(again["filings_skipped_already_held"], 2)

    def test_statements_point_in_time_restatement_and_fallback(self):
        esef.ingest_issuer(self.s, esef_stub(), tick(), LEI)
        early = EsefStatements(self.s, LEI, utc_ns(2025, 6, 30)).statement()
        self.assertEqual([c["label"] for c in early["columns"]], ["FY2024"])
        rev = {line["item"]: line for line in early["statements"]["income"]}["revenue"]["cells"][0]
        self.assertEqual((rev["value"], rev["concept"], rev["unit"]), ("1000.0", "ifrs-full:Revenue", "EUR"))
        ni = {line["item"]: line for line in early["statements"]["income"]}["net_income"]["cells"][0]
        self.assertEqual((ni["value"], ni["method"]), ("100.0", "FALLBACK_CONCEPT"))
        self.assertTrue(ni["flags"][0].startswith("parent_share_not_tagged"))
        late = EsefStatements(self.s, LEI, utc_ns(2026, 6, 30)).statement()
        self.assertEqual([c["label"] for c in late["columns"]], ["FY2025", "FY2024"])
        inc = {line["item"]: line for line in late["statements"]["income"]}
        fy24 = inc["revenue"]["cells"][1]
        self.assertEqual(fy24["value"], "1010.0")
        self.assertTrue(fy24["restated"])
        self.assertEqual(fy24["history"][0]["value"], "1000.0")
        self.assertIn("from_later_filing:2025-12-31", fy24["flags"])
        self.assertEqual(inc["net_income"]["cells"][0]["method"], "REPORTED")
        op = inc["operating_income"]["cells"][0]
        self.assertEqual((op["value"], op["missing_reason"]), (None, "NOT_REPORTED"))   # never zero
        bal = {line["item"]: line for line in late["statements"]["balance"]}
        self.assertEqual(bal["equity_parent"]["cells"][0]["value"], "2500.0")
        eps = {line["item"]: line for line in late["statements"]["per_share"]}["eps_basic"]["cells"][0]
        self.assertEqual((eps["value"], eps["unit"]), ("13.00", "EUR/shares"))
        self.assertEqual(late["currency"], "EUR")
        self.assertEqual(late["source"]["provider"], "xbrl-filings")

    def test_issuer_without_indexed_filings_is_explicit(self):
        out = esef.ingest_issuer(self.s, esef_stub(), tick(), OTHER_LEI)
        self.assertEqual(out["status"], "NO_FILINGS_INDEXED")
        st = EsefStatements(self.s, OTHER_LEI, RECEIPT).statement()
        self.assertEqual(st["status"], "NO_FILINGS_INDEXED")
        self.assertIn("German issuers are not indexed", st["reason"])

    def test_compare_side_by_side_never_converts_currencies(self):
        esef.ingest_issuer(self.s, esef_stub(), tick(), LEI)
        e = EsefStatements(self.s, LEI, RECEIPT).statement()

        def sec_cell(v, unit):
            return {"period": {"end": "2025-12-31"}, "value": v, "unit": unit, "method": "REPORTED", "concept": "ifrs-full:Revenue",
                    "obs_ids": [9], "flags": [], "sources": [{"accession": "0000000000-26-000001"}]}
        sec = {"entity_id": "SEC-CIK-0000000001", "currency": "EUR", "mapping": "std-map@1", "columns": [{"end": "2025-12-31"}],
               "statements": {"income": [{"item": "revenue", "cells": [sec_cell("1195.0", "EUR")]},
                                         {"item": "net_income", "cells": [sec_cell("130.0", "USD")]}]}}
        cmp = compare(e, sec)
        lines = {line["item"]: line for line in cmp["lines"]}
        c = lines["revenue"]["cells"][0]
        self.assertEqual((c["esef"]["value"], c["sec"]["value"], c["difference_esef_minus_sec"]), ("1200.0", "1195.0", "5.0"))
        self.assertEqual((c["esef"]["source"], c["sec"]["document"]), ("ESEF", "0000000000-26-000001"))
        n = lines["net_income"]["cells"][0]
        self.assertIsNone(n["difference_esef_minus_sec"])
        self.assertIn("not converted", n["note"])
        self.assertIsNone(compare(e, None)["sec"])


class FirdsTests(unittest.TestCase):
    def setUp(self):
        self.s = ResearchStore(":memory:")
        self.addCleanup(self.s.close)

    def test_parse_filters_to_eu_venues_and_refuses_dtd(self):
        z = auth017((ISIN, "FIXTURE EUROPA NV", "FIX", LEI, "XAMS"), (ISIN, "FIXTURE EUROPA NV", "FIX", LEI, "XLON"))
        with zipfile.ZipFile(io.BytesIO(z)) as zf, zf.open(zf.namelist()[0]) as xml:
            recs, stats = firds.parse_xml(xml, frozenset({"XAMS"}))
        self.assertEqual((stats["records"], stats["kept"], stats["other_venue"]), (2, 1, 1))
        self.assertEqual({k: recs[0][k] for k in ("isin", "mic", "cfi", "currency", "issuer_lei", "first_trade_date")},
                         {"isin": ISIN, "mic": "XAMS", "cfi": "ESVUFR", "currency": "EUR", "issuer_lei": LEI, "first_trade_date": "2001-01-02"})
        with self.assertRaises(ValueError):
            firds.parse_xml(io.BytesIO(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>'), frozenset({"XAMS"}))

    def test_ingest_uses_listed_files_and_feeds_search(self):
        listing = json.dumps({"response": {"docs": [
            {"file_name": "FULINS_E_20261018_01of01.zip", "file_type": "FULINS", "publication_date": "2026-10-18T00:00:00Z",
             "download_link": "https://firds.esma.europa.eu/firds/FULINS_E_20261018_01of01.zip"},
            {"file_name": "DLTINS_20261022_01of01.zip", "file_type": "DLTINS", "publication_date": "2026-10-22T00:00:00Z",
             "download_link": "https://firds.esma.europa.eu/firds/DLTINS_20261022_01of01.zip"}]}}).encode()
        stub = Stub({"esma_registers_firds_files": listing,
                     "FULINS_E_20261018_01of01.zip": auth017((ISIN, "FIXTURE EUROPA NV", "FIX", LEI, "XAMS"))}, RECEIPT)
        out = firds.ingest(self.s, stub, tick())
        self.assertEqual((out["status"], out["NEW"]), ("OK", 1))
        self.assertNotIn("DLTINS", " ".join(stub.urls))
        esef.ingest_issuer(self.s, esef_stub(), tick(), LEI)
        for q in (LEI, ISIN, "fixture europa"):
            res = search(q, as_of_ns=RECEIPT, store=self.s)
            eu = [c for c in res["candidates"] if c["kind"] == "eu_issuer"]
            self.assertEqual(len(eu), 1, q)
            self.assertEqual((eu[0]["lei"], eu[0]["reporting_currency"], eu[0]["source"]), (LEI, "EUR", "filings.xbrl.org (ESEF)"))
            self.assertEqual(eu[0]["listings"][0]["mic"], "XAMS")
        self.assertEqual(search(LEI, as_of_ns=RECEIPT, store=self.s)["query_type"], "LEI")
        self.assertIn("eu_instrument", [c["kind"] for c in search(ISIN, as_of_ns=RECEIPT, store=self.s)["candidates"]])


class EuRouteTests(unittest.TestCase):
    def test_eu_routes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "research").mkdir()
            s = ResearchStore(Path(d) / "research" / "research.sqlite")
            esef.ingest_issuer(s, esef_stub(), tick(), LEI)
            s.close()
            ctx = SimpleNamespace(paths=SimpleNamespace(root=Path(d)))

            def get(*parts, **q):
                return routes.company(ctx, ["company", *parts], q, "SIM-1", None)
            ident = get("eu", LEI, as_of="2025-06-30")
            self.assertEqual([f["period_end"] for f in ident["filings"]], ["2024-12-31"])   # FY2025 not yet available then
            self.assertEqual(get("eu", LEI, "statements", as_of="2026-06-30")["columns"][0]["label"], "FY2025")
            self.assertIsNone(get("eu", LEI, "compare")["sec"])
            with self.assertRaises(ValueError):
                get("eu", "NOTALEI")
            with self.assertRaises(LookupError):
                get("eu", OTHER_LEI)


if __name__ == "__main__":
    unittest.main()
