"""Server-side alerts (CAP-MKT-09, CAP-NEWS-13): rules, crossing semantics, the engine on a fake market-data bus,
news and filing alerts from the news store, snooze, inbox, audit, notifications and the SSE stream.

Quotes, news items and bars here are TEST FIXTURES written by the test, not market data."""
from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import threading
import types
import unittest
from decimal import Decimal
from pathlib import Path

from unknown.alerts import evaluate, rules, studies
from unknown.alerts.engine import AlertEngine
from unknown.alerts.notify import MacNotifier
from unknown.alerts.store import AlertStore, store_path
from unknown.alerts.stream import serve
from unknown.api.routes import RouteConflict, alerts as alert_routes  # noqa: F401
from unknown.news.feeds import FeedItem
from unknown.news.store import NewsStore
from unknown.service.paths import Paths

NS = 1_000_000_000
T0 = 1_790_000_000 * NS


def quote(key="AAPL.XNAS", last=None, data_type="DELAYED", **f):
    return {"key": key, "seq": 1, "last": last, "bid": f.get("bid"), "ask": f.get("ask"), "volume": f.get("volume"),
            "prev_close": f.get("prev_close"), "source": "TEST", "data_type": data_type, "ts_source_ns": T0 - 5,
            "ts_recv_ns": T0 - 2}


class FakeClient:
    def __init__(self):
        self.pending: list = []

    def drain(self, timeout):
        out, self.pending = self.pending, []
        return out


class FakeBus:
    def __init__(self):
        self.opened: list[list[str]] = []
        self.client = FakeClient()

    def open_client(self, keys):
        self.opened.append(keys)
        return self.client

    def close_client(self, c):
        pass


class FakeNotifier:
    def __init__(self, result="delivered"):
        self.sent, self.result = [], result

    def status(self):
        return "available", None

    def send(self, title, message, subtitle=""):
        self.sent.append((title, message, subtitle))
        return self.result, None if self.result == "delivered" else "test failure"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class RuleTests(unittest.TestCase):
    def test_validation(self):
        r = rules.validate({"kind": "price_cross", "name": "AAPL 200", "key": "AAPL.XNAS", "level": "200.50"})
        self.assertEqual((r["field"], r["direction"], r["hysteresis"], r["repeat"], r["allow_delayed"]),
                         ("last", "either", "0", "rearm", True))
        self.assertEqual(rules.validate({"kind": "time", "name": "t", "at": "2026-10-24T15:30:00-04:00"})["at_ns"],
                         1_792_870_200 * NS)
        bad = [{"kind": "price_cross", "name": "x", "key": "bad key", "level": "1"},
               {"kind": "price_cross", "name": "x", "key": "AAPL.XNAS", "level": "NaN"},
               {"kind": "price_cross", "name": "x", "key": "AAPL.XNAS", "level": "1", "quantity": "5"},  # no order fields
               {"kind": "pct_move", "name": "x", "key": "AAPL.XNAS", "threshold": "15"},
               {"kind": "time", "name": "x", "at": "2026-10-24T15:30:00"},
               {"kind": "study", "name": "x", "key": "AAPL.XNAS", "study": "macd", "level": "1"},
               {"kind": "study", "name": "x", "key": "AAPL.XNAS", "study": "sma", "compare": "price", "level": "1"},
               {"kind": "filing", "name": "x", "issuer": "a b"},
               {"kind": "order", "name": "x"}, {"kind": "volume", "name": "", "key": "AAPL.XNAS", "threshold": "1"}]
        for b in bad:
            with self.assertRaises(rules.RuleError, msg=b):
                rules.validate(b)

    def test_studies_match_the_chart_definitions(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0]
        self.assertEqual(studies.sma(x, 3), 4.0)
        e = sum(x[:3]) / 3
        for v in x[3:]:
            e = 0.5 * v + 0.5 * e
        self.assertAlmostEqual(studies.ema(x, 3), e)
        self.assertEqual(studies.rsi([1.0, 2.0, 3.0], 2), 100.0)
        self.assertEqual(studies.rsi([1.0, 1.0, 1.0], 2), 50.0)
        self.assertIsNone(studies.sma(x, 8))


