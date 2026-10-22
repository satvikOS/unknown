"""Performance measurement and ex-post statistics (CAP-PORT-04, CAP-PORT-05).

Published IBKR worked examples are reproduced from the capability register's acceptance
criteria; ledger-driven tests build real valuation histories through the core (Harness) and
the snapshotter. Every price path is a TEST FIXTURE."""
from __future__ import annotations

import math
import random
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import DAY, SEC, booted, buy, eod, ro, snapshot_store  # noqa: E402
from support import ACCOUNT  # noqa: E402

from unknown.analytics.performance import (annualize, drawdown, irr, link, modified_dietz, modified_dietz_totals,  # noqa: E402
                                           n_eff, performance_report, relative_statistics, risk_statistics, sharpe_ratio,
                                           subperiod_return, unit_values)
from unknown.analytics.snapshots import ValuationPoint  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

F = Fixed.parse


class PublishedExamplesTest(unittest.TestCase):
    def test_ibkr_modified_dietz_example(self):
        r = modified_dietz_totals(F("4549863.44"), F("4256598.99"), F("-86000.00"), F("-107629.03"))
        self.assertAlmostEqual(r, -0.0467, delta=0.00005)

    def test_ibkr_twr_linking_example(self):
        self.assertAlmostEqual(link([0.0174, -0.0468, 0.0192, -0.0069, 0.0202]), 0.0014, delta=0.00005)

    def test_ibkr_flow_timing_conventions(self):
        sod, _ = subperiod_return(F("4629129.14"), F("4197829.64"), F("-225000.00"), "start_of_day")
        eod_, _ = subperiod_return(F("4629129.14"), F("4197829.64"), F("-225000.00"), "end_of_day")
        self.assertAlmostEqual(sod, -0.0468, delta=0.00005)
        self.assertAlmostEqual(eod_, -0.0446, delta=0.00005)

    def test_ibkr_sharpe_example(self):
        self.assertAlmostEqual(sharpe_ratio(0.017677, 0.162357, 360, 1.37), 1.62, delta=0.01)

    def test_ibkr_information_ratio_example(self):
        diffs = [0.0040, 0.0071, -0.0038]
        r = relative_statistics(diffs, [0.0, 0.0, 0.0], periods_per_year=1)
        self.assertAlmostEqual(r["information_ratio"], 0.433259219, delta=1e-4)

    def test_ibkr_beta_example_consistent_normalisation(self):
        p = [0.0048, -0.0154, 0.0199, 0.0112]
        b = [0.0044, -0.0137, 0.0119, 0.0089]
        r = relative_statistics(p, b, periods_per_year=4)
        self.assertAlmostEqual(r["beta"], 1.291, delta=0.001)
        # IBKR's published 0.968 is population covariance over sample variance: documented, not adopted.
        self.assertAlmostEqual(r["beta"] * (len(p) - 1) / len(p), 0.968, delta=0.001)


