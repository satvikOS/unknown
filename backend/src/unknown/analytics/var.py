"""Historical VaR and Expected Shortfall with a Kupiec backtest (CAP-PORT-10; book eq. 6.3).

Definitions (formulas risk.*):
* Loss L = -dV in the position currency. Historical simulation revalues each current
  position under each historical scenario: dV_s = sum_i q_i * mark_i * mult_i * R_i,s,
  where R_i,s is the instrument's simple return over the horizon ending on date s. For
  h > 1 the returns are overlapping h-day price ratios, not sqrt-time scaling.
* VaR_alpha = inf{l : P(L <= l) >= alpha}; with N equally weighted scenarios this is
  L_(k), k = ceil(alpha N), on ascending losses.
* ES_alpha by the quantile-integral definition (book eq. 6.3), exact for an empirical
  distribution including ties:  ES = (1/(1-alpha)) [ (k/N - alpha) L_(k) + (1/N) sum_{j>k} L_(j) ].
  The IBKR wording ("average of losses >= VaR") is reported separately and labelled; it
  differs when the distribution has atoms and is not used for subadditivity checks.
* Neither number bounds the worst possible loss.

Scenario data come from the valuation snapshots' fresh marks (or the point-in-time
research store) visible at the decision instant only. VaR, ES and backtest figures are
statistical floats: they never size, reserve or post money."""
from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path

from ..domain.fixed import Fixed
from .common import Scope, finite, frac, lineage, parse_fraction


def _alpha(alpha) -> Fraction:
    a = parse_fraction(alpha, "confidence")
    if not (0 < a <= 1):
        raise ValueError("confidence must be in (0, 1]")
    return a


def var_es(losses, alpha) -> dict:
    """Empirical VaR and both ES forms for a loss vector (formulas risk.var_hist, risk.es_*)."""
    a = _alpha(alpha)
    xs = sorted(Fraction(x) for x in losses)
    n = len(xs)
    if n == 0:
        return {"var": None, "es": None, "reason": "no scenarios"}
    k = math.ceil(a * n)
    k = max(1, min(n, k))
    var = xs[k - 1]
    if a == 1:
        es = xs[-1]
    else:
        es = ((Fraction(k, n) - a) * xs[k - 1] + sum(xs[k:], Fraction(0)) / n) / (1 - a)
    tail = [x for x in xs if x >= var]
    es_tail = sum(tail, Fraction(0)) / len(tail)
    return {"alpha": str(float(a)), "scenarios": n, "quantile_index_k": k, "var": float(var), "es": float(es),
            "es_tail_mean_ibkr_form": float(es_tail), "worst_loss": float(xs[-1]),
            "note": "VaR and ES do not bound the worst possible loss"}


def kupiec(exceptions: int, observations: int, alpha) -> dict:
    """Kupiec proportion-of-failures likelihood ratio and its chi-square(1) p-value (formula risk.kupiec_pof)."""
    a = _alpha(alpha)
    p = float(1 - a)
    x, T = exceptions, observations
    if T <= 0:
        return {"lr": None, "p_value": None, "reason": "no observations"}
    if not 0 < p < 1:
        return {"lr": None, "p_value": None, "reason": "confidence 100% has no exception rate to test"}

    def ll(prob: float) -> float:
        # log[(1-prob)^(T-x) prob^x] with 0*log(0) = 0
        out = 0.0
        if T - x:
            out += (T - x) * math.log(1 - prob) if prob < 1 else -math.inf
        if x:
            out += x * math.log(prob) if prob > 0 else -math.inf
        return out
    lr = -2 * ll(p) + 2 * ll(x / T)
    lr = max(0.0, lr)
    return {"exceptions": x, "observations": T, "expected_exceptions": p * T, "exception_rate": x / T,
            "lr": lr, "p_value": math.erfc(math.sqrt(lr / 2)), "df": 1}


# ------------------------------------------------------------------ scenario data
def returns_from_prices(history: dict[str, list[tuple[str, Fraction]]], dates: list[str], horizon: int = 1
                        ) -> dict[str, dict[str, Fraction]]:
    """R_i(d_j) = P_i(d_j) / P_i(d_{j-h}) - 1 on the global valuation-date sequence ``dates``; a return exists only
    when the instrument has a mark on both dates (no gap is silently spanned)."""
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    out: dict[str, dict[str, Fraction]] = {}
    for iid, pts in history.items():
        price = dict(pts)
        series = {}
        for j in range(horizon, len(dates)):
            d0, d1 = dates[j - horizon], dates[j]
            if d0 in price and d1 in price and price[d0] > 0:
                series[d1] = price[d1] / price[d0] - 1
        out[iid] = series
    return out


