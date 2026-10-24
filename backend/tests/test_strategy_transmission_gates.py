"""Gates a strategy runner applies between a decision and the transmission of its intent, and after it (review fixes
on BR-DAT-02, BR-DAT-18 and BR-DAT-19). Intents now wait for the next session's submission window, so what the
runner checked at the decision can change before it transmits:

* a pending order whose outcome or cancellation is unresolved (UNKNOWN, CANCEL_PENDING) may appear in the instrument
  overnight: no intent is transmitted beside it (BR-DAT-02), it waits until the order resolves or its window ends;
* a restarted runner whose re-warm from the point-in-time store disagrees with the journal's snapshot blocks new
  entries (BR-DAT-18), including a buy the earlier process planned but did not transmit;
* the remainder of an intent already sent is cancelled at its window's end (BR-DAT-19) even when the artifact may no
  longer decide (quarantined, expired), because the cancellation is a consequence of an intent already working.

Every number is a TEST FIXTURE; every money-changing test checks the ledger invariants."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_strategy_runner import (ACCOUNT, CLOCK_UTC, DATASET, RealCoreCase, runtime_spec, trend_bars,  # noqa: E402
                                  wait_for)
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.domain.calendar import WEEKDAYS_ONLY  # noqa: E402
from unknown.domain.clock import NS_PER_S, ManualClock  # noqa: E402
from unknown.research.bars import record_bars  # noqa: E402
from support.research_fixtures import fixture_instrument, manual_bar  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.store.db import connect  # noqa: E402
from unknown.strategy.artifacts import ArtifactRefused  # noqa: E402
from unknown.strategy.journal import cancel_command_id, decision_ids  # noqa: E402
from unknown.strategy.runner import (RunnerConfig, RunnerError, StrategyRunner, decision_instant,  # noqa: E402
                                     submission_window)


class TransmissionGateUnitTest(unittest.TestCase):
    """The transmission-time gate with a scripted core: an UNKNOWN order of another principal, then our own working
    intent of an earlier decision, keep the intent PLANNED and unsent; once neither is open it is sent."""
    AID = "TREND_DAILY_V1@0123456789abcdef"
    DAY = date(2026, 10, 21)

    class Client:
        def __init__(self):
            self.sent = []

        def query(self, name, **args):
            assert name == "quotes"
            return {"FIX-EQ-1": {"bid": "9.99", "ask": "10.01", "age_ms": 10, "delayed": False,
                                 "market_data_type": "unreported", "consolidation": "unknown"}}

        def command(self, command_id, kind, payload, principal):
            self.sent.append(kind)
            return {"accepted": True, "duplicate": False, "body": {"order_id": payload.get("order_id")}, "events": []}

    class Core:
        def __init__(self):
            self.open = []

        def transmission_context(self, account_key, instrument_id):
            return fixture_instrument(instrument_id), 5000, "SIMULATED"

        def open_orders(self, account_key, session_id, instrument_id):
            return list(self.open)

        def commands(self, ids):
            return {}

    def test_an_unresolved_or_own_working_order_holds_the_intent_until_it_resolves(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _, wt = submission_window(runtime_spec(), self.DAY, WEEKDAYS_ONLY, fixture=True)
        clock, client, core = ManualClock(wt.start_ns + 60 * NS_PER_S), self.Client(), self.Core()
        r = StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", Paths(Path(tmp.name))), clock=clock, client=client)
        self.addCleanup(r.close)
        r.core = core
        ids = decision_ids(self.AID, self.DAY, "FIX-EQ-1")
        submit = {"account_key": ACCOUNT, "session_id": "S1", "order_id": ids["order_id"], "instrument_id": "FIX-EQ-1",
                  "side": "BUY", "quantity": "4", "limit_price": "10.05", "deadline_ns": wt.end_ns}
        r.journal.plan([{"artifact_id": self.AID, "session_date": self.DAY, "instrument_id": "FIX-EQ-1", "mode": "live", **ids,
                         "submit_payload": submit, "decision_payload": {"outcome": None, "reason": "z=1", "order_ids": []},
                         "detail": {"window": wt.to_json(), "model_bound": "10.05", "collar_bps": 50}}], clock.now_ns())
        unknown = {"order_id": "owner-1", "state": "UNKNOWN", "side": "BUY", "leaves_qty": "3", "principal_id": "owner",
                   "principal_role": "OWNER"}
        own = {"order_id": "sr-20261020-aaaaaaaaaaaaaaaa", "state": "ACKNOWLEDGED", "side": "BUY", "leaves_qty": "2",
               "principal_id": self.AID, "principal_role": "STRATEGY"}
        for blocking, code in ((unknown, "PENDING_UNRESOLVED"), (own, "OPEN_INTENT")):
            core.open = [blocking]
            with self.assertLogs("unknown.strategy", level="WARNING"):
                row = r._execute(r.journal.get(self.DAY, "FIX-EQ-1"))
            self.assertEqual((row["state"], client.sent), ("PLANNED", []), code)
            self.assertTrue(row["detail"]["pricing_refusal"]["why"].startswith(code), row["detail"]["pricing_refusal"])
            self.assertIsNone(row["detail"].get("transmission"))          # not priced: nothing is journaled as sent
        # another principal's working (resolved) order does not block: its leaves count in the core's checks
        core.open = [{**own, "principal_id": "owner", "principal_role": "OWNER"}]
        row = r._execute(r.journal.get(self.DAY, "FIX-EQ-1"))
        self.assertEqual((row["state"], client.sent), ("RECORDED", ["submit_intent", "record_strategy_decision"]))


class UnresolvedAtTransmissionTest(RealCoreCase):
    """Decided with nothing pending; overnight the owner's FIX-EQ-1 buy goes CANCEL_PENDING. In the window the runner
    transmits FIX-EQ-2 but not FIX-EQ-1, and transmits FIX-EQ-1 once the cancellation is confirmed (BR-DAT-02)."""
    LISTED = ("A",)

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def owner_buy(self, order_id: str, instrument: str, qty: str, limit: str) -> None:
        sess = self.core_rows("SELECT row FROM sessions WHERE session_id='S1'")[0]
        conn = connect(self.paths.db(ACCOUNT), readonly=True)
        try:
            mandate = json.loads(conn.execute("SELECT body FROM mandates WHERE mandate_hash=?",
                                              (sess["mandate_hash"],)).fetchone()["body"])
        finally:
            conn.close()
        self.cmd("submit_intent", account_key=ACCOUNT, session_id="S1", order_id=order_id, instrument_id=instrument,
                 side="BUY", quantity=qty, limit_price=limit, deadline_ns=time.time_ns() + 3600 * NS_PER_S,
                 policy_hash=mandate["policy_hash"])
        wait_for(lambda: self.state(order_id) == "ACKNOWLEDGED", what=f"{order_id} acknowledged")

    def state(self, order_id: str) -> str | None:
        rows = [o for o in self.orders() if o["order_id"] == order_id]
        return rows[0]["state"] if rows else None

    def test_no_intent_is_transmitted_beside_an_order_that_became_unresolved_after_the_decision(self):
        a = self.ids["A"]
        t = decision_instant(self.day, CLOCK_UTC)
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(t + 600 * NS_PER_S)) as r:
            s = r.run_once()
        self.assertEqual({x["waiting"] for x in s["instruments"].values()}, {"WINDOW"}, s)
        # overnight: the owner buys FIX-EQ-1 and cancels; the broker has not confirmed the cancellation
        self.assertEqual(self.orders(), [])
        self.owner_buy("owner-b9", "FIX-EQ-1", "3", "9.00")
        self.svc.broker.defer_cancel_confirmation(True)
        self.cmd("request_cancel", order_id="owner-b9", reason="test")
        wait_for(lambda: self.state("owner-b9") == "CANCEL_PENDING", what="CANCEL_PENDING")
        with self.runner("A", "live") as r, self.assertLogs("unknown.strategy", level="WARNING"):
            s = r.run_once()
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertEqual((one["state"], one["submitted"], one.get("waiting")), ("PLANNED", False, "PRICING"), one)
        self.assertEqual(two["outcome"], "INTENT_SUBMITTED", two)
        self.assertEqual([o["instrument_id"] for o in self.orders(a)], ["FIX-EQ-2"])
        self.assertTrue(self.svc.broker.confirm_cancel("owner-b9"))
        wait_for(lambda: self.state("owner-b9") == "CANCELED", what="cancel confirmed")
        with self.runner("A", "live") as r:
            s = r.run_once()
        self.assertEqual(s["instruments"]["FIX-EQ-1"]["outcome"], "INTENT_SUBMITTED", s["instruments"]["FIX-EQ-1"])
        self.assertEqual(sorted(o["instrument_id"] for o in self.orders(a)), ["FIX-EQ-1", "FIX-EQ-2"])
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class RewarmMismatchBlocksPlannedBuysTest(RealCoreCase):
    """A live runner decides after the close and plans two buys for the next session's window, then exits. The
    provider silently corrects FIX-EQ-1's bar. A new process in the window re-warms, finds the mismatch, and
    transmits neither buy: both are recorded BLOCKED FEATURE_STATE_MISMATCH (BR-DAT-18)."""
    LISTED = ("A",)

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def test_a_buy_planned_before_the_restart_is_not_transmitted_after_a_mismatch(self):
        a = self.ids["A"]
        t = decision_instant(self.day, CLOCK_UTC)
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(t + 600 * NS_PER_S)) as r:
            s = r.run_once()
        self.assertEqual({(x["state"], x["waiting"]) for x in s["instruments"].values()}, {("PLANNED", "WINDOW")}, s)
        bars = trend_bars("FIX-EQ-1", self.day, 60, 800, 1000)
        last = bars[-1]
        fixed = manual_bar("FIX-EQ-1", self.day, last.open.to_str(), last.high.to_str(), last.low.to_str(),
                           last.low.to_str(), int(last.volume.to_str()))
        store = ResearchStore(self.root / "research" / "research.sqlite")
        record_bars(store, [fixed], DATASET, time.time_ns())
        store.close()
        with self.runner("A", "live") as r, self.assertLogs("unknown.strategy", level="WARNING"):
            s = r.run_once()
        self.assertEqual((s["rewarm"]["status"], s["rewarm"]["session_date"]), ("MISMATCH", self.day.isoformat()))
        for inst in ("FIX-EQ-1", "FIX-EQ-2"):
            x = s["instruments"][inst]
            self.assertEqual((x["state"], x["outcome"], x["submitted"]), ("RECORDED", "BLOCKED", False), x)
            self.assertTrue(x["reason"].startswith("FEATURE_STATE_MISMATCH: not submitted"), x["reason"])
        self.assertEqual(self.orders(a), [])
        self.assertEqual(sorted(d["outcome"] for d in self.decisions(a)), ["BLOCKED", "BLOCKED"])
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class QuarantinedWindowEndCase(RealCoreCase):
    """FIX-EQ-1's intent rests below the market (bound 9.54 < ask 10.01); the artifact is quarantined while it works."""
    LISTED = ("A",)
    LAST_CLOSE = {"FIX-EQ-1": 950, "FIX-EQ-2": 4200}

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def resting_then_quarantined(self) -> tuple[str, dict]:
        a = self.ids["A"]
        with self.runner("A", "live") as r:
            s = r.run_once()
        self.assertEqual(s["instruments"]["FIX-EQ-1"]["outcome"], "INTENT_SUBMITTED", s)
        oid = decision_ids(a, self.day, "FIX-EQ-1")["order_id"]
        wait_for(lambda: self.order_state(oid) == "ACKNOWLEDGED", what="ack")
        self.rf.quarantine(self.hashes["A"])
        return oid, s["window"]

    def order_state(self, oid: str) -> str:
        return next(o for o in self.orders(self.ids["A"]) if o["order_id"] == oid)["state"]


