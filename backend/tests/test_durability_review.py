"""Durability and performance fixes from the 2026-10-22 adversarial review.

Findings covered (numbers from the review's confirmed list):

* 39 - per-command ledger verification is incremental (``ledger.verify_touched``):
  it checks only the entries a command emitted and the keys it touched, and is
  proven equal to the full ``ledger.verify`` on a randomized command history with
  corruption injected into the touched keys;
* 16 (first part) - an observation that leaves working SELL holds above the
  shares owned opens a blocking EXECUTION_ANOMALY naming the owner, instead of
  the owner-less ledger-invariant alarm;
* 51 - the latest reconciliation is the last one recorded, not the one with the
  largest wall-clock time;
* 53 - only an OPEN *blocking* case in the *same account* excuses an owner;
* 57 - the writer lease heartbeat no longer depends on a successful cycle, and a
  fenced service stops instead of running as a zombie;
* 58 - broker events popped from the broker but not applied are kept, in order,
  and applied on the next drain; a reconciliation records INCOMPLETE instead of
  losing the run.

Every number is a TEST FIXTURE. The broker is the deterministic SimulatedBroker.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import warnings
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, T0, Harness, load_policy  # noqa: E402
from test_replay_randomized import Scenario  # noqa: E402

from unknown.broker.simulated import SimulatedBroker  # noqa: E402
from unknown.core import desk as desk_module  # noqa: E402
from unknown.core import ledger, state  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.clock import ManualClock  # noqa: E402
from unknown.domain.errors import InvariantViolation  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway.runner import event_backlog  # noqa: E402
from unknown.service import core as core_module  # noqa: E402
from unknown.service.core import CoreService, Intervals  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.store import db as db_module  # noqa: E402
from unknown.store.db import LeaseHeld, Store  # noqa: E402

F = Fixed.parse
NS = 1_000_000_000
INST = "FIX-EQ-1"


# ============================================================ finding 39
class _Abort(Exception):
    """Raised inside a write transaction to roll it back."""


class EquivalenceProbe:
    """Wraps ``ledger.verify_touched`` as the desk calls it after every command.

    At each call it (1) requires the incremental result to equal the full
    ``ledger.verify`` on the same transaction, then (2) injects one corruption
    into a key the command touched, inside a savepoint, requires both checks to
    return exactly the same problems again, and rolls the corruption back. The
    command then continues with the real, uncorrupted result."""

    KINDS = ("balance", "delete_balance", "cash_negative", "posting", "lot_cost", "lot_qty", "lot_owner",
             "delete_lot", "hold")

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.real = ledger.verify_touched
        self.calls = 0
        self.injected: Counter = Counter()
        self.detected: Counter = Counter()
        # Collected rather than raised: a reconciliation records any exception as an INCOMPLETE run.
        self.mismatches: list[str] = []

    def _compare(self, incremental: list[str], full: list[str], when: str) -> None:
        if sorted(incremental) != sorted(full):
            self.mismatches.append(f"{when}: incremental {sorted(incremental)} != full {sorted(full)}")

    def __call__(self, tx, touched):
        self.calls += 1
        result = self.real(tx, touched)
        self._compare(result, ledger.verify(tx), f"call {self.calls} before injection")
        # Lot corruptions are possible only after fills, so they are weighted up to be exercised often.
        options = [k for k in self.KINDS if self._applicable(tx, touched, k) for _ in range(4 if "lot" in k else 1)]
        if options:
            kind = self.rng.choice(options)
            tx.run("SAVEPOINT inject")
            try:
                self._inject(tx, touched, kind)
                incremental, full = self.real(tx, touched), ledger.verify(tx)
                self._compare(incremental, full, f"call {self.calls} after {kind} corruption")
                self.injected[kind] += 1
                if full:
                    self.detected[kind] += 1
            finally:
                tx.run("ROLLBACK TO inject")
                tx.run("RELEASE inject")
        return result

    # ------------------------------------------------------------ corruptions
    def _balances(self, tx, touched, codes=None):
        out = []
        for key in touched.balances:
            if codes is not None and key[1] not in codes:
                continue
            if tx.one(f"SELECT 1 FROM balances WHERE {ledger._BALANCE_KEY}", key) is not None:
                out.append(key)
        return out

    def _lots(self, tx, touched):
        return [lot_id for lot_id in touched.lots if tx.one("SELECT 1 FROM lots WHERE lot_id=?", (lot_id,))]

    def _holds(self, tx, touched):
        rows = []
        for account_key, owner, kind, unit in sorted(touched.holds):
            rows += [r["hold_id"] for r in tx.all("SELECT hold_id FROM holds WHERE account_key=? AND owner=? AND kind=? "
                                                  "AND unit=? ORDER BY hold_id", (account_key, owner, kind, unit))]
        return rows

    def _applicable(self, tx, touched, kind) -> bool:
        if kind in ("balance", "delete_balance"):
            return bool(self._balances(tx, touched))
        if kind == "cash_negative":
            return any(ledger.is_system_owner(k[2]) for k in self._balances(tx, touched, (ledger.CASH,)))
        if kind == "posting":
            return bool(touched.entries)
        if kind in ("lot_cost", "lot_qty", "lot_owner", "delete_lot"):
            return bool(self._lots(tx, touched))
        if kind == "hold":
            return bool(self._holds(tx, touched))
        raise AssertionError(kind)

    def _delta(self) -> Fixed:
        return Fixed(self.rng.choice((1, -1, 37, -250, 100_000)), 2)

    def _inject(self, tx, touched, kind) -> None:
        rng = self.rng
        if kind in ("balance", "delete_balance", "cash_negative"):
            codes = (ledger.CASH,) if kind == "cash_negative" else None
            keys = self._balances(tx, touched, codes)
            if kind == "cash_negative":
                keys = [k for k in keys if ledger.is_system_owner(k[2])]
            key = rng.choice(keys)
            if kind == "delete_balance":
                tx.run(f"DELETE FROM balances WHERE {ledger._BALANCE_KEY}", key)
                return
            old = F(tx.one(f"SELECT amount FROM balances WHERE {ledger._BALANCE_KEY}", key)["amount"])
            new = F("-1000.00") if kind == "cash_negative" else old + self._delta()
            tx.run(f"UPDATE balances SET amount=? WHERE {ledger._BALANCE_KEY}", (new.rescale(6).to_str(),) + key)
        elif kind == "posting":
            entry_id = rng.choice(touched.entries)
            rows = tx.all("SELECT line, amount FROM postings WHERE entry_id=? ORDER BY line", (entry_id,))
            row = rng.choice(rows)
            tx.run("UPDATE postings SET amount=? WHERE entry_id=? AND line=?",
                   ((F(row["amount"]) + self._delta()).rescale(6).to_str(), entry_id, row["line"]))
        elif kind in ("lot_cost", "lot_qty", "lot_owner", "delete_lot"):
            lot_id = rng.choice(self._lots(tx, touched))
            if kind == "delete_lot":
                tx.run("DELETE FROM lots WHERE lot_id=?", (lot_id,))
                return
            lot = json.loads(tx.one("SELECT row FROM lots WHERE lot_id=?", (lot_id,))["row"])
            if kind == "lot_cost":
                lot["open_cost"] = (F(lot["open_cost"]) + self._delta()).rescale(6).to_str()
            elif kind == "lot_qty":
                lot["open_qty"] = (F(lot["open_qty"]) - Fixed(rng.choice((1, 2, 50)))).to_str()
            else:
                lot["owner"] = ledger.BUFFER if lot["owner"] == ledger.UNALLOCATED else ledger.UNALLOCATED
            tx.run("UPDATE lots SET owner=?, open_qty=?, row=? WHERE lot_id=?",
                   (lot["owner"], lot["open_qty"], json.dumps(lot, sort_keys=True), lot_id))
        elif kind == "hold":
            hold_id = rng.choice(self._holds(tx, touched))
            row = tx.one("SELECT kind, amount FROM holds WHERE hold_id=?", (hold_id,))
            bump = Fixed(100_000, 2) if row["kind"] == "CASH" else Fixed(rng.choice((1, 500)))
            tx.run("UPDATE holds SET amount=?, status='ACTIVE' WHERE hold_id=?", ((F(row["amount"]) + bump).to_str(), hold_id))
        else:
            raise AssertionError(kind)


def _connect_without_fsync(original):
    """This test is about what the checks report, not about durability: skip the per-commit fsync."""
    def connect(path, *, readonly: bool = False):
        conn = original(path, readonly=readonly)
        for pragma in ("synchronous=OFF", "fullfsync=OFF", "checkpoint_fullfsync=OFF"):
            conn.execute(f"PRAGMA {pragma}")
        return conn
    return connect


class IncrementalVerificationEquivalenceTest(unittest.TestCase):
    """Review finding 39: per-command verification is incremental and flags exactly what the full check flags."""

    SEEDS = (2, 5, 9, 11, 14, 17, 20, 23)
    STEPS = 70

    def test_randomized_history_incremental_equals_full_under_injected_corruption(self):
        """ADR-0006 command-path invariant, review finding 39. A seeded randomized command history (the replay
        suite's Scenario: intents, partial and duplicated fills, delayed commissions, corrections, cancels,
        faults, restarts, stops, starts, reconciliations, settlement days) runs with EVERY command routed
        through ``verify_touched`` (the full-check kinds are switched off). After each command the probe
        requires the incremental result to equal the full ``verify`` on the same transaction, then corrupts
        one touched key (a balance row changed, deleted or driven negative; one of the command's own
        postings changed; a touched lot's cost, quantity or owner changed, or the lot deleted; a touched hold
        inflated) and requires both checks to return exactly the same problems. Every corruption kind must
        be exercised and detected. The Scenario itself asserts ``Harness.verify() == []`` after every step."""
        totals: Counter = Counter()
        detected: Counter = Counter()
        calls = 0
        for seed in self.SEEDS:
            probe = EquivalenceProbe(seed)
            with mock.patch.object(ledger, "verify_touched", probe), \
                    mock.patch.object(desk_module, "FULL_VERIFY_KINDS", frozenset()), \
                    mock.patch.object(db_module, "connect", _connect_without_fsync(db_module.connect)):
                Scenario(self, seed, self.STEPS).run()
            self.assertEqual(probe.mismatches, [], f"seed {seed}")
            totals.update(probe.injected)
            detected.update(probe.detected)
            calls += probe.calls
        self.assertGreater(calls, 500)
        for kind in EquivalenceProbe.KINDS:
            self.assertGreater(totals[kind], 5, f"corruption {kind} too rarely exercised: {dict(totals)}")
            self.assertGreater(detected[kind], 0, f"corruption {kind} never detected: {dict(detected)}")


# ------------------------------------------------------------------ shared fixtures
def big_policy_harness(**kwargs) -> Harness:
    """The benchmark's fixture: large limits so a long buy-only history passes pretrade."""
    policy = load_policy(account_limits__max_orders_per_minute=600, account_limits__authorized_capital="1000000.00",
                         account_limits__max_gross_exposure="1000000.00", account_limits__max_order_notional="1000000.00",
                         session_template__seed="100000.00", session_template__target_terminal_equity="1000000.00",
                         session_template__session_max_loss="100000.00")
    h = Harness(initial_cash="1000000.00", policy=policy, **kwargs)
    h.bootstrap(authorize="1000000.00")
    assert h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted
    return h


