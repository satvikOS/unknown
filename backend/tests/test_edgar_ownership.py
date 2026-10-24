"""SEC Forms 3/4/5 and Schedules 13D/13G into the point-in-time store, and the ownership route (CAP-CO-09).

Every issuer, person, accession and number here is SYNTHETIC (FIXTURE); the XML mimics the documented
EDGAR ownership and schedule shapes and goes through the real ingestion with a fake opener.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import RouteOpener, ns  # noqa: E402

from unknown.api.routes import ownership as route  # noqa: E402
from unknown.data.http import Fetcher  # noqa: E402
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.data.registry import RateLimit  # noqa: E402
from unknown.data.sources import edgar_ownership as eo  # noqa: E402

UA = "Fixture Owner owner@example.test"
CIK = 9100001
NOW = ns("2026-10-20T00:00:00Z")


def form4(issuer_cik: int = CIK, *, code: str = "S", shares: str = "1000", price: str | None = "200.50", date: str = "2026-09-10",
          doc_type: str = "4", original: str | None = None, owner_cik: str = "0009200001", aff: str = "true") -> bytes:
    price_xml = f"<value>{price}</value>" if price is not None else '<footnoteId id="F1"/>'
    orig = f"<dateOfOriginalSubmission>{original}</dateOfOriginalSubmission>" if original else ""
    return f"""<?xml version="1.0"?>
<ownershipDocument><schemaVersion>X0508</schemaVersion><documentType>{doc_type}</documentType><periodOfReport>{date}</periodOfReport>{orig}
<issuer><issuerCik>{issuer_cik:010d}</issuerCik><issuerName>FIXTURE WIDGETS INC</issuerName><issuerTradingSymbol>FXW</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>{owner_cik}</rptOwnerCik><rptOwnerName>Doe Jane (FIXTURE)</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner><isOther>0</isOther>
<officerTitle>Chief Fixture Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
<aff10b5One>{aff}</aff10b5One>
<nonDerivativeTable>
<nonDerivativeTransaction><securityTitle><value>Common Stock</value></securityTitle><transactionDate><value>{date}</value></transactionDate>
<transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
<transactionAmounts><transactionShares><value>{shares}</value></transactionShares><transactionPricePerShare>{price_xml}</transactionPricePerShare>
<transactionAcquiredDisposedCode><value>{'A' if code == 'P' else 'D'}</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000.25</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
<ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
<nonDerivativeHolding><securityTitle><value>Common Stock</value></securityTitle><postTransactionAmounts><sharesOwnedFollowingTransaction>
<value>1200</value></sharesOwnedFollowingTransaction></postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>I</value>
</directOrIndirectOwnership><natureOfOwnership><value>By trust</value></natureOfOwnership></ownershipNature></nonDerivativeHolding>
</nonDerivativeTable>
<derivativeTable><derivativeTransaction><securityTitle><value>Restricted Stock Unit</value></securityTitle><conversionOrExercisePrice>
<footnoteId id="F2"/></conversionOrExercisePrice><transactionDate><value>{date}</value></transactionDate><transactionCoding><transactionFormType>4
</transactionFormType><transactionCode>M</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding><transactionAmounts>
<transactionShares><value>300</value></transactionShares><transactionPricePerShare><value>0</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts><underlyingSecurity>
<underlyingSecurityTitle><value>Common Stock</value></underlyingSecurityTitle><underlyingSecurityShares><value>300</value></underlyingSecurityShares>
</underlyingSecurity><postTransactionAmounts><sharesOwnedFollowingTransaction><value>900</value></sharesOwnedFollowingTransaction>
</postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
</derivativeTransaction></derivativeTable>
<footnotes><footnote id="F1">Weighted average price (FIXTURE).</footnote><footnote id="F2">Each unit converts into one share.</footnote></footnotes>
</ownershipDocument>""".encode()


def schedule13g(issuer_cik: int = CIK, persons=(("Fixture Capital LP", "8100000", "6.1"),)) -> bytes:
    blocks = "".join(f"""<coverPageHeaderReportingPersonDetails><reportingPersonName>{n}</reportingPersonName>
