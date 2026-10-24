"""Portfolio market risk from stored daily bars (analytics/market_risk.py, api/routes/risk_analytics.py).

The bars are TEST ROWS written into a temporary research store with the schema the IBKR history adapter writes; they
are not market data. The positions come from the fixture ledger harness."""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import DAY, booted, buy, db_hash, quotes, register_second, ro  # noqa: E402
from support import ACCOUNT  # noqa: E402

from unknown.analytics import market_risk  # noqa: E402
from unknown.analytics.common import SYSTEM  # noqa: E402
from unknown.analytics.holdings import holdings  # noqa: E402
from unknown.api.routes import risk_analytics  # noqa: E402
from unknown.broker.ibkr.history import BAR_SCHEMA  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402

COLS = ("provider, dataset_id, instrument_id, bar_spec_version, bar_size, what_to_show, use_rth, timezone, adjustment, "
        "adjustment_note, bar_start_ns, bar_end_ns, bar_date, open, high, low, close, volume, wap, bar_count, currency, "
        "receipt_time_ns, available_time_ns, availability_rule, known_from_ns, known_to_ns, request_id, raw, quality, "
        "fixture")
BENCH_R = [0.01, -0.02, 0.015, 0.003, -0.007, 0.012, -0.004, 0.006, -0.011, 0.009]


def closes_from(returns: list[float], start: str = "100") -> list[Decimal]:
    getcontext().prec = 40
    out = [Decimal(start)]
    for r in returns:
        out.append(out[-1] * (1 + Decimal(repr(r))))
    return out