def filled_buy(h: Harness, i: int) -> None:
    """One closed-loop round as the benchmark drives it: intent, send, fill, commission."""
    h.clock.advance(100_000_000)
    h.quote("9.99", "10.01")
    assert h.intent(f"O{i}", "BUY", "1", "10.00", "B").accepted
    assert h.desk.run_system("outbox_begin", {"outbox_id": f"O{i}:submit"}).body.get("send")
    h.desk.run_system("outbox_result", {"outbox_id": f"O{i}:submit", "status": "ACCEPTED", "broker_order_id": f"B{i}"})
    h.desk.run_system("broker_execution", {
        "account_key": ACCOUNT, "exec_key": f"bench:E{i}", "provider_exec_id": f"E{i}", "client_order_id": f"O{i}",
        "order_ref": None, "broker_order_id": f"B{i}", "instrument_id": INST, "provider_ref": INST, "side": "BUY",
        "quantity": "1", "price": "10.00", "currency": "USD", "exec_time_ns": h.clock.now_ns(), "correction_of": None})
    h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": f"bench:E{i}", "amount": "1.00",
                                            "currency": "USD"})


def running_session(h: Harness, session_id: str = "A") -> None:
    h.bootstrap()
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=session_id, seed="150.00",
                    target_terminal_equity="157.50")
    assert r.accepted and r.body["state"] == "RUNNING", r.body