class FormulaTest(unittest.TestCase):
    def test_modified_dietz_day_weights(self):
        # BMV 1000, +100 on day 10 of a 30-day period, EMV 1150: (1150 - 1000 - 100) / (1000 + 100 * 20/30)
        self.assertAlmostEqual(modified_dietz(F("1000"), F("1150"), [(10, F("100"))], 30), 50 / (1000 + 2000 / 30), places=15)

    def test_irr_solves_the_cash_flow_equation(self):
        r = irr(F("100"), F("110"), [], 365)
        self.assertAlmostEqual(r["irr_annualized"], 0.10, places=12)
        r2 = irr(F("100"), F("260"), [(100, F("150"))], 400)
        g = 1 + r2["irr_annualized"]
        residual = -100 * g ** (400 / 365) - 150 * g ** (300 / 365) + 260
        self.assertAlmostEqual(residual, 0.0, places=8)

    def test_short_periods_are_never_annualized(self):
        self.assertEqual(annualize(0.05, 364)[0], None)
        self.assertIn("GIPS 2.A.12", annualize(0.05, 364)[1])
        self.assertAlmostEqual(annualize(0.21, 730)[0], 0.1, places=2)
        short = irr(F("100"), F("101"), [], 30)
        self.assertIsNone(short["irr_annualized"])
        self.assertAlmostEqual(short["period_mwr"], 0.01, places=12)

    def test_drawdown_on_unit_values(self):
        d = drawdown([("d0", 1000.0), ("d1", 1100.0), ("d2", 880.0), ("d3", 1000.0), ("d4", 1200.0)])
        self.assertAlmostEqual(d["max_drawdown"], 0.2, places=12)
        self.assertEqual((d["peak_date"], d["trough_date"], d["recovery_date"]), ("d1", "d2", "d4"))
        self.assertEqual(drawdown([("a", 1000.0), ("b", 900.0)])["recovery_date"], "ongoing")

    def test_statistics_conventions_and_insufficient_data(self):
        few = risk_statistics([0.01, -0.02, 0.005], min_obs=20)
        self.assertIsNone(few["sharpe"])
        self.assertIn("ratios not presented", few["insufficient_data"])
        rng = random.Random(7)
        rets = [rng.gauss(0.0005, 0.01) for _ in range(250)]
        s = risk_statistics(rets, periods_per_year=252)
        m = math.fsum(rets) / 250
        sd = math.sqrt(math.fsum((x - m) ** 2 for x in rets) / 249)
        self.assertAlmostEqual(s["volatility_annualized"], sd * math.sqrt(252), places=12)
        self.assertAlmostEqual(s["sharpe"], m * 252 / (sd * math.sqrt(252)), places=12)
        self.assertIn("none configured", s["risk_free"]["series_id"])
        self.assertGreater(s["sharpe_standard_error"]["n_eff"], 0)

    def test_n_eff_shrinks_under_positive_autocorrelation(self):
        rng = random.Random(11)
        x, ar = 0.0, []
        for _ in range(400):
            x = 0.9 * x + rng.gauss(0, 1)
            ar.append(x)
        self.assertLess(n_eff(ar)["n_eff"], 100)
        alternating = [(-1) ** i * 0.01 for i in range(100)]
        self.assertEqual(n_eff(alternating)["n_eff"], 100.0)


def _path(test: unittest.TestCase, prices: list[str], *, deposit_after: int | None = None, deposit: str = "50.00"):
    """System-scope history: buy 5 FIXA on day 0, then one end-of-day snapshot per price; optionally authorize more
    capital right after a snapshot (a flow at a day boundary)."""
    h = booted(test, authorize="200.00")
    conn = ro(test, h)
    store = snapshot_store(test)
    buy(h, "S1", "B1", "5", "9.00", bid="8.99", ask="9.01")
    for i, p in enumerate(prices):
        if i:
            h.clock.advance(DAY)
        eod(test, h, store, conn, {"FIX-EQ-1": p})
        if deposit_after == i:
            h.clock.advance(SEC)
            r = h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount=deposit)
            assert r.accepted, r.body
    test.assertEqual(h.verify(), [])
    pts = store.series(ACCOUNT, "system", "USD", as_of_ns=h.clock.now_ns())
    return h, store, pts


