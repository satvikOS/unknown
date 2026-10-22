"""Long-only allocation baselines, shrinkage covariance, eq. 6.2 mean-variance and trade-list proposals
(CAP-PORT-13; book Ch. 6 'returns, exposures and concentration', eq. 6.2 and eq. 6.4).

* Baselines are always computed: equal weight and inverse volatility with floors and caps,
  both after a cash reserve. A complicated optimizer must beat them in research before
  promotion (Ch. 6, Ch. 20).
* Covariance is the sample covariance shrunk toward a scaled identity with the Ledoit-Wolf
  (2004) intensity, which is disclosed together with the condition number before and after.
  Shrinkage guarantees a symmetric positive semidefinite matrix.
* Mean-variance (eq. 6.2): minimise 0.5 w'Sigma w - lambda mu'w + eta ||w - w_old||_1 subject
  to 0 <= w_i <= u_i and sum w = 1 - c_reserve. Solved by pairwise coordinate descent with
  maximal-violating-pair selection (each pairwise step minimises a convex piecewise quadratic
  exactly); it stops when the KKT violation is below 1e-12. A turnover limit
  ||w - w_old||_1 <= tau is enforced by raising eta, found by bisection, because the L1
  constraint's Lagrangian is exactly the eta term. The KKT residual is reported.
  Infeasible constraint sets return the names of the conflicting constraints, never a
  silently relaxed solution.
* Expected returns mu must come from an approved, versioned research artifact (named in
  ``mu_source``); without one, the lambda term is zero and the result is labelled
  minimum variance with turnover penalty.
* Weights become whole-lot target quantities rounded down (eq. 6.4 rule: zero quantity means
  no trade); every constraint is re-verified after rounding and estimated fees. The output is
  a PROPOSAL: it is never an order and must pass the what-if service and the independent risk
  gate as new intents. No leverage and no shorting are ever produced.

Weights, covariances and objective values are statistical floats; quantities, notionals and
fees in the trade list are exact."""
from __future__ import annotations

import math
from fractions import Fraction

from ..domain.fixed import Fixed, Rounding
from ..domain.money import Money, currency
from ..risk import fees as fee_model
from .common import Scope, frac, lineage, parse_fraction
from .linalg import condition_number, is_symmetric, matvec, quad, symmetric_eigenvalues

PROPOSAL = "PROPOSAL ONLY: not an order; convert through the what-if service and the independent risk gate"


# ------------------------------------------------------------------ helpers
def _budget(cash_reserve) -> float:
    c = float(parse_fraction(cash_reserve, "cash_reserve"))
    if not 0 <= c < 1:
        raise ValueError("cash_reserve must be in [0, 1)")
    return 1.0 - c


def project_capped_simplex(v: list[float], caps: list[float], budget: float) -> list[float] | None:
    """argmin ||w - v|| s.t. 0 <= w_i <= caps_i, sum w = budget (bisection on the shift); None if infeasible."""
    if budget < 0 or math.fsum(caps) < budget - 1e-12:
        return None
    lo, hi = min(v) - max(caps) - 1.0, max(v) + 1.0
    for _ in range(200):
        theta = (lo + hi) / 2
        s = math.fsum(min(max(x - theta, 0.0), u) for x, u in zip(v, caps))
        if s > budget:
            lo = theta
        else:
            hi = theta
    theta = (lo + hi) / 2
    return [min(max(x - theta, 0.0), u) for x, u in zip(v, caps)]


def equal_weight(ids: list[str], *, cash_reserve="0") -> dict:
    b = _budget(cash_reserve)
    if not ids:
        return {"method": "equal_weight", "status": "empty_universe", "weights": {}}
    w = b / len(ids)
    return {"method": "equal_weight", "status": "ok", "weights": {i: w for i in ids}, "cash_weight": 1.0 - b}