def open_cases(h: Harness, kind: str) -> list[dict]:
    return [e for e in h.open_exceptions() if e["kind"] == kind]


class IncrementalVerificationCostTest(unittest.TestCase):
    """Review finding 39: the per-command check's work does not grow with history; the full check's does."""

    def measure(self, h: Harness, fn, *args) -> tuple[list[str], int]:
        """Run ``fn`` and count SQLite virtual-machine instructions executed meanwhile (every row a query
        visits costs instructions, so this measures scanned history independently of machine load)."""
        steps = [0]

        def tick() -> int:
            steps[0] += 1
            return 0
        h.desk.store.conn.set_progress_handler(tick, 10)
        try:
            return fn(*args), steps[0]
        finally:
            h.desk.store.conn.set_progress_handler(None, 0)

    def test_per_command_check_does_not_scan_history(self):
        """PRF-01, review finding 39: on the benchmark's buy-only history, the work of the per-command check
        for a fill is the same after 15 and after 150 fills, while the full check's work grows with the
        ledger. Measured in SQLite VM instructions (load independent), inside the real command."""
        h = big_policy_harness()
        samples: dict[str, list[tuple[int, int]]] = {"incremental": [], "full": []}
        real = ledger.verify_touched

        def probe(tx, touched):
            if touched.lots:  # the fill command (it opens a lot)
                result, incremental = self.measure(h, real, tx, touched)
                _, full = self.measure(h, ledger.verify, tx)
                samples["incremental"].append((len(samples["incremental"]), incremental))
                samples["full"].append((len(samples["full"]), full))
                return result
            return real(tx, touched)
        try:
            with mock.patch.object(ledger, "verify_touched", probe):
                for i in range(150):
                    filled_buy(h, i)
            self.assertEqual(h.verify(), [])
        finally:
            h.close()
        early_inc, late_inc = samples["incremental"][14][1], samples["incremental"][149][1]
        early_full, late_full = samples["full"][14][1], samples["full"][149][1]
        self.assertLessEqual(late_inc, early_inc + 2, (early_inc, late_inc))
        self.assertGreater(late_full, 5 * early_full, (early_full, late_full))
        self.assertLess(late_inc * 50, late_full, (late_inc, late_full))

    def test_damage_made_outside_the_core_is_found_by_the_full_checks(self):
        """ADR-0006, review finding 39 (the precondition, stated as behaviour). The per-command check verifies
        what a command changed against the state before it, so it does not re-scan history: a balance edited
        outside the core is not reported by the next fill that touches it. The full check still runs where a
        whole-ledger check is the point: every reconciliation result (here it latches the stop and opens a
        blocking LEDGER_INVARIANT case), process start, green start, and clearing a ledger-invariant case
        (refused while the damage persists)."""
        h = Harness()
        running_session(h)

        def buy(order_id: str, qty: str) -> None:
            self.assertTrue(h.intent(order_id, "BUY", qty, "10.00", "A").accepted)
            h.pump()
            h.broker.fill(order_id, F(qty), F("10.00"))
            h.pump()
            self.assertEqual(h.row("orders", "order_id", order_id)["state"], "FILLED")
        try:
            buy("B1", "5")
            self.assertEqual(h.verify(), [])
            key = (ACCOUNT, ledger.PAYABLE, "session:A", "USD", "")
            true_payable = h.balance(ledger.PAYABLE, "session:A")
            with h.desk.store.write() as tx:  # an edit outside the core: 6.00 of payable disappears
                tx.run(f"UPDATE balances SET amount=? WHERE {ledger._BALANCE_KEY}",
                       ((true_payable + F("6.00")).to_str(),) + key)
            problem = f"balance {key} stored {true_payable + F('6.00')} != postings {true_payable}"
            self.assertEqual(h.verify(), [problem])
            buy("B2", "1")  # touches the edited key; its own effects are consistent, so it is accepted
            true_payable = h.balance(ledger.PAYABLE, "session:A") - F("6.00")
            problem = f"balance {key} stored {true_payable + F('6.00')} != postings {true_payable}"
            self.assertEqual(h.verify(), [problem])
            self.assertEqual(open_cases(h, "LEDGER_INVARIANT"), [])
            r = h.reconcile()  # full check: the damage is found and the account is stopped
            self.assertTrue(r.accepted, r.body)
            self.assertEqual(r.body["status"], "CLEAN")  # the broker side agrees; only the ledger is damaged
            (case,) = open_cases(h, "LEDGER_INVARIANT")
            self.assertTrue(case["blocking"])
            self.assertEqual(case["detail"]["problems"], [problem])
            self.assertTrue(h.control()["stop_latched"])
            self.assertEqual(h.control()["latch_reason"], "LEDGER_INVARIANT")
            with self.assertRaises(InvariantViolation):  # cannot be acknowledged away while the ledger is wrong
                h.owner_cmd("resolve_exception", case_id=case["case_id"], action="acknowledge", note="looks fine")
            self.assertFalse(h.owner_cmd("start", account_key=ACCOUNT).accepted)  # nor can trading be started
            h.restart()  # process start runs the full check too: a second alarm, the stop stays latched
            self.assertEqual(len(open_cases(h, "LEDGER_INVARIANT")), 2)
            self.assertTrue(h.control()["stop_latched"])
        finally:
            h.close()


