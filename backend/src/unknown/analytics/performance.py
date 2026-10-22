"""Performance measurement (CAP-PORT-04) and ex-post risk statistics (CAP-PORT-05).

Conventions (each result states the ones it used):
* Sub-period returns link consecutive *complete* valuations (fresh NAV). External flows
  between them come from the cumulative flow recorded by each snapshot, so a flow is
  counted exactly once even across gaps or revisions. ``start_of_day`` adds the flow to
  BMV; ``end_of_day`` removes it from EMV. Only external flows are flows: P&L never is.
* TWR = prod(1 + r) - 1. Returns shorter than one year are never annualized (GIPS 2.A.12);
  annualization uses 365-day years, the same basis as the IRR.
* Drawdown is computed on the flow-adjusted unit-value series (U_0 = 1000), so a deposit
  can never reset a high-water mark or hide a loss (book Ch. 6).
* Statistics are computed on the TWR sub-period returns only. Standard deviations use
  n-1. Annualization multiplies the mean by n and the standard deviation by sqrt(n),
  n = periods per year (252 by default; 360 is the IBKR-compatible daily preset).
* Returns are statistical floats computed from exact decimal NAVs; nothing here posts money.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from ..domain.fixed import Fixed
from .common import finite, frac, lineage

CONVENTIONS = ("start_of_day", "end_of_day")
UNIT_BASE = 1000.0


@dataclass(frozen=True)
class SubPeriod:
    start_date: str
    end_date: str
    bmv: Fixed
    emv: Fixed
    flow: Fixed
    r: float | None
    convention: str
    skipped_dates: tuple[str, ...]
    reason: str | None = None

    def to_json(self) -> dict:
        return {"start": self.start_date, "end": self.end_date, "bmv": self.bmv.to_str(), "emv": self.emv.to_str(),
                "external_flow": self.flow.to_str(), "return": finite(self.r), "convention": self.convention,
                "gap_skipped_incomplete_dates": list(self.skipped_dates), "reason": self.reason}


def subperiod_return(bmv: Fixed, emv: Fixed, flow: Fixed, convention: str) -> tuple[float | None, str | None]:
    """r for one sub-period under a flow-timing convention (formulas perf.return.*)."""
    if convention == "start_of_day":
        den = frac(bmv) + frac(flow)
        if den <= 0:
            return None, "BMV + CF is not positive: the sub-period return is undefined"
        return float(frac(emv) / den - 1), None
    if convention == "end_of_day":
        if frac(bmv) <= 0:
            return None, "BMV is not positive: the sub-period return is undefined under the end-of-day convention"
        return float((frac(emv) - frac(flow)) / frac(bmv) - 1), None
    raise ValueError(f"convention must be one of {CONVENTIONS}")


def subperiods(points, convention: str = "start_of_day") -> list[SubPeriod]:
    """Link consecutive complete valuation points (snapshots.ValuationPoint-like: date, nav, cum_flow)."""
    if convention not in CONVENTIONS:
        raise ValueError(f"convention must be one of {CONVENTIONS}")
    out: list[SubPeriod] = []
    prev = None
    skipped: list[str] = []
    for p in sorted(points, key=lambda x: (x.valuation_date, x.as_of_ns)):
        if p.nav is None:
            if prev is not None:
                skipped.append(p.valuation_date)
            continue
        if prev is not None:
            flow = p.cum_flow - prev.cum_flow
            r, reason = subperiod_return(prev.nav, p.nav, flow, convention)
            out.append(SubPeriod(prev.valuation_date, p.valuation_date, prev.nav, p.nav, flow, r, convention, tuple(skipped), reason))
        prev = p
        skipped = []
    return out


def link(returns: list[float]) -> float:
    total = 1.0
    for r in returns:
        total *= 1.0 + r
    return total - 1.0


def _no_capital_at_risk(s: SubPeriod) -> bool:
    """Nothing invested during the sub-period and nothing at its end: BMV + CF = 0 and EMV = 0 (start of day), or
    BMV = 0 and EMV - CF = 0 (end of day). Its return is undefined only as 0/0; economically it is 0."""
    if s.convention == "start_of_day":
        return frac(s.bmv) + frac(s.flow) == 0 and s.emv.is_zero()
    return s.bmv.is_zero() and frac(s.emv) - frac(s.flow) == 0


def chain(subs: list[SubPeriod]) -> tuple[list[SubPeriod], list[dict]]:
    """Defined sub-periods used for TWR. Undefined ones before the first defined sub-period are pre-inception (an
    empty bucket before funding); undefined ones after the last are post-termination (a bucket emptied by an outflow,
    e.g. a closed session under the start-of-day convention). Between two defined ones, a sub-period with no capital
    at risk (the bucket was emptied by an outflow and later refunded) links as a 0 return and is labelled (review fix:
    it used to null the TWR of the whole history, e.g. 'unallocated' after funding a session with all of it, or the
    system after a full deauthorization and re-authorization). Any other undefined one breaks the chain and is
    returned as a problem."""
    defined = [k for k, s in enumerate(subs) if s.r is not None]
    if not defined:
        return [], []
    first, last = defined[0], defined[-1]
    linked, problems = [], []
    for s in subs[first:last + 1]:
        if s.r is not None:
            linked.append(s)
        elif _no_capital_at_risk(s):
            linked.append(dataclasses.replace(s, r=0.0, reason="no capital at risk in this sub-period: linked as a 0 return"))
        else:
            problems.append({"start": s.start_date, "end": s.end_date, "reason": s.reason})
    return ([] if problems else linked), problems


def unit_values(subs: list[SubPeriod], base: float = UNIT_BASE) -> list[tuple[str, float]]:
    if not subs:
        return []
    series = [(subs[0].start_date, base)]
    u = base
    for s in subs:
        u *= 1.0 + s.r
        series.append((s.end_date, u))
    return series


def drawdown(series: list[tuple[str, float]]) -> dict:
    """MDD on a unit-value series: D_t = 1 - U_t / max_{s<=t} U_s (formula perf.drawdown@1)."""
    if not series:
        return {"max_drawdown": None, "reason": "no complete sub-periods"}
    peak_v, peak_d = series[0][1], series[0][0]
    mdd, mdd_peak, mdd_trough = 0.0, None, None
    for d, u in series:
        if u > peak_v:
            peak_v, peak_d = u, d
        dd = 1.0 - u / peak_v
        if dd > mdd:
            mdd, mdd_peak, mdd_trough = dd, peak_d, d
    recovery = None
    if mdd_peak is not None:
        level = dict(series)[mdd_peak]
        after = [(d, u) for d, u in series if d > mdd_trough and u >= level]
        recovery = after[0][0] if after else "ongoing"
    current_peak = max(u for _, u in series)
    return {"max_drawdown": mdd, "peak_date": mdd_peak, "trough_date": mdd_trough, "recovery_date": recovery,
            "current_drawdown": 1.0 - series[-1][1] / current_peak}


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def at_least_one_year(start: str, end: str) -> bool:
    """True when [start, end] spans at least one calendar year: end >= start + 1 year (29 Feb maps to 28 Feb).

    A day count cannot decide this: 365 days is a full year from 2026-01-01 but one day short of a year inside 2028,
    and GIPS 2.A.12 forbids annualizing any period shorter than one year (review fix)."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    try:
        anniversary = a.replace(year=a.year + 1)
    except ValueError:  # 29 February
        anniversary = a.replace(year=a.year + 1, day=28)
    return b >= anniversary