def inverse_volatility(vols: dict[str, float], *, cash_reserve="0", floor="0", cap="1") -> dict:
    """w_i proportional to 1/sigma_i, scaled to the budget, then floors and caps applied with proportional
    redistribution until stable, i.e. w_i = clip(k / sigma_i, floor, cap) with k solved exactly so that the weights
    sum to the budget (formula alloc.inverse_vol@2)."""
    b = _budget(cash_reserve)
    fl, cp = float(parse_fraction(floor, "floor")), float(parse_fraction(cap, "cap"))
    ids = sorted(vols)
    excluded = [{"instrument_id": i, "reason": "volatility unavailable or not positive"} for i in ids if not vols[i] or vols[i] <= 0]
    ids = [i for i in ids if vols[i] and vols[i] > 0]
    if not ids:
        return {"method": "inverse_volatility", "status": "empty_universe", "weights": {}, "excluded": excluded}
    conflicts = []
    if fl * len(ids) > b + 1e-12:
        conflicts.append(f"floor {fl} x {len(ids)} names exceeds the invested budget {b}")
    if cp * len(ids) < b - 1e-12:
        conflicts.append(f"cap {cp} x {len(ids)} names is below the invested budget {b}")
    if fl > cp:
        conflicts.append("floor exceeds cap")
    if conflicts:
        return {"method": "inverse_volatility", "status": "infeasible", "conflicting_constraints": conflicts, "weights": {},
                "excluded": excluded}
    raw = {i: 1 / Fraction(vols[i]) for i in ids}            # exact rationals of the float volatilities
    budget, lo_w, hi_w = Fraction(b), Fraction(fl), Fraction(cp)

    def total(k: Fraction) -> Fraction:
        return sum((min(hi_w, max(lo_w, k * raw[i])) for i in ids), Fraction(0))
    # w_i(k) = clip(k / sigma_i, floor, cap): the fixed point of "scale, clip, redistribute among unclipped names".
    # sum_i w_i(k) is continuous, piecewise linear and non-decreasing in k, from n*floor (k -> 0) to n*cap; the
    # feasibility checks above guarantee it crosses the budget, and the crossing is found exactly between the
    # breakpoints floor/raw_i and cap/raw_i (review fix: the earlier fix-and-repeat loop could freeze a name at its
    # floor, then cap the rest, and return weights summing to less than the budget with status 'ok').
    knots = sorted({Fraction(0)} | {lo_w / raw[i] for i in ids} | {hi_w / raw[i] for i in ids})
    k = None
    for k0, k1 in zip(knots, knots[1:]):
        s0, s1 = total(k0), total(k1)
        if s0 <= budget <= s1:
            k = k1 if s1 == s0 else k0 + (budget - s0) * (k1 - k0) / (s1 - s0)
            break
    if k is None:
        k = knots[-1]
    exact = {i: min(hi_w, max(lo_w, k * raw[i])) for i in ids}
    fixed = {i: float(exact[i]) for i in ids}
    binding = sorted(i for i in ids if exact[i] in (hi_w, lo_w))
    return {"method": "inverse_volatility", "status": "ok", "weights": fixed, "cash_weight": 1.0 - b,
            "binding": binding, "scale_k": float(k), "budget_residual": float(sum(exact.values(), Fraction(0)) - budget),
            "excluded": excluded, "floor": fl, "cap": cp}


# ------------------------------------------------------------------ covariance
def sample_covariance(rows: list[list[float]], *, ddof: int = 1) -> list[list[float]]:
    n, p = len(rows), len(rows[0])
    means = [math.fsum(r[j] for r in rows) / n for j in range(p)]
    return [[math.fsum((r[i] - means[i]) * (r[j] - means[j]) for r in rows) / (n - ddof) for j in range(p)] for i in range(p)]