class CrossingTests(unittest.TestCase):
    def run_series(self, rule, values, data_type="REALTIME"):
        state, fired = {}, []
        for v in values:
            t, state, _ = evaluate.evaluate_quote(rule, state, quote(last=v, data_type=data_type))
            if t:
                fired.append((v, t["direction"]))
        return fired

    def test_repeatable_cross_with_hysteresis_rearms_only_below_level_minus_h(self):
        rule = rules.validate({"kind": "price_cross", "name": "x", "key": "AAPL.XNAS", "level": "100", "direction": "up",
                               "hysteresis": "1"})
        fired = self.run_series(rule, ["99", "100.5", "99.5", "100.2", "98.9", "100", "101", "99.8", "100.1"])
        self.assertEqual(fired, [("100.5", "up"), ("100", "up")])

    def test_first_value_beyond_the_level_does_not_fire_a_cross(self):
        rule = rules.validate({"kind": "price_cross", "name": "x", "key": "AAPL.XNAS", "level": "100"})
        self.assertEqual(self.run_series(rule, ["101", "102", "99"]), [("99", "down")])

    def test_trigger_record_and_delayed_labelling(self):
        rule = rules.validate({"kind": "price_cross", "name": "x", "key": "AAPL.XNAS", "level": "100", "field": "mid"})
        state = {}
        _, state, _ = evaluate.evaluate_quote(rule, state, quote(bid="99", ask="99.5"))
        t, _, _ = evaluate.evaluate_quote(rule, state, quote(bid="100", ask="100.10"))
        self.assertEqual((t["observed"], t["threshold"], t["source"], t["data_type"], t["label"]),
                         ("100.05", "100", "TEST", "DELAYED", "triggered on delayed data"))
        self.assertEqual((t["ts_source_ns"], t["ts_recv_ns"]), (T0 - 5, T0 - 2))

    def test_real_time_only_and_frozen_data_are_not_evaluated(self):
        rule = rules.validate({"kind": "price_cross", "name": "x", "key": "AAPL.XNAS", "level": "100",
                               "allow_delayed": False})
        self.assertEqual(self.run_series(rule, ["99", "101", "99", "101"], data_type="DELAYED"), [])
        _, _, status = evaluate.evaluate_quote(rule, {}, quote(last="99", data_type="DELAYED"))
        self.assertEqual((status["evaluating"], status["reason"]), (False, "NOT_ENTITLED"))
        open_rule = rules.validate({"kind": "price_cross", "name": "x", "key": "AAPL.XNAS", "level": "100"})
        self.assertEqual(self.run_series(open_rule, ["99", "101"], data_type="FROZEN"), [])

    def test_percent_move_and_volume_fire_on_first_observation(self):
        rule = rules.validate({"kind": "pct_move", "name": "x", "key": "AAPL.XNAS", "threshold": "0.05"})
        t, _, _ = evaluate.evaluate_quote(rule, {}, quote(last="94", prev_close="100"))
        self.assertEqual((t["direction"], t["observed"]), ("down", "-0.060000"))
        vol = rules.validate({"kind": "volume", "name": "v", "key": "AAPL.XNAS", "threshold": "1000000"})
        t, st, _ = evaluate.evaluate_quote(vol, {}, quote(volume="1200000"))
        self.assertIsNotNone(t)
        t, _, _ = evaluate.evaluate_quote(vol, st, quote(volume="1300000"))
        self.assertIsNone(t)

    def test_study_value_uses_stored_closes_plus_the_live_price(self):
        rule = rules.validate({"kind": "study", "name": "s", "key": "AAPL.XNAS", "study": "sma", "n": 3,
                               "compare": "price", "direction": "up"})
        closes = [10.0, 10.0]
        _, st, _ = evaluate.evaluate_quote(rule, {}, quote(last="9"), closes)        # SMA 9.667, price below
        t, _, _ = evaluate.evaluate_quote(rule, st, quote(last="11"), closes)       # SMA 10.333, price above
        self.assertEqual((t["direction"], t["study"], t["threshold"]), ("up", "SMA 3", "10.3333"))
        _, _, status = evaluate.evaluate_quote(rule, {}, quote(last="9"), [])
        self.assertEqual(status["reason"], "NO_DATA")


class EngineCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = Clock()
        self.bus = FakeBus()
        self.notes = FakeNotifier()
        self.store = AlertStore(store_path(self.root), clock_ns=self.clock)
        self.addCleanup(self.store.close)
        self.engine = None

    def make_engine(self):
        self.engine = AlertEngine(self.root, bus_factory=lambda: self.bus, notifier=self.notes, clock_ns=self.clock)
        self.addCleanup(self.engine.stop)
        return self.engine

    def add(self, **rule):
        return self.store.create_rule(rules.validate({"name": "t", **rule}), actor="owner")

    def push(self, *quotes_):
        self.bus.client.pending += [("quote", q) for q in quotes_]
        self.clock.t += NS
        self.engine.step()


class EngineTests(EngineCase):
    def test_once_rule_triggers_exactly_once_on_an_oscillating_fixture(self):
        r = self.add(kind="price_cross", key="AAPL.XNAS", level="100", direction="up", repeat="once")
        self.make_engine().step()
        self.assertEqual(self.bus.opened, [["AAPL.XNAS"]])
        for v in ["99", "101", "99", "101", "99", "101"]:
            self.push(quote(last=v))
        inbox = self.store.events()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(self.store.rule(r["rule_id"])["status"], "triggered")
        self.assertEqual(inbox[0]["trigger"]["observed"], "101")
        self.assertEqual(inbox[0]["notify_status"], "delivered")
        self.assertEqual(len(self.notes.sent), 1)
        actions = [a["action"] for a in self.store.audit_log()]
        self.assertIn("trigger", actions)
        self.assertIn("triggered", actions)

    def test_snooze_suppresses_and_pause_stops_evaluation(self):
        r = self.add(kind="price_cross", key="AAPL.XNAS", level="100")
        self.make_engine().step()
        self.store.snooze(r["rule_id"], self.clock.t + 60 * NS, actor="owner")
        self.push(quote(last="99"), )
        self.push(quote(last="101"))
        self.assertEqual(self.store.events(), [])
        self.assertIn("suppressed", [a["action"] for a in self.store.audit_log()])
        self.clock.t += 120 * NS
        self.push(quote(last="99"))
        self.assertEqual(len(self.store.events()), 1)
        self.store.set_status(r["rule_id"], "paused", actor="owner")
        self.push(quote(last="101"))
        self.push(quote(last="99"))
        self.assertEqual(len(self.store.events()), 1)

    def test_source_outage_puts_rules_in_not_evaluating_and_notifies_once(self):
        r = self.add(kind="price_cross", key="AAPL.XNAS", level="100")
        self.make_engine().step()
        for _ in range(3):
            self.bus.client.pending.append(("status", {"key": "AAPL.XNAS", "state": "UNAVAILABLE",
                                                       "reason": "NOT_CONNECTED: gateway down", "code": "NOT_CONNECTED"}))
            self.clock.t += 3 * NS
            self.engine.step()
        ev = self.store.rule(r["rule_id"])["evaluation"]
        self.assertEqual((ev["evaluating"], ev["reason"]), (False, "NOT_CONNECTED"))
        self.assertEqual([n[0] for n in self.notes.sent], ["Alert not evaluating"])

    def test_restart_lists_the_downtime_window_as_not_evaluated_and_resumes(self):
        self.add(kind="time", at="2026-10-24T15:30:00-04:00")
        self.make_engine().step()
        self.engine.stop()
        self.clock.t += 600 * NS
        eng = self.make_engine()
        eng.step()
        gap = [a for a in self.store.audit_log() if a["action"] == "not_evaluated"]
        self.assertEqual(len(gap), 1)
        self.assertEqual(gap[0]["detail"]["to_ns"] - gap[0]["detail"]["from_ns"], 600 * NS)

    def test_time_rule_fires_once(self):
        r = self.add(kind="time", at="2026-10-24T15:30:00-04:00")
        eng = self.make_engine()
        self.clock.t = r["rule"]["at_ns"] - NS
        eng.step()
        self.clock.t += 2 * NS
        eng.step()
        eng.step()
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.rule(r["rule_id"])["status"], "triggered")

    def test_failed_notification_is_retried_and_stays_visible(self):
        self.notes.result = "failed"
        self.add(kind="price_cross", key="AAPL.XNAS", level="100")
        self.make_engine().step()
        self.push(quote(last="99"))
        self.push(quote(last="101"))
        self.assertEqual(self.store.events()[0]["notify_status"], "retrying")
        for _ in range(4):
            self.clock.t += 31 * NS
            self.engine.step()
        self.assertEqual(self.store.events()[0]["notify_status"], "failed")
        self.assertEqual(len(self.notes.sent), 4)