def annualize(total_return: float, days: int, *, full_year: bool | None = None) -> tuple[float | None, str | None]:
    """(1+R)^(365/days) - 1 only for periods of at least one year (GIPS 2.A.12). ``full_year`` comes from
    ``at_least_one_year`` when the dates are known; otherwise ``days >= 365`` is used."""
    if not (full_year if full_year is not None else days >= 365):
        return None, f"period of {days} days is shorter than one year: not annualized (GIPS 2.A.12)"
    return (1.0 + total_return) ** (365.0 / days) - 1.0, None


def _q(x) -> Fraction:
    return frac(x) if isinstance(x, Fixed) else Fraction(x)


def modified_dietz_totals(bmv, emv, net_flow, weighted_flow) -> float | None:
    """R = (EMV - BMV - sum CF) / (BMV + sum W_i CF_i) from the two flow totals (formula perf.modified_dietz@1)."""
    den = _q(bmv) + _q(weighted_flow)
    if den <= 0:
        return None
    return float((_q(emv) - _q(bmv) - _q(net_flow)) / den)


def modified_dietz(bmv: Fixed | Fraction, emv: Fixed | Fraction, flows: list[tuple[int, Fixed | Fraction]],
                   calendar_days: int) -> float | None:
    """Modified Dietz with day weights W_i = (CD - D_i)/CD, D_i = day offset of the flow from the period start."""
    if calendar_days <= 0:
        return None
    net = sum((_q(a) for _, a in flows), Fraction(0))
    weighted = sum((Fraction(calendar_days - d, calendar_days) * _q(a) for d, a in flows), Fraction(0))
    return modified_dietz_totals(bmv, emv, net, weighted)