# ============================================================ finding 16 (first part)
class QuantityShortfallTest(unittest.TestCase):
    """Review finding 16: a correction that leaves working SELL holds above the shares owned."""

    def setUp(self):
        self.h = Harness()
        running_session(self.h)

    def tearDown(self):
        self.h.close()

    def test_correction_below_a_working_sell_opens_an_owner_case_not_the_invariant_alarm(self):
        """ADR-0006 observation path, review finding 16. Session A buys 5, offers all 5, then the broker
        corrects the buy to 3 shares. The fact is booked (lots 3), and because the working SELL still
        reserves 5 the core opens ONE blocking EXECUTION_ANOMALY naming session:A (owner_hint) with the
        order, the held and the owned quantity, instead of the owner-less LEDGER_INVARIANT alarm that
        latched the stop and refused every later owner command (the owner could not even cancel the sell).
        The owner then cancels the sell, the hold is released, and the case can be acknowledged."""
        h = self.h
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        fill = h.broker.fill("B1", F("5"), F("10.00"))
        h.pump()
        self.assertTrue(h.intent("S1", "SELL", "5", "10.00", "A").accepted)
        h.pump()
        self.assertEqual(tuple(h.desk.store.query("SELECT amount, status FROM holds WHERE hold_id='S1:qty'")[0]),
                         ("5", "ACTIVE"))
        corrected = h.broker.correct_execution(fill.exec_key, F("3"), F("10.00"))
        h.pump()
        self.assertEqual(h.row("executions", "exec_key", corrected.exec_key)["status"], "ACTIVE")
        with h.desk.store.read() as tx:
            self.assertEqual(ledger.owned_quantity(tx, ACCOUNT, "session:A", INST), Fixed(3))
            self.assertEqual(ledger.active_quantity_holds(tx, ACCOUNT, "session:A", INST), Fixed(5))
        self.assertEqual(open_cases(h, "LEDGER_INVARIANT"), [])
        (case,) = [e for e in open_cases(h, "EXECUTION_ANOMALY") if e["key"].startswith("quantity-shortfall:")]
        self.assertEqual((case["owner_hint"], case["session_id"], case["order_id"], case["blocking"]),
                         ("session:A", "A", "S1", True))
        self.assertEqual((case["detail"]["held"], case["detail"]["owned"], case["detail"]["orders"]), ("5", "3", ["S1"]))
        self.assertFalse(h.control()["stop_latched"])
        self.assertEqual(h.verify(), [])  # covered by the case that names the owner
        # The owner can act: cancel the over-reserved sell; the confirmation releases the hold.
        r = h.owner_cmd("request_cancel", order_id="S1", reason="sell exceeds corrected holding")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "CANCELED")
        self.assertEqual(h.verify(), [])
        r = h.owner_cmd("resolve_exception", case_id=case["case_id"], action="acknowledge", note="sell canceled")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.verify(), [])

    def test_shortfall_case_is_opened_once_per_owner_and_instrument(self):
        """Review finding 16: a second correction that deepens the same shortfall reuses the open case."""
        h = self.h
        self.assertTrue(h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        h.pump()
        fill = h.broker.fill("B1", F("5"), F("10.00"))
        h.pump()
        self.assertTrue(h.intent("S1", "SELL", "5", "10.00", "A").accepted)
        h.pump()
        first = h.broker.correct_execution(fill.exec_key, F("4"), F("10.00"))
        h.pump()
        h.broker.correct_execution(first.exec_key, F("2"), F("10.00"))
        h.pump()
        cases = [e for e in open_cases(h, "EXECUTION_ANOMALY") if e["key"].startswith("quantity-shortfall:")]
        self.assertEqual(len(cases), 1)
        self.assertEqual(open_cases(h, "LEDGER_INVARIANT"), [])
        self.assertEqual(h.verify(), [])


# ============================================================ finding 53
class CoverageTest(unittest.TestCase):
    """Review finding 53: only an OPEN blocking case in the same account excuses an owner."""

    def setUp(self):
        self.h = Harness()
        running_session(self.h)

    def tearDown(self):
        self.h.close()

    def check(self, owner: str, kind: str, unit: str, expected: str, cases: list[tuple[str, bool]], flagged: bool):
        """Inside a rolled-back transaction: add an ACTIVE hold the owner cannot cover and the given cases
        [(account, blocking)] naming the owner; both checks must agree on whether the owner is flagged."""
        h = self.h
        try:
            with h.desk.store.write() as tx:
                amount = "1000.000000" if kind == "CASH" else "7"
                tx.run("INSERT INTO holds(hold_id, order_id, account_key, owner, kind, unit, amount, status) "
                       "VALUES ('probe:hold', 'probe', ?, ?, ?, ?, ?, 'ACTIVE')", (ACCOUNT, owner, kind, unit, amount))
                for n, (account_key, blocking) in enumerate(cases):
                    row = {"case_id": f"probe-{n}", "account_key": account_key, "kind": "EXECUTION_ANOMALY",
                           "blocking": blocking, "status": "OPEN", "owner_hint": owner, "key": f"probe-{n}"}
                    tx.run("INSERT INTO exceptions(case_id, account_key, kind, blocking, status, row) VALUES (?,?,?,?,?,?)",
                           (row["case_id"], account_key, row["kind"], int(blocking), "OPEN", json.dumps(row)))
                touched = ledger.Touched()
                touched.holds.add((ACCOUNT, owner, kind, unit))
                full, incremental = ledger.verify(tx), ledger.verify_touched(tx, touched)
                raise _Abort
        except _Abort:
            pass
        self.assertEqual(full, [expected] if flagged else [], cases)
        self.assertEqual(incremental, full, cases)

    def test_cash_shortfall_is_excused_only_by_a_blocking_case_in_the_same_account(self):
        """ADR-0006, review finding 53: a non-blocking case, or a blocking case in another account, naming
        'unallocated' does not excuse its negative available cash; a blocking case in this account does."""
        expected = f"available cash negative for {ACCOUNT}/unallocated/USD"
        self.check("unallocated", "CASH", "USD", expected, [], True)
        self.check("unallocated", "CASH", "USD", expected, [(ACCOUNT, False)], True)
        self.check("unallocated", "CASH", "USD", expected, [("SIM-2", True)], True)
        self.check("unallocated", "CASH", "USD", expected, [("SIM-2", True), (ACCOUNT, False)], True)
        self.check("unallocated", "CASH", "USD", expected, [(ACCOUNT, True)], False)

    def test_quantity_shortfall_is_excused_only_by_a_blocking_case_in_the_same_account(self):
        """ADR-0006, review finding 53: the same rule for SELL quantity holds above the shares owned."""
        expected = f"quantity holds exceed owned lots for {ACCOUNT}/session:A/{INST}"
        self.check("session:A", "QUANTITY", INST, expected, [], True)
        self.check("session:A", "QUANTITY", INST, expected, [(ACCOUNT, False)], True)
        self.check("session:A", "QUANTITY", INST, expected, [("SIM-2", True)], True)
        self.check("session:A", "QUANTITY", INST, expected, [(ACCOUNT, True)], False)


# ============================================================ finding 51
class SteppingClock(ManualClock):
    """A wall clock that an NTP correction can step backwards (monotonic time still only advances)."""

    def step_back(self, ns: int) -> None:
        self._now -= ns


class ReconciliationOrderTest(unittest.TestCase):
    """Review finding 51: the latest reconciliation is the last one recorded, whatever the wall clock says."""

    def setUp(self):
        self.clock = SteppingClock(T0)
        self.h = Harness(clock=self.clock)
        running_session(self.h)

    def tearDown(self):
        self.h.close()

    def test_a_clock_step_back_does_not_resurrect_an_older_clean_run(self):
        """REC-01, ADR-0010, review finding 51: CLEAN at T+60 s, then the wall clock steps back 10 minutes and
        a run made while the broker is disconnected is INCOMPLETE. The latest run is the INCOMPLETE one (the
        green-start gate must see it), although the CLEAN run carries the larger completed_at_ns."""
        h = self.h
        h.clock.advance(60 * NS)
        clean = h.reconcile()
        self.assertEqual(clean.body["status"], "CLEAN")
        self.clock.step_back(600 * NS)
        h.broker.disconnect()
        incomplete = h.reconcile()
        self.assertEqual(incomplete.body["status"], "INCOMPLETE")
        runs = {r["run_id"]: r for r in (json.loads(x["row"]) for x in h.desk.store.query("SELECT row FROM reconciliations"))}
        self.assertGreater(runs[clean.body["run_id"]]["completed_at_ns"], runs[incomplete.body["run_id"]]["completed_at_ns"])
        with h.desk.store.read() as tx:
            latest = state.latest_reconciliation(tx, ACCOUNT)
        self.assertEqual((latest["run_id"], latest["status"]), (incomplete.body["run_id"], "INCOMPLETE"))

    def test_the_execution_watermark_comes_from_the_last_clean_run(self):
        """REC-01, review finding 51: two CLEAN runs with a clock step back between them; the next
        reconciliation asks the broker for executions after the LATER run's watermark."""
        h = self.h
        self.assertTrue(h.intent("B1", "BUY", "2", "10.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("1"), F("10.00"))
        h.pump()
        h.clock.advance(60 * NS)
        first = h.reconcile()
        self.assertEqual(first.body["status"], "CLEAN")
        self.clock.step_back(600 * NS)
        h.broker.fill("B1", F("1"), F("10.00"))
        h.pump()
        second = h.reconcile()
        self.assertEqual(second.body["status"], "CLEAN", second.body)
        marks = {r["run_id"]: r.get("watermark") for r in
                 (json.loads(x["row"]) for x in h.desk.store.query("SELECT row FROM reconciliations"))}
        self.assertNotEqual(marks[first.body["run_id"]], marks[second.body["run_id"]])
        self.assertEqual(h.runner.last_watermark(), marks[second.body["run_id"]])
        self.assertEqual(h.verify(), [])


# ============================================================ finding 58
class FlakyCommands:
    """Wraps ``Desk.run_system``: the first ``failures`` commands of ``kind`` raise; every kind is logged."""

    def __init__(self, h: Harness, kind: str, failures: int = 1):
        self.real = h.desk.run_system
        self.kind = kind
        self.failures = failures
        self.applied: list[str] = []

    def __call__(self, kind, payload, *args, **kwargs):
        if kind == self.kind and self.failures:
            self.failures -= 1
            raise RuntimeError(f"transient failure applying {kind}")
        result = self.real(kind, payload, *args, **kwargs)
        self.applied.append(kind)
        return result


class EventBacklogTest(unittest.TestCase):
    """Review finding 58: a failing event command never drops the broker events popped with it."""

    def setUp(self):
        self.h = Harness()
        running_session(self.h)
        self.assertTrue(self.h.intent("B1", "BUY", "5", "10.00", "A").accepted)
        self.h.pump()

    def tearDown(self):
        self.h.close()

    def queued_kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.h.broker._events]

    def test_unapplied_events_are_kept_in_order_and_applied_on_the_next_drain(self):
        """ADR-0006 (never discard a real execution), review finding 58: the broker emits the execution,
        its commission and the FILLED status; applying the execution raises once. The pump raises, but
        all three events stay queued in order, and the next pump applies them once, in broker order."""
        h = self.h
        h.broker.fill("B1", F("5"), F("10.00"))
        queued = self.queued_kinds()
        self.assertEqual(queued[0], "ExecutionEvent")
        flaky = FlakyCommands(h, "broker_execution")
        with mock.patch.object(h.desk, "run_system", flaky):
            with self.assertRaises(RuntimeError):
                h.pump()
            self.assertEqual(h.broker.pending_event_count(), 0)  # popped from the broker...
            self.assertEqual([type(e).__name__ for e in event_backlog(h.broker)], queued)  # ...but kept
            self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 0)
            stats = h.pump()
        self.assertEqual(stats.events, len(queued))
        expected = {"ExecutionEvent": "broker_execution", "CommissionEvent": "broker_commission",
                    "OrderStatusEvent": "broker_order_status", "ConnectivityEvent": "broker_connectivity"}
        self.assertEqual([k for k in flaky.applied if k.startswith("broker_")], [expected[k] for k in queued])
        self.assertEqual(len(event_backlog(h.broker)), 0)
        o = h.row("orders", "order_id", "B1")
        self.assertEqual((o["state"], o["cum_qty"]), ("FILLED", "5"))
        (ex,) = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM executions")]
        self.assertEqual((ex["status"], ex["fee_status"]), ("ACTIVE", "FINAL"))
        self.assertEqual(h.verify(), [])

    def test_reconciliation_records_incomplete_instead_of_losing_the_run(self):
        """REC-01, review finding 58: when a queued event fails while a reconciliation drains it, the run is
        recorded as INCOMPLETE (it used to raise and record nothing); the event stays queued
        and the next reconciliation applies it and is CLEAN."""
        h = self.h
        h.broker.fill("B1", F("5"), F("10.00"))
        flaky = FlakyCommands(h, "broker_execution")
        with mock.patch.object(h.desk, "run_system", flaky):
            r = h.reconcile()
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body["status"], "INCOMPLETE")
        self.assertEqual(h.row("reconciliations", "run_id", r.body["run_id"])["status"], "INCOMPLETE")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM executions")[0]["n"], 0)
        self.assertGreater(len(event_backlog(h.broker)), 0)
        r = h.reconcile()
        self.assertEqual(r.body["status"], "CLEAN", r.body)
        self.assertEqual(len(event_backlog(h.broker)), 0)
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "FILLED")
        self.assertEqual(h.verify(), [])


