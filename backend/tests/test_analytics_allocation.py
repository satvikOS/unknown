"""Long-only baselines, shrinkage covariance, eq. 6.2 mean-variance and trade-list proposals (CAP-PORT-13).

Random fixtures are seeded; optimality is certified by the KKT residual of the convex problem and
cross-checked against random feasible points. Trade lists are proposals: the core is never written."""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import DAY, booted, buy, db_hash, eod, quotes, register_second, ro, snapshot_store  # noqa: E402
from support import ACCOUNT  # noqa: E402

from unknown.analytics.allocation import (PROPOSAL, _objective, equal_weight, inverse_volatility, ledoit_wolf,  # noqa: E402
                                          mean_variance, project_capped_simplex, propose, trade_list)
from unknown.analytics.common import parse_scope  # noqa: E402
from unknown.analytics.linalg import symmetric_eigenvalues  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.policy.model import FeeModel  # noqa: E402
from unknown.domain.money import Money  # noqa: E402

F = Fixed.parse
FEES = FeeModel(per_share=F("0.005"), minimum=Money("USD", F("1.00")), maximum_bps_of_notional=100, estimate_margin_bps=2000,
                source="fixture", verified_on="fixture")


def random_problem(rng: random.Random, p: int):
    a = [[rng.gauss(0, 0.1) for _ in range(p)] for _ in range(p)]
    sigma = [[sum(a[i][k] * a[j][k] for k in range(p)) + (0.001 if i == j else 0.0) for j in range(p)] for i in range(p)]
    mu = [rng.gauss(0.01, 0.02) for _ in range(p)]
    w_old = [rng.random() for _ in range(p)]
    total = sum(w_old)
    w_old = [x / total * rng.uniform(0.5, 1.0) for x in w_old]
    caps = [rng.uniform(1.2 / p, 0.8) for _ in range(p)]
    return sigma, mu, w_old, caps


def random_feasible(rng, caps, budget):
    return project_capped_simplex([rng.random() for _ in caps], caps, budget)


class LinalgTest(unittest.TestCase):
    def test_jacobi_eigenvalues(self):
        self.assertEqual([round(x, 12) for x in symmetric_eigenvalues([[2.0, 1.0], [1.0, 2.0]])], [1.0, 3.0])
        e = symmetric_eigenvalues([[4.0, 1.0, 0.5], [1.0, 3.0, 0.2], [0.5, 0.2, 1.0]])
        self.assertAlmostEqual(sum(e), 8.0, places=12)  # trace