class NewsAlertTests(EngineCase):
    def item(self, news, guid, cik, *, form=None, first_seen=None):
        news.add("sec-filings", "filing" if form else "news",
                 FeedItem(guid=guid, title=f"title {guid}", link=f"https://www.sec.gov/{guid}", form=form,
                          accession=guid if form else None),
                 first_seen_ns=first_seen or self.clock.t, fetch_id=None,
                 entities=[{"scheme": "SEC_CIK", "value": cik, "role": "filer", "method": "test", "confidence": 1.0}])

    def test_filing_alert_carries_form_and_accession_and_matches_only_new_items(self):
        news = NewsStore(self.root / "news" / "news.sqlite")
        self.addCleanup(news.close)
        self.item(news, "0000320193-26-000001", 320193, form="8-K", first_seen=T0 - 10 * NS)   # before the rule
        r = self.add(kind="filing", issuer="320193", forms=["8-K"])
        self.make_engine()
        self.clock.t += 3 * NS
        self.engine.step()
        self.item(news, "0000320193-26-000002", 320193, form="8-K")
        self.item(news, "0000320193-26-000003", 320193, form="4")
        self.item(news, "headline-1", 320193)
        self.item(news, "0000999999-26-000004", 999999, form="8-K")
        self.clock.t += 3 * NS
        self.engine.step()
        inbox = self.store.events()
        self.assertEqual(len(inbox), 1)
        t = inbox[0]["trigger"]
        self.assertEqual((t["form"], t["accession"], t["issuers"]), ("8-K", "0000320193-26-000002", [320193]))
        self.assertEqual(inbox[0]["rule_id"], r["rule_id"])

    def test_missing_news_store_is_a_reason(self):
        r = self.add(kind="news", issuer="320193")
        self.make_engine().step()
        ev = self.store.rule(r["rule_id"])["evaluation"]
        self.assertEqual((ev["evaluating"], ev["reason"]), (False, "NO_SOURCE"))


