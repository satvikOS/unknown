"""Read-only market-data routes: IBKR quote status and entitlements, stored research bars, contract verification.

Endpoints (GET, owner session required by the server; nothing here writes):

* ``/api/marketdata/status``: the IBKR market-data session's status file. It holds the delivered type, the
  entitlement and unavailable states, the line budget, pacing and the audit log (CAP-MKT-01, CAP-IBKR-08,
  CAP-IBKR-12, CAP-DATA-10). It is joined with the core's in-memory quotes to compute mid, spread, spread_bps, age
  and staleness, each with a formula id.
* ``/api/marketdata/bars``: point-in-time bars from the research bars table. Only rows with
  ``available_time <= as_of_ns`` are returned, plus lineage (CAP-MKT-04, CAP-MKT-05, DAT-02, DAT-05).
* ``/api/marketdata/bars/series``: the stored series. ``/api/marketdata/bars/gaps``: recorded coverage gaps.
* ``/api/marketdata/contracts``: the latest contract resolution per instrument (CAP-INS-04 verification table).

Live bus (ADR-0018; wire format docs/design/marketdata-stream-contract.md; the SSE stream itself is served by
``server.py`` because it is not a JSON response):

* ``/api/marketdata/snapshot?keys=``: the latest ``quote`` object per streaming key (same objects as the stream).
* ``/api/marketdata/latency``: p50, p99 and max per stage (source_to_recv, recv_to_pub, pub_to_write).
* ``/api/marketdata/trades?key=&before_ns=&limit=``: time & sales history from the tick store (newest first).
* ``/api/marketdata/sources``: configured sources, capabilities, data types, line budgets and lines in use, routes.

When the source is absent, every endpoint returns an explicit ``unavailable_reason`` and a ``reason`` code
(docs/design/unavailable-values.md), never placeholder data. Rows flagged FIXTURE (test data) are never served: there
is no option to include them (ADR-0020).

This module never imports the IBKR adapter package or ``ibapi`` (tests/test_ibkr_adapter.py enforces that outside
``broker/ibkr``). It reads only the files that package writes: the status file name and the table names below
must match ``broker/ibkr/marketdata.py`` and ``history.py`` / ``contracts.py``, and tests/test_ibkr_marketdata.py
checks that they do.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ...domain.fixed import Fixed, Rounding
from ...domain.unavailable import classify

STATUS_FILE_NAME = "ibkr-marketdata-status.json"
STATUS_STALE_AFTER_MS = 60_000          # the owning service rewrites the file on every maintain() cycle
RESEARCH_DB = ("research", "research.sqlite")
OPEN_END = 2 ** 62
MAX_LIMIT = 10_000

FORMULAS = {
    "mid": "mkt.mid@1 = (ask + bid) / 2 [quote currency per share] (book eq. 2.1; CAP-MKT-01)",
    "spread": "mkt.spread@1 = ask - bid [quote currency per share]",
    "spread_bps": "mkt.spread_bps@1 = 10^4 x spread / mid [basis points], 4 decimals, half-even",
    "age_ms": "mkt.age_ms@1 = now - receipt_time [ms] (receipt-time age: IBKR L1 carries no provider timestamp)",
    "stale": "mkt.stale@1 = age_ms > budget_ms[use] for use in display, signal, pre_trade (owner-set budgets)",
    "quote_state": "mkt.quote_state@1 = crossed if bid > ask; locked if bid = ask; one_sided; empty; normal",
}


# ============================================================================ helpers
def _now_ns() -> int:
    return time.time_ns()


def _research_db(ctx) -> Path:
    return Path(ctx.paths.root).joinpath(*RESEARCH_DB)


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _int(q: dict, key: str, default: int | None = None) -> int | None:
    if key not in q or q[key] == "":
        return default
    try:
        return int(q[key])
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None


def _bool(q: dict, key: str, default: bool) -> bool:
    if key not in q:
        return default
    v = str(q[key]).lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    raise ValueError(f"{key} must be 0 or 1")


def _fx(text) -> Fixed | None:
    if text is None:
        return None
    try:
        return Fixed.parse(str(text))
    except ValueError:
        return None


def quote_analytics(quote: dict, budgets: dict, now_ns: int) -> dict:
    """CAP-MKT-01 analytics for one core quote (JSON from ``Quote.to_json``); exact decimals, never floats."""
    bid, ask = _fx(quote.get("bid")), _fx(quote.get("ask"))
    out: dict = {"inputs": {"bid": quote.get("bid"), "ask": quote.get("ask"), "last": quote.get("last"),
                            "currency": quote.get("currency"), "source": quote.get("source"),
                            "receipt_time_ns": quote.get("receipt_time_ns"), "event_time_ns": quote.get("event_time_ns"),
                            "delayed": quote.get("delayed")},
                 "formulas": FORMULAS, "units": {"mid": quote.get("currency"), "spread": quote.get("currency"),
                                                 "spread_bps": "bps", "age_ms": "ms"}}
    if bid is None and ask is None:
        state = "empty"
    elif bid is None or ask is None:
        state = "one_sided"
    elif bid > ask:
        state = "crossed"
    elif bid == ask:
        state = "locked"
    else:
        state = "normal"
    out["quote_state"] = state
    if bid is not None and ask is not None and bid <= ask and bid.is_positive():
        mid = (bid + ask).div(2, max(bid.scale, ask.scale) + 1, rounding=Rounding.HALF_EVEN)
        spread = ask - bid
        out["mid"] = mid.to_str()
        out["spread"] = spread.to_str()
        out["spread_bps"] = (spread * 10_000).div(mid, 4, rounding=Rounding.HALF_EVEN).to_str()
    else:
        reason = {"crossed": "crossed quote (bid > ask)", "empty": "no bid and no ask",
                  "one_sided": "one side missing"}.get(state, "non-positive bid")
        out["mid"] = out["spread"] = out["spread_bps"] = None
        out["missing_reason"] = reason
        out["reason"] = "NOT_APPLICABLE" if state == "crossed" else "NO_DATA"
    receipt = quote.get("receipt_time_ns")
    out["age_ms"] = (now_ns - int(receipt)) // 1_000_000 if receipt is not None else None
    if budgets:
        out["stale"] = {use: (out["age_ms"] is None or out["age_ms"] > int(b)) for use, b in sorted(budgets.items())}
    else:
        out["stale"] = None
        out["stale_missing_reason"] = "owner has not set freshness budgets (CAP-MKT-01 open question)"
    out["live"] = not quote.get("delayed", True) and str(quote.get("source", "")).startswith("ibkr:live:")
    return out


# ============================================================================ status
def status(ctx, q: dict) -> dict:
    now = _now_ns()
    path = Path(ctx.paths.run) / STATUS_FILE_NAME
    base = {"provider": "ibkr", "now_ns": now, "status_file": str(path), "formulas": FORMULAS,
            "evidence_level": "offline-tested; NOT paper-verified (ADR-0001)"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**base, "available": False, "reason": "NOT_CONNECTED",
                "unavailable_reason": "the IBKR market-data session is not running: no status file has been written "
                                      "(it is written by IbkrMarketDataSession.maintain(); see "
                                      "docs/integrations/ibkr-marketdata.md)"}
    except (OSError, json.JSONDecodeError) as exc:
        return {**base, "available": False, "reason": "ERROR", "unavailable_reason": f"status file unreadable: {exc}"}
    written = int(data.get("written_at_ns") or 0)
    file_age_ms = (now - written) // 1_000_000
    file_stale = file_age_ms > STATUS_STALE_AFTER_MS
    try:
        quotes = ctx.core.query("quotes")
        quotes_reason = None
    except Exception as exc:   # core unreachable: statuses still shown, analytics explicitly unavailable
        quotes, quotes_reason = {}, f"core unreachable for quotes: {type(exc).__name__}: {exc}"
    budgets = data.get("freshness_budgets_ms") or {}
    subs = []
    stale_reason = (f"status older than {STATUS_STALE_AFTER_MS} ms: the market-data session may have stopped, so no "
                    "subscription is reported live") if file_stale else None
    for s in data.get("subscriptions", []):
        s = dict(s)
        if file_stale:
            # Fail closed: a status written by a session that may have stopped never reads as live.
            s["reported_status"], s["reported_live"] = s.get("status"), s.get("live")
            s["status"], s["live"], s["live_reason"] = "STATUS_FILE_STALE", False, stale_reason
        quote = quotes.get(s.get("instrument_id")) if isinstance(quotes, dict) else None
        if not s.get("current", True):
            s["analytics"] = None
            s["analytics_reason"] = "NOT_APPLICABLE"
            s["analytics_missing_reason"] = ("earlier subscription record of a re-subscribed instrument; the core "
                                             "quote belongs to the current record")
        elif quote is not None and str(quote.get("source", "")).startswith("ibkr:"):
            s["analytics"] = quote_analytics(quote, budgets, now)
            if file_stale:
                s["analytics"]["live"] = False
                s["analytics"]["live_reason"] = stale_reason
        else:
            s["analytics"] = None
            s["analytics_reason"] = "ERROR" if quotes_reason else "PENDING"
            s["analytics_missing_reason"] = quotes_reason or ("no IBKR quote for this instrument in the core quote "
                                                              "book (session not wired to the core, or no data yet)")
        subs.append(s)
    return {**base, "available": True, "status_file_age_ms": file_age_ms,
            "status_file_stale": file_stale, "status_file_stale_reason": stale_reason,
            "connection": data.get("connection"), "line_budget": data.get("line_budget"), "pacing": data.get("pacing"),
            "requested_market_data_type": data.get("requested_market_data_type"),
            "entitlement_error_rate": data.get("entitlement_error_rate"), "freshness_budgets_ms": budgets,
            "subscriptions": subs, "audit": data.get("audit", [])[-100:], "anomalies": data.get("anomalies", []),
            "storage": data.get("storage"), "written_at_ns": written}


# ============================================================================ bars
def _with_research(ctx, fn):
    path = _research_db(ctx)
    if not path.exists():
        return {"available": False, "reason": "NO_DATA", "unavailable_reason": f"research database {path} does not exist (no data "
                                                          "has been ingested on this machine)"}
    conn = _open_ro(path)
    try:
        return fn(conn)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {"available": False, "reason": "NO_DATA",
                    "unavailable_reason": f"no bars or contracts have been stored yet ({exc})"}
        raise
    finally:
        conn.close()


def _bar_row(r: sqlite3.Row, t_ns: int) -> dict:
    d = dict(r)
    d["quality"] = json.loads(d["quality"])
    d.pop("raw", None)
    d["units"] = {"price": d["currency"], "volume": "as reported by IBKR (unit not verified on fetched pages)",
                  "wap": d["currency"], "times": "UTC ns"}
    d["formula"] = d["bar_spec_version"]
    d["adjustment_basis_ns"] = d["known_from_ns"]
    d["adjustment_basis_after_decision"] = d["adjustment"] != "unadjusted" and d["known_from_ns"] > t_ns
    d["current_version"] = d["known_to_ns"] == OPEN_END
    d.pop("fixture", None)
    return d


def bars(ctx, q: dict) -> dict:
    instrument = q.get("instrument")
    if not instrument:
        raise ValueError("instrument is required")
    bar_size = q.get("bar_size", "1 day")
    what = q.get("what_to_show", "TRADES")
    use_rth = _bool(q, "use_rth", True)
    now = _now_ns()
    t_ns = _int(q, "as_of_ns", now)
    cutoff = _int(q, "knowledge_cutoff_ns")
    start, end = _int(q, "start_ns"), _int(q, "end_ns")
    limit = min(MAX_LIMIT, max(1, _int(q, "limit", 1000)))

    def run(conn):
        sql = ("SELECT * FROM mkt_bar WHERE instrument_id=? AND bar_size=? AND what_to_show=? AND use_rth=? "
               "AND available_time_ns<=?")
        params: list = [instrument, bar_size, what, int(use_rth), t_ns]
        if cutoff is None:
            sql += " AND known_to_ns=?"
            params.append(OPEN_END)
        else:
            sql += " AND known_from_ns<=? AND known_to_ns>?"
            params += [cutoff, cutoff]
        sql += " AND fixture=0"                  # test data is never served (ADR-0020)
        if start is not None:
            sql += " AND bar_start_ns>=?"
            params.append(start)
        if end is not None:
            sql += " AND bar_start_ns<?"
            params.append(end)
        sql += " ORDER BY bar_start_ns LIMIT ?"
        params.append(limit + 1)                 # one extra row tells whether the result was cut off
        rows = [_bar_row(r, t_ns) for r in conn.execute(sql, tuple(params))]
        truncated = len(rows) > limit
        rows = rows[:limit]
        not_yet = conn.execute("SELECT COUNT(*) FROM mkt_bar WHERE instrument_id=? AND bar_size=? AND what_to_show=? "
                               "AND use_rth=? AND known_to_ns=? AND available_time_ns>? AND fixture=0",
                               (instrument, bar_size, what, int(use_rth), OPEN_END, t_ns)).fetchone()[0]
        gaps = [dict(g) for g in conn.execute(
            "SELECT * FROM mkt_bar_gap WHERE instrument_id=? AND bar_size=? AND what_to_show=? AND use_rth=? "
            "AND fixture=0 ORDER BY gap_id", (instrument, bar_size, what, int(use_rth)))]
        out = {"available": True, "instrument_id": instrument,
               "spec": {"bar_size": bar_size, "what_to_show": what, "use_rth": use_rth},
               "as_of_ns": t_ns, "mode": "audit" if cutoff is not None else "research",
               "knowledge_cutoff_ns": cutoff, "count": len(rows), "bars": rows, "limit": limit,
               "truncated": truncated,
               **({"truncation_note": f"more than {limit} bars match; these are the earliest {limit} (ordered by "
                                      "bar start). Narrow start_ns/end_ns or raise limit."} if truncated else {}),
               "excluded_not_yet_available": not_yet, "coverage_gaps": gaps,
               "point_in_time_rule": "available_time_ns <= as_of_ns (available = bar end + settle delay); audit mode "
                                     "also requires known_from_ns <= cutoff < known_to_ns",
               "source": "IBKR reqHistoricalData (TWS API 10.51.01), stored by unknown.broker.ibkr.history"}
        if not rows:
            if not_yet:
                out["missing_reason"] = "NOT_YET_PUBLISHED: stored bars exist but none is available at as_of_ns"
            elif gaps:
                out["missing_reason"] = ("COVERAGE_GAP: IBKR cannot supply this series for the recorded ranges "
                                         f"({', '.join(sorted({g['reason'] for g in gaps}))}); see coverage_gaps")
            else:
                out["missing_reason"] = ("NO_DATA: no stored bars for this series (never fetched, or not permitted to "
                                         "be stored)")
            out["reason"] = classify(out["missing_reason"])
        return out

    return _with_research(ctx, run)


def bar_series(ctx, q: dict) -> dict:
    def run(conn):
        rows = [dict(r) for r in conn.execute(
            "SELECT instrument_id, bar_size, what_to_show, use_rth, dataset_id, bar_spec_version, adjustment, "
            "SUM(known_to_ns = ?) AS current_bars, COUNT(*) AS versions, MIN(bar_start_ns) AS first_bar_start_ns, "
            "MAX(bar_start_ns) AS last_bar_start_ns, MAX(receipt_time_ns) AS last_receipt_ns FROM mkt_bar "
            "WHERE fixture=0 GROUP BY instrument_id, bar_size, what_to_show, use_rth, dataset_id, bar_spec_version, "
            "adjustment ORDER BY instrument_id, bar_size", (OPEN_END,))]
        return {"available": True, "series": rows, "count": len(rows),
                **({} if rows else {"reason": "NO_DATA", "missing_reason": "no bars stored"})}

    return _with_research(ctx, run)


def bar_gaps(ctx, q: dict) -> dict:
    instrument = q.get("instrument")

    def run(conn):
        sql, params = "SELECT * FROM mkt_bar_gap WHERE fixture=0", []
        if instrument:
            sql += " AND instrument_id=?"
            params.append(instrument)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY gap_id", tuple(params))]
        for r in rows:
            r.pop("fixture", None)
        return {"available": True, "gaps": rows, "count": len(rows)}

    return _with_research(ctx, run)


# ============================================================================ contracts
def contracts(ctx, q: dict) -> dict:
    def run(conn):
        rows = conn.execute(
            "SELECT r.*, (SELECT MAX(v.resolved_at_ns) FROM ibkr_contract_resolution v WHERE v.instrument_id = "
            "r.instrument_id AND v.status = 'VERIFIED') AS last_verified_at_ns FROM ibkr_contract_resolution r JOIN "
            "(SELECT instrument_id, MAX(resolution_id) AS mx FROM ibkr_contract_resolution GROUP BY instrument_id) l "
            "ON r.resolution_id = l.mx"
            " WHERE r.fixture=0 ORDER BY r.instrument_id").fetchall()
        table = []
        for r in rows:
            rec = json.loads(r["record"])
            match = (rec.get("matches") or [{}])[0] if len(rec.get("matches") or []) == 1 else {}
            row = {"instrument_id": r["instrument_id"], "status": r["status"],
                   "blocks_routing": rec.get("blocks_routing", True), "broker": "ibkr", "con_id": r["con_id"],
                   "primary_exchange": match.get("primaryExchange"), "currency": match.get("currency"),
                   "min_tick": match.get("minTick"), "tick_rules": rec.get("tick_rules"),
                   "mapping_version": r["mapping_version"], "last_resolved_ns": r["resolved_at_ns"],
                   # the latest attempt that PASSED; a failed later attempt never counts as a verification
                   "last_verified_ns": r["last_verified_at_ns"],
                   "checks": rec.get("checks"), "reasons": rec.get("reasons"), "warnings": rec.get("warnings"),
                   "match_count": len(rec.get("matches") or []), "query": rec.get("query"),
                   "resolution_version": rec.get("resolution_version")}
            table.append(row)
        verified = sum(1 for t in table if t["status"] == "VERIFIED")
        return {"available": True, "contracts": table, "count": len(table),
                "verification_pass_rate": {"value": f"{verified}/{len(table)}" if table else None,
                                           "formula": "ins.verification_pass_rate@1 = listings with all checks passed / "
                                                      "listings resolved (CAP-INS-04)"},
                **({} if table else {"reason": "NO_DATA", "missing_reason": "no contract resolutions stored"})}

    return _with_research(ctx, run)


# ============================================================================ dispatch
def _bus(ctx):
    return ctx.marketdata_bus()


def live_snapshot(ctx, q: dict) -> dict:
    from ...marketdata.sse import parse_keys
    keys, refused = parse_keys(q.get("keys"))
    out = _bus(ctx).snapshot(keys)
    out["status"] = refused + out["status"]
    return out


def live_latency(ctx, q: dict) -> dict:
    bus = _bus(ctx)
    d = bus.describe()
    return {"stages": bus.latency.report(), "unit": "ns", "counters": d["counters"], "clients": d["clients"],
            "keys": d["keys"], "percentile": "nearest-rank over the most recent samples per stage"}


def live_trades(ctx, q: dict) -> dict:
    from ...marketdata.events import parse_key
    key = q.get("key") or ""
    parse_key(key)                                   # ValueError -> 400
    bus = _bus(ctx)
    if bus.store is None:
        return {"key": key, "trades": [], "reason": "NO_SOURCE", "unavailable_reason": "no tick store configured"}
    rows = bus.store.trades(key, before_ns=_int(q, "before_ns"), limit=_int(q, "limit", 500))
    return {"key": key, "trades": rows, "retention": bus.store.stats()}


def handle(ctx, parts, q, account_key, conn):
    sub = list(parts[1:])
    if sub == ["snapshot"]:
        return live_snapshot(ctx, q)
    if sub == ["latency"]:
        return live_latency(ctx, q)
    if sub == ["trades"]:
        return live_trades(ctx, q)
    if sub == ["sources"]:
        return _bus(ctx).describe()
    if sub == ["status"]:
        return status(ctx, q)
    if sub == ["bars"]:
        return bars(ctx, q)
    if sub == ["bars", "series"]:
        return bar_series(ctx, q)
    if sub == ["bars", "gaps"]:
        return bar_gaps(ctx, q)
    if sub == ["contracts"]:
        return contracts(ctx, q)
    raise LookupError("unknown market-data endpoint")


ROUTES = [(("marketdata",), handle)]
