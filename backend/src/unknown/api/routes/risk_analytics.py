"""Portfolio risk analytics from real data only (CAP-PORT-03, CAP-PORT-05, CAP-PORT-11; ADR-0020).

  GET /api/risk/analytics?scope=&benchmark=SPY.ARCX&lookback=252&min_obs=60&shocks=-0.05,-0.10,-0.20&top_k=5

Read-only: positions come from the request's read-only core connection and the core quote book; daily bars come from
the research bar store (``<UNKNOWN_HOME>/research/research.sqlite``, written by bar sources such as the IBKR history
adapter). The response joins three read models, each figure carrying its formula id and its inputs' as-of:

* ``market_risk``: historical volatility, beta and correlation versus the benchmark ETF, portfolio beta and volatility,
  a correlation matrix, concentration by issuer and owner bucket, and uniform and beta-propagated scenario P&L;
* ``exposure``: the existing exposure report (sector by SEC SIC division, currency, owner, instrument, HHI);
* ``quote_snapshot``: where the marks came from, or why there are none.

Without stored bars every statistic is null with a reason code; nothing is filled in.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ...analytics import exposure, holdings, market_risk
from ...analytics.common import parse_scope

KEY = re.compile(r"^[A-Z0-9][A-Z0-9.\-/ ]{0,30}$")


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


def research_db(ctx) -> Path:
    return Path(ctx.paths.root) / "research" / "research.sqlite"


def handle(ctx, parts: list[str], q: dict, account_key: str, conn) -> dict:
    if parts != ["risk", "analytics"]:
        raise LookupError(f"unknown risk analytics endpoint /{'/'.join(parts)}")
    bench = (q.get("benchmark") or market_risk.DEFAULT_BENCHMARK).strip().upper()
    if not KEY.fullmatch(bench):
        raise ValueError("benchmark must be a security key such as SPY.ARCX")
    lookback = _int(q, "lookback", 252, 20, 2520)
    min_obs = _int(q, "min_obs", 60, 10, 2520)
    shocks = market_risk.parse_shocks(q.get("shocks"))
    scope = parse_scope(q.get("scope"))
    now = time.time_ns()
    try:
        quotes, why = ctx.core.query("quotes"), None
    except Exception as exc:  # the core is down: positions are still read; marks are unavailable with the reason
        quotes, why = {}, f"core unreachable, no quote snapshot: {exc}"
    h = holdings.holdings(conn, account_key, quotes, now_ns=now, scope=scope)
    clf = exposure.SicClassifier(research_db(ctx))
    try:
        expo = exposure.exposure_report(h, classifier=clf, top_k=_int(q, "top_k", 5, 1, 50))
    finally:
        clf.close()
    risk = market_risk.market_risk_report(h, research_db(ctx), benchmark=bench, lookback=lookback, min_obs=min_obs,
                                          shocks=shocks, now_ns=now)
    return {"kind": "risk_analytics", "account_key": account_key, "scope": scope.label, "as_of_ns": now,
            "market_risk": risk, "exposure": expo,
            "quote_snapshot": {"source": "core quote book (IPC query 'quotes')", "unavailable_reason": why}}


ROUTES = [(("risk", "analytics"), handle)]
