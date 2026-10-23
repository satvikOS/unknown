"""Market-data bus (ADR-0018, docs/design/marketdata-stream-contract.md): events, config and routing, SYNTHETIC
source, coalescing, trade overflow, line budgets, fallback, tick store and the API endpoints. All data is SYNTHETIC or
a test fixture."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unknown.api.auth import write_owner_record
from unknown.api.server import ApiContext, make_server
from unknown.marketdata import config as C
from unknown.marketdata.bus import MarketDataBus
from unknown.marketdata.events import InvalidKey, dec, ns, parse_key, quote_event
from unknown.marketdata.source import Capabilities, MarketDataSource, Outcome, UnavailableSource
from unknown.marketdata.sse import parse_keys
from unknown.marketdata.store import TickStore
from unknown.marketdata.synthetic import SyntheticSource
from unknown.service.paths import Paths

PASSPHRASE = "market data bus tests passphrase"


class FakeSource(MarketDataSource):
    """Test fixture source: subscriptions are recorded; data is pushed by the test."""

    def __init__(self, sid: str, budget: int | None = None, refuse: dict | None = None):
        self.source_id = sid
        self.budget = budget
        self.refuse = refuse or {}
        self.capabilities = Capabilities(quotes=True, trades=True)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def line_budget(self):
        return self.budget

    def subscribe(self, key):
        if key in self.refuse:
            return Outcome(False, *self.refuse[key])
        self.subscribed.append(key)
        return Outcome(True, "SUBSCRIBED", "fixture")

    def unsubscribe(self, key):
        self.unsubscribed.append(key)


def make_bus(sources: dict, routes=None, trade_buffer=8192, store=None) -> MarketDataBus:
    cfg = C.BusConfig({sid: {} for sid in sources},
                      tuple(C.Route(m, tuple(s)) for m, s in (routes or [("*", list(sources))])), "test", trade_buffer)
    bus = MarketDataBus(cfg, sources, store=store)
    bus.start()
    return bus


def drain_all(client, wait=0.3) -> list:
    out, end = [], time.monotonic() + wait
    while time.monotonic() < end:
        out += client.drain(0.02)
    return out


class EventTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(parse_key("AAPL.XNAS"), ("AAPL", "XNAS"))
        self.assertEqual(parse_key("SPY.ARCX"), ("SPY", "ARCX"))
        self.assertEqual(parse_key("BRK.B.XNYS"), ("BRK.B", "XNYS"))
        self.assertEqual(parse_key("FIX-INSTR-1"), ("FIX-INSTR-1", None))
        for bad in ("", "a b", "x" * 65, "AAPL.XNAS;drop"):
            with self.assertRaises(InvalidKey):
                parse_key(bad)
        keys, bad = parse_keys("AAPL.XNAS, AAPL.XNAS,,bad key,MSFT.XNAS")
        self.assertEqual(keys, ["AAPL.XNAS", "MSFT.XNAS"])
        self.assertEqual([b["state"] for b in bad], ["UNAVAILABLE"])

    def test_decimals_are_strings_and_never_floats(self):
        self.assertEqual(dec("187.2300"), "187.2300")
        self.assertIsNone(dec(None))
        with self.assertRaises(TypeError):
            dec(187.23)
        with self.assertRaises(ValueError):
            dec("NaN")
        self.assertEqual(ns(5), 5)
        self.assertEqual(ns(2 ** 53 + 1), str(2 ** 53 + 1))

    def test_quote_has_every_contract_field_and_nulls_for_missing(self):
        ev = quote_event("AAPL.XNAS", 1, {"bid": "1.00"}, currency="USD", source="SYNTHETIC", data_type="SYNTHETIC",
                         feed="synthetic", size_unit="shares", ts_source_ns=None, ts_recv_ns=1, ts_pub_ns=2)
        contract = {"key", "seq", "bid", "ask", "bid_size", "ask_size", "last", "last_size", "volume", "open", "high",
                    "low", "prev_close", "currency", "source", "data_type", "ts_source_ns", "ts_recv_ns", "ts_pub_ns"}
        self.assertLessEqual(contract, set(ev))
        self.assertIsNone(ev["ask"])
        self.assertIsNone(ev["ts_source_ns"])


class ConfigTests(unittest.TestCase):
    def test_default_is_synthetic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = C.load_config(Path(tmp))
        self.assertEqual(list(cfg.sources), ["SYNTHETIC"])
        self.assertEqual(cfg.sources_for("AAPL.XNAS"), ("SYNTHETIC",))
        self.assertTrue(cfg.origin.startswith("default"))

    def test_synthetic_is_refused_alongside_a_real_source(self):
        with self.assertRaisesRegex(C.MarketDataConfigError, "never used when a real source"):
            C.config_from_dict({"sources": {"SYNTHETIC": {}, "IBKR": {"lines": 5}}})

    def test_routes_choose_sources_in_fallback_order(self):
        cfg = C.config_from_dict({"sources": {"IBKR": {}, "ALPACA": {}},
                                  "routes": [{"match": "*.XNAS", "sources": ["ALPACA", "IBKR"]},
                                             {"match": "*", "sources": ["IBKR"]}]})
        self.assertEqual(cfg.sources_for("AAPL.XNAS"), ("ALPACA", "IBKR"))
        self.assertEqual(cfg.sources_for("JPM.XNYS"), ("IBKR",))
        with self.assertRaisesRegex(C.MarketDataConfigError, "undeclared"):
            C.config_from_dict({"sources": {"IBKR": {}}, "routes": [{"match": "*", "sources": ["X"]}]})

    def test_a_source_that_cannot_be_built_stays_visible(self):
        cfg = C.config_from_dict({"sources": {"BROKEN": {"factory": "unknown.marketdata.nope:build"}}})
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("unknown.marketdata", "WARNING"):
            sources = C.build_sources(cfg, Path(tmp))
        self.assertIsInstance(sources["BROKEN"], UnavailableSource)
        self.assertEqual(sources["BROKEN"].subscribe("AAPL.XNAS").state, "UNAVAILABLE")
        self.assertEqual(sources["BROKEN"].describe()["state"], "unavailable")


class SyntheticTests(unittest.TestCase):
    def test_deterministic_and_labelled(self):
        a, b = SyntheticSource(run_thread=False), SyntheticSource(run_thread=False)
        for src in (a, b):
            src.subscribe("AAPL.XNAS")
        seq_a = [a.step(a._keys["AAPL.XNAS"]) for _ in range(20)]
        seq_b = [b.step(b._keys["AAPL.XNAS"]) for _ in range(20)]
        self.assertEqual([s.quote for s in seq_a], [s.quote for s in seq_b])
        self.assertEqual([s.trade for s in seq_a], [s.trade for s in seq_b])
        q = seq_a[0].quote
        from decimal import Decimal
        self.assertLess(Decimal(q["bid"]), Decimal(q["ask"]))
        self.assertEqual(seq_a[0].trade["conditions"], ["SYNTHETIC"])
        self.assertEqual(a.data_type, "SYNTHETIC")
        self.assertNotEqual(a.base_cents("AAPL.XNAS"), a.base_cents("MSFT.XNAS"))

    def test_events_through_the_bus_carry_the_synthetic_label(self):
        src = SyntheticSource(run_thread=False, currency_for=C.currency_for)
        bus = make_bus({"SYNTHETIC": src})
        client = bus.open_client(["AAPL.XNAS"])
        src.emit(src._keys["AAPL.XNAS"])
        items = drain_all(client, 0.1)
        kinds = [n for n, _ in items]
        self.assertIn("quote", kinds)
        self.assertIn("trade", kinds)
        self.assertIn("depth", kinds)
        for name, ev in items:
            if name != "status":
                self.assertEqual((ev["source"], ev["data_type"]), ("SYNTHETIC", "SYNTHETIC"))
        quote = next(ev for n, ev in items if n == "quote")
        self.assertEqual(quote["currency"], "USD")
        bus.stop()


class BusTests(unittest.TestCase):
    def test_quotes_coalesce_per_key_to_the_latest_state(self):
        src = FakeSource("FIXTURE")
        bus = make_bus({"FIXTURE": src})
        client = bus.open_client(["AAPL.XNAS"])
        for i in range(10):
            bus.quote("FIXTURE", "AAPL.XNAS", {"bid": f"10.0{i}", "ask": "10.10"}, data_type="REALTIME",
                      ts_recv_ns=time.time_ns())
        quotes = [ev for n, ev in drain_all(client, 0.2) if n == "quote"]
        self.assertEqual(len(quotes), 2)                    # the first at once, the latest when the window ends
        self.assertEqual(quotes[0]["bid"], "10.00")
        self.assertEqual(quotes[-1]["bid"], "10.09")
        self.assertEqual([q["seq"] for q in quotes], [1, 2])
        self.assertGreaterEqual(int(quotes[1]["ts_pub_ns"]) - int(quotes[0]["ts_pub_ns"]), 45_000_000)
        self.assertEqual(bus.counters["quotes_coalesced"], 8)
        bus.stop()

    def test_trades_are_never_coalesced_and_overflow_is_reported(self):
        store_dir = tempfile.TemporaryDirectory()
        self.addCleanup(store_dir.cleanup)
        store = TickStore(Path(store_dir.name) / "ticks.sqlite")
        src = FakeSource("FIXTURE")
        bus = make_bus({"FIXTURE": src}, trade_buffer=64, store=store)
        client = bus.open_client(["AAPL.XNAS"])
        client.drain(0.01)
        for i in range(100):
            bus.trade("FIXTURE", "AAPL.XNAS", price="10.00", size=str(i + 1), exchange="X", conditions=[],
                      data_type="REALTIME", ts_recv_ns=time.time_ns())
        items = drain_all(client, 0.1)
        trades = [ev for n, ev in items if n == "trade"]
        status = [ev for n, ev in items if n == "status"]
        self.assertEqual(len(trades), 64)
        self.assertEqual([t["seq"] for t in trades], list(range(1, 65)))
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["state"], "STALE")
        self.assertIn("36 trade(s) not delivered", status[0]["reason"])
        self.assertEqual(bus.counters["trades_dropped"], 36)
        store.flush()
        history = store.trades("AAPL.XNAS", limit=1000)
        self.assertEqual(len(history), 100)                 # the store has every trade, dropped ones included
        bus.stop()

    def test_line_budget_refuses_with_a_status_per_key_and_falls_back(self):
        a, b = FakeSource("A", budget=2), FakeSource("B", budget=1)
        bus = make_bus({"A": a, "B": b}, routes=[("*", ["A", "B"])])
        client = bus.open_client(["K1.XNAS", "K2.XNAS", "K3.XNAS", "K4.XNAS"])
        items = drain_all(client, 0.05)
        by_key = {ev["key"]: ev for n, ev in items if n == "status"}
        self.assertEqual(a.subscribed, ["K1.XNAS", "K2.XNAS"])
        self.assertEqual(b.subscribed, ["K3.XNAS"])
        self.assertEqual(by_key["K3.XNAS"]["state"], "SUBSCRIBED")
        self.assertIn("B (REALTIME)", by_key["K3.XNAS"]["reason"])
        self.assertEqual(by_key["K4.XNAS"]["state"], "LINE_BUDGET")
        self.assertIn("A: 2 of 2 lines in use", by_key["K4.XNAS"]["reason"])
        bus.close_client(client)
        self.assertEqual(sorted(a.unsubscribed), ["K1.XNAS", "K2.XNAS"])
        bus.stop()

    def test_refusal_falls_back_and_every_event_names_its_source(self):
        a = FakeSource("A", refuse={"AAPL.XNAS": ("NOT_ENTITLED", "fixture: not entitled")})
        b = FakeSource("B")
        bus = make_bus({"A": a, "B": b}, routes=[("*", ["A", "B"])])
        client = bus.open_client(["AAPL.XNAS"])
        bus.quote("A", "AAPL.XNAS", {"bid": "1"}, data_type="REALTIME", ts_recv_ns=1)      # ignored: not its key
        bus.quote("B", "AAPL.XNAS", {"bid": "2"}, data_type="DELAYED", ts_recv_ns=time.time_ns())
        quotes = [ev for n, ev in drain_all(client, 0.05) if n == "quote"]
        self.assertEqual([(q["source"], q["data_type"], q["bid"]) for q in quotes], [("B", "DELAYED", "2")])
        self.assertEqual(bus.counters["ignored"], 1)
        bus.stop()

    def test_entitlement_error_after_subscribe_fails_over(self):
        a, b = FakeSource("A"), FakeSource("B")
        bus = make_bus({"A": a, "B": b}, routes=[("*", ["A", "B"])])
        client = bus.open_client(["AAPL.XNAS"])
        bus.status("A", "AAPL.XNAS", "NOT_ENTITLED", "fixture 354")
        deadline = time.monotonic() + 2
        while not b.subscribed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(b.subscribed, ["AAPL.XNAS"])
        statuses = [ev for n, ev in drain_all(client, 0.1) if n == "status"]
        self.assertEqual(statuses[-1]["state"], "SUBSCRIBED")
        self.assertIn("fell back from A", statuses[-1]["reason"])
        self.assertEqual(a.unsubscribed, ["AAPL.XNAS"])
        bus.stop()

    def test_shared_keys_are_ref_counted_and_snapshot_matches_the_stream(self):
        src = FakeSource("FIXTURE")
        bus = make_bus({"FIXTURE": src})
        c1 = bus.open_client(["AAPL.XNAS"])
        bus.quote("FIXTURE", "AAPL.XNAS", {"bid": "5.00", "ask": "5.02"}, data_type="REALTIME",
                  ts_recv_ns=time.time_ns(), currency="USD")
        c2 = bus.open_client(["AAPL.XNAS"])
        self.assertEqual(src.subscribed, ["AAPL.XNAS"])
        late = [ev for n, ev in drain_all(c2, 0.05) if n == "quote"]
        self.assertEqual(late[0]["bid"], "5.00")              # a late client gets the current state at once
        snap = bus.snapshot(["AAPL.XNAS", "MSFT.XNAS"])
        self.assertEqual(snap["quotes"], [late[0]])
        self.assertEqual(snap["status"][0]["key"], "MSFT.XNAS")
        bus.close_client(c1)
        self.assertEqual(src.unsubscribed, [])
        bus.close_client(c2)
        self.assertEqual(src.unsubscribed, ["AAPL.XNAS"])
        bus.stop()


class TickStoreTests(unittest.TestCase):
    def test_paging_and_bounded_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [10_000 * 1_000_000_000]
            store = TickStore(Path(tmp) / "t.sqlite", retention_s=100, max_rows_per_key=5, time_ns=lambda: now[0])
            for i in range(8):
                store.append({"key": "AAPL.XNAS", "seq": i + 1, "price": "1.00", "size": "1", "exchange": None,
                              "conditions": [], "ts_source_ns": None, "ts_recv_ns": str(now[0] - (8 - i) * 10**9),
                              "source": "SYNTHETIC", "data_type": "SYNTHETIC", "feed": "synthetic"})
            store.append({"key": "OLD.XNAS", "seq": 1, "price": "1.00", "size": "1", "exchange": None,
                          "conditions": [], "ts_source_ns": None, "ts_recv_ns": now[0] - 1000 * 10**9,
                          "source": "SYNTHETIC", "data_type": "SYNTHETIC", "feed": None})
            store.flush()
            page = store.trades("AAPL.XNAS", limit=3)
            self.assertEqual([r["seq"] for r in page], [8, 7, 6])
            older = store.trades("AAPL.XNAS", before_ns=int(page[-1]["ts_recv_ns"]), limit=3)
            self.assertEqual([r["seq"] for r in older], [5, 4, 3])
            self.assertEqual(store.prune(), 4)                # 1 too old + 3 beyond 5 rows per key
            self.assertEqual(len(store.trades("AAPL.XNAS")), 5)
            self.assertEqual(store.trades("OLD.XNAS"), [])
            self.assertEqual(page[0]["data_type"], "SYNTHETIC")
            store.close()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.paths = Paths(Path(cls.tmp.name) / "home")
        cls.paths.ensure()
        write_owner_record(cls.paths.secrets / "owner.json", PASSPHRASE)
        from unknown.store.db import connect
        connect(cls.paths.db("SIM-MD")).close()
        cls.paths.token("api", create=True)
        cls.ctx = ApiContext(cls.paths, "SIM-MD", None, None, False)
        cls.server = make_server(cls.ctx, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        if cls.ctx.marketdata is not None:
            cls.ctx.marketdata.stop()
        cls.tmp.cleanup()

    def request(self, path, cookie=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        resp = conn.getresponse()
        return resp, conn

    def login(self) -> str:
        resp, conn = self.request("/api/auth/login", method="POST", body={"passphrase": PASSPHRASE})
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        return resp.getheader("Set-Cookie").split(";")[0]

    def test_stream_requires_the_owner_session(self):
        resp, conn = self.request("/api/marketdata/stream?keys=AAPL.XNAS")
        self.assertEqual(resp.status, 401)
        conn.close()

    def test_stream_snapshot_latency_sources_and_trades(self):
        cookie = self.login()
        resp, conn = self.request("/api/marketdata/stream?keys=AAPL.XNAS,MSFT.XNAS,bad%20key", cookie)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        events, deadline = [], time.monotonic() + 5
        name = None
        while time.monotonic() < deadline and sum(1 for n, _ in events if n == "trade") < 2:
            line = resp.fp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[6:])))
        conn.close()
        names = {n for n, _ in events}
        self.assertLessEqual({"status", "quote", "trade"}, names)
        self.assertEqual(next(ev for n, ev in events if n == "status")["key"], "bad key")
        quote = next(ev for n, ev in events if n == "quote")
        self.assertEqual((quote["source"], quote["data_type"]), ("SYNTHETIC", "SYNTHETIC"))
        for path in ("/api/marketdata/snapshot?keys=AAPL.XNAS", "/api/marketdata/latency", "/api/marketdata/sources",
                     "/api/marketdata/trades?key=AAPL.XNAS"):
            r, c = self.request(path, cookie)
            body = json.loads(r.read())
            c.close()
            self.assertEqual(r.status, 200, (path, body))
            if path.endswith("latency"):
                self.assertEqual(set(body["stages"]), {"source_to_recv", "recv_to_pub", "pub_to_write"})
                self.assertGreater(body["stages"]["pub_to_write"]["count"], 0)
            if path.endswith("sources"):
                self.assertEqual([s["source"] for s in body["sources"]], ["SYNTHETIC"])
            if "trades" in path:
                self.assertIn("retention", body)


if __name__ == "__main__":
    unittest.main()
