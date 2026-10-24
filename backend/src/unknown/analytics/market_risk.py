"""Portfolio market risk from stored REAL daily bars (CAP-PORT-03, CAP-PORT-05, CAP-PORT-11; ADR-0020).

Inputs are the holdings read model (``holdings.holdings``) and the research bar store
``<UNKNOWN_HOME>/research/research.sqlite`` table ``mkt_bar``, which every bar provider writes (the IBKR history
adapter writes it with ``provider='IBKR'``). Only current, non-fixture daily bars that were available at the
valuation instant are read. There is no generated, demo or default series: without stored bars a figure is ``null``
with a reason from ``domain.unavailable`` and a detail naming what is missing.

Every figure is an object ``{value, formula, reason, detail, n, window, inputs_as_of}`` so the terminal can show its
formula id (``analytics.formulas``) and the as-of of the inputs it used. Statistics are IEEE floats (display numbers,
never money); scenario P&L is exact ``Fixed`` arithmetic on real market values.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from ..domain import unavailable as ua
from ..domain.fixed import Fixed, Rounding
from ..domain.money import currency
from .common import fixed_of_fraction, frac, lineage, parse_fraction, weight
from .exposure import _group
from .formulas import ref

DEFAULT_BENCHMARK = "SPY.ARCX"
TRADING_DAYS = 252
OPEN_END = 2 ** 62
DAILY = "1 day"
MAX_MATRIX = 20
MAX_SHOCKS = 8
BETA_PLACES = Decimal("0.0001")
UNKNOWN_ISSUER = "unknown issuer"


@dataclass
class CloseSeries:
    """Daily closes of one security from the bar store, oldest first, one per session date."""
    key: str
    ids: list[str]
    closes: list[tuple[str, Decimal]] = field(default_factory=list)
    provider: str | None = None
    what_to_show: str | None = None
    use_rth: bool | None = None
    adjustment: str | None = None
    currency: str | None = None
    last_available_ns: int | None = None
    reason: str | None = None
    detail: str | None = None

    @property
    def last_date(self) -> str | None:
        return self.closes[-1][0] if self.closes else None

    def describe(self) -> dict:
        return {"key": self.key, "bar_ids": self.ids, "provider": self.provider, "what_to_show": self.what_to_show,
                "regular_session_only": self.use_rth, "adjustment": self.adjustment, "currency": self.currency,
                "closes": len(self.closes), "first_date": self.closes[0][0] if self.closes else None,
                "last_date": self.last_date, "last_available_ns": self.last_available_ns,
                "reason": self.reason, "detail": self.detail,
                "note": ("unadjusted closes: a split or dividend inside the window distorts returns"
                         if self.adjustment == "unadjusted" else None)}


def _session_date(bar_date, start_ns: int) -> str:
    s = str(bar_date or "").strip()[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).date().isoformat()


def load_closes(db: Path, key: str, ids: list[str], *, now_ns: int, lookback: int) -> CloseSeries:
    """The last ``lookback + 1`` stored daily closes for any of ``ids`` (TRADES and regular-session bars preferred,
    then the provider with the most bars). Never fabricates: a missing store or series is a reason."""
    out = CloseSeries(key, list(dict.fromkeys(i for i in ids if i)))
    if not Path(db).is_file():
        out.reason, out.detail = ua.NO_SOURCE, (f"no research bar store at {db}: no bar provider has stored daily "
                                                "bars yet (the IBKR history adapter writes them)")
        return out
    marks = ",".join("?" * len(out.ids))
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        out.reason, out.detail = ua.ERROR, f"bar store unreadable: {exc}"
        return out
    conn.row_factory = sqlite3.Row
    try:
        base = (f"FROM mkt_bar WHERE instrument_id IN ({marks}) AND bar_size=? AND fixture=0 AND known_to_ns=? "
                "AND available_time_ns<=?")
        args = (*out.ids, DAILY, OPEN_END, now_ns)
        series = [dict(r) for r in conn.execute(
            f"SELECT provider, what_to_show, use_rth, COUNT(*) AS n {base} GROUP BY 1, 2, 3", args)]
        if not series:
            out.reason, out.detail = ua.NO_DATA, f"no stored daily bars for {', '.join(out.ids)} available by the valuation time"
            return out
        pick = sorted(series, key=lambda s: (s["what_to_show"] != "TRADES", s["use_rth"] != 1, -s["n"], s["provider"]))[0]
        rows = conn.execute(f"SELECT bar_date, bar_start_ns, close, available_time_ns, adjustment, currency {base} "
                            "AND provider=? AND what_to_show=? AND use_rth=? ORDER BY bar_start_ns DESC LIMIT ?",
                            (*args, pick["provider"], pick["what_to_show"], pick["use_rth"],
                             (lookback + 1) * 2)).fetchall()
    except sqlite3.Error as exc:
        out.reason, out.detail = ua.ERROR, f"bar store unreadable: {exc}"
        return out
    finally:
        conn.close()
    by_day: dict[str, tuple[Decimal, int]] = {}
    adjustments, currencies, bad = set(), set(), 0
    for r in rows:
        day = _session_date(r["bar_date"], r["bar_start_ns"])
        try:
            c = Decimal(str(r["close"]))
        except (InvalidOperation, ValueError):
            bad += 1
            continue
        if not c.is_finite() or c <= 0:
            bad += 1
            continue
        if day not in by_day or r["available_time_ns"] > by_day[day][1]:
            by_day[day] = (c, r["available_time_ns"])
        adjustments.add(r["adjustment"])
        currencies.add(r["currency"])
    days = sorted(by_day)[-(lookback + 1):]
    out.closes = [(d, by_day[d][0]) for d in days]
    out.provider, out.what_to_show, out.use_rth = pick["provider"], pick["what_to_show"], bool(pick["use_rth"])
    out.adjustment = (sorted(adjustments)[0] if len(adjustments) == 1 else
                      ("mixed: " + ", ".join(sorted(adjustments)) if adjustments else None))
    out.currency = sorted(currencies)[0] if len(currencies) == 1 else None
    out.last_available_ns = max((by_day[d][1] for d in days), default=None)
    if not out.closes:
        out.reason, out.detail = ua.NO_DATA, f"stored daily bars for {key} have no usable close ({bad} rejected)"
    elif bad:
        out.detail = f"{bad} bar(s) with a missing or non-positive close were ignored"
    return out


# ----------------------------------------------------------------------------------------------- statistics
def returns_by_date(closes: list[tuple[str, Decimal]], dates: set[str] | None = None) -> list[tuple[str, float]]:
    """Simple returns between consecutive closes (restricted to ``dates`` when given), keyed by the later date."""
    pts = [(d, c) for d, c in closes if dates is None or d in dates]
    return [(pts[i][0], float(pts[i][1] / pts[i - 1][1] - 1)) for i in range(1, len(pts))]


def _mean(x: list[float]) -> float:
    return sum(x) / len(x)


def _cov(x: list[float], y: list[float]) -> float:
    mx, my = _mean(x), _mean(y)
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (len(x) - 1)


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


def figure(formula: str, value, *, reason: str | None = None, detail: str | None = None, **extra) -> dict:
    """One published figure: the value or null with a reason code, always with its formula id."""
    if value is None and reason is None:
        reason = ua.ERROR
    return {"value": value, "formula": ref(formula), "reason": None if value is not None else reason,
            "detail": detail, **extra}


def _window(rs: list[tuple[str, float]]) -> dict:
    return {"from": rs[0][0], "to": rs[-1][0]} if rs else {"from": None, "to": None}


def hist_vol(s: CloseSeries, min_obs: int) -> dict:
    asof = {"series": s.last_date}
    if s.reason and not s.closes:
        return figure("mrisk.hist_vol", None, reason=s.reason, detail=s.detail, n=0, window=_window([]), inputs_as_of=asof)
    rs = returns_by_date(s.closes)
    if len(rs) < max(min_obs, 2):
        return figure("mrisk.hist_vol", None, reason=ua.NO_DATA, n=len(rs), window=_window(rs), inputs_as_of=asof,
                      detail=f"{len(rs)} daily returns stored; at least {max(min_obs, 2)} are required")
    x = [r for _, r in rs]
    v = _finite(math.sqrt(_cov(x, x)) * math.sqrt(TRADING_DAYS))
    return figure("mrisk.hist_vol", v, reason=ua.ERROR, n=len(x), window=_window(rs), inputs_as_of=asof)


def paired(a: CloseSeries, b: CloseSeries) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    common = {d for d, _ in a.closes} & {d for d, _ in b.closes}
    return returns_by_date(a.closes, common), returns_by_date(b.closes, common)


def _pair_figure(formula: str, a: CloseSeries, b: CloseSeries, min_obs: int, fn) -> dict:
    asof = {a.key: a.last_date, b.key: b.last_date}
    for s in (a, b):
        if s.reason and not s.closes:
            return figure(formula, None, reason=s.reason, detail=f"{s.key}: {s.detail}", n=0, window=_window([]),
                          inputs_as_of=asof)
    ra, rb = paired(a, b)
    if len(ra) < max(min_obs, 2):
        return figure(formula, None, reason=ua.NO_DATA, n=len(ra), window=_window(ra), inputs_as_of=asof,
                      detail=f"{len(ra)} date-aligned daily returns; at least {max(min_obs, 2)} are required")
    x, y = [r for _, r in ra], [r for _, r in rb]
    value, why = fn(x, y)
    return figure(formula, value, reason=ua.NOT_APPLICABLE if why else None, detail=why, n=len(x), window=_window(ra),
                  inputs_as_of=asof)


def _beta(x: list[float], y: list[float]):
    vb = _cov(y, y)
    if vb <= 0:
        return None, "the benchmark returns have zero variance over the window"
    return _finite(_cov(x, y) / vb), None


def _corr(x: list[float], y: list[float]):
    vx, vy = _cov(x, x), _cov(y, y)
    if vx <= 0 or vy <= 0:
        return None, "a series has zero variance over the window"
    return _finite(_cov(x, y) / math.sqrt(vx * vy)), None


def beta(s: CloseSeries, bench: CloseSeries, min_obs: int) -> dict:
    return _pair_figure("mrisk.beta", s, bench, min_obs, _beta)


def correlation(s: CloseSeries, other: CloseSeries, min_obs: int) -> dict:
    return _pair_figure("mrisk.correlation", s, other, min_obs, _corr)


# ----------------------------------------------------------------------------------------------- report
def bar_ids(row: dict) -> list[str]:
    """Bar-store ids a position may be stored under: the core instrument id and ``SYMBOL.MIC``."""
    ids = [row["instrument_id"]]
    if row.get("symbol") and row.get("venue"):
        ids.append(f"{row['symbol']}.{row['venue']}")
    return ids


def _instruments(holdings_report: dict) -> list[dict]:
    """Positions aggregated per (instrument, currency) across the owners in scope."""
    agg: dict[tuple[str, str], dict] = {}
    for p in holdings_report["positions"]:
        k = (p["instrument_id"], p["currency"])
        scale = currency(p["currency"]).ledger_scale
        a = agg.setdefault(k, {"instrument_id": p["instrument_id"], "symbol": p.get("symbol"), "venue": p.get("venue"),
                               "currency": p["currency"], "quantity": Fixed(0), "market_value": Fixed(0, scale),
                               "priced": True, "owners": [], "sec_cik": p.get("sec_cik"),
                               "mark_status": p["mark"]["status"]})
        a["quantity"] += Fixed.parse(p["quantity"])
        a["owners"].append(p["owner"])
        if p["market_value"] is None:
            a["priced"] = False
        else:
            a["market_value"] += Fixed.parse(p["market_value"])
    return list(agg.values())


def _scenario_pnl(mv: Fixed, shock: Fraction, scale: int) -> Fixed:
    return fixed_of_fraction(frac(mv) * max(shock, Fraction(-1)), scale, Rounding.FLOOR)


def _beta_text(b: float) -> str:
    return str(Decimal(repr(b)).quantize(BETA_PLACES, rounding=ROUND_HALF_EVEN))


def parse_shocks(text: str | None) -> list[str]:
    raw = [t.strip() for t in (text or "-0.05,-0.10,-0.20").split(",") if t.strip()]
    if not raw or len(raw) > MAX_SHOCKS:
        raise ValueError(f"shocks: 1..{MAX_SHOCKS} comma-separated decimals")
    for t in raw:
        s = parse_fraction(t, "shock")
        if s <= -1 or s > 10:
            raise ValueError("each shock must be greater than -1 and at most 10")
    return raw


def market_risk_report(holdings_report: dict, research_db: Path, *, benchmark: str = DEFAULT_BENCHMARK,
                       lookback: int = TRADING_DAYS, min_obs: int = 60, shocks: list[str] | None = None,
                       now_ns: int | None = None) -> dict:
    now_ns = holdings_report["as_of_ns"] if now_ns is None else now_ns
    shocks = shocks or parse_shocks(None)
    bench = load_closes(research_db, benchmark, [benchmark], now_ns=now_ns, lookback=lookback)
    insts = _instruments(holdings_report)
    series = {(i["instrument_id"], i["currency"]): load_closes(research_db, i["instrument_id"], bar_ids(i),
                                                                 now_ns=now_ns, lookback=lookback) for i in insts}
    rows = []
    for i in insts:
        s = series[(i["instrument_id"], i["currency"])]
        rows.append({"instrument_id": i["instrument_id"], "symbol": i["symbol"], "venue": i["venue"],
                     "currency": i["currency"], "quantity": i["quantity"].to_str(),
                     "market_value": i["market_value"].to_str() if i["priced"] else None,
                     "market_value_reason": None if i["priced"] else ua.NO_DATA,
                     "mark_status": i["mark_status"], "owners": sorted(set(i["owners"])), "bars": s.describe(),
                     "hist_vol": hist_vol(s, min_obs), "beta": beta(s, bench, min_obs),
                     "correlation": correlation(s, bench, min_obs)})
    portfolio = {}
    for ccy in sorted({i["currency"] for i in insts}):
        portfolio[ccy] = _portfolio(ccy, [(i, r, series[(i["instrument_id"], ccy)]) for i, r in zip(insts, rows)
                                          if i["currency"] == ccy], min_obs)
    return {
        "kind": "market_risk", "account_key": holdings_report["account_key"], "scope": holdings_report["scope"],
        "as_of_ns": now_ns, "core_seq": holdings_report["core_seq"], "base_currency": holdings_report.get("base_currency"),
        "parameters": {"benchmark": benchmark, "lookback_sessions": lookback, "min_obs": min_obs,
                       "periods_per_year": TRADING_DAYS, "shocks": shocks},
        "benchmark": bench.describe(),
        "benchmark_vol": hist_vol(bench, min_obs),
        "instruments": rows,
        "portfolio": portfolio,
        "correlation_matrix": _matrix(insts, series, min_obs),
        "concentration": _concentration(holdings_report),
        "scenarios": [_scenario(s, insts, rows) for s in shocks],
        "fixture_data": holdings_report.get("fixture_data", False),
        "fixture_note": holdings_report.get("fixture_note"),
        "lineage": lineage(["mrisk.daily_return", "mrisk.hist_vol", "mrisk.beta", "mrisk.correlation",
                            "mrisk.portfolio_beta", "mrisk.portfolio_vol", "mrisk.scenario", "expo.issuer",
                            "expo.owner_bucket", "port.market_value"],
                           inputs={"holdings_core_seq": holdings_report["core_seq"],
                                   "mark_policy": holdings_report.get("mark_policy"),
                                   "bar_store": str(Path(research_db).name), "benchmark": benchmark},
                           units="volatility per year as a decimal; beta and correlation dimensionless; P&L in the "
                                 "position's currency as an exact decimal string",
                           source_dates={"valuation_ns": now_ns, "benchmark_last_date": bench.last_date,
                                         "benchmark_last_available_ns": bench.last_available_ns},
                           missing=[{"instrument_id": r["instrument_id"], "reason": r["bars"]["reason"],
                                     "detail": r["bars"]["detail"]} for r in rows if r["bars"]["reason"]],
                           notes=["only stored, non-fixture daily bars available at the valuation instant are used; "
                                  "nothing is filled, interpolated or defaulted"]),
    }


def _portfolio(ccy: str, members: list[tuple[dict, dict, CloseSeries]], min_obs: int) -> dict:
    priced = [(i, r, s) for i, r, s in members if i["priced"]]
    scale = currency(ccy).ledger_scale
    total = sum((i["market_value"] for i, _, _ in priced), Fixed(0, scale))
    unpriced = [i["instrument_id"] for i, _, _ in members if not i["priced"]]
    base = {"currency": ccy, "priced_market_value": total.to_str(), "unpriced_instruments": unpriced}
    if not priced or not total.is_positive():
        why = "no priced position in this currency" if not priced else "priced market value is not positive"
        return {**base, "portfolio_beta": figure("mrisk.portfolio_beta", None, reason=ua.NOT_APPLICABLE, detail=why),
                "portfolio_vol": figure("mrisk.portfolio_vol", None, reason=ua.NOT_APPLICABLE, detail=why)}
    weights = {i["instrument_id"]: frac(i["market_value"]) / frac(total) for i, _, _ in priced}
    missing = [i["instrument_id"] for i, r, _ in priced if r["beta"]["value"] is None]
    if missing:
        pb = figure("mrisk.portfolio_beta", None, reason=ua.NO_DATA, coverage=_coverage(weights, missing),
                    detail=f"no beta for {', '.join(missing)}: a position is never assumed to have beta 1")
    else:
        pb = figure("mrisk.portfolio_beta", sum(float(weights[i["instrument_id"]]) * r["beta"]["value"]
                                                for i, r, _ in priced), coverage="1.00000000",
                    inputs_as_of={i["instrument_id"]: r["beta"]["inputs_as_of"] for i, r, _ in priced})
    common = None
    for _, _, s in priced:
        ds = {d for d, _ in s.closes}
        common = ds if common is None else common & ds
    common = common or set()
    per = {i["instrument_id"]: dict(returns_by_date(s.closes, common)) for i, _, s in priced}
    dates = sorted(set.intersection(*(set(v) for v in per.values()))) if per else []
    no_bars = [i["instrument_id"] for i, _, s in priced if not s.closes]
    if no_bars or len(dates) < max(min_obs, 2):
        detail = (f"no stored daily bars for {', '.join(no_bars)}" if no_bars else
                  f"{len(dates)} daily returns common to every priced position; at least {max(min_obs, 2)} are required")
        pv = figure("mrisk.portfolio_vol", None, reason=ua.NO_DATA, detail=detail, n=len(dates),
                    coverage=_coverage(weights, no_bars))
    else:
        rp = [sum(float(weights[k]) * per[k][d] for k in per) for d in dates]
        pv = figure("mrisk.portfolio_vol", _finite(math.sqrt(_cov(rp, rp)) * math.sqrt(TRADING_DAYS)), n=len(rp),
                    window={"from": dates[0], "to": dates[-1]}, coverage="1.00000000")
    return {**base, "weights": {i["instrument_id"]: weight(i["market_value"], total) for i, _, _ in priced},
            "portfolio_beta": pb, "portfolio_vol": pv}


def _coverage(weights: dict[str, Fraction], missing: list[str]) -> str:
    covered = sum((w for k, w in weights.items() if k not in missing), Fraction(0))
    return str(fixed_of_fraction(covered, 8))


def _matrix(insts: list[dict], series: dict, min_obs: int) -> dict:
    ranked = sorted(insts, key=lambda i: (not i["priced"], -frac(i["market_value"]), i["instrument_id"]))[:MAX_MATRIX]
    keys = [(i["instrument_id"], i["currency"]) for i in ranked]
    cells = []
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            f = correlation(series[keys[a]], series[keys[b]], min_obs)
            cells.append({"a": keys[a][0], "b": keys[b][0], **f})
    return {"instruments": [k[0] for k in keys], "pairs": cells,
            "note": f"pairwise over date-aligned daily returns; at most {MAX_MATRIX} largest positions"}


def _concentration(h: dict) -> dict:
    by_ccy: dict[str, list[dict]] = defaultdict(list)
    for p in h["positions"]:
        if p["market_value"] is None:
            continue
        by_ccy[p["currency"]].append({"issuer": f"SEC CIK {p['sec_cik']}" if p.get("sec_cik") else UNKNOWN_ISSUER,
                                      "owner_bucket": p.get("bucket_kind") or p["owner"],
                                      "quantity": p["quantity"], "market_value": p["market_value"]})
    out = {}
    for ccy, rows in sorted(by_ccy.items()):
        scale = currency(ccy).ledger_scale
        gross = sum((abs(Fixed.parse(r["market_value"])) for r in rows), Fixed(0, scale))
        nav = h["totals"].get(ccy, {}).get("nav_marked")
        den = Fixed.parse(nav) if nav is not None else None
        out[ccy] = {"currency": ccy, "gross": gross.to_str(),
                    "denominator": {"name": "scope NAV (nav_marked)" if den is not None else None,
                                    "value": nav, "reason": None if den is not None else ua.NO_DATA},
                    "by_issuer": _group(rows, "issuer", den, gross, scale),
                    "by_owner_bucket": _group(rows, "owner_bucket", den, gross, scale),
                    "formulas": [ref("expo.issuer"), ref("expo.owner_bucket")]}
    return out


def _scenario(shock: str, insts: list[dict], rows: list[dict]) -> dict:
    s = parse_fraction(shock, "shock")
    uni: dict[str, Fixed] = {}
    bet: dict[str, Fixed] = {}
    positions, out_uniform, out_beta = [], [], []
    for i, r in zip(insts, rows):
        ccy, scale = i["currency"], currency(i["currency"]).ledger_scale
        if not i["priced"]:
            out_uniform.append({"instrument_id": i["instrument_id"], "reason": ua.NO_DATA, "detail": "unpriced position"})
            out_beta.append({"instrument_id": i["instrument_id"], "reason": ua.NO_DATA, "detail": "unpriced position"})
            continue
        du = _scenario_pnl(i["market_value"], s, scale)
        uni[ccy] = uni.get(ccy, Fixed(0, scale)) + du
        b = r["beta"]["value"]
        db, bt = None, None
        if b is None:
            out_beta.append({"instrument_id": i["instrument_id"], "reason": r["beta"]["reason"],
                             "detail": "no beta from stored bars: not revalued (never assumed 1)"})
        else:
            bt = _beta_text(b)
            db = _scenario_pnl(i["market_value"], Fraction(bt) * s, scale)
            bet[ccy] = bet.get(ccy, Fixed(0, scale)) + db
        positions.append({"instrument_id": i["instrument_id"], "currency": ccy, "market_value": i["market_value"].to_str(),
                          "uniform_pnl": du.to_str(), "beta_used": bt, "beta_pnl": db.to_str() if db is not None else None,
                          "capped": bool(bt is not None and Fraction(bt) * s < -1)})
    return {"shock": shock, "formula": ref("mrisk.scenario"), "label": "HYPOTHETICAL",
            "uniform": {"description": f"every priced position moves {shock}",
                        "pnl_by_currency": {c: v.to_str() for c, v in sorted(uni.items())},
                        "not_revalued": out_uniform},
            "beta": {"description": f"benchmark moves {shock}; each position moves beta x {shock} (capped at -100%)",
                     "pnl_by_currency": {c: v.to_str() for c, v in sorted(bet.items())},
                     "not_revalued": out_beta},
            "positions": positions}


__all__ = ["DEFAULT_BENCHMARK", "CloseSeries", "load_closes", "market_risk_report", "parse_shocks", "hist_vol", "beta",
           "correlation"]
