"""Control plane: red stop, green start preflight, restart recovery, restore detection,
execution ownership, foreign activity, connectivity and loss budgets.

Requirements: RSK-03 (red stop), RSK-04 (green start preflight), RSK-05 (restart never
clears a stop; auto-resumption policy), OPS-03 (stop trading when data, authentication,
reconciliation or risk require it), EXE-01 (one accountable writer), RSK-02 (loss
budgets), REC-03 (reconciliation failure blocks entries, permits reduction).
Decisions: ADR-0010 and ADR-0011 (plus ADR-0003 #6 for restore).

Every number below is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, DEPLOYMENT, Harness, load_policy  # noqa: E402

from unknown.broker.port import ExecutionEvent, ExecutionRow
from unknown.broker.simulated import SubmitBehavior
from unknown.core.desk import Command, open_desk
from unknown.domain.calendar import WEEKDAYS_ONLY
from unknown.domain.errors import LeaseLost
from unknown.domain.fixed import Fixed
from unknown.domain.ids import IdGenerator, SequenceRandom
from unknown.domain.principal import Principal, Role
from unknown.market.quotes import Quote
from unknown.store.db import LeaseHeld

F = Fixed.parse
S = 1_000_000_000
DAY = 24 * 3600 * S
SYSTEM_ID = f"system.{DEPLOYMENT}"
OTHER_DEPLOYMENT = "aws-prod"


# --------------------------------------------------------------------- helpers
def blockers(result) -> list[str]:
    """Preflight blockers of a rejected green start."""
    assert not result.accepted, result.body
    assert result.rejected_code == "PREFLIGHT_FAILED", result.body
    return result.body["details"]["blockers"]


def failed_checks(result) -> set[str]:
    """Names of every failed pretrade check of a rejected intent."""
    assert not result.accepted, result.body
    return {c["check"] for c in result.body["details"]["decision"]["checks"] if not c["passed"]}


def check_detail(result, name: str) -> str:
    return next(c["detail"] for c in result.body["details"]["decision"]["checks"] if c["check"] == name)


def outbox(h: Harness, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM outbox WHERE order_id=? ORDER BY created_seq, outbox_id", (order_id,))]


def hold(h: Harness, order_id: str) -> dict:
    (r,) = h.desk.store.query("SELECT * FROM holds WHERE order_id=?", (order_id,))
    return dict(r)


def event_types(h: Harness) -> list[str]:
    return [r["type"] for r in h.desk.store.query("SELECT type FROM events ORDER BY seq")]


def risk(h: Harness) -> dict:
    return h.row("risk_state", "account_key", ACCOUNT)


def value(h: Harness) -> dict:
    """Run one SYSTEM valuation and return its decision body (system_value, breach, valuation_ok)."""
    r = h.desk.run_system("valuation", {"account_key": ACCOUNT})
    assert r.accepted, r.body
    return r.body


def start_session(h: Harness, session_id: str = "A") -> None:
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=session_id)
    assert r.accepted and r.body["state"] == "RUNNING", r.body


def bought(h: Harness, order_id: str, qty: str, price: str, session_id: str = "A") -> None:
    """Buy intent -> transmitted -> fully filled at the limit -> executions ingested."""
    r = h.intent(order_id, "BUY", qty, price, session_id)
    assert r.accepted, r.body
    h.pump()
    h.broker.fill(order_id, F(qty), F(price))
    h.pump()
    assert h.row("orders", "order_id", order_id)["state"] == "FILLED"


def stale_owner(h: Harness, age_s: int) -> Principal:
    return Principal("owner", Role.OWNER, authenticated_at_ns=h.clock.now_ns() - age_s * S)


class _Base(unittest.TestCase):
    def harness(self, **kw) -> Harness:
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h


# ===================================================================== red stop
class RedStopTests(_Base):
    def test_owner_stop_latches_durably_with_actor_reason_and_command(self):
        """RSK-03 / ADR-0010: one command durably sets the latch with actor, reason and command id;
        repeating it re-asserts without overwriting the original attribution."""
        h = self.harness()
        h.bootstrap()
        self.assertFalse(h.control()["stop_latched"])
        r = h.cmd("stop", h.owner, command_id="stop.first", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertTrue(r.body["first"])
        c = h.control()
        self.assertTrue(c["stop_latched"])
        self.assertEqual(c["latch_reason"], "OWNER_STOP")
        self.assertEqual(c["latched_by"], "owner")
        self.assertEqual(c["latch_command_id"], "stop.first")
        self.assertEqual(c["latched_at_ns"], h.clock.now_ns())

        h.clock.advance(5 * S)
        again = h.desk.run_system("stop", {"account_key": ACCOUNT, "reason": "RISK_MONITOR"}, command_id="stop.second")
        self.assertTrue(again.accepted, again.body)
        self.assertFalse(again.body["first"])
        c = h.control()
        self.assertEqual((c["latch_reason"], c["latched_by"], c["latch_command_id"]), ("OWNER_STOP", "owner", "stop.first"))
        types = event_types(h)
        self.assertIn("StopLatched", types)
        self.assertIn("StopReasserted", types)
        self.assertEqual(h.verify(), [])

    def test_system_risk_stop_is_attributed_to_system_and_only_owner_can_start(self):
        """RSK-03 / RSK-04: SYSTEM may latch on a risk trigger; green start is an OWNER command."""
        h = self.harness()
        h.bootstrap()
        r = h.desk.run_system("stop", {"account_key": ACCOUNT, "reason": "RISK_TRIGGER_FIXTURE"})
        self.assertTrue(r.accepted, r.body)
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"], c["latched_by"]), (True, "RISK_TRIGGER_FIXTURE", SYSTEM_ID))
        sys_start = h.desk.run_system("start", {"account_key": ACCOUNT})
        self.assertEqual(sys_start.rejected_code, "UNAUTHORIZED")
        strat_start = h.cmd("start", h.strategy, account_key=ACCOUNT)
        self.assertEqual(strat_start.rejected_code, "UNAUTHORIZED")
        strat_stop = h.cmd("stop", h.strategy, account_key=ACCOUNT)
        self.assertEqual(strat_stop.rejected_code, "UNAUTHORIZED")
        self.assertTrue(h.control()["stop_latched"])
        ok = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(ok.accepted, ok.body)
        c = h.control()
        self.assertFalse(c["stop_latched"])
        self.assertIsNone(c["latch_reason"])
        self.assertEqual(c["last_start"]["by"], "owner")
        self.assertEqual(h.verify(), [])

    def _book(self, h: Harness) -> None:
        """Position of 5, a working sell S1 (2), a working buy W1 (2) and an unsent buy R1 (1)."""
        h.bootstrap()
        start_session(h)
        bought(h, "L1", "5", "10.00")
        for oid, side, qty, px in (("S1", "SELL", "2", "10.05"), ("W1", "BUY", "2", "10.00")):
            r = h.intent(oid, side, qty, px, "A")
            self.assertTrue(r.accepted, r.body)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "ACKNOWLEDGED")
        self.assertEqual(h.row("orders", "order_id", "W1")["state"], "ACKNOWLEDGED")
        r = h.intent("R1", "BUY", "1", "10.00", "A")  # accepted, never pumped
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.row("orders", "order_id", "R1")["state"], "RESERVED")

    def test_stop_cancels_unsent_buy_locally_and_releases_its_hold(self):
        """RSK-03 / ADR-0010 step 2: an untransmitted buy is canceled without any broker call and
        its reservation is released exactly."""
        h = self.harness()
        self._book(h)
        # 100 seed - 51 payable (50 + 1.00 commission) - W1 hold 21.20 - R1 hold 11.20
        self.assertEqual(h.available("session:A"), F("16.600000"))
        self.assertEqual(F(hold(h, "R1")["amount"]), F("11.200000"))
        r = h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        r1 = h.row("orders", "order_id", "R1")
        self.assertEqual((r1["state"], r1["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertEqual([b["state"] for b in outbox(h, "R1")], ["SKIPPED"])
        self.assertEqual(hold(h, "R1")["status"], "RELEASED")
        self.assertEqual(F(hold(h, "R1")["amount"]), F("0"))
        self.assertEqual(h.available("session:A"), F("27.800000"))
        self.assertIn({"order_id": "R1", "state": "CANCELED"}, r.body["entries_canceled"])
        h.pump()
        self.assertFalse(h.broker.lookup("R1").found, "an unsent buy must never reach the broker after red")
        self.assertEqual(h.verify(), [])

    def test_stop_sends_cancel_for_working_buy_and_keeps_reservation_until_confirmed(self):
        """RSK-03 / ADR-0010 step 2 / ADR-0009: a transmitted buy gets an outbox CANCEL; the reservation
        is retained until the broker confirms the cancellation, then released exactly."""
        h = self.harness()
        self._book(h)
        r = h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertIn({"order_id": "W1", "state": "CANCEL_PENDING"}, r.body["entries_canceled"])
        w1 = h.row("orders", "order_id", "W1")
        self.assertEqual((w1["state"], w1["cancel_state"]), ("CANCEL_PENDING", "REQUESTED"))
        cancels = [b for b in outbox(h, "W1") if b["action"] == "CANCEL"]
        self.assertEqual([b["state"] for b in cancels], ["PENDING"])
        self.assertEqual(hold(h, "W1")["status"], "ACTIVE")
        self.assertEqual(F(hold(h, "W1")["amount"]), F("21.200000"))
        self.assertEqual(h.available("session:A"), F("27.800000"))
        stats = h.pump()
        self.assertEqual(stats.sent, 1)
        w1 = h.row("orders", "order_id", "W1")
        self.assertEqual((w1["state"], w1["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertEqual(hold(h, "W1")["status"], "RELEASED")
        self.assertEqual(h.available("session:A"), F("49.000000"))
        self.assertEqual(h.verify(), [])

    def test_stop_leaves_working_sells_and_still_permits_reduction(self):
        """RSK-03 / ADR-0010 on_stop_liquidation=NONE: working sells stay working, nothing new is sold
        automatically, buys are refused and owner sells of unreserved inventory remain possible."""
        h = self.harness()
        self._book(h)
        h.owner_cmd("stop", account_key=ACCOUNT)
        s1 = h.row("orders", "order_id", "S1")
        self.assertEqual((s1["state"], s1["cancel_state"]), ("ACKNOWLEDGED", "NONE"))
        self.assertEqual([b["action"] for b in outbox(h, "S1")], ["SUBMIT"])
        h.pump()
        open_at_broker = {o.client_order_id for o in h.broker.open_orders()[0]}
        self.assertEqual(open_at_broker, {"S1"})
        buy = h.intent("B9", "BUY", "1", "10.00", "A")
        self.assertEqual(buy.rejected_code, "STOP_LATCH")
        sell = h.intent("S2", "SELL", "1", "10.05", "A")
        self.assertTrue(sell.accepted, sell.body)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "S2")["state"], "ACKNOWLEDGED")
        h.broker.fill("S1", F("2"), F("10.05"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "FILLED")
        # 2 x 10.05 = 20.10 proceeds less the 1.00 commission, receivable until settlement
        self.assertEqual(h.balance("RECEIVABLE", "session:A"), F("19.100000"))
        self.assertEqual(h.balance("PAYABLE", "session:A"), F("-51.000000"))
        # No automatic liquidation order was created for the residual position.
        sells = [r for r in h.desk.store.query("SELECT order_id FROM orders WHERE json_extract(row,'$.side')='SELL'")]
        self.assertEqual(sorted(r["order_id"] for r in sells), ["S1", "S2"])
        self.assertEqual(h.verify(), [])

    def test_stop_progress_reports_open_buys_cancels_unknown_sells_and_residual_positions(self):
        """RSK-03 / ADR-0010 step 3: progress = entries_blocked, open buys, cancels pending, UNKNOWN
        orders, open sells and residual positions per owner; not flat while a position remains."""
        h = self.harness()
        self._book(h)
        r = h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertEqual(r.body["progress"], {
            "entries_blocked": True, "open_buys": 1, "cancels_pending": 1, "unknown_orders": 0, "open_sells": 1,
            "residual_positions": {"session:A": {"FIX-EQ-1": "5"}}, "reconciled_flat": False})
        h.pump()  # W1 cancel confirmed
        again = h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertEqual(again.body["progress"], {
            "entries_blocked": True, "open_buys": 0, "cancels_pending": 0, "unknown_orders": 0, "open_sells": 1,
            "residual_positions": {"session:A": {"FIX-EQ-1": "5"}}, "reconciled_flat": False})
        self.assertEqual(h.verify(), [])

    def test_stop_progress_counts_unknown_orders(self):
        """RSK-03: an order whose submission outcome is unknown is shown as UNKNOWN in stop progress."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        h.broker.script_submit(SubmitBehavior.TIMEOUT_BEFORE_ACCEPT)
        self.assertTrue(h.intent("U1", "BUY", "1", "10.00", "A").accepted)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "U1")["state"], "UNKNOWN")
        r = h.owner_cmd("stop", account_key=ACCOUNT)
        p = r.body["progress"]
        self.assertEqual((p["unknown_orders"], p["open_buys"], p["cancels_pending"]), (1, 1, 1))
        self.assertEqual(h.row("orders", "order_id", "U1")["state"], "UNKNOWN",
                         "a cancel request must not resolve an unknown outcome")
        self.assertEqual(hold(h, "U1")["status"], "ACTIVE")
        self.assertEqual(h.verify(), [])

    def test_flat_is_reported_only_after_a_reconciliation_that_covers_the_exit(self):
        """RSK-03 / ADR-0010: 'Flat' is reported only after a reconciliation shows zero positions,
        not because local orders and lots are empty. A reconciliation that predates the exit
        executions cannot show the exit."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        bought(h, "L1", "5", "10.00")
        h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertTrue(h.intent("X1", "SELL", "5", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("X1", F("5"), F("10.00"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "X1")["state"], "FILLED")
        before = h.owner_cmd("stop", account_key=ACCOUNT).body["progress"]
        self.assertEqual(before["residual_positions"], {})
        self.assertFalse(before["reconciled_flat"],
                         "only the bootstrap reconciliation exists; it predates the buy and the exit")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        after = h.owner_cmd("stop", account_key=ACCOUNT).body["progress"]
        self.assertTrue(after["reconciled_flat"])
        self.assertEqual(h.verify(), [])

    def test_stop_blocks_session_funding_until_green_start(self):
        """RSK-03 / ADR-0010 step 1: fundings that lead to entries are blocked under the latch;
        the green start retries them."""
        h = self.harness()
        h.bootstrap()
        h.owner_cmd("stop", account_key=ACCOUNT)
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A")
        self.assertTrue(r.accepted, r.body)
        s = h.row("sessions", "session_id", "A")
        self.assertEqual(s["state"], "WAITING_FUNDS")
        self.assertEqual(s["funding"]["reason"], "STOP_LATCHED:OWNER_STOP")
        self.assertEqual(h.available("unallocated"), F("300.000000"))
        self.assertEqual(h.balance("CASH", "session:A"), F("0"))
        h.quote("9.99", "10.01")
        ok = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(ok.accepted, ok.body)
        self.assertEqual(h.session_state("A"), "RUNNING")
        self.assertEqual(h.available("unallocated"), F("200.000000"))
        self.assertEqual(h.balance("CASH", "session:A"), F("100.000000"))
        self.assertEqual(h.verify(), [])


# ============================================================= restart recovery
class RestartRecoveryTests(_Base):
    def test_stop_survives_restart_and_restart_sets_recovery_hold(self):
        """RSK-05 / ADR-0010: a restart never clears a stop latch; it also sets the restart hold and
        the recovery gate. Only an owner green start after a clean reconciliation clears them."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        bought(h, "L1", "5", "10.00")
        h.owner_cmd("stop", account_key=ACCOUNT, reason="OWNER_FIXTURE_STOP")
        latched_cmd = h.control()["latch_command_id"]
        h.restart()
        c = h.control()
        self.assertTrue(c["stop_latched"])
        self.assertEqual(c["latch_reason"], "OWNER_FIXTURE_STOP")
        self.assertEqual(c["latch_command_id"], latched_cmd)
        self.assertTrue(c["resume_requires_owner"])
        self.assertEqual(c["resume_reason"], "PROCESS_RESTART")
        self.assertTrue(c["needs_reconciliation"])
        self.assertFalse(c["broker_connected"])
        h.quote("9.99", "10.01")
        pre = blockers(h.owner_cmd("start", account_key=ACCOUNT))
        self.assertIn("RECONCILIATION_REQUIRED", pre)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertTrue(h.control()["stop_latched"], "a clean reconciliation never clears a latch")
        self.assertEqual(h.intent("B2", "BUY", "1", "10.00", "A").rejected_code, "STOP_LATCH")
        ok = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(ok.accepted, ok.body)
        c = h.control()
        self.assertEqual((c["stop_latched"], c["resume_requires_owner"]), (False, False))
        self.assertTrue(h.intent("B2", "BUY", "1", "10.00", "A").accepted)
        self.assertEqual(h.verify(), [])

    def test_restart_without_stop_holds_entries_for_reconciliation_and_owner(self):
        """RSK-05 / ADR-0010 recovery gate: with auto-resume off (default), a restart blocks new
        exposure until a clean reconciliation in this process AND an owner start."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        h.restart()
        c = h.control()
        self.assertFalse(c["stop_latched"])
        self.assertEqual((c["resume_requires_owner"], c["resume_reason"], c["needs_reconciliation"]),
                         (True, "PROCESS_RESTART", True))
        h.quote("9.99", "10.01")
        r = h.intent("B1", "BUY", "1", "10.00", "A")
        self.assertTrue({"RESUME_HOLD", "RECOVERY_GATE"} <= failed_checks(r), r.body)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertTrue(h.control()["resume_requires_owner"], "auto-resume is off by default")
        r = h.intent("B1", "BUY", "1", "10.00", "A")
        self.assertEqual(failed_checks(r), {"RESUME_HOLD"})
        self.assertTrue(h.owner_cmd("start", account_key=ACCOUNT).accepted)
        self.assertTrue(h.intent("B1", "BUY", "1", "10.00", "A").accepted)
        self.assertEqual(h.verify(), [])

    def test_auto_resume_policy_clears_only_the_restart_hold_after_clean_reconciliation(self):
        """RSK-05 / ADR-0010: with auto_resume_after_clean_reconciliation=true and no latch, the
        recovery gate clears on a clean reconciliation with no owner command."""
        h = self.harness(policy=load_policy(auto_resume_after_clean_reconciliation=True))
        h.bootstrap()
        start_session(h)
        h.restart()
        c = h.control()
        self.assertFalse(c["stop_latched"])
        self.assertTrue(c["needs_reconciliation"])
        self.assertFalse(c["resume_requires_owner"])
        h.quote("9.99", "10.01")
        r = h.intent("B1", "BUY", "1", "10.00", "A")
        self.assertEqual(failed_checks(r), {"RECOVERY_GATE"})
        starts_before = event_types(h).count("StartGranted")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        r = h.intent("B1", "BUY", "1", "10.00", "A")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(event_types(h).count("StartGranted"), starts_before, "resumed without an owner start")
        self.assertEqual(h.verify(), [])

    def test_auto_resume_policy_never_clears_a_stop_latch(self):
        """RSK-05 / ADR-0010: auto-resumption is allowed only when no stop latch was set."""
        h = self.harness(policy=load_policy(auto_resume_after_clean_reconciliation=True))
        h.bootstrap()
        start_session(h)
        h.owner_cmd("stop", account_key=ACCOUNT)
        h.restart()
        h.quote("9.99", "10.01")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        c = h.control()
        self.assertTrue(c["stop_latched"])
        self.assertEqual(c["latch_reason"], "OWNER_STOP")
        self.assertEqual(h.intent("B1", "BUY", "1", "10.00", "A").rejected_code, "STOP_LATCH")
        self.assertTrue(h.owner_cmd("start", account_key=ACCOUNT).accepted)
        self.assertTrue(h.intent("B1", "BUY", "1", "10.00", "A").accepted)
        self.assertEqual(h.verify(), [])

    def test_auto_resume_policy_does_not_clear_a_never_started_account(self):
        """RSK-05 / ADR-0010: the NEW_ACCOUNT latch and hold are not a restart hold; a clean
        reconciliation after restart leaves both in place even with auto-resume on."""
        h = self.harness(policy=load_policy(auto_resume_after_clean_reconciliation=True))
        h.bootstrap(start=False)
        h.restart()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"]), (True, "NEW_ACCOUNT"))
        self.assertEqual((c["resume_requires_owner"], c["resume_reason"]), (True, "NEW_ACCOUNT"))
        self.assertEqual(h.verify(), [])


# ================================================================ green preflight
class GreenStartPreflightTests(_Base):
    def _stopped(self, h: Harness, *, position: bool = False) -> None:
        h.bootstrap()
        start_session(h)
        if position:
            bought(h, "L1", "5", "10.00")
        h.owner_cmd("stop", account_key=ACCOUNT)

    def _assert_starts(self, h: Harness) -> None:
        r = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertFalse(h.control()["stop_latched"])
        self.assertEqual(h.verify(), [])

    def test_failed_preflight_lists_every_blocker_by_name_and_keeps_latch(self):
        """RSK-04 / ADR-0010: preflight reports each blocker by name (not only the first) and the
        latch stays set; the refusal is a durable decision record."""
        h = self.harness()
        self._stopped(h, position=True)
        h.clock.advance(301 * S)                   # reconciliation and quote both age out
        h.broker.disconnect()                      # health: not connected / not authenticated
        r = h.cmd("start", stale_owner(h, 901), command_id="start.many", account_key=ACCOUNT)
        self.assertEqual(blockers(r), sorted([
            "BROKER_AUTHENTICATION_REQUIRED", "BROKER_DISCONNECTED", "CLOCK_SKEW_UNKNOWN", "MARKET_DATA_STALE:FIX-EQ-1",
            "OWNER_REAUTHENTICATION_REQUIRED", "RECONCILIATION_STALE"]))  # no server time while disconnected: fail closed
        self.assertTrue(h.control()["stop_latched"])
        (stored,) = h.desk.store.query("SELECT status FROM commands WHERE command_id='start.many'")
        self.assertEqual(stored["status"], "REJECTED")
        h.broker.reconnect()
        h.pump()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.quote("9.99", "10.01")
        self._assert_starts(h)

    def test_preflight_reports_broker_disconnected(self):
        """RSK-04 / OPS-03: a disconnected broker session blocks the green start."""
        h = self.harness()
        self._stopped(h)
        h.broker.disconnect()
        self.assertIn("BROKER_DISCONNECTED", blockers(h.owner_cmd("start", account_key=ACCOUNT)))
        h.broker.reconnect()
        h.pump()  # the disconnect event forces a reconciliation
        self.assertIn("RECONCILIATION_REQUIRED", blockers(h.owner_cmd("start", account_key=ACCOUNT)))
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self._assert_starts(h)

    def test_preflight_reports_authentication_required(self):
        """RSK-04 / OPS-03: a connected but unauthenticated broker session blocks the green start."""
        h = self.harness()
        self._stopped(h)
        h.broker.require_auth("fixture: session token expired")
        b = blockers(h.owner_cmd("start", account_key=ACCOUNT))
        self.assertIn("BROKER_AUTHENTICATION_REQUIRED", b)
        self.assertNotIn("BROKER_DISCONNECTED", b)
        h.broker.authenticate()
        h.pump()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self._assert_starts(h)

    def test_preflight_reports_unknown_orders_until_reconciled(self):
        """RSK-04 / ADR-0010: no UNKNOWN order and no open blocking exception may remain."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        h.broker.script_submit(SubmitBehavior.TIMEOUT_BEFORE_ACCEPT)
        self.assertTrue(h.intent("U1", "BUY", "1", "10.00", "A").accepted)
        h.runner.pump_outbox()
        self.assertEqual(h.row("orders", "order_id", "U1")["state"], "UNKNOWN")
        h.owner_cmd("stop", account_key=ACCOUNT)
        b = blockers(h.owner_cmd("start", account_key=ACCOUNT))
        self.assertIn("UNKNOWN_ORDERS", b)
        self.assertIn("BLOCKING_EXCEPTIONS:UNKNOWN_ORDER", b)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        u1 = h.row("orders", "order_id", "U1")
        self.assertEqual(u1["state"], "REJECTED")  # broker conclusively never received it
        self.assertEqual(hold(h, "U1")["status"], "RELEASED")
        self.assertEqual(h.available("session:A"), F("100.000000"))
        self._assert_starts(h)

    def test_preflight_reports_stale_reconciliation_after_max_age(self):
        """RSK-04 / ADR-0010: the last clean reconciliation must be newer than max_reconciliation_age
        (300 s in the fixture policy); exactly 300 s is still fresh."""
        h = self.harness()
        self._stopped(h)
        h.clock.advance(300 * S)
        self._assert_starts(h)
        h.owner_cmd("stop", account_key=ACCOUNT)
        h.clock.advance(1 * S)
        self.assertEqual(blockers(h.owner_cmd("start", account_key=ACCOUNT)), ["RECONCILIATION_STALE"])
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self._assert_starts(h)

    def test_preflight_requires_recent_owner_reauthentication(self):
        """RSK-04 / ADR-0010: the owner's authentication must be no older than reauth_max_age (900 s)."""
        h = self.harness()
        self._stopped(h)
        never = Principal("owner", Role.OWNER, authenticated_at_ns=None)
        self.assertEqual(blockers(h.cmd("start", never, account_key=ACCOUNT)), ["OWNER_REAUTHENTICATION_REQUIRED"])
        self.assertEqual(blockers(h.cmd("start", stale_owner(h, 901), account_key=ACCOUNT)),
                         ["OWNER_REAUTHENTICATION_REQUIRED"])
        self.assertTrue(h.control()["stop_latched"])
        r = h.cmd("start", stale_owner(h, 900), account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.verify(), [])

    def test_preflight_reports_stale_market_data_for_held_positions(self):
        """RSK-04 / ADR-0010: market data required by running sessions (held positions) must be fresh."""
        h = self.harness()
        self._stopped(h, position=True)
        h.clock.advance(6 * S)  # quote_max_age_ms = 5000
        self.assertEqual(blockers(h.owner_cmd("start", account_key=ACCOUNT)), ["MARKET_DATA_STALE:FIX-EQ-1"])
        h.quote("9.99", "10.01")
        self._assert_starts(h)

    def test_preflight_needs_no_market_data_without_held_positions(self):
        """RSK-04: the market data check applies to held positions, not to an empty book."""
        h = self.harness()
        self._stopped(h)
        h.clock.advance(6 * S)
        self._assert_starts(h)

    def test_preflight_reports_loss_budget_breach(self):
        """RSK-04 / RSK-02: a breached loss budget blocks the green start until it no longer holds."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        h.tick()
        bought(h, "L1", "8", "10.00")
        h.quote("9.75", "9.77")
        h.tick()
        self.assertEqual(h.control()["latch_reason"], "LOSS_BUDGET:DAILY_LOSS")
        self.assertIn("LOSS_BUDGET:DAILY_LOSS", blockers(h.owner_cmd("start", account_key=ACCOUNT)))
        h.quote("9.99", "10.01")
        h.tick()
        self.assertIsNone(risk(h)["breach"])
        self.assertTrue(h.control()["stop_latched"], "recovery of the mark never clears the latch by itself")
        self._assert_starts(h)


# ======================================================= ownership / split brain
class ExecutionOwnershipTests(_Base):
    def _ids(self, h: Harness, seed: int) -> IdGenerator:
        return IdGenerator(lambda: h.clock.now_ns() // 1_000_000, SequenceRandom(seed))

    def _open(self, h: Harness, deployment: str, seed: int, **kw):
        d = open_desk(h.db_path, h.clock, self._ids(h, seed), deployment_id=deployment, quotes=None,
                      health=h.health, calendars={"WEEKDAYS": WEEKDAYS_ONLY}, **kw)
        self.addCleanup(d.store.close)
        return d

    def test_second_writer_cannot_open_live_database_and_stale_writer_is_fenced(self):
        """EXE-01 / ADR-0011 #1: a second process cannot take the writer lease while it is live, and a
        process whose epoch was superseded cannot write."""
        h = self.harness()
        h.bootstrap()
        with self.assertRaises(LeaseHeld):
            open_desk(h.db_path, h.clock, self._ids(h, 5 * 10**12), deployment_id=OTHER_DEPLOYMENT, quotes=None, health=h.health)
        other = self._open(h, OTHER_DEPLOYMENT, 6 * 10**12, takeover=True)
        self.assertEqual(other.store.epoch, h.desk.store.epoch + 1)
        with self.assertRaises(LeaseLost):
            h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertEqual(other.verify(), [])

    def test_second_deployment_cannot_acquire_execution_ownership_while_held(self):
        """EXE-01 / ADR-0011 #2: ownership is acquired only by owner command, never while another
        deployment holds it; transfer requires a latched stop, a clean reconciliation and a release."""
        h = self.harness()
        h.bootstrap()
        h.desk.store.close()
        h.clock.advance(31 * S)  # the mac-dev process is gone; its lease is stale
        other = self._open(h, OTHER_DEPLOYMENT, 8 * 10**12)
        owner = Principal("owner", Role.OWNER, authenticated_at_ns=h.clock.now_ns())
        r = other.execute(Command("acq.other.1", "acquire_execution_ownership", owner,
                                  {"account_key": ACCOUNT, "deployment_id": OTHER_DEPLOYMENT}))
        self.assertEqual(r.rejected_code, "OWNED_ELSEWHERE")
        r = other.execute(Command("acq.other.2", "acquire_execution_ownership", owner,
                                  {"account_key": ACCOUNT, "deployment_id": DEPLOYMENT}))
        self.assertEqual(r.rejected_code, "WRONG_DEPLOYMENT")
        r = other.execute(Command("acq.other.3", "acquire_execution_ownership", other.system(),
                                  {"account_key": ACCOUNT, "deployment_id": OTHER_DEPLOYMENT}))
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        r = other.execute(Command("start.other.1", "start", owner, {"account_key": ACCOUNT}))
        self.assertIn("EXECUTION_OWNERSHIP_NOT_HELD", blockers(r))
        own = json.loads(other.store.query("SELECT row FROM execution_ownership WHERE account_key=?",
                                           (ACCOUNT,))[0]["row"])
        self.assertEqual((own["deployment_id"], own["epoch"], own["status"]), (DEPLOYMENT, 1, "HELD"))
        other.store.close()

        # Audited transfer: the holder latches, reconciles and releases; then the other side acquires.
        h.clock.advance(31 * S)
        h.restart()
        self.assertEqual(h.owner_cmd("release_execution_ownership", account_key=ACCOUNT).rejected_code, "STOP_REQUIRED")
        h.owner_cmd("stop", account_key=ACCOUNT)
        self.assertEqual(h.owner_cmd("release_execution_ownership", account_key=ACCOUNT).rejected_code,
                         "RECONCILIATION_REQUIRED")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        rel = h.owner_cmd("release_execution_ownership", account_key=ACCOUNT)
        self.assertTrue(rel.accepted, rel.body)
        self.assertEqual((rel.body["certificate"]["deployment_id"], rel.body["certificate"]["epoch"]), (DEPLOYMENT, 1))
        h.desk.store.close()
        h.clock.advance(31 * S)
        third = self._open(h, OTHER_DEPLOYMENT, 9 * 10**12)
        owner = Principal("owner", Role.OWNER, authenticated_at_ns=h.clock.now_ns())
        r = third.execute(Command("acq.other.4", "acquire_execution_ownership", owner,
                                  {"account_key": ACCOUNT, "deployment_id": OTHER_DEPLOYMENT}))
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body["epoch"], 2)
        self.assertEqual(third.verify(), [])

    def test_transmitted_orders_carry_deployment_epoch_tag(self):
        """EXE-01 / ADR-0011 #3: every transmitted order carries <deployment>:<epoch>:<client-order-id>."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        self.assertTrue(h.intent("B1", "BUY", "1", "10.00", "A").accepted)
        h.pump()
        (row,) = h.broker.open_orders()[0]
        self.assertEqual(row.order_ref, f"{DEPLOYMENT}:1:B1")
        self.assertEqual(h.verify(), [])


