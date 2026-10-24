"""Reconciliation, opening import, external activity and evidence-based settlement.

Requirements under test (docs/requirements/register.md): REC-01, REC-02, REC-03,
REC-04 and LED-08, read through ADR-0006 (owner dimension, external activity,
settlement), ADR-0010 (recovery gate, red stop) and ADR-0012 (broker contract).

Every number here is a TEST FIXTURE. Nothing touches the network: the broker is
the deterministic SimulatedBroker (optionally with the small scripted subclass
below) and time is a ManualClock.
"""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from datetime import date

try:  # run as ``python -m unittest tests.test_reconciliation`` from backend/
    from tests.support import ACCOUNT, DEPLOYMENT, FIXTURE_INSTRUMENT, T0, Harness, load_policy
except ImportError:  # run with tests/ itself on sys.path
    from support import ACCOUNT, DEPLOYMENT, FIXTURE_INSTRUMENT, T0, Harness, load_policy

from unknown.broker.port import BrokerOrderStatus, PositionRow
from unknown.broker.simulated import SimulatedBroker
from unknown.core.desk import open_desk
from unknown.core.replay import replay_and_compare
from unknown.domain.calendar import WEEKDAYS_ONLY, BusinessCalendar
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed

F = Fixed.parse
HOUR = 3600 * 10**9
DAY = 24 * HOUR
INST = "FIX-EQ-1"
# Friday 2026-10-23 14:00 UTC = 10:00 New York (fixture time only).
FRIDAY = utc_ns(2026, 10, 23, 14, 0, 0)
HOLIDAY_CALENDAR = BusinessCalendar(
    "FIXTURE-HOLIDAYS", "1", holidays=frozenset({date(2026, 10, 26)}),
    source="test fixture: Monday 2026-10-26 treated as a settlement holiday (not a real exchange calendar)")


# --------------------------------------------------------------------------- fixtures
class ScriptedBroker(SimulatedBroker):
    """SimulatedBroker with three test-only scripts layered on its public behaviour.

    * ``extra_positions``: rows appended to every account snapshot, i.e. a
      broker-side position change that no execution explains (transfer-in,
      unrecorded corporate action, operator error at the broker).
    * ``disconnect_during``: the connection drops inside the named read
      (``account_snapshot`` or ``open_orders``) of a reconciliation run.
    * ``forget_history``: the execution-history window no longer reaches the
      trades made so far, as with a provider's limited execution history; those
      holdings are then visible only in positions and cash.
    * ``settled_source``: every cash row says its settled figure was derived
      by the adapter (ADR-0012 amendment 2026-10-24, IBKR without SettledCash).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra_positions: tuple[PositionRow, ...] = ()
        self.disconnect_during: str | None = None
        self.history_floor = 0
        self.settled_source: str | None = None

    def forget_history(self) -> None:
        self.drain_events()
        self.history_floor = int(super().executions_since(None).watermark)

    def _maybe_drop(self, call: str) -> None:
        if self.disconnect_during == call:
            self.disconnect_during = None
            self.disconnect()

    def account_snapshot(self):
        self._maybe_drop("account_snapshot")
        snap = super().account_snapshot()
        if self.extra_positions:
            snap = dataclasses.replace(snap, positions=snap.positions + self.extra_positions)
        if self.settled_source:
            snap = dataclasses.replace(snap, cash=tuple(dataclasses.replace(c, settled_source=self.settled_source)
                                                        for c in snap.cash))
        return snap

    def open_orders(self):
        self._maybe_drop("open_orders")
        return super().open_orders()

    def executions_since(self, watermark):
        if self.history_floor and (watermark is None or int(watermark) < self.history_floor):
            watermark = str(self.history_floor)
        return super().executions_since(watermark)


class CalendarHarness(Harness):
    """Harness whose desk also knows extra settlement calendars by id."""

    def __init__(self, *, calendars: dict[str, BusinessCalendar], **kwargs):
        self._extra_calendars = calendars
        super().__init__(**kwargs)

    def _open(self):
        return open_desk(self.db_path, self.clock, self.ids, deployment_id=DEPLOYMENT, quotes=None,
                         health=self.health, calendars={"WEEKDAYS": WEEKDAYS_ONLY, **self._extra_calendars})


def scripted_harness(initial_cash: str = "300.00", *, start_ns: int = T0, **kwargs) -> tuple[Harness, ScriptedBroker]:
    clock = ManualClock(start_ns)
    broker = ScriptedBroker(clock, initial_settled_cash=F(initial_cash), calendar=WEEKDAYS_ONLY, settlement_days=1)
    return Harness(clock=clock, broker=broker, **kwargs), broker


# --------------------------------------------------------------------------- helpers
def register_until_reconcile(h: Harness):
    """``Harness.bootstrap`` up to and including the first reconciliation, without asserting CLEAN."""
    assert h.owner_cmd("register_account", account_key=ACCOUNT, provider="simulated", mode="SIMULATED",
                       base_currency="USD").accepted
    r = h.owner_cmd("publish_policy", body=h.policy_body)
    assert r.accepted, r.body
    h.policy_hash = r.body["policy_hash"]
    assert h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=h.policy_hash).accepted
    assert h.owner_cmd("register_instrument", instrument=FIXTURE_INSTRUMENT.to_json(),
                       mappings=[{"provider": "simulated", "external_id": INST}]).accepted
    assert h.owner_cmd("acquire_execution_ownership", account_key=ACCOUNT, deployment_id=DEPLOYMENT).accepted
    h.connect()
    return h.reconcile()


def open_session(h: Harness, session_id: str = "A") -> None:
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=session_id)
    assert r.accepted and r.body["state"] == "RUNNING", r.body


def filled_buy(h: Harness, order_id: str, qty: str = "10", price: str = "9.00", session_id: str = "A", **fill_kw) -> None:
    """Session buy, transmitted, filled at ``price`` with the broker's default commission, and ingested."""
    h.quote("8.99", "9.01")
    r = h.intent(order_id, "BUY", qty, price, session_id)
    assert r.accepted, r.body
    h.pump()
    h.broker.fill(order_id, F(qty), F(price), **fill_kw)
    h.pump()
    assert h.row("orders", "order_id", order_id)["state"] == "FILLED"