def irr(bmv: Fixed | Fraction, emv: Fixed | Fraction, flows: list[tuple[int, Fixed | Fraction]], total_days: int,
        *, full_year: bool | None = None) -> dict:
    """Annualized money-weighted return solving -BMV(1+r)^(T/365) - sum CF_i (1+r)^((T-t_i)/365) + EMV = 0.

    ``full_year`` says whether the period spans at least one calendar year (``at_least_one_year``); without dates,
    ``total_days >= 365`` is used. Shorter periods show only the period MWR (GIPS 2.A.12)."""
    def q(x):
        return float(frac(x)) if isinstance(x, Fixed) else float(x)
    if total_days <= 0:
        return {"irr_annualized": None, "reason": "period has no length"}
    b, e = q(bmv), q(emv)
    cfs = [(t, q(a)) for t, a in flows]
    T = total_days

    def f(r: float) -> float:
        g = 1.0 + r
        return -b * g ** (T / 365.0) - sum(a * g ** ((T - t) / 365.0) for t, a in cfs) + e

    grid = [-0.999999] + [x / 100.0 for x in range(-99, 100)] + [1.5, 2, 3, 5, 10, 30, 100, 1000, 1e4, 1e6]
    values = []
    for r in grid:
        try:
            values.append((r, f(r)))
        except OverflowError:
            break
    # Distinct roots: grid points that solve the equation exactly, plus strict sign changes between adjacent grid
    # points. A root lying exactly on a grid point (r = 0 for a period with no gain or loss) is counted once, not as
    # the two half-open brackets around it (review fix: a flat period was reported as 'not unique').
    exact = [r for r, fr in values if fr == 0]
    brackets = [(a, b2) for (a, fa), (b2, fb) in zip(values, values[1:]) if fa != 0 and fb != 0 and (fa > 0) != (fb > 0)]
    roots = len(exact) + len(brackets)
    if roots == 0:
        return {"irr_annualized": None, "reason": "no rate in (-99.9999%, 1e8%) solves the cash-flow equation"}
    if roots > 1:
        return {"irr_annualized": None, "reason": f"{roots} sign changes: the money-weighted return is not unique"}
    if exact:
        r = exact[0]
    else:
        lo, hi = brackets[0]
        flo = f(lo)
        r = None
        for _ in range(200):
            mid = (lo + hi) / 2.0
            fm = f(mid)
            if fm == 0:
                r = mid
                break
            if hi - lo < 1e-15:
                break
            if (fm > 0) == (flo > 0):
                lo, flo = mid, fm
            else:
                hi = mid
        if r is None:
            r = (lo + hi) / 2.0
    period = (1.0 + r) ** (T / 365.0) - 1.0
    if not (full_year if full_year is not None else T >= 365):
        return {"irr_annualized": None, "period_mwr": period, "days": T,
                "reason": f"period of {T} days is shorter than one year: only the period MWR is shown (GIPS 2.A.12)"}
    return {"irr_annualized": r, "period_mwr": period, "days": T, "reason": None}


# ------------------------------------------------------------------ statistics
def mean(xs: list[float]) -> float:
    return math.fsum(xs) / len(xs)


