"""Durability and performance fixes from the 2026-10-22 adversarial review.

Findings covered (numbers from the review's confirmed list):

* 39 - per-command ledger verification is incremental (``ledger.verify_touched``):
  it checks only the entries a command emitted and the keys it touched, and is
  proven equal to the full ``ledger.verify`` on a randomized command history with
  corruption injected into the touched keys;
* 16 (first part) - an observation that leaves working SELL holds above the
  shares owned opens a blocking EXECUTION_ANOMALY naming the owner, instead of
  the owner-less ledger-invariant alarm;
* 53 - only an OPEN *blocking* case in the *same account* excuses an owner.

Every number is a TEST FIXTURE. The broker is the deterministic SimulatedBroker.
"""
from __future__ import annotations

import json
import random
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness, load_policy  # noqa: E402
from test_replay_randomized import Scenario  # noqa: E402

from unknown.core import desk as desk_module  # noqa: E402
from unknown.core import ledger  # noqa: E402
from unknown.domain.errors import InvariantViolation  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.store import db as db_module  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
