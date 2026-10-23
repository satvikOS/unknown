"""Global macro sources (CAP-MAC-05, CAP-MAC-06, CAP-MAC-09): World Bank, OECD, BIS, FRED and the comparison
read path. No network: payloads are SYNTHETIC with the documented shapes (World Bank v2 JSON, SDMX-CSV,
FRED observations JSON); values are invented."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from unknown.api.routes import load as load_routes
from unknown.api.routes import macro_global as route
from unknown.data.http import FetchError, Response
from unknown.data.pit import ResearchStore
from unknown.data.registry import dataset, load_registry
from unknown.data.sources import bis, fred, macro_common as mc, macro_global, oecd, worldbank
from unknown.domain.clock import utc_ns

WB = json.dumps([{"page": 1, "pages": 1, "per_page": 1000, "total": 3, "lastupdated": "2026-10-08"}, [
    {"indicator": {"id": "NY.GDP.MKTP.KD.ZG"}, "country": {"id": "US"}, "countryiso3code": "USA", "date": "2025", "value": None,
     "obs_status": ""},
    {"indicator": {"id": "NY.GDP.MKTP.KD.ZG"}, "country": {"id": "US"}, "countryiso3code": "USA", "date": "2024", "value": 2.80000000000001,
     "obs_status": ""},
    {"indicator": {"id": "NY.GDP.MKTP.KD.ZG"}, "country": {"id": "DE"}, "countryiso3code": "DEU", "date": "2024", "value": -0.25,
     "obs_status": ""}]]).encode()
OECD_CSV = ("STRUCTURE,STRUCTURE_ID,ACTION,REF_AREA,FREQ,METHODOLOGY,MEASURE,UNIT_MEASURE,EXPENDITURE,ADJUSTMENT,TRANSFORMATION,"
            "TIME_PERIOD,OBS_VALUE,BASE_PER,OBS_STATUS,UNIT_MULT,DECIMALS\n"
            "DATAFLOW,X,I,USA,M,N,CPI,PA,_T,N,GY,2026-08,3.396548,,A,,2\n"
            "DATAFLOW,X,I,USA,M,N,CPI,PA,_T,N,GY,2026-09,3.737685,,A,,2\n"
            "DATAFLOW,X,I,IND,M,N,CPI,PA,_T,N,GY,2026-09,,,M,,2\n"
            "DATAFLOW,X,I,FRA,M,N,CPI,PA,_T,N,GY,2026-09,1.1,,A,,2\n").encode()
BIS_CSV = ("FREQ,REF_AREA,UNIT_MEASURE,UNIT_MULT,TIME_FORMAT,COMPILATION,DECIMALS,SOURCE_REF,SUPP_INFO_BREAKS,TITLE,TIME_PERIOD,"
           "OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK\n"
           "M,US,368,0,,mid-point of the target range,4,US Federal Reserve System,, Policy rates - US,2026-08,3.625,A,F,\n"
           "M,US,368,0,,mid-point of the target range,4,US Federal Reserve System,, Policy rates - US,2026-09,3.875,A,F,\n"
           "M,XM,368,0,,ECB deposit facility,4,European Central Bank,, Policy rates - euro area,2026-09,2.00,A,F,\n"
           "M,IN,368,0,,policy repo rate,4,Reserve Bank of India,, Policy rates - India,2026-09,5.50,A,F,\n").encode()
RECEIPT = utc_ns(2026, 10, 23, 12)


class Stub:
    def __init__(self, bodies: dict[str, bytes] | bytes, receipt_ns: int = RECEIPT):
        self.bodies, self.receipt_ns, self.urls = bodies, receipt_ns, []

    def get(self, url):
        self.urls.append(url)
        body = self.bodies if isinstance(self.bodies, bytes) else next(v for k, v in self.bodies.items() if k in url)
        return Response(url, 200, body, "text/csv", self.receipt_ns)


def tick():
    t = [RECEIPT]

    def now():
        t[0] += 1
        return t[0]
    return now


class MacroSourceTests(unittest.TestCase):
    def setUp(self):
        self.s = ResearchStore(":memory:")

    def tearDown(self):
        self.s.close()

    def test_countries_come_from_the_venue_registry(self):
        codes = [c.code for c in mc.countries()]
        self.assertEqual(codes, ["US", "IN", "DE", "FR", "NL", "BE", "PT", "IT", "ES"])
        self.assertEqual(sorted(mc.eu_equity_mics()), ["XAMS", "XBRU", "XETR", "XLIS", "XMAD", "XMIL", "XPAR"])
        self.assertTrue(all(c.euro_area for c in mc.countries() if c.eu_member))

    def test_world_bank_values_exact_and_null_is_missing_not_zero(self):
        out = worldbank.ingest(self.s, Stub(WB), tick(), start_year=2020, end_year=2025, columns=worldbank.WB_COLUMNS[:1])
        self.assertEqual(out["NEW"], 3)
        rows = self.s.as_of(mc.entity("wb_gdp_growth", "US"), "value", RECEIPT, dataset=worldbank.DATASET)
        by = {r["period_end"]: r for r in rows}
        self.assertEqual(by["2024-12-31"]["value"], "2.80000000000001")           # literal text, never a float round trip
        self.assertIsNone(by["2025-12-31"]["value"])
        self.assertEqual(by["2025-12-31"]["missing_reason"], "NOT_YET_PUBLISHED")
        self.assertIn("available_from_first_capture", json.loads(by["2024-12-31"]["quality"]))
        self.assertEqual(by["2024-12-31"]["available_time_ns"], RECEIPT)
        self.assertEqual(self.s.as_of(mc.entity("wb_gdp_growth", "US"), "value", RECEIPT - 1, dataset=worldbank.DATASET), [])

    def test_revision_is_a_new_publication_at_its_receipt_time(self):
        worldbank.ingest(self.s, Stub(WB), tick(), start_year=2020, end_year=2025, columns=worldbank.WB_COLUMNS[:1])
        revised = WB.replace(b"-0.25", b"-0.3")
        later = RECEIPT + 86_400 * 10**9
        out = worldbank.ingest(self.s, Stub(revised, later), tick(), start_year=2020, end_year=2025, columns=worldbank.WB_COLUMNS[:1])
        self.assertEqual((out["REVISED"], out["UNCHANGED"]), (1, 2))
        eid = mc.entity("wb_gdp_growth", "DE")
        self.assertEqual(self.s.as_of(eid, "value", RECEIPT, dataset=worldbank.DATASET)[0]["value"], "-0.25")
        r = self.s.as_of(eid, "value", later, dataset=worldbank.DATASET)[0]
        self.assertEqual(r["value"], "-0.3")
        self.assertIn("revised_after_first_seen", json.loads(r["quality"]))

    def test_oecd_csv_and_one_request_for_all_countries(self):
        stub = Stub(OECD_CSV)
        out = oecd.ingest(self.s, stub, tick(), start_period="2026-08", columns=oecd.OECD_COLUMNS[:1])
        self.assertEqual(len(stub.urls), 1)
        self.assertIn("USA+IND+DEU+FRA+NLD+BEL+PRT+ITA+ESP.M.N.CPI.PA._T.N.GY", stub.urls[0])
        self.assertEqual(out["NEW"], 4)
        india = self.s.as_of(mc.entity("oecd_cpi_yoy", "IN"), "value", RECEIPT, dataset=oecd.DATASET)
        self.assertEqual(india[0]["missing_reason"], "NOT_REPORTED")
        self.assertIn("DEU", out["series"][0]["not_returned"])
        unemp = oecd.OECD_COLUMNS[1]
        self.assertNotIn("IN", unemp.areas)
        self.assertNotIn("IND", oecd.url(unemp.column_id, ["USA", "DEU"], "2026-01"))

    def test_bis_backfill_available_at_month_end_and_live_at_receipt(self):
        bis.ingest(self.s, Stub(BIS_CSV, utc_ns(2026, 9, 20)), tick(), start_period="2026-08")
        rows = self.s.as_of(mc.entity("bis_policy_rate", "US"), "value", utc_ns(2026, 9, 21), dataset=bis.DATASET)
        aug, sep = rows
        self.assertEqual(aug["available_time_ns"], utc_ns(2026, 8, 31, 23, 59, 59))
        self.assertIn("availability_estimated_backfill", json.loads(aug["quality"]))
        self.assertEqual(sep["available_time_ns"], utc_ns(2026, 9, 20))               # captured before the month ended
        attrs = json.loads(self.s.conn.execute("SELECT attributes FROM entity WHERE entity_id=?",
                                               (mc.entity("bis_policy_rate", "IN"),)).fetchone()[0])
        self.assertEqual(attrs["source_ref"], "Reserve Bank of India")

    def test_blocked_sources_refuse_storage(self):
        reg = load_registry()
        for ds in ("imf.data", "rbi.website_rates", "fbil.reference_rates", "nse.website", "bse.website", "sebi.website"):
            self.assertFalse(dataset(reg, ds).permits("storage"), ds)
            with self.assertRaises(PermissionError):
                mc.require_storage(ds)
        for ds in ("worldbank.wdi", "oecd.sdmx_macro", "bis.cbpol", "fred.series", "xbrl_org.esef_facts", "esma.firds_equity"):
            d = dataset(reg, ds)
            self.assertTrue(d.permits("storage") and not d.permits("redistribution") and not d.permits("model_processing_cloud"), ds)
            self.assertEqual(d.rights_verified_on, "2026-10-23")
        self.assertEqual(reg["oecd"].rate_limit.per_seconds, 180.0)

    def test_comparison_cells_label_source_area_and_reasons(self):
        worldbank.ingest(self.s, Stub(WB), tick(), start_year=2020, end_year=2025, columns=worldbank.WB_COLUMNS[:1])
        oecd.ingest(self.s, Stub(OECD_CSV), tick(), start_period="2026-08", columns=oecd.OECD_COLUMNS[:1])
        bis.ingest(self.s, Stub(BIS_CSV), tick(), start_period="2026-08")
        t = RECEIPT + 10
        cmp = macro_global.comparison(self.s, t)
        rows = {r["country"]["code"]: r["cells"] for r in cmp["rows"]}
        self.assertEqual(rows["US"]["wb_gdp_growth"]["value"], "2.80000000000001")    # 2025 is null: latest *value* is 2024
        self.assertEqual(rows["US"]["wb_gdp_growth"]["period_end"], "2024-12-31")
        self.assertEqual(rows["US"]["oecd_cpi_yoy"]["value"], "3.737685")
        self.assertEqual(rows["IN"]["oecd_cpi_yoy"]["status"], "NOT_AVAILABLE_AT_AS_OF")
        self.assertEqual(rows["IN"]["oecd_unemployment"]["status"], "NOT_COVERED")
        self.assertEqual(rows["US"]["wb_cpi_inflation"]["status"], "NOT_INGESTED")
        de_rate = rows["DE"]["bis_policy_rate"]
        self.assertEqual((de_rate["value"], de_rate["area"]), ("2.00", "XM"))
        self.assertIn("area_rate:XM", de_rate["flags"])
        self.assertEqual(rows["IN"]["bis_policy_rate"]["value"], "5.50")
        self.assertTrue(all(c["value"] is None for r in rows.values() for c in r.values() if c["status"] != "OK"))
        self.assertIn("imf", [b["provider_id"] for b in cmp["blocked_sources"]])
        past = macro_global.comparison(self.s, RECEIPT - 1)
        self.assertEqual(past["rows"][0]["cells"]["wb_gdp_growth"]["status"], "NOT_AVAILABLE_AT_AS_OF")
        s = macro_global.series(self.s, "bis_policy_rate", t, country_codes=["US", "DE", "FR"])
        self.assertEqual([x["status"] for x in s["series"]], ["OK", "OK", "SAME_AS"])
        self.assertEqual([p["value"] for p in s["series"][0]["points"]], ["3.625", "3.875"])


class FredTests(unittest.TestCase):
    def test_inactive_without_key_file_and_no_request(self):
        with tempfile.TemporaryDirectory() as d:
            st = fred.status(Path(d))
            self.assertEqual(st["status"], "INACTIVE")
            self.assertTrue(st["key_file"].endswith("secrets/fred_api_key"))
            stub = Stub(b"{}")
            s = ResearchStore(":memory:")
            self.addCleanup(s.close)
            self.assertEqual(fred.ingest(s, stub, tick(), home=Path(d))["status"], "INACTIVE")
            self.assertEqual(stub.urls, [])

    def test_active_with_key_redacts_it_everywhere(self):
        key = "abcdef0123456789abcdef0123456789"
        body = json.dumps({"observations": [{"date": "2026-08-01", "value": "4.3"}, {"date": "2026-09-01", "value": "."}]}).encode()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "secrets").mkdir()
            (Path(d) / "secrets" / "fred_api_key").write_text(key + "\n")
            s = ResearchStore(":memory:")
            self.addCleanup(s.close)
            out = fred.ingest(s, Stub(body), tick(), home=Path(d), series=("UNRATE",))
            self.assertEqual(out["NEW"], 2)
            self.assertNotIn(key, json.dumps(out))
            self.assertNotIn(key, " ".join(r[0] for r in s.conn.execute("SELECT request FROM raw_object")))
            rows = s.as_of(mc.entity("fred_UNRATE", "US"), "value", RECEIPT, dataset=fred.DATASET)
            self.assertEqual([(r["period_end"], r["value"], r["missing_reason"]) for r in rows],
                             [("2026-08-31", "4.3", None), ("2026-09-30", None, "NOT_REPORTED")])

            class Failing:
                def get(self, url):
                    raise FetchError(f"giving up on {url} after 5 attempts: HTTP 503")
            with self.assertRaises(FetchError) as ctx:
                fred.ingest(s, Failing(), tick(), home=Path(d), series=("UNRATE",))
            self.assertNotIn(key, str(ctx.exception))
            self.assertIn("api_key=REDACTED", str(ctx.exception))


class MacroGlobalRouteTests(unittest.TestCase):
    def test_route_registered_and_reads_store(self):
        self.assertIn(("macro", "global"), [p for p, _ in load_routes()])
        with tempfile.TemporaryDirectory() as d:
            ctx = SimpleNamespace(paths=SimpleNamespace(root=Path(d)))
            self.assertEqual(route.handler(ctx, ["macro", "global"], {}, "SIM-1", None)["status"], "UNAVAILABLE")
            (Path(d) / "research").mkdir()
            s = ResearchStore(Path(d) / "research" / "research.sqlite")
            bis.ingest(s, Stub(BIS_CSV), tick(), start_period="2026-08")
            s.close()
            body = route.handler(ctx, ["macro", "global"], {"countries": "US,IN"}, "SIM-1", None)
            self.assertEqual([r["country"]["code"] for r in body["rows"]], ["US", "IN"])
            ser = route.handler(ctx, ["macro", "global", "series"], {"column": "bis_policy_rate", "countries": "IN"}, "SIM-1", None)
            self.assertEqual(ser["series"][0]["points"][0]["value"], "5.50")
            with self.assertRaises(ValueError):
                route.handler(ctx, ["macro", "global", "series"], {}, "SIM-1", None)
            with self.assertRaises(LookupError):
                route.handler(ctx, ["macro", "global", "series"], {"column": "nope"}, "SIM-1", None)


if __name__ == "__main__":
    unittest.main()