def mark_table(store, account_key: str, *, as_of_ns: int) -> list[tuple]:
    """(valuation_date, created_at_ns, as_of_ns, instrument_id, mark, status) for every run existing at ``as_of_ns``."""
    rows = store.conn.execute(
        "SELECT r.valuation_date, r.created_at_ns, r.as_of_ns, q.instrument_id, q.mark, q.mark_status FROM valuation_run r "
        "JOIN run_quote q ON q.run_id = r.run_id WHERE r.account_key=? AND r.created_at_ns<=? AND r.as_of_ns<=?",
        (account_key, as_of_ns, as_of_ns)).fetchall()
    return [tuple(r) for r in rows]


def history_as_of(table: list[tuple], cutoff_ns: int) -> tuple[list[str], dict[str, list[tuple[str, Fraction]]]]:
    """Point-in-time price history: for each date, the latest run created at or before ``cutoff_ns``; fresh marks only."""
    latest: dict[str, tuple[int, int]] = {}
    for d, created, asof, _, _, _ in table:
        if created <= cutoff_ns and asof <= cutoff_ns:
            if d not in latest or (created, asof) > latest[d]:
                latest[d] = (created, asof)
    hist: dict[str, list[tuple[str, Fraction]]] = {}
    for d, created, asof, iid, mark, status in table:
        if latest.get(d) == (created, asof) and mark is not None and status == "fresh":
            hist.setdefault(iid, []).append((d, frac(Fixed.parse(mark))))
    for v in hist.values():
        v.sort()
    return sorted(latest), hist


def research_price_history(research_db: str | Path, entity_by_instrument: dict[str, str], *, as_of_ns: int,
                           field: str = "close", dataset: str | None = None) -> dict[str, list[tuple[str, Fraction]]]:
    """Prices from the point-in-time research store (``data.pit.ResearchStore.as_of``): only publications with
    available_time <= as_of_ns. Opened read-only."""
    import sqlite3
    from ..data.pit import ResearchStore
    store = object.__new__(ResearchStore)  # read-only handle: never runs the store's schema DDL
    store.path = str(research_db)
    store.raw_dir = None
    store.conn = sqlite3.connect(f"file:{research_db}?mode=ro", uri=True)
    store.conn.row_factory = sqlite3.Row
    try:
        out = {}
        for iid, eid in entity_by_instrument.items():
            rows = [r for r in store.as_of(eid, field, as_of_ns, dataset=dataset) if r["value"] is not None and r["period_end"]]
            by_date: dict[str, set] = {}
            for r in rows:
                by_date.setdefault(r["period_end"], set()).add((r["dataset"], r["value"]))
            clash = sorted(d for d, v in by_date.items() if len(v) > 1)
            if clash:
                # Two datasets (or period kinds) give a price for the same date: refuse rather than let the last one
                # silently win (review fix); the caller names the dataset.
                raise ValueError(f"{iid}: several prices for {clash[0]} (and {len(clash) - 1} more dates) visible at "
                                 f"as_of; name the dataset")
            out[iid] = sorted((r["period_end"], frac(Fixed.parse(r["value"]))) for r in rows)
        return out
    finally:
        store.conn.close()