# ============================================================ foreign activity
class ForeignActivityTests(_Base):
    def _assert_foreign_latched(self, h: Harness) -> None:
        c = h.control()
        self.assertTrue(c["stop_latched"])
        self.assertEqual(c["latch_reason"], "FOREIGN_ACTIVITY")
        self.assertEqual(c["latched_by"], SYSTEM_ID)
        foreign = [e for e in h.open_exceptions() if e["kind"] == "FOREIGN_ACTIVITY"]
        self.assertEqual(len(foreign), 1)
        self.assertTrue(foreign[0]["blocking"])
        self.assertEqual(foreign[0]["severity"], "critical")
        h.quote("9.99", "10.01")
        self.assertEqual(h.intent("B1", "BUY", "1", "10.00", "A").rejected_code, "STOP_LATCH")
        self.assertIn("BLOCKING_EXCEPTIONS:FOREIGN_ACTIVITY", blockers(h.owner_cmd("start", account_key=ACCOUNT)))
        self.assertEqual(h.verify(), [])

    def test_foreign_open_order_tagged_by_another_deployment_latches_stop(self):
        """EXE-01 / ADR-0011 #3: an open order whose tag names another deployment is foreign activity;
        it opens a blocking exception and latches the stop. Recovery needs owner resolution + start."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        h.broker.foreign_order("FIX-EQ-1", "BUY", F("1"), F("10.00"), order_ref=f"{OTHER_DEPLOYMENT}:3:Z1")
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        self.assertIn("FOREIGN_ACTIVITY", [d["kind"] for d in rec.body["differences"]])
        self._assert_foreign_latched(h)
        h.reconcile()  # observing it again does not duplicate the case
        self.assertEqual(len([e for e in h.open_exceptions() if e["kind"] == "FOREIGN_ACTIVITY"]), 1)
        h.broker.expire_day_orders()
        (case,) = [e for e in h.open_exceptions() if e["kind"] == "FOREIGN_ACTIVITY"]
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=case["case_id"],
                                    note="fixture: other deployment's order expired").accepted)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertTrue(h.owner_cmd("start", account_key=ACCOUNT).accepted)
        self.assertEqual(h.verify(), [])

    def test_untagged_foreign_open_order_latches_stop(self):
        """EXE-01 / ADR-0011 #3: an open order that names no deployment is foreign activity."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        h.broker.foreign_order("FIX-EQ-1", "SELL", F("1"), F("10.00"), order_ref=None)
        self.assertEqual(h.reconcile().body["status"], "DIFFERENCES")
        self._assert_foreign_latched(h)

    def test_foreign_execution_latches_stop_and_is_booked_to_external(self):
        """EXE-01 / ADR-0011 #3 / ADR-0006: an execution tagged by another deployment latches the stop
        with a blocking exception, and the fact is booked (never discarded) under owner external."""
        h = self.harness()
        h.bootstrap(authorize="250.00")  # leaves 50.00 owned by outside to cover external activity
        start_session(h)
        row = ExecutionRow(exec_key="sim:SIM:FX1", provider_exec_id="FX1", client_order_id=None,
                           order_ref=f"{OTHER_DEPLOYMENT}:4:Z9", broker_order_id="OTHER-9", instrument_id="FIX-EQ-1",
                           provider_ref="FIX-EQ-1", side="BUY", quantity=F("1"), price=F("10.00"), currency="USD",
                           exec_time_ns=h.clock.now_ns())
        h.runner.apply_event(ExecutionEvent(row))
        (case,) = [e for e in h.open_exceptions() if e["kind"] == "FOREIGN_ACTIVITY"]
        self.assertIn(OTHER_DEPLOYMENT, case["detail"]["reason"])
        lots = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM lots")]
        self.assertEqual([(l["owner"], l["open_qty"]) for l in lots], [("external", "1")])
        self.assertEqual(h.available("outside"), F("40.000000"))
        self.assertEqual(h.available("session:A"), F("100.000000"))
        self._assert_foreign_latched(h)