<citizenshipOrOrganization>DE</citizenshipOrOrganization><reportingPersonBeneficiallyOwnedNumberOfShares><soleVotingPower>{a}</soleVotingPower>
<sharedVotingPower>0</sharedVotingPower><soleDispositivePower>{a}</soleDispositivePower><sharedDispositivePower>0</sharedDispositivePower>
</reportingPersonBeneficiallyOwnedNumberOfShares><reportingPersonBeneficiallyOwnedAggregateNumberOfShares>{a}
</reportingPersonBeneficiallyOwnedAggregateNumberOfShares><classPercent>{p}</classPercent><typeOfReportingPerson>IA</typeOfReportingPerson>
</coverPageHeaderReportingPersonDetails>""" for n, a, p in persons)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g" xmlns:com="http://www.sec.gov/edgar/common"><schemaVersion>X0202</schemaVersion>
<headerData><submissionType>SCHEDULE 13G</submissionType><filerInfo><filer><filerCredentials><cik>0009300001</cik></filerCredentials></filer>
</filerInfo></headerData><formData><coverPageHeader><securitiesClassTitle>Common Stock</securitiesClassTitle>
<eventDateRequiresFilingThisStatement>06/30/2026</eventDateRequiresFilingThisStatement><issuerInfo><issuerCik>{issuer_cik:010d}</issuerCik>
<issuerName>Fixture Widgets Inc</issuerName><issuerCusips><issuerCusipNumber>99999A101</issuerCusipNumber></issuerCusips></issuerInfo>
</coverPageHeader>{blocks}<signatureInformation><reportingPersonName>Fixture Capital LP</reportingPersonName><signatureDetails>
<signature>J. Doe</signature></signatureDetails></signatureInformation></formData></edgarSubmission>""".encode()


# (accession, form, acceptance, filing date, primary document, body)
FILINGS = [
    ("0009200001-26-000001", "4", "2026-09-11T21:00:00.000Z", "2026-09-11", "xslF345X06/form4.xml", form4()),
    ("0009200001-26-000002", "4", "2026-09-15T21:00:00.000Z", "2026-09-15", "xslF345X06/form4.xml", form4(code="P", shares="200", price="190")),
    ("0009200001-26-000003", "4/A", "2026-09-18T21:00:00.000Z", "2026-09-18", "xslF345X06/form4.xml",
     form4(code="S", shares="1100", doc_type="4/A", original="2026-09-11")),
    ("0009200001-26-000004", "4", "2026-10-01T21:00:00.000Z", "2026-10-01", "xslF345X06/form4.xml", form4(code="S", price=None)),
    ("0009100001-26-000009", "4", "2026-10-02T21:00:00.000Z", "2026-10-02", "xslF345X06/form4.xml", form4(issuer_cik=9555555)),
    ("0009300001-26-000001", "SCHEDULE 13G", "2026-08-01T21:00:00.000Z", "2026-08-01", "xslSCHEDULE_13G_X02/primary_doc.xml",
     schedule13g(persons=(("Fixture Capital LP", "8100000", "6.1"), ("Fixture Capital GP LLC", "8100000", "6.1")))),
    ("0009100001-26-000005", "SCHEDULE 13G", "2026-08-02T21:00:00.000Z", "2026-08-02", "xslSCHEDULE_13G_X02/primary_doc.xml",
     schedule13g(issuer_cik=9555555)),
    ("0009400001-24-000001", "SC 13G/A", "2024-02-10T21:00:00.000Z", "2024-02-10", "fixture13g.txt", None),
    ("0009200001-20-000001", "4", "2020-01-10T21:00:00.000Z", "2020-01-10", "xslF345X03/form4.xml", form4(date="2020-01-09")),
]


