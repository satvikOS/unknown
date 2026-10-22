"""Session state machine: fixes for adversarial-review findings 8 (sell part), 9, 10, 31, 32, 33, 34, 45,
46 and 48 (ADR-0008 sessions, ADR-0010 stop/start/recovery).

Every number below is a TEST FIXTURE (seed 100.00, target 105.00, fixture prices and commissions), not
market data, a forecast or a funding authorization.

Fixture arithmetic used here (default policy; fee estimate = min(max(0.005/share, 1.00), 1% of notional);
reservation envelope = uncapped estimate + 20%; simulated broker commission max(1.00, 0.005/share)):

* BUY 10 @ 9.00 reserves 90.00 + 1.20; its estimate is 0.90.
* BUY 1 @ 7.00 reserves 7.00 + 1.20 = 8.20.
* BUY 5 @ 9.00 with a 1.00 commission leaves session liquid value 100 - 45 - 1 = 54.00.
* Wind-down sell limit = floor_to_tick(bid x (1 - 50 bps)); the pretrade sell floor is
  ceil(mid x (1 - 100 bps)).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness, load_policy  # noqa: E402

from unknown.broker.simulated import SubmitBehavior  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402

SEC = 1_000_000_000
DAY = 86_400 * SEC
F = Fixed.parse
TERMINAL_ORDER_STATES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED")


# ---------------------------------------------------------------- helpers
def _harness(test: unittest.TestCase, **kw) -> Harness:
    h = Harness(**kw)
    test.addCleanup(h.close)
    return h


def _create(h: Harness, sid: str) -> dict:
    r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id=sid)
    assert r.accepted, r.body
    return r.body


def _buy_fill(h: Harness, sid: str, oid: str, qty: str, price: str, *, commission: str | None = None) -> None:
    h.quote("8.99", "9.01")
    r = h.intent(oid, "BUY", qty, price, sid)
    assert r.accepted, r.body
    h.pump()
    h.broker.fill(oid, F(qty), F(price), commission=F(commission) if commission else None)
    h.pump()
    assert h.row("orders", "order_id", oid)["state"] == "FILLED"


def _orders(h: Harness, sid: str, purpose: str | None = None) -> list[dict]:
    rows = [json.loads(r["row"]) for r in h.desk.store.query(
        "SELECT row FROM orders WHERE session_id=? ORDER BY accepted_at_ns, order_id", (sid,))]
    return [o for o in rows if purpose is None or o["purpose"] == purpose]


def _working_wind_down(h: Harness, sid: str) -> list[dict]:
    return [o for o in _orders(h, sid, "WIND_DOWN") if o["state"] not in TERMINAL_ORDER_STATES]


def _events(h: Harness, event_type: str, session_id: str | None = None) -> list[dict]:
    sql, params = "SELECT payload FROM events WHERE type=?", [event_type]
    if session_id is not None:
        sql += " AND session_id=?"
        params.append(session_id)
    return [json.loads(r["payload"]) for r in h.desk.store.query(sql + " ORDER BY seq", tuple(params))]


def _cases(h: Harness, kind: str) -> list[dict]:
    return [e for e in h.open_exceptions() if e["kind"] == kind]


def _executions(h: Harness, order_id: str) -> list[dict]:
    return [json.loads(r["row"]) for r in h.desk.store.query("SELECT row FROM executions WHERE order_id=?", (order_id,))]


def _hold(h: Harness, order_id: str) -> tuple[Fixed, str]:
    (r,) = h.desk.store.query("SELECT amount, status FROM holds WHERE order_id=?", (order_id,))
    return F(r["amount"]), r["status"]


def _overcommitted_review(h: Harness, *, b2_unknown: bool = False) -> dict:
    """Session A buys 10 @ 9.00 (B1) while B2 (1 @ 7.00, hold 8.20) is working. The broker charges 2.50
    against B1's 0.90 estimate: available = 100.00 - 92.50 - 8.20 = -0.70, so the 0.70 shortfall moves to
    exception:<case> (OVERCOMMITTED, attributed to A) and the next tick moves A to FAILED_REVIEW.

    With ``b2_unknown`` B2's submission times out before the broker gets it: B2 is UNKNOWN with a blocking
    UNKNOWN_ORDER case attributed to A, which only reconciliation can resolve."""
    _create(h, "A")
    h.quote("8.99", "9.01")
    r = h.intent("B1", "BUY", "10", "9.00", "A")
    assert r.accepted, r.body
    r = h.intent("B2", "BUY", "1", "7.00", "A")
    assert r.accepted, r.body
    if b2_unknown:
        h.broker.script_submit(SubmitBehavior.ACCEPT, SubmitBehavior.TIMEOUT_BEFORE_ACCEPT)
    h.pump()
    h.broker.fill("B1", F("10"), F("9.00"), commission=F("2.50"))
    h.pump()
    (case,) = _cases(h, "OVERCOMMITTED")
    assert case["session_id"] == "A", case
    assert F(case["detail"]["shortfall"]) == F("0.70"), case
    assert h.available("session:A") == F("0"), h.available("session:A")
    h.quote("9.00", "9.02")  # equity 8.20 + 90.00 = 98.20 > drawdown floor 98.00
    h.tick()
    assert h.session_state("A") == "FAILED_REVIEW", h.row("sessions", "session_id", "A")
    return case


def _stale_owner(h: Harness, age_s: int) -> Principal:
    return Principal("owner", Role.OWNER, authenticated_at_ns=h.clock.now_ns() - age_s * SEC)


# ---------------------------------------------------------------- FAILED_REVIEW
class FailedReviewTest(unittest.TestCase):
    def test_risk_reducing_sell_is_allowed_while_failed_review(self):
        """Finding 8 (sell part), ADR-0008/ADR-0010: a review freeze blocks new exposure, never reduction.

        A is in FAILED_REVIEW on an OVERCOMMITTED case. Before the fix pretrade refused every order on a
        FAILED_REVIEW session (SESSION_STATE), so the owner could not sell the session's 10 shares to free
        the cash the case needs. Now the sell is accepted, transmitted and booked; buys stay refused."""
        h = _harness(self)
        h.bootstrap()
        case = _overcommitted_review(h)
        h.quote("8.99", "9.01")
        r = h.intent("S1", "SELL", "10", "8.99", "A")
        self.assertTrue(r.accepted, r.body)
        h.pump()
        h.broker.fill("S1", F("10"), F("8.99"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "FILLED")
        self.assertEqual(h.desk.store.query("SELECT COUNT(*) n FROM lots WHERE owner='session:A' AND open_qty!='0'")[0]["n"], 0)
        self.assertEqual(h.session_state("A"), "FAILED_REVIEW", "the sell does not end the review")
        r = h.intent("B3", "BUY", "1", "8.99", "A")
        self.assertFalse(r.accepted)
        self.assertEqual(h.available(f"exception:{case['case_id']}"), F("-0.70"))
        self.assertEqual(h.verify(), [])

    def test_entering_failed_review_cancels_the_sessions_working_buys(self):
        """Finding 33, ADR-0008: a session frozen for review must not keep adding inventory.

        B2 was working when A entered FAILED_REVIEW. Before the fix it stayed live (cancel_state NONE), so a
        fill could add shares to a session that could no longer reduce them. Now the transition requests the
        cancel; the hold is kept until the broker confirms it (ORD-04), then released."""
        h = _harness(self)
        h.bootstrap()
        _overcommitted_review(h)
        b2 = h.row("orders", "order_id", "B2")
        self.assertEqual((b2["state"], b2["cancel_state"]), ("CANCEL_PENDING", "REQUESTED"))
        self.assertEqual(_hold(h, "B2"), (F("8.20"), "ACTIVE"), "a cancel request releases nothing")
        self.assertIn("session failed review", b2["reason"])
        h.pump()
        b2 = h.row("orders", "order_id", "B2")
        self.assertEqual((b2["state"], b2["cancel_state"]), ("CANCELED", "CONFIRMED"))
        self.assertEqual(_hold(h, "B2"), (F("0"), "RELEASED"))
        self.assertEqual(h.available("session:A"), F("8.20"))
        self.assertEqual(h.verify(), [])

    def test_failed_review_exits_when_the_last_session_case_is_resolved_by_the_system(self):
        """Finding 10, ADR-0008 (FAILED_REVIEW -> previous state once its exceptions are resolved).

        A is in FAILED_REVIEW with two blocking cases attributed to it: OVERCOMMITTED and UNKNOWN_ORDER (B2).
        The owner covers the shortfall while B2 is still unknown, so A stays frozen. Reconciliation then
        resolves B2 (the broker never received it). Before the fix nothing ever re-checked the session, so it
        stayed FAILED_REVIEW for good with live inventory. Now the next tick returns it to SUSPENDED (a
        RUNNING session returns suspended: new exposure still needs an owner resume)."""
        h = _harness(self)
        h.bootstrap()
        over = _overcommitted_review(h, b2_unknown=True)
        (unknown,) = _cases(h, "UNKNOWN_ORDER")
        self.assertEqual((unknown["session_id"], unknown["order_id"]), ("A", "B2"))
        r = h.owner_cmd("resolve_exception", case_id=over["case_id"], action="cover_shortfall", from_owner="unallocated",
                        note="commission above the reserved envelope")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.session_state("A"), "FAILED_REVIEW", "B2 is still unknown")

        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        self.assertEqual(h.row("orders", "order_id", "B2")["state"], "REJECTED")
        self.assertEqual(h.open_exceptions(), [])
        h.quote("9.00", "9.02")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["review_return_state"]), ("SUSPENDED", None))
        (ev,) = _events(h, "SessionReviewResolved", "A")
        self.assertEqual(ev["info"]["returns_to"], "SUSPENDED")
        self.assertEqual(h.available(f"exception:{over['case_id']}"), F("0"))
        self.assertEqual(h.verify(), [])


# ---------------------------------------------------------------- RECONCILING
class ReconcilingTest(unittest.TestCase):
    def test_correction_after_reconciling_resumes_wind_down_instead_of_sticking(self):
        """Finding 9, ADR-0008 (WINDING_DOWN -> RECONCILING only when flat with no open orders).

        A sold its 5 shares and reached RECONCILING. The broker then corrects the sale to 4 shares, so A holds
        1 share again; a session-attributed exception records the late activity. Before the fix A stayed in
        RECONCILING with NOT_FLAT for good (it cannot sell there, so it also blocked every later session).
        Now it goes back to WINDING_DOWN, reduces the share once the owner has reviewed the case, and closes."""
        h = _harness(self, policy=load_policy(account_limits__daily_loss_trigger="10.00"))
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.99", "9.01")
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        (wd,) = _working_wind_down(h, "A")
        h.pump()
        h.broker.fill(wd["order_id"], F("5"), F("8.94"))
        h.pump()
        h.tick()
        self.assertEqual(h.session_state("A"), "RECONCILING")

        (sale,) = _executions(h, wd["order_id"])
        h.broker.correct_execution(sale["exec_key"], F("4"), F("8.94"))
        h.pump()
        self.assertEqual(h.desk.store.query("SELECT open_qty FROM lots WHERE owner='session:A' AND open_qty!='0'")[0]["open_qty"], "1")
        (late,) = _cases(h, "EXECUTION_ANOMALY")
        self.assertEqual(late["session_id"], "A")

        h.quote("8.99", "9.01")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["review_return_state"]), ("FAILED_REVIEW", "WINDING_DOWN"))
        self.assertEqual(len(_events(h, "SessionWindDownResumed", "A")), 1)
        self.assertEqual(_working_wind_down(h, "A"), [], "no automatic sale while the anomaly is open")

        r = h.owner_cmd("resolve_exception", case_id=late["case_id"], note="broker corrected the wind-down sale")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.session_state("A"), "WINDING_DOWN")
        h.quote("8.99", "9.01")
        h.tick()
        (wd2,) = _working_wind_down(h, "A")
        self.assertEqual((wd2["side"], F(wd2["quantity"]), F(wd2["limit_price"])), ("SELL", F("1"), F("8.94")))
        h.pump()
        h.broker.fill(wd2["order_id"], F("1"), F("8.94"))
        h.pump()
        h.tick()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("CLOSED", "OWNER_ENDED"))
        # 54.00 + (35.76 - 1.00) + (8.94 - 1.00)
        self.assertEqual(F(a["terminal_equity"]), F("96.70"))
        self.assertEqual(h.verify(), [])

    def test_case_attributed_by_owner_only_blocks_completion(self):
        """Finding 31, ADR-0008 (any non-terminal -> FAILED_REVIEW when an open exception is attributed to the
        session; completion needs no open session exceptions).

        The broker overfills A's 5-share wind-down sell with 6 shares. Two cases open: the order anomaly
        (session_id A) and the uncovered sell, which names A only through owner_hint session:A. After the owner
        acknowledges the order anomaly, the uncovered-sell case is still open. Before the fix
        evaluate_reconciling saw only session_id, so A closed with the critical case open (and a covered
        negative balance would have been moved out of the session, failing the ledger invariants on every
        tick). Now the owner-attributed case freezes A in FAILED_REVIEW until it is resolved."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.99", "9.01")
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        (wd,) = _working_wind_down(h, "A")
        h.pump()
        h.broker.fill(wd["order_id"], F("6"), F("8.94"), allow_overfill=True)
        h.pump()
        cases = _cases(h, "EXECUTION_ANOMALY")
        (order_case,) = [c for c in cases if c["session_id"] == "A"]
        (uncovered,) = [c for c in cases if c["session_id"] is None]
        self.assertEqual(uncovered["owner_hint"], "session:A")
        self.assertTrue(uncovered["blocking"])
        self.assertTrue(h.owner_cmd("resolve_exception", case_id=order_case["case_id"], note="overfill seen").accepted)

        h.quote("8.99", "9.01")
        h.tick()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        h.quote("8.99", "9.01")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["review_return_state"]), ("FAILED_REVIEW", "RECONCILING"))
        self.assertIsNone(a["outcome"])

        r = h.owner_cmd("resolve_exception", case_id=uncovered["case_id"], note="broker overfill; short quarantined")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.session_state("A"), "RECONCILING")
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("CLOSED", "OWNER_ENDED"))
        # 54.00 + 5 x 8.94 - 1.00; the sixth share's proceeds stay with exception:<case>
        self.assertEqual(F(a["terminal_equity"]), F("97.70"))
        self.assertEqual(h.verify(), [])