def sample_std(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def autocorrelation(xs: list[float], k: int) -> float | None:
    n = len(xs)
    if k >= n:
        return None
    m = mean(xs)
    den = math.fsum((x - m) ** 2 for x in xs)
    if den == 0:
        return None
    return math.fsum((xs[t] - m) * (xs[t + k] - m) for t in range(n - k)) / den


def n_eff(xs: list[float]) -> dict:
    """Effective sample size under serial correlation (formula stats.n_eff@1)."""
    n = len(xs)
    if n < 4:
        return {"n_eff": float(n), "lags_used": 0, "note": "too few observations to estimate autocorrelation"}
    K = min(20, n // 4)
    s, used, rhos = 0.0, 0, []
    for k in range(1, K + 1):
        rho = autocorrelation(xs, k)
        if rho is None or rho <= 0:
            break
        rhos.append(rho)
        s += (1 - k / n) * rho
        used = k
    ne = n / (1 + 2 * s)
    return {"n_eff": max(1.0, min(float(n), ne)), "lags_used": used, "autocorrelations": rhos,
            "note": "positive serial correlation reduces the information in n returns; standard errors use n_eff"}


def sharpe_ratio(mean_per_period: float, std_per_period: float, periods_per_year: int, rf_annual: float = 0.0) -> float | None:
    """(mean * n - Rf_ann) / (std * sqrt(n)) (formula stats.sharpe@1); None when the volatility is zero."""
    if std_per_period <= 0:
        return None
    return (mean_per_period * periods_per_year - rf_annual) / (std_per_period * math.sqrt(periods_per_year))


def risk_statistics(returns: list[float], *, periods_per_year: int = 252, rf_annual: float | None = None,
                    rf_series_id: str | None = None, mar_annual: float | None = None, min_obs: int = 20) -> dict:
    """Volatility, Sharpe, Sortino and their sampling caveats (CAP-PORT-05)."""
    n = len(returns)
    out: dict = {"n": n, "periods_per_year": periods_per_year, "std_normalisation": "n-1",
                 "risk_free": {"annual": rf_annual if rf_annual is not None else 0.0,
                               "series_id": rf_series_id or ("caller-supplied constant rate; no series id given"
                                                             if rf_annual else
                                                             "none configured: Rf = 0 (Sharpe is excess over zero)")},
                 "min_obs_for_ratios": min_obs}
    if n < 2:
        out["insufficient_data"] = f"{n} sub-period returns: at least 2 are needed for a standard deviation"
        return out
    m = mean(returns)
    sd = sample_std(returns)
    out.update(mean_return_per_period=m, std_per_period=sd, volatility_annualized=sd * math.sqrt(periods_per_year),
               positive_periods=sum(1 for r in returns if r > 0), negative_periods=sum(1 for r in returns if r < 0),
               best_period=max(returns), worst_period=min(returns))
    ne = n_eff(returns)
    out["n_eff"] = ne
    if n < min_obs:
        out["sharpe"] = None
        out["sortino"] = None
        out["insufficient_data"] = f"{n} < {min_obs} sub-period returns: ratios not presented"
        return out
    rf = rf_annual or 0.0
    mar = rf if mar_annual is None else mar_annual
    mar_p = mar / periods_per_year
    ann_mean = m * periods_per_year
    out["sharpe"] = finite(sharpe_ratio(m, sd, periods_per_year, rf))
    if sd == 0:
        out["sharpe_reason"] = "zero volatility"
    dd = math.sqrt(math.fsum(min(0.0, r - mar_p) ** 2 for r in returns) / n)
    out["downside_deviation_per_period"] = dd
    out["sortino"] = finite((ann_mean - mar) / (dd * math.sqrt(periods_per_year))) if dd > 0 else None
    if dd == 0:
        out["sortino_reason"] = "no returns below the minimum acceptable return"
    out["mar_annual"] = mar
    neg = [r for r in returns if r < 0]
    sneg = sample_std(neg)
    out["sortino_ibkr_variant"] = finite((ann_mean - mar) / (sneg * math.sqrt(periods_per_year))) if sneg else None
    if out["sharpe"] is not None:
        sr_p = (m - rf / periods_per_year) / sd
        se_iid = math.sqrt((1 + sr_p ** 2 / 2) / n) * math.sqrt(periods_per_year)
        se_eff = math.sqrt((1 + sr_p ** 2 / 2) / ne["n_eff"]) * math.sqrt(periods_per_year)
        out["sharpe_standard_error"] = {"iid": se_iid, "n_eff": se_eff,
                                        "note": "approximate (Lo 2002, iid normal returns); a Sharpe ratio within about "
                                                "two standard errors of zero is not evidence of skill"}
    return out


def relative_statistics(rp: list[float], rb: list[float], *, periods_per_year: int = 252,
                        rf_annual: float = 0.0) -> dict:
    """Beta, correlation, tracking error, information ratio, alpha against a benchmark (formula stats.relative@1)."""
    if len(rp) != len(rb):
        raise ValueError("portfolio and benchmark series must be aligned")
    n = len(rp)
    if n < 2:
        return {"n": n, "insufficient_data": "at least 2 aligned returns are needed"}
    mp, mb = mean(rp), mean(rb)
    cov = math.fsum((a - mp) * (b - mb) for a, b in zip(rp, rb)) / (n - 1)
    vb = math.fsum((b - mb) ** 2 for b in rb) / (n - 1)
    vp = math.fsum((a - mp) ** 2 for a in rp) / (n - 1)
    diff = [a - b for a, b in zip(rp, rb)]
    te = sample_std(diff) * math.sqrt(periods_per_year)
    beta = cov / vb if vb > 0 else None
    out = {"n": n, "periods_per_year": periods_per_year, "beta": beta,
           "correlation": cov / math.sqrt(vp * vb) if vp > 0 and vb > 0 else None,
           "tracking_error": te, "information_ratio": (mean(diff) * periods_per_year / te) if te > 0 else None,
           "normalisation": "n-1 in covariance and variance"}
    if beta is not None:
        out["alpha_annual"] = (mp * periods_per_year - rf_annual) - beta * (mb * periods_per_year - rf_annual)
    return out


# ------------------------------------------------------------------ report
def flow_schedule(points, start_date: str, end_date: str, convention: str) -> list[tuple[int, Fixed]]:
    """Net flows between consecutive snapshots, dated at the earlier snapshot (start-of-day) or the later one
    (end-of-day), as day offsets from ``start_date``. Uses cumulative flows, so incomplete days still count."""
    pts = [p for p in sorted(points, key=lambda x: (x.valuation_date, x.as_of_ns)) if start_date <= p.valuation_date <= end_date]
    out = []
    for a, b in zip(pts, pts[1:]):
        f = b.cum_flow - a.cum_flow
        if f.is_zero():
            continue
        d = a.valuation_date if convention == "start_of_day" else b.valuation_date
        out.append((days_between(start_date, d), f))
    return out


def performance_report(points, *, scope: str, currency_code: str, as_of_ns: int, convention: str = "start_of_day",
                       periods_per_year: int = 252, rf_annual: float | None = None, rf_series_id: str | None = None,
                       min_obs: int = 20, nav_field: str = "nav_fresh") -> dict:
    """TWR, unit value, drawdown, Modified Dietz, IRR and statistics from point-in-time valuation points."""
    subs = subperiods(points, convention)
    used, problems = chain(subs)
    fixture = any(p.fixture for p in points)
    base = {"kind": "performance", "scope": scope, "currency": currency_code, "as_of_ns": as_of_ns, "convention": convention,
            "valuation_points": [p.to_json() for p in points], "fixture_data": fixture,
            "fixture_note": "computed from FIXTURE quotes: test data, not market data" if fixture else None}
    lin = lineage(["perf.return." + convention, "perf.twr", "perf.unit_value", "perf.drawdown", "perf.modified_dietz",
                   "perf.irr", "perf.annualize", "stats.volatility", "stats.sharpe", "stats.sortino", "stats.n_eff",
                   "stats.sharpe_se", "port.external_flow", "port.nav"],
                  inputs={"snapshot_runs": [p.run_id for p in points], "nav_field": nav_field},
                  units="returns as decimals (0.01 = 1%); money as decimal strings",
                  source_dates={"first": points[0].valuation_date if points else None,
                                "last": points[-1].valuation_date if points else None, "as_of_ns": as_of_ns},
                  missing=[{"date": p.valuation_date, "reason": f"valuation {p.status}"} for p in points if p.nav is None],
                  notes=["only external flows are flows; P&L is not", "periods under one year are not annualized"])
    if problems:
        return {**base, "twr": None, "reason": "a sub-period return is undefined inside the period; the chain is broken",
                "problems": problems, "subperiods": [s.to_json() for s in subs], "lineage": lin}
    if not used:
        return {**base, "twr": None, "reason": "fewer than two complete valuations with a positive base",
                "subperiods": [s.to_json() for s in subs], "lineage": lin}
    rets = [s.r for s in used]
    linked = {(s.start_date, s.end_date): s for s in used}
    spanning = sum(1 for s in used if s.skipped_dates)
    total = link(rets)
    start, end = used[0].start_date, used[-1].end_date
    days = days_between(start, end)
    full_year = at_least_one_year(start, end)
    ann, ann_reason = annualize(total, days, full_year=full_year)
    uv = unit_values(used)
    by_date = {p.valuation_date: p for p in points if p.nav is not None}
    flows = flow_schedule(points, start, end, convention)
    md = modified_dietz(by_date[start].nav, by_date[end].nav, flows, days)
    mwr = irr(by_date[start].nav, by_date[end].nav, flows, days, full_year=full_year)
    return {**base, "period": {"start": start, "end": end, "calendar_days": days},
            "twr": total, "twr_annualized": ann, "twr_annualized_reason": ann_reason,
            "unit_values": [{"date": d, "unit_value": u} for d, u in uv],
            "drawdown": drawdown(uv),
            "modified_dietz": md, "money_weighted": mwr,
            "external_flows": [{"day_offset": d, "amount": a.to_str()} for d, a in flows],
            "statistics": {**risk_statistics(rets, periods_per_year=periods_per_year, rf_annual=rf_annual,
                                             rf_series_id=rf_series_id, min_obs=min_obs),
                           "subperiods_spanning_gaps": spanning,
                           "gap_note": (f"{spanning} sub-period return(s) span skipped incomplete valuations and count as "
                                        f"one period each although they carry several days of variance, which inflates "
                                        f"the per-period volatility") if spanning else None},
            "subperiods": [linked.get((s.start_date, s.end_date), s).to_json() for s in subs], "lineage": lin}