def ledoit_wolf(rows: list[list[float]]) -> dict:
    """Shrinkage toward m*I with the Ledoit-Wolf (2004) optimal intensity (formula alloc.ledoit_wolf@1)."""
    n, p = len(rows), len(rows[0])
    if n < 2:
        raise ValueError("at least two observations are needed")
    means = [math.fsum(r[j] for r in rows) / n for j in range(p)]
    x = [[r[j] - means[j] for j in range(p)] for r in rows]
    s = [[math.fsum(x[t][i] * x[t][j] for t in range(n)) / n for j in range(p)] for i in range(p)]
    m = math.fsum(s[i][i] for i in range(p)) / p
    d2 = math.fsum((s[i][j] - (m if i == j else 0.0)) ** 2 for i in range(p) for j in range(p)) / p
    b2bar = math.fsum(math.fsum((x[t][i] * x[t][j] - s[i][j]) ** 2 for i in range(p) for j in range(p)) / p
                      for t in range(n)) / n ** 2
    b2 = min(b2bar, d2)
    delta = b2 / d2 if d2 > 0 else 1.0
    shrunk = [[delta * (m if i == j else 0.0) + (1 - delta) * s[i][j] for j in range(p)] for i in range(p)]
    e_before = symmetric_eigenvalues(s)
    e_after = symmetric_eigenvalues(shrunk)
    return {"sigma": shrunk, "sample": s, "intensity": delta, "target": "scaled identity m*I", "m": m,
            "normalisation": "1/n (Ledoit-Wolf)", "observations": n,
            "min_eigenvalue_before": e_before[0], "min_eigenvalue_after": e_after[0],
            "condition_number_before": condition_number(e_before), "condition_number_after": condition_number(e_after),
            "symmetric": is_symmetric(shrunk), "psd": e_after[0] >= -1e-15}


# ------------------------------------------------------------------ eq. 6.2 solver
def _objective(sigma, mu, lam, eta, w, w_old) -> float:
    return 0.5 * quad(sigma, w) - lam * math.fsum(a * b for a, b in zip(mu, w)) + eta * math.fsum(abs(a - b) for a, b in zip(w, w_old))


def _dabs(x: float, t: float) -> float:
    """|x + t| - |x| without cancellation when the sign does not change."""
    if x > 0 and x + t >= 0:
        return t
    if x < 0 and x + t <= 0:
        return -t
    return abs(x + t) - abs(x)


def _pair_step(a: float, b: float, eta: float, wi: float, wj: float, oi: float, oj: float, lo: float, hi: float) -> float:
    """argmin over t in [lo, hi] of 0.5 a t^2 + b t + eta (|wi + t - oi| + |wj - t - oj|), exactly: the minimum of a
    convex piecewise quadratic lies at an end point, a kink, or a stationary point of one of its four pieces.
    Candidates are compared by the change from t = 0, which keeps tiny improvements visible."""
    di, dj = wi - oi, wj - oj

    def dphi(t: float) -> float:
        return 0.5 * a * t * t + b * t + eta * (_dabs(di, t) + _dabs(dj, -t))
    cands = {0.0, lo, hi} if lo <= 0.0 <= hi else {lo, hi}
    for k in (-di, dj):
        if lo <= k <= hi:
            cands.add(k)
    if a > 0:
        for si in (-1.0, 1.0):
            for sj in (-1.0, 1.0):
                cands.add(min(hi, max(lo, -(b + eta * si - eta * sj) / a)))
    return min(cands, key=lambda t: (dphi(t), abs(t)))