def probe_buy(h: Harness, session_id: str = "A"):
    """A small exposure-increasing intent used to observe whether entries are permitted."""
    h.quote("9.99", "10.01")
    return h.intent(f"PROBE-{h.n + 1}", "BUY", "1", "10.00", session_id)


def failed_checks(result) -> set[str]:
    decision = result.body.get("details", {}).get("decision") or {}
    return {c["check"] for c in decision.get("checks", []) if not c["passed"]}


def open_of_kind(h: Harness, kind: str) -> list[dict]:
    return [e for e in h.open_exceptions() if e["kind"] == kind]


def lots(h: Harness) -> list[dict]:
    rows = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM lots ORDER BY rowid")]
    return [r for r in rows if not F(r["open_qty"]).is_zero()]


def settlement(h: Harness, settlement_id: str) -> dict:
    row = h.row("settlements", "settlement_id", settlement_id)
    assert row is not None, settlement_id
    return row


def advance_to(h: Harness, ns: int) -> None:
    h.clock.set(ns)


class _Base(unittest.TestCase):
    def track(self, h: Harness) -> Harness:
        self.addCleanup(h.close)
        return h

    def assertBlocked(self, h: Harness, session_id: str = "A") -> None:
        r = probe_buy(h, session_id)
        self.assertFalse(r.accepted, "an exposure-increasing intent was accepted while entries must be blocked")
        self.assertTrue(failed_checks(r) & {"RECOVERY_GATE", "OPEN_EXCEPTIONS", "STOP_LATCH"}, r.body)

    def assertPermitted(self, h: Harness, session_id: str = "A") -> None:
        r = probe_buy(h, session_id)
        self.assertTrue(r.accepted, r.body)


