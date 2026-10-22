"""Tests for the deterministic SimulatedBroker fixture (BRK-05)."""
import unittest
from datetime import date

from unknown.broker.port import (
    AccountMode,
    BrokerOrder,
    BrokerOrderStatus,
    CommissionEvent,
    ConnectivityEvent,
    ExecutionEvent,
    OrderStatusEvent,
    SubmitStatus,
)
from unknown.broker.simulated import CancelBehavior, SimulatedBroker, SubmitBehavior
from unknown.domain.calendar import WEEKDAYS_ONLY, BusinessCalendar
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed

F = Fixed.parse
# Friday 2026-10-23 11:00 America/New_York (15:00 UTC, EDT).
FRIDAY = utc_ns(2026, 10, 23, 15, 0)
HOUR = 3600 * 10**9
DAY = 24 * HOUR


def order(cid="C1", side="BUY", qty="10", limit="10.00", instrument="INS-A", tif="DAY"):
    return BrokerOrder(client_order_id=cid, order_ref=f"DEP:1:{cid}", instrument_id=instrument,
                       provider_ref=instrument, side=side, quantity=F(qty), limit_price=F(limit),
                       currency="USD", time_in_force=tif)


class SimulatedBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(FRIDAY)
        self.broker = SimulatedBroker(self.clock, initial_settled_cash=F("1000.00"), calendar=WEEKDAYS_ONLY)

    def cash(self):
        (row,) = self.broker.account_snapshot().cash
        return row.settled, row.total

    def test_capabilities_are_labelled_simulated(self):
        caps = self.broker.capabilities()
        self.assertEqual(caps.account_mode, AccountMode.SIMULATED)
        self.assertEqual(caps.provider, "simulated")
        self.assertEqual(caps.order_types, ("LIMIT",))
        self.assertFalse(caps.short_selling or caps.margin or caps.fractional_quantities)
        self.assertEqual(caps.order_reference_max_length, 64)

    def test_accept_emits_acknowledgment(self):
        result = self.broker.submit(order())
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        self.assertEqual(result.broker_order_id, "SIMORD-1")
        (event,) = self.broker.drain_events()
        self.assertIsInstance(event, OrderStatusEvent)
        self.assertEqual(event.status, BrokerOrderStatus.ACKNOWLEDGED)
        self.assertEqual(event.client_order_id, "C1")
        self.assertEqual(event.remaining_quantity, F("10"))

    def test_scripted_reject_and_duplicate_id(self):
        self.broker.script_submit(SubmitBehavior.REJECT)
        self.assertEqual(self.broker.submit(order()).status, SubmitStatus.REJECTED)
        self.assertFalse(self.broker.lookup("C1").found)
        self.assertEqual(self.broker.submit(order()).status, SubmitStatus.ACCEPTED)
        dup = self.broker.submit(order())
        self.assertEqual(dup.status, SubmitStatus.REJECTED)
        self.assertIn("duplicate", dup.reason)

    def test_timeout_after_accept_is_unknown_but_findable(self):
        self.broker.script_submit(SubmitBehavior.TIMEOUT_AFTER_ACCEPT)
        result = self.broker.submit(order())
        self.assertEqual(result.status, SubmitStatus.UNKNOWN)
        found = self.broker.lookup("C1")
        self.assertTrue(found.found)
        self.assertEqual(found.status, BrokerOrderStatus.ACKNOWLEDGED)
        self.assertEqual(found.broker_order_id, "SIMORD-1")
        self.assertEqual(len(self.broker.open_orders()[0]), 1)

    def test_timeout_before_accept_lookup_conclusively_absent(self):
        self.broker.script_submit(SubmitBehavior.TIMEOUT_BEFORE_ACCEPT)
        self.assertEqual(self.broker.submit(order()).status, SubmitStatus.UNKNOWN)
        self.assertIs(self.broker.lookup("C1").found, False)
        self.assertEqual(self.broker.drain_events(), [])

    def test_raise_and_disconnect_never_store(self):
        self.broker.script_submit(SubmitBehavior.RAISE)
        with self.assertRaises(ConnectionError):
            self.broker.submit(order())
        self.broker.disconnect()
        self.assertEqual(self.broker.submit(order("C2")).status, SubmitStatus.UNKNOWN)
        self.assertIsNone(self.broker.lookup("C2").found)
        with self.assertRaises(ConnectionError):
            self.broker.account_snapshot()
        self.broker.reconnect()
        self.assertIs(self.broker.lookup("C2").found, False)
        kinds = [type(e) for e in self.broker.drain_events()]
        self.assertEqual(kinds, [ConnectivityEvent, ConnectivityEvent])

    def test_partial_fill_then_cancel_race(self):
        self.broker.submit(order())
        self.broker.defer_cancel_confirmation(True)
        self.broker.fill("C1", F("4"), F("9.99"))
        self.assertEqual(self.broker.cancel("C1", "SIMORD-1").status, SubmitStatus.ACCEPTED)
        self.broker.fill("C1", F("2"), F("9.98"))  # fill arrives while the cancel is pending
        self.assertTrue(self.broker.confirm_cancel("C1"))
        statuses = [e for e in self.broker.drain_events() if isinstance(e, OrderStatusEvent)]
        self.assertEqual([s.status for s in statuses], [
            BrokerOrderStatus.ACKNOWLEDGED, BrokerOrderStatus.PARTIALLY_FILLED,
            BrokerOrderStatus.PENDING_CANCEL, BrokerOrderStatus.PENDING_CANCEL, BrokerOrderStatus.CANCELED])
        self.assertEqual(statuses[-1].filled_quantity, F("6"))
        self.assertEqual(statuses[-1].remaining_quantity, F("4"))
        (pos,) = self.broker.account_snapshot().positions
        self.assertEqual(pos.quantity, F("6"))
        self.assertEqual(self.broker.cancel("C1", "SIMORD-1").reason, "too late")

    def test_cancel_scripts(self):
        self.broker.submit(order())
        self.broker.script_cancel(CancelBehavior.TIMEOUT, CancelBehavior.REJECT_TOO_LATE)
        self.assertEqual(self.broker.cancel("C1", None).status, SubmitStatus.UNKNOWN)
        self.assertEqual(self.broker.cancel("C1", None).status, SubmitStatus.REJECTED)
        self.assertTrue(self.broker.lookup("C1").status in (BrokerOrderStatus.ACKNOWLEDGED,))
        self.assertEqual(self.broker.cancel("C1", None).status, SubmitStatus.ACCEPTED)
        self.assertEqual(self.broker.lookup("C1").status, BrokerOrderStatus.CANCELED)

    def test_duplicate_callbacks_deliver_same_execution(self):
        self.broker.submit(order())
        self.broker.drain_events()
        row = self.broker.fill("C1", F("10"), F("10.00"), duplicate_callbacks=2)
        execs = [e.execution for e in self.broker.drain_events() if isinstance(e, ExecutionEvent)]
        self.assertEqual(len(execs), 3)
        self.assertTrue(all(e == row for e in execs))
        self.assertEqual(row.exec_key, "sim:SIM:E1")
        self.assertEqual(len(self.broker.executions_since(None).executions), 1)

    def test_delayed_commission_release(self):
        self.broker.submit(order())
        row = self.broker.fill("C1", F("10"), F("10.00"), delay_commission=True)
        self.assertFalse(any(isinstance(e, CommissionEvent) for e in self.broker.drain_events()))
        batch = self.broker.executions_since(None)
        self.assertEqual(batch.commissions, ())
        self.assertEqual(self.broker.release_commissions(), 1)
        (event,) = self.broker.drain_events()
        self.assertEqual(event.commission.exec_key, row.exec_key)
        self.assertEqual(event.commission.amount, F("1.000000"))
        self.assertEqual(len(self.broker.executions_since(None).commissions), 1)
        # Re-delivery from an older watermark returns the same rows again.
        self.assertEqual(self.broker.executions_since("0").executions, (row,))
        self.assertEqual(self.broker.executions_since(batch.watermark).executions, ())

    def test_settlement_friday_trade_settles_monday(self):
        self.broker.submit(order())
        self.broker.fill("C1", F("10"), F("10.00"), commission=F("1.00"))
        self.assertEqual(self.cash(), (F("1000.00"), F("899.00")))
        self.clock.set(FRIDAY + 1 * DAY)  # Saturday
        self.assertEqual(self.broker.advance_settlement(), 0)
        self.clock.set(FRIDAY + 2 * DAY)  # Sunday
        self.assertEqual(self.broker.advance_settlement(), 0)
        self.assertEqual(self.cash(), (F("1000.00"), F("899.00")))
        self.clock.set(FRIDAY + 3 * DAY)  # Monday
        self.assertEqual(self.broker.advance_settlement(), 1)
        self.assertEqual(self.cash(), (F("899.00"), F("899.00")))

    def test_settlement_skips_holiday(self):
        cal = BusinessCalendar("TEST", "1", holidays=frozenset({date(2026, 10, 26)}), source="test fixture")
        broker = SimulatedBroker(self.clock, initial_settled_cash=F("1000.00"), calendar=cal)
        broker.submit(order())
        broker.fill("C1", F("10"), F("10.00"), commission=F("1.00"))
        self.clock.set(FRIDAY + 3 * DAY)  # Monday is a holiday
        self.assertEqual(broker.advance_settlement(), 0)
        self.clock.set(FRIDAY + 4 * DAY)  # Tuesday
        self.assertEqual(broker.advance_settlement(), 1)
        (row,) = broker.account_snapshot().cash
        self.assertEqual(row.settled, F("899.00"))

    def test_sale_proceeds_pending_until_settlement(self):
        self.broker.submit(order())
        self.broker.fill("C1", F("10"), F("10.00"), commission=F("1.00"))
        self.broker.submit(order("C2", "SELL", "10", "11.00"))
        self.broker.fill("C2", F("10"), F("11.00"), commission=F("1.00"))
        settled, total = self.cash()
        self.assertEqual(settled, F("1000.00"))
        self.assertEqual(total, F("1008.00"))
        self.assertEqual(self.broker.account_snapshot().positions, ())

    def test_correction_emits_suffixed_execution(self):
        self.broker.submit(order())
        row = self.broker.fill("C1", F("10"), F("10.00"), commission=F("1.00"))
        self.broker.drain_events()
        corrected = self.broker.correct_execution(row.exec_key, F("10"), F("9.90"))
        self.assertEqual(corrected.provider_exec_id, "E1.01")
        self.assertEqual(corrected.exec_key, "sim:SIM:E1.01")
        self.assertEqual(corrected.correction_of, row.exec_key)
        again = self.broker.correct_execution(corrected.exec_key, F("9"), F("9.90"))
        self.assertEqual(again.provider_exec_id, "E1.02")
        (pos,) = self.broker.account_snapshot().positions
        self.assertEqual(pos.quantity, F("9"))
        self.assertEqual(pos.average_cost, F("9.90000000"))
        _, total = self.cash()
        self.assertEqual(total, F("1000.00") - F("1.00") - F("89.10"))

    def test_external_trade_and_foreign_order_visible(self):
        row = self.broker.external_trade("INS-B", "BUY", F("5"), F("20.00"))
        self.assertIsNone(row.client_order_id)
        self.assertIsNone(row.order_ref)
        self.assertEqual(row.broker_order_id, "MANUAL-1")
        foreign = self.broker.foreign_order("INS-B", "SELL", F("5"), F("25.00"))
        rows, complete = self.broker.open_orders()
        self.assertTrue(complete)
        self.assertEqual([r.broker_order_id for r in rows], [foreign.broker_order_id])
        self.assertEqual(rows[0].order_ref, "OTHER-DEPLOYMENT:1:x")
        self.assertIsNone(rows[0].client_order_id)
        (pos,) = self.broker.account_snapshot().positions
        self.assertEqual((pos.instrument_id, pos.quantity), ("INS-B", F("5")))

    def test_short_sale_and_overspend_rejected(self):
        short = self.broker.submit(order("S1", "SELL", "1", "10.00"))
        self.assertEqual(short.status, SubmitStatus.REJECTED)
        self.assertIn("short", short.reason)
        self.broker.submit(order())
        self.broker.fill("C1", F("10"), F("10.00"), commission=F("1.00"))
        self.assertEqual(self.broker.submit(order("S2", "SELL", "6", "10.00")).status, SubmitStatus.ACCEPTED)
        self.assertEqual(self.broker.submit(order("S3", "SELL", "5", "10.00")).status, SubmitStatus.REJECTED)
        big = self.broker.submit(order("B2", "BUY", "100", "10.00"))
        self.assertEqual(big.status, SubmitStatus.REJECTED)
        self.assertIn("cash", big.reason)

    def test_snapshot_cash_and_completeness(self):
        self.broker.deposit(F("50.00"))
        self.broker.withdraw(F("25.00"))
        self.assertEqual(self.cash(), (F("1025.00"), F("1025.00")))
        self.broker.set_snapshot_incomplete(True)
        self.assertFalse(self.broker.account_snapshot().complete)
        with self.assertRaises(ValueError):
            self.broker.withdraw(F("5000.00"))

    def test_expire_and_shuffle_events(self):
        self.broker.submit(order())
        self.broker.submit(order("C2"))
        self.assertEqual(self.broker.pending_event_count(), 2)
        self.broker.shuffle_pending_events([1, 0])
        first, second = self.broker.drain_events()
        self.assertEqual((first.client_order_id, second.client_order_id), ("C2", "C1"))
        self.assertEqual(self.broker.expire_day_orders(), 2)
        self.assertEqual(self.broker.lookup("C1").status, BrokerOrderStatus.EXPIRED)
        self.assertEqual(self.broker.open_orders()[0], [])
        with self.assertRaises(ValueError):
            self.broker.fill("C1", F("1"), F("10.00"))

    def test_anomaly_flags(self):
        self.broker.submit(order(qty="2"))
        with self.assertRaises(ValueError):
            self.broker.fill("C1", F("3"), F("10.00"))
        with self.assertRaises(ValueError):
            self.broker.fill("C1", F("1"), F("10.01"))
        row = self.broker.fill("C1", F("3"), F("10.01"), allow_overfill=True, allow_price_violation=True)
        self.assertEqual(row.quantity, F("3"))
        self.assertEqual(self.broker.lookup("C1").status, BrokerOrderStatus.FILLED)

    def test_auth_required_rejects_submission(self):
        self.broker.require_auth("daily reauthentication")
        state = self.broker.session()
        self.assertFalse(state.authenticated)
        self.assertEqual(state.auth_required_reason, "daily reauthentication")
        self.assertEqual(self.broker.submit(order()).status, SubmitStatus.REJECTED)
        self.broker.authenticate()
        self.assertEqual(self.broker.submit(order()).status, SubmitStatus.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
