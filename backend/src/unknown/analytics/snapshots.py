"""Daily valuation snapshotter (CAP-PORT-01, CAP-PORT-04, CAP-PORT-10 backtest inputs).

Records end-of-day NAV per system bucket (each ``owner:<o>`` that is system-owned, so
every session and every protected bucket), for the ``system`` and ``account`` scopes,
together with the quotes and marks used, the positions valued, and the external flows
known at that instant. The store is a separate research SQLite file
(default ``<UNKNOWN_HOME>/research/portfolio.sqlite``); the core database is only read.

History is append-only. Re-valuing a date later (for example after a late commission)
inserts a new run that supersedes the earlier one; the earlier run is kept. Reads are
point in time: a run is visible at decision time t only if ``created_at_ns <= t`` and
``as_of_ns <= t``, and for each valuation date the latest visible run wins. A series as
of t therefore never contains a valuation or revision that did not exist at t.

  PYTHONPATH=src python3 -m unknown.analytics.snapshots record --account SIM-1 [--home DIR]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core import ledger
from ..domain.calendar import local_date
from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed
from ..domain.money import currency
from .common import ACCOUNT, SYSTEM, Scope, frac, read_snapshot
from .flows import cumulative, external_flows
from .formulas import ref
from .holdings import book_marks, load_book, owner_currencies, owner_summary, positions, scope_totals

DEFAULT_EOD_MAX_AGE_MS = 900_000
EOD_BUDGET_NOTE = ("end-of-day valuation freshness budget (analytics convention, not a trading limit): a mark older than "
                   "this at the snapshot instant is stale and the bucket has no fresh NAV for that day")

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS valuation_run (
  run_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, valuation_date TEXT NOT NULL, timezone TEXT NOT NULL,
  as_of_ns INTEGER NOT NULL, created_at_ns INTEGER NOT NULL, core_seq INTEGER NOT NULL, base_currency TEXT NOT NULL,
  mark_basis TEXT NOT NULL, max_age_ms INTEGER NOT NULL, formulas TEXT NOT NULL, fixture INTEGER NOT NULL,
  supersedes TEXT, content_hash TEXT NOT NULL, note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_by_date ON valuation_run(account_key, valuation_date, created_at_ns);
CREATE TABLE IF NOT EXISTS scope_value (
  run_id TEXT NOT NULL REFERENCES valuation_run(run_id), scope TEXT NOT NULL, currency TEXT NOT NULL,
  nav_fresh TEXT, nav_marked TEXT, liquid_value TEXT NOT NULL, market_value_fresh TEXT NOT NULL,
  market_value_stale TEXT NOT NULL, valuation_status TEXT NOT NULL, cum_external_flow TEXT NOT NULL,
  day_external_flow TEXT NOT NULL, detail TEXT NOT NULL,
  PRIMARY KEY (run_id, scope, currency)
);
CREATE TABLE IF NOT EXISTS run_quote (
  run_id TEXT NOT NULL REFERENCES valuation_run(run_id), instrument_id TEXT NOT NULL, quote TEXT,
  mark TEXT, mark_status TEXT NOT NULL, mark_basis TEXT NOT NULL, reason TEXT, fixture INTEGER NOT NULL,
  PRIMARY KEY (run_id, instrument_id)
);
CREATE TABLE IF NOT EXISTS run_position (
  run_id TEXT NOT NULL REFERENCES valuation_run(run_id), owner TEXT NOT NULL, instrument_id TEXT NOT NULL,
  currency TEXT NOT NULL, quantity TEXT NOT NULL, multiplier TEXT NOT NULL, cost_basis TEXT NOT NULL,
  mark TEXT, market_value TEXT, mark_status TEXT NOT NULL,
  PRIMARY KEY (run_id, owner, instrument_id)
);
CREATE TABLE IF NOT EXISTS run_flow (
  run_id TEXT NOT NULL REFERENCES valuation_run(run_id), scope TEXT NOT NULL, entry_id TEXT NOT NULL,
  currency TEXT NOT NULL, time_ns INTEGER NOT NULL, amount TEXT NOT NULL, kind TEXT NOT NULL, event_type TEXT NOT NULL,
  memo TEXT NOT NULL,
  PRIMARY KEY (run_id, scope, entry_id, currency)
);
"""


class SnapshotConflict(RuntimeError):
    """The same valuation instant was recorded before with different content."""


