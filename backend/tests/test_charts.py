"""Chart bars (CAP-MKT-07): exact session-anchored aggregation, gaps, source labels and the synthetic rule.

Every price and volume below is a TEST FIXTURE, not market data. Alpaca replies are hand-written fixtures in the
documented shape (docs.alpaca.markets, stock bars v2); no network is used.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from unknown.charts import service
from unknown.charts.aggregate import BaseBar, aggregate
from unknown.charts.intervals import INTERVALS, NS, compatible_bases
from unknown.charts.sessions import SessionClock
from unknown.service.paths import Paths

try:
    from test_api_server import LiveCase
except ImportError:          # pragma: no cover - run from another directory
    LiveCase = None

SRC = Path(__file__).resolve().parents[1] / "src"
NY, KOL = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")
D = Decimal


def ns(y, mo, d, h, mi, tz=NY) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()) * NS


def minute_bars(start_ns: int, closes: list[str], step_s: int = 60, vol: str = "100") -> list[BaseBar]:
    out = []
    for i, c in enumerate(closes):
        s = start_ns + i * step_s * NS
        out.append(BaseBar(s, s + step_s * NS, D(c), D(c) + D("0.05"), D(c) - D("0.05"), D(c), D(vol), D(c), 3))
    return out


LATER = ns(2026, 12, 31, 0, 0)


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.nyse = SessionClock("XNYS")

    def test_five_minute_bucket_is_exact(self):
        open_ = ns(2026, 10, 20, 9, 30)
        bars = minute_bars(open_, ["10.01", "10.03", "9.99", "10.10", "10.07"], vol="100")
        bars[2] = BaseBar(bars[2].start_ns, bars[2].end_ns, D("10.00"), D("10.20"), D("9.90"), D("9.99"), D("50"),
                          D("10.0333333"), 1)
        r = aggregate(bars, 60, INTERVALS["5m"], self.nyse, open_, open_ + 3600 * NS, LATER)
        self.assertEqual(len(r.bars), 1)
        b = r.bars[0]
        self.assertEqual((b["open"], b["high"], b["low"], b["close"], b["volume"], b["trades"]),
                         ("10.01", "10.20", "9.90", "10.07", "450", 13))
        # vwap = (10.01*100 + 10.03*100 + 10.0333333*50 + 10.10*100 + 10.07*100) / 450, 6 dp half-even
        exact = (D("10.01") * 100 + D("10.03") * 100 + D("10.0333333") * 50 + D("10.10") * 100 + D("10.07") * 100) / 450
        self.assertEqual(b["vwap"], format(exact.quantize(D("0.000001")), "f"))
        self.assertEqual((b["start_ns"], b["end_ns"], b["partial"], b["complete"]),
                         (str(open_), str(open_ + 300 * NS), False, True))

    def test_null_volume_is_unknown_never_zero(self):
        open_ = ns(2026, 10, 20, 9, 30)
        bars = minute_bars(open_, ["1", "2"])
        bars[1] = BaseBar(bars[1].start_ns, bars[1].end_ns, D(2), D(2), D(2), D(2), None)
        b = aggregate(bars, 60, INTERVALS["5m"], self.nyse, open_, open_ + 600 * NS, LATER).bars[0]
        self.assertIsNone(b["volume"])
        self.assertIsNone(b["vwap"])
        self.assertTrue(b["partial"])

    def test_hour_buckets_anchor_on_the_session_open_and_cut_at_the_close(self):
        nse = SessionClock("XNSE")
        self.assertEqual(compatible_bases(INTERVALS["1h"], nse.open_offsets())[0], 900)   # 09:15 open: not 1800
        open_ = ns(2026, 10, 21, 9, 15, KOL)   # 20 Oct is Dussehra (holiday)
        bars = minute_bars(open_, [str(100 + i) for i in range(25)], step_s=900)       # 09:15 .. 15:30
        r = aggregate(bars, 900, INTERVALS["1h"], nse, open_, open_ + 86_400 * NS, LATER)
        starts = [datetime.fromtimestamp(int(b["start_ns"]) // NS, KOL).strftime("%H:%M") for b in r.bars]
        self.assertEqual(starts, ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"])
        last = r.bars[-1]
        self.assertEqual(int(last["end_ns"]), ns(2026, 10, 21, 15, 30, KOL))
        self.assertEqual((last["expected_constituents"], last["constituents"], last["partial"]), (1, 1, False))

    def test_missing_bars_become_gaps_and_partial_buckets(self):
        open_ = ns(2026, 10, 20, 9, 30)
        bars = minute_bars(open_, ["1"] * 30)
        del bars[5:15]            # 09:35-09:45 missing: two whole 5m buckets
        del bars[16]              # original minute 26: one minute missing in the 09:55 bucket
        r = aggregate(bars, 60, INTERVALS["5m"], self.nyse, open_, open_ + 1800 * NS, LATER)
        self.assertEqual(r.gaps, [{"start_ns": str(open_ + 300 * NS), "end_ns": str(open_ + 900 * NS),
                                   "missing_buckets": 2, "reason": "NO_BARS"}])
        self.assertEqual([b["partial"] for b in r.bars], [False, False, False, True])

    def test_bars_outside_the_session_are_excluded(self):
        pre = minute_bars(ns(2026, 10, 20, 9, 0), ["5"] * 30)          # 09:00-09:30 pre-market
        r = aggregate(pre, 60, INTERVALS["5m"], self.nyse, ns(2026, 10, 20, 0, 0), LATER, LATER)
        self.assertEqual((r.bars, r.excluded_outside_session), ([], 30))

    def test_daily_week_and_month_follow_the_calendar(self):
        days = [date(2026, 11, d) for d in (23, 24, 25, 27, 30)]            # 26th is Thanksgiving
        bars = [BaseBar(0, 0, D("10"), D("11"), D("9"), D(str(10 + i)), D("1000"), day=d) for i, d in enumerate(days)]
        frm, to = ns(2026, 11, 23, 0, 0), ns(2026, 12, 1, 0, 0)
        daily = aggregate(bars, "day", INTERVALS["1d"], self.nyse, frm, to, LATER)
        self.assertEqual(daily.gaps, [])                                  # the holiday is not a gap
        self.assertEqual(int(daily.bars[3]["end_ns"]), ns(2026, 11, 27, 13, 0))   # early close
        weekly = aggregate(bars, "day", INTERVALS["1w"], self.nyse, frm, to, LATER)
        self.assertEqual([(b["open"], b["close"], b["volume"], b["constituents"], b["expected_constituents"],
                           b["partial"]) for b in weekly.bars],
                         [("10", "13", "4000", 4, 4, False), ("10", "14", "1000", 1, 5, True)])
        monthly = aggregate(bars[:4], "day", INTERVALS["1mo"], self.nyse, ns(2026, 11, 1, 0, 0), to, LATER)
        self.assertEqual((monthly.bars[0]["constituents"], monthly.bars[0]["expected_constituents"]), (4, 20))
        self.assertFalse(aggregate(bars, "day", INTERVALS["1d"], self.nyse, frm, to, ns(2026, 11, 30, 12, 0))
                         .bars[-1]["complete"])

    def test_missing_trading_day_is_a_gap(self):
        days = [date(2026, 10, d) for d in (19, 21)]
        bars = [BaseBar(0, 0, D(1), D(1), D(1), D(1), D(1), day=d) for d in days]
        r = aggregate(bars, "day", INTERVALS["1d"], self.nyse, ns(2026, 10, 19, 0, 0), ns(2026, 10, 22, 0, 0), LATER)
        self.assertEqual([(g["missing_buckets"], int(g["start_ns"])) for g in r.gaps], [(1, ns(2026, 10, 20, 9, 30))])

    def test_unknown_mic_falls_back_to_utc_days_and_says_so(self):
        clock = SessionClock("XQQQ")
        self.assertIsNone(clock.calendar_id)
        self.assertIn("no sourced trading calendar for MIC XQQQ", clock.finish_notes()[0])


BAR_COLS = ("provider, dataset_id, instrument_id, bar_spec_version, bar_size, what_to_show, use_rth, timezone, "
            "adjustment, adjustment_note, bar_start_ns, bar_end_ns, bar_date, open, high, low, close, volume, wap, "
            "bar_count, currency, receipt_time_ns, available_time_ns, availability_rule, known_from_ns, known_to_ns, "
            "request_id, raw, quality, fixture")


class ServiceCase(unittest.TestCase):
    NOW = ns(2026, 10, 23, 12, 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = Paths(Path(self.tmp.name))
        self.paths.secrets.mkdir(parents=True)
        self.ctx = SimpleNamespace(paths=self.paths)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, provider="IBKR", instrument="AAPL.XNYS", fixture=0, days=(19, 20, 21)):
        from unknown.broker.ibkr.history import BAR_SCHEMA     # the schema the history adapter writes
        db = self.paths.root / "research" / "research.sqlite"
        db.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript(BAR_SCHEMA)
        for i, d in enumerate(days):
            o = ns(2026, 10, d, 9, 30)
            conn.execute(f"INSERT INTO mkt_bar ({BAR_COLS}) VALUES ({','.join('?' * 30)})",
                         (provider, "fixture-ds", instrument, "v1", "1 day", "TRADES", 1, "America/New_York",
                          "unadjusted", "none", o, ns(2026, 10, d, 16, 0), f"202610{d}", "100.10", "101.20", "99.30",
                          f"100.{i}5", "12345", "100.4", 99, "USD", o, o, "rule", o, 2 ** 62, "r1", "{}", "{}", fixture))
        conn.commit()
        conn.close()

    def alpaca_keys(self, **extra):
        (self.paths.secrets / "alpaca.json").write_text(json.dumps({"key_id": "FIXTURE-ID", "secret_key": "FIXTURE-SECRET",
                                                                    **extra}))

    def http(self, pages, status=200):
        def get(url, headers):
            self.calls.append((url, headers))
            return status, json.dumps(pages[min(len(self.calls), len(pages)) - 1]).encode()
        return get

    def bars(self, http_get=None, **q):
        return service.bars(self.ctx, q, None, now_ns=self.NOW, http_get=http_get)


class StoredAndSyntheticTests(ServiceCase):
    def test_stored_bars_are_labelled_with_their_provider_and_unadjusted(self):
        self.store()
        r = self.bars(key="AAPL.XNYS", interval="1d", from_ns=ns(2026, 10, 19, 0, 0), to_ns=ns(2026, 10, 23, 0, 0))
        self.assertEqual((r["source"], r["data_type"], r["adjustment"], r["count"], r["currency"]),
                         ("IBKR", "DELAYED", "unadjusted", 3, "USD"))
        self.assertIn("splits and dividends", r["adjustment_note"])
        self.assertEqual([b["close"] for b in r["bars"]], ["100.05", "100.15", "100.25"])
        self.assertIsNone(r["label"])

    def test_any_provider_in_the_store_is_a_source(self):
        self.store(provider="OTHERBROKER")
        r = self.bars(key="AAPL.XNYS", interval="1w", from_ns=ns(2026, 10, 19, 0, 0), to_ns=ns(2026, 10, 23, 0, 0))
        self.assertEqual((r["source"], r["bars"][0]["close"], r["bars"][0]["volume"]), ("OTHERBROKER", "100.25", "37035"))

    def test_fixture_rows_never_chart_and_synthetic_is_labelled(self):
        self.store(fixture=1)
        r = self.bars(key="AAPL.XNYS", interval="1d")
        self.assertEqual((r["source"], r["data_type"]), ("SYNTHETIC", "SYNTHETIC"))
        self.assertTrue(r["label"].startswith("SYNTHETIC"))
        self.assertGreater(r["count"], 400)
        again = self.bars(key="AAPL.XNYS", interval="1d")
        self.assertEqual(r["bars"], again["bars"])                       # deterministic demo data

    def test_synthetic_is_refused_when_a_real_source_is_configured(self):
        self.store()
        with self.assertRaisesRegex(ValueError, "SYNTHETIC refused"):
            self.bars(key="AAPL.XNYS", interval="1d", source="SYNTHETIC")
        r = self.bars(key="AAPL.XNYS", interval="1d", from_ns=ns(2026, 1, 5, 0, 0), to_ns=ns(2026, 1, 9, 0, 0))
        self.assertEqual((r["source"], r["count"], r["available"]), ("IBKR", 0, False))
        self.assertIn("not substituted", r["missing_reason"])

    def test_owner_source_order_is_honoured(self):
        self.store()
        self.alpaca_keys()
        (self.paths.root / "config").mkdir()
        (self.paths.root / "config" / "charts.json").write_text(json.dumps({"source_order": ["ALPACA", "IBKR"]}))
        page = {"bars": [{"t": "2026-10-20T04:00:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.75, "v": 10, "n": 1,
                          "vw": 1.6}], "next_page_token": None}
        r = self.bars(self.http([page]), key="AAPL.XNYS", interval="1d", from_ns=ns(2026, 10, 19, 0, 0),
                      to_ns=ns(2026, 10, 23, 0, 0))
        self.assertEqual((r["source"], r["bars"][0]["close"]), ("ALPACA", "1.75"))

    def test_validation(self):
        for q, msg in (({}, "key is required"), ({"key": "A.XNYS", "interval": "2m"}, "interval must be"),
                       ({"key": "A.XNYS", "interval": "1m", "from_ns": "0"}, "at most 31 days"),
                       ({"key": "A.XNYS", "from_ns": "5", "to_ns": "4"}, "before")):
            with self.subTest(q=q), self.assertRaisesRegex(ValueError, msg):
                self.bars(**q)


class AlpacaTests(ServiceCase):
    PAGE1 = {"bars": [{"t": "2026-10-20T13:30:00Z", "o": 187.15, "h": 187.5, "l": 187.0, "c": 187.33, "v": 1200,
                       "n": 10, "vw": 187.2}], "symbol": "AAPL", "next_page_token": "p2"}
    PAGE2 = {"bars": [{"t": "2026-10-20T14:00:00Z", "o": 187.33, "h": 188.1, "l": 187.3, "c": 188.0, "v": 800,
                       "n": 7, "vw": 187.9}], "symbol": "AAPL", "next_page_token": None}

    def test_rest_request_pagination_and_exact_decimals(self):
        self.alpaca_keys()
        r = self.bars(self.http([self.PAGE1, self.PAGE2]), key="AAPL.XNYS", interval="1h",
                      from_ns=ns(2026, 10, 20, 9, 30), to_ns=ns(2026, 10, 20, 16, 0))
        self.assertEqual(len(self.calls), 2)
        url, headers = self.calls[0]
        parts = urlsplit(url)
        q = parse_qs(parts.query)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", "https://data.alpaca.markets/v2/stocks/AAPL/bars")
        self.assertEqual((q["timeframe"], q["adjustment"], q["feed"], q["limit"]), (["30Min"], ["raw"], ["iex"], ["10000"]))
        self.assertEqual((headers["APCA-API-KEY-ID"], headers["APCA-API-SECRET-KEY"]), ("FIXTURE-ID", "FIXTURE-SECRET"))
        self.assertEqual(parse_qs(urlsplit(self.calls[1][0]).query)["page_token"], ["p2"])
        b = r["bars"][0]
        self.assertEqual((r["source"], r["data_type"], r["adjustment"]), ("ALPACA", "REALTIME", "unadjusted"))
        self.assertEqual((b["open"], b["high"], b["low"], b["close"], b["volume"], b["trades"], b["vwap"]),
                         ("187.15", "188.1", "187.0", "188.0", "2000", 17, "187.480000"))
        self.assertEqual(int(b["start_ns"]), ns(2026, 10, 20, 9, 30))
        self.assertNotIn("FIXTURE-SECRET", json.dumps(r))

    def test_failure_is_reported_and_never_replaced_by_synthetic(self):
        self.alpaca_keys(feed="sip")
        r = self.bars(self.http([{"message": "forbidden"}], status=403), key="AAPL.XNAS", interval="1d")
        self.assertEqual((r["available"], r["source"], r["bars"]), (False, None, []))
        alp = next(s for s in r["sources_considered"] if s["source"] == "ALPACA")
        self.assertIn("HTTP 403", alp["error"])

    def test_non_us_market_is_not_covered_so_the_demo_may_use_synthetic(self):
        self.alpaca_keys()
        r = self.bars(self.http([self.PAGE1]), key="SAP.XETR", interval="1d")
        self.assertEqual((r["source"], self.calls), ("SYNTHETIC", []))

    def test_sources_endpoint_lists_availability(self):
        self.alpaca_keys()
        got = service.sources(self.ctx, {"key": "AAPL.XNYS"}, None, now_ns=self.NOW)
        self.assertEqual([(s["source"], s["configured"], s["covers"]) for s in got["sources"]],
                         [("ALPACA", True, True), ("SYNTHETIC", False, True)])


class BoundaryTests(unittest.TestCase):
    def test_route_is_registered(self):
        from unknown.api import routes
        self.assertIn(("charts",), [p for p, _ in routes.load()])

    def test_charts_never_import_a_broker_package(self):
        code = ("import sys; import unknown.api.routes.charts; "
                "print(sorted(m for m in sys.modules if m.startswith(('unknown.broker', 'ibapi'))))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": str(SRC)},
                             check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if LiveCase is not None:
    class ChartsServerTests(LiveCase):
        def test_bars_require_the_owner_session(self):
            status, got, _ = self.call("GET", "/api/charts/bars?key=SAP.XETR&interval=1d")
            self.assertEqual((status, got["code"]), (401, "LOGIN_REQUIRED"))
            cookie, _ = self.login()
            status, got, _ = self.call("GET", "/api/charts/bars?key=SAP.XETR&interval=1w", cookie=cookie)
            self.assertEqual((status, got["data_type"], got["interval"]), (200, "SYNTHETIC", "1w"))


if __name__ == "__main__":
    unittest.main()
