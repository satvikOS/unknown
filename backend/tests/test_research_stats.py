"""Known-answer tests for research statistics and fold generators (CAP-RES-07, -08, -09; book eq. 4.2).

Independent arithmetic: expected values are computed here from first
principles (``math.erf`` for the normal CDF, bisection for its inverse,
numerical integration for the expected maximum of normals), not by calling
the functions under test. Monte Carlo studies that need thousands of
replications run only with ``UNKNOWN_SLOW_TESTS=1``.
"""
from __future__ import annotations

import math
import os
import random
import unittest

from unknown.research import stats
from unknown.research.folds import cpcv, cpcv_path_count, cpcv_split_count, purged_kfold, walk_forward

SLOW = os.environ.get("UNKNOWN_SLOW_TESTS") == "1"


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def phi_inv(p: float) -> float:
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def expected_max_normal(n: int) -> float:
    """E[max of n iid N(0,1)] = integral x n phi(x) Phi(x)^(n-1) dx (Simpson on [-10, 10])."""
    a, b, m = -10.0, 10.0, 4000
    h = (b - a) / m
    total = 0.0
    for i in range(m + 1):
        x = a + i * h
        f = x * n * math.exp(-x * x / 2) / math.sqrt(2 * math.pi) * phi(x) ** (n - 1)
        total += f * (1 if i in (0, m) else (4 if i % 2 else 2))
    return total * h / 3


class MomentsTest(unittest.TestCase):
    def test_sharpe_per_period(self):
        r = [0.01, -0.02, 0.03, 0.0]
        m = sum(r) / 4
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / 3)
        self.assertAlmostEqual(stats.sharpe(r), m / sd, places=15)
        self.assertIsNone(stats.sharpe([0.01, 0.01, 0.01]))
        self.assertAlmostEqual(stats.annualized(0.1)["value"], 0.1 * math.sqrt(252))

    def test_max_drawdown_and_expected_shortfall(self):
        self.assertAlmostEqual(stats.max_drawdown([100, 120, 90, 130, 104]), 0.25)
        r = [-0.05, -0.01, 0.02, 0.03, -0.04, 0.0, 0.01, 0.02, 0.01, -0.02]
        self.assertAlmostEqual(stats.expected_shortfall(r, 0.2), 0.045)   # mean of the two worst: -0.05, -0.04

    def test_skew_kurt_non_excess(self):
        g3, g4 = stats.skew_kurt([-1.0, 1.0] * 50)
        self.assertAlmostEqual(g3, 0.0)
        self.assertAlmostEqual(g4, 1.0)   # two-point distribution: kurtosis 1 (non-excess)


class NeffTest(unittest.TestCase):
    def test_eq_4_2_known_answers(self):
        self.assertEqual(stats.n_eff_from_rhos(100, [0.5]), 50.0)
        self.assertEqual(stats.n_eff_from_rhos(100, [0.25, 0.25]), 50.0)
        self.assertAlmostEqual(stats.n_eff_from_rhos(100, [-0.3]), 250.0)
        self.assertIsNone(stats.n_eff_from_rhos(100, [-0.5]))

    def test_estimator_and_gate_form_on_an_alternating_series(self):
        x = [1.0, -1.0] * 50
        self.assertAlmostEqual(stats.autocorrelation(x, 1), -0.99)   # -(n-1)/n
        r = stats.n_eff(x, truncation=1)
        self.assertIsNone(r["raw"])            # 1 + 2(-0.99) < 0: eq. 4.2 undefined
        self.assertEqual(r["gate"], 100.0)     # BR-DAT-13: min(n, n / max(1, .)) caps at n

    def test_gate_form_includes_the_non_overlap_term(self):
        """BR-DAT-13: gate n_eff = min(n, n / label_horizon, n / max(1, 1 + 2 sum rho))."""
        x = [1.0, -1.0] * 50                       # sum rho < 0: the dependence term caps at n
        self.assertEqual(stats.n_eff(x, truncation=1, label_horizon=5)["gate"], 20.0)
        rng = random.Random(2)
        iid = [rng.gauss(0, 1) for _ in range(400)]
        out = stats.n_eff(iid, truncation=1, label_horizon=1)
        self.assertEqual(out["gate"], min(400.0, 400 / max(1.0, 1 + 2 * out["rhos"][0])))
        with self.assertRaises(ValueError):
            stats.n_eff(iid, label_horizon=0)

    def test_positive_dependence_reduces_evidence(self):
        rng = random.Random(1)
        e = [rng.gauss(0, 1) for _ in range(4000)]
        x = [e[0]]
        for v in e[1:]:
            x.append(0.6 * x[-1] + v)
        r = stats.n_eff(x, truncation=20)
        # AR(1) with phi=0.6: n (1 - phi) / (1 + phi) = 1000
        self.assertGreater(r["gate"], 850)
        self.assertLess(r["gate"], 1200)