@dataclass(frozen=True)
class ValuationPoint:
    run_id: str
    valuation_date: str
    as_of_ns: int
    created_at_ns: int
    currency: str
    nav: Fixed | None
    cum_flow: Fixed
    day_flow: Fixed
    status: str
    fixture: bool

    def to_json(self) -> dict:
        return {"run_id": self.run_id, "date": self.valuation_date, "as_of_ns": self.as_of_ns,
                "created_at_ns": self.created_at_ns, "currency": self.currency,
                "nav": self.nav.to_str() if self.nav is not None else None, "cum_external_flow": self.cum_flow.to_str(),
                "day_external_flow": self.day_flow.to_str(), "valuation_status": self.status, "fixture": self.fixture}


def default_path(root: Path) -> Path:
    return Path(root) / "research" / "portfolio.sqlite"


# ------------------------------------------------------------------ build
def recorded_scopes(summaries: list[dict], pos) -> list[Scope]:
    owners = sorted({s["owner"] for s in summaries} | {p.owner for p in pos})
    return [ACCOUNT, SYSTEM] + [Scope("owner", o) for o in owners if ledger.is_system_owner(o)]


def build_valuation(conn: sqlite3.Connection, account_key: str, quotes, *, now_ns: int,
                    max_age_ms: int = DEFAULT_EOD_MAX_AGE_MS, mark_basis: str = "bid", tz: str | None = None) -> dict:
    """Value every recorded scope at ``now_ns`` from the core (read-only) and a quote snapshot.

    The back-dating check, the projections and the flows are read inside ONE read transaction (review fix: they were
    three separate reads, so an event committed by the live core between them could enter the balances but not the
    flows, breaking the P&L identity and the TWR flow adjustment for that day)."""
    with read_snapshot(conn):
        return _build_valuation(conn, account_key, quotes, now_ns=now_ns, max_age_ms=max_age_ms, mark_basis=mark_basis, tz=tz)


def _build_valuation(conn: sqlite3.Connection, account_key: str, quotes, *, now_ns: int, max_age_ms: int, mark_basis: str,
                     tz: str | None) -> dict:
    last = conn.execute("SELECT MAX(recorded_time_ns) AS t FROM events").fetchone()["t"]
    if last is not None and last > now_ns:
        raise ValueError("the valuation instant precedes events already in the core: balances at that instant are not "
                         "reconstructible from the projections, so a back-dated valuation is refused")
    book = load_book(conn, account_key)
    tz = tz or (book.policy.limits.risk_day_timezone if book.policy else "UTC")
    vdate = local_date(now_ns, tz).isoformat()
    marks = book_marks(book, quotes, now_ns=now_ns, basis=mark_basis, max_age_ms=max_age_ms)
    pos = positions(book, marks)
    summaries = [owner_summary(book, o, c, pos) for o, c in owner_currencies(book, pos)]
    scopes = []
    for scope in recorded_scopes(summaries, pos):
        totals = scope_totals(summaries, scope)
        flows = external_flows(conn, account_key, scope, until_ns=now_ns)
        cum = cumulative(flows)
        day = [f for f in flows if local_date(f.time_ns, tz).isoformat() == vdate]
        day_tot = cumulative(day)
        if scope.kind == "owner" and not day and not any(p.owner == scope.owner for p in pos) and all(
                Fixed.parse(t["liquid_value"]).is_zero() for t in totals.values()):
            continue  # an emptied bucket (e.g. a closed session) is not re-recorded every day; its last flow day was
        for ccy in sorted(set(totals) | set(cum)):
            scale = currency(ccy).ledger_scale
            t = totals.get(ccy)
            zero = Fixed(0, scale).to_str()
            scopes.append({
                "scope": scope.label, "currency": ccy,
                "nav_fresh": t["nav_fresh"] if t else zero, "nav_marked": t["nav_marked"] if t else zero,
                "liquid_value": t["liquid_value"] if t else zero,
                "market_value_fresh": t["market_value_fresh"] if t else zero,
                "market_value_stale": t["market_value_stale_last_mark"] if t else zero,
                "valuation_status": t["valuation_status"] if t else "complete",
                "cum_external_flow": cum.get(ccy, Fixed(0, scale)).to_str(),
                "day_external_flow": day_tot.get(ccy, Fixed(0, scale)).to_str(),
                "detail": {"stale_instruments": t["stale_instruments"] if t else [],
                           "unpriced_instruments": t["unpriced_instruments"] if t else [], "flow_note": scope.flow_note()},
                "flows": [f.to_json(tz) for f in day if f.currency == ccy],
            })
    fixture = any(m.fixture for m in marks.values()) or any(
        bool(book.instruments[p.instrument_id].attributes.get("fixture")) for p in pos if p.instrument_id in book.instruments)
    return {
        "account_key": account_key, "valuation_date": vdate, "timezone": tz, "as_of_ns": now_ns,
        "core_seq": book.core_seq, "base_currency": book.base_currency, "mark_basis": mark_basis, "max_age_ms": max_age_ms,
        "formulas": [ref(f) for f in ("port.mark", "port.market_value", "port.nav", "port.external_flow")],
        "fixture": fixture,
        "quotes": [{"instrument_id": i, "quote": m.quote, "mark": m.price.to_str() if m.price is not None else None,
                    "mark_status": m.status, "mark_basis": m.basis, "reason": m.reason, "fixture": m.fixture}
                   for i, m in sorted(marks.items())],
        "positions": [{"owner": p.owner, "instrument_id": p.instrument_id, "currency": p.currency,
                       "quantity": p.quantity.to_str(), "multiplier": p.multiplier.to_str(), "cost_basis": p.cost.to_str(),
                       "mark": p.mark.price.to_str() if p.mark.price is not None else None,
                       "market_value": p.market_value.to_str() if p.market_value is not None else None,
                       "mark_status": p.mark.status} for p in pos],
        "scopes": scopes,
        "note": EOD_BUDGET_NOTE,
    }