def submissions() -> dict:
    rows = FILINGS
    return {"cik": str(CIK), "name": "Fixture Widgets Inc (synthetic)", "sic": "3571", "tickers": ["FXW"], "exchanges": ["Nasdaq"],
            "filings": {"recent": {"accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
                                   "acceptanceDateTime": [r[2] for r in rows], "filingDate": [r[3] for r in rows],
                                   "reportDate": ["" for _ in rows], "items": ["" for _ in rows], "primaryDocument": [r[4] for r in rows]}}}


def routes() -> dict:
    out = {eo.SUBMISSIONS.format(cik=CIK): json.dumps(submissions()).encode()}
    for acc, _, _, _, primary, body in FILINGS:
        if body is not None:
            out[eo.document_url(CIK, acc, primary)] = body
    return out


def ingest_fixture(store: ResearchStore) -> tuple[dict, RouteOpener]:
    opener = RouteOpener(routes())
    fetcher = Fetcher(UA, RateLimit(10, 1, "fixture"), opener=opener, clock_ns=lambda: NOW, sleep=lambda s: None)
    return eo.ingest(store, CIK, fetcher, lambda: NOW), opener


class ParserTests(unittest.TestCase):
    def test_form4_rows_codes_and_exact_numbers(self):
        doc = eo.parse_ownership_xml(form4())
        self.assertEqual(doc["issuer_cik"], f"{CIK:010d}")
        self.assertEqual(doc["owners"][0]["officer_title"], "Chief Fixture Officer")
        self.assertEqual([(r["table"], r["kind"], r["code"]) for r in doc["rows"]],
                         [("non_derivative", "transaction", "S"), ("non_derivative", "holding", None), ("derivative", "transaction", "M")])
        tx = doc["rows"][0]
        self.assertEqual((tx["shares"], tx["price"], tx["shares_after"], tx["acquired_disposed"]), ("1000", "200.50", "50000.25", "D"))
        self.assertIsInstance(tx["price"], str)
        self.assertEqual(doc["rows"][1]["nature"], "By trust")
        self.assertEqual(doc["rows"][2]["underlying_shares"], "300")
        self.assertIsNone(doc["rows"][2]["conversion_price"])          # footnote only: no number invented
        self.assertEqual(doc["footnotes"]["F1"], "Weighted average price (FIXTURE).")
        self.assertIsNone(eo.parse_ownership_xml(form4(price=None))["rows"][0]["price"])

    def test_dtd_refused_and_wrong_root(self):
        with self.assertRaises(ValueError):
            eo.parse_ownership_xml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><ownershipDocument/>')
        with self.assertRaises(ValueError):
            eo.parse_ownership_xml(b"<edgarSubmission/>")

    def test_schedule_persons_skip_signature_block(self):
        doc = eo.parse_schedule_xml(schedule13g(persons=(("A LP", "10", "5.5"), ("B LLC", "10", "5.5"))))
        self.assertEqual([p["name"] for p in doc["persons"]], ["A LP", "B LLC"])
        self.assertEqual((doc["cusips"], doc["event_date"], doc["persons"][0]["percent_of_class"]), (["99999A101"], "2026-06-30", "5.5"))

    def test_document_url_strips_rendering_folder(self):
        self.assertEqual(eo.document_url(320193, "0001140361-26-040127", "xslF345X06/form4.xml"),
                         "https://www.sec.gov/Archives/edgar/data/320193/000114036126040127/form4.xml")
        self.assertTrue(eo.document_url(1, "0000000001-26-000001", "doc.htm").endswith("/doc.htm"))


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "research").mkdir()
        self.store = ResearchStore(self.root / "research" / "research.sqlite")
        self.result, self.opener = ingest_fixture(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_counts_issuer_filter_lookback_and_skip(self):
        ins, sch = self.result["insiders"], self.result["schedules"]
        self.assertEqual((ins["filings_listed"], ins["kept"], ins["other_issuer"]), (5, 4, 1))   # the 2020 Form 4 is outside the lookback
        self.assertEqual(ins["rows"], 12)
        self.assertEqual((sch["kept"], sch["skipped_filed_by_issuer"], sch["legacy_listed"]), (1, 1, 1))
        # the issuer's own schedule (filed as a holder of another company) is never fetched
        self.assertEqual([u for u in self.opener.calls if "000910000126000005" in u], [])
        self.assertTrue(all("xslF345" not in u for u in self.opener.calls))
        again, _ = ingest_fixture(self.store)
        self.assertEqual(set(again["insiders"]["outcomes"]), {"UNCHANGED"})

    def test_point_in_time_and_flow(self):
        eid = eo.entity_id(CIK)
        before = eo.insiders(self.store, eid, ns("2026-09-11T20:59:59Z"))
        self.assertEqual(before["status"], "NO_DATA")
        view = eo.insiders(self.store, eid, ns("2026-10-20T00:00:00Z"))
        self.assertEqual(view["filings"], 4)
        superseded = {t["accession"] for t in view["transactions"] if t["superseded_by_amendment"]}
        self.assertEqual(superseded, {"0009200001-26-000001"})
        s = view["summary"][90]
        # 4/A sale 1100 x 200.50 = 220550.00; purchase 200 x 190 = 38000; the 4 without a price is counted, not valued
        self.assertEqual((s["purchases"], s["sales"], s["net"], s["excluded_without_price"]), ("38000", "220550.00", "-182550.00", 1))
        cats = {t["category"] for t in view["transactions"]}
        self.assertEqual(cats, {"open_market_purchase", "open_market_sale", "holding", "other"})
        no_price = [t for t in view["transactions"] if t["accession"] == "0009200001-26-000004" and t["kind"] == "transaction"
                    and t["table"] == "non_derivative"][0]
        self.assertEqual((no_price["price"], no_price["price_reason"]), (None, "NOT_REPORTED"))

    def test_ten_b5_one_flag_is_never_presented(self):
        view = eo.insiders(self.store, eo.entity_id(CIK), NOW)
        text = json.dumps(view["transactions"])
        self.assertNotIn("aff10b5", text)
        self.assertNotIn("10b5", text)
        self.assertIn("not confirmed", view["ten_b5_one"])
        raw = self.store.conn.execute("SELECT value_text FROM observation WHERE field LIKE 'form345:%:filing' LIMIT 1").fetchone()[0]
        self.assertEqual(json.loads(raw)["aff10b5one_as_published"], "true")    # kept as published

    def test_major_holders_and_cusip(self):
        eid = eo.entity_id(CIK)
        mh = eo.major_holders(self.store, eid, NOW)
        self.assertEqual([f["parsed"] for f in mh["filings"]], [True, False])
        self.assertEqual(len(mh["filings"][0]["persons"]), 2)
        self.assertIn("not parsed", mh["filings"][1]["reason"])
        self.assertEqual(eo.issuer_cusips(self.store, eid, NOW)[0]["cusip"], "99999A101")
        self.assertEqual(eo.issuer_cusips(self.store, eid, ns("2026-08-01T20:00:00Z")), [])

    def test_route(self):
        ctx = SimpleNamespace(paths=SimpleNamespace(root=self.root))
        body = json.loads(json.dumps(route.handle(ctx, ["ownership", str(CIK)], {"as_of": "2026-10-19"}, "SIM", None), allow_nan=False))
        self.assertEqual(body["insiders"]["status"], "OK")
        self.assertEqual(body["institutional"]["status"], "NO_DATA")
        self.assertEqual(body["institutional"]["reason"], "NO_DATA")
        self.assertEqual(body["major_holders"]["count"], 2)
        with self.assertRaises(LookupError):
            route.handle(ctx, ["ownership", "123"], {}, "SIM", None)
        with self.assertRaises(ValueError):
            route.handle(ctx, ["ownership", "AAPL"], {}, "SIM", None)


if __name__ == "__main__":
    unittest.main()