class NotifierTests(unittest.TestCase):
    def test_text_is_passed_as_arguments_and_cleaned(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stderr=b"")
        with tempfile.TemporaryDirectory() as d:
            n = MacNotifier(Path(d), run=run, platform="darwin", which=lambda _: "/usr/bin/osascript")
            self.assertEqual(n.send('Evil" & do shell script "x', "line1\nline2\x07"), ("delivered", None))
            argv = calls[0]
            self.assertEqual(argv[:2], ["osascript", "-e"])
            self.assertTrue(all("Evil" not in a for a in argv[:7]))
            self.assertEqual(argv[-3:-1], ["line1 line2", 'Evil" & do shell script "x'])
            (Path(d) / "config").mkdir()
            (Path(d) / "config" / "alerts.json").write_text('{"macos_notifications": false}')
            self.assertEqual(n.send("t", "m")[0], "disabled")
        self.assertEqual(MacNotifier(Path("/x"), platform="linux").status()[0], "unavailable")

    def test_alerts_package_cannot_reach_orders(self):
        pkg = Path(rules.__file__).parent
        forbidden = ("broker", "core", "gateway", "service", "policy", "reconcile", "strategy", "ops", "api")
        for f in pkg.glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.ImportFrom) and node.level >= 2:
                    top = (node.module or "").split(".")[0]
                    self.assertNotIn(top, forbidden, f"{f.name} imports ..{node.module}")


class RouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = types.SimpleNamespace(paths=Paths(Path(tmp.name)))

    def post(self, *parts, **body):
        return alert_routes.post_alerts(self.ctx, ["alerts", *parts], body, None)

    def get(self, *parts, **q):
        out = alert_routes.get_alerts(self.ctx, ["alerts", *parts], q, "acct", None)
        json.dumps(out, allow_nan=False)
        return out

    def test_rules_inbox_ack_snooze_and_status(self):
        self.assertEqual(self.get("rules"), {"rules": [], "version": 0})
        self.assertEqual(self.get("status")["engine_reason"], "NO_DATA")
        r = self.post("rules", rule={"kind": "price_cross", "name": "AAPL", "key": "AAPL.XNAS", "level": "200"})
        with self.assertRaises(ValueError):
            self.post("rules", rule={"kind": "price_cross", "name": "AAPL", "key": "AAPL.XNAS"})
        rid = r["rule_id"]
        self.assertEqual(self.post("rules", rid, "snooze", minutes=30)["rule_id"], rid)
        with self.assertRaises(ValueError):
            self.post("rules", rid, "snooze", minutes=0)
        self.assertEqual(self.post("rules", rid, "pause")["status"], "paused")
        self.assertEqual(self.post("rules", rid, "resume")["status"], "active")
        store = AlertStore(store_path(self.ctx.paths.root))
        store.fire(store.rule(rid), {"observed": "201"}, title="AAPL", now_ns=T0, retire=False)
        store.close()
        self.assertEqual(self.get("inbox")["unread"], 1)
        self.assertEqual(self.post("inbox", "ack", event_ids=[1])["unread"], 0)
        with self.assertRaises(ValueError):
            self.post("inbox", "ack", event_ids=["1"])
        self.assertEqual(self.post("rules", rid, "delete")["status"], "deleted")
        with self.assertRaises(LookupError):
            self.post("rules", rid, "resume")
        actions = [a["action"] for a in self.get("audit")["entries"]]
        self.assertEqual(actions[::-1][:2], ["create", "snooze"])
        self.assertEqual(self.get("rules")["rules"], [])

    def test_stream_replays_after_the_last_event_id(self):
        store = AlertStore(store_path(self.ctx.paths.root))
        rule = store.create_rule(rules.validate({"kind": "time", "name": "t", "at": "2026-10-24T15:30:00Z"}), actor="owner")
        for i in range(3):
            store.fire(store.rule(rule["rule_id"]), {"observed": str(i)}, title="t", now_ns=T0 + i, retire=False)
        store.close()
        out, stop = [], threading.Event()

        def write(b):
            out.append(b.decode())
            if any("event: alert" in x for x in out):
                stop.set()
        run = alert_routes.stream(self.ctx, {})
        run(1, write, lambda: None, stop)
        text = "".join(out)
        self.assertIn("event: status", text)
        self.assertEqual(text.count("event: alert"), 2)
        self.assertIn("id: 3", text)
        self.assertNotIn("id: 1\n", text)


if __name__ == "__main__":
    unittest.main()