class MeanVarianceTest(unittest.TestCase):
    def test_two_asset_minimum_variance_matches_the_analytic_solution(self):
        s11, s22, s12 = 0.04, 0.09, 0.006
        sigma = [[s11, s12], [s12, s22]]
        w1 = (s22 - s12) / (s11 + s22 - 2 * s12)
        r = mean_variance(sigma, names=["A", "B"])
        self.assertEqual(r["status"], "optimal")
        self.assertAlmostEqual(r["weights"]["A"], w1, delta=1e-9)
        self.assertAlmostEqual(r["weights"]["B"], 1 - w1, delta=1e-9)
        reserved = mean_variance(sigma, names=["A", "B"], cash_reserve="0.10")
        self.assertAlmostEqual(reserved["weights"]["A"], 0.9 * w1, delta=1e-9)
        self.assertAlmostEqual(reserved["cash_weight"], 0.10, places=12)

    def test_random_fixtures_satisfy_constraints_and_kkt(self):
        rng = random.Random(6)
        for case in range(150):
            p = rng.randint(2, 6)
            sigma, mu, w_old, caps = random_problem(rng, p)
            lam, eta = rng.choice([0.0, 0.5, 2.0]), rng.choice([0.0, 0.001, 0.01])
            r = mean_variance(sigma, mu, lam=lam, eta=eta, w_old=w_old, cash_reserve="0.05", caps=caps)
            if r["status"] == "infeasible":
                self.assertGreater(0.95, sum(caps) - 1e-12)
                continue
            w = list(r["weights"].values())
            self.assertIn(r["status"], ("optimal", "optimal_within_precision"), case)
            self.assertAlmostEqual(sum(w), 0.95, delta=1e-9)
            for x, u in zip(w, caps):
                self.assertGreaterEqual(x, -1e-12)
                self.assertLessEqual(x, u + 1e-12)
            self.assertLess(r["kkt_residual"], 1e-7, case)
            best = _objective(sigma, mu, lam, eta, w, w_old)
            for _ in range(20):
                other = random_feasible(rng, caps, 0.95)
                self.assertLessEqual(best, _objective(sigma, mu, lam, eta, other, w_old) + 1e-12)

    def test_turnover_limit_is_enforced_by_raising_eta(self):
        rng = random.Random(9)
        sigma, mu, _, caps = random_problem(rng, 4)
        w_old = project_capped_simplex([0.1, 0.5, 0.2, 0.2], [1.0] * 4, 0.95)
        free = mean_variance(sigma, mu, lam=1.0, w_old=w_old, cash_reserve="0.05")
        limited = mean_variance(sigma, mu, lam=1.0, w_old=w_old, cash_reserve="0.05", tau=free["turnover_l1"] / 4)
        self.assertLessEqual(limited["turnover_l1"], free["turnover_l1"] / 4 + 1e-9)
        self.assertGreater(limited["effective_eta"], 0.0)
        self.assertIn("Lagrange", limited["turnover_note"])

    def test_infeasible_constraints_are_named(self):
        r = mean_variance([[0.04, 0.0], [0.0, 0.09]], caps=[0.3, 0.3], cash_reserve="0.10")
        self.assertEqual(r["status"], "infeasible")
        self.assertIn("position caps", r["conflicting_constraints"][0])
        t = mean_variance([[0.04, 0.0], [0.0, 0.09]], w_old=[0.0, 0.0], tau=0.5)
        self.assertEqual(t["status"], "infeasible")
        self.assertIn("turnover limit", t["conflicting_constraints"][0])


class BaselinesTest(unittest.TestCase):
    def test_equal_weight_after_cash_reserve(self):
        r = equal_weight(["A", "B", "C", "D"], cash_reserve="0.20")
        self.assertEqual(r["weights"], {k: 0.2 for k in "ABCD"})

    def test_inverse_volatility_floors_and_caps(self):
        vols = {"A": 0.1, "B": 0.2, "C": 0.4}
        plain = inverse_volatility(vols)
        self.assertAlmostEqual(plain["weights"]["A"], 4 / 7, places=12)
        capped = inverse_volatility(vols, cap="0.5", floor="0.2")
        self.assertAlmostEqual(capped["weights"]["A"], 0.5, places=12)
        self.assertAlmostEqual(capped["weights"]["C"], 0.2, places=12)
        self.assertAlmostEqual(capped["weights"]["B"], 0.3, places=12)
        self.assertEqual(capped["binding"], ["A", "C"])
        bad = inverse_volatility(vols, floor="0.4")
        self.assertEqual(bad["status"], "infeasible")
        self.assertIn("floor", bad["conflicting_constraints"][0])


class ShrinkageTest(unittest.TestCase):
    def test_ledoit_wolf_repairs_a_singular_sample_covariance(self):
        rng = random.Random(3)
        rows = [[rng.gauss(0, 0.01) for _ in range(8)] for _ in range(5)]  # p > n: sample covariance is singular
        r = ledoit_wolf(rows)
        self.assertTrue(r["symmetric"] and r["psd"])
        self.assertTrue(0 < r["intensity"] <= 1)
        self.assertLess(r["min_eigenvalue_before"], 1e-12)
        self.assertGreater(r["min_eigenvalue_after"], 0)
        self.assertIsNotNone(r["condition_number_after"])


