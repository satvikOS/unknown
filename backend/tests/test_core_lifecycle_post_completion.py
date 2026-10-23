"""BR-LED-03 and BR-SES-02: a late fill, a bust (a correction to quantity zero) or a correction that arrives after a
session ended (COMPLETED_UNSETTLED, COMPLETED_SETTLED, CLOSED) is booked to whoever holds the session's balances
now (protected:<id> after completion, unallocated after close; ADR-0008), opens the right case, never re-opens the
session or touches its successor, and any inventory it leaves there has a disposal path: an owner DISPOSAL intent
that the pretrade allows, recorded and reconciled.

Fixture arithmetic (default policy; simulated commission max(1.00, 0.005/share)):
* B1 buys 10 @ 9.00 (payable 90 + commission 1); B2 rests 1 @ 7.00; the bid 9.70 marks equity 9 + 97 = 106 >= 105
  and starts the wind-down, which cancels B2 and sells 10 @ 9.70 (receivable 97 - 1).
* Completion protects CASH 100 + PAYABLE -91 + RECEIVABLE 96 = 105.00 in protected:A.
Every number is a TEST FIXTURE, not market data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402

from unknown.broker.port import ExecutionRow  # noqa: E402
from unknown.broker.simulated import SimulatedBroker, _PendingCash  # noqa: E402
from unknown.core import order_machine  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402

F = Fixed.parse
SEC = 1_000_000_000
DAY = 86_400 * SEC
INST = "FIX-EQ-1"
LIQUID = ("CASH", "RECEIVABLE", "PAYABLE", "FEES_ACCRUED")


class BustingBroker(SimulatedBroker):
    """The simulated broker plus a bust: the provider withdraws an execution entirely, reported (as IBKR reports a
    correction) under a new execution id that names the original, here with quantity zero. Position and pending
    cash move back by the whole execution and its commission is refunded. TEST FIXTURE only."""

    def bust_execution(self, exec_key: str) -> ExecutionRow:
        original = self._exec_by_key[exec_key]
        old = original.row
        base = old.provider_exec_id.split(".")[0]
        count = self._correction_count.get(base, 0) + 1
        self._correction_count[base] = count
        provider_exec_id = f"{base}.{count:02d}"
        row = ExecutionRow(
            exec_key=f"sim:{self._tag}:{provider_exec_id}", provider_exec_id=provider_exec_id,
            client_order_id=old.client_order_id, order_ref=old.order_ref, broker_order_id=old.broker_order_id,
            instrument_id=old.instrument_id, provider_ref=old.provider_ref, side=old.side, quantity=Fixed(0),
            price=old.price, currency=old.currency, exec_time_ns=old.exec_time_ns, correction_of=old.exec_key,
            raw={"provider_exec_id": provider_exec_id, "bust_of": old.exec_key})
        notional = old.price * old.quantity
        if old.side == "BUY":
            self._adjust_position(old.instrument_id, old.currency, -old.quantity, -notional)
            cash = notional
        else:
            self._adjust_position(old.instrument_id, old.currency, old.quantity, notional)
            cash = -notional
        if original.commission is not None:
            cash = cash + original.commission.amount
        self._pending_cash.append(_PendingCash(old.currency, cash, self._settle_date(old.exec_time_ns), row.exec_key))
        if old.client_order_id in self._orders:
            self._orders[old.client_order_id].filled = self._orders[old.client_order_id].filled - old.quantity
        self._record_execution(row, None, False, 0)
        return row


# ---------------------------------------------------------------- helpers
def executions(h: Harness, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM executions WHERE order_id=? ORDER BY rowid", (order_id,))]


def cases(h: Harness, kind: str, status: str = "OPEN") -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM exceptions WHERE kind=? AND status=? ORDER BY rowid", (kind, status))]


def liquid(h: Harness, owner: str) -> Fixed:
    total = Fixed(0)
    for code in LIQUID:
        total = total + h.balance(code, owner)
    return total


def lots(h: Harness, owner: str) -> list[dict]:
    rows = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM lots ORDER BY rowid")]
    return [l for l in rows if l["owner"] == owner and not F(l["open_qty"]).is_zero()]


def owner_is_empty(h: Harness, owner: str) -> bool:
    return all(F(r["amount"]).is_zero() for r in h.desk.store.query(
        "SELECT amount FROM balances WHERE account_key=? AND owner=? AND account_code IN "
        "('CASH','RECEIVABLE','PAYABLE','FEES_ACCRUED','INVENTORY')", (ACCOUNT, owner))) and not lots(h, owner)


def successors(h: Harness) -> list[tuple[str, str]]:
    return [(r["predecessor_id"], r["successor_id"]) for r in h.desk.store.query(
        "SELECT predecessor_id, successor_id FROM successors ORDER BY predecessor_id")]


def wind_down_order(h: Harness) -> dict:
    rows = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM orders WHERE json_extract(row,'$.purpose')='WIND_DOWN'")]
    (wd,) = [o for o in rows if o["state"] not in order_machine.TERMINAL_STATES]
    return wd


def policy_hash(h: Harness, session_id: str) -> str:
    sess = h.row("sessions", "session_id", session_id)
    return json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])["policy_hash"]


def disposal(h: Harness, order_id: str, qty: str, limit: str, session_id: str = "A", principal: Principal | None = None,
             side: str = "SELL"):
    return h.cmd("submit_intent", principal or h.owner, account_key=ACCOUNT, session_id=session_id, order_id=order_id,
                 instrument_id=INST, side=side, quantity=qty, limit_price=limit, purpose="DISPOSAL",
                 deadline_ns=h.clock.now_ns() + 60 * SEC, policy_hash=policy_hash(h, session_id))


class _Base(unittest.TestCase):
    def harness(self, broker_cls=SimulatedBroker) -> Harness:
        from support import T0
        from unknown.domain.clock import ManualClock
        clock = ManualClock(T0)
        broker = broker_cls(clock, initial_settled_cash=F("300.00"), calendar=WEEKDAYS_ONLY, settlement_days=1)
        h = Harness(broker=broker, clock=clock)
        self.addCleanup(h.close)
        return h

    def sound(self, h: Harness) -> None:
        self.assertEqual(h.verify(), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])

    def to_wind_down(self, h: Harness, *, resting_buy: bool) -> dict:
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("B1", "BUY", "10", "9.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("10"), F("9.00"))
        h.pump()
        if resting_buy:
            r = h.intent("B2", "BUY", "1", "7.00", "A")
            self.assertTrue(r.accepted, r.body)
            h.pump()
            self.assertEqual(h.row("orders", "order_id", "B2")["state"], "ACKNOWLEDGED")
        h.quote("9.70", "9.72")
        h.tick()
        self.assertEqual(h.session_state("A"), "WINDING_DOWN")
        return wind_down_order(h)

    def finish(self, h: Harness) -> None:
        h.tick()
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        h.tick()

    def completed(self, h: Harness, *, resting_buy: bool = False) -> dict:
        """Session A completed at protected 105.00 (wind-down sold 10 @ 9.70); returns the sell order."""
        wd = self.to_wind_down(h, resting_buy=resting_buy)
        h.pump()
        if resting_buy:
            self.assertEqual(h.row("orders", "order_id", "B2")["state"], "CANCELED")
        h.broker.fill(wd["order_id"], F("10"), F("9.70"))
        h.pump()
        self.finish(h)
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("COMPLETED_UNSETTLED", "TARGET_REACHED"))
        self.assertEqual(liquid(h, "protected:A"), F("105.00"))
        self.assertEqual(len(successors(h)), 1)
        return wd

    def settle_next_day(self, h: Harness):
        h.clock.advance(DAY)
        h.quote("9.70", "9.72")
        h.broker.advance_settlement()
        return h.reconcile()

    def assert_session_stands(self, h: Harness, state: str = "COMPLETED_UNSETTLED") -> None:
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), (state, "TARGET_REACHED"))
        self.assertEqual(F(a["protected_value"]), F("105.00"), "completion stands on the evidence it was decided on")
        self.assertEqual(len(successors(h)), 1, "the successor is neither unwound nor duplicated")
        self.assertTrue(owner_is_empty(h, "session:A"), "nothing is booked to the ended session itself")


class LateFillTests(_Base):
    def test_late_fill_after_completion_lands_in_protected_and_is_disposed_and_reconciled(self):
        """BR-SES-02, BR-LED-03: B2 (canceled during the wind-down) reports a fill after A completed. The share and
        its payable land in protected:A (never session:A); the completion, its protected value and the successor
        stand; a blocking POST_COMPLETION_ADJUSTMENT (booked_to protected:A) and an EXECUTION_ANOMALY (fill after a
        terminal state) open. Disposal: the anomaly blocks reductions until the owner acknowledges it; then a
        DISPOSAL sell of protected:A's share passes the pretrade (the decision record says why), fills, books to
        protected:A, reconciles CLEAN and settles into protected:A's cash (105 - 8 + 8.70 = 105.70)."""
        h = self.harness()
        self.completed(h, resting_buy=True)
        h.broker.fill("B2", F("1"), F("7.00"), allow_terminal=True)
        h.pump()
        (late,) = executions(h, "B2")
        self.assertEqual((late["owner"], late["status"]), ("protected:A", "ACTIVE"))
        (lot,) = lots(h, "protected:A")
        self.assertEqual((F(lot["open_qty"]), F(lot["open_cost"])), (F("1"), F("7.00")))
        self.assertEqual(h.balance("PAYABLE", "protected:A"), F("-99.00"))   # -91 - 7 - 1 (late commission)
        self.assert_session_stands(h)
        (post,) = cases(h, "POST_COMPLETION_ADJUSTMENT")
        self.assertEqual((post["blocking"], post["session_id"], post["owner_hint"], post["detail"]["booked_to"]),
                         (True, "A", "protected:A", "protected:A"))
        (anomaly,) = cases(h, "EXECUTION_ANOMALY")
        self.assertEqual(anomaly["detail"]["anomalies"][0]["issue"], "fill after terminal state")
        self.sound(h)

        refused = disposal(h, "D1", "1", "9.70")
        self.assertEqual(refused.rejected_code, "OPEN_EXCEPTIONS", "an execution anomaly blocks reductions (pretrade)")
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=anomaly["case_id"],
                                    note="late fill of B2 confirmed on the broker's trade report").accepted)
        r = disposal(h, "D1", "1", "9.70")
        self.assertTrue(r.accepted, r.body)
        d1 = h.row("orders", "order_id", "D1")
        self.assertEqual((d1["owner"], d1["purpose"], d1["session_id"], d1["state"]), ("protected:A", "DISPOSAL", "A", "RESERVED"))
        (ev,) = [json.loads(x["payload"]) for x in h.desk.store.query("SELECT payload FROM events WHERE type='OrderAccepted' AND order_id='D1'")]
        (check,) = [c for c in ev["info"]["decision"]["checks"] if c["check"] == "SESSION_STATE"]
        self.assertTrue(check["passed"])
        self.assertIn("owner disposal of inventory owned by protected:A", check["detail"])
        self.assertIn("COMPLETED_UNSETTLED", check["detail"])
        hold = dict(h.desk.store.query("SELECT * FROM holds WHERE order_id='D1'")[0])
        self.assertEqual((hold["owner"], hold["kind"], F(hold["amount"])), ("protected:A", "QUANTITY", F("1")))

        h.pump()
        h.broker.fill("D1", F("1"), F("9.70"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "D1")["state"], "FILLED")
        self.assertEqual(lots(h, "protected:A"), [])
        (sale,) = executions(h, "D1")
        self.assertEqual(sale["owner"], "protected:A")
        self.assertEqual(len(cases(h, "POST_COMPLETION_ADJUSTMENT")), 1, "the disposal's own fill opens no new case")
        self.assertEqual(liquid(h, "protected:A"), F("105.70"))
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assert_session_stands(h)
        rec = self.settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.balance("CASH", "protected:A"), F("105.70"))
        self.assertEqual(h.session_state("A"), "COMPLETED_SETTLED")
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=post["case_id"], note="late fill disposed").accepted)
        self.assertEqual(cases(h, "POST_COMPLETION_ADJUSTMENT"), [])
        self.assert_session_stands(h, "COMPLETED_SETTLED")
        self.sound(h)

    def test_late_fill_after_close_lands_in_unallocated_with_a_case_and_is_disposed(self):
        """BR-SES-02, ADR-0008: after an owner-ended session CLOSED, B2's late fill is booked to unallocated (where
        the closed session's balances went) and, unlike before, also opens a blocking POST_COMPLETION_ADJUSTMENT
        naming unallocated. A DISPOSAL naming the closed session sells unallocated's share; reconciliation is CLEAN."""
        h = self.harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("B1", "BUY", "10", "9.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F("10"), F("9.00"))
        h.pump()
        self.assertTrue(h.intent("B2", "BUY", "1", "7.00", "A").accepted)
        h.pump()
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        wd = wind_down_order(h)
        h.pump()
        h.broker.fill(wd["order_id"], F("10"), F(wd["limit_price"]))
        h.pump()
        self.finish(h)
        self.assertEqual(h.row("sessions", "session_id", "A")["state"], "CLOSED")
        before = liquid(h, "unallocated")
        h.broker.fill("B2", F("1"), F("7.00"), allow_terminal=True)
        h.pump()
        (lot,) = lots(h, "unallocated")
        self.assertEqual(F(lot["open_qty"]), F("1"))
        self.assertEqual(liquid(h, "unallocated"), before - F("8.00"))
        self.assertTrue(owner_is_empty(h, "session:A"))
        (post,) = cases(h, "POST_COMPLETION_ADJUSTMENT")
        self.assertEqual((post["blocking"], post["owner_hint"], post["detail"]["session_state"]), (True, "unallocated", "CLOSED"))
        (anomaly,) = cases(h, "EXECUTION_ANOMALY")
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=anomaly["case_id"], note="late fill confirmed").accepted)
        h.quote("8.99", "9.01")
        r = disposal(h, "D1", "1", "8.99")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.row("orders", "order_id", "D1")["owner"], "unallocated")
        h.pump()
        h.broker.fill("D1", F("1"), F("8.99"))
        h.pump()
        self.assertEqual(lots(h, "unallocated"), [])
        self.assertEqual(liquid(h, "unallocated"), before - F("8.00") + F("7.99"))
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.session_state("A"), "CLOSED")
        self.sound(h)

    def test_disposal_is_only_an_owner_sell_of_an_ended_session_and_only_of_what_it_owns(self):
        """BR-LED-03: DISPOSAL is refused for a strategy, for a buy, for a session that has not ended (it sells with
        EXIT) and for more than the owner of the residue holds (pretrade OWNED_QUANTITY)."""
        h = self.harness()
        self.completed(h, resting_buy=True)
        h.broker.fill("B2", F("1"), F("7.00"), allow_terminal=True)
        h.pump()
        (anomaly,) = cases(h, "EXECUTION_ANOMALY")
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=anomaly["case_id"], note="confirmed").accepted)
        strategy = Principal("strategy.fixture", Role.STRATEGY)
        self.assertEqual(disposal(h, "D1", "1", "9.70", principal=strategy).rejected_code, "UNAUTHORIZED")
        self.assertEqual(disposal(h, "D2", "1", "9.70", side="BUY").rejected_code, "INVALID_FIELD")
        self.assertEqual(disposal(h, "D3", "2", "9.70").rejected_code, "OWNED_QUANTITY")
        (succ,) = [s for _, s in successors(h)]
        self.assertEqual(h.session_state(succ), "RUNNING")
        self.assertEqual(disposal(h, "D4", "1", "9.70", session_id=succ).rejected_code, "SESSION_NOT_ENDED")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM orders WHERE order_id LIKE 'D%'")[0]["n"], 0)
        self.sound(h)