# ------------------------------------------------------------------ portfolio VaR
def historical_var(positions: list[dict], returns: dict[str, dict[str, Fraction]], *, alpha, lookback: int | None = 250,
                   min_scenarios: int = 20) -> dict:
    """Full revaluation of linear positions under each common historical date (formula risk.hist_scenario@1).

    ``positions``: dicts with instrument_id, quantity, mark, multiplier (decimal strings), currency."""
    covered, uncovered = [], []
    for p in positions:
        if p.get("mark") is None:
            uncovered.append({"instrument_id": p["instrument_id"], "reason": "no mark: position cannot be revalued"})
        elif not returns.get(p["instrument_id"]):
            uncovered.append({"instrument_id": p["instrument_id"], "reason": "no return history visible at the decision time"})
        else:
            covered.append(p)
    if not covered:
        return {"var": None, "es": None, "reason": "no position has a mark and a return history", "uncovered": uncovered,
                "scenarios": 0}
    common = None
    for p in covered:
        ds = set(returns[p["instrument_id"]])
        common = ds if common is None else common & ds
    dates = sorted(common or [])
    if lookback is not None:
        dates = dates[-lookback:]
    if len(dates) < min_scenarios:
        return {"var": None, "es": None, "scenarios": len(dates), "uncovered": uncovered,
                "reason": f"{len(dates)} common scenario dates < {min_scenarios} required"}
    exposure = {p["instrument_id"] + "|" + p.get("owner", ""): frac(Fixed.parse(p["quantity"])) * frac(Fixed.parse(p["mark"]))
                * frac(Fixed.parse(p.get("multiplier") or "1")) for p in covered}
    losses = []
    for d in dates:
        dv = sum((exposure[p["instrument_id"] + "|" + p.get("owner", "")] * returns[p["instrument_id"]][d] for p in covered),
                 Fraction(0))
        losses.append((-dv, d))
    res = var_es([l for l, _ in losses], alpha)
    ordered = sorted(losses)
    tail_loss, tail_date = ordered[res["quantile_index_k"] - 1]
    mv_total = sum(exposure.values(), Fraction(0))
    res.update(scenario_dates={"first": dates[0], "last": dates[-1]}, horizon_returns="price ratio over the horizon",
               var_scenario_date=tail_date,
               var_scenario_positions=[{"instrument_id": p["instrument_id"], "owner": p.get("owner"),
                                        "return": float(returns[p["instrument_id"]][tail_date]),
                                        "pnl": float(exposure[p["instrument_id"] + "|" + p.get("owner", "")]
                                                     * returns[p["instrument_id"]][tail_date])} for p in covered],
               covered_market_value=float(mv_total), uncovered=uncovered,
               var_pct_of_covered_mv=finite(res["var"] / float(mv_total)) if mv_total else None,
               coverage_complete=not uncovered,
               partial_note=(f"VaR and ES cover {len(covered)} of {len(covered) + len(uncovered)} positions; the "
                             f"uncovered ones carry unmeasured risk and are listed") if uncovered else None)
    return res


def var_report(holdings_report: dict, store, *, alpha="0.99", horizon: int = 1, lookback: int = 250,
               min_scenarios: int = 20) -> dict:
    """Per-currency historical VaR/ES for the positions in a holdings report, using snapshot marks visible at its
    valuation instant."""
    as_of = holdings_report["as_of_ns"]
    table = mark_table(store, holdings_report["account_key"], as_of_ns=as_of) if store is not None else []
    dates, hist = history_as_of(table, as_of)
    rets = returns_from_prices(hist, dates, horizon)
    by_ccy: dict[str, list[dict]] = {}
    for p in holdings_report["positions"]:
        by_ccy.setdefault(p["currency"], []).append(
            {"instrument_id": p["instrument_id"], "owner": p["owner"], "quantity": p["quantity"],
             "mark": p["mark"]["price"], "multiplier": "1" if p.get("multiplier") is None else p["multiplier"],
             "currency": p["currency"]})
    results = {c: historical_var(ps, rets, alpha=alpha, lookback=lookback, min_scenarios=min_scenarios)
               for c, ps in sorted(by_ccy.items())}
    for c, r in results.items():
        r["stale_marked_positions"] = sorted({p["instrument_id"] for p in holdings_report["positions"]
                                              if p["currency"] == c and p["mark"]["status"] == "stale"})
        if r["stale_marked_positions"]:
            r["stale_note"] = "exposures of these positions use their last (stale) mark"
        nav = holdings_report["totals"].get(c, {}).get("nav_marked")
        r["var_pct_of_scope_nav"] = finite(r["var"] / float(frac(Fixed.parse(nav)))) if (r.get("var") is not None and nav
                                                                                         and Fixed.parse(nav).is_positive()) else None
    fixture = holdings_report.get("fixture_data", False)
    return {"kind": "var", "method": "historical simulation (full revaluation of linear positions)",
            "account_key": holdings_report["account_key"], "scope": holdings_report["scope"], "as_of_ns": as_of,
            "alpha": alpha, "horizon_days": horizon, "lookback_scenarios": lookback,
            "by_currency": results,
            "aggregate": results[next(iter(results))] if len(results) == 1 else
            {"var": None, "reason": "positions in several currencies and no FX scenario set: aggregate not computed"},
            "history": {"valuation_dates_visible": len(dates), "source": "valuation snapshot fresh marks visible at as_of"},
            "fixture_data": fixture, "fixture_note": "FIXTURE quotes: test data, not market data" if fixture else None,
            "statement": "VaR does not represent the worst-case loss",
            "lineage": lineage(["risk.hist_scenario", "risk.var_hist", "risk.es_quantile_integral", "risk.es_tail_mean"],
                               inputs={"holdings_core_seq": holdings_report["core_seq"], "snapshot_dates": len(dates)},
                               units="currency (loss positive)", source_dates={"as_of_ns": as_of,
                                                                               "first_date": dates[0] if dates else None,
                                                                               "last_date": dates[-1] if dates else None},
                               missing=[u for r in results.values() for u in r.get("uncovered", [])])}


