"""Session state machine, completion, successor and funding semantics (ADR-0008, ADR-0006, ADR-0010).

Every number below is a TEST FIXTURE (seed 100.00, target 105.00, fixture
prices), not market data, a forecast or a funding authorization.

Fixture arithmetic used throughout (default policy, simulated commission
schedule max(1.00, 0.005/share)):

* BUY 10 @ 9.00  -> INVENTORY 90.00, PAYABLE -90.00, final commission 1.00
  -> session liquid value = CASH 100 + PAYABLE -91 = 9.00.
* BUY 5 @ 9.00   -> PAYABLE -45.00, commission 1.00 -> liquid value 54.00.
* Wind-down sell limit = floor_to_tick(bid * (1 - 50 bps)).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness, load_policy  # noqa: E402

from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

SEC = 1_000_000_000
DAY = 86_400 * SEC
F = Fixed.parse
LIQUID = ("CASH", "RECEIVABLE", "PAYABLE", "FEES_ACCRUED")


# ---------------------------------------------------------------- helpers
def _harness(test: unittest.TestCase, **kw) -> Harness:
    h = Harness(**kw)
    test.addCleanup(h.close)
    return h


def _create(h: Harness, sid: str):
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=sid)
    assert r.accepted, r.body
    return r


def _buy_fill(h: Harness, sid: str, oid: str, qty: str, price: str, *, bid: str = "8.99", ask: str = "9.01",
              commission: str | None = None, delay_commission: bool = False) -> None:
    h.quote(bid, ask)
    r = h.intent(oid, "BUY", qty, price, sid)
    assert r.accepted, r.body
    h.pump()
    h.broker.fill(oid, F(qty), F(price), commission=F(commission) if commission else None,
                  delay_commission=delay_commission)
    h.pump()
    assert h.row("orders", "order_id", oid)["state"] == "FILLED"


def _orders(h: Harness, sid: str, purpose: str | None = None) -> list[dict]:
    rows = [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM orders WHERE session_id=? ORDER BY accepted_at_ns, order_id", (sid,))]
    return [o for o in rows if purpose is None or o["purpose"] == purpose]


def _wind_down_order(h: Harness, sid: str) -> dict:
    rows = [o for o in _orders(h, sid, "WIND_DOWN") if o["state"] not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED")]
    assert len(rows) == 1, rows
    return rows[0]


def _successors(h: Harness) -> list[tuple[str, str]]:
    return [(r["predecessor_id"], r["successor_id"]) for r in h.desk.store.query(
        "SELECT predecessor_id, successor_id FROM successors ORDER BY predecessor_id")]


def _liquid(h: Harness, owner: str) -> Fixed:
    total = Fixed(0)
    for code in LIQUID:
        total = total + h.balance(code, owner)
    return total


def _code_total(h: Harness, code: str) -> Fixed:
    total = Fixed(0)
    for r in h.desk.store.query("SELECT amount FROM balances WHERE account_key=? AND account_code=?", (ACCOUNT, code)):
        total = total + F(r["amount"])
    return total


def _owner_is_empty(h: Harness, owner: str) -> bool:
    return all(F(r["amount"]).is_zero() for r in h.desk.store.query(
        "SELECT amount FROM balances WHERE account_key=? AND owner=? AND account_code IN ('CASH','RECEIVABLE','PAYABLE','FEES_ACCRUED','INVENTORY')",
        (ACCOUNT, owner)))


def _events(h: Harness, event_type: str, session_id: str | None = None) -> list[dict]:
    sql, params = "SELECT payload FROM events WHERE type=?", [event_type]
    if session_id is not None:
        sql += " AND session_id=?"
        params.append(session_id)
    return [json.loads(r["payload"]) for r in h.desk.store.query(sql + " ORDER BY seq", tuple(params))]


def _exceptions(h: Harness, kind: str) -> list[dict]:
    return [e for e in h.open_exceptions() if e["kind"] == kind]


def _reach_target(h: Harness, sid: str, *, buy_commission: str | None = None, delay_buy_commission: bool = False) -> dict:
    """Buy 10 @ 9.00, then mark at bid 9.70 so marked equity 9.00 + 97.00 = 106.00 >= 105.00.

    Returns the wind-down order (10 @ floor(9.70 * 0.995) = 9.65) created by the target observation."""
    _buy_fill(h, sid, "B1", "10", "9.00", commission=buy_commission, delay_commission=delay_buy_commission)
    h.quote("9.70", "9.72")
    h.tick()
    assert h.session_state(sid) == "WINDING_DOWN", h.row("sessions", "session_id", sid)
    return _wind_down_order(h, sid)


def _fill_wind_down(h: Harness, sid: str, price: str, *, commission: str | None = None,
                    delay_commission: bool = False) -> dict:
    wd = _wind_down_order(h, sid)
    h.pump()
    h.broker.fill(wd["order_id"], F(wd["quantity"]), F(price), commission=F(commission) if commission else None,
                  delay_commission=delay_commission)
    h.pump()
    return h.row("orders", "order_id", wd["order_id"])


def _finish(h: Harness, sid: str) -> None:
    """Flat -> RECONCILING -> clean reconciliation after the last execution -> decision."""
    h.tick()
    h.reconcile()
    h.tick()


def _complete_successfully(h: Harness, sid: str = "A") -> str:
    """Drive ``sid`` through target observation, wind-down at 9.70 and completion; return the successor id.

    Realized: CASH 100 + PAYABLE -91 + RECEIVABLE (97.00 - 1.00) = 105.00 == target -> completion."""
    _create(h, sid)
    _reach_target(h, sid)
    _fill_wind_down(h, sid, "9.70")
    _finish(h, sid)
    assert h.session_state(sid) == "COMPLETED_UNSETTLED", h.row("sessions", "session_id", sid)
    succ = [s for p, s in _successors(h) if p == sid]
    assert len(succ) == 1, _successors(h)
    return succ[0]


def _settle_next_business_day(h: Harness) -> None:
    h.clock.advance(DAY)
    h.broker.advance_settlement()
    r = h.reconcile()
    assert r.accepted and r.body["status"] == "CLEAN", r.body


# ---------------------------------------------------------------- tests
class SuccessLifecycleTest(unittest.TestCase):
    def test_success_protects_exact_amount_and_creates_exactly_one_successor(self):
        """SES-04, SES-05, SES-07, SES-08, SES-12 (repeated completion), LED-04.

        Target observed on marks starts a wind-down (not completion); completion only after flat, final fees
        and a clean reconciliation after the last execution; every session balance moves to protected:A
        (receivables included); exactly one successor however often ticks/reconciliations/completion repeat."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        self.assertEqual(h.session_state("A"), "RUNNING")
        self.assertEqual(h.available("unallocated"), F("200.00"))
        self.assertEqual(h.balance("CASH", "session:A"), F("100.00"))

        wd = _reach_target(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["wind_down_reason"], "TARGET_OBSERVED")
        self.assertEqual(F(a["wind_down_evidence"]["equity"]), F("106.00"))
        self.assertIsNone(a["outcome"], "target observation on marks is not completion")
        self.assertEqual((wd["side"], F(wd["quantity"]), F(wd["limit_price"])), ("SELL", F("10"), F("9.65")))
        self.assertEqual(_successors(h), [])

        filled = _fill_wind_down(h, "A", "9.70")
        self.assertEqual(filled["state"], "FILLED")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "RECONCILING")
        self.assertIn("CLEAN_RECONCILIATION_AFTER_LAST_EXECUTION_REQUIRED", a["blockers"])
        self.assertEqual(_successors(h), [], "no successor before completion")
        h.tick()  # still no clean reconciliation after the last execution
        self.assertEqual(h.session_state("A"), "RECONCILING")

        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN")
        cash_before = _code_total(h, "CASH")
        complete_tick = h.desk.run_system("session_tick", {"account_key": ACCOUNT}, command_id="test.tick.completion")
        self.assertTrue(complete_tick.accepted)
        # SES-07: protection is an owner re-labelling inside the broker account, never an invented transfer
        self.assertEqual(_code_total(h, "CASH"), cash_before)
        protect = h.desk.store.query("SELECT kind FROM journal_entries WHERE memo=?", ("protect completed session A",))
        self.assertEqual([r["kind"] for r in protect], ["OWNER_TRANSFER"])

        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "COMPLETED_UNSETTLED")
        self.assertEqual(a["outcome"], "TARGET_REACHED")
        self.assertEqual(F(a["protected_value"]), F("105.00"))
        self.assertEqual(F(a["terminal_equity"]), F("105.00"))
        # every liquid balance (receivables included) moved to protected:A; nothing left in session:A
        self.assertEqual(h.balance("CASH", "protected:A"), F("100.00"))
        self.assertEqual(h.balance("RECEIVABLE", "protected:A"), F("96.00"))
        self.assertEqual(h.balance("PAYABLE", "protected:A"), F("-91.00"))
        self.assertEqual(h.balance("FEES_ACCRUED", "protected:A"), F("0"))
        self.assertEqual(_liquid(h, "protected:A"), F("105.00"))
        self.assertTrue(_owner_is_empty(h, "session:A"))
        stl = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM settlements WHERE status='EXPECTED'")]
        self.assertEqual({s["owner"] for s in stl}, {"protected:A"}, "unsettled items re-owned to protected:A")

        succ = _successors(h)
        self.assertEqual(len(succ), 1)
        succ_id = succ[0][1]
        srow = h.row("sessions", "session_id", succ_id)
        self.assertEqual(srow["predecessor_id"], "A")
        mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (srow["mandate_hash"],))[0]["body"])
        self.assertEqual(mandate["origin"], "SUCCESSOR_OF:A")
        # successor funded from unallocated only (200 - 100), protected untouched
        self.assertEqual(srow["state"], "RUNNING")
        self.assertEqual(h.balance("CASH", f"session:{succ_id}"), F("100.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("100.00"))
        self.assertEqual(h.balance("CASH", "protected:A"), F("100.00"))

        # repeated completion command returns the original result; repeated ticks/reconciliations add nothing
        again = h.desk.run_system("session_tick", {"account_key": ACCOUNT}, command_id="test.tick.completion")
        self.assertTrue(again.duplicate)
        self.assertEqual(again.body, complete_tick.body)
        for _ in range(3):
            h.tick()
        h.reconcile()
        h.tick()
        h.restart()
        h.connect()
        h.reconcile()
        h.tick()
        self.assertEqual(len(_successors(h)), 1)
        self.assertEqual(len(h.sessions()), 2)
        self.assertEqual(len(_events(h, "SessionCompleted")), 1)
        self.assertEqual(len(_events(h, "SuccessorRequested")), 1)
        # the uniqueness constraint backs "exactly one successor per predecessor"
        with self.assertRaises(sqlite3.IntegrityError):
            with h.desk.store.write() as tx:
                tx.run("INSERT INTO successors(predecessor_id, successor_id) VALUES (?, ?)", ("A", "ses-duplicate"))
        self.assertEqual(len(_successors(h)), 1)

        # settlement moves protected value into settled cash, never into unallocated
        _settle_next_business_day(h)
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "COMPLETED_SETTLED")
        self.assertEqual(h.balance("CASH", "protected:A"), F("105.00"))
        self.assertEqual(h.balance("RECEIVABLE", "protected:A"), F("0"))
        self.assertEqual(h.balance("PAYABLE", "protected:A"), F("0"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("100.00"))
        self.assertEqual(len(_successors(h)), 1)
        self.assertEqual(h.verify(), [])
        # the whole lifecycle (completion, successor, settlement) is reproducible from the event log
        out = tempfile.mkdtemp(dir=h.tmp.name)
        self.assertEqual(replay_and_compare(h.db_path, Path(out) / "replay.sqlite"), [])


class UnsuccessfulOutcomeTest(unittest.TestCase):
    def test_target_observed_then_sell_below_target_closes_below_target(self):
        """SES-04, SES-06, SES-12, LED-04: marks reached the target but the realized sale did not.

        Wind-down sell fills at its limit 9.65: realized 100 - 91 + (96.50 - 1.00) = 104.50 < 105.00, so the
        session ends CLOSED/BELOW_TARGET under the default CLOSE policy, never re-enters, capital (cash and
        receivables) goes back to unallocated and no successor is created."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        wd = _reach_target(h, "A")
        self.assertEqual(F(wd["limit_price"]), F("9.65"))
        _fill_wind_down(h, "A", "9.65")
        _finish(h, "A")

        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "CLOSED")
        self.assertEqual(a["outcome"], "BELOW_TARGET")
        self.assertEqual(F(a["terminal_equity"]), F("104.50"))
        self.assertIsNone(a["protected_value"])
        self.assertEqual(_successors(h), [])
        self.assertEqual(len(h.sessions()), 1)
        self.assertTrue(_owner_is_empty(h, "session:A"))
        self.assertTrue(_owner_is_empty(h, "protected:A"))
        # unallocated: 200 + returned cash 100; receivable 95.50 and payable -91.00 re-owned
        self.assertEqual(h.balance("CASH", "unallocated"), F("300.00"))
        self.assertEqual(h.balance("RECEIVABLE", "unallocated"), F("95.50"))
        self.assertEqual(h.balance("PAYABLE", "unallocated"), F("-91.00"))
        self.assertEqual(h.available("unallocated"), F("209.00"), "receivables are never available")
        stl = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM settlements WHERE status='EXPECTED'")]
        self.assertEqual({s["owner"] for s in stl}, {"unallocated"})

        # it does not re-enter or change on later ticks
        h.quote("9.90", "9.92")
        h.tick()
        h.tick()
        self.assertEqual(h.session_state("A"), "CLOSED")
        self.assertEqual(_orders(h, "A", "WIND_DOWN")[-1]["state"], "FILLED")
        self.assertEqual(len(_orders(h, "A")), 2)

        _settle_next_business_day(h)
        self.assertEqual(h.balance("CASH", "unallocated"), F("304.50"))
        self.assertEqual(h.available("unallocated"), F("304.50"))
        self.assertEqual(h.verify(), [])

    def test_below_target_resume_policy_is_bounded_by_max_resumes(self):
        """SES-06, ADR-0008 (RESUME is permitted only with a positive max_resumes written into the mandate):
        with RESUME and max_resumes=1 the first below-target outcome returns the session to RUNNING with its
        capital still in session:A and no successor; the second below-target outcome closes BELOW_TARGET."""
        policy = load_policy(session_template__below_target_after_target_observed="RESUME", session_template__max_resumes=1)
        h = _harness(self, policy=policy)
        h.bootstrap()
        _create(h, "A")
        _reach_target(h, "A")
        _fill_wind_down(h, "A", "9.65")  # realized 104.50 < 105.00
        _finish(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["resumes_used"], a["outcome"], a["wind_down_reason"]), ("RUNNING", 1, None, None))
        self.assertEqual(_liquid(h, "session:A"), F("104.50"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("200.00"))
        self.assertEqual(_successors(h), [])

        _settle_next_business_day(h)
        self.assertEqual(h.available("session:A"), F("104.50"))
        _buy_fill(h, "A", "B2", "10", "9.00")  # liquid 104.50 - 91.00 = 13.50
        h.quote("9.20", "9.22")  # marks 13.50 + 92.00 = 105.50 >= 105.00
        h.tick()
        self.assertEqual(h.session_state("A"), "WINDING_DOWN")
        self.assertEqual(F(_wind_down_order(h, "A")["limit_price"]), F("9.15"))
        _fill_wind_down(h, "A", "9.15")  # realized 13.50 + 91.50 - 1.00 = 104.00
        _finish(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"], a["resumes_used"]), ("CLOSED", "BELOW_TARGET", 1))
        self.assertEqual(F(a["terminal_equity"]), F("104.00"))
        self.assertEqual(_successors(h), [])
        self.assertTrue(_owner_is_empty(h, "session:A"))
        self.assertEqual(_liquid(h, "unallocated"), F("304.00"))
        self.assertEqual(h.verify(), [])

    def test_owner_end_winds_down_and_closes_owner_ended(self):
        """SES-12 (unsuccessful sessions), LED-04: owner end -> WINDING_DOWN -> automatic reduction ->
        CLOSED/OWNER_ENDED, capital returned to unallocated, no successor."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.99", "9.01")
        r = h.owner_cmd("end_session", session_id="A")
        self.assertTrue(r.accepted, r.body)
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["wind_down_reason"]), ("WINDING_DOWN", "OWNER_ENDED"))
        wd = _wind_down_order(h, "A")
        # floor(8.99 * 0.995 = 8.94505) to the 0.01 tick
        self.assertEqual((wd["side"], F(wd["quantity"]), F(wd["limit_price"])), ("SELL", F("5"), F("8.94")))

        _fill_wind_down(h, "A", "8.94")
        _finish(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "CLOSED")
        self.assertEqual(a["outcome"], "OWNER_ENDED")
        # 54.00 + 44.70 - 1.00
        self.assertEqual(F(a["terminal_equity"]), F("97.70"))
        self.assertEqual(_successors(h), [])
        self.assertTrue(_owner_is_empty(h, "session:A"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("300.00"))
        self.assertEqual(h.balance("RECEIVABLE", "unallocated"), F("43.70"))
        self.assertEqual(h.balance("PAYABLE", "unallocated"), F("-46.00"))
        self.assertEqual(_liquid(h, "unallocated"), F("297.70"))
        self.assertEqual(h.verify(), [])

    def test_owner_end_of_flat_session_closes_immediately(self):
        """SES-12, LED-04: a flat session with no orders ends CLOSED/OWNER_ENDED in the same command and
        its whole seed returns to unallocated."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        self.assertEqual(h.balance("CASH", "unallocated"), F("200.00"))
        r = h.owner_cmd("end_session", session_id="A")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body["state"], "CLOSED")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["outcome"], F(a["terminal_equity"])), ("OWNER_ENDED", F("100.00")))
        self.assertEqual(h.balance("CASH", "unallocated"), F("300.00"))
        self.assertEqual(h.balance("CASH", "session:A"), F("0"))
        self.assertEqual(_successors(h), [])
        self.assertEqual(h.verify(), [])


class SuccessorFundingTest(unittest.TestCase):
    def test_successor_waits_and_is_never_funded_from_protected_or_unsettled_proceeds(self):
        """SES-09, SES-10, LED-03, LED-04: only the seed (+ operational buffer) is authorized.

        After completion the successor needs 100.00 but unallocated settled cash minus the 5.00 buffer is 0.
        Protected proceeds (100.00 cash + 96.00 receivable, then 105.00 settled cash) must never fund it,
        before or after settlement. Monitoring continues while it waits; it cannot trade."""
        h = _harness(self)
        h.bootstrap(authorize="105.00")
        self.assertEqual(h.available("unallocated"), F("105.00"))
        succ = _complete_successfully(h, "A")

        s = h.row("sessions", "session_id", succ)
        self.assertEqual(s["state"], "WAITING_FUNDS")
        self.assertEqual(s["funding"]["status"], "WAITING")
        self.assertEqual(s["funding"]["reason"], "INSUFFICIENT_UNALLOCATED_SETTLED_CASH")
        self.assertEqual(F(s["funding"]["shortfall"]), F("100.00"))
        self.assertEqual(_liquid(h, "protected:A"), F("105.00"))
        self.assertEqual(h.balance("RECEIVABLE", "protected:A"), F("96.00"))
        self.assertTrue(_owner_is_empty(h, f"session:{succ}"))
        self.assertEqual(h.available("unallocated"), F("5.00"))

        # no borrowing: the waiting request cannot place an order
        h.quote("9.69", "9.71")
        r = h.intent("W1", "BUY", "1", "9.70", succ)
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "SESSION_STATE")
        # monitoring and research continue while waiting
        h.tick()
        self.assertTrue(h.cmd("submit_research_proposal", h.agent, proposal={
            "schema_version": 1, "job_id": "job-1", "hypothesis": "fixture hypothesis", "evidence": [],
            "horizon_seconds": 3600, "assumptions": [], "abstain": True, "reason": "fixture"}).accepted)

        # settlement turns the protected receivable into settled protected cash; still not eligible
        _settle_next_business_day(h)
        h.tick()
        h.tick()
        self.assertEqual(h.session_state("A"), "COMPLETED_SETTLED")
        self.assertEqual(h.balance("CASH", "protected:A"), F("105.00"))
        self.assertEqual(h.available("protected:A"), F("105.00"))
        s = h.row("sessions", "session_id", succ)
        self.assertEqual(s["state"], "WAITING_FUNDS")
        self.assertEqual(F(s["funding"]["shortfall"]), F("100.00"))
        self.assertTrue(_owner_is_empty(h, f"session:{succ}"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("5.00"))
        self.assertEqual(len(_successors(h)), 1)
        self.assertEqual(h.verify(), [])

    def test_request_not_funded_from_unsettled_receivables_until_settlement_confirms_them(self):
        """SES-09, SES-10, LED-03, ADR-0008 (funding retried after every settlement).

        A BELOW_TARGET close re-owns 95.50 of unsettled sale proceeds to unallocated, so unallocated holds
        CASH 105.00, RECEIVABLE 95.50, PAYABLE -91.00: available 14.00 - 5.00 buffer = 9.00. A new request
        waits (shortfall 91.00) and is funded automatically by the reconciliation that confirms settlement
        (settled cash 109.50 - 5.00 >= 100.00)."""
        h = _harness(self)
        h.bootstrap(authorize="105.00")
        _create(h, "A")
        _reach_target(h, "A")
        _fill_wind_down(h, "A", "9.65")
        _finish(h, "A")
        self.assertEqual(h.row("sessions", "session_id", "A")["outcome"], "BELOW_TARGET")
        self.assertEqual(h.balance("RECEIVABLE", "unallocated"), F("95.50"))
        self.assertEqual(h.available("unallocated"), F("14.00"))

        _create(h, "B")
        b = h.row("sessions", "session_id", "B")
        self.assertEqual((b["state"], b["funding"]["reason"]), ("WAITING_FUNDS", "INSUFFICIENT_UNALLOCATED_SETTLED_CASH"))
        self.assertEqual(F(b["funding"]["shortfall"]), F("91.00"))
        h.tick()
        self.assertEqual(h.session_state("B"), "WAITING_FUNDS")
        self.assertTrue(_owner_is_empty(h, "session:B"))

        _settle_next_business_day(h)
        b = h.row("sessions", "session_id", "B")
        self.assertEqual(b["funding"], {"status": "FUNDED", "amount": "100.00"})
        self.assertEqual(b["state"], "RUNNING")
        self.assertEqual(h.balance("CASH", "session:B"), F("100.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("9.50"))
        self.assertEqual(h.balance("RECEIVABLE", "unallocated"), F("0"))
        self.assertEqual(h.verify(), [])

    def test_successor_funds_automatically_after_capital_is_authorized(self):
        """SES-09, SES-10, LED-04: a waiting successor is funded (and auto-started) by the funding retry that
        follows authorize_capital, from unallocated only; protected proceeds are unchanged."""
        h = _harness(self)
        h.bootstrap(authorize="105.00")
        succ = _complete_successfully(h, "A")
        _settle_next_business_day(h)
        self.assertEqual(h.session_state(succ), "WAITING_FUNDS")

        r = h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="100.00")
        self.assertTrue(r.accepted, r.body)
        s = h.row("sessions", "session_id", succ)
        self.assertEqual(s["funding"], {"status": "FUNDED", "amount": "100.00"})
        self.assertEqual(s["state"], "RUNNING")
        self.assertEqual(h.balance("CASH", f"session:{succ}"), F("100.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("5.00"))
        self.assertEqual(h.balance("CASH", "protected:A"), F("105.00"))
        self.assertEqual(h.balance("CASH", "outside"), F("95.00"))
        self.assertEqual(len(_events(h, "SessionFunded", succ)), 1)
        self.assertEqual(h.verify(), [])


class SessionRiskTest(unittest.TestCase):
    def test_session_drawdown_suspends_and_cancels_open_buys(self):
        """RSK-02, SES-12: marked equity at or below high_water * (1 - 200 bps) suspends the session
        (entries blocked, position held) and cancels its open buys; the buy hold is released only on the
        broker's cancel confirmation (ORD-04)."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.99", "9.01")
        r = h.intent("B2", "BUY", "2", "8.90", "A")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B2")["state"], "ACKNOWLEDGED")
        # hold = 2 * 8.90 + fee envelope (1.00 + 20%) = 19.00
        self.assertEqual(h.available("session:A"), F("35.00"))

        h.quote("8.81", "8.83")  # equity 54.00 + 44.05 = 98.05 > floor 98.00
        h.tick()
        self.assertEqual(h.session_state("A"), "RUNNING")
        h.quote("8.80", "8.82")  # equity 54.00 + 44.00 = 98.00 <= floor 98.00
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "SUSPENDED")
        self.assertEqual(a["suspend_reason"], "SESSION_DRAWDOWN")
        ev = _events(h, "SessionSuspended", "A")[-1]["info"]["evidence"]
        self.assertEqual((F(ev["equity"]), F(ev["drawdown_floor"])), (F("98.00"), F("98.00")))
        b2 = h.row("orders", "order_id", "B2")
        self.assertEqual((b2["state"], b2["cancel_state"]), ("CANCEL_PENDING", "REQUESTED"))
        self.assertEqual(h.available("session:A"), F("35.00"), "cancel request does not release the hold")
        self.assertEqual(h.control()["stop_latched"], False, "a session trigger is not an account stop")

        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B2")["state"], "CANCELED")
        self.assertEqual(h.available("session:A"), F("54.00"))
        # position held, entries blocked, reductions allowed
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) n FROM lots WHERE owner='session:A' AND open_qty='5'")[0]["n"], 1)
        h.quote("8.80", "8.82")
        r = h.intent("B3", "BUY", "1", "8.80", "A")
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_code, "SESSION_STATE")
        self.assertTrue(h.intent("S1", "SELL", "1", "8.80", "A").accepted)
        # SYSTEM cannot resume a suspended session
        self.assertEqual(h.cmd("resume_session", h.desk.system(), session_id="A").rejected_code, "UNAUTHORIZED")
        self.assertEqual(h.verify(), [])

    def test_target_observed_while_suspended_starts_wind_down(self):
        """SES-04, ADR-0008 transition table: RUNNING/SUSPENDED -> WINDING_DOWN on "target observed (SYSTEM)".

        A suspended session holds its position; when a fresh mark shows equity 9.00 + 97.00 = 106.00 >= 105.00
        the target observation must start the controlled wind-down exactly as it does for RUNNING."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "10", "9.00")
        self.assertTrue(h.owner_cmd("suspend_session", session_id="A").accepted)
        h.quote("9.70", "9.72")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(F(a["high_water"]), F("106.00"), "the mark was observed")
        self.assertEqual((a["state"], a["wind_down_reason"]), ("WINDING_DOWN", "TARGET_OBSERVED"))
        wd = _wind_down_order(h, "A")
        self.assertEqual((F(wd["quantity"]), F(wd["limit_price"])), (F("10"), F("9.65")))
        self.assertEqual(h.verify(), [])

    def test_session_max_loss_suspends(self):
        """RSK-02, SES-12: seed - marked equity >= session_max_loss (5.00) suspends the session with reason
        SESSION_MAX_LOSS (drawdown trigger widened to 1000 bps and daily trigger to 10.00 to isolate it)."""
        policy = load_policy(session_template__session_drawdown_trigger_bps=1000, account_limits__daily_loss_trigger="10.00")
        h = _harness(self, policy=policy)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.21", "8.23")  # equity 95.05: loss 4.95
        h.tick()
        self.assertEqual(h.session_state("A"), "RUNNING")
        h.quote("8.20", "8.22")  # equity 95.00: loss 5.00
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["suspend_reason"]), ("SUSPENDED", "SESSION_MAX_LOSS"))
        ev = _events(h, "SessionSuspended", "A")[-1]["info"]["evidence"]
        self.assertEqual((F(ev["equity"]), F(ev["session_max_loss"])), (F("95.00"), F("5.00")))
        self.assertFalse(h.control()["stop_latched"])
        self.assertIsNone(h.row("risk_state", "account_key", ACCOUNT)["breach"])
        self.assertEqual(h.verify(), [])

    def test_mandate_expiry_starts_wind_down_and_closes_expired(self):
        """SES-12, ADR-0008 (mandate expiry -> WINDING_DOWN -> CLOSED/EXPIRED), LED-04."""
        h = _harness(self, policy=load_policy(session_template__expires_after_s=3600))
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.clock.advance(3599 * SEC)
        h.quote("8.99", "9.01")
        h.tick()
        self.assertEqual(h.session_state("A"), "RUNNING")
        self.assertEqual(_orders(h, "A", "WIND_DOWN"), [])

        h.clock.advance(1 * SEC)
        h.quote("8.99", "9.01")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["wind_down_reason"]), ("WINDING_DOWN", "EXPIRED"))
        wd = _wind_down_order(h, "A")
        self.assertEqual((wd["side"], F(wd["quantity"]), F(wd["limit_price"])), ("SELL", F("5"), F("8.94")))

        _fill_wind_down(h, "A", "8.94")
        _finish(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("CLOSED", "EXPIRED"))
        self.assertEqual(F(a["terminal_equity"]), F("97.70"))
        self.assertEqual(_successors(h), [])
        self.assertTrue(_owner_is_empty(h, "session:A"))
        self.assertEqual(_liquid(h, "unallocated"), F("297.70"))
        self.assertEqual(h.verify(), [])


class ActiveLimitAndCancelTest(unittest.TestCase):
    def test_one_active_session_limit_keeps_second_ready_session_waiting(self):
        """SES-10, ADR-0008 (one active trading session per account): a funded second session stays READY
        while the first is RUNNING or SUSPENDED, and auto-starts when the first closes."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _create(h, "B")
        b = h.row("sessions", "session_id", "B")
        self.assertEqual(b["state"], "READY")
        self.assertEqual(b["blockers"], ["ACTIVE_SESSION_LIMIT:A"])
        self.assertEqual(h.balance("CASH", "session:B"), F("100.00"))
        r = h.owner_cmd("start_session", session_id="B")
        self.assertEqual(r.rejected_code, "START_BLOCKED")
        self.assertIn("ACTIVE_SESSION_LIMIT:A", r.body["details"]["blockers"])

        self.assertTrue(h.owner_cmd("suspend_session", session_id="A").accepted)
        h.tick()
        self.assertEqual(h.session_state("B"), "READY", "a SUSPENDED session still counts as active")

        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        self.assertEqual(h.session_state("A"), "CLOSED")
        self.assertEqual(h.session_state("B"), "RUNNING")
        self.assertEqual(h.balance("CASH", "unallocated"), F("200.00"))
        self.assertEqual(h.verify(), [])

    def test_cancel_session_returns_allocation_and_retries_waiting_funding(self):
        """SES-12, LED-04, ADR-0008 (REQUESTED/WAITING_FUNDS/READY -> CANCELLED returns the allocated seed to
        unallocated; funding is retried after a cancel)."""
        h = _harness(self)
        h.bootstrap(authorize="210.00")
        _create(h, "A")  # RUNNING, unallocated 110
        _create(h, "B")  # READY (active limit), unallocated 10
        _create(h, "C")  # WAITING_FUNDS: 10 - 5 buffer < 100
        self.assertEqual([h.session_state(x) for x in "ABC"], ["RUNNING", "READY", "WAITING_FUNDS"])
        self.assertEqual(F(h.row("sessions", "session_id", "C")["funding"]["shortfall"]), F("95.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("10.00"))

        self.assertEqual(h.owner_cmd("cancel_session", session_id="A").rejected_code, "INVALID_STATE")
        r = h.owner_cmd("cancel_session", session_id="B")
        self.assertTrue(r.accepted, r.body)
        b = h.row("sessions", "session_id", "B")
        self.assertEqual((b["state"], b["outcome"]), ("CANCELLED", "CANCELLED"))
        self.assertEqual(h.balance("CASH", "session:B"), F("0"))
        # B's 100.00 came back and immediately funded C
        self.assertEqual(h.session_state("C"), "READY")
        self.assertEqual(h.balance("CASH", "session:C"), F("100.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("10.00"))

        self.assertTrue(h.owner_cmd("cancel_session", session_id="C").accepted)
        self.assertEqual(h.balance("CASH", "unallocated"), F("110.00"))
        _create(h, "D")  # READY, unallocated 10
        _create(h, "E")  # WAITING_FUNDS, nothing allocated
        self.assertEqual(h.session_state("E"), "WAITING_FUNDS")
        self.assertTrue(h.owner_cmd("cancel_session", session_id="E").accepted)
        self.assertEqual(h.session_state("E"), "CANCELLED")
        self.assertEqual(h.balance("CASH", "unallocated"), F("10.00"))
        self.assertEqual(h.balance("CASH", "session:E"), F("0"))
        self.assertEqual(h.owner_cmd("cancel_session", session_id="E").rejected_code, "INVALID_STATE")
        self.assertEqual(h.verify(), [])


class PostCompletionTest(unittest.TestCase):
    def test_late_commission_after_completion_posts_to_protected_and_opens_exception(self):
        """SES-12, LED-07, ADR-0008 (late fees after completion): the owner accepts the sell's fee estimate
        (0.97) so completion proceeds at 105.03; the broker's later 1.50 commission posts against
        protected:A, drops protected value to 104.50 (< target) and the completion stands with a
        non-blocking POST_COMPLETION_ADJUSTMENT exception."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _reach_target(h, "A")
        sell = _fill_wind_down(h, "A", "9.70", commission="1.50", delay_commission=True)
        exec_key = [json.loads(r["row"]) for r in h.desk.store.query(
            "SELECT row FROM executions WHERE order_id=?", (sell["order_id"],))][0]["exec_key"]
        _finish(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "RECONCILING")
        self.assertIn("FEES_NOT_FINAL:1", a["blockers"])

        self.assertTrue(h.owner_cmd("accept_fee_estimate", exec_key=exec_key).accepted)
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "COMPLETED_UNSETTLED")
        self.assertEqual(F(a["protected_value"]), F("105.03"))
        self.assertEqual(len(_successors(h)), 1)

        self.assertEqual(h.broker.release_commissions(), 1)
        h.pump()
        ex = h.row("executions", "exec_key", exec_key)
        self.assertEqual((ex["fee_status"], F(ex["fee_final"])), ("FINAL", F("1.50")))
        self.assertEqual(h.balance("COMMISSION", "protected:A"), F("1.50"))
        self.assertEqual(h.balance("RECEIVABLE", "protected:A"), F("95.50"))
        self.assertEqual(h.balance("FEES_ACCRUED", "protected:A"), F("0"))
        self.assertEqual(_liquid(h, "protected:A"), F("104.50"))
        self.assertTrue(_owner_is_empty(h, "session:A"))
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("COMPLETED_UNSETTLED", "TARGET_REACHED"))
        self.assertEqual(F(a["protected_value"]), F("105.03"), "completion stands on the evidence it was decided on")
        cases = _exceptions(h, "POST_COMPLETION_ADJUSTMENT")
        self.assertEqual(len(cases), 1)
        self.assertEqual((cases[0]["session_id"], cases[0]["blocking"], cases[0]["owner_hint"]), ("A", False, "protected:A"))
        self.assertEqual(len(_successors(h)), 1)

        _settle_next_business_day(h)
        h.tick()
        self.assertEqual(h.session_state("A"), "COMPLETED_SETTLED")
        self.assertEqual(h.balance("CASH", "protected:A"), F("104.50"))
        self.assertEqual(len(_successors(h)), 1)
        self.assertEqual(h.verify(), [])

    def test_late_fee_making_protected_cash_negative_opens_blocking_exception(self):
        """SES-12, LED-07, ADR-0008: a late BUY commission (12.00 vs accepted estimate 0.90) after completion
        posts against protected:A's payable, making its available cash 100 - 102 = -2.00; a blocking
        POST_COMPLETION_ADJUSTMENT is opened and the ledger invariants still hold (exception-covered)."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _reach_target(h, "A", buy_commission="12.00", delay_buy_commission=True)
        _fill_wind_down(h, "A", "9.70")
        buy_exec = [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM executions WHERE order_id='B1'")][0]
        self.assertEqual((buy_exec["fee_status"], F(buy_exec["fee_estimate"])), ("ESTIMATED", F("0.90")))
        self.assertTrue(h.owner_cmd("accept_fee_estimate", exec_key=buy_exec["exec_key"]).accepted)
        _finish(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "COMPLETED_UNSETTLED")
        # 100 - 90 - 0.90 (accepted estimate) + 97 - 1.00
        self.assertEqual(F(a["protected_value"]), F("105.10"))

        h.broker.release_commissions()
        h.pump()
        self.assertEqual(h.balance("PAYABLE", "protected:A"), F("-102.00"))
        cases = _exceptions(h, "POST_COMPLETION_ADJUSTMENT")
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0]["blocking"])
        self.assertEqual(cases[0]["owner_hint"], "protected:A")
        # ADR-0006: the 2.00 shortfall moves to exception:<case>, leaving protected:A at zero, not negative.
        (over,) = _exceptions(h, "OVERCOMMITTED")
        self.assertTrue(over["blocking"])
        self.assertEqual(over["owner_hint"], "protected:A")
        self.assertEqual(h.available("protected:A"), F("0"))
        self.assertEqual(h.available(f"exception:{over['case_id']}"), F("-2.00"))
        self.assertEqual(h.session_state("A"), "COMPLETED_UNSETTLED")
        self.assertEqual(h.verify(), [])
        # The owner covers it from unallocated cash; the case resolves and nothing stays negative.
        r = h.owner_cmd("resolve_exception", case_id=over["case_id"], action="cover_shortfall", from_owner="unallocated",
                        note="late buy commission above the accepted estimate")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.available(f"exception:{over['case_id']}"), F("0"))
        self.assertEqual(_exceptions(h, "OVERCOMMITTED"), [])
        self.assertEqual(h.verify(), [])


class SettlementAndRecoveryTest(unittest.TestCase):
    def test_settlement_late_detected_after_grace_when_broker_never_settles(self):
        """SES-12, REC-04, ADR-0008 (failed settlement): protected items expected Wed 2026-10-21 are not late
        on Fri 10-23 (2 business-day grace) but are on Mon 10-26; the exceptions are non-blocking and the
        session stays COMPLETED_UNSETTLED."""
        h = _harness(self)
        h.bootstrap(authorize="105.00")
        succ = _complete_successfully(h, "A")
        expected = {json.loads(r["row"])["expected_date"] for r in h.desk.store.query("SELECT row FROM settlements WHERE status='EXPECTED'")}
        self.assertEqual(expected, {"2026-10-21"})

        h.clock.advance(3 * DAY)  # Fri 2026-10-23: within grace
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        self.assertEqual(_exceptions(h, "SETTLEMENT_LATE"), [])

        h.clock.advance(3 * DAY)  # Mon 2026-10-26: past expected + 2 business days
        r = h.reconcile()
        self.assertEqual(r.body["status"], "CLEAN", "a late settlement is not a reconciliation difference")
        h.tick()
        late = _exceptions(h, "SETTLEMENT_LATE")
        self.assertEqual(len(late), 2)
        self.assertEqual({c["owner_hint"] for c in late}, {"protected:A"})
        self.assertTrue(all(not c["blocking"] for c in late))
        self.assertEqual({c["detail"]["expected_date"] for c in late}, {"2026-10-21"})
        self.assertEqual(h.session_state("A"), "COMPLETED_UNSETTLED")
        self.assertEqual(h.balance("RECEIVABLE", "protected:A"), F("96.00"))
        self.assertEqual(h.session_state(succ), "WAITING_FUNDS")
        h.reconcile()
        self.assertEqual(len(_exceptions(h, "SETTLEMENT_LATE")), 2, "one case per settlement, not per run")
        self.assertEqual(h.verify(), [])

    def test_wind_down_creates_no_sells_while_stop_latched_and_resumes_after_green_start(self):
        """RSK-03, SES-12, ADR-0010 (on_stop_liquidation = NONE): under a latched stop the wind-down creates
        no new sell; after a green start it proceeds and the session closes OWNER_ENDED."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.99", "9.01")
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "WINDING_DOWN")
        self.assertTrue(any(b.startswith("FIX-EQ-1:STOP_LATCHED") for b in a["blockers"]), a["blockers"])
        self.assertEqual(_orders(h, "A", "WIND_DOWN"), [])
        for _ in range(3):
            h.quote("8.99", "9.01")
            h.tick()
        self.assertEqual(_orders(h, "A", "WIND_DOWN"), [])
        self.assertEqual(h.session_state("A"), "WINDING_DOWN")
        self.assertEqual(h.row("sessions", "session_id", "A")["wind_down_attempts"], {})

        h.reconcile()
        h.quote("8.99", "9.01")
        r = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(r.accepted, r.body)
        h.tick()
        wd = _wind_down_order(h, "A")
        self.assertEqual((wd["side"], F(wd["quantity"]), F(wd["limit_price"])), ("SELL", F("5"), F("8.94")))
        _fill_wind_down(h, "A", "8.94")
        _finish(h, "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("CLOSED", "OWNER_ENDED"))
        self.assertEqual(F(a["terminal_equity"]), F("97.70"))
        self.assertEqual(h.verify(), [])

    def test_interrupted_wind_down_survives_restart_and_continues_after_recovery_gate(self):
        """SES-12, RSK-05, ADR-0008 (interrupted wind-down): after a restart the session stays WINDING_DOWN,
        no reduction order is created before a clean reconciliation in the new process, and wind-down
        continues once the recovery gate clears."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.clock.advance(10 * SEC)  # quote now stale: wind-down cannot price a sell yet
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "WINDING_DOWN")
        self.assertIn("FIX-EQ-1:NO_FRESH_BID", a["blockers"])

        h.restart()
        self.assertEqual(h.session_state("A"), "WINDING_DOWN")
        h.quote("8.99", "9.01")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "WINDING_DOWN")
        self.assertEqual(_orders(h, "A", "WIND_DOWN"), [])
        self.assertIn("FIX-EQ-1:RECOVERY_GATE", a["blockers"])

        h.connect()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.quote("8.99", "9.01")
        h.tick()
        wd = _wind_down_order(h, "A")
        self.assertEqual((F(wd["quantity"]), F(wd["limit_price"])), (F("5"), F("8.94")))
        _fill_wind_down(h, "A", "8.94")
        _finish(h, "A")
        self.assertEqual(h.row("sessions", "session_id", "A")["outcome"], "OWNER_ENDED")
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
