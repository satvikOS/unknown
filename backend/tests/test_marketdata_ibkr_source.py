"""IBKR as a market-data bus source (broker/ibkr/mdsource.py): key -> contract from venues.json, delivered-type labels,
entitlement states, tick-by-tick trades and depth only when configured, and the session's new callback routing.
Account ids and prices are TEST FIXTURES; no TWS connection and no ibapi are needed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from unknown.broker.ibkr import marketdata as MD
from unknown.broker.ibkr.adapter import Sdk
from unknown.broker.ibkr.config import config_from_dict
from unknown.broker.ibkr.mdsource import IbkrSource, contract_for_key
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed
from unknown.marketdata import config as C


class FakeContract:
    def __init__(self):
        self.conId, self.symbol, self.secType, self.exchange, self.primaryExchange, self.currency = 0, "", "", "", "", ""


class FakeSession:
    def __init__(self):
        self.listeners, self.handlers, self.requests, self.subs, self.cancelled = [], {}, [], [], []
        self.sdk = SimpleNamespace(Contract=FakeContract)
        self.lines = SimpleNamespace(capacity=100)
        self._req = 100

    def add_quote_listener(self, fn):
        self.listeners.append(fn)

    def subscribe(self, key, spec):
        self.subs.append((key, spec))
        return MD.SubscribeResult(True, key, 1, "requested; status follows marketDataType")

    def unsubscribe(self, key):
        self.cancelled.append(key)
        return True

    def new_req_id(self):
        self._req += 1
        return self._req

    def register(self, req_id, handler):
        self.handlers[req_id] = handler

    def unregister(self, req_id):
        self.handlers.pop(req_id, None)

    def request(self, cls, method, *args):
        self.requests.append((method, *args))
        return None


class Rec:
    def __init__(self):
        self.ev = []

    def quote(self, sid, key, fields, **kw):
        self.ev.append(("quote", key, fields, kw))

    def trade(self, sid, key, **kw):
        self.ev.append(("trade", key, kw))

    def depth(self, sid, key, **kw):
        self.ev.append(("depth", key, kw))

    def status(self, sid, key, state, reason):
        self.ev.append(("status", key, state, reason))


def info(kind="live", **over):
    d = {"instrument_id": "AAPL.XNAS", "fields": {"bid": "10.01", "ask": "10.03", "close": "9.90", "volume": "1200"},
         "kind": kind, "live": kind == "live", "blank": False, "stale": None, "unavailable": None, "active": True,
         "currency": "USD", "receipt_ns": 5}
    d.update(over)
    return d


class IbkrSourceTests(unittest.TestCase):
    def setUp(self):
        self.session, self.rec = FakeSession(), Rec()

    def source(self, **kw) -> IbkrSource:
        src = IbkrSource(self.session, lines=kw.pop("lines", 40), venue_lookup=C.venue, run_thread=False, **kw)
        src.start(self.rec)
        return src

    def test_contract_from_the_venue_registry(self):
        spec = contract_for_key("AAPL.XNAS", C.venue)
        self.assertEqual((spec.symbol, spec.primary_exchange, spec.currency, spec.exchange), ("AAPL", "NASDAQ", "USD",
                                                                                               "SMART"))
        self.assertEqual(contract_for_key("SPY.ARCX", C.venue).primary_exchange, "ARCA")
        self.assertEqual(contract_for_key("SAP.XETR", C.venue).currency, "EUR")
        self.assertIn("no IBKR exchange code for XBOM", contract_for_key("RELIANCE.XBOM", C.venue))
        self.assertIn("SYMBOL", contract_for_key("FIX-INSTR-1", C.venue))

    def test_delivered_type_labels_and_entitlement_states(self):
        src = self.source()
        self.assertTrue(src.subscribe("AAPL.XNAS").ok)
        listener = self.session.listeners[0]
        for kind, expected in (("live", "REALTIME"), ("delayed", "DELAYED"), ("delayed_frozen", "DELAYED"),
                               ("frozen", "FROZEN")):
            listener(info(kind))
            ev = self.rec.ev[-1]
            self.assertEqual((ev[0], ev[3]["data_type"], ev[3]["replace"]), ("quote", expected, True))
        self.assertEqual(self.rec.ev[0][2]["prev_close"], "9.90")
        n = len(self.rec.ev)
        listener(info("unreported"))
        self.assertEqual(len(self.rec.ev), n)               # nothing is published before IBKR reports the type
        listener(info(unavailable={"code": 354, "state": "NOT_SUBSCRIBED", "cause": "fixture"}))
        self.assertEqual(self.rec.ev[-1][2], "NOT_ENTITLED")
        listener(info(blank=True, stale="IBKR 1100 connectivity lost"))
        self.assertEqual(self.rec.ev[-1][2], "STALE")
        listener(info(instrument_id="MSFT.XNAS"))            # not this source's key
        self.assertEqual(self.rec.ev[-1][2], "STALE")

    def test_no_trades_or_depth_unless_configured(self):
        src = self.source()
        self.assertFalse(src.capabilities.trades or src.capabilities.depth)
        src.subscribe("AAPL.XNAS")
        self.assertEqual(self.session.requests, [])

    def test_tick_by_tick_within_five_percent_of_lines(self):
        src = self.source(lines=40, tick_by_tick=True)
        self.assertEqual(src.tbt_budget, 2)
        for k in ("AAPL.XNAS", "MSFT.XNAS", "NVDA.XNAS"):
            out = src.subscribe(k)
        self.assertIn("tick-by-tick budget 2 in use", out.reason)
        self.assertEqual([r[0] for r in self.session.requests], ["reqTickByTickData"] * 2)
        self.assertEqual(self.session.requests[0][3:], ("AllLast", 0, False))
        handler = self.session.handlers[self.session.requests[0][1]]
        handler.on_tick_by_tick(1_700_000_000, 187.25, Fixed.parse("100"), SimpleNamespace(pastLimit=False,
                                                                                         unreported=False),
                                "NASDAQ", "")
        trade = self.rec.ev[-1]
        self.assertEqual((trade[0], trade[2]["price"], trade[2]["size"], trade[2]["ts_source_ns"]),
                         ("trade", "187.25", "100", 1_700_000_000 * 10**9))
        handler.on_error(10089, "fixture")
        self.assertEqual(self.rec.ev[-1][2], "STALE")
        self.assertIn("not entitled", self.rec.ev[-1][3])
        src.unsubscribe("AAPL.XNAS")
        self.assertIn("AAPL.XNAS", self.session.cancelled)

    def test_depth_book_operations_and_reset(self):
        with self.assertRaises(ValueError):
            IbkrSource(self.session, lines=10, venue_lookup=C.venue, depth=True)
        src = self.source(depth=True, depth_lines=1, depth_rows=5)
        src.subscribe("AAPL.XNAS")
        method, req_id, contract, rows, smart, opts = self.session.requests[0]
        self.assertEqual((method, rows, smart, opts), ("reqMktDepth", 5, True, []))
        h = self.session.handlers[req_id]
        h.on_depth(0, None, 0, 1, 10.01, Fixed.parse("300"))
        h.on_depth(1, "MMX", 0, 1, 10.00, Fixed.parse("200"))
        h.on_depth(0, None, 1, 1, 10.01, Fixed.parse("100"))
        h.on_depth(1, None, 2, 1, 0, 0)
        last = self.rec.ev[-1][2]
        self.assertEqual((last["side"], last["levels"]), ("bid", [{"price": "10.01", "size": "100", "mm": None}]))
        h.on_error(317, "Market depth data has been RESET")
        self.assertEqual([e[2]["levels"] for e in self.rec.ev[-2:]], [[], []])
        out = src.subscribe("MSFT.XNAS")
        self.assertIn("depth subscriptions in use", out.reason)


class SessionRoutingTests(unittest.TestCase):
    def test_wrapper_routes_tick_by_tick_and_depth_to_registered_handlers(self):
        session = object.__new__(MD.IbkrMarketDataSession)
        import threading
        session.cv = threading.Condition(threading.RLock())
        got = []
        session._handlers = {7: SimpleNamespace(on_tick_by_tick=lambda *a: got.append(("tbt", a)),
                                                on_depth=lambda *a: got.append(("depth", a)))}
        wrapper = MD._md_wrapper_class(object)(session)
        wrapper.tickByTickAllLast(7, 1, 1_700_000_000, 10.5, 100, None, "ARCA", "")
        wrapper.updateMktDepth(7, 0, 0, 1, 10.4, 300)
        wrapper.updateMktDepthL2(7, 1, "MMX", 0, 0, 10.6, 200, True)
        self.assertEqual(got, [("tbt", (1_700_000_000, 10.5, 100, None, "ARCA", "")),
                               ("depth", (0, None, 0, 1, 10.4, 300)), ("depth", (1, "MMX", 0, 0, 10.6, 200))])

    def test_publish_notifies_quote_listeners_with_full_field_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_from_dict({"host": "127.0.0.1", "port": 4002, "client_id": 17,
                                    "expected_account_id": "DUFIXTURE1", "mode": "PAPER",
                                    "host_time_zone": "America/New_York", "state_dir": str(Path(tmp)),
                                    "market_data_lines": 100})
            md = MD.market_data_config_from_dict({"client_id": 18})
            sdk = Sdk(EClient=None, EWrapper=object, Contract=FakeContract, Order=None, OrderCancel=None,
                      ExecutionFilter=None, version="pure-fake", path="", fingerprint="")
            session = MD.IbkrMarketDataSession(cfg, md, sdk=sdk, clock=ManualClock(utc_ns(2026, 10, 22, 14, 30)))
            seen = []
            session.add_quote_listener(seen.append)
            sub = MD._Subscription(1, "AAPL.XNAS", MD.ContractSpec(symbol="AAPL", primary_exchange="NASDAQ"),
                                   MD.UNKNOWN_FEED, 1, 0)
            sub.delivered_type = 1
            sub.live_fields["bid"] = MD.FieldValue(Fixed.parse("10.01"), "10.01", 0)
            with session.cv:
                session._publish(sub)
        self.assertEqual(seen[0]["fields"], {"bid": "10.01"})
        self.assertEqual((seen[0]["instrument_id"], seen[0]["blank"], seen[0]["currency"]), ("AAPL.XNAS", False, "USD"))


if __name__ == "__main__":
    unittest.main()