def backtest(store, account_key: str, scope: Scope, currency_code: str, *, as_of_ns: int, alpha="0.99", window: int = 250,
             lookback: int = 250, min_scenarios: int = 20) -> dict:
    """Daily 1-day VaR backtest: VaR forecast from positions and data visible at the end of day t-1, compared with the
    realized flow-adjusted loss L_t = -((NAV_t - NAV_{t-1}) - CF_t) (CAP-PORT-10 backtest panel)."""
    runs = {r["valuation_date"]: r for r in store.visible_runs(account_key, as_of_ns=as_of_ns)}
    points = [p for p in store.series(account_key, scope.label, currency_code, as_of_ns=as_of_ns)]
    table = mark_table(store, account_key, as_of_ns=as_of_ns)
    all_runs: dict[str, list] = {}
    for r in store.conn.execute("SELECT run_id, valuation_date, as_of_ns, created_at_ns FROM valuation_run WHERE account_key=? "
                                "AND created_at_ns<=? AND as_of_ns<=? ORDER BY created_at_ns, as_of_ns",
                                (account_key, as_of_ns, as_of_ns)):
        all_runs.setdefault(r["valuation_date"], []).append(r)
    rows, skipped = [], []
    pairs = [(a, b) for a, b in zip(points, points[1:]) if a.nav is not None and b.nav is not None]
    for prev, cur in pairs[-window:]:
        # Ex ante: the forecast for day t uses the latest run of day t-1 that EXISTED before day t was valued. The
        # latest visible run of t-1 can be a revision recorded later (``SnapshotStore.record(created_at_ns=...)``);
        # using its creation time as the cutoff would put day t's own return into the scenario set (review fix).
        before = [r for r in all_runs.get(prev.valuation_date, []) if r["created_at_ns"] < cur.as_of_ns]
        if not before:
            skipped.append({"date": cur.valuation_date,
                            "reason": "no valuation of the prior day existed before this day was valued: no ex-ante forecast"})
            continue
        run_prev = before[-1]
        cutoff = run_prev["created_at_ns"]
        dates, hist = history_as_of(table, cutoff)
        rets = returns_from_prices(hist, dates, 1)
        pos = [{"instrument_id": p["instrument_id"], "owner": p["owner"], "quantity": p["quantity"], "mark": p["mark"],
                "multiplier": p["multiplier"]} for p in store.run_positions(run_prev["run_id"])
               if scope.includes(p["owner"]) and p["currency"] == currency_code]
        realized = -((frac(cur.nav) - frac(prev.nav)) - (frac(cur.cum_flow) - frac(prev.cum_flow)))
        if not pos:
            var = 0.0
        else:
            r = historical_var(pos, rets, alpha=alpha, lookback=lookback, min_scenarios=min_scenarios)
            if r.get("var") is None or r.get("uncovered"):
                skipped.append({"date": cur.valuation_date, "reason": r.get("reason") or "positions without history"})
                continue
            var = r["var"]
        rows.append({"date": cur.valuation_date, "var_forecast": var, "realized_loss": float(realized),
                     "exception": float(realized) > var})
    x = sum(1 for r in rows if r["exception"])
    return {"kind": "var_backtest", "account_key": account_key, "scope": scope.label, "currency": currency_code,
            "as_of_ns": as_of_ns, "alpha": alpha, "window": window, "observations": len(rows), "exceptions": x,
            "kupiec": kupiec(x, len(rows), alpha), "days": rows, "skipped": skipped,
            "note": "diagnostic mirroring the BIS desk-level design (1-day VaR vs realized P&L); not a regulatory claim",
            "lineage": lineage(["risk.var_hist", "risk.kupiec_pof", "port.external_flow", "port.nav"],
                               inputs={"snapshot_runs": len(runs)}, units="currency (loss positive)",
                               source_dates={"as_of_ns": as_of_ns}, missing=skipped,
                               notes=["each forecast uses only positions and marks visible at the prior valuation"])}