class DeflatedSharpeTest(unittest.TestCase):
    def test_psr_known_answer(self):
        # SR=0.2, SR*=0, T=101, normal moments: z = 0.2 * 10 / sqrt(1 + 0.5 * 0.04)
        expected = phi(2.0 / math.sqrt(1.02))
        self.assertAlmostEqual(stats.psr(0.2, 0.0, 101, 0.0, 3.0), expected, places=12)
        self.assertAlmostEqual(stats.psr(0.1, 0.1, 500, -1.0, 6.0), 0.5, places=15)
        # skew and kurtosis enter the denominator exactly as eq. 2
        z = (0.15 - 0.05) * math.sqrt(251) / math.sqrt(1 - (-0.5) * 0.15 + (5.0 - 1) / 4 * 0.15 ** 2)
        self.assertAlmostEqual(stats.psr(0.15, 0.05, 252, -0.5, 5.0), phi(z), places=12)

    def test_expected_max_sharpe_known_answers(self):
        g = 0.5772156649015329
        for n, v in ((2, 1.0), (10, 0.04), (100, 0.25)):
            expected = math.sqrt(v) * ((1 - g) * phi_inv(1 - 1 / n) + g * phi_inv(1 - 1 / (n * math.e)))
            self.assertAlmostEqual(stats.expected_max_sharpe(v, n), expected, places=9)
        self.assertEqual(stats.expected_max_sharpe(0.3, 1), 0.0)
        # The approximation tracks the exact expected maximum of N standard normals within ~2% for large N.
        self.assertAlmostEqual(stats.expected_max_sharpe(1.0, 100) / expected_max_normal(100), 1.0, delta=0.02)

    def test_implied_trials(self):
        self.assertEqual(stats.implied_trials(0.0, 10), 10.0)
        self.assertEqual(stats.implied_trials(1.0, 10), 1.0)
        self.assertEqual(stats.implied_trials(0.5, 10), 5.5)

    def test_deflated_sharpe_combines_the_pieces(self):
        rng = random.Random(3)
        cols = [[rng.gauss(0.0005 * j, 0.01) for _ in range(500)] for j in range(5)]
        out = stats.deflated_sharpe(cols[4], cols)
        sr0 = stats.expected_max_sharpe(stats.variance([stats.sharpe(c) for c in cols]), out["n_hat"])
        g3, g4 = stats.skew_kurt(cols[4])
        self.assertAlmostEqual(out["sr0"], sr0)
        self.assertAlmostEqual(out["dsr"], stats.psr(stats.sharpe(cols[4]), sr0, 500, g3, g4))
        self.assertEqual(out["M"], 5)

    def test_registry_trial_count_is_the_m_in_n_hat(self):
        """Review fix: failed trials have no returns but still count; M must not shrink to the number of columns."""
        rng = random.Random(6)
        cols = [[rng.gauss(0.0004 * j, 0.01) for _ in range(300)] for j in range(4)]
        short = stats.deflated_sharpe(cols[3], cols)
        full = stats.deflated_sharpe(cols[3], cols, trial_count=10)       # 6 further trials failed
        self.assertEqual((short["M"], full["M"], full["M_with_returns"]), (4, 10, 4))
        rho = stats.average_offdiagonal_correlation(cols)
        self.assertAlmostEqual(full["n_hat"], rho + (1 - rho) * 10, places=12)
        var = stats.variance([stats.sharpe(c) for c in cols])
        self.assertAlmostEqual(full["sr0"], stats.expected_max_sharpe(var, rho + (1 - rho) * 10), places=12)
        self.assertLess(full["dsr"], short["dsr"])                         # more trials, more deflation
        with self.assertRaises(ValueError):
            stats.deflated_sharpe(cols[3], cols, trial_count=3)            # registry cannot count fewer than exist

    def test_not_computable_instead_of_a_number(self):
        self.assertIsNone(stats.deflated_sharpe([0.01] * 10, [[0.01] * 10])["dsr"])
        self.assertIn("not computable", stats.deflated_sharpe([0.01] * 10, [[0.01] * 10])["reason"])

    @unittest.skipUnless(SLOW, "Monte Carlo calibration: set UNKNOWN_SLOW_TESTS=1")
    def test_dsr_null_rejection_rate(self):
        """CAP-RES-08: under the null, DSR > 0.95 at most 5% + 3 MC standard errors of 2,000 simulations."""
        rng = random.Random(11)
        sims, hits = 2000, 0
        for _ in range(sims):
            cols = [[rng.gauss(0, 0.01) for _ in range(250)] for _ in range(10)]
            best = max(cols, key=stats.sharpe)
            if (stats.deflated_sharpe(best, cols)["dsr"] or 0) > 0.95:
                hits += 1
        self.assertLessEqual(hits / sims, 0.05 + 3 * math.sqrt(0.05 * 0.95 / sims))


