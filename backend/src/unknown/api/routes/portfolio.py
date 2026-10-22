"""Read-only portfolio and risk routes (capability records CAP-PORT-01..13).

Mounted under ``/api/portfolio/``. Every handler reads the core through the request's
read-only connection and the research stores through read-only handles; none of them can
write. Every response carries a lineage block (formula ids, inputs, units, source dates,
missing reasons) or an explicit unavailable reason; fixture data is labelled as such.

  GET /api/portfolio/holdings?scope=&basis=bid|mid|last&max_age_ms=
  GET /api/portfolio/exposure?scope=&top_k=
  GET /api/portfolio/snapshots?limit=
  GET /api/portfolio/performance?scope=&currency=&convention=start_of_day|end_of_day&periods_per_year=&rf=&rf_id=&nav=nav_fresh
  GET /api/portfolio/pnl?scope=&currency=&from=&to=
  GET /api/portfolio/var?scope=&alpha=&horizon=&lookback=
  GET /api/portfolio/var/backtest?scope=&currency=&alpha=&window=&lookback=
  GET /api/portfolio/stress/<overnight_gap|correlated_selloff|stale_quote|fee_revision>?scope=&...
  GET /api/portfolio/whatif?session=&instrument=&side=&quantity=&limit=[&deployment=]
  GET /api/portfolio/allocation?scope=owner:session:<id>&cash_reserve=&floor=&cap=&eta=&tau=&lookback=
  GET /api/portfolio/formulas
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from ...analytics import allocation, exposure, holdings, performance, pnl, snapshots, stress, var, whatif
from ...analytics.common import parse_scope
from ...analytics.formulas import FORMULAS

UNAVAILABLE_STORE = ("no valuation snapshots yet: run `python -m unknown.analytics.snapshots record --account <key>` at end "
                     "of day to build the time series")


def _int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = q.get(key)
    if v in (None, ""):
        return default
    try:
        n = int(v)
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None
    if not lo <= n <= hi:
        raise ValueError(f"{key} must be in [{lo}, {hi}]")
    return n


def _float(q: dict, key: str, *, lo: float | None = None) -> float | None:
    """A finite float parameter; 'nan', 'inf' and values below ``lo`` are refused (review fix: they reached the
    optimiser and the Sharpe ratio unchecked)."""
    v = q.get(key)
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except ValueError:
        raise ValueError(f"{key} must be a number") from None
    if not math.isfinite(x):
        raise ValueError(f"{key} must be a finite number")
    if lo is not None and x < lo:
        raise ValueError(f"{key} must be >= {lo}")
    return x


def _quotes(ctx) -> tuple[dict, str | None]:
    try:
        return ctx.core.query("quotes"), None
    except Exception as exc:  # IpcError, OSError: the core is down; analytics still answer with the reason
        return {}, f"core unreachable, no quote snapshot: {exc}"


def _research_dir(ctx) -> Path:
    return Path(ctx.paths.root) / "research"


def _store(ctx):
    try:
        return snapshots.SnapshotStore(snapshots.default_path(ctx.paths.root), readonly=True), None
    except FileNotFoundError:
        return None, UNAVAILABLE_STORE


def _with_quotes(body: dict, reason: str | None) -> dict:
    body["quote_snapshot"] = {"source": "core quote book (IPC query 'quotes')", "unavailable_reason": reason}
    return body


def handle(ctx, parts: list[str], q: dict, account_key: str, conn) -> dict:
    sub = parts[1:]
    now = time.time_ns()
    scope = parse_scope(q.get("scope"))
    if sub == ["formulas"]:
        return {"kind": "formulas", "formulas": [f.to_json() for f in FORMULAS.values()]}
    if sub == ["holdings"]:
        quotes, why = _quotes(ctx)
        max_age = _int(q, "max_age_ms", -1, 0, 86_400_000)
        max_age = None if max_age < 0 else max_age  # absent -> the policy budget; an explicit 0 is honoured
        return _with_quotes(holdings.holdings(conn, account_key, quotes, now_ns=now, scope=scope,
                                              mark_basis=q.get("basis", "bid"), max_age_ms=max_age), why)
    if sub == ["exposure"]:
        quotes, why = _quotes(ctx)
        h = holdings.holdings(conn, account_key, quotes, now_ns=now, scope=scope)
        clf = exposure.SicClassifier(_research_dir(ctx) / "research.sqlite")
        try:
            return _with_quotes(exposure.exposure_report(h, classifier=clf, top_k=_int(q, "top_k", 5, 1, 50)), why)
        finally:
            clf.close()
    if sub == ["whatif"]:
        quotes, why = _quotes(ctx)
        intent = {"session_id": q.get("session"), "instrument_id": q.get("instrument"), "side": q.get("side"),
                  "quantity": q.get("quantity"), "limit_price": q.get("limit")}
        missing = [k for k, v in intent.items() if not v]
        if missing:
            raise ValueError(f"missing parameters: {', '.join(missing)}")
        try:
            return _with_quotes(whatif.dry_run(conn, account_key, intent, quotes, now_ns=now,
                                               deployment_id=q.get("deployment") or None), why)
        except whatif.SandboxFault as exc:
            return {"kind": "whatif", "label": whatif.HYPOTHETICAL, "approved": False, "fault": str(exc),
                    "reason": "the core command path raised a fault on the private copy; no decision is available"}
    if sub and sub[0] == "stress":
        name = sub[1] if len(sub) > 1 else q.get("scenario")
        if name not in stress.SCENARIOS:
            raise LookupError(f"unknown stress scenario {name!r}; one of {sorted(stress.SCENARIOS)}")
        quotes, why = _quotes(ctx)
        kw: dict = {"now_ns": now, "scope": scope}
        if name == "overnight_gap" and q.get("gap"):
            kw["gap"] = q["gap"]
        if name == "correlated_selloff" and q.get("shock"):
            kw["market_shock"] = q["shock"]
        if name == "stale_quote":
            kw["delay_s"] = _int(q, "delay_s", 600, 1, 7 * 86_400)
            if q.get("haircut"):
                kw["haircut"] = q["haircut"]
        if name == "fee_revision" and q.get("factor"):
            kw["factor"] = q["factor"]
        try:
            return _with_quotes(stress.SCENARIOS[name](conn, account_key, quotes, **kw), why)
        except whatif.SandboxFault as exc:
            return {"kind": "stress", "scenario": name, "fault": str(exc),
                    "reason": "the core raised a fault on the private copy; the scenario result is unavailable"}
    store, store_why = _store(ctx)
    try:
        if sub == ["snapshots"]:
            if store is None:
                return {"kind": "snapshots", "available": False, "reason": store_why}
            runs = store.visible_runs(account_key, as_of_ns=now)[-_int(q, "limit", 30, 1, 5000):]
            return {"kind": "snapshots", "available": True, "account_key": account_key,
                    "runs": [{k: r[k] for k in ("run_id", "valuation_date", "as_of_ns", "created_at_ns", "core_seq", "mark_basis",
                                                 "max_age_ms", "fixture", "supersedes")} for r in runs]}
        if sub == ["performance"]:
            if store is None:
                return {"kind": "performance", "available": False, "reason": store_why}
            ccy = q.get("currency") or conn.execute("SELECT base_currency FROM accounts WHERE account_key=?",
                                                    (account_key,)).fetchone()["base_currency"]
            nav_field = q.get("nav", "nav_fresh")
            pts = store.series(account_key, scope.label, ccy, as_of_ns=now, nav_field=nav_field,
                               start_date=q.get("from"), end_date=q.get("to"))
            return performance.performance_report(pts, scope=scope.label, currency_code=ccy, as_of_ns=now,
                                                  convention=q.get("convention", "start_of_day"),
                                                  periods_per_year=_int(q, "periods_per_year", 252, 1, 366),
                                                  rf_annual=_float(q, "rf"), rf_series_id=q.get("rf_id"),
                                                  min_obs=_int(q, "min_obs", 20, 2, 10_000), nav_field=nav_field)
        if sub == ["pnl"]:
            if store is None:
                return {"kind": "pnl", "available": False, "reason": store_why}
            ccy = q.get("currency") or conn.execute("SELECT base_currency FROM accounts WHERE account_key=?",
                                                    (account_key,)).fetchone()["base_currency"]
            return pnl.pnl_report(store, conn, account_key, scope, ccy, as_of_ns=now, start_date=q.get("from"),
                                  end_date=q.get("to"))
        if sub == ["var"]:
            quotes, why = _quotes(ctx)
            h = holdings.holdings(conn, account_key, quotes, now_ns=now, scope=scope)
            body = var.var_report(h, store, alpha=q.get("alpha", "0.99"), horizon=_int(q, "horizon", 1, 1, 60),
                                  lookback=_int(q, "lookback", 250, 1, 10_000), min_scenarios=_int(q, "min_scenarios", 20, 1, 10_000))
            if store is None:
                body["history"]["unavailable_reason"] = store_why
            return _with_quotes(body, why)
        if sub == ["var", "backtest"]:
            if store is None:
                return {"kind": "var_backtest", "available": False, "reason": store_why}
            ccy = q.get("currency") or conn.execute("SELECT base_currency FROM accounts WHERE account_key=?",
                                                    (account_key,)).fetchone()["base_currency"]
            return var.backtest(store, account_key, scope, ccy, as_of_ns=now, alpha=q.get("alpha", "0.99"),
                                window=_int(q, "window", 250, 1, 5000), lookback=_int(q, "lookback", 250, 1, 10_000),
                                min_scenarios=_int(q, "min_scenarios", 20, 1, 10_000))
        if sub == ["allocation"]:
            quotes, why = _quotes(ctx)
            body = allocation.propose(conn, account_key, quotes, store, now_ns=now, scope=scope,
                                      cash_reserve=q.get("cash_reserve", "0.05"), floor=q.get("floor", "0"),
                                      cap=q.get("cap", "1"), eta=_float(q, "eta", lo=0.0) or 0.0, tau=_float(q, "tau", lo=0.0),
                                      lookback=_int(q, "lookback", 250, 2, 10_000), min_obs=_int(q, "min_obs", 20, 2, 10_000))
            if store is None:
                body["history_unavailable_reason"] = store_why
            return _with_quotes(body, why)
    finally:
        if store is not None:
            store.close()
    raise LookupError(f"unknown portfolio endpoint /{'/'.join(parts)}")


ROUTES = [(("portfolio",), handle)]