# ------------------------------------------------------------------ store
class SnapshotStore:
    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = str(path)
        if readonly:
            if not Path(self.path).exists():
                raise FileNotFoundError(f"no valuation snapshot store at {self.path}")
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None, timeout=5)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ write
    def record(self, valuation: dict, *, created_at_ns: int | None = None) -> dict:
        """Append a valuation run. Same instant + same content -> UNCHANGED; same instant + different content ->
        SnapshotConflict; a later instant on an already valued date -> REVISION superseding the latest run."""
        created = valuation["as_of_ns"] if created_at_ns is None else created_at_ns
        if created < valuation["as_of_ns"]:
            raise ValueError("a snapshot cannot be created before its valuation instant")
        content_hash = sha256_hex(canonical_json(valuation))
        key, vdate = valuation["account_key"], valuation["valuation_date"]
        run_id = f"val:{key}:{vdate}:{valuation['as_of_ns']}"
        existing = self.conn.execute("SELECT content_hash FROM valuation_run WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None:
            if existing["content_hash"] == content_hash:
                return {"run_id": run_id, "outcome": "UNCHANGED"}
            raise SnapshotConflict(f"{run_id} already recorded with different content")
        prev = self.conn.execute("SELECT run_id FROM valuation_run WHERE account_key=? AND valuation_date=? "
                                 "ORDER BY created_at_ns DESC, as_of_ns DESC LIMIT 1", (key, vdate)).fetchone()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT INTO valuation_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (run_id, key, vdate, valuation["timezone"], valuation["as_of_ns"], created, valuation["core_seq"],
                               valuation["base_currency"], valuation["mark_basis"], valuation["max_age_ms"],
                               json.dumps(valuation["formulas"]), int(valuation["fixture"]),
                               prev["run_id"] if prev else None, content_hash, valuation["note"]))
            for q in valuation["quotes"]:
                self.conn.execute("INSERT INTO run_quote VALUES (?,?,?,?,?,?,?,?)",
                                  (run_id, q["instrument_id"], json.dumps(q["quote"], sort_keys=True) if q["quote"] else None,
                                   q["mark"], q["mark_status"], q["mark_basis"], q["reason"], int(q["fixture"])))
            for p in valuation["positions"]:
                self.conn.execute("INSERT INTO run_position VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (run_id, p["owner"], p["instrument_id"], p["currency"], p["quantity"], p["multiplier"],
                                   p["cost_basis"], p["mark"], p["market_value"], p["mark_status"]))
            for s in valuation["scopes"]:
                self.conn.execute("INSERT INTO scope_value VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (run_id, s["scope"], s["currency"], s["nav_fresh"], s["nav_marked"], s["liquid_value"],
                                   s["market_value_fresh"], s["market_value_stale"], s["valuation_status"],
                                   s["cum_external_flow"], s["day_external_flow"], json.dumps(s["detail"], sort_keys=True)))
                for f in s["flows"]:
                    self.conn.execute("INSERT INTO run_flow VALUES (?,?,?,?,?,?,?,?,?)",
                                      (run_id, s["scope"], f["entry_id"], f["currency"], f["time_ns"], f["amount"], f["kind"],
                                       f["event_type"], f["memo"]))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"run_id": run_id, "outcome": "REVISION" if prev else "NEW", "supersedes": prev["run_id"] if prev else None}

    # ------------------------------------------------------------ read (point in time)
    def visible_runs(self, account_key: str, *, as_of_ns: int) -> list[sqlite3.Row]:
        """Latest run per valuation date among runs that existed at ``as_of_ns`` (no look-ahead)."""
        rows = self.conn.execute("SELECT * FROM valuation_run WHERE account_key=? AND created_at_ns<=? AND as_of_ns<=? "
                                 "ORDER BY valuation_date, created_at_ns, as_of_ns", (account_key, as_of_ns, as_of_ns)).fetchall()
        best: dict[str, sqlite3.Row] = {}
        for r in rows:
            best[r["valuation_date"]] = r
        return [best[d] for d in sorted(best)]

    def series(self, account_key: str, scope: str, currency_code: str, *, as_of_ns: int, nav_field: str = "nav_fresh",
               start_date: str | None = None, end_date: str | None = None) -> list[ValuationPoint]:
        if nav_field not in ("nav_fresh", "nav_marked"):
            raise ValueError("nav_field must be nav_fresh or nav_marked")
        out = []
        for run in self.visible_runs(account_key, as_of_ns=as_of_ns):
            d = run["valuation_date"]
            if (start_date and d < start_date) or (end_date and d > end_date):
                continue
            v = self.conn.execute("SELECT * FROM scope_value WHERE run_id=? AND scope=? AND currency=?",
                                  (run["run_id"], scope, currency_code)).fetchone()
            if v is None:
                continue
            nav = v[nav_field]
            out.append(ValuationPoint(run["run_id"], d, run["as_of_ns"], run["created_at_ns"], currency_code,
                                      Fixed.parse(nav) if nav is not None else None, Fixed.parse(v["cum_external_flow"]),
                                      Fixed.parse(v["day_external_flow"]), v["valuation_status"], bool(run["fixture"])))
        return out

    def flows(self, run_id: str, scope: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM run_flow WHERE run_id=? AND scope=? ORDER BY time_ns, entry_id",
                                                   (run_id, scope))]

    def run_positions(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM run_position WHERE run_id=? ORDER BY owner, instrument_id",
                                                   (run_id,))]

    def mark_history(self, account_key: str, *, as_of_ns: int, instrument_ids: list[str] | None = None,
                     fresh_only: bool = True) -> dict[str, list[tuple[str, Fraction]]]:
        """Per-instrument (date, mark) from visible runs. Stale marks are excluded by default: repeating a stale
        price would manufacture zero returns and understate risk."""
        out: dict[str, list[tuple[str, Fraction]]] = {}
        for run in self.visible_runs(account_key, as_of_ns=as_of_ns):
            for q in self.conn.execute("SELECT instrument_id, mark, mark_status FROM run_quote WHERE run_id=?", (run["run_id"],)):
                if q["mark"] is None or (fresh_only and q["mark_status"] != "fresh"):
                    continue
                if instrument_ids is not None and q["instrument_id"] not in instrument_ids:
                    continue
                out.setdefault(q["instrument_id"], []).append((run["valuation_date"], frac(Fixed.parse(q["mark"]))))
        return out