class PboTest(unittest.TestCase):
    def test_in_sample_best_always_out_of_sample_worst_gives_pbo_one(self):
        # S=4 blocks of 2 rows, N=2. Per-block mean difference (strategy 0 minus 1): 1, 2, 4, -7.
        # Every half-split and its complement have opposite non-zero sums, so the IS winner always loses OOS.
        d = [1, 2, 4, -7]
        rows = []
        for v in d:
            rows += [[float(v), 0.0], [float(v), 0.0]]
        out = stats.pbo_cscv(rows, 4, metric="mean")
        self.assertEqual(out["combinations"], 6)
        self.assertEqual(out["pbo"], 1.0)
        self.assertTrue(all(abs(l - math.log(0.5)) < 1e-12 for l in out["logits"]))   # rank 1 of 2: omega = 1/3

    def test_dominant_strategy_gives_pbo_zero(self):
        rng = random.Random(4)
        rows = [[rng.gauss(0, 1) + 10, rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(64)]
        out = stats.pbo_cscv(rows, 8, metric="mean")
        self.assertEqual(out["pbo"], 0.0)
        self.assertTrue(all(abs(l - math.log(3)) < 1e-12 for l in out["logits"]))      # rank 3 of 3: omega = 3/4

    def test_s16_enumerates_12870_combinations(self):
        rows = [[float(i % 3), float((i * 7) % 5)] for i in range(32)]
        self.assertEqual(math.comb(16, 8), 12870)   # the paper prints "12, 780", a typo (CAP-RES-08)
        self.assertEqual(stats.pbo_cscv(rows, 16)["combinations"], 12870)

    def test_pure_noise_is_near_one_half(self):
        rng = random.Random(9)
        vals = []
        for _ in range(12):
            rows = [[rng.gauss(0, 0.01) for _ in range(8)] for _ in range(160)]
            vals.append(stats.pbo_cscv(rows, 8)["pbo"])
        self.assertAlmostEqual(sum(vals) / len(vals), 0.5, delta=0.15)

    def test_planted_edge_has_low_pbo(self):
        rng = random.Random(10)
        rows = [[rng.gauss(0.004 if j == 0 else 0.0, 0.01) for j in range(8)] for _ in range(320)]
        self.assertLess(stats.pbo_cscv(rows, 16)["pbo"], 0.1)

    def test_remainder_rows_are_dropped_and_reported(self):
        rows = [[float(i), float(-i)] for i in range(35)]
        self.assertEqual(stats.pbo_cscv(rows, 4)["rows_dropped"], 3)


class PairedBootstrapTest(unittest.TestCase):
    def test_both_series_use_identical_indices(self):
        rng = random.Random(2)
        c = [rng.gauss(0.001, 0.01) for _ in range(120)]
        i = [rng.gauss(0.0, 0.01) for _ in range(120)]
        out = stats.paired_bootstrap(c, i, replications=50, seed=7, keep_indices=True)
        # Re-derive every replicate from the recorded indices: the difference must use the same idx for both.
        for idx, _ in zip(out["indices"], range(50)):
            self.assertEqual(len(idx), 120)
        diffs = sorted(stats.mean([c[k] for k in idx]) - stats.mean([i[k] for k in idx]) for idx in out["indices"])
        self.assertAlmostEqual(stats.mean(diffs), out["bootstrap_mean"], places=15)

    def test_constant_shift_gives_a_degenerate_interval(self):
        rng = random.Random(3)
        i = [rng.gauss(0, 0.01) for _ in range(100)]
        c = [x + 0.0005 for x in i]
        out = stats.paired_bootstrap(c, i, replications=300, seed=1, hurdle=0.0)
        self.assertAlmostEqual(out["lower"], 0.0005, places=12)
        self.assertAlmostEqual(out["upper"], 0.0005, places=12)
        self.assertTrue(out["passes"])
        same = stats.paired_bootstrap(i, i, replications=100, seed=1)
        self.assertEqual((same["lower"], same["upper"], same["passes"]), (0.0, 0.0, False))

    def test_infinite_blocks_are_rotations_with_the_sample_mean(self):
        x = [float(v) for v in (3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8)]
        zeros = [0.0] * len(x)
        out = stats.paired_bootstrap(x, zeros, replications=200, mean_block=1e12, seed=5, keep_indices=True)
        for idx in out["indices"]:
            self.assertEqual([(a - idx[0]) % len(x) for a in idx], list(range(len(x))))
        self.assertEqual((out["lower"], out["upper"]), (stats.mean(x), stats.mean(x)))

    def test_mean_block_length_is_respected(self):
        rng = random.Random(4)
        idx = stats.stationary_bootstrap_indices(100_000, 8.0, rng)
        breaks = sum(1 for a, b in zip(idx, idx[1:]) if b != (a + 1) % 100_000)
        self.assertAlmostEqual(len(idx) / (breaks + 1), 8.0, delta=0.3)

    def test_unequal_lengths_refused_and_short_samples_not_computable(self):
        with self.assertRaises(ValueError):
            stats.paired_bootstrap([0.1] * 20, [0.1] * 19)
        self.assertFalse(stats.paired_bootstrap([0.1] * 5, [0.1] * 5)["computable"])

    @unittest.skipUnless(SLOW, "Monte Carlo coverage study: set UNKNOWN_SLOW_TESTS=1")
    def test_ar1_coverage_of_the_paired_interval(self):
        """CAP-RES-09: the gate's nominal 95% paired interval covers the true difference 93%-97% over 2,000 AR(1)
        replications (phi = 0.3, n = 300). Measured on 2026-10-22: the bootstrap percentile interval alone covered
        92.75% (B = 999), so the gate uses the more conservative of it and the flat-top HAC interval."""
        rng = random.Random(21)
        reps, hits, boot_hits = 2000, 0, 0
        for k in range(reps):
            e = [rng.gauss(0, 0.01) for _ in range(300)]
            d = [e[0]]
            for v in e[1:]:
                d.append(0.3 * d[-1] + v)
            c = [x + 0.0005 for x in d]
            out = stats.paired_bootstrap(c, [0.0] * 300, replications=999, confidence=0.95, seed=k)
            hits += out["conservative_lower"] <= 0.0005 <= out["conservative_upper"]
            boot_hits += out["lower"] <= 0.0005 <= out["upper"]
        print(f"coverage: gate interval {hits / reps:.4f}, bootstrap percentile alone {boot_hits / reps:.4f}")
        self.assertTrue(0.93 <= hits / reps <= 0.97, hits / reps)


class LongRunVarianceTest(unittest.TestCase):
    def test_short_series_reduces_to_the_variance(self):
        # n = 10: K_n = 5 consumes every available lag, so M = 1 and the flat-top weight at k = M is 0.
        out = stats.flat_top_long_run_variance([float(v) for v in range(1, 11)])
        self.assertEqual(out["bandwidth_M"], 1)
        self.assertAlmostEqual(out["lrv"], 8.25)   # population variance of 1..10

    def test_iid_and_ar1_known_long_run_variances(self):
        rng = random.Random(5)
        iid = [rng.gauss(0, 1) for _ in range(20000)]
        self.assertAlmostEqual(stats.flat_top_long_run_variance(iid)["lrv"], 1.0, delta=0.12)
        e = [rng.gauss(0, 1) for _ in range(20000)]
        x = [e[0]]
        for v in e[1:]:
            x.append(0.5 * x[-1] + v)
        self.assertAlmostEqual(stats.flat_top_long_run_variance(x)["lrv"], 1 / (1 - 0.5) ** 2, delta=0.5)   # = 4

    def test_constant_series_is_not_computable(self):
        self.assertIsNone(stats.flat_top_long_run_variance([0.0005] * 50)["lrv"])

    def test_gate_uses_the_more_conservative_interval(self):
        rng = random.Random(8)
        e = [rng.gauss(0, 0.01) for _ in range(300)]
        d = [e[0]]
        for v in e[1:]:
            d.append(0.5 * d[-1] + v)
        out = stats.paired_bootstrap([x + 0.001 for x in d], [0.0] * 300, replications=400, seed=2)
        self.assertEqual(out["conservative_lower"], min(out["lower"], out["hac"]["lower"]))
        self.assertEqual(out["conservative_upper"], max(out["upper"], out["hac"]["upper"]))
        self.assertEqual(out["passes"], out["conservative_lower"] > 0.0)


class MultipleTestingTest(unittest.TestCase):
    def test_bonferroni_and_holm(self):
        p = [0.01, 0.04, 0.03, 0.20]
        self.assertEqual(stats.bonferroni(p), [0.04, 0.16, 0.12, 0.8])
        self.assertEqual([round(v, 10) for v in stats.holm(p)], [0.04, 0.09, 0.09, 0.2])


class FoldTest(unittest.TestCase):
    def test_walk_forward_known_layout(self):
        folds = walk_forward(20, train_size=8, test_size=4, horizon=1)
        self.assertEqual([f.test for f in folds], [(8, 9, 10, 11), (12, 13, 14, 15), (16, 17, 18, 19)])
        self.assertEqual(folds[0].train, (0, 1, 2, 3, 4, 5, 6))
        self.assertEqual(folds[0].purged, (7,))
        self.assertEqual(folds[1].train, (4, 5, 6, 7, 8, 9, 10))
        self.assertEqual(walk_forward(20, train_size=8, test_size=4, horizon=1, expanding=True)[2].train[0], 0)

    def test_cpcv_counts(self):
        self.assertEqual(cpcv_split_count(6, 2), 15)
        self.assertEqual(cpcv_path_count(6, 2), 5)
        self.assertEqual(len(cpcv(60, 6, 2, horizon=2, embargo=1)), 15)

    def test_purge_and_embargo_properties_over_random_configurations(self):
        """CAP-RES-07: zero violations of the purge and embargo rules over 10,000 random configurations."""
        rng = random.Random(17)
        configs = 10_000
        for _ in range(configs):
            n = rng.randint(30, 90)
            h = rng.randint(0, 5)
            emb = rng.randint(0, 5)
            kind = rng.randint(0, 2)
            if kind == 0:
                folds = walk_forward(n, train_size=rng.randint(5, 20), test_size=rng.randint(3, 10), horizon=h, embargo=emb,
                                     expanding=rng.random() < 0.5)
            elif kind == 1:
                folds = purged_kfold(n, rng.randint(2, 6), horizon=h, embargo=emb)
            else:
                g = rng.randint(3, 6)
                folds = cpcv(n, g, rng.randint(1, g - 1), horizon=h, embargo=emb)
            for f in folds:
                test = set(f.test)
                ends = [i for i in f.test if i + 1 not in test]
                for i in f.train:
                    self.assertTrue(all(abs(i - j) > h for j in f.test), "label interval overlaps a test label")
                    self.assertFalse(any(b < i <= b + emb for b in ends), "training starts inside an embargo")
                self.assertFalse(test & set(f.train))


if __name__ == "__main__":
    unittest.main()
