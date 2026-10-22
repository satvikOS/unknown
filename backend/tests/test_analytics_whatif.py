"""What-if sandbox: the dry run is the live gate's own code and never writes to the core (CAP-PORT-12).

The shared-code test walks a randomized sequence of real ledger states (intents, partial fills,
quote changes, clock moves) and, before every real intent, dry-runs the identical intent; the
approval, the decisive code and every check must equal what the core then decides. All
values are TEST FIXTURES."""
from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_analytics_fixtures import F, SEC, booted, buy, db_hash, quotes, ro  # noqa: E402
from support import ACCOUNT, DEPLOYMENT  # noqa: E402

from unknown.analytics.whatif import HYPOTHETICAL, dry_run, is_stale  # noqa: E402


def _policy_hash(h, sid: str) -> str:
    sess = h.row("sessions", "session_id", sid)
    return json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])[
        "policy_hash"]


def _real(h, intent: dict) -> tuple[bool, str, dict | None, list]:
    r = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, **intent)
    if r.accepted:
        ev = h.desk.store.query("SELECT payload FROM events WHERE type='OrderAccepted' AND order_id=?", (intent["order_id"],))
        holds = [dict(x) for x in h.desk.store.query("SELECT kind, unit, amount, status FROM holds WHERE order_id=?",
                                                     (intent["order_id"],))]
        return True, "APPROVED", json.loads(ev[0]["payload"])["info"]["decision"], holds
    return False, r.body["code"], r.body.get("details", {}).get("decision"), []


class SharedCodeTest(unittest.TestCase):
    def test_dry_run_equals_the_live_gate_over_random_states(self):
        h = booted(self)
        conn = ro(self, h)
        rng = random.Random(20261022)
        policy_hash = _policy_hash(h, "S1")
        h.quote("9.00", "9.02")
        compared, codes = 0, set()
        for step in range(160):
            action = rng.choices(["intent", "fill", "quote", "advance"], weights=[5, 2, 2, 1])[0]
            if action == "quote":
                bid = F(f"{rng.uniform(8.5, 10.5):.2f}")
                h.quote(bid.to_str(), (bid + F("0.02")).to_str())
            elif action == "advance":
                h.clock.advance(rng.choice([1, 2, 4, 6]) * SEC)
            elif action == "fill":
                working = [json.loads(r["row"]) for r in h.desk.store.query(
                    "SELECT row FROM orders WHERE state IN ('ACKNOWLEDGED','PARTIALLY_FILLED')")]
                if working:
                    o = rng.choice(working)
                    qty = F(o["leaves_qty"]) if rng.random() < 0.6 else F("1")
                    h.broker.fill(o["order_id"], qty, F(o["limit_price"]))
                    h.pump()
            else:
                side = "BUY" if rng.random() < 0.65 else "SELL"
                q = h.desk.quotes.get("FIX-EQ-1")
                ref = (q.ask if side == "BUY" else q.bid) if q else F("9.00")
                limit = (ref + F(rng.choice(["-0.20", "-0.05", "0", "0.03", "0.15"]))).to_str()
                intent = {"session_id": "S1", "order_id": f"R{step}", "instrument_id": "FIX-EQ-1", "side": side,
                          "quantity": str(rng.choice([1, 2, 3, 5, 8, 12])), "limit_price": limit,
                          "deadline_ns": h.clock.now_ns() + 60 * SEC, "policy_hash": policy_hash}
                before = db_hash(h.db_path)
                dry = dry_run(conn, ACCOUNT, intent, quotes(h), now_ns=h.clock.now_ns(), deployment_id=DEPLOYMENT)
                self.assertEqual(db_hash(h.db_path), before, "a dry run changed the core")
                approved, code, decision, holds = _real(h, intent)
                self.assertEqual((dry["approved"], dry["code"]), (approved, code), f"step {step}: {intent}")
                self.assertEqual(dry["decision"], decision, f"step {step}")
                self.assertEqual(dry["reservation_preview"], holds, f"step {step}")
                compared += 1
                codes.add(code)
                h.pump()
            self.assertEqual(h.verify(), [])
        self.assertGreater(compared, 60)
        self.assertTrue({"APPROVED", "CASH", "OWNED_QUANTITY"} <= codes, codes)


class DryRunSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.h = booted(self)
        self.conn = ro(self, self.h)
        buy(self.h, "S1", "B1", "5", "9.00", bid="8.99", ask="9.01")

    def intent(self, **kw) -> dict:
        return {"session_id": "S1", "instrument_id": "FIX-EQ-1", **kw}

    def test_unfunded_buy_fails_cash_with_exact_shortfall(self):
        r = dry_run(self.conn, ACCOUNT, self.intent(side="BUY", quantity="11", limit_price="9.00"), quotes(self.h),
                    now_ns=self.h.clock.now_ns(), deployment_id=DEPLOYMENT)
        self.assertFalse(r["approved"])
        self.assertEqual(r["code"], "CASH")
        self.assertEqual(r["label"], HYPOTHETICAL)
        cash = r["impact"]["cash"]
        # required = 99.00 + envelope (1.00 minimum + 20% margin) = 100.20; available = 100 - 46 = 54.00
        self.assertEqual((cash["required_reservation"], cash["available_before"], cash["shortfall"]),
                         ("100.200000", "54.000000", "46.200000"))
        self.assertEqual(r["reservation_preview"], [])

    def test_sell_beyond_unreserved_quantity_fails(self):
        r = dry_run(self.conn, ACCOUNT, self.intent(side="SELL", quantity="6", limit_price="8.99"), quotes(self.h),
                    now_ns=self.h.clock.now_ns(), deployment_id=DEPLOYMENT)
        self.assertEqual(r["code"], "OWNED_QUANTITY")
        self.assertEqual(r["impact"]["quantity"]["excess_over_unreserved"], "1")

    def test_unsettled_sale_proceeds_do_not_fund_a_buy(self):
        h = self.h
        r = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id="S1", order_id="S-ALL", instrument_id="FIX-EQ-1",
                  side="SELL", quantity="5", limit_price="8.99", deadline_ns=h.clock.now_ns() + 60 * SEC,
                  policy_hash=_policy_hash(h, "S1"))
        self.assertTrue(r.accepted, r.body)
        h.pump()
        h.broker.fill("S-ALL", F("5"), F("8.99"))
        h.pump()
        self.assertEqual(h.verify(), [])
        # Liquid value is ~97.95 but only 54.00 is settled and available; a 60.00 buy must fail CASH.
        d = dry_run(self.conn, ACCOUNT, self.intent(side="BUY", quantity="6", limit_price="9.01"), quotes(h),
                    now_ns=h.clock.now_ns(), deployment_id=DEPLOYMENT)
        self.assertEqual(d["code"], "CASH")
        self.assertEqual(d["impact"]["cash"]["available_before"], "54.000000")

    def test_approved_buy_previews_the_reservation_and_impact(self):
        r = dry_run(self.conn, ACCOUNT, self.intent(side="BUY", quantity="3", limit_price="9.01"), quotes(self.h),
                    now_ns=self.h.clock.now_ns(), deployment_id=DEPLOYMENT)
        self.assertTrue(r["approved"])
        self.assertEqual(r["reservation_preview"], [{"kind": "CASH", "unit": "USD", "amount": "28.230000", "status": "ACTIVE"}])
        f = r["impact"]["if_filled"]
        self.assertEqual((f["position_value_before"], f["position_value_after"]), ("44.950000", "71.920000"))
        self.assertIn("whatif.cash@1", r["lineage"]["formulas"])

    def test_never_writes_and_becomes_stale_when_the_core_moves(self):
        before = db_hash(self.h.db_path)
        r = dry_run(self.conn, ACCOUNT, self.intent(side="BUY", quantity="1", limit_price="9.01"), quotes(self.h),
                    now_ns=self.h.clock.now_ns(), deployment_id=DEPLOYMENT)
        self.assertEqual(db_hash(self.h.db_path), before)
        self.assertFalse(is_stale(self.conn, r))
        buy(self.h, "S1", "B2", "1", "9.00", bid="8.99", ask="9.01")
        self.assertTrue(is_stale(self.conn, r))

    def test_quote_from_after_the_decision_instant_is_not_used(self):
        q = quotes(self.h)
        r = dry_run(self.conn, ACCOUNT, self.intent(side="BUY", quantity="1", limit_price="9.01"), q,
                    now_ns=self.h.clock.now_ns() - 1, deployment_id=DEPLOYMENT)
        failed = {c["check"] for c in r["decision"]["checks"] if not c["passed"]}
        self.assertIn("QUOTE_FRESH", failed)

    def test_stale_quote_refuses_exits_too(self):
        r = dry_run(self.conn, ACCOUNT, self.intent(side="SELL", quantity="5", limit_price="8.99"), quotes(self.h),
                    now_ns=self.h.clock.now_ns() + 10 * SEC, deployment_id=DEPLOYMENT)
        self.assertEqual(r["code"], "QUOTE_FRESH")

    def test_stop_latch_blocks_buys_but_not_reductions(self):
        h = self.h
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT, reason="test").accepted)
        now = h.clock.now_ns()
        b = dry_run(self.conn, ACCOUNT, self.intent(side="BUY", quantity="1", limit_price="9.01"), quotes(h), now_ns=now,
                    deployment_id=DEPLOYMENT)
        s = dry_run(self.conn, ACCOUNT, self.intent(side="SELL", quantity="1", limit_price="8.99"), quotes(h), now_ns=now,
                    deployment_id=DEPLOYMENT)
        self.assertEqual(b["code"], "STOP_LATCH")
        self.assertNotIn("STOP_LATCH", {c["check"] for c in s["decision"]["checks"]})


if __name__ == "__main__":
    unittest.main()
