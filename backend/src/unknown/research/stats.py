"""Dependence-aware research statistics (CAP-RES-08, CAP-RES-09; book ch. 4, 7, 20).

All values here are Python floats and are statistics, never money. Conventions:

* Returns are simple per-session returns from the exact NAV series.
* ``sharpe`` is per observation: mean(r - rf) / sd(r - rf) with the n - 1
  standard deviation (formula ``sharpe.per_period@1``). Annualization by
  sqrt(K), K = 252, is reported only as a labelled assumption next to n_eff.
* Skewness g3 and kurtosis g4 are population moments; g4 is NON-excess (a
  normal has g4 = 3), as the DSR's (g4 - 1)/4 term requires.
* ``expected_shortfall`` at level a is minus the mean of the ceil(a n)
  smallest returns.
* ``n_eff`` is book eq. 4.2 with a declared truncation K; the gate form
  (BR-DAT-13) is min(n, n / max(1, 1 + 2 sum rho_k)).
* The paired bootstrap is the stationary bootstrap (Politis-Romano) with
  geometric block lengths of declared mean; the SAME index sequence is applied
  to candidate and incumbent in every replication. The default mean block
  length is round(n^(1/3)) - a documented rule, not the automatic optimal
  block length, which is not implemented.
* DSR/PSR/SR0/N-hat follow Bailey and Lopez de Prado as recorded in
  CAP-RES-08 (checked there against the rendered PDF). With fewer than two
  independent trials SR0 is 0 (no selection happened).
* PBO uses CSCV with S equal row blocks (leading remainder rows dropped and
  reported); rank 1 is the worst OOS performance, ties get average ranks,
  the IS-best strategy is the lowest index among IS ties.

Functions return ``None`` (with a reason where a dict is returned) when a
quantity is not computable, never a made-up number (CAP-RES-08 degraded rule).
"""
from __future__ import annotations

import itertools
import math
import random
from statistics import NormalDist
from typing import Callable, Sequence

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()
TRADING_DAYS = 252


# ------------------------------------------------------------- moments
def mean(x: Sequence[float]) -> float:
    if not x:
        raise ValueError("mean of empty sequence")
    return math.fsum(x) / len(x)


def variance(x: Sequence[float], ddof: int = 1) -> float | None:
    n = len(x)
    if n - ddof <= 0:
        return None
    m = mean(x)
    return math.fsum((v - m) ** 2 for v in x) / (n - ddof)


def std(x: Sequence[float], ddof: int = 1) -> float | None:
    v = variance(x, ddof)
    return math.sqrt(v) if v is not None else None


def sharpe(r: Sequence[float], rf: float = 0.0) -> float | None:
    """Per-observation Sharpe ratio (sharpe.per_period@1); None if n < 2 or sd == 0."""
    if len(r) < 2:
        return None
    ex = [v - rf for v in r]
    s = std(ex)
    if not s:
        return None
    return mean(ex) / s


def annualized(sr: float | None, k: int = TRADING_DAYS) -> dict:
    return {"value": sr * math.sqrt(k) if sr is not None else None, "K": k,
            "convention": f"SR_per_period x sqrt({k}); assumes serially uncorrelated returns - read with n_eff"}


def skew_kurt(r: Sequence[float]) -> tuple[float, float] | None:
    """Population skewness g3 and NON-excess kurtosis g4."""
    n = len(r)
    if n < 3:
        return None
    m = mean(r)
    m2 = math.fsum((v - m) ** 2 for v in r) / n
    if m2 <= 0:
        return None
    m3 = math.fsum((v - m) ** 3 for v in r) / n
    m4 = math.fsum((v - m) ** 4 for v in r) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def path(r: Sequence[float], start: float = 1.0) -> list[float]:
    out = [start]
    for v in r:
        out.append(out[-1] * (1.0 + v))
    return out


def max_drawdown(nav: Sequence[float]) -> float:
    """maxdd@1: max over t of (running peak - NAV_t) / running peak, as a fraction."""
    peak = -math.inf
    dd = 0.0
    for v in nav:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd


def expected_shortfall(r: Sequence[float], alpha: float = 0.05) -> float | None:
    """es@1: -mean of the ceil(alpha n) smallest returns."""
    if not r or not 0 < alpha < 1:
        return None
    k = max(1, math.ceil(alpha * len(r)))
    return -mean(sorted(r)[:k])