# ================================================================ connectivity
class ConnectivityTests(_Base):
    def test_disconnect_event_forces_reconciliation_before_entries(self):
        """OPS-03 / ADR-0010 / REC-03: a disconnect requires a fresh clean reconciliation before any new
        entry or transmission; the system stays alive (incomplete reconciliation, no latch)."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        self.assertTrue(h.intent("B1", "BUY", "1", "10.00", "A").accepted)  # RESERVED, not yet sent
        h.broker.disconnect()
        h.runner.drain_events()
        c = h.control()
        self.assertEqual((c["broker_connected"], c["needs_reconciliation"], c["stop_latched"]), (False, True, False))
        stats = h.runner.pump_outbox()
        self.assertEqual((stats.sent, stats.skipped), (0, 1))
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "RESERVED")
        self.assertEqual(failed_checks(h.intent("B2", "BUY", "1", "10.00", "A")), {"RECOVERY_GATE"})
        inc = h.reconcile()
        self.assertEqual(inc.body["status"], "INCOMPLETE")
        self.assertTrue(h.control()["needs_reconciliation"])
        h.broker.reconnect()
        h.runner.drain_events()
        c = h.control()
        self.assertEqual((c["broker_connected"], c["needs_reconciliation"]), (True, True),
                         "reconnecting alone does not clear the recovery requirement")
        self.assertEqual(failed_checks(h.intent("B2", "BUY", "1", "10.00", "A")), {"RECOVERY_GATE"})
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "ACKNOWLEDGED")
        self.assertTrue(h.intent("B2", "BUY", "1", "10.00", "A").accepted)
        self.assertFalse(h.control()["stop_latched"])
        self.assertEqual(h.verify(), [])


# ================================================================ loss budgets
class LossBudgetTests(_Base):
    def test_daily_loss_breach_latches_stop(self):
        """RSK-02 / ADR-0010: the daily loss trigger (3.00) latches the stop as a SYSTEM risk stop.
        8 bought at 10.00 + 1.00 commission: P&L = 8 x bid - 81.00."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        self.assertEqual(F(value(h)["system_value"]), F("300.000000"))  # first valuation of the day: P&L 0
        bought(h, "L1", "8", "10.00")
        h.quote("9.76", "9.78")
        v = value(h)
        # 200 unallocated + session (100 - 81 payable + 8 x 9.76) = 297.08: P&L -2.92, inside 3.00
        self.assertEqual(F(v["system_value"]), F("297.080000"))
        self.assertIsNone(v["breach"])
        self.assertIsNone(risk(h)["breach"])
        self.assertFalse(h.control()["stop_latched"])
        h.quote("9.75", "9.77")
        v = value(h)
        self.assertEqual(F(v["system_value"]), F("297.000000"))
        self.assertEqual(v["breach"], "DAILY_LOSS")
        r = risk(h)
        self.assertEqual(F(r["cumulative_pnl"]), F("-3.000000"))
        self.assertEqual(F(r["day_start_pnl"]), F("0"))
        self.assertEqual(r["breach"], "DAILY_LOSS")
        self.assertEqual(r["breach_at_ns"], h.clock.now_ns())
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"], c["latched_by"]),
                         (True, "LOSS_BUDGET:DAILY_LOSS", SYSTEM_ID))
        self.assertTrue({"STOP_LATCH", "LOSS_BUDGET"} <= failed_checks(h.intent("B2", "BUY", "1", "9.76", "A")))
        self.assertTrue(h.intent("S1", "SELL", "8", "9.75", "A").accepted, "reduction stays possible")
        self.assertEqual(h.verify(), [])

    def test_cumulative_loss_is_measured_from_high_water_so_gains_do_not_refill(self):
        """RSK-02 / valuation.py loss-budget resolution: cumulative loss = high-water P&L - current P&L.
        Day 1 gains raise the high water to +2.20; on day 2 P&L is -12.84, only 12.84 below the
        authorized capital (inside 15.00) but 15.04 below the high water: the stop latches."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        h.tick()
        bought(h, "L1", "8", "10.00")
        h.quote("10.40", "10.42")
        h.tick()
        r = risk(h)
        self.assertEqual((F(r["cumulative_pnl"]), F(r["pnl_high_water"])), (F("2.200000"), F("2.200000")))
        self.assertIsNone(r["breach"])
        h.clock.advance(DAY)  # next risk day: the daily trigger restarts from the day's first valuation
        h.quote("8.52", "8.54")
        h.tick()
        r = risk(h)
        self.assertEqual(F(r["cumulative_pnl"]), F("-12.840000"))
        self.assertEqual(F(r["pnl_high_water"]), F("2.200000"))
        self.assertEqual(F(r["day_start_pnl"]), F("-12.840000"), "no daily loss on day 2")
        self.assertEqual(r["breach"], "CUMULATIVE_LOSS_FROM_HIGH_WATER")
        c = h.control()
        self.assertEqual((c["stop_latched"], c["latch_reason"]), (True, "LOSS_BUDGET:CUMULATIVE_LOSS_FROM_HIGH_WATER"))
        self.assertEqual(h.verify(), [])

    def test_valuation_with_stale_mark_fails_closed(self):
        """RSK-02 / OPS-03: if a held position lacks a fresh bid the valuation is incomplete,
        valuation_ok is false and buys are refused (sells remain possible) until a complete valuation."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        bought(h, "L1", "5", "10.00")
        h.tick()
        self.assertTrue(risk(h)["valuation_ok"])
        h.clock.advance(6 * S)
        h.tick()
        r = risk(h)
        self.assertFalse(r["valuation_ok"])
        self.assertEqual(r["stale_instruments"], ["FIX-EQ-1"])
        h.quote("9.99", "10.01")  # fresh quote, but no complete valuation yet
        buy = h.intent("B2", "BUY", "1", "10.00", "A")
        self.assertEqual(buy.rejected_code, "LOSS_BUDGET")
        self.assertEqual(check_detail(buy, "LOSS_BUDGET"), "VALUATION_STALE")
        self.assertIn("VALUATION_STALE", blockers(h.owner_cmd("start", account_key=ACCOUNT)))
        self.assertTrue(h.intent("S1", "SELL", "1", "10.00", "A").accepted)
        h.tick()
        self.assertTrue(risk(h)["valuation_ok"])
        # 9.99, not 10.00: a buy at 10.00 would cross our own working sell S1 (SELF_CROSS).
        self.assertTrue(h.intent("B2", "BUY", "1", "9.99", "A").accepted)
        self.assertEqual(h.verify(), [])

    def test_delayed_quote_cannot_mark_a_position(self):
        """RSK-02 / DAT: a delayed quote never counts as a fresh mark; valuation fails closed."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        bought(h, "L1", "5", "10.00")
        now = h.clock.now_ns()
        h.desk.quotes.put(Quote("FIX-EQ-1", F("9.99"), F("10.01"), None, "USD", "fixture:delayed", now, now + 1,
                                delayed=True, fixture=True))
        h.tick()
        self.assertFalse(risk(h)["valuation_ok"])
        self.assertEqual(h.verify(), [])

    def test_reserved_buy_is_not_transmitted_while_valuation_is_stale(self):
        """RSK-02 / OPS-03: exposure-increasing orders are refused while valuation_ok is false; the
        transmission of an accepted-but-unsent buy is the moment exposure increases, so it must be
        held back exactly like under a latch or loss breach (outbox_begin re-checks entry gates)."""
        h = self.harness()
        h.bootstrap()
        start_session(h)
        bought(h, "L1", "5", "10.00")
        h.tick()
        self.assertTrue(h.intent("B2", "BUY", "1", "10.00", "A").accepted)
        h.clock.advance(6 * S)
        h.tick()
        self.assertFalse(risk(h)["valuation_ok"])
        h.pump()
        self.assertNotEqual(h.row("orders", "order_id", "B2")["state"], "ACKNOWLEDGED",
                            "a buy reached the broker while valuation was failing closed")
        self.assertFalse(h.broker.lookup("B2").found)
        self.assertEqual(h.verify(), [])


# =================================================================== restore
class RestoreDetectionTests(_Base):
    def _restore(self, h: Harness, backup: Path) -> Harness:
        """Stop the live process, then reopen the OLD copy beside the live <db>.epoch marker."""
        h.desk.store.close()
        shutil.copyfile(f"{h.db_path}.epoch", f"{backup}.epoch")
        h2 = Harness(db_path=backup, broker=h.broker, clock=h.clock, seed=7 * 10**12)  # ids disjoint from h's
        h2.n = 1000
        self.addCleanup(h2.desk.store.close)
        return h2

    def _assert_restored(self, h2: Harness) -> None:
        c = h2.control()
        self.assertTrue(c["stop_latched"])
        self.assertEqual(c["latch_reason"], "RESTORED_FROM_BACKUP")
        kinds = {e["kind"]: e for e in h2.open_exceptions()}
        self.assertIn("RESTORED_FROM_BACKUP", kinds)
        self.assertTrue(kinds["RESTORED_FROM_BACKUP"]["blocking"])
        b1 = h2.row("orders", "order_id", "B1")
        self.assertEqual(b1["state"], "UNKNOWN")
        rows = {b["action"]: b["state"] for b in outbox(h2, "B1")}
        # The restore latch goes through latch_stop, which also pulls every open buy (review finding 3).
        self.assertEqual(rows, {"SUBMIT": "NEEDS_LOOKUP", "CANCEL": "PENDING"})
        self.assertIn("UNKNOWN_ORDER", kinds)
        self.assertEqual(hold(h2, "B1")["status"], "ACTIVE", "the reservation is kept while the outcome is unknown")
        h2.pump()
        submit = next(b for b in outbox(h2, "B1") if b["action"] == "SUBMIT")
        self.assertEqual(submit["state"], "NEEDS_LOOKUP", "an order that may have been sent after the backup is never resent")
        self.assertEqual(h2.verify(), [])

    def _trade_after_backup(self, h: Harness) -> Path:
        h.bootstrap()
        start_session(h)
        self.assertTrue(h.intent("B1", "BUY", "2", "10.00", "A").accepted)
        backup = Path(h.tmp.name) / "backup.sqlite"
        h.desk.store.backup_to(backup)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "ACKNOWLEDGED")
        return backup

    def test_restore_of_backup_taken_in_current_process_is_detected(self):
        """ADR-0010 / ADR-0003 #6 / RSK-05: reopening an older copy of the database (backup, trading
        continued, then the old copy reopened beside the same <db>.epoch marker) latches the stop,
        opens RESTORED_FROM_BACKUP and treats in-flight orders as UNKNOWN."""
        h = self.harness()
        backup = self._trade_after_backup(h)
        h.broker.fill("B1", F("2"), F("10.00"))
        h.pump()
        h2 = self._restore(h, backup)
        self._assert_restored(h2)

    def test_restore_of_backup_from_before_a_restart_is_detected_and_recovers(self):
        """ADR-0010 / ADR-0003 #6 / RSK-05: same as above with a restart between backup and restore;
        recovery needs reconciliation of broker activity since the backup, owner resolution and start."""
        h = self.harness()
        backup = self._trade_after_backup(h)
        h.restart()
        h.broker.fill("B1", F("2"), F("10.00"))
        h2 = self._restore(h, backup)
        self._assert_restored(h2)
        rec = h2.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        b1 = h2.row("orders", "order_id", "B1")
        self.assertEqual((b1["state"], b1["cum_qty"]), ("FILLED", "2"))
        self.assertEqual(h2.balance("PAYABLE", "session:A"), F("-21.000000"))
        h2.quote("9.99", "10.01")
        b = blockers(h2.owner_cmd("start", account_key=ACCOUNT))
        self.assertIn("BLOCKING_EXCEPTIONS:RESTORED_FROM_BACKUP", b)
        (case,) = [e for e in h2.open_exceptions() if e["kind"] == "RESTORED_FROM_BACKUP"]
        self.assertTrue(h2.owner_cmd("resolve_exception", case_id=case["case_id"],
                                     note="restore drill: broker activity since backup reconciled").accepted)
        ok = h2.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(ok.accepted, ok.body)
        self.assertEqual(h2.verify(), [])


if __name__ == "__main__":
    unittest.main()