# =========================================================================== REC-01 / LED-08: opening import
class OpeningImportTests(_Base):
    def test_first_reconciliation_imports_preexisting_cash_as_outside(self):
        """REC-01, LED-08 (ADR-0006 OPENING): cash present before the system is owned by
        ``outside`` and booked once against OPENING; nothing is authorized implicitly."""
        h = self.track(Harness(initial_cash="1000.00"))
        rec = register_until_reconcile(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.balance("CASH", "outside"), F("1000.00"))
        self.assertEqual(h.balance("OPENING", "outside"), F("-1000.00"))
        self.assertEqual(h.available("outside"), F("1000.00"))
        self.assertEqual(h.available("unallocated"), F("0"))
        ctl = h.control()
        self.assertTrue(ctl["imported"])
        self.assertFalse(ctl["needs_reconciliation"])
        # A second reconciliation never imports again.
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.balance("OPENING", "outside"), F("-1000.00"))
        n = h.desk.store.query("SELECT COUNT(*) AS n FROM journal_entries WHERE kind='OPENING'")[0]["n"]
        self.assertEqual(n, 1)
        self.assertEqual(h.verify(), [])

    def test_derived_settled_cash_reconciles_and_the_run_names_its_source(self):
        """ADR-0012 amendment 2026-10-24: IBKR sends no SettledCash row, so the adapter reports settled =
        TotalCashValue with broker evidence of no unsettled activity. Reconciliation uses that figure like any
        settled cash and records its source on the run's event, so the terminal can show it."""
        h, broker = scripted_harness("1000.00")
        self.track(h)
        broker.settled_source = "TotalCashValue (no executions in settlement window)"
        rec = register_until_reconcile(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.balance("CASH", "outside"), F("1000.00"))
        (row,) = h.desk.store.query("SELECT payload FROM events WHERE type='ReconciliationCompleted'")
        self.assertEqual(json.loads(row["payload"])["info"]["settled_sources"], {"USD": broker.settled_source})
        broker.settled_source = None
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        last = h.desk.store.query("SELECT payload FROM events WHERE type='ReconciliationCompleted' ORDER BY seq")[-1]
        self.assertNotIn("settled_sources", json.loads(last["payload"])["info"])   # unchanged when the broker reports it
        self.assertEqual(h.verify(), [])

    def test_position_bought_manually_before_first_reconciliation_is_imported_as_outside(self):
        """REC-01, LED-08 (ADR-0006 OPENING, owner ``outside``): an account that already holds a
        position bought before this system was connected imports it, with the cash, as
        ``outside`` value; the first reconciliation is CLEAN and nothing is attributed to
        ``external`` or blocked. The broker's execution history still contains that trade
        (as a real provider's would for recent fills)."""
        h, broker = scripted_harness("1000.00")
        self.track(h)
        broker.external_trade(INST, "BUY", F("10"), F("9.00"))  # pending cash -91.00 (90 + 1.00 commission)
        rec = register_until_reconcile(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual([(l["owner"], l["open_qty"], l["open_cost"]) for l in lots(h)], [("outside", "10", "90.000000")])
        self.assertEqual(h.balance("INVENTORY", "outside", instrument_id=INST), F("90.00"))
        self.assertEqual(h.balance("CASH", "outside"), F("1000.00"))
        self.assertEqual(h.balance("PAYABLE", "outside"), F("-91.00"))
        self.assertEqual(h.available("outside"), F("909.00"))
        self.assertEqual(h.balance("INVENTORY", "external", instrument_id=INST), F("0"))
        self.assertEqual([e["kind"] for e in h.open_exceptions() if e["blocking"]], [])
        self.assertEqual(h.verify(), [])

    def test_opening_import_of_holdings_outside_history_settles_on_broker_evidence(self):
        """REC-01, REC-04, LED-08: holdings visible only in the snapshot (older than the execution
        history) are imported as ``outside`` lots, unsettled cash becomes an ``outside`` payable
        with an expected settlement, and that settlement is confirmed only when broker settled
        cash shows it."""
        h, broker = scripted_harness("1000.00")
        self.track(h)
        broker.external_trade(INST, "BUY", F("10"), F("9.00"))
        broker.forget_history()
        rec = register_until_reconcile(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        (lot,) = lots(h)
        self.assertEqual((lot["lot_id"], lot["owner"], lot["open_qty"], lot["open_cost"]),
                         ("lot:opening:FIX-EQ-1", "outside", "10", "90.000000"))
        self.assertEqual(h.balance("CASH", "outside"), F("1000.00"))
        self.assertEqual(h.balance("PAYABLE", "outside"), F("-91.00"))
        self.assertEqual(h.balance("INVENTORY", "outside", instrument_id=INST), F("90.00"))
        self.assertEqual(h.balance("OPENING", "outside"), F("-999.00"))
        stl = settlement(h, "stl:opening:USD")
        self.assertEqual((stl["owner"], stl["direction"], F(stl["amount"]), stl["expected_date"], stl["status"]),
                         ("outside", "PAY", F("91.00"), "2026-10-21", "EXPECTED"))
        self.assertEqual(h.available("outside"), F("909.00"))
        self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="300.00").accepted)
        # Expected date reached, broker has not settled yet: no timer confirms it.
        advance_to(h, T0 + DAY)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:opening:USD")["status"], "EXPECTED")
        self.assertEqual(broker.advance_settlement(), 1)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:opening:USD")["status"], "SETTLED")
        self.assertEqual(h.balance("CASH", "outside"), F("609.00"))
        self.assertEqual(h.balance("PAYABLE", "outside"), F("0"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("300.00"))
        self.assertEqual(h.verify(), [])


# =========================================================================== authorize_capital bounds
class AuthorizeCapitalTests(_Base):
    def test_authorization_bounded_by_outside_cash(self):
        """LED-04/LED-08 (ADR-0006): capital can be authorized only out of ``outside`` available cash."""
        h = self.track(Harness(initial_cash="200.00"))
        h.bootstrap(authorize="", start=False)
        r = h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="200.01")
        self.assertEqual(r.rejected_code, "INSUFFICIENT_SOURCE_CASH")
        self.assertEqual(h.balance("CASH", "outside"), F("200.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("0"))
        self.assertEqual(F(h.row("risk_state", "account_key", ACCOUNT)["authorized_total"]), F("0"))
        r = h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="200.00")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(F(r.body["authorized_total"]), F("200.00"))
        self.assertEqual(h.available("outside"), F("0"))
        self.assertEqual(h.available("unallocated"), F("200.00"))
        self.assertEqual(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="0.01").rejected_code,
                         "INSUFFICIENT_SOURCE_CASH")
        self.assertEqual(h.verify(), [])

    def test_authorization_bounded_by_policy_authorized_capital(self):
        """LED-04 (ADR-0006/0007): cumulative authorization never exceeds policy authorized_capital (300.00)."""
        h = self.track(Harness(initial_cash="1000.00"))
        h.bootstrap(authorize="", start=False)
        self.assertEqual(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="300.01").rejected_code,
                         "ABOVE_AUTHORIZED_CAPITAL")
        self.assertEqual(h.balance("CASH", "unallocated"), F("0"))
        self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="200.00").accepted)
        self.assertEqual(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="100.01").rejected_code,
                         "ABOVE_AUTHORIZED_CAPITAL")
        self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="100.00").accepted)
        self.assertEqual(h.balance("CASH", "unallocated"), F("300.00"))
        self.assertEqual(h.balance("CASH", "outside"), F("700.00"))
        # Deauthorizing frees headroom; the same amount can be authorized again.
        self.assertTrue(h.owner_cmd("deauthorize_capital", account_key=ACCOUNT, amount="50.00").accepted)
        self.assertEqual(h.balance("CASH", "outside"), F("750.00"))
        self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="50.00").accepted)
        self.assertEqual(F(h.row("risk_state", "account_key", ACCOUNT)["authorized_total"]), F("300.00"))
        self.assertEqual(h.balance("CASH", "unallocated") + h.balance("CASH", "outside"), F("1000.00"))
        self.assertEqual(h.verify(), [])

    def test_unsettled_opening_purchase_reduces_authorizable_outside_cash(self):
        """LED-03, LED-08: an imported, still-unsettled purchase is a payable of ``outside`` and is not
        authorizable cash (250.00 settled - 91.00 payable = 159.00)."""
        h, broker = scripted_harness("250.00")
        self.track(h)
        broker.external_trade(INST, "BUY", F("10"), F("9.00"))
        broker.forget_history()
        self.assertEqual(register_until_reconcile(h).body["status"], "CLEAN")
        self.assertEqual(h.available("outside"), F("159.00"))
        self.assertEqual(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="159.01").rejected_code,
                         "INSUFFICIENT_SOURCE_CASH")
        self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="159.00").accepted)
        self.assertEqual(h.available("outside"), F("0"))
        self.assertEqual(h.verify(), [])


