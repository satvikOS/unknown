"""ECB euro reference rates (CAP-DATA-07, CAP-MAC-12). No network: a FIXTURE csvdata payload with the documented
column names; values are synthetic (the USD 1.1234 example mirrors the register's acceptance criterion)."""
from __future__ import annotations

import re
import unittest
from decimal import Decimal
from pathlib import Path

from unknown.data.http import Response
from unknown.data.pit import ResearchStore
from unknown.data.registry import dataset, load_registry
from unknown.data.sources import ecb
from unknown.domain.clock import utc_ns
from unknown.domain.fixed import Fixed, Rounding

HEADER = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK,OBS_COM,"
          "TIME_FORMAT,BREAKS,COLLECTION,COMPILING_ORG,DISS_ORG,DOM_SER_IDS,PUBL_ECB,PUBL_MU,PUBL_PUBLIC,UNIT_INDEX_BASE,COMPILATION,"
          "COVERAGE,DECIMALS,NAT_TITLE,SOURCE_AGENCY,SOURCE_PUB,TITLE,TITLE_COMPL,UNIT,UNIT_MULT")


def row(ccy: str, day: str, value: str) -> str:
    return (f"EXR.D.{ccy}.EUR.SP00.A,D,{ccy},EUR,SP00,A,{day},{value},A,F,,,P1D,,A,,,,,,,,,,4,,4F0,,"
            f"\"FIXTURE {ccy}/EUR\",\"FIXTURE\",{ccy},0")


CSV = "\n".join([HEADER, row("USD", "2026-10-20", "1.1200"), row("USD", "2026-10-21", "1.1234"), row("JPY", "2026-10-20", "160.10"),
                 row("JPY", "2026-10-21", "160.25"), row("GBP", "2026-10-21", "NaN")]).encode()


class StubFetcher:
    def __init__(self, body: bytes, receipt_ns: int):
        self.body, self.receipt_ns = body, receipt_ns

    def get(self, url):
        return Response(url, 200, self.body, "text/csv", self.receipt_ns)


