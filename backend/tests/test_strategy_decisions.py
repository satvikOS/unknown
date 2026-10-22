"""Strategy decision records (ADR-0017, book algorithm A15): a mandated strategy records each evaluated
decision, including no-trade, blocked and shadow outcomes, under its own artifact id; the record has no
economic effect, cannot claim another strategy's identity or orders, and a non-mandated strategy is refused."""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness, load_policy  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402

NS = 1_000_000_000
DIGEST = hashlib.sha256(b"bars through 2026-10-21 close").hexdigest()


class StrategyDecisionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(policy=load_policy(session_template__strategy_eligibility=["owner-manual", "strategy.fixture"]))
        self.addCleanup(self.h.close)
        self.h.bootstrap()
        self.assertTrue(self.h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        self.h.quote("8.99", "9.01")

    def decision(self, principal=None, **over):
        p = dict(account_key=ACCOUNT, session_id="A", artifact_id="strategy.fixture", mode="live", outcome="NO_TRADE",
                 inputs_digest=DIGEST, observation_time_ns=self.h.clock.now_ns() - NS, instrument_id="FIX-EQ-1",
                 score="-0.42", target_quantity="0", current_quantity="0", reason="trend score below entry band")
        p.update(over)
        return self.h.cmd("record_strategy_decision", principal or self.h.strategy, **p)

    def events(self):
        return [json.loads(r["payload"])["info"] for r in self.h.desk.store.query(
            "SELECT payload FROM events WHERE type='StrategyDecisionRecorded' ORDER BY seq")]

    def test_no_trade_decision_is_recorded_without_economic_effect(self):
        before = (self.h.available("session:A"), self.h.desk.store.query("SELECT COUNT(*) AS n FROM journal_entries")[0]["n"])
        r = self.decision()
        self.assertTrue(r.accepted, r.body)
        (rec,) = self.events()
        self.assertEqual((rec["artifact_id"], rec["outcome"], rec["score"]), ("strategy.fixture", "NO_TRADE", "-0.42"))
        after = (self.h.available("session:A"), self.h.desk.store.query("SELECT COUNT(*) AS n FROM journal_entries")[0]["n"])
        self.assertEqual(before, after)
        self.assertEqual(self.h.verify(), [])

    def test_submitted_intent_is_linked_to_the_strategys_own_order(self):
        h = self.h
        sess = h.row("sessions", "session_id", "A")
        mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])
        intent = h.cmd("submit_intent", h.strategy, account_key=ACCOUNT, session_id="A", order_id="S1", instrument_id="FIX-EQ-1",
                       side="BUY", quantity="1", limit_price="9.00", deadline_ns=h.clock.now_ns() + 60 * NS,
                       policy_hash=mandate["policy_hash"], strategy_id="strategy.fixture")
        self.assertTrue(intent.accepted, intent.body)
        self.assertTrue(self.decision(outcome="INTENT_SUBMITTED", order_ids=["S1"], target_quantity="1").accepted)
        self.assertEqual(self.decision(outcome="INTENT_SUBMITTED", order_ids=[]).rejected_code, "INVALID_FIELD")
        owner_order = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id="A", order_id="O1",
                            instrument_id="FIX-EQ-1", side="BUY", quantity="1", limit_price="9.00",
                            deadline_ns=h.clock.now_ns() + 60 * NS, policy_hash=mandate["policy_hash"])
        self.assertTrue(owner_order.accepted, owner_order.body)
        self.assertEqual(self.decision(outcome="INTENT_SUBMITTED", order_ids=["O1"]).rejected_code, "UNKNOWN_ORDER")

    def test_identity_and_mandate_are_enforced(self):
        self.assertEqual(self.decision(artifact_id="owner-manual").rejected_code, "STRATEGY_IDENTITY_MISMATCH")
        other = Principal("TREND_DAILY@0123456789abcdef", Role.STRATEGY)
        self.assertEqual(self.decision(other, artifact_id=other.id).rejected_code, "STRATEGY_NOT_PERMITTED")
        self.assertEqual(self.decision(self.h.agent).rejected_code, "UNAUTHORIZED")
        self.assertEqual(self.events(), [])

    def test_shadow_mode_never_submits_and_fields_are_validated(self):
        self.assertTrue(self.decision(mode="shadow", outcome="NO_TRADE").accepted)
        self.assertEqual(self.decision(mode="shadow", outcome="INTENT_SUBMITTED", order_ids=["X"]).rejected_code, "INVALID_FIELD")
        self.assertEqual(self.decision(inputs_digest="not-a-digest").rejected_code, "INVALID_FIELD")
        self.assertEqual(self.decision(observation_time_ns=self.h.clock.now_ns() + NS).rejected_code, "INVALID_FIELD")
        with self.assertRaises(TypeError):      # floats are refused at the command boundary, before any handler
            self.decision(score=0.5)
        self.assertEqual(self.decision(score="half").rejected_code, "INVALID_FIELD")
        self.assertEqual(self.decision(outcome="MAYBE").rejected_code, "INVALID_FIELD")
        self.assertTrue(self.decision(outcome="BLOCKED", reason="pretrade rejected: STOP_LATCH").accepted)
        self.assertEqual([e["outcome"] for e in self.events()], ["NO_TRADE", "BLOCKED"])


if __name__ == "__main__":
    unittest.main()