# ---------------------------------------------------------------- wind-down
class WindDownTest(unittest.TestCase):
    def test_reprice_never_pulls_a_working_reduction_under_a_stop(self):
        """Finding 32, ADR-0010 (on_stop_liquidation = NONE: sells already working stay working).

        The wind-down sell is working when the owner latches the stop. Before the fix the reprice timer
        canceled it and the stop then forbade a replacement, so the position was left with no sell at all.
        Now it keeps working."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.99", "9.01")
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        h.pump()
        (wd,) = _working_wind_down(h, "A")
        self.assertEqual(wd["state"], "ACKNOWLEDGED")
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        h.clock.advance(301 * SEC)
        h.quote("8.99", "9.01")
        h.tick()
        h.pump()
        (still,) = _working_wind_down(h, "A")
        self.assertEqual((still["order_id"], still["state"], still["cancel_state"]), (wd["order_id"], "ACKNOWLEDGED", "NONE"))
        h.broker.fill(wd["order_id"], F("5"), F("8.94"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", wd["order_id"])["state"], "FILLED")
        self.assertEqual(h.verify(), [])

    def test_reprice_never_pulls_the_last_permitted_attempt(self):
        """Finding 32 (same defect class), ADR-0008: with max_attempts reached the working sell is the last one
        wind-down may place. Before the fix the reprice canceled it and MAX_ATTEMPTS then blocked any
        replacement; now it keeps working."""
        h = _harness(self, policy=load_policy(wind_down__max_attempts=1))
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.99", "9.01")
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        h.pump()
        (wd,) = _working_wind_down(h, "A")
        h.clock.advance(301 * SEC)
        h.quote("8.99", "9.01")
        h.tick()
        h.pump()
        (still,) = _working_wind_down(h, "A")
        self.assertEqual((still["order_id"], still["cancel_state"]), (wd["order_id"], "NONE"))
        self.assertEqual(h.verify(), [])

    def test_wind_down_price_meets_the_pretrade_collar_on_a_wide_spread(self):
        """Finding 46: bid 8.00 / ask 9.00 (mid 8.50). floor(8.00 x 0.995) = 7.96 is below the pretrade sell
        floor ceil(8.50 x 0.99) = 8.415, so before the fix every wind-down attempt failed PRICE_COLLAR and
        liquidation never started. Now the limit is raised to the collar floor on the tick grid, 8.42."""
        h = _harness(self)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "5", "9.00")
        h.quote("8.00", "9.00")
        self.assertTrue(h.owner_cmd("end_session", session_id="A").accepted)
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["blockers"]), ("WINDING_DOWN", []))
        (wd,) = _working_wind_down(h, "A")
        self.assertEqual((wd["side"], F(wd["quantity"]), F(wd["limit_price"])), ("SELL", F("5"), F("8.42")))
        self.assertEqual(h.verify(), [])

    def test_below_target_resume_restarts_the_wind_down_attempt_budget(self):
        """Finding 48, ADR-0008 (RESUME with max_resumes): a resumed session starts a new wind-down later.

        With max_attempts = 1 the first wind-down used its attempt. Before the fix the RESUME kept the
        counter, so the next target observation was immediately MAX_ATTEMPTS and never placed a sell."""
        policy = load_policy(session_template__below_target_after_target_observed="RESUME", session_template__max_resumes=1,
                             wind_down__max_attempts=1)
        h = _harness(self, policy=policy)
        h.bootstrap()
        _create(h, "A")
        _buy_fill(h, "A", "B1", "10", "9.00")
        h.quote("9.70", "9.72")  # marks 9.00 + 97.00 >= 105.00
        h.tick()
        (wd,) = _working_wind_down(h, "A")
        h.pump()
        h.broker.fill(wd["order_id"], F("10"), F("9.65"))  # realized 104.50 < 105.00
        h.pump()
        h.tick()
        h.reconcile()
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["resumes_used"], a["wind_down_attempts"]), ("RUNNING", 1, {}))

        h.clock.advance(DAY)
        h.broker.advance_settlement()
        self.assertEqual(h.reconcile().body["status"], "CLEAN")
        _buy_fill(h, "A", "B2", "10", "9.00")
        h.quote("9.20", "9.22")  # 13.50 + 92.00 >= 105.00
        h.tick()
        a = h.row("sessions", "session_id", "A")
        self.assertEqual(a["state"], "WINDING_DOWN")
        self.assertNotIn("FIX-EQ-1:MAX_ATTEMPTS", a["blockers"])
        (wd2,) = _working_wind_down(h, "A")
        self.assertEqual((F(wd2["quantity"]), F(wd2["limit_price"])), (F("10"), F("9.15")))
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