class EcbTests(unittest.TestCase):
    def setUp(self):
        self.s = ResearchStore(":memory:")

    def tearDown(self):
        self.s.close()

    def test_url_and_registry(self):
        u = ecb.url(last_n=1)
        self.assertEqual(u, "https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?format=csvdata&lastNObservations=1")
        self.assertIn("startPeriod=2026-10-01", ecb.url(start="2026-10-01", currencies=["USD", "JPY"]))
        ds = dataset(load_registry(), ecb.DATASET)
        self.assertTrue(ds.permits("storage"))
        self.assertFalse(ds.permits("model_processing_cloud"))

    def test_exact_values_and_availability(self):
        """CAP-MAC-12: 1.1234 stored exactly, available at first-seen time, never the 14:15 CET fixing time."""
        receipt = utc_ns(2026, 10, 21, 14, 5)                 # 16:05 CEST on the reference date
        out = ecb.ingest(self.s, StubFetcher(CSV, receipt), lambda: receipt, last_n=2)
        self.assertEqual((out["rows"], out["skipped"], out["currencies"]), (4, 1, 2))
        r = self.s.latest_as_of(ecb.entity("USD"), ecb.FIELD, receipt)
        self.assertEqual((r["value"], r["unit"], r["period_end"], r["available_time_ns"]), ("1.1234", "USD/EUR", "2026-10-21", receipt))
        three_pm_cet = utc_ns(2026, 10, 21, 13, 0)            # 15:00 CEST
        self.assertEqual(self.s.latest_as_of(ecb.entity("USD"), ecb.FIELD, three_pm_cet)["period_end"], "2026-10-20")
        prev = self.s.as_of(ecb.entity("USD"), ecb.FIELD, receipt)[0]
        self.assertIn("availability_estimated_backfill", prev["quality"])

    def test_rates_as_of_previous_fixing_and_normalize(self):
        receipt = utc_ns(2026, 10, 21, 14, 5)
        ecb.ingest(self.s, StubFetcher(CSV, receipt), lambda: receipt, last_n=2)
        rates = ecb.rates_as_of(self.s, receipt, "2026-10-22")
        self.assertEqual(rates["rates"]["USD"]["reference_date"], "2026-10-21")
        self.assertIn("previous_fixing", rates["rates"]["USD"]["flags"])
        self.assertIn("transaction purposes", rates["use_note"])
        usd = ecb.normalize(rates["rates"], "USD")
        self.assertEqual(usd["JPY"]["value"], Fixed.parse("1.1234").div(Fixed.parse("160.25"), 10, Rounding.HALF_EVEN).to_str())
        self.assertEqual(usd["EUR"]["value"], "1.1234000000")
        jpy = ecb.normalize(rates["rates"], "JPY")
        # round trip x_{USD->JPY} * x_{JPY->USD} = 1 within the declared rounding (10 dp)
        prod = Decimal(usd["JPY"]["value"]) * Decimal(jpy["USD"]["value"])
        self.assertLess(abs(prod - 1), Decimal("0.0000001"))
        with self.assertRaises(LookupError):
            ecb.normalize(rates["rates"], "CHF")
        old = "\n".join([HEADER, row("CYP", "2007-12-31", "0.585274")]).encode()
        ecb.ingest(self.s, StubFetcher(old, receipt), lambda: receipt, last_n=1)
        with_old = ecb.rates_as_of(self.s, receipt)
        self.assertEqual(with_old["stale"], ["CYP"])                      # a discontinued series is flagged, not current
        self.assertTrue(any(f.startswith("stale:") for f in with_old["rates"]["CYP"]["flags"]))
        blank = ResearchStore(":memory:")
        empty = ecb.rates_as_of(blank, receipt)
        blank.close()
        self.assertEqual(empty["status"], "UNAVAILABLE")

    def test_cross_rates_carry_input_dates_and_flags(self):
        """Regression (review fix): a cross built from fixings of different dates, or from a stale or previous-day
        fixing, says so; it is never presented as a same-day rate (CAP-MAC-12)."""
        receipt = utc_ns(2026, 10, 21, 14, 5)
        ecb.ingest(self.s, StubFetcher(CSV, receipt), lambda: receipt, last_n=2)
        old = "\n".join([HEADER, row("CYP", "2007-12-31", "0.585274"), row("SEK", "2026-10-20", "11.5000")]).encode()
        ecb.ingest(self.s, StubFetcher(old, receipt), lambda: receipt, last_n=1)
        rates = ecb.rates_as_of(self.s, receipt, "2026-10-21")
        usd = ecb.normalize(rates["rates"], "USD")
        same_day = usd["JPY"]
        self.assertEqual((same_day["reference_dates"], same_day["flags"]), (["2026-10-21"], []))
        self.assertEqual(len(same_day["inputs"]), 2)
        sek = usd["SEK"]                                   # SEK has no 2026-10-21 fixing: previous day used
        self.assertEqual(sek["reference_dates"], ["2026-10-20", "2026-10-21"])
        self.assertEqual(sek["flags"], ["mixed_reference_dates", "previous_fixing_input:SEK"])
        cyp = usd["CYP"]
        self.assertIn("mixed_reference_dates", cyp["flags"])
        self.assertIn("stale_input:CYP", cyp["flags"])
        self.assertEqual(usd["EUR"]["flags"], [])
        sek_base = ecb.normalize(rates["rates"], "SEK")    # a previous-day base taints every cross
        self.assertIn("previous_fixing_input:SEK", sek_base["EUR"]["flags"])
        self.assertIn("mixed_reference_dates", sek_base["USD"]["flags"])

    def test_reference_rates_never_reach_the_money_path(self):
        """CAP-MAC-12: no order-pricing, funding or core module imports the ECB reference-rate source."""
        src = Path(__file__).resolve().parents[1] / "src" / "unknown"
        offenders = []
        for pkg in ("core", "risk", "policy", "broker", "gateway", "service", "store", "domain", "market", "reconcile"):
            for f in (src / pkg).rglob("*.py"):
                text = f.read_text()
                if re.search(r"sources(\.| import )ecb|ecb\.exr_reference", text):
                    offenders.append(str(f.relative_to(src)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
