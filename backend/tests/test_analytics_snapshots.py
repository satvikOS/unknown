"""External flows from the core journal and the daily valuation snapshotter (CAP-PORT-01, CAP-PORT-04).

Proves: capital authorizations/deauthorizations are flows at system scope and session
funding is internal; P&L is never a flow; flows and snapshots are point in time; the
snapshotter writes only its research SQLite and never the core. All values are TEST FIXTURES."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import (DAY, SEC, booted, buy, create_session, db_hash, eod, quotes, ro,  # noqa: E402
                                     snapshot_store)
from support import ACCOUNT  # noqa: E402

from unknown.analytics.common import ACCOUNT as ACCOUNT_SCOPE, SYSTEM, parse_scope  # noqa: E402
from unknown.analytics.flows import cumulative, external_flows  # noqa: E402
from unknown.analytics.holdings import holdings  # noqa: E402
from unknown.analytics.snapshots import SnapshotConflict, build_valuation, record_eod  # noqa: E402


def amounts(flows) -> list[tuple[str, str]]:
    return [(f.event_type, f.amount.to_str()) for f in flows]


class ExternalFlowTest(unittest.TestCase):
    def setUp(self):
        self.h = booted(self, authorize="200.00", session=False)
        self.conn = ro(self, self.h)

    def test_capital_moves_are_flows_and_pnl_is_not(self):
        h = self.h
        t_auth1 = h.clock.now_ns()
        create_session(h)                                             # unallocated -> session:S1 (internal to system)
        buy(h, "S1", "B1", "5", "9.00", bid="8.99", ask="9.01")      # trade + commission: not flows
        h.clock.advance(SEC)
        assert h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="100.00").accepted
        t_auth2 = h.clock.now_ns()
        h.clock.advance(SEC)
        assert h.owner_cmd("deauthorize_capital", account_key=ACCOUNT, amount="50.00").accepted
        self.assertEqual(h.verify(), [])
        system = external_flows(self.conn, ACCOUNT, SYSTEM)
        self.assertEqual(amounts(system), [("CapitalAuthorized", "200.000000"), ("CapitalAuthorized", "100.000000"),
                                           ("CapitalDeauthorized", "-50.000000")])
        self.assertEqual(cumulative(system)["USD"].to_str(), "250.000000")
        session = external_flows(self.conn, ACCOUNT, parse_scope("session:S1"))
        self.assertEqual(amounts(session), [("SessionFunded", "100.000000")])
        account = external_flows(self.conn, ACCOUNT, ACCOUNT_SCOPE)
        self.assertEqual(amounts(account), [("OpeningBalancesImported", "300.000000")])
        # Point in time: nothing recorded after the cutoff is visible.
        self.assertEqual(amounts(external_flows(self.conn, ACCOUNT, SYSTEM, until_ns=t_auth1)),
                         [("CapitalAuthorized", "200.000000")])
        self.assertEqual(len(external_flows(self.conn, ACCOUNT, SYSTEM, until_ns=t_auth2)), 2)

    def test_flow_entries_carry_their_journal_identity(self):
        (f,) = external_flows(self.conn, ACCOUNT, SYSTEM)
        self.assertEqual(f.kind, "OWNER_TRANSFER")
        self.assertTrue(f.entry_id.startswith("je-"))
        self.assertEqual(f.to_json("America/New_York")["date"], "2026-10-20")


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self):
        self.h = booted(self)
        self.conn = ro(self, self.h)
        self.store = snapshot_store(self)
        buy(self.h, "S1", "B1", "5", "9.00", bid="8.99", ask="9.01")

    def test_records_every_system_bucket_with_quotes_and_never_writes_the_core(self):
        before = db_hash(self.h.db_path)
        r = eod(self, self.h, self.store, self.conn, {"FIX-EQ-1": "9.20"})
        self.assertEqual(r["outcome"], "NEW")
        self.assertEqual(db_hash(self.h.db_path), before)
        self.assertNotEqual(Path(self.store.path).resolve(), Path(self.h.db_path).resolve())
        scopes = {row["scope"] for row in self.store.conn.execute("SELECT scope FROM scope_value WHERE run_id=?", (r["run_id"],))}
        self.assertEqual(scopes, {"account", "system", "owner:session:S1", "owner:unallocated"})
        (q,) = self.store.conn.execute("SELECT * FROM run_quote WHERE run_id=?", (r["run_id"],)).fetchall()
        self.assertEqual((q["mark"], q["mark_status"], q["fixture"]), ("9.20", "fresh", 1))
        now = self.h.clock.now_ns()
        (pt,) = self.store.series(ACCOUNT, "owner:session:S1", "USD", as_of_ns=now)
        live = holdings(self.conn, ACCOUNT, quotes(self.h), now_ns=now, scope=parse_scope("session:S1"))
        self.assertEqual(pt.nav.to_str(), live["totals"]["USD"]["nav_fresh"])
        self.assertEqual(pt.nav.to_str(), "100.000000")   # CASH 100 - PAYABLE 46 + 5 x 9.20
        self.assertEqual(pt.cum_flow.to_str(), "100.000000")  # the funding transfer
        self.assertTrue(pt.fixture)

    def test_idempotent_revision_and_conflict(self):
        r1 = eod(self, self.h, self.store, self.conn, {"FIX-EQ-1": "9.20"})
        again = record_eod(self.conn, self.store, ACCOUNT, quotes(self.h), now_ns=self.h.clock.now_ns())
        self.assertEqual(again["outcome"], "UNCHANGED")
        valuation = build_valuation(self.conn, ACCOUNT, quotes(self.h), now_ns=self.h.clock.now_ns())
        tampered = copy.deepcopy(valuation)
        tampered["scopes"][0]["nav_fresh"] = "1.000000"
        with self.assertRaises(SnapshotConflict):
            self.store.record(tampered)
        self.h.clock.advance(60 * SEC)  # same New York date, a later valuation instant: a revision
        r2 = eod(self, self.h, self.store, self.conn, {"FIX-EQ-1": "9.30"})
        self.assertEqual((r2["outcome"], r2["supersedes"]), ("REVISION", r1["run_id"]))
        runs = self.store.visible_runs(ACCOUNT, as_of_ns=self.h.clock.now_ns())
        self.assertEqual([r["run_id"] for r in runs], [r2["run_id"]])

    def test_series_is_point_in_time(self):
        t1 = self.h.clock.now_ns()
        r1 = eod(self, self.h, self.store, self.conn, {"FIX-EQ-1": "9.20"})
        day1 = build_valuation(self.conn, ACCOUNT, quotes(self.h), now_ns=t1)
        self.h.clock.advance(DAY)
        t2 = self.h.clock.now_ns()
        eod(self, self.h, self.store, self.conn, {"FIX-EQ-1": "9.40"})
        # A day-2 core event (a funded, waiting session): the day-1 state is no longer in the projections.
        self.assertTrue(self.h.owner_cmd("create_session", account_key=ACCOUNT, session_id="S2").accepted)
        with self.assertRaises(ValueError):  # the core state of day 1 is not reconstructible on day 2
            build_valuation(self.conn, ACCOUNT, quotes(self.h), now_ns=t1 + 30 * SEC)
        # A late revision of day 1 (e.g. re-marked after a quote correction), stored on day 2: invisible before it existed.
        revised = copy.deepcopy(day1)
        revised["as_of_ns"] = t1 + 30 * SEC
        revised["scopes"][0]["nav_fresh"] = "299.000000"
        rev = self.store.record(revised, created_at_ns=t2 + 10 * SEC)
        self.assertEqual(rev["supersedes"], r1["run_id"])
        as_of_day1 = self.store.series(ACCOUNT, "system", "USD", as_of_ns=t1)
        self.assertEqual([p.run_id for p in as_of_day1], [r1["run_id"]])
        as_of_day2 = self.store.series(ACCOUNT, "system", "USD", as_of_ns=t2)
        self.assertEqual([p.run_id for p in as_of_day2][0], r1["run_id"])  # the revision did not exist yet
        later = self.store.series(ACCOUNT, "system", "USD", as_of_ns=t2 + 10 * SEC)
        self.assertEqual(later[0].run_id, rev["run_id"])
        self.assertEqual(len(later), 2)
        hist = self.store.mark_history(ACCOUNT, as_of_ns=t1)
        self.assertEqual(len(hist["FIX-EQ-1"]), 1)  # day-2 mark not visible on day 1

    def test_unpriced_day_is_recorded_as_incomplete(self):
        r = record_eod(self.conn, self.store, ACCOUNT, {}, now_ns=self.h.clock.now_ns())
        self.assertIn("system", r["incomplete"])
        (pt,) = self.store.series(ACCOUNT, "system", "USD", as_of_ns=self.h.clock.now_ns())
        self.assertIsNone(pt.nav)
        self.assertEqual(pt.status, "unpriced_positions")


if __name__ == "__main__":
    unittest.main()