class BustAndCorrectionTests(_Base):
    def test_bust_of_the_wind_down_sell_returns_the_shares_to_protected_for_disposal(self):
        """BR-LED-03 (c), BR-SES-02: after completion the broker busts the wind-down sell (a correction to quantity
        zero). Before this change the zero-quantity replacement made the command fail and roll back, discarding the
        broker fact. Now the sale is reversed onto protected:A: the 10 shares come back with their 90.00 cost, the
        sale's receivable and expected settlement are withdrawn, the commission is refunded; the bust is recorded
        with no booking of its own and a blocking POST_COMPLETION_ADJUSTMENT opens. The session, its protected value
        and its successor stand. A DISPOSAL sells the shares again at 9.50; reconciliation is CLEAN and the next day
        protected:A settles at 100 - 91 + 95 - 1 = 103.00."""
        h = self.harness(BustingBroker)
        wd = self.completed(h)
        (sell,) = executions(h, wd["order_id"])
        bust = h.broker.bust_execution(sell["exec_key"])
        h.pump()
        busted = h.row("executions", "exec_key", bust.exec_key)
        self.assertEqual((busted["status"], busted["quantity"], busted["correction_of"]), ("BUST", "0", sell["exec_key"]))
        self.assertEqual(h.row("executions", "exec_key", sell["exec_key"])["status"], "CORRECTED")
        (lot,) = lots(h, "protected:A")
        self.assertEqual((F(lot["open_qty"]), F(lot["open_cost"])), (F("10"), F("90.00")))
        self.assertEqual(h.balance("RECEIVABLE", "protected:A"), F("0"))
        self.assertEqual(liquid(h, "protected:A"), F("9.00"))   # CASH 100 + PAYABLE -91; the shares are inventory
        self.assertEqual(h.balance("INVENTORY", "protected:A", instrument_id=INST), F("90.00"))
        stl = h.row("settlements", "settlement_id", f"stl:{sell['exec_key']}")
        self.assertEqual(stl["status"], "CANCELLED")
        self.assert_session_stands(h)
        (post,) = cases(h, "POST_COMPLETION_ADJUSTMENT")
        self.assertEqual((post["blocking"], post["owner_hint"]), (True, "protected:A"))
        self.assertEqual(cases(h, "EXECUTION_ANOMALY"), [], "a bust of a finished order blocks entries, not the disposal")
        self.sound(h)

        h.quote("9.50", "9.52")
        r = disposal(h, "D1", "10", "9.50")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        h.broker.fill("D1", F("10"), F("9.50"))
        h.pump()
        self.assertEqual(lots(h, "protected:A"), [])
        self.assertEqual(liquid(h, "protected:A"), F("103.00"))
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        rec = self.settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.balance("CASH", "protected:A"), F("103.00"))
        self.assert_session_stands(h, "COMPLETED_SETTLED")
        self.sound(h)

    def test_bust_of_a_sold_buy_after_completion_restates_on_protected_and_flags_the_sold_shares(self):
        """BR-LED-03 (b, c): the broker busts B1 after A completed, but its 10 shares were already sold by the
        wind-down. The reversal and the restatement post to protected:A (inventory nets to zero; the sale's cost is
        restated, so protected:A's liquid value rises by the 91.00 payable and commission that are withdrawn); a
        critical correction-below-disposed case names protected:A, and reconciliation reports the broker's short
        position as a blocking POSITION_MISMATCH. The ledger invariants hold and the completion stands."""
        h = self.harness(BustingBroker)
        self.completed(h)
        (buy,) = executions(h, "B1")
        h.broker.bust_execution(buy["exec_key"])
        h.pump()
        self.assertEqual(h.balance("INVENTORY", "protected:A", instrument_id=INST), F("0"))
        self.assertEqual(lots(h, "protected:A"), [])
        self.assertEqual(liquid(h, "protected:A"), F("196.00"))
        (case,) = [c for c in cases(h, "EXECUTION_ANOMALY") if c["key"].startswith("correction-below-disposed:")]
        self.assertEqual((case["severity"], case["owner_hint"]), ("critical", "protected:A"))
        self.assertEqual(len(cases(h, "POST_COMPLETION_ADJUSTMENT")), 1)
        self.assert_session_stands(h)
        self.sound(h)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "DIFFERENCES")
        self.assertIn({"instrument_id": INST, "broker": "-10", "ledger": "0"},
                      [d["detail"] for d in rec.body["differences"] if d["kind"] == "POSITION_MISMATCH"])
        self.assertEqual(h.verify(), [])

    def test_bust_of_a_working_orders_fill_restores_its_reservation_and_opens_a_case(self):
        """A bust while the order still works (session RUNNING): the 4 busted shares and their payable are reversed
        on session:A, the order's cumulative quantity returns to 0 and its cash hold is re-reserved for all 10
        shares; a blocking EXECUTION_ANOMALY asks for the broker's remaining quantity to be confirmed.
        Reconciliation agrees with the broker (nothing filled, nothing held)."""
        h = self.harness(BustingBroker)
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("B1", "BUY", "10", "9.00", "A").accepted)
        h.pump()
        hold_before = dict(h.desk.store.query("SELECT * FROM holds WHERE order_id='B1'")[0])
        h.broker.fill("B1", F("4"), F("9.00"))
        h.pump()
        (fill,) = executions(h, "B1")
        h.broker.bust_execution(fill["exec_key"])
        h.pump()
        b1 = h.row("orders", "order_id", "B1")
        self.assertEqual((b1["state"], b1["cum_qty"], b1["leaves_qty"]), ("PARTIALLY_FILLED", "0", "10"))
        hold = dict(h.desk.store.query("SELECT * FROM holds WHERE order_id='B1'")[0])
        self.assertEqual((hold["status"], F(hold["amount"])), ("ACTIVE", F(hold_before["amount"])))
        self.assertEqual(lots(h, "session:A"), [])
        (case,) = cases(h, "EXECUTION_ANOMALY")
        self.assertEqual((case["key"], case["order_id"]), (f"bust-working:{executions(h, 'B1')[-1]['exec_key']}", "B1"))
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.sound(h)

    def test_price_correction_after_completed_settled_books_on_protected_and_the_session_stays_settled(self):
        """BR-LED-03, ADR-0008: after A is COMPLETED_SETTLED, the broker corrects B1's price 9.00 -> 8.90. The
        settled payable is compensated and the corrected trade rebooked, all on protected:A (+1.00), with a blocking
        POST_COMPLETION_ADJUSTMENT; A stays COMPLETED_SETTLED and the next reconciliation settles protected:A's cash
        at 106.00."""
        h = self.harness()
        self.completed(h)
        rec = self.settle_next_day(h)
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.session_state("A"), "COMPLETED_SETTLED")
        self.assertEqual(h.balance("CASH", "protected:A"), F("105.00"))
        (buy,) = executions(h, "B1")
        h.broker.correct_execution(buy["exec_key"], F("10"), F("8.90"))
        h.pump()
        self.assertEqual(liquid(h, "protected:A"), F("106.00"))
        self.assertEqual(lots(h, "protected:A"), [])
        (post,) = cases(h, "POST_COMPLETION_ADJUSTMENT")
        self.assertEqual((post["blocking"], post["owner_hint"]), (True, "protected:A"))
        self.assert_session_stands(h, "COMPLETED_SETTLED")
        h.broker.advance_settlement()
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.balance("CASH", "protected:A"), F("106.00"))
        self.assert_session_stands(h, "COMPLETED_SETTLED")
        self.sound(h)


if __name__ == "__main__":
    unittest.main()
