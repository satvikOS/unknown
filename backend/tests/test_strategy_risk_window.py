"""The strategy runner applies the backtest's own risk term (BR-DAT-04) and execution rule (BR-DAT-19), against a real
core service: intents wait for the submission window of the next session, their limit is set at transmission from a
fresh validated quote, the remainder is cancelled at the window's end, and a buy whose loss at the adverse move plus
entry and exit fees exceeds the risk budget is sized down, to zero if need be. Every number is a TEST FIXTURE."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_strategy_runner import (ACCOUNT, CLOCK_UTC, DEV, HOLD, ResearchFixture, RealCoreCase, runtime_spec,  # noqa: E402
                                  wait_for, window_clock)
from unknown.domain.calendar import WEEKDAYS_ONLY, load_calendar  # noqa: E402
from unknown.domain.clock import NS_PER_S, ManualClock, utc_ns  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.research import execution, sizing as research_sizing  # noqa: E402
from unknown.research.fixtures import fixture_instrument  # noqa: E402
from unknown.research.spec import parse_spec  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.strategy import artifacts, runner as runner_module, sizing as runtime_sizing  # noqa: E402
from unknown.strategy.artifacts import ArtifactRefused  # noqa: E402
from unknown.strategy.journal import cancel_command_id, decision_ids  # noqa: E402
from unknown.strategy.runner import (RunnerConfig, StrategyRunner, decision_instant, quote_reference,  # noqa: E402
                                     submission_window)

REPO = Path(__file__).resolve().parents[2]


class SharedImplementationTest(unittest.TestCase):
    def test_the_runner_uses_the_backtests_window_limit_and_sizing_functions(self):
        self.assertIs(runner_module.window_times, execution.window_times)
        self.assertIs(runner_module.transmission_limit, execution.transmission_limit)
        self.assertIs(runtime_sizing.plan_orders, research_sizing.plan_orders)

    def test_the_window_is_calendar_aware(self):
        xnys = load_calendar(REPO / "config" / "calendars" / "XNYS.json")
        exec_date, wt = submission_window(runtime_spec(), date(2026, 11, 25), xnys, fixture=False)
        self.assertEqual(exec_date, date(2026, 11, 27))                      # Thanksgiving skipped
        self.assertEqual((wt.start_ns, wt.end_ns), (utc_ns(2026, 11, 27, 14, 45), utc_ns(2026, 11, 27, 17, 45)))  # early close
        exec_date, wt = submission_window(runtime_spec(), date(2026, 10, 23), WEEKDAYS_ONLY, fixture=True)
        self.assertEqual((exec_date, wt.start_ns, wt.end_ns),
                         (date(2026, 10, 26), utc_ns(2026, 10, 26, 14, 45), utc_ns(2026, 10, 26, 20, 45)))

    def test_quote_validation_before_transmission(self):
        ok = {"bid": "9.99", "ask": "10.01", "age_ms": 100, "delayed": False, "market_data_type": "unreported",
              "consolidation": "unknown"}
        self.assertEqual(quote_reference(ok, max_age_ms=5000, account_mode="SIMULATED"), (Fixed.parse("10.000"), ""))
        cases = [(None, "NO_QUOTE"), ({**ok, "age_ms": 6000}, "QUOTE_STALE"), ({**ok, "delayed": True}, "QUOTE_NOT_LIVE"),
                 ({**ok, "market_data_type": "frozen"}, "QUOTE_NOT_LIVE"), ({**ok, "ask": None}, "QUOTE_NOT_TWO_SIDED"),
                 ({**ok, "bid": "10.02"}, "QUOTE_CROSSED")]
        for q, code in cases:
            mid, why = quote_reference(q, max_age_ms=5000, account_mode="SIMULATED")
            self.assertIsNone(mid)
            self.assertTrue(why.startswith(code), (q, why))
        self.assertTrue(quote_reference(ok, max_age_ms=5000, account_mode="LIVE")[1].startswith("QUOTE_NOT_CONSOLIDATED"))
        self.assertEqual(quote_reference({**ok, "consolidation": "consolidated"}, max_age_ms=5000, account_mode="LIVE")[1], "")
        self.assertTrue(quote_reference(ok, max_age_ms=None, account_mode="SIMULATED")[1].startswith("NO_ACTIVE_POLICY"))


class SchemaV1RefusedTest(unittest.TestCase):
    def test_the_runner_refuses_a_v1_spec(self):
        import copy
        body = copy.deepcopy(runtime_spec().body)
        body["schema_version"] = 1
        del body["rules"]["sizing"]["risk_budget"]
        del body["rules"]["execution"]["submission_window"]
        body["rules"]["execution"]["unfilled_policy"] = "EXPIRE_END_OF_DAY"
        for f in body["features"]:
            del f["validity_window_s"]
        with tempfile.TemporaryDirectory() as tmp:
            rf = ResearchFixture(Path(tmp) / "experiments.sqlite", parse_spec(body))
            try:
                h, aid = rf.register()
                rf.to_shadow(h)
                with self.assertRaises(ArtifactRefused) as cm:
                    artifacts.load(rf.reg, aid, mode="shadow", today=date.today())
                self.assertEqual(cm.exception.code, "SPEC_SCHEMA_V1")
            finally:
                rf.close()


class PricingRefusalTest(unittest.TestCase):
    """No valid quote in the window: the intent stays PLANNED and is retried; past the window it is never sent and
    the decision is recorded BLOCKED RUNNER_LATE naming the last refusal."""
    AID = "TREND_DAILY_V1@0123456789abcdef"
    DAY = date(2026, 10, 21)

    class Client:
        def __init__(self):
            self.sent = []

        def query(self, name, **args):
            assert name == "quotes"
            return {}                                         # the core has no quote for the instrument

        def command(self, command_id, kind, payload, principal):
            self.sent.append(kind)
            if kind == "submit_intent":
                raise AssertionError("sent without a valid quote")
            return {"accepted": True, "duplicate": False, "body": {"recorded": True}, "events": []}

    class Core:
        def transmission_context(self, account_key, instrument_id):
            return fixture_instrument(instrument_id), 5000, "SIMULATED"

        def open_orders(self, account_key, session_id, instrument_id):
            return ()                                         # nothing pending in the instrument (BR-DAT-02 gate passes)

        def commands(self, ids):
            return {}

    def test_no_quote_keeps_it_planned_then_it_expires_unsent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _, wt = submission_window(runtime_spec(), self.DAY, WEEKDAYS_ONLY, fixture=True)
        clock, client = ManualClock(wt.start_ns + 60 * NS_PER_S), self.Client()
        r = StrategyRunner(RunnerConfig(self.AID, ACCOUNT, "live", Paths(Path(tmp.name))), clock=clock, client=client)
        self.addCleanup(r.close)
        r.core = self.Core()
        ids = decision_ids(self.AID, self.DAY, "FIX-EQ-1")
        submit = {"account_key": ACCOUNT, "session_id": "S1", "order_id": ids["order_id"], "instrument_id": "FIX-EQ-1",
                  "side": "BUY", "quantity": "4", "limit_price": "10.05", "deadline_ns": wt.end_ns}
        r.journal.plan([{"artifact_id": self.AID, "session_date": self.DAY, "instrument_id": "FIX-EQ-1", "mode": "live", **ids,
                         "submit_payload": submit, "decision_payload": {"outcome": None, "reason": "z=1", "order_ids": []},
                         "detail": {"window": wt.to_json(), "model_bound": "10.05", "collar_bps": 50}}], clock.now_ns())
        with self.assertLogs("unknown.strategy", level="WARNING"):
            row = r._execute(r.journal.get(self.DAY, "FIX-EQ-1"))
        self.assertEqual(row["state"], "PLANNED")
        self.assertEqual(row["detail"]["pricing_refusal"]["why"], "NO_QUOTE")
        self.assertEqual(r._row_summary(row)["waiting"], "PRICING")
        self.assertEqual(client.sent, [])
        clock.set(wt.end_ns)
        with self.assertLogs("unknown.strategy", level="WARNING"):
            row = r._execute(r.journal.get(self.DAY, "FIX-EQ-1"))
        self.assertEqual((row["state"], client.sent), ("RECORDED", ["record_strategy_decision"]))
        self.assertEqual(row["decision_payload"]["outcome"], "BLOCKED")
        self.assertIn("RUNNER_LATE", row["decision_payload"]["reason"])
        self.assertIn("last transmission attempt: NO_QUOTE", row["decision_payload"]["reason"])


# ===================================================================== end to end
class WindowTransmissionTest(RealCoreCase):
    """BR-DAT-19 against a real core. FIX-EQ-2's model bound (44.22) is far above the market (mid 42.00): fixed at
    the decision it was rejected by the core's 1% price collar; set at transmission it is 42.21 and trades."""
    LISTED = ("A",)
    LAST_CLOSE = {"FIX-EQ-1": 1000, "FIX-EQ-2": 4400}

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def test_intents_wait_for_the_window_are_priced_at_transmission_and_never_carried_over(self):
        a = self.ids["A"]
        t = decision_instant(self.day, CLOCK_UTC)
        exec_date = WEEKDAYS_ONLY.add_business_days(self.day, 1)
        # 1. at the decision (after the close): decided and journaled, nothing sent before the window opens
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(t + 600 * NS_PER_S)) as r:
            s = r.run_once()
            planned = {x["instrument_id"]: x for x in r.journal.rows(self.day)}
        self.assertEqual(s["window"]["start_ns"], utc_ns(exec_date.year, exec_date.month, exec_date.day, 14, 45))
        self.assertEqual(s["deadline_ns"], s["window"]["end_ns"])
        for i in ("FIX-EQ-1", "FIX-EQ-2"):
            self.assertEqual((s["instruments"][i]["state"], s["instruments"][i]["waiting"]), ("PLANNED", "WINDOW"), s)
        self.assertEqual(planned["FIX-EQ-2"]["submit_payload"]["limit_price"], "44.22")           # the model bound
        self.assertEqual((self.orders(a), self.decisions(a)), ([], []))
        # 2. inside the window: priced from the core's quote, journaled, then sent
        with self.runner("A", "live") as r:
            s = r.run_once()
            rows = {x["instrument_id"]: x for x in r.journal.rows(self.day)}
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertEqual({one["outcome"], two["outcome"]}, {"INTENT_SUBMITTED"}, s)
        self.assertEqual((two["transmission"]["model_bound"], two["transmission"]["reference_mid"], two["transmission"]["limit"]),
                         ("44.22", "42.000", "42.21"))
        self.assertEqual(rows["FIX-EQ-2"]["submit_payload"]["limit_price"], "42.21")
        self.assertEqual(one["transmission"]["limit"], "10.05")                                    # min(10.05, 10.05)
        orders = {o["instrument_id"]: o for o in self.orders(a)}
        self.assertEqual((orders["FIX-EQ-2"]["limit_price"], orders["FIX-EQ-2"]["deadline_ns"]), ("42.21", s["window"]["end_ns"]))
        recs = {d["instrument_id"]: d for d in self.decisions(a)}
        self.assertTrue(recs["FIX-EQ-2"]["reason"].startswith(
            "TRANSMITTED in the window: limit 42.21 = min(model bound 44.22, mid 42.000 + 50 bps)"), recs["FIX-EQ-2"]["reason"])
        self.assertEqual(recs["FIX-EQ-2"]["observation_time_ns"], t)                               # decided at the close
        wait_for(lambda: all(o["state"] == "FILLED" for o in self.orders(a)), what="fills")
        # 3. after the window: nothing left to cancel (filled), and the next run decides nothing again
        clock = ManualClock(s["window"]["end_ns"] + NS_PER_S)
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=clock) as r:
            s3 = r.run_once()
        self.assertTrue(all(x["already_decided"] for x in s3["instruments"].values()), s3)
        self.assertEqual({v["state"] for v in s3["window_cancels"].values()}, {"FILLED"})
        self.assertEqual(len(self.orders(a)), 2)
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class WindowEndCancelTest(RealCoreCase):
    """An intent resting below the market at the window's end is cancelled (CANCEL_AT_WINDOW_END): its reservation is
    released once the broker confirms, and nothing is carried over to the next session."""
    LISTED = ("A",)
    LAST_CLOSE = {"FIX-EQ-1": 950, "FIX-EQ-2": 4200}     # FIX-EQ-1 bound 9.54 < ask 10.01: rests unfilled

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def test_the_remainder_is_cancelled_at_the_windows_end(self):
        a = self.ids["A"]
        with self.runner("A", "live") as r:
            s = r.run_once()
        one = s["instruments"]["FIX-EQ-1"]
        self.assertEqual((one["outcome"], one["transmission"]["limit"]), ("INTENT_SUBMITTED", "9.54"), one)
        oid = decision_ids(a, self.day, "FIX-EQ-1")["order_id"]
        wait_for(lambda: next(o for o in self.orders(a) if o["order_id"] == oid)["state"] == "ACKNOWLEDGED", what="ack")
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths),
                            clock=ManualClock(s["window"]["end_ns"] - NS_PER_S)) as r:
            self.assertNotIn("window_cancels", r.run_once())                     # the window is still open
        with StrategyRunner(RunnerConfig(a, ACCOUNT, "live", self.paths), clock=ManualClock(s["window"]["end_ns"])) as r:
            s2 = r.run_once()
            again = r.run_once()                                                   # one cancel per decision
        note = s2["window_cancels"][f"{self.day.isoformat()}|FIX-EQ-1"]
        self.assertEqual((note["command_id"], note["accepted"], note["state_before"]),
                         (cancel_command_id(a, self.day, "FIX-EQ-1"), True, "ACKNOWLEDGED"))
        self.assertNotIn(f"{self.day.isoformat()}|FIX-EQ-1", again.get("window_cancels", {}))
        wait_for(lambda: next(o for o in self.orders(a) if o["order_id"] == oid)["state"] == "CANCELED", what="cancel")
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