def mean_variance(sigma: list[list[float]], mu: list[float] | None = None, *, lam: float = 0.0, eta: float = 0.0,
                  w_old: list[float] | None = None, cash_reserve="0", caps: list[float] | None = None,
                  tau: float | None = None, tol: float = 1e-12, max_sweeps: int = 200_000,
                  names: list[str] | None = None) -> dict:
    """Book eq. 6.2 with long-only, cash-reserve and position-cap constraints (formula alloc.mean_variance_l1@1)."""
    p = len(sigma)
    for label, value, ok in (("lambda", lam, lam is not None and math.isfinite(lam) and lam >= 0),
                             ("eta", eta, eta is not None and math.isfinite(eta) and eta >= 0),
                             ("tau", tau, tau is None or (math.isfinite(tau) and tau >= 0))):
        if not ok:
            # A negative or non-finite eta/lambda makes eq. 6.2 non-convex or undefined; the solver would return a
            # meaningless 'optimal' (review fix: the route passed any float through).
            raise ValueError(f"{label} must be a finite number >= 0, got {value!r}")
    names = names or [str(i) for i in range(p)]
    mu = mu or [0.0] * p
    w_old = w_old or [0.0] * p
    caps = caps or [1.0] * p
    b = _budget(cash_reserve)
    conflicts = []
    if math.fsum(caps) < b - 1e-12:
        conflicts.append(f"position caps sum to {math.fsum(caps)} < invested budget {b} (1 - cash_reserve)")
    if any(u < 0 for u in caps):
        conflicts.append("a position cap is negative")
    if conflicts:
        return {"status": "infeasible", "conflicting_constraints": conflicts, "weights": None}
    if tau is not None:
        base = _solve(sigma, mu, lam, eta, w_old, b, caps, tol, max_sweeps)
        l1 = math.fsum(abs(x - y) for x, y in zip(base["w"], w_old))
        if l1 <= tau + 1e-12:
            res, extra = base, 0.0
        else:
            hi = max(1e-8, eta)
            found = None
            for _ in range(80):
                r = _solve(sigma, mu, lam, eta + hi, w_old, b, caps, tol, max_sweeps)
                if math.fsum(abs(x - y) for x, y in zip(r["w"], w_old)) <= tau + 1e-12:
                    found = r
                    break
                hi *= 2
            if found is None:
                floor_l1 = math.fsum(abs(x - y) for x, y in zip(project_capped_simplex(w_old, caps, b), w_old))
                return {"status": "infeasible", "weights": None,
                        "conflicting_constraints": [f"turnover limit tau={tau} vs budget and caps (the nearest feasible "
                                                    f"portfolio needs L1 turnover {floor_l1:.6g})"]}
            lo = 0.0
            res = found
            for _ in range(60):
                mid = (lo + hi) / 2
                r = _solve(sigma, mu, lam, eta + mid, w_old, b, caps, tol, max_sweeps)
                if math.fsum(abs(x - y) for x, y in zip(r["w"], w_old)) <= tau + 1e-12:
                    hi, res = mid, r
                else:
                    lo = mid
            extra = hi
    else:
        res, extra = _solve(sigma, mu, lam, eta, w_old, b, caps, tol, max_sweeps), 0.0
    w = res["w"]
    var = quad(sigma, w)
    return {"status": res["status"], "weights": dict(zip(names, w)), "cash_weight": 1.0 - b,
            "objective": _objective(sigma, mu, lam, eta + extra, w, w_old), "portfolio_variance": var,
            "portfolio_volatility": math.sqrt(max(var, 0.0)), "sweeps": res["sweeps"], "kkt_residual": res["kkt"],
            "turnover_l1": math.fsum(abs(x - y) for x, y in zip(w, w_old)),
            "turnover_half_l1": 0.5 * math.fsum(abs(x - y) for x, y in zip(w, w_old)),
            "effective_eta": eta + extra, "turnover_limit": tau,
            "turnover_note": "tau enforced by raising eta (Lagrange multiplier of the L1 constraint)" if extra else None}


