"""Read-only portfolio routes (api/routes/portfolio.py): every endpoint returns computed data with lineage or an
explicit unavailable reason, is JSON-safe, and never writes the core. Values are TEST FIXTURES."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import DAY, booted, buy, db_hash, eod, quotes, ro  # noqa: E402
from support import ACCOUNT  # noqa: E402

from unknown.analytics.snapshots import SnapshotStore, default_path  # noqa: E402
from unknown.api.routes import portfolio  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402


class FakeCore:
    def __init__(self, quote_map: dict | None, fail: bool = False):
        self.quote_map, self.fail = quote_map, fail

    def query(self, name: str, **_):
        if self.fail:
            raise OSError("socket not found (test)")
        assert name == "quotes"
        return self.quote_map


class Ctx:
    def __init__(self, root: Path, core: FakeCore):
        self.paths = Paths(root)
        self.core = core


def call(ctx, conn, path: str, **q) -> dict:
    parts = [p for p in path.split("/") if p]
    body = portfolio.handle(ctx, parts, {k: str(v) for k, v in q.items()}, ACCOUNT, conn)
    json.dumps(body, allow_nan=False)  # must be strict JSON: no NaN or infinity
    return body


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.h = h = booted(self)
        buy(h, "S1", "B1", "5", "9.00", bid="9.00", ask="9.02")
        self.conn = ro(self, h)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ctx = Ctx(self.root, FakeCore(quotes(h)))
        # Routes value at wall-clock time; tests pin it to the fixture clock so point-in-time filters see the fixture history.
        patcher = mock.patch.object(portfolio, "time", types.SimpleNamespace(time_ns=lambda: self.h.clock.now_ns()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _history(self, days: int = 25):
        store = SnapshotStore(default_path(self.root))
        try:
            price = 9.00
            for i in range(days):
                if i:
                    self.h.clock.advance(DAY)
                    price += 0.05 if i % 3 else -0.08
                eod(self, self.h, store, self.conn, {"FIX-EQ-1": f"{price:.2f}"})
        finally:
            store.close()
        self.ctx.core.quote_map = quotes(self.h)

    def test_registration_shape(self):
        self.assertEqual(portfolio.ROUTES[0][0], ("portfolio",))

    def test_every_endpoint_answers_without_history_with_reasons(self):
        before = db_hash(self.h.db_path)
        h = call(self.ctx, self.conn, "portfolio/holdings", scope="system")
        self.assertIn("lineage", h)
        self.assertEqual(h["quote_snapshot"]["unavailable_reason"], None)
        self.assertEqual(h["positions"][0]["mark"]["status"], "fresh")
        self.assertIn("lineage", call(self.ctx, self.conn, "portfolio/exposure", scope="system"))
        for path in ("portfolio/performance", "portfolio/snapshots", "portfolio/var/backtest", "portfolio/pnl"):
            body = call(self.ctx, self.conn, path)
            self.assertFalse(body["available"], path)
            self.assertIn("snapshots record", body["reason"])
        v = call(self.ctx, self.conn, "portfolio/var", scope="system")
        self.assertIn("snapshots record", v["history"]["unavailable_reason"])
        self.assertIsNone(v["by_currency"]["USD"]["var"])
        w = call(self.ctx, self.conn, "portfolio/whatif", session="S1", instrument="FIX-EQ-1", side="BUY", quantity="1",
                 limit="9.02")
        self.assertIn("HYPOTHETICAL", w["label"])
        self.assertEqual(w["code"], "APPROVED")
        self.assertEqual(w["reservation_preview"][0]["amount"], "10.220000")  # 9.02 + fee envelope 1.20
        fee = next(c for c in w["decision"]["checks"] if c["check"] == "FEE_SCHEDULE")
        self.assertTrue(fee["passed"])                      # the simulated broker: the policy fee model, labelled
        self.assertIn("policy fee model", fee["detail"])
        gtc = call(self.ctx, self.conn, "portfolio/whatif", session="S1", instrument="FIX-EQ-1", side="BUY",
                   quantity="1", limit="9.02", tif="GTC")
        self.assertEqual(gtc["code"], "TIME_IN_FORCE")      # the preview runs the core's own TIF check
        for name in ("overnight_gap", "correlated_selloff", "stale_quote", "fee_revision"):
            body = call(self.ctx, self.conn, f"portfolio/stress/{name}", scope="session:S1")
            self.assertEqual(body["scenario"], name)
            self.assertIn("lineage", body)
        a = call(self.ctx, self.conn, "portfolio/allocation", scope="session:S1")
        self.assertIn("PROPOSAL", a["label"])
        self.assertIn("history_unavailable_reason", a)
        self.assertEqual(len(call(self.ctx, self.conn, "portfolio/formulas")["formulas"]), len(portfolio.FORMULAS))
        self.assertEqual(db_hash(self.h.db_path), before)

    def test_with_history(self):
        self._history()
        perf = call(self.ctx, self.conn, "portfolio/performance", scope="session:S1")
        self.assertIsNotNone(perf["twr"])
        self.assertIsNone(perf["twr_annualized"])
        self.assertTrue(perf["fixture_data"])
        p = call(self.ctx, self.conn, "portfolio/pnl", scope="session:S1")
        self.assertTrue(p["identity_holds"])
        snaps = call(self.ctx, self.conn, "portfolio/snapshots", limit="5")
        self.assertEqual(len(snaps["runs"]), 5)
        var = call(self.ctx, self.conn, "portfolio/var", scope="session:S1", alpha="0.95")
        self.assertIsNotNone(var["by_currency"]["USD"]["var"])
        bt = call(self.ctx, self.conn, "portfolio/var/backtest", scope="session:S1", alpha="0.9", min_scenarios="5")
        self.assertGreater(bt["observations"], 0)
        alloc = call(self.ctx, self.conn, "portfolio/allocation", scope="session:S1", min_obs="10")
        self.assertIn("inverse_volatility", alloc["baselines"])

    def test_core_unreachable_degrades_to_unpriced_with_reason(self):
        ctx = Ctx(self.root, FakeCore(None, fail=True))
        h = call(ctx, self.conn, "portfolio/holdings", scope="system")
        self.assertIn("core unreachable", h["quote_snapshot"]["unavailable_reason"])
        self.assertEqual(h["positions"][0]["mark"]["status"], "unpriced")
        self.assertIsNone(h["totals"]["USD"]["nav_marked"])

    def test_bad_requests(self):
        with self.assertRaises(LookupError):
            call(self.ctx, self.conn, "portfolio/nothing")
        with self.assertRaises(LookupError):
            call(self.ctx, self.conn, "portfolio/stress/meteor")
        with self.assertRaises(ValueError):
            call(self.ctx, self.conn, "portfolio/whatif", session="S1")
        with self.assertRaises(ValueError):
            call(self.ctx, self.conn, "portfolio/holdings", scope="owner:nobody")
        with self.assertRaises(ValueError):
            call(self.ctx, self.conn, "portfolio/var", lookback="zero")


if __name__ == "__main__":
    unittest.main()