class QuarantinedRunOnceCancelsAtWindowEndTest(QuarantinedWindowEndCase):
    def test_a_refused_artifacts_working_remainder_is_still_cancelled_at_the_windows_end(self):
        a = self.ids["A"]
        oid, window = self.resting_then_quarantined()
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(window["end_ns"] - NS_PER_S)) as r:
            with self.assertRaises(ArtifactRefused):                         # the window is still open: nothing to do
                r.run_once()
        self.assertEqual(self.order_state(oid), "ACKNOWLEDGED")
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(window["end_ns"])) as r, \
                self.assertLogs("unknown.strategy", level="WARNING") as logs:
            with self.assertRaises(ArtifactRefused) as cm:                   # it may not decide ...
                r.run_once()
            note = r.journal.get(self.day, "FIX-EQ-1")["detail"]["window_cancel"]
        self.assertEqual(cm.exception.code, "STATUS_NOT_PERMITTED")
        self.assertEqual((note["command_id"], note["accepted"]), (cancel_command_id(a, self.day, "FIX-EQ-1"), True))
        self.assertTrue(any("window-end cancellation" in line for line in logs.output), logs.output)
        wait_for(lambda: self.order_state(oid) == "CANCELED", what="cancel")      # ... but its remainder is cancelled
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class QuarantinedRunLoopCancelsAtWindowEndTest(QuarantinedWindowEndCase):
    def test_the_scheduled_loop_cancels_the_remainder_of_a_refused_artifact(self):
        a = self.ids["A"]
        oid, window = self.resting_then_quarantined()
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(window["end_ns"])) as r:
            stop = threading.Event()
            th = threading.Thread(target=r.run_loop, args=(stop,), kwargs={"grace_s": 0, "poll_s": 0.05}, daemon=True)
            try:
                with self.assertLogs("unknown.strategy", level="WARNING") as logs:
                    th.start()
                    wait_for(lambda: self.order_state(oid) in ("CANCEL_PENDING", "CANCELED"), what="the loop's cancel")
                    # The loop logs after the core has accepted the cancel: wait for the warning too, or the block
                    # can close before the loop thread logs it (a race that failed under load).
                    wait_for(lambda: any("window-end cancellation" in line for line in logs.output),
                             what="the loop's window-end warning")
            finally:
                stop.set()
                th.join(10)
            self.assertFalse(th.is_alive())
        wait_for(lambda: self.order_state(oid) == "CANCELED", what="cancel")
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class FailedCycleCancelsAtWindowEndTest(QuarantinedWindowEndCase):
    """Not only a refusal: a cycle that fails before deciding (here the point-in-time store is gone, so the restart
    check cannot run) still cancels the remainder whose window has ended."""

    def test_a_cycle_that_cannot_decide_still_cancels_the_remainder(self):
        a = self.ids["A"]
        with self.runner("A", "live") as r:
            s = r.run_once()
        oid = decision_ids(a, self.day, "FIX-EQ-1")["order_id"]
        self.assertEqual(s["instruments"]["FIX-EQ-1"]["outcome"], "INTENT_SUBMITTED", s)
        wait_for(lambda: self.order_state(oid) == "ACKNOWLEDGED", what="ack")
        (self.root / "research" / "research.sqlite").unlink()
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(s["window"]["end_ns"])) as r, \
                self.assertLogs("unknown.strategy", level="WARNING"):
            with self.assertRaises(RunnerError) as cm:
                r.run_once()
        self.assertEqual(cm.exception.code, "RESEARCH_STORE_MISSING")
        wait_for(lambda: self.order_state(oid) == "CANCELED", what="cancel")
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


del RealCoreCase, QuarantinedWindowEndCase     # bases only

if __name__ == "__main__":
    unittest.main()