class TradeListTest(unittest.TestCase):
    def test_lot_rounding_down_fees_and_post_trade_checks(self):
        r = trade_list({"A": 0.5, "B": 0.3}, nav=F("100.00"), liquid=F("100.00"), available_cash=F("100.00"), ccy="USD",
                       prices={"A": F("9.00"), "B": F("7.00")}, current_qty={}, lot_sizes={"A": F("1"), "B": F("1")},
                       multipliers={"A": F("1"), "B": F("1")}, fee_model_policy=FEES, cash_reserve="0.10",
                       caps={"A": 0.5, "B": 0.5})
        trades = {t["instrument_id"]: t for t in r["trades"]}
        self.assertEqual((trades["A"]["quantity"], trades["B"]["quantity"]), ("5", "4"))  # floor(50/9), floor(30/7)
        self.assertEqual(trades["A"]["estimated_fee"], "0.450000")  # capped at 1% of 45.00
        self.assertTrue(r["constraints_hold"], r["checks"])
        self.assertEqual(r["label"], PROPOSAL)
        tiny = trade_list({"A": 0.05}, nav=F("100.00"), liquid=F("100.00"), available_cash=F("100.00"), ccy="USD",
                          prices={"A": F("9.00")}, current_qty={}, lot_sizes={"A": F("1")}, multipliers={"A": F("1")},
                          fee_model_policy=FEES)
        self.assertEqual(tiny["trades"], [])  # rounds to zero quantity: no order (eq. 6.4 rule)

    def test_unfunded_buys_are_flagged_not_hidden(self):
        r = trade_list({"A": 0.9}, nav=F("100.00"), liquid=F("100.00"), available_cash=F("40.00"), ccy="USD",
                       prices={"A": F("9.00")}, current_qty={}, lot_sizes={"A": F("1")}, multipliers={"A": F("1")},
                       fee_model_policy=FEES)
        self.assertFalse(r["constraints_hold"])
        self.assertFalse(r["trade_list_usable"])
        self.assertIn("settlement", r["sequencing_note"])


class ProposalTest(unittest.TestCase):
    def test_end_to_end_proposal_from_ledger_and_snapshots_never_writes(self):
        h = booted(self)
        register_second(h)
        buy(h, "S1", "B1", "3", "10.00", bid="10.00", ask="10.02")
        buy(h, "S1", "B2", "3", "8.00", instrument_id="FIX-EQ-2", bid="8.00", ask="8.02")
        conn = ro(self, h)
        store = snapshot_store(self)
        rng = random.Random(1)
        pa, pb = 10.0, 8.0
        for i in range(30):
            if i:
                h.clock.advance(DAY)
                pa *= 1 + rng.gauss(0, 0.01)
                pb *= 1 + rng.gauss(0, 0.03)
            eod(self, h, store, conn, {"FIX-EQ-1": f"{pa:.2f}", "FIX-EQ-2": f"{pb:.2f}"})
        before = db_hash(h.db_path)
        r = propose(conn, ACCOUNT, quotes(h), store, now_ns=h.clock.now_ns(), scope=parse_scope("session:S1"),
                    cash_reserve="0.05", cap="0.6", eta=0.0001)
        self.assertEqual(db_hash(h.db_path), before)
        self.assertEqual(r["label"], PROPOSAL)
        self.assertEqual(set(r["baselines"]), {"equal_weight", "inverse_volatility"})
        ivol = r["baselines"]["inverse_volatility"]["weights"]
        self.assertGreater(ivol["FIX-EQ-1"], ivol["FIX-EQ-2"])  # the calmer fixture gets more weight
        mv = r["mean_variance"]
        self.assertEqual(mv["status"], "optimal")
        self.assertTrue(mv["covariance"]["psd"])
        self.assertIn("none supplied", mv["expected_returns"]["source"])
        self.assertAlmostEqual(sum(mv["weights"].values()), 0.95, delta=1e-9)
        self.assertEqual(set(r["trade_lists"]), {"equal_weight", "inverse_volatility", "mean_variance"})
        for tl in r["trade_lists"].values():
            self.assertEqual(tl["label"], PROPOSAL)
        self.assertTrue(r["fixture_data"])
        with self.assertRaises(ValueError):
            propose(conn, ACCOUNT, quotes(h), store, now_ns=h.clock.now_ns(), scope=parse_scope("session:S1"),
                    mu={"FIX-EQ-1": 0.01})  # expected returns without a named research artifact


if __name__ == "__main__":
    unittest.main()
