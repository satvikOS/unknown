"""Shared research primitives: errors, the formula registry and stable hashing.

Every derived research number carries a ``formula_id@version`` from
``FORMULAS`` so the terminal can show the calculation (DAT-05). Hashes of
research artifacts use ``stable_dumps``: sorted keys, no whitespace, exact
decimal strings for ``Fixed`` and ISO dates. Finite floats are allowed in
research payloads (statistics are floats by convention) and are rendered with
Python's shortest round-trip ``repr``; NaN and infinities are refused so a
hash never depends on a non-number.
"""
from __future__ import annotations

import enum
import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any

from ..domain.fixed import Fixed, Rounding, round_div

NS_PER_S = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_S


# ----------------------------------------------------------------- errors
class ResearchError(Exception):
    code = "RESEARCH_ERROR"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class SpecError(ResearchError, ValueError):
    code = "SPEC_INVALID"


class LookaheadError(ResearchError):
    """A decision tried to read data whose availability time is after the decision time."""
    code = "LOOKAHEAD"


class CausalityError(ResearchError):
    """An order asked to fill on information it was derived from (e.g. a same-bar close fill)."""
    code = "CAUSALITY"


class HoldoutError(ResearchError):
    code = "HOLDOUT_REFUSED"


class RegistryError(ResearchError):
    code = "REGISTRY_REFUSED"


class AuthorityError(ResearchError, PermissionError):
    code = "UNAUTHORIZED"


class EvidenceError(ResearchError):
    code = "EVIDENCE_INVALID"


# --------------------------------------------------------- formula registry
FORMULAS: dict[str, dict[str, str]] = {
    "eq15.1@1": {
        "name": "TREND_DAILY_V1 score", "book": "ch. 15 eq. 15.1",
        "expression": "z_t = (EMA_f(log P)_t - EMA_s(log P)_t) / (sigma_t * sqrt(h) + eps); w_t = min(w_max, k * max(0, z_t))",
        "units": "z dimensionless; w fraction of marked equity"},
    "eq5.2.ewma_var@1": {
        "name": "EWMA variance of log returns", "book": "ch. 5 eq. 5.2",
        "expression": "v_t = lambda * v_{t-1} + (1 - lambda) * r_t^2, lambda = 2^(-1/H); sigma_t = sqrt(v_t)",
        "units": "per-session log-return variance; zero-mean approximation"},
    "ema.span@1": {
        "name": "exponential moving average by span", "book": "ch. 5 eq. 5.2 (first form)",
        "expression": "m_t = (1 - a) m_{t-1} + a x_t, a = 2 / (span + 1), m_1 = x_1",
        "units": "units of x"},
    "eq4.2@1": {
        "name": "effective sample size", "book": "ch. 4 eq. 4.2",
        "expression": "n_eff = n / (1 + 2 * sum_{k=1..K} rho_k); gate form (BR-DAT-13): "
                      "min(n, n / label_horizon, n / max(1, 1 + 2 sum rho_k))",
        "units": "observations"},
    "eq7.1.br-dat-03@1": {
        "name": "implementation shortfall (corrected, BR-DAT-03)", "book": "ch. 7 eq. 7.1 as resolved by BR-DAT-03",
        "expression": "IS = s (sum q_j p_j - (sum q_j) p0) + fees + s (Q - sum q_j)(p_mark - p0); s=+1 buy, -1 sell",
        "units": "instrument currency; bps = IS / (Q p0) * 1e4"},
    "impact.sqrt@1": {
        "name": "square-root impact scenario", "book": "ch. 7",
        "expression": "I = Y * sigma * sqrt(Q / V)",
        "units": "fraction of price; sigma daily log-return volatility, Q fill shares, V bar volume (shares)"},
    "cost.price_fraction@1": {
        "name": "per-side price cost", "book": "ch. 7; CAP-RES-05",
        "expression": "c = k * (spread_bps / 2 + slippage_bps) / 1e4 + k * I, rounded up to 1e-8",
        "units": "fraction of reference price"},
    "fee.ibkr.us.fixed@2026-10-22": {
        "name": "IBKR Pro Fixed (US stocks)", "book": "CAP-RES-05 (IBKR page fetched 2026-10-22)",
        "expression": "C = min(max(0.005 * shares, 1.00), 0.01 * trade_value) per order",
        "units": "USD"},
    "fee.ibkr.us.tiered@2026-10-22": {
        "name": "IBKR Pro Tiered base commission (US stocks)", "book": "CAP-RES-05 (IBKR page fetched 2026-10-22)",
        "expression": "marginal per-share rate by calendar-month volume tier; per order min 0.35, max 1% of trade value",
        "units": "USD, before third-party fees"},
    "nav@1": {"name": "net asset value", "book": "CAP-RES-04",
              "expression": "NAV_t = settled_cash + sum(unsettled signed amounts) + sum_i q_i * close_i", "units": "USD"},
    "return.simple@1": {"name": "simple return", "book": "ch. 5; CAP-RES-04",
                        "expression": "r_t = NAV_t / NAV_{t-1} - 1 (no external flows in a backtest)", "units": "fraction per session"},
    "turnover.one_way@1": {"name": "one-way turnover", "book": "CAP-RES-04",
                           "expression": "0.5 * sum |traded notional| / NAV_{t-1}", "units": "fraction per session"},
    "sharpe.per_period@1": {"name": "Sharpe ratio per observation", "book": "ch. 7",
                            "expression": "SR = mean(r - rf) / sd(r - rf), sd with n - 1; annualized SR * sqrt(K) only as a labelled assumption",
                            "units": "per session (K = 252 for the labelled annualization)"},
    "maxdd@1": {"name": "maximum drawdown", "book": "CAP-RES-09",
                "expression": "max_t (peak_{<=t} - NAV_t) / peak_{<=t}", "units": "fraction"},
    "es@1": {"name": "expected shortfall", "book": "ch. 6 eq. 6.3; CAP-RES-09",
             "expression": "ES_a = -mean of the ceil(a n) smallest returns", "units": "fraction per session"},
    "psr@1": {"name": "probabilistic Sharpe ratio", "book": "CAP-RES-08 (Bailey and Lopez de Prado)",
              "expression": "PSR(SR*) = Phi((SR - SR*) sqrt(T - 1) / sqrt(1 - g3 SR + (g4 - 1)/4 SR^2)), g4 non-excess",
              "units": "probability"},
    "dsr@1": {"name": "deflated Sharpe ratio", "book": "CAP-RES-08 (Bailey and Lopez de Prado eq. 1-2)",
              "expression": "DSR = PSR(SR0), SR0 = sqrt(V[SR_n]) ((1 - gamma) Phi^-1(1 - 1/N) + gamma Phi^-1(1 - 1/(N e)))",
              "units": "probability; SR per period"},
    "n_hat@1": {"name": "implied independent trials", "book": "CAP-RES-08 (Appendix A.3 eq. 9)",
                "expression": "N_hat = rho + (1 - rho) M, rho = average off-diagonal correlation", "units": "trials"},
    "pbo.cscv@1": {"name": "probability of backtest overfitting via CSCV", "book": "CAP-RES-08 (Bailey et al.)",
                   "expression": "PBO = share of C(S, S/2) splits with logit(omega_c) <= 0, omega_c = OOS rank of IS-best / (N + 1)",
                   "units": "probability"},
    "bootstrap.stationary.paired@1": {
        "name": "paired stationary block bootstrap", "book": "ch. 4, ch. 20; CAP-RES-09",
        "expression": "D* = U(c[idx]) - U(i[idx]) with the SAME stationary-bootstrap indices idx for both series; percentile interval",
        "units": "utility units per session"},
    "lrv.flat_top@1": {
        "name": "flat-top long-run variance (HAC)", "book": "ch. 4 (dependence); CAP-RES-09 second interval",
        "expression": "LRV = sum_{|k|<=M} lambda(k/M) gamma_k, trapezoid flat-top lambda, M = 2 m_hat by the empirical rule; "
                      "interval point +/- z sqrt(LRV / n)",
        "units": "variance per session; interval in the statistic's units"},
    "utility.ch20@1": {"name": "promotion utility", "book": "ch. 20; CAP-RES-09",
                       "expression": "U = mean(r) - c1 * maxdd(path(r)) - c2 * ES_a(r)", "units": "fraction per session"},
}