def _solve(sigma, mu, lam, eta, w_old, b, caps, tol, max_iter) -> dict:
    """Maximal-violating-pair coordinate descent (SMO working-set selection): each step moves weight from the
    coordinate whose decrease helps most to the one whose increase costs least, by the exact pairwise minimiser.
    Stops when the KKT violation (the best directional derivative of any feasible pair move) is below ``tol``."""
    p = len(sigma)
    feasible = abs(math.fsum(w_old) - b) <= 1e-12 and all(0 <= x <= u for x, u in zip(w_old, caps))
    w = list(w_old if feasible else project_capped_simplex(w_old, caps, b))
    g = [gi - lam * m for gi, m in zip(matvec(sigma, w), mu)]
    eps = 1e-12
    status, it = "max_iterations_reached", 0
    for it in range(1, max_iter + 1):
        best_i, best_up = None, math.inf      # cheapest coordinate to increase (right derivative)
        best_j, best_down = None, -math.inf   # most valuable coordinate to decrease (minus left derivative)
        for k in range(p):
            dev = w[k] - w_old[k]
            if w[k] < caps[k] - eps:
                up = g[k] + (eta if dev >= -eps else -eta)
                if up < best_up:
                    best_i, best_up = k, up
            if w[k] > eps:
                down = g[k] - (eta if dev <= eps else -eta)
                if down > best_down:
                    best_j, best_down = k, down
        if best_i is None or best_j is None or best_down - best_up <= tol or best_i == best_j:
            status = "optimal"
            break
        i, j = best_i, best_j
        lo, hi = max(-w[i], w[j] - caps[j]), min(caps[i] - w[i], w[j])
        a = sigma[i][i] + sigma[j][j] - 2 * sigma[i][j]
        t = _pair_step(a, g[i] - g[j], eta, w[i], w[j], w_old[i], w_old[j], max(lo, 0.0), hi)
        if t <= 0.0:  # the remaining violation is below floating-point resolution of the objective
            status = "optimal_within_precision"
            break
        w[i] += t
        w[j] -= t
        for k in (i, j):  # snap to bounds and kinks reached within rounding
            for target in (0.0, caps[k], w_old[k]):
                if abs(w[k] - target) <= eps:
                    w[k] = target
        for k in range(p):
            g[k] += t * (sigma[k][i] - sigma[k][j])
    g = [gi - lam * m for gi, m in zip(matvec(sigma, w), mu)]  # recompute: no drift from incremental updates
    return {"w": w, "sweeps": it, "status": status, "kkt": _kkt(g, eta, w, w_old, caps)}


def _kkt(g, eta, w, w_old, caps, eps: float = 1e-12) -> float:
    """max(0, max lower - min upper) over the subgradient intervals (0 at an exact optimum)."""
    lowers, uppers = [], []
    for gi, wi, oi, ui in zip(g, w, w_old, caps):
        if abs(wi - oi) <= eps:
            lo, hi = gi - eta, gi + eta
        else:
            lo = hi = gi + eta * (1 if wi > oi else -1)
        at_zero, at_cap = wi <= eps, wi >= ui - eps
        if not at_zero:
            lowers.append(lo)
        if not at_cap:
            uppers.append(hi)
    if not lowers or not uppers:
        return 0.0
    return max(0.0, max(lowers) - min(uppers))