# =========================================================================== REC-02 / LED-08: manual trades
class ExternalTradeTests(_Base):
    def test_manual_trade_is_owned_by_external_with_non_blocking_case(self):
        """REC-02, LED-08: a trade placed outside the API is recorded, owned by ``external`` (never by a
        session or unallocated), opens a NON-blocking EXTERNAL_TRADE case, and entries stay permitted."""
        h = self.track(Harness(initial_cash="1000.00"))
        h.bootstrap()
        open_session(h)
        h.broker.external_trade(INST, "BUY", F("10"), F("9.00"))
        h.pump()
        self.assertEqual([(l["owner"], l["open_qty"], l["open_cost"]) for l in lots(h)], [("external", "10", "90.000000")])
        self.assertEqual(h.balance("INVENTORY", "external", instrument_id=INST), F("90.00"))
        (case,) = open_of_kind(h, "EXTERNAL_TRADE")
        self.assertFalse(case["blocking"])
        self.assertEqual(case["owner_hint"], "external")
        self.assertEqual([e["kind"] for e in h.open_exceptions() if e["blocking"]], [])
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.available("unallocated"), F("200.00"))
        ex = h.desk.store.query("SELECT row FROM executions")
        self.assertEqual([json.loads(r["row"])["owner"] for r in ex], ["external"])
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertPermitted(h)
        self.assertEqual(h.verify(), [])

    def test_manual_trade_cash_need_including_commission_is_covered_from_outside(self):
        """REC-02, LED-08 (ADR-0006 'Manual trade ... cash need is covered by an owner transfer from
        outside'): the full cash need of the manual purchase (90.00 + 1.00 commission) reduces
        ``outside`` available capital and leaves ``external`` with nothing uncovered, before and
        after settlement."""
        h = self.track(Harness(initial_cash="1000.00"))
        h.bootstrap()  # outside 700.00, unallocated 300.00
        h.broker.external_trade(INST, "BUY", F("10"), F("9.00"))
        h.pump()
        self.assertEqual(h.available("outside"), F("609.00"))
        self.assertEqual(h.available("external"), F("0"))
        advance_to(h, T0 + DAY)
        self.assertEqual(h.broker.advance_settlement(), 1)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.balance("CASH", "external"), F("0"))
        self.assertEqual(h.balance("CASH", "outside"), F("609.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("300.00"))
        self.assertEqual(h.verify(), [])

    def test_uncoverable_manual_purchase_opens_blocking_exception(self):
        """REC-02, LED-11 (ADR-0006): with no ``outside`` cash to cover a manual purchase the fact is
        still recorded, an OVERCOMMITTED exception blocks new exposure, and system owners keep
        their balances."""
        h = self.track(Harness(initial_cash="300.00"))
        h.bootstrap()  # everything authorized: outside 0.00
        open_session(h)
        h.broker.external_trade(INST, "BUY", F("10"), F("9.00"))
        h.pump()
        self.assertEqual([(l["owner"], l["open_qty"]) for l in lots(h)], [("external", "10")])
        (case,) = open_of_kind(h, "OVERCOMMITTED")
        self.assertTrue(case["blocking"])
        self.assertEqual(case["owner_hint"], "external")
        # 90.00 notional plus the broker's 1.00 commission: the fee is part of what external owes (finding 26).
        self.assertEqual(F(case["detail"]["need"]), F("91.00"))
        self.assertEqual(F(case["detail"]["outside_available"]), F("0"))
        self.assertEqual(len(open_of_kind(h, "EXTERNAL_TRADE")), 1)
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.available("unallocated"), F("200.00"))
        self.assertBlocked(h)
        self.assertEqual(h.verify(), [])


# =========================================================================== LED-08: deposits and withdrawals
class ExternalCashFlowTests(_Base):
    def _mismatch(self, h: Harness, unexplained: str) -> dict:
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES", rec.body)
        (diff,) = rec.body["differences"]
        self.assertEqual((diff["kind"], diff["blocking"]), ("CASH_MISMATCH", True))
        self.assertEqual(F(diff["detail"]["unexplained"]), F(unexplained))
        (case,) = open_of_kind(h, "CASH_MISMATCH")
        self.assertTrue(case["blocking"])
        return case

    def test_deposit_blocks_until_owner_attributes_it_then_reconciles_clean(self):
        """LED-08, REC-01, REC-03 (ADR-0006 'unexplained deposit: CONTRIBUTED, owner outside'): an
        unexplained deposit is a blocking CASH_MISMATCH, is never booked by guess, is attributed by
        the owner with record_external_cash_flow, and the next reconciliation is CLEAN."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        h.broker.deposit(F("50.00"))
        case = self._mismatch(h, "50.00")
        self.assertTrue(h.control()["needs_reconciliation"])
        self.assertEqual(h.balance("CASH", "outside"), F("0"))
        self.assertBlocked(h)
        self.assertEqual(h.owner_cmd("resolve_exception", case_id=case["case_id"], action="record_external_cash_flow",
                                     currency="USD", amount="50.00").rejected_code, "NOTE_REQUIRED")
        r = h.owner_cmd("resolve_exception", case_id=case["case_id"], action="record_external_cash_flow",
                        currency="USD", amount="50.00", note="owner deposit (fixture)")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.balance("CASH", "outside"), F("50.00"))
        self.assertEqual(h.balance("CONTRIBUTED", "outside"), F("-50.00"))
        self.assertEqual(h.balance("CASH", "unallocated") + h.balance("CASH", "session:A"), F("300.00"))
        resolved = h.row("exceptions", "case_id", case["case_id"])
        self.assertEqual((resolved["status"], resolved["resolution"]["action"]), ("RESOLVED", "record_external_cash_flow"))
        self.assertBlocked(h)  # resolution alone is not reconciliation
        rec = h.reconcile()
        self.assertEqual((rec.body["status"], rec.body["differences"]), ("CLEAN", []))
        self.assertFalse(h.control()["needs_reconciliation"])
        self.assertEqual(h.available("outside"), F("50.00"))
        # ADR-0010: a reconciliation failure is a risk stop; clearing it needs the owner's green start.
        self.assertEqual(h.control()["latch_reason"], "RECONCILIATION_FAILURE")
        self.assertBlocked(h)
        h.quote("9.99", "10.01")
        start = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(start.accepted, start.body)
        self.assertPermitted(h)
        self.assertEqual(h.verify(), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, os.path.join(d, "replay.sqlite")), [])

    def test_withdrawal_up_to_outside_cash_is_recorded_against_outside(self):
        """LED-08 (ADR-0006 'Withdrawal: taken from outside first'): a withdrawal equal to the
        ``outside`` cash is attributed to outside; authorized capital is untouched."""
        h = self.track(Harness(initial_cash="320.00"))
        h.bootstrap()  # outside 20.00
        h.broker.withdraw(F("20.00"))
        case = self._mismatch(h, "-20.00")
        r = h.owner_cmd("resolve_exception", case_id=case["case_id"], action="record_external_cash_flow",
                        currency="USD", amount="-20.00", note="owner withdrawal (fixture)")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.balance("CASH", "outside"), F("0"))
        self.assertEqual(h.balance("CONTRIBUTED", "outside"), F("20.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("300.00"))
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.verify(), [])

    def test_withdrawal_beyond_outside_cash_is_refused_and_stays_blocking(self):
        """LED-08, REC-03 (ADR-0006 'If outside is insufficient, an exception blocks new exposure'):
        recording a withdrawal larger than ``outside`` cash is refused; the CASH_MISMATCH stays open
        and blocking and no authorized capital is silently consumed."""
        h = self.track(Harness(initial_cash="320.00"))
        h.bootstrap()  # outside 20.00
        open_session(h)
        h.broker.withdraw(F("20.01"))
        case = self._mismatch(h, "-20.01")
        r = h.owner_cmd("resolve_exception", case_id=case["case_id"], action="record_external_cash_flow",
                        currency="USD", amount="-20.01", note="owner withdrawal (fixture)")
        self.assertEqual(r.rejected_code, "WITHDRAWAL_EXCEEDS_OUTSIDE")
        self.assertEqual(h.row("exceptions", "case_id", case["case_id"])["status"], "OPEN")
        self.assertEqual(h.balance("CASH", "outside"), F("20.00"))
        self.assertEqual(h.balance("CONTRIBUTED", "outside"), F("0"))
        self.assertEqual(h.balance("CASH", "unallocated") + h.balance("CASH", "session:A"), F("300.00"))
        self.assertBlocked(h)
        # Reconciling again reports the same difference without duplicating the case.
        self.assertEqual(h.reconcile().body["status"], "DIFFERENCES")
        self.assertEqual(len(open_of_kind(h, "CASH_MISMATCH")), 1)
        self.assertEqual(h.verify(), [])


# =========================================================================== REC-01: positions, overlap, watermark
class ComparisonTests(_Base):
    def test_position_difference_is_detected_and_blocks(self):
        """REC-01, REC-03: a broker position the ledger cannot explain is a blocking
        POSITION_MISMATCH; the ledger is not silently adjusted."""
        h, broker = scripted_harness()
        self.track(h)
        h.bootstrap()
        open_session(h)
        filled_buy(h, "B1")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        broker.extra_positions = (PositionRow(INST, INST, F("5"), F("9.50"), "USD"),)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        self.assertIn({"kind": "POSITION_MISMATCH", "blocking": True,
                       "detail": {"instrument_id": INST, "broker": "15", "ledger": "10"}}, rec.body["differences"])
        (case,) = open_of_kind(h, "POSITION_MISMATCH")
        self.assertTrue(case["blocking"])
        self.assertTrue(h.control()["needs_reconciliation"])
        self.assertEqual([(l["owner"], l["open_qty"]) for l in lots(h)], [("session:A", "10")])
        self.assertBlocked(h)
        self.assertEqual(h.verify(), [])

    def test_unmapped_broker_position_is_a_blocking_difference(self):
        """REC-01: a broker position with no instrument mapping is reported, never ignored."""
        h, broker = scripted_harness()
        self.track(h)
        h.bootstrap()
        broker.extra_positions = (PositionRow(None, "CONID-FIXTURE-999", F("3"), F("1.00"), "USD"),)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        (diff,) = rec.body["differences"]
        self.assertEqual((diff["kind"], diff["blocking"]), ("POSITION_MISMATCH", True))
        self.assertEqual(diff["detail"]["unmapped_position"]["provider_ref"], "CONID-FIXTURE-999")
        self.assertEqual(len(open_of_kind(h, "POSITION_MISMATCH")), 1)
        self.assertEqual(h.verify(), [])

    def test_overlapping_executions_are_deduplicated_and_watermark_advances_only_on_clean(self):
        """REC-01: executions are re-read with overlap and deduplicated; the watermark used for the
        next read advances only after a clean, durable reconciliation."""
        h = self.track(Harness(initial_cash="1000.00"))
        h.bootstrap()
        open_session(h)
        filled_buy(h, "B1", duplicate_callbacks=2)
        trades = "SELECT COUNT(*) AS n FROM journal_entries WHERE kind='TRADE'"
        self.assertEqual(h.desk.store.query(trades)[0]["n"], 1)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.runner.last_watermark(), "1")
        h.broker.external_trade(INST, "BUY", F("1"), F("10.00"))
        h.broker.set_snapshot_incomplete(True)
        self.assertEqual(h.reconcile().body["status"], "INCOMPLETE")
        self.assertEqual(h.runner.last_watermark(), "1")
        h.broker.set_snapshot_incomplete(False)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.runner.last_watermark(), "2")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 2)
        self.assertEqual(h.desk.store.query(trades)[0]["n"], 2)
        self.assertEqual(sorted((l["owner"], l["open_qty"]) for l in lots(h)), [("external", "1"), ("session:A", "10")])
        self.assertEqual(h.verify(), [])


# =========================================================================== REC-01 / REC-03: incomplete runs
class IncompleteReconciliationTests(_Base):
    def test_incomplete_snapshot_after_restart_keeps_entries_blocked(self):
        """REC-01, REC-03 (ADR-0010 recovery gate): partial broker data never proves anything; an
        INCOMPLETE run after a restart leaves new exposure and green start blocked, and a later
        complete run clears the gate and resolves the incomplete case."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        h.restart()
        h.broker.set_snapshot_incomplete(True)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "INCOMPLETE")
        (case,) = open_of_kind(h, "RECONCILIATION_INCOMPLETE")
        self.assertFalse(case["detail"]["snapshot_complete"])
        ctl = h.control()
        self.assertTrue(ctl["needs_reconciliation"])
        self.assertNotEqual(ctl["reconciled_epoch"], h.desk.store.epoch)
        self.assertBlocked(h)
        r = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertEqual(r.rejected_code, "PREFLIGHT_FAILED")
        self.assertIn("RECONCILIATION_REQUIRED", r.body["details"]["blockers"])
        h.broker.set_snapshot_incomplete(False)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(open_of_kind(h, "RECONCILIATION_INCOMPLETE"), [])
        self.assertTrue(h.owner_cmd("start", account_key=ACCOUNT).accepted)
        self.assertPermitted(h)
        self.assertEqual(h.verify(), [])

    def test_incomplete_periodic_reconciliation_blocks_new_entries(self):
        """REC-03 ('Reconciliation failure blocks new entries'): a periodic run that cannot complete
        is a reconciliation failure, so exposure-increasing intents are refused until a complete,
        clean run."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        self.assertPermitted(h)
        h.broker.set_snapshot_incomplete(True)
        self.assertEqual(h.reconcile().body["status"], "INCOMPLETE")
        self.assertBlocked(h)
        h.broker.set_snapshot_incomplete(False)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertPermitted(h)
        self.assertEqual(h.verify(), [])

    def test_disconnect_before_reconcile_is_incomplete_and_blocks(self):
        """REC-01, REC-03: a reconciliation against a disconnected broker is INCOMPLETE and entries
        and green start are blocked; after reconnecting, a clean run restores them."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        h.broker.disconnect()
        self.assertEqual(h.reconcile().body["status"], "INCOMPLETE")
        self.assertEqual(len(open_of_kind(h, "RECONCILIATION_INCOMPLETE")), 1)
        self.assertFalse(h.control()["broker_connected"])
        self.assertBlocked(h)
        blockers = h.owner_cmd("start", account_key=ACCOUNT).body["details"]["blockers"]
        self.assertIn("BROKER_DISCONNECTED", blockers)
        self.assertIn("RECONCILIATION_REQUIRED", blockers)
        h.broker.reconnect()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(open_of_kind(h, "RECONCILIATION_INCOMPLETE"), [])
        self.assertPermitted(h)
        self.assertEqual(h.verify(), [])

    def test_disconnect_during_reconcile_is_incomplete_and_blocks_immediately(self):
        """REC-01, REC-03: the connection dropping inside a reconciliation run (here, while reading
        open orders) yields INCOMPLETE, and new entries are blocked as soon as that result is
        recorded, without waiting for the queued connectivity event to be pumped."""
        h, broker = scripted_harness()
        self.track(h)
        h.bootstrap()
        open_session(h)
        broker.disconnect_during = "open_orders"
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "INCOMPLETE")
        self.assertEqual(len(open_of_kind(h, "RECONCILIATION_INCOMPLETE")), 1)
        self.assertBlocked(h)
        h.pump()
        self.assertFalse(h.control()["broker_connected"])
        broker.reconnect()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(open_of_kind(h, "RECONCILIATION_INCOMPLETE"), [])
        self.assertEqual(h.verify(), [])


