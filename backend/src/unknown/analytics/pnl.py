"""Daily P&L from the journal with an exact identity check (CAP-PORT-02; ADR-0006; book Ch. 9).

Between two consecutive complete valuation snapshots a and b of a scope:

    P&L        = (NAV_b - NAV_a) - external flows                       (formula pnl.daily@1)
    explained  = realized + dividends + interest - fees - withholding - rounding
                 - suspense movement + (unrealized_b - unrealized_a)
    residual   = P&L - explained, which must be exactly zero in ledger units

The components are read from the journal postings of the scope's owners recorded in
(as_of_a, as_of_b]; credits to income codes are gains. Unrealized P&L is the snapshot's
marked value minus open lot cost, using the same rounded market values as the NAV, so the
identity is exact rather than approximate. A non-zero residual means a posting the
decomposition does not understand and is reported, never absorbed. Estimated and final fees
are shown separately (LED-07). P&L is computed from the journal, never taken from a broker."""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from ..core import ledger
from ..domain.fixed import Fixed
from ..domain.money import currency
from .common import Scope, lineage

COMPONENTS = {
    ledger.REALIZED: "realized",
    ledger.COMMISSION: "fees_final",
    ledger.FEE_ESTIMATE: "fees_estimated",
    ledger.OTHER_FEES: "other_fees",
    ledger.DIVIDEND: "dividends",
    ledger.WITHHOLDING: "withholding",
    ledger.INTEREST: "interest",
    ledger.ROUNDING: "rounding",
    ledger.SUSPENSE: "suspense_movement",
}


def journal_components(conn: sqlite3.Connection, account_key: str, scope: Scope, currency_code: str, *, after_ns: int,
                       until_ns: int) -> dict[str, Fixed]:
    """-(sum of postings) per income/expense code for in-scope owners, events recorded in (after_ns, until_ns]."""
    scale = currency(currency_code).ledger_scale
    out: dict[str, Fixed] = defaultdict(lambda: Fixed(0, scale))
    codes = ",".join("?" for _ in COMPONENTS)
    for r in conn.execute(
            f"SELECT p.account_code, p.owner, p.amount FROM postings p JOIN journal_entries je ON je.entry_id = p.entry_id "
            f"JOIN events e ON e.seq = je.event_seq WHERE je.account_key=? AND p.currency=? AND p.account_code IN ({codes}) "
            f"AND e.recorded_time_ns > ? AND e.recorded_time_ns <= ?",
            (account_key, currency_code, *COMPONENTS, after_ns, until_ns)):
        if scope.includes(r["owner"]):
            out[COMPONENTS[r["account_code"]]] -= Fixed.parse(r["amount"])
    return {name: out[name] for name in COMPONENTS.values()}


def unrealized(store, run_id: str, scope: Scope, currency_code: str) -> Fixed | None:
    scale = currency(currency_code).ledger_scale
    total = Fixed(0, scale)
    for p in store.run_positions(run_id):
        if not scope.includes(p["owner"]) or p["currency"] != currency_code:
            continue
        if p["market_value"] is None:
            return None
        total += Fixed.parse(p["market_value"]) - Fixed.parse(p["cost_basis"])
    return total


def pnl_report(store, conn: sqlite3.Connection, account_key: str, scope: Scope, currency_code: str, *, as_of_ns: int,
               start_date: str | None = None, end_date: str | None = None) -> dict:
    """Per-day decomposition between consecutive complete snapshots visible at ``as_of_ns`` (point in time)."""
    scale = currency(currency_code).ledger_scale
    pts = [p for p in store.series(account_key, scope.label, currency_code, as_of_ns=as_of_ns,
                                   start_date=start_date, end_date=end_date) if p.nav is not None]
    days, totals = [], defaultdict(lambda: Fixed(0, scale))
    for a, b in zip(pts, pts[1:]):
        comps = journal_components(conn, account_key, scope, currency_code, after_ns=a.as_of_ns, until_ns=b.as_of_ns)
        ua, ub = unrealized(store, a.run_id, scope, currency_code), unrealized(store, b.run_id, scope, currency_code)
        flows = b.cum_flow - a.cum_flow
        pnl = (b.nav - a.nav) - flows
        row = {"start": a.valuation_date, "end": b.valuation_date, "nav_start": a.nav.to_str(), "nav_end": b.nav.to_str(),
               "external_flows": flows.to_str(), "pnl": pnl.to_str(), "components": {k: v.to_str() for k, v in comps.items()}}
        if ua is None or ub is None:
            row.update(unrealized_change=None, residual=None, reason="a position was unpriced in a snapshot")
        else:
            du = ub - ua
            explained = sum(comps.values(), Fixed(0, scale)) + du
            row.update(unrealized_change=du.to_str(), residual=(pnl - explained).to_str())
            for k, v in comps.items():
                totals[k] += v
            totals["unrealized_change"] += du
        totals["pnl"] += pnl
        totals["external_flows"] += flows
        days.append(row)
    unexplained = [d for d in days if d.get("residual") not in (None,) and not Fixed.parse(d["residual"]).is_zero()]
    fixture = any(p.fixture for p in pts)
    return {"kind": "pnl", "account_key": account_key, "scope": scope.label, "currency": currency_code, "as_of_ns": as_of_ns,
            "days": days, "totals": {k: v.to_str() for k, v in totals.items()},
            "identity_holds": not unexplained and all(d.get("residual") is not None for d in days),
            "unexplained_days": unexplained,
            "conventions": {"sign": "gains and income positive, fees and losses negative",
                            "day_boundary": "consecutive end-of-day snapshots (risk-day timezone of the active policy)",
                            "fees": "estimated and final fees shown separately; a later final fee posts on its own day"},
            "fixture_data": fixture, "fixture_note": "FIXTURE quotes: test data, not market data" if fixture else None,
            "lineage": lineage(["pnl.daily", "port.nav", "port.external_flow", "port.market_value"],
                               inputs={"snapshot_runs": [p.run_id for p in pts]}, units=f"{currency_code} (ledger scale {scale})",
                               source_dates={"first": pts[0].valuation_date if pts else None,
                                             "last": pts[-1].valuation_date if pts else None, "as_of_ns": as_of_ns},
                               missing=[{"day": d["end"], "reason": d["reason"]} for d in days if d.get("reason")])}