# ------------------------------------------------------------------ trade list (exact)
def trade_list(target: dict[str, float], *, nav: Fixed, liquid: Fixed, available_cash: Fixed, ccy: str,
               prices: dict[str, Fixed], current_qty: dict[str, Fixed], lot_sizes: dict[str, Fixed],
               multipliers: dict[str, Fixed], fee_model_policy, cash_reserve="0", caps: dict[str, float] | None = None
               ) -> dict:
    """Whole-lot target quantities rounded down, the trades to reach them, and post-trade constraint checks."""
    scale = currency(ccy).ledger_scale
    c = parse_fraction(cash_reserve, "cash_reserve")
    trades, targets, problems = [], {}, []
    buy_need, buy_notional, sell_notional, fees = Fixed(0, scale), Fixed(0, scale), Fixed(0, scale), Fixed(0, scale)
    for iid in sorted(set(target) | set(current_qty)):
        w = target.get(iid, 0.0)
        if w < 0:
            problems.append(f"{iid}: negative target weight refused (long-only)")
            continue
        price = prices.get(iid)
        if price is None or not price.is_positive():
            problems.append(f"{iid}: no positive mark; cannot size")
            continue
        unit = frac(price) * frac(multipliers.get(iid, Fixed(1))) * frac(lot_sizes.get(iid, Fixed(1)))
        lots = math.floor(Fraction(w) * frac(nav) / unit)
        q_target = Fixed(max(lots, 0)) * lot_sizes.get(iid, Fixed(1))
        targets[iid] = q_target
        d = q_target - current_qty.get(iid, Fixed(0))
        if d.is_zero():
            continue
        side = "BUY" if d.is_positive() else "SELL"
        qty = abs(d)
        raw = qty * price * multipliers.get(iid, Fixed(1))
        notional = raw.rescale(scale, Rounding.CEILING if side == "BUY" else Rounding.FLOOR)
        fee = fee_model.estimate(fee_model_policy, qty, Money(ccy, notional)).amount
        env = fee_model.envelope(fee_model_policy, qty, Money(ccy, notional)).amount if side == "BUY" else Fixed(0, scale)
        fees += fee
        if side == "BUY":
            buy_notional += notional
            buy_need += notional + env
        else:
            sell_notional += notional
        trades.append({"instrument_id": iid, "side": side, "quantity": qty.to_str(), "sizing_price": price.to_str(),
                       "estimated_notional": notional.to_str(), "estimated_fee": fee.to_str(),
                       "reservation_estimate": (notional + env).to_str() if side == "BUY" else None,
                       "target_quantity": q_target.to_str()})
    nav_after = nav - fees
    liquid_after = liquid - buy_notional + sell_notional - fees
    checks = []
    reserve_needed = (Fixed(c.numerator) * nav_after).div(Fixed(c.denominator), scale, Rounding.CEILING)
    checks.append({"check": "CASH_RESERVE", "passed": liquid_after >= reserve_needed,
                   "detail": f"liquid after {liquid_after} vs reserve {reserve_needed} ({float(c):.4%} of NAV after fees)"})
    for iid, q in targets.items():
        cap = (caps or {}).get(iid)
        mv = (q * prices[iid] * multipliers.get(iid, Fixed(1))).rescale(scale, Rounding.FLOOR)
        if cap is not None and nav_after.is_positive():
            w_after = float(frac(mv) / frac(nav_after))
            checks.append({"check": f"CAP:{iid}", "passed": w_after <= cap + 1e-12, "detail": f"weight {w_after:.8f} vs cap {cap}"})
        checks.append({"check": f"LONG_ONLY:{iid}", "passed": not q.is_negative(), "detail": q.to_str()})
    funded = buy_need <= available_cash
    checks.append({"check": "FUNDING", "passed": funded,
                   "detail": f"buy reservations {buy_need} vs available settled cash {available_cash}; sale proceeds are "
                             f"receivables and fund nothing until settled"})
    for pr in problems:
        checks.append({"check": "SIZING", "passed": False, "detail": pr})
    ok = all(ch["passed"] for ch in checks)
    return {"label": PROPOSAL, "currency": ccy, "trades": trades, "checks": checks, "constraints_hold": ok,
            "trade_list_usable": ok,
            "sequencing_note": None if funded else "buys exceed available settled cash: sell first and wait for settlement (ch09)",
            "totals": {"buy_notional": buy_notional.to_str(), "sell_notional": sell_notional.to_str(), "fees": fees.to_str(),
                       "nav_after_fees": nav_after.to_str(), "liquid_after": liquid_after.to_str()}}


