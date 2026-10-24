"""Point-in-time equity screener (CAP-MKT-10): compile, run, missing/NM treatment, look-ahead, reproducible runs,
saved screens and the routes. FIXTURE issuers only (test_company_fixtures, ingested through the real SEC path).
"""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_company_fixtures import CIK, INGESTED_AT, fixture_home, ns  # noqa: E402

from unknown.api.routes import screener as route  # noqa: E402
from unknown.data.fundamentals.metrics import record_price_input  # noqa: E402
from unknown.data.pit import OPEN_END, Observation  # noqa: E402
from unknown.data.sources import esef  # noqa: E402
from unknown.domain.clock import NS_PER_S  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.screener import engine  # noqa: E402
from unknown.screener.fields import FIELDS, catalog  # noqa: E402
from unknown.screener.store import ScreenStore, path_for  # noqa: E402

EID = f"SEC-CIK-{CIK:010d}"
LOSS = "SEC-CIK-0009000004"
LEI = "FIXTURE0000000000001"
AFTER = INGESTED_AT + 86_400 * NS_PER_S
AS_OF = ns("2026-09-01T00:00:00Z")


def run(store, definition, as_of=AS_OF, **kw):
    kw.setdefault("now_ns", AFTER)
    return engine.run(store, definition, as_of, **kw)


def row(result, eid):
    return next((r for r in result["results"] if r["entity_id"] == eid), None)


class ScreenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.store = fixture_home()
        t = ns("2026-03-20T12:00:00Z")
        cls.store.upsert_entity(esef.entity_id(LEI), "eu_issuer", "Fixture Europa SE (synthetic)", {"countries": ["FR"], "lei": LEI})
        for concept, v in (("ifrs-full:Revenue", "1000000"), ("ifrs-full:ProfitLoss", "80000")):
            cls.store.record(Observation(dataset=esef.DATASET, entity_id=esef.entity_id(LEI), field=concept, unit="EUR", currency="EUR",
                                         value=Fixed.parse(v), period_start="2025-01-01", period_end="2025-12-31", period_kind="FY",
                                         receipt_time_ns=t, available_time_ns=t, publication_id="fixture-esef-2025", source_ref="FIXTURE"),
                             INGESTED_AT)
        cls.ctx = SimpleNamespace(paths=SimpleNamespace(root=cls.root))

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    # ------------------------------------------------------------------ compile
    def test_compile_refuses_floats_frames_and_currency_less_money(self):
        with self.assertRaises(engine.ScreenRefused):
            engine.compile_definition({"criteria": [{"field": "gross_margin", "op": "gt", "value": 0.4}]})
        with self.assertRaisesRegex(engine.ScreenRefused, "not point-in-time"):
            engine.compile_definition({"criteria": [{"field": "revenue_ttm", "op": "gt", "value": "1", "currency": "USD",
                                                     "source": "https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2025.json"}]})
        with self.assertRaisesRegex(engine.ScreenRefused, "currency"):
            engine.compile_definition({"criteria": [{"field": "revenue_ttm", "op": "gt", "value": "1"}]})
        with self.assertRaises(engine.ScreenRefused):
            engine.compile_definition({"criteria": [{"field": "no_such_field", "op": "gt", "value": "1"}]})
        with self.assertRaises(engine.ScreenRefused):
            engine.compile_definition({"criteria": [{"field": "roe", "op": "between", "value": "2", "value2": "1"}]})
        spec = engine.compile_definition({"criteria": [{"field": "roe", "op": "gte", "value": "0.10"}]})
        self.assertEqual(spec["criteria"][0], {"field": "roe", "op": "gte", "value": "0.10", "missing": "exclude", "nm": "exclude"})
        names = {f["field"] for f in catalog()}
        self.assertTrue({"pe_ttm", "revenue_fy", "net_margin_fy", "price"} <= names)

    # ------------------------------------------------------------------ outcomes
    def test_criteria_pass_fail_and_missing_reasons(self):
        r = run(self.store, {"criteria": [{"field": "gross_margin", "op": "gte", "value": "0"}], "columns": ["revenue_ttm"]})
        counts = r["counts"]
        self.assertEqual(sum(counts.values()), r["coverage"]["universe"])
        self.assertIsNotNone(row(r, EID))
        europa = [x for x in r["excluded"] if x["source"] == "esef"][0]
        self.assertEqual(europa["reasons"][0]["reason"], "NOT_APPLICABLE")       # TTM formula on an annual-only filer
        self.assertIn("gross_margin: NOT_APPLICABLE", r["excluded_by_reason"])
        self.assertTrue(all(v["value"] is not None or v.get("reason") for x in r["results"] for v in x["values"].values()))

    def test_nm_excluded_by_default_and_included_on_request(self):
        crit = {"field": "pe_ttm", "op": "lt", "value": "100"}
        price = record_price_input(self.store, LOSS, Fixed.parse("10"), "USD", "fixture-owner", ns("2026-08-31T20:00:00Z"),
                                   ns("2026-08-31T20:00:00Z"))
        self.assertIsNotNone(price.obs_id)
        r = run(self.store, {"criteria": [crit], "universe": {"entities": [LOSS]}})
        self.assertEqual(r["counts"]["EXCLUDED"], 1)
        self.assertEqual(r["excluded"][0]["reasons"][0]["reason"], "NOT_APPLICABLE")
        r2 = run(self.store, {"criteria": [{**crit, "nm": "include"}], "universe": {"entities": [LOSS]}})
        self.assertEqual(r2["results"][0]["outcome"], "UNKNOWN")
        self.assertEqual(r2["results"][0]["checks"][0]["status"], "NM")
        self.assertEqual(r2["manifest"]["price_inputs"][LOSS]["source"], "fixture-owner")

    def test_missing_price_is_a_reason_never_a_pass(self):
        crit = {"field": "price_to_sales", "op": "lt", "value": "1000"}
        r = run(self.store, {"criteria": [crit], "universe": {"entities": [EID]}})
        self.assertEqual(r["counts"], {"PASS": 0, "UNKNOWN": 0, "FAIL": 0, "EXCLUDED": 1})
        reason = r["excluded"][0]["reasons"][0]
        self.assertIn(reason["reason"], ("NO_DATA", "NO_SOURCE"))
        r2 = run(self.store, {"criteria": [{**crit, "missing": "include"}], "universe": {"entities": [EID]}})
        self.assertEqual(r2["results"][0]["outcome"], "UNKNOWN")
        self.assertIsNone(r2["results"][0]["values"]["price_to_sales"]["value"])

    def test_live_quote_only_for_now_and_recorded(self):
        quote = {"key": "FXW.XNAS", "last": "250.00", "currency": "USD", "source": "fixture-bus", "data_type": "live",
                 "ts_source_ns": str(AFTER - NS_PER_S)}
        r = run(self.store, {"criteria": [{"field": "price", "op": "gt", "value": "100", "currency": "USD"}],
                             "universe": {"entities": [EID]}}, as_of=AFTER, live_quotes={"FXW.XNAS": quote}, live_status={})
        self.assertEqual(r["results"][0]["values"]["price"]["value"], "250.00")
        self.assertTrue(r["manifest"]["price_inputs"][EID]["source"].startswith("live:fixture-bus"))
        future = {**quote, "ts_source_ns": str(AFTER + NS_PER_S)}
        r2 = run(self.store, {"criteria": [{"field": "price", "op": "gt", "value": "100", "currency": "USD"}],
                              "universe": {"entities": [EID]}}, as_of=AFTER, live_quotes={"FXW.XNAS": future}, live_status={})
        self.assertEqual(r2["counts"]["EXCLUDED"], 1)                       # a quote after the as-of time is look-ahead: refused

    def test_currency_mismatch_is_not_applicable(self):
        r = run(self.store, {"universe": {"sources": ["esef"]},
                             "criteria": [{"field": "revenue_fy", "op": "gt", "value": "1", "currency": "USD"}]})
        self.assertEqual(r["excluded"][0]["reasons"][0]["reason"], "NOT_APPLICABLE")
        r2 = run(self.store, {"universe": {"sources": ["esef"]},
                              "criteria": [{"field": "revenue_fy", "op": "gt", "value": "1", "currency": "EUR"},
                                           {"field": "net_margin_fy", "op": "gte", "value": "0.08"}]})
        self.assertEqual(r2["results"][0]["values"]["net_margin_fy"]["value"], "0.0800000000")

    # ------------------------------------------------------------------ point in time
    def test_look_ahead_a_later_filing_changes_the_result_only_after_acceptance(self):
        q3 = ns("2026-07-31T20:00:00Z")              # FY2026 Q3 10-Q acceptance of the fixture issuers
        before = run(self.store, {"criteria": [{"field": "revenue_ttm", "op": "gt", "value": "0", "currency": "USD"}],
                                  "universe": {"entities": [EID]}}, as_of=q3 - NS_PER_S)
        after = run(self.store, {"criteria": [{"field": "revenue_ttm", "op": "gt", "value": "0", "currency": "USD"}],
                                 "universe": {"entities": [EID]}}, as_of=q3)
        v0, v1 = (Fixed.parse(x["results"][0]["values"]["revenue_ttm"]["value"]) for x in (before, after))
        self.assertGreater(v1, v0)
        threshold = {"field": "revenue_ttm", "op": "gte", "value": v1.to_str(), "currency": "USD"}
        self.assertEqual(run(self.store, {"criteria": [threshold], "universe": {"entities": [EID]}}, as_of=q3 - NS_PER_S)["counts"]["FAIL"], 1)
        self.assertEqual(run(self.store, {"criteria": [threshold], "universe": {"entities": [EID]}}, as_of=q3)["counts"]["PASS"], 1)

    def test_rerun_reproduces_despite_later_knowledge(self):
        d = {"criteria": [{"field": "revenue_ttm", "op": "gt", "value": "0", "currency": "USD"}], "sort": {"field": "revenue_ttm"}}
        first = run(self.store, d, now_ns=AFTER)
        self.assertEqual(engine.rerun(self.store, first, now_ns=AFTER + NS_PER_S)["reproducible"], True)
        # A silent correction of a revenue fact this system learns later must not change the stored run's answer.
        r = self.store.conn.execute("SELECT * FROM observation WHERE entity_id=? AND dataset='sec.companyfacts' AND field LIKE 'us-gaap:Revenue%' "
                                    "AND period_start IS NOT NULL AND available_time_ns<=? AND known_to_ns=? ORDER BY period_end DESC, "
                                    "period_start DESC LIMIT 1", (EID, AS_OF, OPEN_END)).fetchone()
        obs = Observation(dataset=r["dataset"], entity_id=EID, field=r["field"], unit=r["unit"], currency=r["currency"],
                          value=Fixed.parse(r["value"]) + Fixed(7), period_start=r["period_start"], period_end=r["period_end"],
                          period_kind=r["period_kind"], receipt_time_ns=AFTER + 5 * NS_PER_S, available_time_ns=r["available_time_ns"],
                          publication_id=r["publication_id"], source_ref="FIXTURE-correction")
        self.store.record(replace(obs), AFTER + 5 * NS_PER_S)
        try:
            check = engine.rerun(self.store, first, now_ns=AFTER + 10 * NS_PER_S)
            self.assertTrue(check["reproducible"])
            fresh = run(self.store, d, now_ns=AFTER + 10 * NS_PER_S)
            self.assertNotEqual(fresh["result_hash"], first["result_hash"])
        finally:
            cur = self.store.conn.execute("SELECT obs_id FROM observation WHERE source_ref='FIXTURE-correction'").fetchone()[0]
            self.store.conn.execute("DELETE FROM observation WHERE obs_id=?", (cur,))
            self.store.conn.execute("UPDATE observation SET known_to_ns=? WHERE obs_id=?", (OPEN_END, r["obs_id"]))

    def test_manifest_records_versions_and_universe(self):
        r = run(self.store, {"criteria": [{"field": "roe", "op": "gte", "value": "0"}]})
        m = r["manifest"]
        self.assertEqual(m["engine"], "screener@1")
        self.assertEqual(m["universe"]["count"], len(m["universe"]["entity_ids"]))
        self.assertGreater(m["data_versions"]["sec.companyfacts"]["observations"], 0)
        self.assertIn("roe@1", m["formulas"])
        self.assertEqual(len(r["result_hash"]), 64)

    # ------------------------------------------------------------------ store and routes
    def test_saved_screens_are_versioned_and_runs_append_only(self):
        ss = ScreenStore(path_for(self.root))
        try:
            s1 = ss.save("Profitable", {"criteria": [{"field": "roe", "op": "gte", "value": "0.1"}]}, AFTER)
            s2 = ss.save("Profitable", {"criteria": [{"field": "roe", "op": "gte", "value": "0.2"}]}, AFTER + 1, s1["screen_id"])
            self.assertEqual((s2["version"], len(s2["versions"])), (2, 2))
            run_id = ss.record_run(run(self.store, s2["definition"]), AFTER + 2, s2["screen_id"], 2)
            with self.assertRaises(Exception):
                ss.conn.execute("UPDATE screen_run SET result_hash='x' WHERE run_id=?", (run_id,))
            with self.assertRaises(ValueError):
                ss.save("", {}, AFTER)
        finally:
            ss.close()

    def test_routes(self):
        fields = route.handle(self.ctx, ["screener", "fields"], {}, "SIM", None)
        self.assertEqual(len(fields["fields"]), len(FIELDS))
        d = {"criteria": [{"field": "roe", "op": "gte", "value": "0"}]}
        known = "2026-12-02"           # the fixture was ingested on 2026-12-01: a cutoff after it sees the data
        adhoc = json.loads(json.dumps(route.handle(self.ctx, ["screener", "run"], {"definition": json.dumps(d), "as_of": "2026-09-01",
                                                                                    "known_at": known}, "SIM", None), allow_nan=False))
        self.assertIn("not used", adhoc["live_quotes"])
        self.assertGreater(adhoc["counts"]["PASS"], 0)
        early = route.handle(self.ctx, ["screener", "run"], {"definition": json.dumps(d), "as_of": "2026-09-01", "known_at": "2026-11-30"},
                             "SIM", None)
        self.assertEqual(early["counts"]["PASS"], 0)          # nothing was known to this system before ingestion
        with self.assertRaises(ValueError):
            route.handle(self.ctx, ["screener", "run"], {"definition": "{bad"}, "SIM", None)
        with self.assertRaises(ValueError):
            route.post_screens(self.ctx, ["screener", "screens"], {"name": "x", "definition": {"criteria": [{"field": "nope"}]}}, None)
        saved = route.post_screens(self.ctx, ["screener", "screens"], {"name": "Route screen", "definition": d}, None)
        out = route.post_runs(self.ctx, ["screener", "runs"], {"screen_id": saved["screen_id"], "as_of": "2026-09-01", "known_at": known}, None)
        self.assertGreater(out["counts"]["PASS"], 0)
        stored = route.handle(self.ctx, ["screener", "runs", out["run_id"]], {}, "SIM", None)
        self.assertEqual(stored["result_hash"], out["result_hash"])
        ver = route.handle(self.ctx, ["screener", "runs", out["run_id"], "verify"], {}, "SIM", None)
        self.assertTrue(ver["reproducible"])
        listing = route.handle(self.ctx, ["screener", "screens"], {}, "SIM", None)
        self.assertIn(saved["screen_id"], [s["screen_id"] for s in listing["screens"]])
        with self.assertRaises(LookupError):
            route.handle(self.ctx, ["screener", "runs", "run-missing"], {}, "SIM", None)

    def test_route_live_bus_used_only_when_streaming(self):
        class Bus:
            def snapshot(self, keys):
                return {"quotes": [], "status": [{"key": k, "state": "SUBSCRIBED", "reason": "no data yet"} for k in keys]}
        ctx = SimpleNamespace(paths=self.ctx.paths, marketdata=Bus())
        d = {"criteria": [{"field": "price", "op": "gt", "value": "1", "currency": "USD"}], "universe": {"entities": [EID]}}
        out = route.handle(ctx, ["screener", "run"], {"definition": json.dumps(d)}, "SIM", None)
        self.assertEqual(out["live_quotes"], "used where streaming")
        self.assertEqual(out["excluded"][0]["reasons"][0]["reason"], "PENDING")


if __name__ == "__main__":
    unittest.main()
