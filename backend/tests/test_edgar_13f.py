"""Form 13F data sets -> institutional holders per issuer (CAP-CO-09). FIXTURE data only.

Synthetic ZIPs mimic the SEC data set layout (SUBMISSION, COVERPAGE, INFOTABLE tab-separated, headers as in
FORM13F_readme.htm). Two windows: Q1 then Q2 report periods, with a restatement, a new-holdings amendment,
an option row, an exit, an entry and a manager that has not filed yet.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from unknown.data.pit import Observation, ResearchStore
from unknown.data.sources import edgar_13f as f13
from unknown.domain.clock import NS_PER_S
from unknown.domain.fixed import Fixed

CUSIP = "99999A101"
EID = "SEC-CIK-0009100001"
SUB_H = "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT"
COV_H = ("ACCESSION_NUMBER\tREPORTCALENDARORQUARTER\tISAMENDMENT\tAMENDMENTNO\tAMENDMENTTYPE\tCONFDENIEDEXPIRED\tDATEDENIEDEXPIRED\tDATEREPORTED\t"
         "REASONFORNONCONFIDENTIALITY\tFILINGMANAGER_NAME\tFILINGMANAGER_STREET1\tFILINGMANAGER_STREET2\tFILINGMANAGER_CITY\t"
         "FILINGMANAGER_STATEORCOUNTRY\tFILINGMANAGER_ZIPCODE\tREPORTTYPE\tFORM13FFILENUMBER\tCRDNUMBER\tSECFILENUMBER\t"
         "PROVIDEINFOFORINSTRUCTION5\tADDITIONALINFORMATION")
INFO_H = ("ACCESSION_NUMBER\tINFOTABLE_SK\tNAMEOFISSUER\tTITLEOFCLASS\tCUSIP\tFIGI\tVALUE\tSSHPRNAMT\tSSHPRNAMTTYPE\tPUTCALL\t"
          "INVESTMENTDISCRETION\tOTHERMANAGER\tVOTING_AUTH_SOLE\tVOTING_AUTH_SHARED\tVOTING_AUTH_NONE")


def build_zip(filings, rows) -> bytes:
    """filings: (acc, filing date, type, cik, period, manager, amendment type); rows: (acc, cusip, value, shares, type, putcall)."""
    sub = [SUB_H] + [f"{a}\t{d}\t{t}\t{c:010d}\t{p}" for a, d, t, c, p, _, _ in filings]
    cov = [COV_H] + [f"{a}\t{p}\t{'Y' if am else ''}\t\t{am or ''}\t\t\t\t\t{m}\t\t\t\t\t\t13F HOLDINGS REPORT\t028-0\t\t\tN\t"
                     for a, d, t, c, p, m, am in filings]
    info = [INFO_H] + [f"{a}\t{i}\tFIXTURE WIDGETS INC\tCOM\t{cu}\t\t{v}\t{s}\t{ty}\t{pc}\tSOLE\t\t{s}\t0\t0"
                       for i, (a, cu, v, s, ty, pc) in enumerate(rows)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", "\n".join(sub) + "\n")
        z.writestr("COVERPAGE.tsv", "\n".join(cov) + "\n")
        z.writestr("INFOTABLE.tsv", "\n".join(info) + "\n")
    return buf.getvalue()


Q1 = build_zip(
    [("0000000001-26-000001", "14-MAY-2026", "13F-HR", 1, "31-MAR-2026", "Alpha Fixture Capital", None),
     ("0000000002-26-000001", "15-MAY-2026", "13F-HR", 2, "31-MAR-2026", "Beta Fixture Advisors", None),
     ("0000000003-26-000001", "15-MAY-2026", "13F-HR", 3, "31-MAR-2026", "Gamma Fixture LLC", None),
     ("0000000004-26-000001", "15-MAY-2026", "13F-HR", 4, "31-MAR-2026", "Delta Fixture (holds other names)", None),
     ("0000000005-26-000001", "15-MAY-2026", "13F-HR", 5, "31-MAR-2026", "Epsilon Fixture", None)],
    [("0000000001-26-000001", CUSIP, "1000000", "5000", "SH", ""),
     ("0000000001-26-000001", CUSIP, "200000", "1000", "SH", ""),          # second row of the same manager: summed
     ("0000000002-26-000001", CUSIP, "400000", "2000", "SH", ""),
     ("0000000003-26-000001", CUSIP, "300000", "1500", "SH", ""),
     ("0000000005-26-000001", CUSIP, "100000", "500", "SH", ""),
     ("0000000004-26-000001", "88888B202", "999", "9", "SH", "")])
Q2 = build_zip(
    [("0000000001-26-000002", "10-AUG-2026", "13F-HR", 1, "30-JUN-2026", "Alpha Fixture Capital", None),
     ("0000000002-26-000002", "12-AUG-2026", "13F-HR", 2, "30-JUN-2026", "Beta Fixture Advisors", None),
     ("0000000004-26-000002", "12-AUG-2026", "13F-HR", 4, "30-JUN-2026", "Delta Fixture (holds other names)", None),
     ("0000000002-26-000003", "20-AUG-2026", "13F-HR/A", 2, "30-JUN-2026", "Beta Fixture Advisors", "RESTATEMENT"),
     ("0000000001-26-000003", "25-AUG-2026", "13F-HR/A", 1, "30-JUN-2026", "Alpha Fixture Capital", "NEW HOLDINGS")],
    [("0000000001-26-000002", CUSIP, "1500000", "7000", "SH", ""),
     ("0000000001-26-000002", CUSIP, "50000", "100", "SH", "CALL"),        # an option row: counted, never added to shares
     ("0000000002-26-000002", CUSIP, "500000", "2500", "SH", ""),
     ("0000000004-26-000002", CUSIP, "60000", "300", "SH", ""),            # Delta enters
     ("0000000002-26-000003", CUSIP, "440000", "2200", "SH", ""),          # Beta restates its 2,500 to 2,200
     ("0000000001-26-000003", CUSIP, "100000", "500", "SH", "")])          # Alpha adds 500 more: cumulative 7,500
# Gamma (3) has not filed for Q2 yet: unknown, not an exit. Epsilon (5) filed Q1 only: unknown for Q2.
EXIT_ZIP = build_zip([("0000000003-26-000002", "14-SEP-2026", "13F-HR", 3, "30-JUN-2026", "Gamma Fixture LLC", None)],
                     [("0000000003-26-000002", "77777C303", "10", "1", "SH", "")])


def day_end(text: str) -> int:
    from unknown.data.sec import end_of_filed_day_ns
    return end_of_filed_day_ns(text)


class Form13FTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.tmp.name) / "research.sqlite")
        t = day_end("2026-04-30")
        for end, shares in (("2026-04-17", "100000"), ("2026-07-17", "100000")):
            self.store.record(Observation(dataset="sec.companyfacts", entity_id=EID, field="dei:EntityCommonStockSharesOutstanding",
                                          unit="shares", value=Fixed.parse(shares), period_end=end, period_kind="INSTANT",
                                          receipt_time_ns=t, available_time_ns=day_end(end) if end > "2026-05-01" else t,
                                          publication_id=f"fixture-{end}", source_ref="FIXTURE"), t)
        self.now = day_end("2026-10-01")
        self.r1 = f13.ingest_data_set(self.store, Q1, "01mar2026-31may2026", "fixture://q1", {CUSIP: EID}, self.now, lambda: self.now)
        self.r2 = f13.ingest_data_set(self.store, Q2, "01jun2026-31aug2026", "fixture://q2", {CUSIP: EID}, self.now, lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def holders(self, as_of: str) -> dict:
        return f13.institutional(self.store, EID, day_end(as_of))

    def test_parse_dates_and_links(self):
        self.assertEqual(f13.parse_sec_date("31-JUL-2026").isoformat(), "2026-07-31")
        html = ('<a href="/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip">x</a>'
                '<a href="/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip">y</a>')
        self.assertEqual([w for w, _ in f13.data_set_links(html)], ["01mar2026-31may2026", "01jun2026-31aug2026"])

    def test_aggregation_amendments_and_changes(self):
        v = self.holders("2026-09-30")
        self.assertEqual((v["period"], v["prior_period"]), ("2026-06-30", "2026-03-31"))
        by = {h["manager"]: h for h in v["holders"]}
        alpha, beta, delta = by["Alpha Fixture Capital"], by["Beta Fixture Advisors"], by["Delta Fixture (holds other names)"]
        self.assertEqual((alpha["shares"], alpha["change"], alpha["change_shares"], alpha["option_rows"]), ("7500", "INCREASE", "1500", 1))
        self.assertTrue(any(f.startswith("new_holdings_added_to:0000000001-26-000002") for f in alpha["flags"]))
        self.assertEqual((beta["shares"], beta["accession"], beta["change"]), ("2200", "0000000002-26-000003", "INCREASE"))
        self.assertEqual(delta["change"], "ENTRY")               # filed for Q1 without the issuer, holds it in Q2
        self.assertNotIn("Gamma Fixture LLC", by)                 # not filed for Q2 yet: unknown, not an exit
        self.assertNotIn("Epsilon Fixture", by)
        self.assertEqual(v["changes"], {"entries": 1, "exits": 0, "increases": 2, "decreases": 0})
        self.assertEqual(alpha["pct_held"], "0.0750000000")       # 7,500 / 100,000 shares outstanding at 2026-07-17
        self.assertEqual(v["shares_outstanding"]["date"], "2026-07-17")
        self.assertEqual(v["concentration"]["top5"], "0.1000000000")
        self.assertEqual(v["total_shares_reported"], "10000")
        self.assertEqual(v["coverage"][0]["filers_count"], 3)

    def test_point_in_time(self):
        # Before the restatement (filed 20 Aug) Beta's original 2,500 is what had been filed; before 10 Aug the latest period is Q1.
        before_restate = {h["manager"]: h for h in self.holders("2026-08-15")["holders"]}
        self.assertEqual(before_restate["Beta Fixture Advisors"]["shares"], "2500")
        self.assertEqual(self.holders("2026-08-01")["period"], "2026-03-31")
        q1 = {h["manager"]: h["shares"] for h in self.holders("2026-08-01")["holders"]}
        self.assertEqual(q1["Alpha Fixture Capital"], "6000")
        self.assertEqual(self.holders("2026-05-01")["status"], "NO_DATA")

    def test_exit_recorded_only_when_the_manager_filed_without_the_issuer(self):
        f13.ingest_data_set(self.store, EXIT_ZIP, "01sep2026-30nov2026", "fixture://q3", {CUSIP: EID}, self.now, lambda: self.now)
        v = self.holders("2026-09-30")
        gamma = [h for h in v["holders"] if h["manager_cik"] == 3]
        self.assertEqual((gamma[0]["change"], gamma[0]["shares"], gamma[0]["shares_reason"]), ("EXIT", None, "NOT_REPORTED"))
        self.assertEqual(v["changes"]["exits"], 1)

    def test_restatement_without_the_position_is_not_reported(self):
        z = build_zip([("0000000002-26-000009", "28-AUG-2026", "13F-HR/A", 2, "30-JUN-2026", "Beta Fixture Advisors", "RESTATEMENT")],
                      [("0000000002-26-000009", "77777C303", "10", "1", "SH", "")])
        out = f13.ingest_data_set(self.store, z, "fixture-late", "fixture://late", {CUSIP: EID}, self.now, lambda: self.now)
        self.assertEqual(out["issuers"][EID]["restated_out"], 1)
        beta = [h for h in self.holders("2026-09-30")["holders"] if h["manager_cik"] == 2][0]
        self.assertEqual((beta["shares"], beta["change"]), (None, "EXIT"))

    def test_reingest_is_unchanged_and_values_exact(self):
        again = f13.ingest_data_set(self.store, Q2, "01jun2026-31aug2026", "fixture://q2", {CUSIP: EID}, self.now, lambda: self.now)
        self.assertEqual(set(again["issuers"][EID]["outcomes"]), {"UNCHANGED"})
        row = self.store.conn.execute("SELECT value_text FROM observation WHERE field='13f:0000000001' AND publication_id=?",
                                      ("0000000001-26-000002",)).fetchone()
        doc = json.loads(row[0])
        self.assertEqual((doc["shares"], doc["value"], doc["value_unit"], doc["rows"]), ("7000", "1500000", "USD", 2))
        self.assertEqual(self.r1["issuers"][EID]["holdings"], 4)

    def test_missing_shares_outstanding_gives_reason(self):
        self.store.conn.execute("DELETE FROM observation WHERE dataset='sec.companyfacts'")
        v = self.holders("2026-09-30")
        self.assertIsNone(v["holders"][0]["pct_held"])
        self.assertEqual(v["holders"][0]["pct_reason"], "NOT_REPORTED")
        self.assertIsNone(v["concentration"]["top5"])


if __name__ == "__main__":
    unittest.main()

assert NS_PER_S