def write_bars(db: Path, instrument: str, closes: list[Decimal], end_ns: int, *, fixture: int = 0,
               available_after: int = 0) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(BAR_SCHEMA)
    n = len(closes)
    for k, c in enumerate(closes):
        start = end_ns - (n - k + 1) * DAY
        day = datetime.fromtimestamp(start / 1e9, tz=timezone.utc).strftime("%Y%m%d")
        conn.execute(f"INSERT INTO mkt_bar ({COLS}) VALUES ({','.join('?' * 30)})",
                     ("IBKR", "test-ds", instrument, "v1", "1 day", "TRADES", 1, "America/New_York", "unadjusted",
                      "none", start, start + DAY // 4, day, str(c), str(c), str(c), str(c), "1000", str(c), 1, "USD",
                      start, start + DAY // 4 + available_after, "rule", start, 2 ** 62, "r1", "{}", "{}", fixture))
    conn.commit()
    conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.h = h = booted(self)
        register_second(h)
        buy(h, "S1", "B1", "4", "10.00", bid="10.00", ask="10.02")
        buy(h, "S1", "B2", "3", "8.00", instrument_id="FIX-EQ-2", bid="8.00", ask="8.02")
        self.assertEqual(h.verify(), [])
        self.conn = ro(self, h)
        self.now = h.clock.now_ns()
        self.rep = holdings(self.conn, ACCOUNT, quotes(h), now_ns=self.now, scope=SYSTEM)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "research" / "research.sqlite"
        self.bench_r = BENCH_R * 7                       # 70 returns
        write_bars(self.db, "SPY.ARCX", closes_from(self.bench_r), self.now)
        write_bars(self.db, "FIX-EQ-1", closes_from([2 * r for r in self.bench_r], "50"), self.now)
        write_bars(self.db, "FIX-EQ-2", closes_from(BENCH_R[:5], "20"), self.now)     # too short for a beta

    def report(self, **kw):
        body = market_risk.market_risk_report(self.rep, self.db, now_ns=self.now, min_obs=30, **kw)
        json.dumps(body, allow_nan=False)
        return body


class MarketRiskTest(Base):
    def test_beta_correlation_and_volatility_from_stored_bars(self):
        r = self.report()
        a = {x["instrument_id"]: x for x in r["instruments"]}["FIX-EQ-1"]
        self.assertAlmostEqual(a["beta"]["value"], 2.0, places=9)
        self.assertAlmostEqual(a["correlation"]["value"], 1.0, places=9)
        expected = statistics.stdev([2 * x for x in self.bench_r]) * math.sqrt(252)
        self.assertAlmostEqual(a["hist_vol"]["value"], expected, places=9)
        self.assertEqual((a["beta"]["formula"], a["hist_vol"]["formula"], a["beta"]["n"]), ("mrisk.beta@1", "mrisk.hist_vol@1", 70))
        self.assertEqual(a["bars"]["provider"], "IBKR")
        self.assertIn("unadjusted", a["bars"]["note"])
        self.assertEqual(set(a["beta"]["inputs_as_of"]), {"FIX-EQ-1", "SPY.ARCX"})

    def test_short_history_is_unavailable_and_never_assumed(self):
        r = self.report()
        b = {x["instrument_id"]: x for x in r["instruments"]}["FIX-EQ-2"]
        self.assertIsNone(b["beta"]["value"])
        self.assertEqual(b["beta"]["reason"], "NO_DATA")
        self.assertIn("at least 30", b["beta"]["detail"])
        usd = r["portfolio"]["USD"]
        self.assertIsNone(usd["portfolio_beta"]["value"])
        self.assertEqual(usd["portfolio_beta"]["coverage"], "0.62500000")       # 40 of 64 has a beta
        self.assertIsNone(usd["portfolio_vol"]["value"])

    def test_scenarios_are_exact_and_leave_out_positions_without_beta(self):
        r = self.report(shocks=["-0.10"])
        sc = r["scenarios"][0]
        self.assertEqual(sc["uniform"]["pnl_by_currency"], {"USD": "-6.400000"})    # -(40 + 24) * 0.10
        self.assertEqual(sc["beta"]["pnl_by_currency"], {"USD": "-8.000000"})       # 40 * 2.0000 * -0.10
        self.assertEqual([x["instrument_id"] for x in sc["beta"]["not_revalued"]], ["FIX-EQ-2"])
        pos = {p["instrument_id"]: p for p in sc["positions"]}
        self.assertEqual(pos["FIX-EQ-1"]["beta_used"], "2.0000")
        self.assertIsNone(pos["FIX-EQ-2"]["beta_pnl"])

    def test_portfolio_figures_when_every_position_has_history(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM mkt_bar WHERE instrument_id='FIX-EQ-2'")
        conn.commit()
        conn.close()
        write_bars(self.db, "FIX-EQ-2", closes_from(self.bench_r, "20"), self.now)
        r = self.report()
        usd = r["portfolio"]["USD"]
        self.assertAlmostEqual(usd["portfolio_beta"]["value"], 40 / 64 * 2 + 24 / 64 * 1, places=9)
        rp = [40 / 64 * 2 * x + 24 / 64 * x for x in self.bench_r]
        self.assertAlmostEqual(usd["portfolio_vol"]["value"], statistics.stdev(rp) * math.sqrt(252), places=9)
        pair = r["correlation_matrix"]["pairs"][0]
        self.assertAlmostEqual(pair["value"], 1.0, places=9)

    def test_concentration_by_issuer_and_owner_bucket(self):
        c = self.report()["concentration"]["USD"]
        self.assertEqual([g["issuer"] for g in c["by_issuer"]], ["unknown issuer"])
        self.assertEqual([(g["owner_bucket"], g["gross"]) for g in c["by_owner_bucket"]], [("session", "64.000000")])

    def test_lookahead_fixture_and_missing_store(self):
        write_bars(self.db, "FIX-EQ-9", closes_from(self.bench_r), self.now, available_after=10 * DAY)
        s = market_risk.load_closes(self.db, "FIX-EQ-9", ["FIX-EQ-9"], now_ns=self.now, lookback=252)
        self.assertEqual(len(s.closes), 62)            # bars not yet available at the valuation instant are unseen
        write_bars(self.db, "FIX-EQ-8", closes_from(self.bench_r), self.now, fixture=1)
        s = market_risk.load_closes(self.db, "FIX-EQ-8", ["FIX-EQ-8"], now_ns=self.now, lookback=252)
        self.assertEqual((s.closes, s.reason), ([], "NO_DATA"))
        body = market_risk.market_risk_report(self.rep, self.root / "missing.sqlite", now_ns=self.now)
        json.dumps(body, allow_nan=False)
        for row in body["instruments"]:
            for k in ("hist_vol", "beta", "correlation"):
                self.assertEqual((row[k]["value"], row[k]["reason"]), (None, "NO_SOURCE"))
        self.assertEqual(body["scenarios"][0]["beta"]["pnl_by_currency"], {})


class RouteTest(Base):
    def test_route_is_read_only_and_validates(self):
        class Core:
            def query(_self, name):
                return quotes(self.h)
        ctx = types.SimpleNamespace(paths=Paths(self.root), core=Core())
        before = db_hash(self.h.db_path)
        with mock.patch.object(risk_analytics, "time", types.SimpleNamespace(time_ns=lambda: self.now)):
            body = risk_analytics.handle(ctx, ["risk", "analytics"], {"scope": "system", "min_obs": "30"}, ACCOUNT, self.conn)
        json.dumps(body, allow_nan=False)
        self.assertEqual(db_hash(self.h.db_path), before)
        self.assertEqual(body["exposure"]["kind"], "exposure")
        self.assertAlmostEqual(body["market_risk"]["instruments"][0]["beta"]["value"], 2.0, places=9)
        for bad in ({"benchmark": "spy;drop"}, {"lookback": "5"}, {"shocks": "-1"}, {"shocks": "x"}):
            with self.assertRaises(ValueError):
                risk_analytics.handle(ctx, ["risk", "analytics"], bad, ACCOUNT, self.conn)
        with self.assertRaises(LookupError):
            risk_analytics.handle(ctx, ["risk", "analytics", "x"], {}, ACCOUNT, self.conn)


if __name__ == "__main__":
    unittest.main()