class LedgerDrivenPerformanceTest(unittest.TestCase):
    def test_deposit_at_a_day_boundary_leaves_twr_unchanged_but_moves_mwr(self):
        """CAP-PORT-04: same market path with and without a deposit gives identical TWR and different MWR."""
        prices = ["9.00", "9.50", "9.50", "9.50"]
        _, _, a = _path(self, prices)
        _, _, b = _path(self, prices, deposit_after=1)
        ra = performance_report(a, scope="system", currency_code="USD", as_of_ns=0)
        rb = performance_report(b, scope="system", currency_code="USD", as_of_ns=0)
        self.assertLess(abs(ra["twr"] - rb["twr"]), 1e-12)
        self.assertAlmostEqual(ra["twr"], 2.5 / 199.0, places=12)  # 5 x 0.50 gain on 199.00 (100 unallocated + 99 session)
        self.assertEqual([f["amount"] for f in rb["external_flows"]], ["50.000000"])
        self.assertNotAlmostEqual(ra["money_weighted"]["period_mwr"], rb["money_weighted"]["period_mwr"], places=6)
        self.assertIsNone(rb["twr_annualized"])                   # 3 days: never annualized
        self.assertIsNone(rb["money_weighted"]["irr_annualized"])
        for convention in ("start_of_day", "end_of_day"):
            r = performance_report(b, scope="system", currency_code="USD", as_of_ns=0, convention=convention)
            self.assertLess(abs(r["twr"] - ra["twr"]), 1e-12, convention)

    def test_deposit_at_the_trough_does_not_hide_the_drawdown(self):
        prices = ["9.00", "10.00", "8.00", "9.00"]
        _, _, a = _path(self, prices)
        _, _, b = _path(self, prices, deposit_after=2, deposit="100.00")
        da = performance_report(a, scope="system", currency_code="USD", as_of_ns=0)["drawdown"]
        db = performance_report(b, scope="system", currency_code="USD", as_of_ns=0)["drawdown"]
        self.assertLess(abs(da["max_drawdown"] - db["max_drawdown"]), 1e-12)
        self.assertAlmostEqual(da["max_drawdown"], 10.0 / 204.0, places=12)  # 5 x (10 - 8) from a 204.00 peak
        navs = [float(Fraction(p.nav.mantissa, 10 ** p.nav.scale)) for p in b]
        self.assertGreater(navs[-1], max(navs[:2]))  # the raw NAV series 'recovers' only because of the deposit

    def test_incomplete_days_are_skipped_and_flagged(self):
        h, store, _ = _path(self, ["9.00", "9.10"])
        conn = ro(self, h)
        h.clock.advance(DAY)
        from unknown.analytics.snapshots import record_eod
        record_eod(conn, store, ACCOUNT, {}, now_ns=h.clock.now_ns())  # no quotes: unpriced day
        h.clock.advance(DAY)
        eod(self, h, store, conn, {"FIX-EQ-1": "9.20"})
        pts = store.series(ACCOUNT, "system", "USD", as_of_ns=h.clock.now_ns())
        r = performance_report(pts, scope="system", currency_code="USD", as_of_ns=h.clock.now_ns())
        gap = [s for s in r["subperiods"] if s["gap_skipped_incomplete_dates"]]
        self.assertEqual(len(gap), 1)
        self.assertEqual(r["lineage"]["missing"][0]["reason"], "valuation unpriced_positions")
        self.assertAlmostEqual(r["twr"], 1.0 / 199.0, places=12)  # 5 x (9.20 - 9.00) over the whole period


class ClosedSessionTest(unittest.TestCase):
    def test_closure_outflow_is_terminal_not_a_broken_chain_and_empty_buckets_stop_recording(self):
        h = booted(self)
        conn = ro(self, h)
        store = snapshot_store(self)
        eod(self, h, store, conn, {})
        h.clock.advance(DAY)
        eod(self, h, store, conn, {})
        h.clock.advance(DAY)
        r = h.owner_cmd("end_session", account_key=ACCOUNT, session_id="S1")
        self.assertEqual(r.body["state"], "CLOSED")   # flat and never traded: closes at once, capital back to unallocated
        self.assertEqual(h.verify(), [])
        eod(self, h, store, conn, {})
        h.clock.advance(DAY)
        eod(self, h, store, conn, {})
        pts = store.series(ACCOUNT, "owner:session:S1", "USD", as_of_ns=h.clock.now_ns())
        self.assertEqual([p.nav.to_str() for p in pts], ["100.000000", "100.000000", "0.000000"])  # not re-recorded after
        rep = performance_report(pts, scope="owner:session:S1", currency_code="USD", as_of_ns=h.clock.now_ns())
        self.assertEqual(rep["twr"], 0.0)
        self.assertIn("not positive", rep["subperiods"][-1]["reason"])
        system = store.series(ACCOUNT, "system", "USD", as_of_ns=h.clock.now_ns())
        self.assertEqual(len(system), 4)  # closure is internal to the system scope


class AnnualizationTest(unittest.TestCase):
    def test_periods_of_a_year_or_more_are_annualized(self):
        pts = [ValuationPoint("r0", "2025-01-01", 0, 0, "USD", F("100"), F("0"), F("0"), "complete", False),
               ValuationPoint("r1", "2026-01-02", 1, 1, "USD", F("110"), F("0"), F("0"), "complete", False)]
        r = performance_report(pts, scope="system", currency_code="USD", as_of_ns=1)
        self.assertAlmostEqual(r["twr"], 0.10, places=12)
        self.assertAlmostEqual(r["twr_annualized"], 1.1 ** (365 / 366) - 1, places=12)
        self.assertAlmostEqual(r["money_weighted"]["irr_annualized"], 1.1 ** (365 / 366) - 1, places=9)
        self.assertEqual(unit_values([])[:1], [])


if __name__ == "__main__":
    unittest.main()