# ============================================================ finding 57
class FailingFeedBroker(SimulatedBroker):
    """A broker whose event feed always fails, so every service cycle fails at the pump."""

    def drain_events(self, limit: int = 1000):
        raise RuntimeError("event feed broken (fixture)")


class ServiceHeartbeatTest(unittest.TestCase):
    """Review finding 57: the lease heartbeat does not depend on a successful cycle; a fenced core stops."""

    def wait_for(self, predicate, what: str, timeout_s: float = 20.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    def lease(self, path) -> dict:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT holder, epoch, heartbeat_at_ns FROM writer_lease WHERE id=1").fetchone())
        finally:
            conn.close()

    def test_failing_cycles_keep_the_lease_and_a_takeover_stops_the_old_core(self):
        """ADR-0011 #1 and #4, RB-11, review finding 57. Every cycle of the core fails (its broker feed
        raises). The heartbeat still runs each cycle, so 50 s after start (lease staleness is 30 s) a second
        instance WITHOUT --takeover is refused (LeaseHeld). With --takeover the lease moves to the new
        writer; the old core's next heartbeat is fenced (LeaseLost) and it stops instead of running on
        as a zombie, without disturbing the new holder's lease."""
        quiet = mock.patch.object(core_module.log, "disabled", True)  # the failing cycles are logged by design
        quiet.start()
        self.addCleanup(quiet.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = Paths(Path(tmp.name) / "home")
        paths.ensure()
        clock = ManualClock(T0)
        broker = FailingFeedBroker(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        h = Harness(db_path=paths.db(ACCOUNT), clock=clock, broker=SimulatedBroker(
            clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1))
        h.bootstrap(start=False)
        h.desk.store.release_lease()
        h.close()
        svc = CoreService(paths=paths, account_key=ACCOUNT, deployment_id="mac-dev", broker=broker, clock=clock,
                          intervals=Intervals(loop_s=0.005, valuation_s=3600.0, reconcile_s=3600.0,
                                              reconcile_retry_s=3600.0, heartbeat_s=0.0))
        thread = threading.Thread(target=svc.run, name="core-under-test", daemon=True)
        thread.start()
        self.addCleanup(lambda: (svc.shutdown(), thread.join(10)))
        self.assertTrue(svc.ready.wait(20))
        self.wait_for(lambda: svc.cycle_errors >= 3, "failing cycles")
        db = paths.db(ACCOUNT)
        epoch = self.lease(db)["epoch"]
        for _ in range(2):
            clock.advance(25 * NS)
            self.wait_for(lambda: self.lease(db)["heartbeat_at_ns"] == clock.now_ns(), "a heartbeat at the new time")
        self.assertGreaterEqual(svc.cycle_errors, 3)  # still failing, still the live writer
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with self.assertRaises(LeaseHeld):
                Store(db, clock, holder="standby:1")
            intruder = Store(db, clock, holder="standby:1", takeover=True)
        self.addCleanup(intruder.close)
        self.assertEqual(intruder.epoch, epoch + 1)
        thread.join(20)
        self.assertFalse(thread.is_alive(), "the fenced core kept running")
        self.assertTrue(svc.lease_lost)
        self.assertEqual((self.lease(db)["holder"], self.lease(db)["epoch"]), ("standby:1", epoch + 1))


if __name__ == "__main__":
    unittest.main()
