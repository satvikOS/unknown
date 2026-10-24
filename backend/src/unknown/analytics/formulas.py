"""Formula registry for portfolio and risk analytics (DAT-05: formula, inputs, units, provenance).

Every published analytic names a formula as ``id@version``. A change in definition is a
new version, never an edit in place, so a stored result stays interpretable. The text is
the definition actually implemented (not an aspiration); ``caps`` and ``book`` point at
the capability records and book equations it implements."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Formula:
    formula_id: str
    version: int
    text: str
    units: str
    caps: tuple[str, ...]
    book: str = ""

    @property
    def ref(self) -> str:
        return f"{self.formula_id}@{self.version}"

    def to_json(self) -> dict:
        return {"id": self.ref, "definition": self.text, "units": self.units, "capabilities": list(self.caps),
                "book": self.book}


_ALL = [
    # ------------------------------------------------------------- holdings / NAV
    Formula("port.mark", 1, "mark = bid | mid=(bid+ask)/2 | last of the supplied quote snapshot; age = t_val - "
            "(event_time or receipt_time); 'fresh' iff age <= budget and not delayed; 'stale' otherwise; 'unpriced' "
            "if the chosen field is missing; a quote received after t_val is ignored (look-ahead guard)",
            "currency per unit; ms", ("CAP-PORT-01",), "ch09; ch11 freshness"),
    Formula("port.market_value", 1, "MV = q * mark * multiplier, rounded toward -infinity at the currency ledger scale "
            "(same rounding as core valuation)", "currency", ("CAP-PORT-01",), "ch03 eq 3.1"),
    Formula("port.nav", 1, "NAV(owner, ccy) = CASH + RECEIVABLE + PAYABLE + FEES_ACCRUED + sum MV; payables and accrued "
            "fees carry credit (negative) balances; policy buckets partition NAV and never add to it (ADR-0006). "
            "nav_fresh is published only when every position has a fresh mark; nav_marked uses last marks (stale "
            "flagged) and is null when any position is unpriced. Unpriced positions are never valued at zero.",
            "currency", ("CAP-PORT-01",), "ch09; ADR-0006"),
    Formula("port.available_cash", 1, "A(owner, ccy) = CASH + PAYABLE + FEES_ACCRUED - active cash holds (ADR-0006); "
            "receivables are never available", "currency", ("CAP-PORT-01", "CAP-PORT-12"), "ch09 eq 9.1"),
    Formula("port.weight", 1, "w = MV / D, D named explicitly (scope NAV, owner NAV, account NAV, authorized capital, "
            "session seed); 8 decimal places, half-even", "dimensionless", ("CAP-PORT-01", "CAP-PORT-03"), "ch06"),
    Formula("port.external_flow", 1, "CF(scope) = sum of postings to value codes (CASH, RECEIVABLE, PAYABLE, FEES_ACCRUED, "
            "INVENTORY) on owners inside the scope, over journal entries of kind OWNER_TRANSFER, EXTERNAL_FLOW or "
            "OPENING, dated at the event's recorded time. Transfers between two owners inside the scope net to zero; "
            "trading P&L, fees, income and settlement are never flows.", "currency", ("CAP-PORT-04",), "GIPS 2020; ADR-0006"),
    Formula("pnl.daily", 1, "P&L = (NAV_b - NAV_a) - external flows between consecutive complete snapshots; explained = "
            "-(sum of postings to REALIZED, COMMISSION, FEE_ESTIMATE, OTHER_FEES, DIVIDEND, WITHHOLDING, INTEREST, ROUNDING, "
            "SUSPENSE) for in-scope owners recorded in (as_of_a, as_of_b] + change in (marked value - open lot cost); "
            "residual = P&L - explained must be exactly zero", "currency", ("CAP-PORT-02",), "ch09; ADR-0006"),
    # ------------------------------------------------------------- performance
    Formula("perf.return.start_of_day", 1, "r = EMV / (BMV + CF) - 1, CF = net external flow between the two valuations, "
            "treated as arriving at the start of the sub-period", "decimal", ("CAP-PORT-04",), "IBKR TWR example"),
    Formula("perf.return.end_of_day", 1, "r = (EMV - CF) / BMV - 1, CF treated as arriving at the end of the sub-period",
            "decimal", ("CAP-PORT-04",), ""),
    Formula("perf.twr", 2, "TWR = prod(1 + r_t) - 1 over consecutive complete valuations from inception to termination; a "
            "sub-period with no capital at risk between two funded ones (BMV + CF = 0 and EMV = 0 start of day; BMV = 0 "
            "and EMV - CF = 0 end of day) links as r = 0; any other undefined sub-period breaks the chain",
            "decimal", ("CAP-PORT-04",), "ch06"),
    Formula("perf.unit_value", 1, "U_0 = 1000; U_t = U_{t-1} * (1 + r_t)", "index points", ("CAP-PORT-04", "CAP-PORT-05"), "ch06"),
    Formula("perf.drawdown", 1, "D_t = 1 - U_t / max_{s<=t} U_s on the flow-adjusted unit-value series; MDD = max D_t",
            "decimal", ("CAP-PORT-05",), "ch06 drawdown"),
    Formula("perf.modified_dietz", 1, "R = (EMV - BMV - sum CF_i) / (BMV + sum W_i CF_i), W_i = (CD - D_i) / CD",
            "decimal", ("CAP-PORT-04",), "IBKR MWR white paper"),
    Formula("perf.irr", 2, "solve -BMV (1+r)^(T/365) - sum CF_i (1+r)^((T - t_i)/365) + EMV = 0 for r by bisection on "
            "(-0.999999, 1e6); refused when the equation has more than one distinct root on the grid (a root exactly on a "
            "grid point counts once); r is annualized; the period MWR (1+r)^(T/365) - 1 is shown instead when the period "
            "is shorter than one calendar year",
            "decimal per year", ("CAP-PORT-04",), "GIPS 2.A.29"),
    Formula("perf.annualize", 1, "(1+R)^(365/days) - 1 only when the period spans at least one calendar year (end >= start "
            "+ 1 year); otherwise not annualized (GIPS 2.A.12)",
            "decimal per year", ("CAP-PORT-04",), ""),
    Formula("stats.volatility", 1, "sigma = sample std (n-1) of sub-period returns; sigma_ann = sigma * sqrt(n_per_year)",
            "decimal", ("CAP-PORT-05",), ""),
    Formula("stats.sharpe", 1, "Sharpe = (mean(r) * n_per_year - Rf_ann) / (std(r) * sqrt(n_per_year)); Rf_ann = 0 and "
            "labelled when no risk-free series is configured", "dimensionless", ("CAP-PORT-05",), ""),
    Formula("stats.sortino", 1, "Sortino = (mean(r) * n - MAR_ann) / (DD * sqrt(n)); DD = sqrt(sum_t min(0, r_t - MAR)^2 / N) "
            "over all N periods (target semideviation); the IBKR variant (sample std of negative returns) is reported "
            "separately", "dimensionless", ("CAP-PORT-05",), ""),
    Formula("stats.n_eff", 1, "n_eff = n / (1 + 2 sum_{k=1}^{K} (1 - k/n) rho_k), rho_k sample autocorrelation, summed while "
            "rho_k > 0 up to K = min(20, floor(n/4)); clipped to [1, n]", "count", ("CAP-PORT-05",), "ch04 sample size"),
    Formula("stats.sharpe_se", 1, "SE(SR_period) = sqrt((1 + SR_period^2 / 2) / n) (iid normal, Lo 2002); also with n_eff; "
            "annualized by sqrt(n_per_year)", "dimensionless", ("CAP-PORT-05",), ""),
    Formula("stats.relative", 1, "TE = std(r_p - r_b) sqrt(n); IR = mean(r_p - r_b) n / TE; beta = Cov(r_p, r_b)/Var(r_b) "
            "with n-1 in both; rho = Cov/(sigma_p sigma_b)", "mixed", ("CAP-PORT-05",), ""),
    # ------------------------------------------------------------- exposure
    Formula("expo.long_short", 1, "L = sum MV_i (q_i > 0); S = sum MV_i (q_i < 0); G = L + |S|; N = L + S", "currency",
            ("CAP-PORT-03",), "ch06"),
    Formula("expo.bucket_weight", 1, "w_g = sum_{i in g} MV_i / D, D named", "dimensionless", ("CAP-PORT-03",), "ch06"),
    Formula("expo.hhi", 1, "HHI = sum_i (|MV_i| / G)^2 over priced positions; effective N = 1/HHI; top-k = sum of k largest "
            "|MV_i| / G", "dimensionless", ("CAP-PORT-03",), ""),
    Formula("expo.sic_division", 1, "sector = SEC EDGAR 'sic' of the issuer resolved through a ticker alias valid at the "
            "decision time; division by 1987 SIC Manual major-group ranges; otherwise bucket 'unknown'",
            "classification", ("CAP-PORT-03",), "SEC SIC code list"),
    # ------------------------------------------------------------- VaR / ES
    Formula("risk.hist_scenario", 1, "scenario s: dV_s = sum_i q_i * mark_i * mult_i * R_i,s with R the simple (h-day "
            "overlapping, compounded) return of instrument i ending on date s; L_s = -dV_s", "currency",
            ("CAP-PORT-10",), "ch06"),
    Formula("risk.var_hist", 1, "VaR_alpha = L_(k), k = ceil(alpha * N), losses sorted ascending (inf{l: P(L<=l) >= alpha})",
            "currency", ("CAP-PORT-10",), "ch06"),
    Formula("risk.es_quantile_integral", 1, "ES_alpha = (1/(1-alpha)) * integral_alpha^1 VaR_u du = (1/(1-alpha)) * "
            "[ (k/N - alpha) L_(k) + (1/N) sum_{j>k} L_(j) ]; ES_1 = L_(N)", "currency", ("CAP-PORT-10",), "book eq 6.3"),
    Formula("risk.es_tail_mean", 1, "ES_tail = mean{ L_s : L_s >= VaR_alpha } (IBKR wording); differs from eq 6.3 when the "
            "empirical distribution has atoms; not used for subadditivity checks", "currency", ("CAP-PORT-10",), ""),
    Formula("risk.kupiec_pof", 1, "LR = -2 ln[(1-p)^(T-x) p^x] + 2 ln[(1-x/T)^(T-x) (x/T)^x], p = 1 - alpha; p-value = "
            "P(chi2_1 > LR) = erfc(sqrt(LR/2)); exception when realized loss > prior-day VaR", "dimensionless",
            ("CAP-PORT-10",), "BIS backtesting design reference"),
    # ------------------------------------------------------------- stress
    Formula("stress.revalue", 2, "dV = sum_i q_i * mult_i * (p_i' - p_i), p_i' = p_i * (1 + s_i) at exact precision; loss "
            "thresholds (session drawdown, session max loss, account loss budgets) are reported as crossed or not; the "
            "loss is never capped at a threshold because a reduction request does not execute at the threshold. Account "
            "budgets use system P&L at current marks plus dV, and the core's daily window (day start = first core "
            "valuation of the risk day, or the scenario valuation itself when none exists yet that day)",
            "currency", ("CAP-PORT-11",), "ch06 stress"),
    Formula("stress.gap_fills", 2, "on a gap, a resting BUY limit at L fills when the gapped ask <= L, at price L, adding "
            "q*(p' - L) of immediate marked loss; a resting SELL limit at L fills when the gapped bid >= L, at price L, "
            "adding q*(L - p') (the sold quantity earns L, not the gapped bid p'); it does not fill when the gapped bid < L",
            "currency", ("CAP-PORT-11",), "ch06; ch13"),
    Formula("stress.fee_revision", 1, "fee' = estimate(policy fee model with per_share and minimum scaled by f); impact = "
            "(pending estimated fees) * (f - 1) + exit-cost change + envelope shortfall on open buys", "currency",
            ("CAP-PORT-11",), "ch09 fees"),
    # ------------------------------------------------------------- what-if
    Formula("whatif.cash", 1, "required = ceil(q * limit * mult) + fee envelope (buys); A' = A - required; receivables add 0",
            "currency", ("CAP-PORT-12",), "ch09 reservation"),
    Formula("whatif.headroom", 1, "h = limit - metric(after); pass iff h >= 0 for each exposure-increasing check; checks come "
            "from risk.pretrade.evaluate, the same function the core command path runs", "unit of limit",
            ("CAP-PORT-12",), "ch13"),
    # ------------------------------------------------------------- allocation
    Formula("alloc.equal_weight", 1, "w_i = (1 - c_reserve) / N", "weights", ("CAP-PORT-13",), "ch06"),
    Formula("alloc.inverse_vol", 2, "w_i = clip(k / sigma_i, floor, cap) with k solved exactly so that sum w_i = 1 - c_reserve "
            "(proportional to 1/sigma_i, with the excess of clipped names redistributed proportionally among the unclipped "
            "ones); v1 could stop with weights summing to less than the budget", "weights", ("CAP-PORT-13",), "ch06"),
    Formula("alloc.ledoit_wolf", 1, "Sigma* = delta * m I + (1 - delta) S, S = (1/n) X'X of demeaned returns, m = tr(S)/p, "
            "delta = min(b2, d2)/d2 (Ledoit-Wolf 2004 target mu*I)", "return^2 per period", ("CAP-PORT-13",), "ch06"),
    Formula("alloc.mean_variance_l1", 1, "minimise 0.5 w'Sigma w - lambda mu'w + eta ||w - w_old||_1 s.t. 0 <= w_i <= u_i, "
            "sum w = 1 - c_reserve (book eq 6.2); exact pairwise coordinate descent; a turnover limit tau is enforced by "
            "raising eta (Lagrangian of the L1 constraint) found by bisection", "weights", ("CAP-PORT-13",), "book eq 6.2"),
    Formula("alloc.lot_rounding", 1, "q_i = floor(w_i * NAV / (p_i * mult_i * lot_i)) * lot_i; zero quantity -> no trade "
            "(book eq 6.4 rule); constraints re-verified after rounding and fees", "units", ("CAP-PORT-13",), "book eq 6.4"),
    Formula("alloc.turnover", 1, "turnover = 0.5 * sum_i |w_i(new) - w_i(old)|", "decimal", ("CAP-PORT-13",), ""),
    # ------------------------------------------------------------- market risk from stored real bars
    Formula("mrisk.daily_return", 1, "r_t = C_t / C_{t-1} - 1 on consecutive stored daily closes (regular-session, "
            "non-fixture, latest version available at the valuation instant); when two series are compared, only "
            "session dates present in both are used and returns run between consecutive common dates",
            "decimal", ("CAP-PORT-05", "CAP-PORT-03"), ""),
    Formula("mrisk.hist_vol", 1, "sigma_ann = sample std (n-1) of the last N daily returns * sqrt(252); published only "
            "when N >= min_obs", "decimal per year", ("CAP-PORT-05",), ""),
    Formula("mrisk.beta", 1, "beta_i = Cov(r_i, r_b) / Var(r_b), sample (n-1) normalisation, on date-aligned daily "
            "returns versus the benchmark ETF b; published only when n >= min_obs and Var(r_b) > 0",
            "dimensionless", ("CAP-PORT-03", "CAP-PORT-05"), ""),
    Formula("mrisk.correlation", 1, "rho_ij = Cov(r_i, r_j) / (sd_i sd_j) on date-aligned daily returns (sample "
            "normalisation); null when either standard deviation is zero or n < min_obs", "dimensionless",
            ("CAP-PORT-05",), ""),
    Formula("mrisk.portfolio_beta", 1, "beta_p = sum_i w_i beta_i, w_i = MV_i / sum_j MV_j over the priced positions of "
            "one currency; published only when every priced position has a beta (no position is assumed to be 1)",
            "dimensionless", ("CAP-PORT-03",), ""),
    Formula("mrisk.portfolio_vol", 1, "sigma_p = sample std of r_p,t = sum_i w_i r_i,t * sqrt(252), current weights "
            "held constant over the dates common to every priced position; published only when every priced position "
            "has returns on at least min_obs common dates", "decimal per year", ("CAP-PORT-05",), ""),
    Formula("mrisk.scenario", 1, "uniform: dV_i = MV_i * s; beta: dV_i = MV_i * beta_i * m with beta_i rounded half-even "
            "to 4 decimals; each dV_i rounded toward -infinity at the currency's ledger scale; a position without a "
            "beta is listed as not in the scenario and never assumed to move with the market",
            "currency (P&L signed)", ("CAP-PORT-11",), ""),
    Formula("expo.issuer", 1, "issuer = the instrument's SEC CIK attribute; instruments without one form the explicit "
            "'unknown issuer' bucket; grouped like expo.bucket_weight", "dimensionless",
            ("CAP-PORT-03",), ""),
    Formula("expo.owner_bucket", 1, "bucket = owner kind (unallocated, buffer, session, protected, exception, outside); "
            "grouped like expo.bucket_weight", "dimensionless", ("CAP-PORT-03",), "ADR-0006"),
]

FORMULAS: dict[str, Formula] = {f.formula_id: f for f in _ALL}


def ref(formula_id: str) -> str:
    """``id@version`` of a registered formula; unknown ids raise (no unregistered analytic is published)."""
    return FORMULAS[formula_id].ref


def describe(*formula_ids: str) -> list[dict]:
    return [FORMULAS[f].to_json() for f in formula_ids]