def autocorrelation(x: Sequence[float], lag: int) -> float | None:
    """Standard estimator: sum_{t>=lag} (x_t - m)(x_{t-lag} - m) / sum_t (x_t - m)^2."""
    n = len(x)
    if lag <= 0 or lag >= n:
        return None
    m = mean(x)
    den = math.fsum((v - m) ** 2 for v in x)
    if den == 0:
        return None
    return math.fsum((x[t] - m) * (x[t - lag] - m) for t in range(lag, n)) / den


def n_eff_from_rhos(n: int, rhos: Sequence[float]) -> float | None:
    """Book eq. 4.2 exactly: n / (1 + 2 sum rho_k); None where the denominator is not positive."""
    den = 1.0 + 2.0 * math.fsum(rhos)
    return n / den if den > 0 else None


def n_eff(r: Sequence[float], truncation: int | None = None, *, label_horizon: int = 1) -> dict:
    """eq4.2@1 with declared truncation K (default min(10, n // 4)) and the BR-DAT-13 gate form
    min(n, n_nonoverlap, n / max(1, 1 + 2 sum rho_k)), n_nonoverlap = n / label_horizon."""
    if type(label_horizon) is not int or label_horizon < 1:
        raise ValueError("label_horizon must be an integer >= 1")
    n = len(r)
    k = truncation if truncation is not None else max(1, min(10, n // 4))
    rhos = [autocorrelation(r, lag) for lag in range(1, k + 1)]
    if n < 3 or any(v is None for v in rhos):
        return {"n": n, "K": k, "rhos": None, "raw": None, "gate": None, "formula": "eq4.2@1", "label_horizon": label_horizon,
                "reason": "not computable: fewer than 3 observations or zero variance"}
    raw = n_eff_from_rhos(n, rhos)
    gate = min(float(n), n / label_horizon, n / max(1.0, 1.0 + 2.0 * math.fsum(rhos)))
    return {"n": n, "K": k, "rhos": rhos, "raw": raw, "gate": gate, "formula": "eq4.2@1", "label_horizon": label_horizon,
            "note": "raw is eq. 4.2 (None if 1 + 2 sum rho <= 0); gate = min(n, n / label_horizon, "
                    "n / max(1, 1 + 2 sum rho)) per BR-DAT-13"}


def hit_rate(r: Sequence[float]) -> float | None:
    nz = [v for v in r if v != 0]
    return (sum(1 for v in nz if v > 0) / len(nz)) if nz else None


# ------------------------------------------------- DSR, PSR, SR0, N-hat
def psr(sr: float, sr_star: float, t: int, g3: float, g4: float) -> float | None:
    """psr@1. sr and sr_star per period; g4 non-excess. None if T < 2 or the variance term is not positive."""
    if t < 2:
        return None
    var_term = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if var_term <= 0:
        return None
    return _N.cdf((sr - sr_star) * math.sqrt(t - 1) / math.sqrt(var_term))


def expected_max_sharpe(var_sr: float, n_trials: float) -> float:
    """SR0 (DSR eq. 1-2): expected maximum Sharpe of N independent zero-skill trials; 0 when N < 2."""
    if var_sr < 0:
        raise ValueError("variance of trial Sharpe ratios must be >= 0")
    if n_trials < 2:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
                                + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def implied_trials(rho: float, m: int) -> float:
    """n_hat@1: N_hat = rho + (1 - rho) M."""
    return rho + (1 - rho) * m


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    if len(a) != len(b) or len(a) < 2:
        return None
    ma, mb = mean(a), mean(b)
    sa = math.fsum((x - ma) ** 2 for x in a)
    sb = math.fsum((y - mb) ** 2 for y in b)
    if sa == 0 or sb == 0:
        return None
    return math.fsum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(sa * sb)


def average_offdiagonal_correlation(columns: Sequence[Sequence[float]]) -> float | None:
    vals = []
    for i in range(len(columns)):
        for j in range(i + 1, len(columns)):
            c = correlation(columns[i], columns[j])
            if c is None:
                return None
            vals.append(c)
    return mean(vals) if vals else None


def deflated_sharpe(selected: Sequence[float], trial_columns: Sequence[Sequence[float]], *,
                    trial_count: int | None = None, min_observations: int = 60) -> dict:
    """dsr@1 for the selected trial against its whole family (all trials, from the registry).

    ``trial_columns`` are the synchronous per-period return series of the
    family's trials that produced returns, the selected one included.
    ``trial_count`` is the registry's M for the family (CAP-RES-10: every
    status, failures included); when given, it - not the number of columns -
    is the M in N-hat = rho + (1 - rho) M, so a failed or aborted trial can
    never lower the deflation (review fix 2026-10-22). V[SR_n] and rho come
    from the columns that exist. Returns inputs and assumption checks with the
    result; 'not computable' instead of a number when the sample is too short
    or the family has fewer than two trials."""
    cols = len(trial_columns)
    if trial_count is not None and (type(trial_count) is not int or trial_count < cols):
        raise ValueError(f"registry trial count {trial_count} is below the {cols} trials with returns")
    m = cols if trial_count is None else trial_count
    t = len(selected)
    out: dict = {"formula": ["dsr@1", "psr@1", "n_hat@1"], "M": m, "M_with_returns": cols, "T": t,
                 "M_source": "registry trial_count (all statuses)" if trial_count is not None else "columns supplied"}
    if cols < 2 or t < min_observations or any(len(c) != t for c in trial_columns):
        out.update(dsr=None, reason=f"not computable: need >= 2 synchronous trials and >= {min_observations} observations")
        return out
    srs = [sharpe(c) for c in trial_columns]
    sr = sharpe(selected)
    moments = skew_kurt(selected)
    if sr is None or moments is None or any(v is None for v in srs):
        out.update(dsr=None, reason="not computable: a trial has zero variance")
        return out
    rho = average_offdiagonal_correlation(trial_columns)
    n_hat = implied_trials(rho, m) if rho is not None else float(m)
    var_sr = variance(srs) or 0.0
    sr0 = expected_max_sharpe(var_sr, n_hat)
    g3, g4 = moments
    out.update(sr_selected=sr, trial_sharpes=srs, var_sr=var_sr, rho_bar=rho, n_hat=n_hat, sr0=sr0, g3=g3, g4=g4,
               dsr=psr(sr, sr0, t, g3, g4), psr_zero=psr(sr, 0.0, t, g3, g4),
               ill_conditioned=t < m * (m - 1) / 2,
               note="per-period Sharpe; N-hat from the average off-diagonal correlation (both M and N-hat reported); "
                    "the paper warns the estimate can be ill-conditioned when T < M(M-1)/2")
    return out


# ----------------------------------------------------------------- PBO
def pbo_cscv(matrix: Sequence[Sequence[float]], s: int = 16, *, metric: str = "sharpe") -> dict:
    """pbo.cscv@1 on a T x N matrix of synchronous per-period returns (rows = periods, columns = trials)."""
    if s < 2 or s % 2:
        raise ValueError("S must be an even number >= 2")
    t = len(matrix)
    n = len(matrix[0]) if t else 0
    if n < 2 or t < s * 2:
        return {"pbo": None, "reason": "not computable: need >= 2 trials and >= 2 rows per block", "S": s, "T": t, "N": n,
                "formula": "pbo.cscv@1"}
    if any(len(row) != n for row in matrix):
        raise ValueError("matrix rows must be synchronous (same number of trials)")
    size = t // s
    dropped = t - size * s
    rows = matrix[dropped:]
    blocks = []  # per block, per trial: (count, sum, sumsq)
    for b in range(s):
        chunk = rows[b * size:(b + 1) * size]
        blocks.append([(size, math.fsum(r[j] for r in chunk), math.fsum(r[j] * r[j] for r in chunk)) for j in range(n)])

    def perf(block_ids: Sequence[int], j: int) -> float:
        cnt = sum(blocks[b][j][0] for b in block_ids)
        sm = math.fsum(blocks[b][j][1] for b in block_ids)
        sq = math.fsum(blocks[b][j][2] for b in block_ids)
        mu = sm / cnt
        if metric == "mean":
            return mu
        var = (sq - cnt * mu * mu) / (cnt - 1)
        return mu / math.sqrt(var) if var > 1e-300 else (math.inf if mu > 0 else (-math.inf if mu < 0 else 0.0))

    logits = []
    pairs = []
    below = 0
    combos = 0
    for train in itertools.combinations(range(s), s // 2):
        test = [b for b in range(s) if b not in train]
        is_perf = [perf(train, j) for j in range(n)]
        oos_perf = [perf(test, j) for j in range(n)]
        best = max(range(n), key=lambda j: (is_perf[j], -j))
        target = oos_perf[best]
        lower = sum(1 for v in oos_perf if v < target)
        equal = sum(1 for v in oos_perf if v == target)
        rank = lower + (equal + 1) / 2.0          # average rank among ties, 1 = worst
        omega = rank / (n + 1)
        lam = math.log(omega / (1 - omega))
        logits.append(lam)
        pairs.append((is_perf[best], target))
        below += lam <= 0
        combos += 1
    return {"pbo": below / combos, "combinations": combos, "S": s, "T": t, "N": n, "rows_per_block": size,
            "rows_dropped": dropped, "metric": metric, "logit_mean": mean(logits), "logits": logits,
            "prob_oos_loss": sum(1 for _, o in pairs if o < 0) / combos, "formula": "pbo.cscv@1"}


# ------------------------------------------------------------ bootstrap
def default_block_length(n: int) -> int:
    return max(1, round(n ** (1.0 / 3.0)))


def stationary_bootstrap_indices(n: int, mean_block: float, rng: random.Random) -> list[int]:
    """Politis-Romano stationary bootstrap: geometric blocks with mean ``mean_block``, circular wrap."""
    if n <= 0 or mean_block < 1:
        raise ValueError("need n > 0 and mean_block >= 1")
    p = 1.0 / mean_block
    idx = [rng.randrange(n)]
    for _ in range(n - 1):
        idx.append(rng.randrange(n) if rng.random() < p else (idx[-1] + 1) % n)
    return idx


def flat_top_long_run_variance(x: Sequence[float], *, c: float = 2.0) -> dict:
    """lrv.flat_top@1: long-run variance with the trapezoidal flat-top kernel and the empirical bandwidth rule.

    lambda(t) = 1 for |t| <= 1/2, 2(1 - |t|) for 1/2 < |t| <= 1, else 0 (Politis-Romano flat-top);
    M = 2 m_hat, m_hat the smallest lag after which K_n consecutive autocorrelations are below
    c sqrt(log10(n) / n), K_n = max(5, ceil(sqrt(log10 n))) (Politis' empirical rule). A non-positive
    estimate (possible with flat-top kernels) falls back to the lag-0 variance and is flagged."""
    n = len(x)
    if n < 10:
        return {"lrv": None, "reason": "fewer than 10 observations"}
    m = mean(x)
    xc = [v - m for v in x]
    lmax = max(1, min(n // 2, 100))
    gam = [math.fsum(xc[t] * xc[t - k] for t in range(k, n)) / n for k in range(lmax + 1)]
    if gam[0] <= 0:
        return {"lrv": None, "reason": "zero variance"}
    kn = max(5, math.ceil(math.sqrt(math.log10(n))))
    thr = c * math.sqrt(math.log10(n) / n)
    m_hat = None
    for mm in range(0, lmax - kn):
        if all(abs(gam[mm + j] / gam[0]) < thr for j in range(1, kn + 1)):
            m_hat = mm
            break
    if m_hat is None:
        m_hat = lmax - kn
    big_m = max(1, 2 * m_hat)
    total = gam[0]
    for k in range(1, min(big_m, lmax) + 1):
        t = k / big_m
        total += 2.0 * (1.0 if t <= 0.5 else 2.0 * (1.0 - t)) * gam[k]
    fallback = total <= 0
    return {"lrv": gam[0] if fallback else total, "bandwidth_M": big_m, "fallback_to_variance": fallback,
            "formula": "lrv.flat_top@1"}


def utility(r: Sequence[float], c_drawdown: float = 0.0, c_es: float = 0.0, alpha: float = 0.05) -> float:
    """utility.ch20@1: mean(r) - c1 maxdd(path(r)) - c2 ES_alpha(r); coefficients fixed in the promotion protocol."""
    u = mean(r)
    if c_drawdown:
        u -= c_drawdown * max_drawdown(path(r))
    if c_es:
        u -= c_es * (expected_shortfall(r, alpha) or 0.0)
    return u


def _quantile(sorted_x: Sequence[float], q: float) -> float:
    """Linear interpolation between order statistics (type 7)."""
    if not sorted_x:
        raise ValueError("empty")
    pos = q * (len(sorted_x) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_x) - 1)
    return sorted_x[lo] + (sorted_x[hi] - sorted_x[lo]) * (pos - lo)


def paired_bootstrap(candidate: Sequence[float], incumbent: Sequence[float], *, replications: int = 2000,
                     mean_block: float | None = None, confidence: float = 0.90, seed: int = 0, hurdle: float = 0.0,
                     statistic: Callable[[Sequence[float]], float] = mean, statistic_id: str = "mean",
                     keep_indices: bool = False) -> dict:
    """bootstrap.stationary.paired@1: interval for U(candidate) - U(incumbent) on identical dates.

    The same stationary-bootstrap index sequence is applied to both series in
    every replication (paired; never two independent intervals). The gate
    passes when the lower end of the two-sided ``confidence`` interval exceeds
    ``hurdle`` (per-period units)."""
    n = len(candidate)
    if n != len(incumbent):
        raise ValueError("paired comparison needs series on identical dates (equal length)")
    if n < 10:
        return {"computable": False, "reason": "not computable: fewer than 10 paired observations", "n": n}
    b = mean_block if mean_block is not None else default_block_length(n)
    rng = random.Random(seed)
    diffs = []
    kept = []
    for _ in range(replications):
        idx = stationary_bootstrap_indices(n, b, rng)
        c = [candidate[i] for i in idx]
        k = [incumbent[i] for i in idx]
        diffs.append(statistic(c) - statistic(k))
        if keep_indices:
            kept.append(idx)
    diffs.sort()
    a = (1.0 - confidence) / 2.0
    lower, upper = _quantile(diffs, a), _quantile(diffs, 1.0 - a)
    point = statistic(candidate) - statistic(incumbent)
    # Second dependence-aware interval for the mean statistic: normal with a flat-top long-run variance of the
    # paired differences. The gate uses the more conservative of the two (measured coverage in the docs).
    hac = None
    cons_lower, cons_upper = lower, upper
    if statistic_id == "mean":
        lrv = flat_top_long_run_variance([c_ - i_ for c_, i_ in zip(candidate, incumbent)])
        if lrv["lrv"] is not None:
            z = _N.inv_cdf(1.0 - a)
            se = math.sqrt(lrv["lrv"] / n)
            hac = {"lower": point - z * se, "upper": point + z * se, "se": se, "bandwidth_M": lrv["bandwidth_M"],
                   "fallback_to_variance": lrv["fallback_to_variance"], "formula": "lrv.flat_top@1"}
            cons_lower, cons_upper = min(lower, hac["lower"]), max(upper, hac["upper"])
    out = {"computable": True, "n": n, "point": point, "lower": lower, "upper": upper, "confidence": confidence,
           "hac": hac, "conservative_lower": cons_lower, "conservative_upper": cons_upper,
           "hurdle": hurdle, "passes": cons_lower > hurdle, "replications": replications, "mean_block": b,
           "block_rule": "given" if mean_block is not None else "round(n^(1/3))", "seed": seed,
           "statistic": statistic_id, "formula": "bootstrap.stationary.paired@1",
           "gate_rule": "conservative_lower = min(bootstrap percentile lower, flat-top HAC normal lower) > hurdle",
           "bootstrap_mean": mean(diffs), "bootstrap_sd": std(diffs)}
    if keep_indices:
        out["indices"] = kept
    return out


# ------------------------------------------------------ multiple testing
def bonferroni(pvalues: Sequence[float]) -> list[float]:
    m = len(pvalues)
    return [min(1.0, p * m) for p in pvalues]


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values (monotone)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adj[i] = running
    return adj


# --------------------------------------------------------------- summary
def summary(returns: Sequence[float], nav: Sequence[float], *, turnover: Sequence[float] = (),
            exposure: Sequence[float] = ()) -> dict:
    r = list(returns)
    sr = sharpe(r)
    wins = [v for v in r if v > 0]
    losses = [v for v in r if v < 0]
    total = (nav[-1] / nav[0] - 1.0) if nav and nav[0] else None
    years = len(r) / TRADING_DAYS if r else 0
    ann = ((1.0 + total) ** (1.0 / years) - 1.0) if total is not None and years > 0 and total > -1 else None
    sk = skew_kurt(r)
    return {
        "n": len(r), "mean": mean(r) if r else None, "sd": std(r), "sharpe_per_period": sr,
        "sharpe_annualized": annualized(sr), "total_return": total,
        "annualized_return": {"value": ann, "convention": f"geometric, {TRADING_DAYS} sessions per year (labelled assumption)"},
        "max_drawdown": max_drawdown(nav) if nav else None, "expected_shortfall_5pct": expected_shortfall(r, 0.05),
        "hit_rate": hit_rate(r), "avg_win": mean(wins) if wins else None, "avg_loss": mean(losses) if losses else None,
        "skewness": sk[0] if sk else None, "kurtosis_non_excess": sk[1] if sk else None,
        "turnover_mean": mean(turnover) if turnover else None, "exposure_mean": mean(exposure) if exposure else None,
        "time_in_market": (sum(1 for e in exposure if e > 1e-12) / len(exposure)) if exposure else None,
        "n_eff": n_eff(r) if len(r) >= 3 else None,
        "formulas": ["return.simple@1", "sharpe.per_period@1", "maxdd@1", "es@1", "turnover.one_way@1", "eq4.2@1"],
    }