def record_eod(conn: sqlite3.Connection, store: SnapshotStore, account_key: str, quotes, *, now_ns: int,
               max_age_ms: int = DEFAULT_EOD_MAX_AGE_MS, mark_basis: str = "bid", tz: str | None = None) -> dict:
    valuation = build_valuation(conn, account_key, quotes, now_ns=now_ns, max_age_ms=max_age_ms, mark_basis=mark_basis, tz=tz)
    result = store.record(valuation, created_at_ns=now_ns)
    return {**result, "valuation_date": valuation["valuation_date"], "scopes": len(valuation["scopes"]),
            "fixture": valuation["fixture"],
            "incomplete": sorted({s["scope"] for s in valuation["scopes"] if s["valuation_status"] != "complete"})}


def main(argv: list[str] | None = None) -> None:
    import time
    from ..service.ipc import CoreClient, IpcError
    from ..service.paths import Paths
    from ..store.db import connect
    ap = argparse.ArgumentParser(description="Record an end-of-day valuation snapshot into the research store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--account", required=True)
    rec.add_argument("--home", default=None)
    rec.add_argument("--max-age-ms", type=int, default=DEFAULT_EOD_MAX_AGE_MS)
    rec.add_argument("--basis", default="bid", choices=("bid", "mid", "last"))
    a = ap.parse_args(argv)
    paths = Paths(Path(a.home).expanduser()) if a.home else Paths.default()
    try:
        quotes = CoreClient(paths.socket, paths.token("api")).query("quotes")
    except (IpcError, OSError, FileNotFoundError) as exc:
        quotes = {}
        print(json.dumps({"warning": f"core unreachable, valuing without quotes (positions will be unpriced): {exc}"}))
    conn = connect(paths.db(a.account), readonly=True)
    store = SnapshotStore(default_path(paths.root))
    try:
        print(json.dumps(record_eod(conn, store, a.account, quotes, now_ns=time.time_ns(), max_age_ms=a.max_age_ms,
                                    mark_basis=a.basis), indent=2))
    finally:
        store.close()
        conn.close()


if __name__ == "__main__":
    main()