# =========================================================================== REC-03: controlled risk reduction
class RiskReductionTests(_Base):
    def test_cash_difference_blocks_buys_but_permits_reducing_sell(self):
        """REC-03 ('blocks new entries but permits controlled risk reduction'): with positions
        reconciled and only cash unexplained, a buy is refused but a sell of reconciled,
        unreserved session inventory is accepted and transmitted."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        filled_buy(h, "B1")
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.broker.deposit(F("50.00"))
        rec = h.reconcile()
        self.assertEqual([d["kind"] for d in rec.body["differences"]], ["CASH_MISMATCH"])
        self.assertBlocked(h)
        h.quote("8.99", "9.01")
        r = h.intent("S1", "SELL", "10", "9.00", "A")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        self.assertEqual(h.broker.lookup("S1").status, BrokerOrderStatus.ACKNOWLEDGED)
        self.assertEqual(h.verify(), [])

    def test_red_stop_cancels_are_transmitted_despite_reconciliation_differences(self):
        """REC-03, ADR-0010 red stop: after a reconciliation difference the stop's CANCEL for an open
        buy is still transmitted (cancelling an entry reduces risk), the broker confirms, and the
        reservation is released."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("B1", "BUY", "10", "9.00", "A").accepted)
        h.pump()
        self.assertEqual(h.broker.lookup("B1").status, BrokerOrderStatus.ACKNOWLEDGED)
        h.broker.deposit(F("50.00"))
        self.assertEqual(h.reconcile().body["status"], "DIFFERENCES")
        r = h.owner_cmd("stop", account_key=ACCOUNT, reason="owner red stop (fixture)")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        self.assertEqual(h.broker.lookup("B1").status, BrokerOrderStatus.CANCELED)
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "CANCELED")
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])