class RiskBudgetRunnerTest(RealCoreCase):
    """BR-DAT-04 against a real core, with TREND_DAILY_V1's own 3% risk budget on the 100.00 fixture session: the
    1.00 minimum commission (1.20 reserved each way under the policy) leaves room for one FIX-EQ-1 share
    (0.5136 + 2.40 <= 3.00) and none of FIX-EQ-2 (2.1569 + 2.40 > 3.00)."""
    LISTED = ("A",)
    LOSS_FRACTION = "0.03"

    def make_artifacts(self) -> None:
        self.add("A", "10", "paper")

    def test_fees_in_the_risk_term_size_buys_down_and_to_zero(self):
        a = self.ids["A"]
        with self.runner("A", "live") as r:
            s = r.run_once()
            rows = {x["instrument_id"]: x for x in r.journal.rows(self.day)}
        one, two = s["instruments"]["FIX-EQ-1"], s["instruments"]["FIX-EQ-2"]
        self.assertEqual(one["outcome"], "INTENT_SUBMITTED", one)
        self.assertIn("RISK_BUDGET: buy limited 4->1", one["reason"])
        self.assertEqual(two["outcome"], "NO_TRADE", two)
        self.assertIn("risk budget cannot cover", two["reason"])
        self.assertEqual(rows["FIX-EQ-1"]["detail"]["risk"]["budget"], "3.000000")
        self.assertEqual(rows["FIX-EQ-2"]["detail"]["risk"]["max_add"], "0")
        self.assertEqual([(o["instrument_id"], o["quantity"]) for o in self.orders(a)], [("FIX-EQ-1", "1")])
        recs = {d["instrument_id"]: d for d in self.decisions(a)}
        self.assertEqual((recs["FIX-EQ-1"]["target_quantity"], recs["FIX-EQ-2"]["outcome"]), ("4", "NO_TRADE"))
        wait_for(lambda: self.orders(a)[0]["state"] == "FILLED", what="the fill")
        self.stop_core()
        self.assertEqual(self.verify_ledger(), [])


del RealCoreCase     # imported as a base only

if __name__ == "__main__":
    unittest.main()