def formula(formula_id: str) -> dict[str, str]:
    if formula_id not in FORMULAS:
        raise KeyError(f"unregistered formula {formula_id!r}")
    return {"formula_id": formula_id, **FORMULAS[formula_id]}


# ----------------------------------------------------------------- hashing
def _plain(value: Any) -> Any:
    if isinstance(value, Fixed):
        return value.to_str()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float in a hashed research payload")
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, "to_json"):
        return _plain(value.to_json())
    raise TypeError(f"not encodable in a research payload: {type(value).__name__}")


def plain(value: Any) -> Any:
    """JSON-ready copy: Fixed -> exact decimal string, dates -> ISO, enums -> value."""
    return _plain(value)


def stable_dumps(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_dumps(value).encode("utf-8")).hexdigest()


def fixed_to_float(x: Fixed) -> float:
    """Exact decimal -> nearest float, for statistics only (never for money)."""
    return x.mantissa / (10 ** x.scale)


def float_to_fixed(x: float, scale: int, rounding: Rounding) -> Fixed:
    """Float statistic -> Fixed at ``scale`` with one explicit rounding step.

    The float is read through its shortest round-trip ``repr`` (so 0.1 is the
    decimal 0.1, not its binary expansion) as an exact rational, then rounded
    once at the target scale. Used only where a statistical estimate (a cost
    fraction, a target weight) crosses into exact arithmetic."""
    if not isinstance(x, (float, int)) or isinstance(x, bool) or not math.isfinite(x):
        raise ValueError(f"not a finite number: {x!r}")
    q = Fraction(Decimal(repr(float(x))))
    return Fixed(round_div(q.numerator * 10 ** scale, q.denominator, rounding), scale)