# =========================================================================== REC-04: settlement evidence
class SettlementEvidenceTests(_Base):
    def test_due_settlement_is_not_confirmed_by_the_calendar_and_late_case_opens(self):
        """REC-04: reaching the expected date confirms nothing; while the broker's settled cash does
        not show the movement the item stays EXPECTED, a non-blocking SETTLEMENT_LATE opens only
        after the grace period (2 business days, across the weekend), and broker evidence then
        confirms it."""
        h = self.track(Harness())
        h.bootstrap()  # Tuesday 2026-10-20
        open_session(h)
        filled_buy(h, "B1")
        stl = settlement(h, "stl:sim:SIM:E1")
        self.assertEqual((stl["direction"], F(stl["amount"]), stl["expected_date"], stl["status"]),
                         ("PAY", F("91.00"), "2026-10-21", "EXPECTED"))
        for day, label in ((1, "Wed"), (3, "Fri")):
            advance_to(h, T0 + day * DAY)
            self.assertEqual(h.reconcile().body["status"], "CLEAN", label)
            self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "EXPECTED", label)
            self.assertEqual(open_of_kind(h, "SETTLEMENT_LATE"), [], label)
        self.assertEqual(h.balance("CASH", "session:A"), F("100.00"))
        self.assertEqual(h.balance("PAYABLE", "session:A"), F("-91.00"))
        advance_to(h, T0 + 6 * DAY)  # Monday 2026-10-26: past Friday's grace limit
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "EXPECTED")
        (late,) = open_of_kind(h, "SETTLEMENT_LATE")
        self.assertFalse(late["blocking"])
        self.assertEqual(late["owner_hint"], "session:A")
        self.assertEqual(h.broker.advance_settlement(), 1)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "SETTLED")
        self.assertEqual(h.balance("CASH", "session:A"), F("9.00"))
        self.assertEqual(h.balance("PAYABLE", "session:A"), F("0"))
        self.assertEqual(h.verify(), [])

    def test_sale_proceeds_become_available_only_on_broker_evidence(self):
        """REC-04, LED-03: receivables are never spendable; they become cash only when broker settled
        cash equals ledger cash plus the net of due items (300.00 + 97.00 - 91.00 = 306.00)."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        filled_buy(h, "B1")
        h.quote("9.79", "9.81")
        self.assertTrue(h.intent("S1", "SELL", "10", "9.80", "A").accepted)
        h.pump()
        h.broker.fill("S1", F("10"), F("9.80"))
        h.pump()
        self.assertEqual(h.balance("RECEIVABLE", "session:A"), F("97.00"))
        self.assertEqual(h.balance("PAYABLE", "session:A"), F("-91.00"))
        self.assertEqual(h.available("session:A"), F("9.00"))
        advance_to(h, T0 + DAY)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:sim:SIM:E2")["status"], "EXPECTED")
        self.assertEqual(h.available("session:A"), F("9.00"))
        self.assertEqual(h.broker.advance_settlement(), 2)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN")
        self.assertEqual({settlement(h, s)["status"] for s in ("stl:sim:SIM:E1", "stl:sim:SIM:E2")}, {"SETTLED"})
        self.assertEqual(h.balance("CASH", "session:A"), F("106.00"))
        self.assertEqual(h.balance("RECEIVABLE", "session:A"), F("0"))
        self.assertEqual(h.available("session:A"), F("106.00"))
        self.assertEqual(h.verify(), [])

    def test_settlement_requires_exact_broker_cash_agreement(self):
        """REC-04: broker settled cash that differs from ledger + due items by even 0.01 confirms
        nothing (CASH_MISMATCH); once the owner attributes the 0.01 the same evidence confirms."""
        h = self.track(Harness())
        h.bootstrap()
        open_session(h)
        filled_buy(h, "B1")
        advance_to(h, T0 + DAY)
        self.assertEqual(h.broker.advance_settlement(), 1)
        h.broker.deposit(F("0.01"))
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        # The only blocking difference is the cash one; because nothing was confirmed, the ledger's
        # unsettled -91.00 versus the broker's 0.00 is also reported (informational).
        self.assertEqual(sorted((d["kind"], d["blocking"]) for d in rec.body["differences"]),
                         [("CASH_MISMATCH", True), ("UNSETTLED_DIFFERENCE", False)])
        (diff,) = [d for d in rec.body["differences"] if d["kind"] == "CASH_MISMATCH"]
        self.assertEqual(F(diff["detail"]["due_settlements_net"]), F("-91.00"))
        self.assertEqual(F(diff["detail"]["unexplained"]), F("0.01"))
        self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "EXPECTED")
        self.assertEqual(h.balance("CASH", "session:A"), F("100.00"))
        (case,) = open_of_kind(h, "CASH_MISMATCH")
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=case["case_id"], action="record_external_cash_flow",
                                    currency="USD", amount="0.01", note="interest credit (fixture)").accepted)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "SETTLED")
        self.assertEqual(h.balance("CASH", "session:A"), F("9.00"))
        self.assertEqual(h.balance("CASH", "outside"), F("0.01"))
        self.assertEqual(h.verify(), [])

    def test_friday_trade_settles_monday_only_when_broker_shows_it(self):
        """REC-04: a Friday purchase is expected Monday; weekend reconciliations confirm nothing and
        Monday's broker evidence confirms it."""
        h, broker = scripted_harness(start_ns=FRIDAY)
        self.track(h)
        h.bootstrap()
        open_session(h)
        filled_buy(h, "B1")
        self.assertEqual(settlement(h, "stl:sim:SIM:E1")["expected_date"], "2026-10-26")
        for day in (1, 2):  # Saturday, Sunday
            advance_to(h, FRIDAY + day * DAY)
            self.assertEqual(broker.advance_settlement(), 0)
            self.assertEqual(h.reconcile().body["status"], "CLEAN")
            self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "EXPECTED")
        advance_to(h, FRIDAY + 3 * DAY)
        self.assertEqual(broker.advance_settlement(), 1)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "SETTLED")
        self.assertEqual(h.balance("CASH", "session:A"), F("9.00"))
        self.assertEqual(h.verify(), [])

    def test_holiday_calendar_moves_expected_date_and_confirmation_waits_for_evidence(self):
        """REC-04 (calendar predicts, broker evidence confirms): under the policy's settlement
        calendar a Friday trade before a Monday holiday is expected Tuesday; reconciliations on the
        weekend and the holiday confirm nothing and raise no late case."""
        clock = ManualClock(FRIDAY)
        broker = SimulatedBroker(clock, initial_settled_cash=F("300.00"), calendar=HOLIDAY_CALENDAR, settlement_days=1)
        h = self.track(CalendarHarness(calendars={"FIXTURE-HOLIDAYS": HOLIDAY_CALENDAR}, clock=clock, broker=broker,
                                       policy=load_policy(settlement_calendar="FIXTURE-HOLIDAYS")))
        h.bootstrap()
        open_session(h)
        filled_buy(h, "B1")
        stl = settlement(h, "stl:sim:SIM:E1")
        self.assertEqual((stl["expected_date"], stl["calendar"]), ("2026-10-27", "FIXTURE-HOLIDAYS"))
        for day in (1, 3):  # Saturday, Monday (holiday)
            advance_to(h, FRIDAY + day * DAY)
            self.assertEqual(broker.advance_settlement(), 0)
            self.assertEqual(h.reconcile().body["status"], "CLEAN")
            self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "EXPECTED")
            self.assertEqual(open_of_kind(h, "SETTLEMENT_LATE"), [])
        advance_to(h, FRIDAY + 4 * DAY)  # Tuesday 2026-10-27
        self.assertEqual(broker.advance_settlement(), 1)
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(settlement(h, "stl:sim:SIM:E1")["status"], "SETTLED")
        self.assertEqual(h.balance("CASH", "session:A"), F("9.00"))
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