# ------------------------------------------------------------------ end-to-end proposal
def propose(conn, account_key: str, quotes, store, *, now_ns: int, scope: Scope, method: str = "all",
            cash_reserve="0.05", floor="0", cap="1", lam: float = 0.0, eta: float = 0.0, tau: float | None = None,
            mu: dict[str, float] | None = None, mu_source: str | None = None, lookback: int = 250,
            extra_instruments: list[str] | None = None, min_obs: int = 20) -> dict:
    """Baselines plus the eq. 6.2 optimiser on the scope's universe, and a trade list for a single-session scope."""
    from .holdings import holdings, load_book
    from .performance import sample_std
    from .var import history_as_of, mark_table, returns_from_prices
    if mu is not None and not mu_source:
        raise ValueError("expected returns need mu_source naming an approved, versioned research artifact")
    book = load_book(conn, account_key)
    h = holdings(conn, account_key, quotes, now_ns=now_ns, scope=scope, book=book)
    ccy = book.base_currency
    candidates = sorted({p["instrument_id"] for p in h["positions"] if p["market_value"] is not None} | set(extra_instruments or []))

    def inst_ccy(iid: str) -> str | None:
        inst = book.instruments.get(iid)
        if inst is not None:
            return inst.currency
        return next((p["currency"] for p in h["positions"] if p["instrument_id"] == iid), None)
    # Weights are fractions of the scope's NAV in the base currency; an instrument priced in another currency cannot
    # share that denominator without an FX conversion, so it is listed instead of being mixed in (review fix).
    universe = [i for i in candidates if inst_ccy(i) == ccy]
    other_ccy = [{"instrument_id": i, "currency": inst_ccy(i),
                  "reason": f"priced in {inst_ccy(i)}, not the base currency {ccy}; no FX conversion is applied"}
                 for i in candidates if inst_ccy(i) != ccy]
    table = mark_table(store, account_key, as_of_ns=now_ns) if store is not None else []
    dates, hist = history_as_of(table, now_ns)
    rets = returns_from_prices(hist, dates, 1)
    vols, excluded = {}, []
    for iid in universe:
        r = [float(x) for _, x in sorted(rets.get(iid, {}).items())][-lookback:]
        if len(r) < min_obs:
            excluded.append({"instrument_id": iid, "reason": f"{len(r)} returns < {min_obs} required"})
            continue
        vols[iid] = sample_std(r)
    out = {"kind": "allocation", "label": PROPOSAL, "account_key": account_key, "scope": scope.label, "as_of_ns": now_ns,
           "core_seq": book.core_seq, "universe": universe, "parameters": {"cash_reserve": str(cash_reserve), "floor": str(floor),
                                                                           "cap": str(cap), "lambda": lam, "eta": eta, "tau": tau,
                                                                           "lookback": lookback},
           "baselines": {"equal_weight": equal_weight(universe, cash_reserve=cash_reserve),
                         "inverse_volatility": inverse_volatility(vols, cash_reserve=cash_reserve, floor=floor, cap=cap)},
           "excluded_from_risk_estimates": excluded, "excluded_from_universe": other_ccy, "currency": ccy,
           "fixture_data": h["fixture_data"], "fixture_note": h["fixture_note"]}
    opt_ids = [i for i in universe if i in vols]
    if method in ("all", "mean_variance") and len(opt_ids) >= 1:
        common = sorted(set.intersection(*(set(rets[i]) for i in opt_ids)))[-lookback:]
        if len(common) < max(min_obs, 2):
            out["mean_variance"] = {"status": "unavailable", "reason": f"{len(common)} common return dates < {max(min_obs, 2)}"}
        else:
            rows = [[float(rets[i][d]) for i in opt_ids] for d in common]
            lw = ledoit_wolf(rows)
            totals = h["totals"]
            nav = Fixed.parse(totals[ccy]["nav_marked"]) if totals.get(ccy, {}).get("nav_marked") else None
            # Current weights aggregate every owner in the scope that holds the instrument (review fix: a dict
            # comprehension kept only the last owner's market value for system and account scopes).
            mv: dict[str, Fixed] = {}
            for p in h["positions"]:
                if p["market_value"] is not None and p["currency"] == ccy:
                    mv[p["instrument_id"]] = mv.get(p["instrument_id"], Fixed(0)) + Fixed.parse(p["market_value"])
            w_old = [float(frac(mv.get(i, Fixed(0))) / frac(nav)) if nav and nav.is_positive() else 0.0 for i in opt_ids]
            mus = [float((mu or {}).get(i, 0.0)) for i in opt_ids]
            mvres = mean_variance(lw["sigma"], mus, lam=lam, eta=eta, w_old=w_old, cash_reserve=cash_reserve,
                                  caps=[float(parse_fraction(cap, "cap"))] * len(opt_ids), tau=tau, names=opt_ids)
            mvres["covariance"] = {k: lw[k] for k in ("intensity", "target", "normalisation", "observations",
                                                      "min_eigenvalue_before", "min_eigenvalue_after",
                                                      "condition_number_before", "condition_number_after", "symmetric", "psd")}
            mvres["expected_returns"] = {"source": mu_source or "none supplied: lambda term is zero (minimum variance with "
                                                                "turnover penalty)", "values": dict(zip(opt_ids, mus))}
            mvres["current_weights"] = dict(zip(opt_ids, w_old))
            out["mean_variance"] = mvres
    # Trade list for a single session (the only scope that can later become intents).
    if scope.kind == "owner" and scope.owner and scope.owner.startswith("session:") and book.policy is not None:
        owner_rows = [s for s in h["owners"] if s["owner"] == scope.owner and s["currency"] == book.base_currency]
        if owner_rows and owner_rows[0]["nav_marked"] is not None:
            s = owner_rows[0]
            prices = {p["instrument_id"]: Fixed.parse(p["mark"]["price"]) for p in h["positions"] if p["mark"]["price"]}
            from .marks import normalize_quotes
            for iid, q in normalize_quotes(quotes).items():
                if iid in universe and iid not in prices and q.bid is not None:
                    prices[iid] = q.bid
            held = {p["instrument_id"]: Fixed.parse(p["quantity"]) for p in h["positions"]}
            lots = {i: book.instruments[i].quantity_increment for i in universe if i in book.instruments}
            mults = {i: book.instruments[i].multiplier for i in universe if i in book.instruments}
            stale_sizing = sorted(p["instrument_id"] for p in h["positions"] if p["mark"]["status"] == "stale")
            lists = {}
            choices = [("equal_weight", out["baselines"]["equal_weight"]), ("inverse_volatility", out["baselines"]["inverse_volatility"])]
            if isinstance(out.get("mean_variance"), dict) and out["mean_variance"].get("weights"):
                choices.append(("mean_variance", out["mean_variance"]))
            for name, res in choices:
                if res.get("status") not in ("ok", "optimal", "optimal_within_precision") or not res.get("weights"):
                    lists[name] = {"status": res.get("status"), "reason": "no weights to round"}
                    continue
                # A holding the method did not weight (no return history, another currency, unpriced) is left as it
                # is and listed: missing data is never turned into a proposal to sell it (review fix).
                cur = {i: q for i, q in held.items() if i in res["weights"]}
                tl = trade_list(res["weights"], nav=Fixed.parse(s["nav_marked"]), liquid=Fixed.parse(s["cash"]["liquid_value"]),
                                available_cash=Fixed.parse(s["cash"]["available_cash"]), ccy=book.base_currency,
                                prices=prices, current_qty=cur, lot_sizes=lots, multipliers=mults,
                                fee_model_policy=book.policy.fees, cash_reserve=cash_reserve,
                                caps={i: float(parse_fraction(cap, "cap")) for i in universe})
                tl["left_unchanged"] = [{"instrument_id": i, "quantity": q.to_str(),
                                         "reason": "held but not weighted by this method (missing return history, another "
                                                   "currency or no mark): no trade proposed"}
                                        for i, q in sorted(held.items()) if i not in res["weights"]]
                tl["stale_sizing_marks"] = [i for i in stale_sizing if i in res["weights"]]
                lists[name] = tl
            out["trade_lists"] = lists
        else:
            out["trade_lists"] = {"reason": "the session has no marked NAV in the base currency (an unpriced position or no "
                                            "balance): trade lists cannot be sized"}
    else:
        out["trade_lists"] = {"reason": "trade lists are produced for a single session scope (owner:session:<id>) only"}
    out["lineage"] = lineage(["alloc.equal_weight", "alloc.inverse_vol", "alloc.ledoit_wolf", "alloc.mean_variance_l1",
                              "alloc.lot_rounding", "alloc.turnover", "stats.volatility"],
                             inputs={"core_seq": book.core_seq, "return_dates_visible": len(dates)},
                             units="weights as decimals of scope NAV; trade quantities and money exact",
                             source_dates={"as_of_ns": now_ns, "first_date": dates[0] if dates else None,
                                           "last_date": dates[-1] if dates else None},
                             missing=excluded + other_ccy,
                             notes=["volatility: sample std (n-1) of daily snapshot-mark returns, not annualized",
                                    "weights are of the scope's marked NAV"])
    return out
