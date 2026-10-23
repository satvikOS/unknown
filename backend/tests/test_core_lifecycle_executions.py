"""BR-ORD-05 and BR-REF-21: the same execution id redelivered with changed economic content, end to end through the
gateway runner; and BR-OPS-19: the SELF_CROSS pretrade rejection through submit_intent.

Decision for a changed redelivery (recorded in core/handlers/broker.py): the booked original stands and nothing of the
amended content is booked. The received payload is recorded in full and a blocking critical EXECUTION_ANOMALY,
attributed to the execution's order, session and owner, holds new exposure (entries and, because it is an execution
anomaly, reductions) until the owner resolves it. A genuine amendment reaches the ledger as a broker correction (a new
execution id) or shows as a reconciliation difference.

Every number is a TEST FIXTURE (see support.py), not market data.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402

from unknown.broker.port import ExecutionEvent, ExecutionRow  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

F = Fixed.parse


def cases(h: Harness, kind: str, status: str = "OPEN") -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM exceptions WHERE kind=? AND status=? ORDER BY rowid", (kind, status))]


def events(h: Harness, event_type: str) -> list[dict]:
    return [json.loads(r["payload"]) for r in h.desk.store.query(
        "SELECT payload FROM events WHERE type=? ORDER BY seq", (event_type,))]


def ledger_state(h: Harness) -> dict:
    """Everything the original booking wrote: postings, balances, lots, holds, executions, orders, settlements."""
    q = h.desk.store.query
    return {t: [tuple(r) for r in q(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in ("postings", "balances", "lots", "holds", "executions", "orders", "settlements")}


class _Base(unittest.TestCase):
    def harness(self) -> Harness:
        h = Harness()
        self.addCleanup(h.close)
        return h

    def running(self, h: Harness) -> None:
        h.bootstrap()
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A")
        assert r.accepted and r.body["state"] == "RUNNING", r.body

    def sound(self, h: Harness) -> None:
        self.assertEqual(h.verify(), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])


class ChangedRedeliveryTests(_Base):
    def _booked(self, h: Harness) -> ExecutionRow:
        self.running(h)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("B1", "BUY", "4", "9.00", "A").accepted)
        h.pump()
        row = h.broker.fill("B1", F("4"), F("9.00"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "FILLED")
        return row

    def test_same_execution_id_with_changed_price_keeps_the_original_and_blocks_until_the_owner_resolves(self):
        """BR-ORD-05, BR-REF-21: E1 (4 @ 9.00) is booked; the broker then re-sends E1 with price 8.50 through the
        gateway runner. Nothing changes in the ledger, lots, order or settlements; an ExecutionContentConflict event
        records the received payload; one critical blocking EXECUTION_ANOMALY carries the stored and received
        economics and names B1, session A and its owner. New buys and sells are refused, the session is frozen for
        review, and a redelivery of the original is a plain duplicate while one of the same changed payload adds no
        second case. After the owner resolves it the original still stands and reconciliation is CLEAN."""
        h = self.harness()
        original = self._booked(h)
        before = ledger_state(h)
        changed = dataclasses.replace(original, price=F("8.50"))
        h.runner.apply_event(ExecutionEvent(changed))
        self.assertEqual(ledger_state(h), before, "the amended content is recorded, never booked")
        (ev,) = events(h, "ExecutionContentConflict")
        self.assertEqual((ev["info"]["exec_key"], ev["info"]["received"]["price"]), (original.exec_key, "8.50"))
        (case,) = cases(h, "EXECUTION_ANOMALY")
        self.assertEqual((case["blocking"], case["severity"], case["order_id"], case["session_id"], case["owner_hint"]),
                         (True, "critical", "B1", "A", "session:A"))
        self.assertEqual((case["detail"]["stored"]["price"], case["detail"]["received"]["price"]), ("9.00", "8.50"))
        self.assertIn("original stays booked", case["detail"]["decision"])

        h.runner.apply_event(ExecutionEvent(original))            # identical content: a duplicate, no record
        h.runner.apply_event(ExecutionEvent(changed))             # the same amended payload again: no second case
        self.assertEqual(len(cases(h, "EXECUTION_ANOMALY")), 1)
        self.assertEqual(len(events(h, "ExecutionContentConflict")), 2)
        self.assertEqual(ledger_state(h), before)

        h.quote("8.99", "9.01")
        self.assertEqual(h.intent("B2", "BUY", "1", "9.00", "A").rejected_code, "OPEN_EXCEPTIONS")
        self.assertEqual(h.intent("S1", "SELL", "1", "9.00", "A").rejected_code, "OPEN_EXCEPTIONS")
        h.tick()
        self.assertEqual(h.session_state("A"), "FAILED_REVIEW")
        self.sound(h)

        r = h.owner_cmd("resolve_exception", case_id=case["case_id"],
                        note="broker statement shows E1 at 9.00; the 8.50 resend was a feed error")
        self.assertTrue(r.accepted, r.body)
        h.quote("8.99", "9.01")
        h.tick()
        self.assertEqual(h.session_state("A"), "SUSPENDED", "review ends; a resume needs the owner")
        self.assertEqual(ledger_state(h), before)
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.sound(h)

    def test_a_different_amendment_of_the_same_id_opens_its_own_case(self):
        """BR-ORD-05: a second, different amendment of E1 (quantity 5) is another case; the original still stands."""
        h = self.harness()
        original = self._booked(h)
        before = ledger_state(h)
        h.runner.apply_event(ExecutionEvent(dataclasses.replace(original, price=F("8.50"))))
        h.runner.apply_event(ExecutionEvent(dataclasses.replace(original, quantity=F("5"))))
        self.assertEqual(len(cases(h, "EXECUTION_ANOMALY")), 2)
        self.assertEqual({c["detail"]["received"]["quantity"] for c in cases(h, "EXECUTION_ANOMALY")}, {"4", "5"})
        self.assertEqual(ledger_state(h), before)
        self.sound(h)


class SelfCrossTests(_Base):
    def _position(self, h: Harness, qty: str) -> None:
        self.running(h)
        h.quote("8.99", "9.01")
        self.assertTrue(h.intent("B1", "BUY", qty, "9.00", "A").accepted)
        h.pump()
        h.broker.fill("B1", F(qty), F("9.00"))
        h.pump()
        h.quote("9.99", "10.01")

    def _decision(self, h: Harness, r) -> dict:
        return {c["check"]: c for c in r.body["details"]["decision"]["checks"]}

    def test_buy_that_would_trade_against_our_working_sell_is_refused_self_cross(self):
        """BR-OPS-19 (book Ch. 25): with our sell S1 working at 10.00, a buy limited at 10.00 would trade against it;
        submit_intent refuses it with SELF_CROSS (every earlier check passes, so SELF_CROSS is the decisive code), the
        refusal is a recorded decision and nothing is reserved. A buy at 9.99 does not cross and is accepted; once
        S1 is canceled a buy at 10.00 is accepted too."""
        h = self.harness()
        self._position(h, "5")
        self.assertTrue(h.intent("S1", "SELL", "5", "10.00", "A").accepted)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "ACKNOWLEDGED")
        r = h.intent("B2", "BUY", "1", "10.00", "A")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "SELF_CROSS")
        checks = self._decision(h, r)
        self.assertFalse(checks["SELF_CROSS"]["passed"])
        self.assertEqual([c for c, v in checks.items() if not v["passed"]], ["SELF_CROSS"])
        self.assertIsNone(h.row("orders", "order_id", "B2"))
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) AS n FROM holds WHERE order_id='B2'")[0]["n"], 0)
        (rej,) = [e for e in events(h, "CommandRejected") if e["info"]["rejection"]["code"] == "SELF_CROSS"]
        self.assertEqual(rej["info"]["kind"], "submit_intent")
        self.assertTrue(h.intent("B3", "BUY", "1", "9.99", "A").accepted, "below our sell's limit: no cross")
        self.assertTrue(h.cmd("request_cancel", order_id="S1").accepted)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "CANCELED")
        self.assertTrue(h.intent("B4", "BUY", "1", "10.00", "A").accepted, "a canceled sell no longer counts")
        self.sound(h)

    def test_sell_that_would_trade_against_our_working_buy_is_refused_self_cross(self):
        """BR-OPS-19: the other side. With our buy B3 working at 9.99, a sell of an unreserved share limited at 9.99
        is refused SELF_CROSS; at 10.00 it does not cross and is accepted."""
        h = self.harness()
        self._position(h, "6")
        self.assertTrue(h.intent("S1", "SELL", "5", "10.00", "A").accepted)
        self.assertTrue(h.intent("B3", "BUY", "1", "9.99", "A").accepted)
        h.pump()
        r = h.intent("S2", "SELL", "1", "9.99", "A")
        self.assertEqual(r.rejected_code, "SELF_CROSS")
        self.assertEqual([c for c, v in self._decision(h, r).items() if not v["passed"]], ["SELF_CROSS"])
        self.assertTrue(h.intent("S3", "SELL", "1", "10.00", "A").accepted)
        self.sound(h)


if __name__ == "__main__":
    unittest.main()
