"""Shared builders for the portfolio-analytics tests, plus checks of the builders themselves.

Every price, quantity and cash amount here is a TEST FIXTURE built through the real core
(``support.Harness``: event store, journal, holds, simulated broker); nothing is market data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, FIXTURE_INSTRUMENT, Harness  # noqa: E402

from unknown.analytics.snapshots import SnapshotStore, record_eod  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.instruments import Instrument  # noqa: E402
from unknown.store.db import connect  # noqa: E402

F = Fixed.parse
SEC = 1_000_000_000
DAY = 86_400 * SEC
SECOND_INSTRUMENT = Instrument.from_json({**FIXTURE_INSTRUMENT.to_json(), "instrument_id": "FIX-EQ-2", "symbol": "FIXB",
                                          "name": "Fixture Equity B (test only)"})


def harness(test: unittest.TestCase, **kw) -> Harness:
    h = Harness(**kw)
    test.addCleanup(h.close)
    return h


def ro(test: unittest.TestCase, h: Harness) -> sqlite3.Connection:
    conn = connect(h.db_path, readonly=True)
    test.addCleanup(conn.close)
    return conn


def snapshot_store(test: unittest.TestCase) -> SnapshotStore:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    store = SnapshotStore(Path(tmp.name) / "research" / "portfolio.sqlite")
    test.addCleanup(store.close)
    return store


def quotes(h: Harness) -> dict:
    now = h.clock.now_ns()
    return {k: v.to_json(now) for k, v in h.desk.quotes.snapshot().items()}


def register_second(h: Harness) -> None:
    r = h.owner_cmd("register_instrument", instrument=SECOND_INSTRUMENT.to_json(),
                    mappings=[{"provider": "simulated", "external_id": "FIX-EQ-2"}])
    assert r.accepted, r.body


def create_session(h: Harness, sid: str = "S1") -> None:
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=sid)
    assert r.accepted, r.body
    assert h.session_state(sid) == "RUNNING", h.row("sessions", "session_id", sid)


def buy(h: Harness, sid: str, oid: str, qty: str, price: str, *, instrument_id: str = "FIX-EQ-1", bid: str | None = None,
        ask: str | None = None, fill: bool = True) -> None:
    h.quote(bid or price, ask or str(F(price) + F("0.02")), instrument_id)
    sess = h.row("sessions", "session_id", sid)
    mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])
    r = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id=sid, order_id=oid, instrument_id=instrument_id,
              side="BUY", quantity=qty, limit_price=price, deadline_ns=h.clock.now_ns() + 60 * SEC,
              policy_hash=mandate["policy_hash"])
    assert r.accepted, r.body
    h.pump()
    if fill:
        h.broker.fill(oid, F(qty), F(price))
        h.pump()
        assert h.row("orders", "order_id", oid)["state"] == "FILLED"


def db_hash(path: Path) -> str:
    """Content hash of every table of a SQLite file, read through a fresh read-only connection."""
    conn = connect(path, readonly=True)
    try:
        digest = hashlib.sha256()
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            digest.update(name.encode())
            for row in conn.execute(f"SELECT * FROM {name} ORDER BY 1, 2"):
                digest.update(json.dumps(list(row), default=str).encode())
        return digest.hexdigest()
    finally:
        conn.close()


def eod(test: unittest.TestCase, h: Harness, store: SnapshotStore, conn: sqlite3.Connection, prices: dict[str, str],
        *, spread: str = "0.02") -> dict:
    """Quote each instrument at ``prices`` (bid) now and record an end-of-day valuation snapshot."""
    for iid, bid in prices.items():
        h.quote(bid, str(F(bid) + F(spread)), iid)
    return record_eod(conn, store, ACCOUNT, quotes(h), now_ns=h.clock.now_ns())


def booted(test: unittest.TestCase, *, authorize: str = "300.00", session: bool = True, **kw) -> Harness:
    h = harness(test, **kw)
    h.bootstrap(authorize=authorize)
    if session:
        create_session(h)
    return h


class FixtureBuilderTest(unittest.TestCase):
    def test_buy_through_the_core_keeps_ledger_invariants(self):
        h = booted(self)
        register_second(h)
        buy(h, "S1", "B1", "4", "10.00")
        buy(h, "S1", "B2", "3", "8.00", instrument_id="FIX-EQ-2")
        self.assertEqual(h.verify(), [])
        lots = h.desk.store.query("SELECT instrument_id, open_qty FROM lots ORDER BY instrument_id")
        self.assertEqual([(r["instrument_id"], r["open_qty"]) for r in lots], [("FIX-EQ-1", "4"), ("FIX-EQ-2", "3")])

    def test_db_hash_changes_only_with_content(self):
        h = booted(self, session=False)
        a = db_hash(h.db_path)
        self.assertEqual(a, db_hash(h.db_path))
        create_session(h)
        self.assertNotEqual(a, db_hash(h.db_path))


if __name__ == "__main__":
    unittest.main()
