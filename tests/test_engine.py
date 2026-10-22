import random
import tempfile
import unittest
from pathlib import Path

from reference.engine import Engine, Rejected


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "simulation.sqlite"
        self.e = Engine(self.path, 30000)
        self.e.apply("start", "start", owner=True, reconciled=True)
        self.e.apply("session", "session", session="A", seed=10000, goal=10500)

    def tearDown(self):
        self.e.close()
        self.tmp.cleanup()

    def order(self, oid="B", side="BUY", qty=10, price=900, fee=10):
        return self.e.apply("order-" + oid, "order", order=oid, session="A", symbol="X", side=side, qty=qty, limit=price, fee_budget=fee)

    def fill(self, fid="E1", oid="B", qty=10, price=900, fee=10):
        return self.e.apply("fill-" + fid, "fill", fill=fid, order=oid, qty=qty, price=price, fee=fee)

    def profit(self):
        self.order()
        self.fill()
        self.order("S", "SELL", 10, 960, 10)
        self.fill("E2", "S", 10, 960, 10)

    def test_cash_reservation_blocks_overcommit(self):
        self.order()
        with self.assertRaises(Rejected):
            self.order("B2", qty=2, price=500, fee=0)
        self.assertEqual(Engine.reserved(self.e.snapshot(), "A"), 9010)

    def test_command_retry_and_changed_payload(self):
        original = self.order()
        self.assertEqual(self.order(), original)
        with self.assertRaises(Rejected):
            self.order(qty=9)

    def test_execution_retry_across_command_ids(self):
        self.order()
        self.fill()
        before = self.e.snapshot()["realized_net"]
        result = self.e.apply("another-delivery", "fill", fill="E1", order="B", qty=10, price=900, fee=10)
        self.assertTrue(result["duplicate"])
        self.assertEqual(self.e.snapshot()["realized_net"], before)
        with self.assertRaises(Rejected):
            self.e.apply("corrupted-duplicate", "fill", fill="E1", order="B", qty=9, price=900, fee=10)

    def test_cancel_fill_race_preserves_remaining_reservation(self):
        self.order()
        self.fill(qty=4, fee=4)
        self.e.apply("cancel", "cancel", order="B")
        self.fill("E2", qty=2, price=890, fee=2)
        self.assertEqual(Engine.reserved(self.e.snapshot(), "A"), 3604)
        self.e.apply("canceled", "canceled", order="B")
        s = self.e.snapshot()
        self.assertEqual(Engine.reserved(s, "A"), 0)
        self.assertEqual(s["sessions"]["A"]["positions"]["X"], {"qty": 6, "cost": 5380})
        self.assertEqual(s["sessions"]["A"]["cash"], 4614)

    def test_cancel_is_not_confirmation(self):
        self.order()
        self.e.apply("cancel", "cancel", order="B")
        self.assertEqual(Engine.reserved(self.e.snapshot(), "A"), 9010)

    def test_unowned_sell_and_double_reservation_rejected(self):
        with self.assertRaises(Rejected):
            self.order("S0", "SELL", 1)
        self.order()
        self.fill()
        self.order("S1", "SELL", 6)
        with self.assertRaises(Rejected):
            self.order("S2", "SELL", 5)

    def test_stop_persists_and_reduction_remains_possible(self):
        self.order()
        self.fill()
        self.e.apply("stop", "stop")
        with self.assertRaises(Rejected):
            self.order("B2", qty=1, price=100)
        self.order("S", "SELL", 10, 800, 10)
        self.e.close()
        self.e = Engine(self.path)
        self.assertTrue(self.e.snapshot()["halted"])
        self.assertFalse(self.e.reconciled)

    def test_restart_requires_fresh_reconciliation(self):
        self.e.close()
        self.e = Engine(self.path)
        with self.assertRaises(Rejected):
            self.order()
        self.e.apply("fresh-start", "start", owner=True, reconciled=True)
        self.order()

    def test_start_requires_owner_and_reconciliation(self):
        with self.assertRaises(Rejected):
            self.e.apply("bad-start", "start", owner=False, reconciled=True)

    def test_unsettled_sale_is_not_session_cash(self):
        self.profit()
        self.assertEqual(self.e.snapshot()["sessions"]["A"]["cash"], 990)
        with self.assertRaises(Rejected):
            self.order("B2", qty=2, price=600, fee=0)

    def test_completion_protects_and_funds_exactly_one_successor(self):
        self.profit()
        result = self.e.apply("complete", "complete", session="A", fees_final=True)
        self.assertEqual(result["protected_value"], 10580)
        self.assertEqual(result["status"], "FUNDED")
        self.e.apply("repeat-completion", "complete", session="A", fees_final=True)
        state = self.e.snapshot()
        self.assertEqual(len(state["requests"]), 1)
        self.assertEqual(state["unallocated"], 10000)
        self.assertEqual(state["sessions"]["A:next"]["cash"], 10000)
        self.assertEqual(state["protected"], 990)
        self.e.apply("settlement", "settle", fill="E2", broker_confirmed=True)
        self.e.apply("settlement-again", "settle", fill="E2", broker_confirmed=True)
        self.assertEqual(self.e.snapshot()["protected"], 10580)

    def test_insufficient_seed_waits_even_after_protected_settlement(self):
        with EngineContext(10000) as e:
            e.apply("go", "start", owner=True, reconciled=True)
            e.apply("s", "session", session="A", seed=10000, goal=10500)
            e.apply("b", "order", order="B", session="A", symbol="X", side="BUY", qty=10, limit=900, fee_budget=0)
            e.apply("f1", "fill", fill="E1", order="B", qty=10, price=900, fee=0)
            e.apply("s1", "order", order="S", session="A", symbol="X", side="SELL", qty=10, limit=960, fee_budget=0)
            e.apply("f2", "fill", fill="E2", order="S", qty=10, price=960, fee=0)
            result = e.apply("done", "complete", session="A", fees_final=True)
            self.assertEqual(result["status"], "WAITING_FUNDS")
            e.apply("settle", "settle", fill="E2", broker_confirmed=True)
            self.assertEqual(e.snapshot()["unallocated"], 0)
            self.assertEqual(len(e.snapshot()["sessions"]), 1)

    def test_completion_requires_goal_flatness_and_fees(self):
        with self.assertRaises(Rejected):
            self.e.apply("early", "complete", session="A", fees_final=True)
        self.order()
        self.fill()
        with self.assertRaises(Rejected):
            self.e.apply("nonflat", "complete", session="A", fees_final=True)
        self.order("S", "SELL", 10, 960)
        self.fill("E2", "S", 10, 960)
        with self.assertRaises(Rejected):
            self.e.apply("fees", "complete", session="A", fees_final=False)

    def test_invalid_fill_rolls_back(self):
        self.order()
        before = self.e.snapshot()
        for i, (qty, price, fee) in enumerate([(11, 900, 0), (10, 901, 0), (10, 900, 11)]):
            with self.assertRaises(Rejected):
                self.fill("bad" + str(i), qty=qty, price=price, fee=fee)
            self.assertEqual(self.e.snapshot(), before)

    def test_numeric_validation(self):
        for i, bad in enumerate([0, -1, True, 1.5, 10**16]):
            with self.assertRaises(Rejected):
                self.order("bad" + str(i), qty=bad)

    def test_drawdown_latches_and_cannot_be_reset_by_start(self):
        self.order()
        self.fill()
        self.e.apply("mark", "mark", prices={"X": 870})
        self.assertTrue(self.e.snapshot()["halted"])
        self.assertEqual(self.e.snapshot()["sessions"]["A"]["state"], "RISK_STOP")
        with self.assertRaises(Rejected):
            self.e.apply("retry-start", "start", owner=True, reconciled=True)

    def test_reinitialization_cannot_duplicate_deposit(self):
        with self.assertRaises(Rejected):
            Engine(self.path, 30000)

    def test_audit_tampering_detected(self):
        self.order()
        self.assertTrue(self.e.verify_audit())
        self.e.db.execute("UPDATE commands SET result='{}' WHERE id='order-B'")
        self.assertFalse(self.e.verify_audit())

    def test_random_round_trips_conserve_every_step(self):
        rng = random.Random(17)
        for i in range(150):
            qty = rng.randint(1, 5)
            buy = rng.randint(100, 200)
            sell = buy + rng.randint(-5, 12)
            self.order("B" + str(i), qty=qty, price=buy, fee=2)
            self.fill("BF" + str(i), "B" + str(i), qty, buy, 2)
            self.order("S" + str(i), "SELL", qty, sell, 2)
            self.fill("SF" + str(i), "S" + str(i), qty, sell, 2)
            self.e.apply("settle-" + str(i), "settle", fill="SF" + str(i), broker_confirmed=True)
            Engine.check(self.e.snapshot())
        self.assertTrue(self.e.verify_audit())


class EngineContext:
    def __init__(self, cash):
        self.e = Engine(":memory:", cash)
    def __enter__(self):
        return self.e
    def __exit__(self, *args):
        self.e.close()


if __name__ == "__main__":
    unittest.main()
