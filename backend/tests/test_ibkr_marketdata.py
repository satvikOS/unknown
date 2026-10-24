"""Tests for IBKR market data, historical bars and contract resolution, run WITHOUT a network.

Most tests drive ``IbkrMarketDataSession`` through ``PureHost``, a plain-Python stand-in for the EClient
socket. It records every request and answers through the session's real wrapper callbacks, so these tests run on
any interpreter. ``SdkTests`` repeat the key flows with ``FakeHost``, a subclass of the official
``ibapi.client.EClient`` that overrides only the socket-facing methods, and real ibapi objects (TickAttrib,
BarData, ContractDetails, TagValue, PriceIncrement). They also check every EClient method and EWrapper callback
signature against the pinned SDK. They skip cleanly without ``ibapi``:

    PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unittest tests.test_ibkr_marketdata -v

Every symbol, conId, ISIN, account id, price and bar is a FIXTURE (synthetic), not market data. Passing these
tests is not evidence of IBKR compatibility (not paper-verified).
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from unknown.broker.ibkr import contracts as C
from unknown.broker.ibkr import history as H
from unknown.broker.ibkr import marketdata as MD
from unknown.broker.ibkr.adapter import Sdk
from unknown.broker.ibkr.config import config_from_dict
from unknown.broker.ibkr.pacing import PacingScheduler
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed
from unknown.domain.instruments import Instrument
from unknown.domain.money import Price
from unknown.market.quotes import QuoteBook
from unknown.service.paths import Paths

try:
    import ibapi  # noqa: F401  (official SDK from the owner's local installation; never vendored)
    from ibapi.client import EClient
    from ibapi.common import BarData, PriceIncrement, TickAttrib
    from ibapi.contract import Contract, ContractDetails
    from ibapi.tag_value import TagValue
    from ibapi.ticktype import TickTypeEnum
    from ibapi.wrapper import EWrapper
    HAVE_IBAPI = True
except ImportError:
    HAVE_IBAPI = False

SKIP_REASON = ("ibapi (official IBKR TWS API 10.51.01 Python client) is not importable in this interpreter; SDK tests "
               "skipped. Run: PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unittest tests.test_ibkr_marketdata")

F = Fixed.parse
SRC = Path(__file__).resolve().parents[1] / "src"
REPO = SRC.parents[1]
ACCOUNT = "DUFIXTURE1"                 # fixture paper account id (not a real account)
ORDER_CLIENT_ID, MD_CLIENT_ID = 17, 18
CON_A, CON_B = 900001, 900002          # fixture conIds
T0 = utc_ns(2026, 10, 22, 14, 30)      # Thursday 10:30 America/New_York (EDT); fixture time only
S = 1_000_000_000
FEED = {"consolidation": "non_consolidated",
        "label": "IBKR US Real-Time Non Consolidated Streaming Quotes (Cboe One + IEX)",
        "evidence": "CAP-DATA-10 (IBKR market data pricing page) and the owner's subscription screenshot",
        "observed_on": "2026-10-22"}
_LOG = logging.getLogger("unknown.broker.ibkr")


def setUpModule():
    _LOG.setLevel(logging.ERROR)


def base_config(state_dir, **over) -> dict:
    data = {"host": "127.0.0.1", "port": 4002, "client_id": ORDER_CLIENT_ID, "expected_account_id": ACCOUNT,
            "mode": "PAPER", "host_time_zone": "America/New_York", "state_dir": str(state_dir),
            "connect_timeout_s": 1.0, "request_timeout_s": 0.2, "pacing_wait_s": 0.01, "market_data_lines": 100}
    data.update(over)
    return data


def md_config(**over) -> dict:
    data = {"client_id": MD_CLIENT_ID, "feeds": {"STK:USD": dict(FEED)},
            "freshness_budgets_ms": {"display": 5000, "pre_trade": 2000}}
    data.update(over)
    return data


# Storage rights for bars a test stores as if fetched (unflagged): product routes never serve fixture-flagged rows.
TEST_RIGHTS = H.StorageRights(True, "TEST.ibkr.historical_bars", "TEST: stored by a unit test", "test double")

class FakeContract:
    """Stands in for ibapi.contract.Contract in the pure tests (same attribute names)."""

    def __init__(self):
        self.conId, self.symbol, self.secType, self.exchange = 0, "", "", ""
        self.primaryExchange, self.currency = "", ""


PURE_SDK = Sdk(EClient=None, EWrapper=object, Contract=FakeContract, Order=None, OrderCancel=None,
               ExecutionFilter=None, version="pure-fake", path="", fingerprint="")


def attrib(**kw):
    return SimpleNamespace(canAutoExecute=False, pastLimit=False, preOpen=False, **kw)


def host_class(base):
    """Socket stand-in. With ``base=EClient`` it is a real EClient whose socket methods are overridden."""

    class Host(base):
        def __init__(self, wrapper, *, accounts=ACCOUNT, handshake=True):
            if base is object:
                self.wrapper = wrapper
            else:
                base.__init__(self, wrapper)
            self.calls: list[tuple] = []
            self.accounts, self.handshake = accounts, handshake
            self.connected = False
            self.hooks: dict = {}

        def _hook(self, name, *args):
            self.calls.append((name, *args))
            fn = self.hooks.get(name)
            if fn is not None:
                fn(self, *args)

        def connect(self, host, port, clientId):
            self.calls.append(("connect", host, port, clientId))
            self.host, self.port, self.clientId = host, port, clientId
            self.connected = True
            if self.handshake:
                self.wrapper.connectAck()
                self.wrapper.managedAccounts(self.accounts)
                self.wrapper.nextValidId(1)

        def isConnected(self):
            return self.connected

        def run(self):
            return None

        def disconnect(self):
            self.calls.append(("disconnect",))
            if self.connected:
                self.connected = False
                self.wrapper.connectionClosed()

        def reqMarketDataType(self, marketDataType):
            self._hook("reqMarketDataType", marketDataType)

        def reqMktData(self, reqId, contract, genericTickList, snapshot, regulatorySnapshot, mktDataOptions):
            self._hook("reqMktData", reqId, contract, genericTickList, snapshot, regulatorySnapshot, mktDataOptions)

        def cancelMktData(self, reqId):
            self._hook("cancelMktData", reqId)

        def reqHistoricalData(self, reqId, contract, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH,
                              formatDate, keepUpToDate, chartOptions):
            self._hook("reqHistoricalData", reqId, contract, endDateTime, durationStr, barSizeSetting, whatToShow,
                       useRTH, formatDate, keepUpToDate, chartOptions)

        def cancelHistoricalData(self, reqId):
            self._hook("cancelHistoricalData", reqId)

        def reqContractDetails(self, reqId, contract):
            self._hook("reqContractDetails", reqId, contract)

        def reqMarketRule(self, marketRuleId):
            self._hook("reqMarketRule", marketRuleId)

        def names(self):
            return [c[0] for c in self.calls]

    return Host


PureHost = host_class(object)


class SessionCase(unittest.TestCase):
    host_cls = PureHost
    sdk = PURE_SDK

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clock = ManualClock(T0)
        self.book = QuoteBook()
        self.hosts: list = []
        self.session = self.make_session()

    def make_session(self, host_kwargs=None, md_over=None, **cfg_over):
        kwargs = dict(host_kwargs or {})

        def factory(wrapper):
            host = self.host_cls(wrapper, **kwargs)
            self.hosts.append(host)
            return host

        cfg = config_from_dict(base_config(self.home / "state", **cfg_over))
        md = MD.market_data_config_from_dict(md_config(**(md_over or {})))
        pacer = PacingScheduler(1000, 20, monotonic_ns=self.clock.monotonic_ns, sleep=lambda s: None)
        return MD.IbkrMarketDataSession(cfg, md, quote_book=self.book, clock=self.clock, client_factory=factory,
                                        sdk=self.sdk, pacer=pacer,
                                        status_path=Paths(self.home).run / MD.STATUS_FILE_NAME)

    @property
    def host(self):
        return self.hosts[-1]

    @property
    def w(self):
        return self.host.wrapper

    def connect(self):
        state = self.session.connect()
        self.assertTrue(state["verified_paper"], state)
        return state

    def subscribe(self, instrument="INS-FXA", con_id=CON_A):
        r = self.session.subscribe(instrument, MD.ContractSpec(con_id=con_id))
        self.assertTrue(r.ok, r.reason)
        return r.req_id


# =============================================================================== configuration and budgets
class ConfigTests(unittest.TestCase):
    def test_market_data_client_id_must_differ_from_the_order_client(self):
        cfg = config_from_dict(base_config("/nonexistent-md-state"))
        md = MD.market_data_config_from_dict(md_config(client_id=ORDER_CLIENT_ID))
        with self.assertRaisesRegex(MD.MarketDataConfigError, "differ"):
            md.validate(order_client_id=cfg.client_id)

    def test_consolidation_claims_need_evidence_and_known_values(self):
        with self.assertRaisesRegex(MD.MarketDataConfigError, "evidence"):
            MD.market_data_config_from_dict(md_config(feeds={"STK:USD": {**FEED, "evidence": ""}}))
        with self.assertRaisesRegex(MD.MarketDataConfigError, "consolidation"):
            MD.market_data_config_from_dict(md_config(feeds={"STK:USD": {**FEED, "consolidation": "SIP_NBBO"}}))
        with self.assertRaisesRegex(MD.MarketDataConfigError, "unknown configuration keys"):
            MD.market_data_config_from_dict(md_config(regulatory_snapshots=True))
        md = MD.market_data_config_from_dict(md_config(feeds={}))
        self.assertEqual(md.feed_for("STK", "USD").consolidation, "unknown")    # never 'consolidated' by default

    def test_owner_config_file_is_read_from_unknown_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(MD.MarketDataConfigError, "no market-data configuration"):
                MD.load_market_data_config(Path(tmp))
            (Path(tmp) / "config").mkdir()
            (Path(tmp) / "config" / "ibkr_marketdata.json").write_text(json.dumps(md_config()))
            self.assertEqual(MD.load_market_data_config(Path(tmp)).feed_for("STK", "USD").consolidation,
                             "non_consolidated")

    def test_split_host_budget_never_exceeds_r_max(self):
        for lines in (2, 3, 100, 101, 125, 400):
            cfg = config_from_dict(base_config("/nonexistent-md-state", market_data_lines=lines))
            for share in (1, 30, 50, 90):
                md = MD.market_data_config_from_dict(md_config(pacing_share_percent=share))
                if cfg.max_requests_per_second < 2:
                    with self.assertRaises(MD.MarketDataConfigError):
                        MD.split_host_budget(cfg, md)
                    continue
                orders, mdp = MD.split_host_budget(cfg, md)
                self.assertLessEqual(orders.max_per_second + mdp.max_per_second, lines // 2, (lines, share))
                self.assertGreaterEqual(min(orders.max_per_second, mdp.max_per_second), 1)

    def test_line_budget_headroom_and_refusal(self):
        lines = MD.LineBudget(100, 10)
        for i in range(90):
            self.assertTrue(lines.acquire(i))
        self.assertEqual((lines.capacity, lines.in_use, lines.headroom), (90, 90, 0))
        self.assertFalse(lines.acquire(999))
        self.assertTrue(lines.acquire(5))          # re-acquiring a held line is idempotent
        lines.release(5)
        self.assertEqual(lines.headroom, 1)
        with self.assertRaises(ValueError):
            MD.LineBudget(100, 100)


class ImportBoundaryTests(unittest.TestCase):
    def _run(self, code: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)

    def test_new_modules_never_import_ibapi(self):
        r = self._run("import sys; import unknown.broker.ibkr.marketdata, unknown.broker.ibkr.history, "
                      "unknown.broker.ibkr.contracts, unknown.api.routes.marketdata; print('ibapi' in sys.modules)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")

    def test_route_module_does_not_import_the_ibkr_package(self):
        text = (SRC / "unknown" / "api" / "routes" / "marketdata.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(from|import)\s+[\w.]*(ibapi|ibkr)\b", text, re.M))

    def test_route_constants_match_the_writers(self):
        from unknown.api.routes import marketdata as R
        self.assertEqual(R.STATUS_FILE_NAME, MD.STATUS_FILE_NAME)
        self.assertEqual(R.OPEN_END, H.OPEN_END)
        for table in ("mkt_bar", "mkt_bar_gap"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", H.BAR_SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS ibkr_contract_resolution (", C.CONTRACT_SCHEMA)


# =============================================================================== connection
class ConnectionTests(SessionCase):
    def test_paper_identity_is_verified_with_its_own_client_id(self):
        state = self.connect()
        self.assertEqual(self.host.calls[0], ("connect", "127.0.0.1", 4002, MD_CLIENT_ID))
        self.assertEqual(self.host.calls[1], ("reqMarketDataType", 1))
        self.assertNotIn("placeOrder", self.host.names())
        self.assertEqual(state["account"], "DU******E1")

    def test_unexpected_managed_accounts_latch_unsafe_and_send_nothing(self):
        session = self.make_session(host_kwargs={"accounts": "DUOTHER123"})
        state = session.connect()
        self.assertFalse(state["verified_paper"])
        self.assertIn("DISCONNECTED_UNSAFE", state["reason"])
        self.assertEqual(self.host.names(), ["connect", "disconnect"])
        r = session.subscribe("INS-FXA", MD.ContractSpec(con_id=CON_A))
        self.assertFalse(r.ok)
        self.assertIn("NOT_READY", r.reason)

    def test_live_profile_is_refused_before_any_socket(self):
        with self.assertRaises(Exception):
            self.make_session(mode="LIVE")
        with self.assertRaises(Exception):
            self.make_session(port=4001)

    def test_no_login_reports_auth_required(self):
        session = self.make_session(host_kwargs={"handshake": False})
        state = session.connect()
        self.assertFalse(state["verified_paper"])
        self.assertIn("AUTH_REQUIRED", state["reason"])


# =============================================================================== quotes
class QuoteTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.req = self.subscribe()

    def quote(self, instrument="INS-FXA"):
        return self.book.get(instrument)

    def two_sided(self, bid=101.25, ask=101.27, delayed=False):
        b, a = (66, 67) if delayed else (1, 2)
        self.w.tickPrice(self.req, b, bid, attrib())
        self.w.tickSize(self.req, 69 if delayed else 0, Decimal("300"))
        self.w.tickPrice(self.req, a, ask, attrib())
        self.w.tickSize(self.req, 70 if delayed else 3, Decimal("200"))

    def test_streaming_request_is_never_a_snapshot_or_paid_regulatory_snapshot(self):
        call = [c for c in self.host.calls if c[0] == "reqMktData"][0]
        _, req_id, contract, generic, snapshot, regulatory, options = call
        self.assertEqual((req_id, generic, snapshot, regulatory, options), (self.req, "", False, False, []))
        self.assertEqual((contract.conId, contract.secType, contract.exchange, contract.currency),
                         (CON_A, "STK", "SMART", "USD"))

    def test_live_quote_is_labelled_non_consolidated_and_feeds_the_book(self):
        self.w.marketDataType(self.req, 1)
        self.two_sided()
        q = self.quote()
        self.assertEqual((q.bid, q.ask, q.source, q.delayed, q.fixture, q.event_time_ns, q.receipt_time_ns),
                         (F("101.25"), F("101.27"), "ibkr:live:non_consolidated", False, False, None, T0))
        self.assertEqual(q.mid(), F("101.260"))
        s = self.session.subscription("INS-FXA")
        self.assertEqual((s["status"], s["live"], s["consolidation"], s["quote_state"]),
                         ("LIVE", True, "non_consolidated", "normal"))

    def test_every_market_data_type_maps_to_one_status_and_only_live_is_live(self):
        expected = {1: ("LIVE", "ibkr:live:non_consolidated", False),
                    2: ("FROZEN", "ibkr:frozen:non_consolidated", True),
                    3: ("DELAYED", "ibkr:delayed:unknown", True),
                    4: ("DELAYED_FROZEN", "ibkr:delayed_frozen:unknown", True)}
        for t, (status, source, delayed) in expected.items():
            with self.subTest(type=t):
                self.w.marketDataType(self.req, t)
                self.two_sided(delayed=t in (3, 4))
                q = self.quote()
                self.assertEqual((self.session.subscription("INS-FXA")["status"], q.source, q.delayed),
                                 (status, source, delayed))
                self.assertIsNotNone(q.bid)

    def test_type_is_taken_from_the_provider_not_the_request(self):
        self.two_sided()                                   # requested live, no marketDataType yet
        q = self.quote()
        self.assertEqual((q.source, q.delayed), ("ibkr:unreported:unknown", True))
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "TYPE_UNREPORTED")
        self.w.marketDataType(self.req, 1)                 # first report keeps the live-id values already received
        self.assertEqual((self.quote().bid, self.quote().delayed), (F("101.25"), False))

    def test_delayed_to_live_updates_without_restart_with_one_audit_entry_and_no_merging(self):
        self.w.marketDataType(self.req, 3)
        self.two_sided(99.0, 99.5, delayed=True)
        self.assertTrue(self.quote().delayed)
        before = len([a for a in self.session.audit() if a["kind"] == "MARKET_DATA_TYPE"])
        self.w.marketDataType(self.req, 1)
        changes = [a for a in self.session.audit() if a["kind"] == "MARKET_DATA_TYPE"]
        self.assertEqual(len(changes) - before, 1)
        self.assertIn("delayed -> live", changes[-1]["detail"])
        q = self.quote()
        self.assertEqual((q.bid, q.ask), (None, None))     # delayed values never carried into the live quote
        self.two_sided()
        self.assertEqual((self.quote().bid, self.quote().delayed), (F("101.25"), False))

    def test_delayed_ticks_under_a_live_report_are_never_live(self):
        self.w.marketDataType(self.req, 1)
        self.w.tickPrice(self.req, 66, 99.0, attrib())
        self.assertTrue(self.quote().delayed)
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "TYPE_MISMATCH")
        self.assertTrue(any(a["kind"] == "TYPE_MISMATCH" for a in self.session.anomalies()))

    def test_minus_one_or_zero_then_size_zero_is_null_no_data(self):
        self.w.marketDataType(self.req, 1)
        self.two_sided()
        for price in (-1.0, 0.0):
            with self.subTest(price=price):
                self.w.tickPrice(self.req, 1, price, attrib())
                fields = self.session.subscription("INS-FXA")["fields"]
                self.assertFalse(fields["bid"]["present"])
                self.assertIn("awaiting paired size", fields["bid"]["null_reason"])
                self.w.tickSize(self.req, 0, Decimal("0"))
                fields = self.session.subscription("INS-FXA")["fields"]
                self.assertEqual(fields["bid"]["null_reason"], MD.NO_DATA_REASON)
                self.assertIsNone(self.quote().bid)          # never a price of -1 or 0
                self.assertEqual(self.session.subscription("INS-FXA")["quote_state"], "one_sided")
                self.two_sided()

    def test_crossed_and_locked_quotes_are_kept_labelled_and_counted(self):
        self.w.marketDataType(self.req, 1)
        for i in range(5):
            self.two_sided(101.30, 101.25)        # crossed
            self.two_sided(101.25, 101.25)        # locked
            self.two_sided(101.25, 101.27)        # normal
        s = self.session.subscription("INS-FXA")
        self.assertEqual((s["quote_state_counts"]["crossed"], s["quote_state_counts"]["locked"]), (5, 5))
        self.two_sided(101.30, 101.25)
        self.assertEqual(self.quote().bid, F("101.3"))     # retained, not deleted
        self.assertIsNone(self.quote().mid())

    def test_entitlement_errors_are_specific_unavailable_states(self):
        cases = {354: "NOT_SUBSCRIBED", 10089: "API_SUBSCRIPTION_REQUIRED", 10186: "NOT_SUBSCRIBED_DELAYED_DISABLED",
                 10197: "COMPETING_SESSION"}
        for i, (code, state) in enumerate(cases.items()):
            with self.subTest(code=code):
                instrument = f"INS-E{i}"
                req = self.subscribe(instrument, CON_A + 10 + i)
                self.w.marketDataType(req, 1)
                self.w.tickPrice(req, 1, 50.0, attrib())
                self.w.error(req, 0, code, f"fixture text {code}", "")
                s = self.session.subscription(instrument)
                self.assertEqual((s["status"], s["unavailable"]["state"], s["live"]), ("UNAVAILABLE", state, False))
                self.assertEqual(s["unavailable"]["cause"], MD.ENTITLEMENT_ERRORS[code][1])
                q = self.book.get(instrument)
                self.assertEqual((q.bid, q.delayed), (None, True))
                self.assertTrue(q.source.startswith("ibkr:unavailable:"))
        self.assertIn("live session preference", MD.ENTITLEMENT_ERRORS[10197][1])
        self.assertEqual(self.session.status_snapshot()["entitlement_errors"], 4)

    def test_terminal_entitlement_frees_its_line_on_maintain_and_10197_can_resume(self):
        in_use = self.session.lines.in_use
        req = self.subscribe("INS-E", CON_B)
        self.w.error(req, 0, 354, "Requested market data is not subscribed.", "")
        self.assertEqual(self.session.lines.in_use, in_use + 1)        # held until the cancel is sent
        out = self.session.maintain()
        self.assertEqual(out["cancelled"], ["INS-E"])
        self.assertIn(("cancelMktData", req), self.host.calls)
        self.assertEqual(self.session.lines.in_use, in_use)
        self.w.marketDataType(self.req, 1)
        self.w.error(self.req, 0, 10197, "No market data during competing session", "")
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "UNAVAILABLE")
        self.two_sided()
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "LIVE")
        self.assertTrue(any(a["kind"] == "DATA_RESUMED" for a in self.session.audit()))

    def test_partial_subscription_10090_is_a_warning_not_an_outage(self):
        self.w.marketDataType(self.req, 1)
        self.w.error(self.req, 0, 10090, "Part of requested market data is not subscribed.", "")
        self.two_sided()
        s = self.session.subscription("INS-FXA")
        self.assertEqual(s["status"], "LIVE")
        self.assertTrue(any("10090" in w for w in s["warnings"]))

    def test_line_budget_refuses_locally_before_error_101(self):
        session = self.make_session(market_data_lines=4, md_over={"tws_reserved_lines": 1})
        session.connect()
        oks = [session.subscribe(f"INS-L{i}", MD.ContractSpec(con_id=CON_A + 100 + i)) for i in range(4)]
        self.assertEqual([r.ok for r in oks], [True, True, True, False])
        self.assertIn("LINE_BUDGET_EXHAUSTED", oks[3].reason)
        self.assertEqual(self.host.names().count("reqMktData"), 3)
        self.assertEqual(session.status_snapshot()["line_budget"]["headroom"], 0)
        # a 101 anyway (e.g. TWS watchlist use) releases the line and is an anomaly
        self.host.wrapper.error(oks[0].req_id, 0, 101, "Max number of tickers has been reached.", "")
        self.assertEqual(session.status_snapshot()["line_budget"]["in_use"], 2)
        self.assertTrue(any(a["kind"] == "LINE_LIMIT_101" for a in session.anomalies()))

    def test_error_100_backs_off_and_alerts(self):
        self.w.error(-1, 0, 100, "Max rate of messages per second has been exceeded.", "")
        self.assertTrue(any(a["kind"] == "PACING_VIOLATION" for a in self.session.anomalies()))
        self.assertEqual(self.session.status_snapshot()["pacing"]["violations"], 1)

    def test_connectivity_loss_blanks_quotes_and_1101_requires_resubscribe(self):
        self.w.marketDataType(self.req, 1)
        self.two_sided()
        self.w.error(-1, 0, 1100, "Connectivity between IB and the TWS has been lost.", "")
        q = self.quote()
        self.assertEqual((q.bid, q.delayed, q.source), (None, True, "ibkr:stale:upstream_down"))
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "STALE")
        self.w.error(-1, 0, 1101, "Connectivity between IB and TWS has been restored- data lost.", "")
        self.assertTrue(self.session.subscription("INS-FXA")["resubscribe_required"])
        sent = self.host.names().count("reqMktData")
        self.assertEqual(self.session.maintain()["resubscribed"], ["INS-FXA"])
        self.assertEqual(self.host.names().count("reqMktData"), sent + 1)
        self.two_sided()
        self.assertFalse(self.quote().delayed)

    def test_data_farm_broken_is_stale_until_that_farm_is_ok(self):
        self.w.marketDataType(self.req, 1)
        self.two_sided()
        self.w.error(-1, 0, 2103, "Market data farm connection is broken:usfarm", "")
        self.assertTrue(self.quote().delayed)
        self.w.error(-1, 0, 2104, "Market data farm connection is OK:otherfarm", "")
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "STALE")
        self.w.error(-1, 0, 2104, "Market data farm connection is OK:usfarm", "")
        self.two_sided()
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "LIVE")

    def test_socket_loss_and_cancel(self):
        self.w.marketDataType(self.req, 1)
        self.two_sided()
        self.assertTrue(self.session.unsubscribe("INS-FXA"))
        self.assertIn(("cancelMktData", self.req), self.host.calls)
        self.assertEqual((self.quote().source, self.quote().delayed), ("ibkr:stale:cancelled", True))
        req = self.subscribe("INS-FXB", CON_B)
        self.w.marketDataType(req, 1)
        self.host.disconnect()
        self.assertTrue(self.book.get("INS-FXB").delayed)
        self.assertEqual(self.session.subscription("INS-FXB")["status"], "STALE")

    def test_symbol_without_primary_exchange_is_refused(self):
        r = self.session.subscribe("INS-X", MD.ContractSpec(symbol="FXA"))
        self.assertFalse(r.ok)
        self.assertIn("primary exchange", r.reason)
        r = self.session.subscribe("INS-X", MD.ContractSpec(symbol="FXA", primary_exchange="SMART"))
        self.assertFalse(r.ok)

    def test_status_file_has_statuses_but_no_prices(self):
        self.w.marketDataType(self.req, 1)
        self.two_sided(123.4567, 123.4589)
        path = self.session.maintain() and Paths(self.home).run / MD.STATUS_FILE_NAME
        text = path.read_text()
        self.assertNotIn("123.45", text)
        data = json.loads(text)
        self.assertEqual(data["subscriptions"][0]["status"], "LIVE")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_non_live_quotes_fail_the_real_pretrade_check(self):
        from unknown.risk import pretrade as P
        from unknown.policy.model import Eligibility
        elig = Eligibility(asset_classes=("EQUITY",), venues=("XNAS",), order_types=("LIMIT",), time_in_force=("DAY",),
                           outside_regular_hours=False, allow_short=False, allow_margin=False, allow_derivatives=False,
                           allow_leveraged_or_inverse=False, allow_fractional=False, allow_fx_conversion=False,
                           allow_transfers=False)
        known = dict(now_ns=T0, side="BUY", order_type="LIMIT", time_in_force="DAY", quantity=F("1"),
                     limit_price=F("101.26"), order_currency="USD", deadline_ns=T0 + S, intent_policy_hash="h",
                     account_mode="PAPER", stop_latched=False, resume_requires_owner=False,
                     reconciled_this_epoch=True, ownership_held=True, blocking_exception_kinds=(),
                     loss_budget_breached=None, session_state="RUNNING", mandate_policy_hash="h",
                     strategy_permitted=True, instrument=None, eligibility=elig, quote=None, quote_max_age_ms=2000,
                     price_collar_bps=100, required_cash=F("0"), available_cash=F("0"), unreserved_quantity=F("0"),
                     order_notional=F("0"), max_order_notional=F("0"), position_notional_after=F("0"),
                     session_equity=F("0"), max_position_bps=0, gross_exposure_after=F("0"),
                     max_gross_exposure=F("0"), orders_last_minute=0, max_orders_per_minute=0,
                     average_daily_volume=None, max_participation_bps_of_adv=0, require_liquidity_data=False)
        fields = dataclasses.fields(P.PretradeInput)
        missing = [f.name for f in fields if f.name not in known and f.default is dataclasses.MISSING
                   and f.default_factory is dataclasses.MISSING]
        # A skip here would silently drop the CAP-IBKR-12 acceptance proof ("delayed or frozen quotes fail the
        # pre-trade freshness check"), so a changed PretradeInput must fail loudly until this fixture is updated.
        self.assertEqual(missing, [], f"PretradeInput gained required fields {missing}: extend this fixture")
        results = {}
        for t in (1, 2, 3, 4):
            self.w.marketDataType(self.req, t)
            self.two_sided(delayed=t in (3, 4))
            inp = P.PretradeInput(**{**{k: v for k, v in known.items() if k in {f.name for f in fields}},
                                     "quote": self.quote()})
            checks = {c["check"]: c["passed"] for c in P.evaluate(inp).checks}
            results[t] = checks["QUOTE_NOT_DELAYED"]
        self.assertEqual(results, {1: True, 2: False, 3: False, 4: False})

    # ---------------------------------------------------------------- adversarial review regressions
    def test_values_from_before_an_interruption_are_never_republished_as_live(self):
        """FIXED: after 10197, 2103->2104 or 1100->1102 the pre-outage ask was kept, and the first new bid tick
        republished it as a live quote with a fresh receipt time."""
        texts = {10197: "No market data during competing session",
                 2103: "Market data farm connection is broken:usfarm",
                 1100: "Connectivity between IB and the TWS has been lost."}
        resume = {10197: lambda: None,
                  2103: lambda: self.w.error(-1, 0, 2104, "Market data farm connection is OK:usfarm", ""),
                  1100: lambda: self.w.error(-1, 0, 1102, "Connectivity restored - data maintained.", "")}
        for code, after in resume.items():
            with self.subTest(code=code):
                self.w.marketDataType(self.req, 1)
                self.two_sided()
                self.w.error(self.req if code == 10197 else -1, 0, code, texts[code], "")
                self.clock.advance(600 * S)
                after()
                self.w.tickPrice(self.req, 1, 99.0, attrib())
                q = self.quote()
                self.assertEqual((q.bid, q.ask, q.delayed), (F("99.0"), None, False))   # the 10-minute-old ask is gone
                self.assertEqual(self.session.subscription("INS-FXA")["quote_state"], "one_sided")
                self.assertTrue(any(a["kind"] == "FIELDS_INVALIDATED" and str(code) in a["detail"]
                                    for a in self.session.audit()))
                self.two_sided()

    def test_maintain_never_resubscribes_a_subscription_it_has_just_cancelled(self):
        """FIXED: a terminal entitlement error followed by a socket loss made maintain() cancel the subscription and
        then send reqMktData for it again."""
        req = self.subscribe("INS-E", CON_B)
        self.w.error(req, 0, 354, "Requested market data is not subscribed.", "")
        self.host.disconnect()
        self.connect()                                  # new connection, new host
        out = self.session.maintain()
        self.assertEqual(out["cancelled"], ["INS-E"])
        self.assertNotIn("INS-E", out["resubscribed"])
        self.assertEqual([c for c in self.host.calls if c[0] == "reqMktData" and c[1] == req], [])
        self.assertIn("INS-FXA", out["resubscribed"])   # a healthy subscription is still restored

    def test_entitlement_error_rate_never_invents_a_denominator(self):
        """FIXED: with no request sent the rate read '0/1'."""
        fresh = self.make_session().status_snapshot()["entitlement_error_rate"]
        self.assertEqual((fresh["value"], fresh["numerator"], fresh["denominator"]), (None, 0, 0))
        self.assertIn("missing_reason", fresh)
        rate = self.session.status_snapshot()["entitlement_error_rate"]
        self.assertEqual(rate["value"], f"0/{self.session.requests_sent}")
        self.assertGreater(self.session.requests_sent, 0)

    def test_a_resubscribed_instrument_reports_its_current_subscription(self):
        """FIXED: subscription() returned the oldest, cancelled record of a re-subscribed instrument."""
        self.assertTrue(self.session.unsubscribe("INS-FXA"))
        again = self.subscribe()
        s = self.session.subscription("INS-FXA")
        self.assertEqual((s["req_id"], s["current"]), (again, True))
        old = [x for x in self.session.status_snapshot()["subscriptions"] if x["req_id"] == self.req][0]
        self.assertEqual((old["status"], old["current"]), ("CANCELLED", False))

    def test_a_backwards_wall_clock_step_never_drops_a_blanking_publish(self):
        """FIXED: QuoteBook.put keeps the later receipt time, so after an NTP step back a 354 left the live quote in
        the book while the session itself reported UNAVAILABLE."""
        self.clock = StepBackClock(T0)
        self.session = self.make_session()
        self.connect()
        self.req = self.subscribe("INS-CLK", CON_B)
        self.w.marketDataType(self.req, 1)
        self.two_sided()
        self.assertFalse(self.book.get("INS-CLK").delayed)
        self.clock.step(-2 * S)                         # the wall clock steps back by 2 s
        self.w.error(self.req, 0, 354, "Requested market data is not subscribed.", "")
        q = self.book.get("INS-CLK")
        self.assertEqual((q.bid, q.ask, q.delayed, q.source), (None, None, True, "ibkr:unavailable:not_subscribed"))
        self.assertGreaterEqual(self.session.status_snapshot()["publish_clock_regressions"], 1)
        self.assertTrue(any(a["kind"] == "CLOCK_STEPPED_BACK" for a in self.session.anomalies()))


class StepBackClock:
    """A wall clock that can step backwards (NTP adjustment); ManualClock refuses to."""

    def __init__(self, start_ns: int):
        self.t, self.m = start_ns, 0

    def now_ns(self) -> int:
        return self.t

    def monotonic_ns(self) -> int:
        return self.m

    def advance(self, ns: int) -> None:
        self.step(ns)

    def step(self, ns: int) -> None:
        self.t += ns
        self.m += max(0, ns)


# =============================================================================== historical pacing
class HistoricalPacerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.pacer = H.HistoricalPacer(lambda: self.now)

    def go(self, ident, same=("K", "SMART", "TRADES"), weight=1, release=True):
        d = self.pacer.try_acquire(ident, same, weight)
        if d.ok and release:
            self.pacer.release()
        return d

    def test_identical_request_within_15_seconds_is_refused(self):
        self.assertTrue(self.go(("a",)).ok)
        self.now += 14 * S
        d = self.go(("a",))
        self.assertEqual((d.ok, d.rule, d.retry_after_ns), (False, "IDENTICAL_WITHIN_15S", 1 * S))
        self.now += 1 * S
        self.assertTrue(self.go(("a",)).ok)

    def test_six_same_key_within_two_seconds_is_refused_and_bid_ask_counts_twice(self):
        for i in range(5):
            self.assertTrue(self.go((i,)).ok)
        self.assertEqual(self.go((9,)).rule, "SIX_SAME_CONTRACT_EXCHANGE_TICKTYPE_WITHIN_2S")
        self.assertTrue(self.go((10,), same=("K", "SMART", "MIDPOINT")).ok)      # another tick type
        self.now += 2 * S
        ba = ("K", "SMART", "BID_ASK")
        self.assertTrue(self.go((20,), ba, 2).ok)
        self.assertTrue(self.go((21,), ba, 2).ok)
        self.assertFalse(self.go((22,), ba, 2).ok)                               # 6 weighted

    def test_the_61st_weighted_request_in_ten_minutes_is_refused(self):
        for i in range(60):
            self.assertTrue(self.go((i,), same=(i,)).ok)
            self.now += S
        d = self.go(("x",), same=("x",))
        self.assertEqual(d.rule, "MORE_THAN_60_IN_10_MINUTES")
        self.now += d.retry_after_ns
        self.assertTrue(self.go(("x",), same=("x",)).ok)

    def test_fifty_open_requests_at_most(self):
        for i in range(50):
            self.assertTrue(self.go((i,), same=(i,), release=False).ok)
        self.assertEqual(self.go(("y",), same=("y",)).rule, "MAX_50_OPEN_REQUESTS")
        self.pacer.release()
        self.assertTrue(self.go(("y",), same=("y",)).ok)

    def test_ten_minute_soak_against_a_mock_enforcing_ibkr_rules(self):
        """CAP-IBKR-08 / CAP-MKT-04: zero requests the rule-enforcing mock would reject."""
        granted = []                     # (t, identical, same, weight)
        violations = []
        keys = [("A", "SMART", "TRADES"), ("A", "SMART", "BID_ASK"), ("B", "SMART", "TRADES")]
        for step in range(6000):         # every 100 ms for 10 minutes
            self.now = step * S // 10
            same = keys[step % 3]
            ident = (same, step % 7)     # frequent identical repeats
            weight = 2 if same[2] == "BID_ASK" else 1
            if self.go(ident, same, weight).ok:
                t = self.now
                if any(g[1] == ident and t - g[0] < 15 * S for g in granted):
                    violations.append(("identical", t))
                if sum(g[3] for g in granted if g[2] == same and t - g[0] < 2 * S) + weight >= 6:
                    violations.append(("six-in-2s", t))
                if sum(g[3] for g in granted if t - g[0] < 600 * S) + weight > 60:
                    violations.append(("60-in-10min", t))
                granted.append((t, ident, same, weight))
        self.assertEqual(violations, [])
        self.assertGreaterEqual(sum(g[3] for g in granted), 55)


# =============================================================================== bars: parsing and the store
def bar_row(date_text, o=100.0, h=101.0, low=99.5, c=100.5, v="1000", wap="100.25", n=42):
    return {"date": date_text, "open": o, "high": h, "low": low, "close": c, "volume": Decimal(v),
            "wap": Decimal(wap), "barCount": n}


def epoch(ns):
    return str(ns // S)


XNYS = REPO / "config" / "calendars" / "XNYS.json"


class BarParsingTests(unittest.TestCase):
    def setUp(self):
        self.rule = H.SessionRule.from_calendar_file(XNYS)

    def req(self, **kw):
        base = dict(instrument_id="INS-FXA", contract=MD.ContractSpec(con_id=CON_A), bar_size="1 min",
                    duration="1800 S")
        base.update(kw)
        return H.HistoricalRequest(**base)

    def test_intraday_epoch_bar_end_availability_and_completeness(self):
        start = utc_ns(2026, 10, 22, 14, 29)
        b = H.parse_bar(bar_row(epoch(start)), self.req(), receipt_ns=T0, settle_delay_ns=2 * S, session_rule=None)
        self.assertEqual((b.bar_start_ns, b.bar_end_ns, b.available_ns, b.complete), (start, T0, T0 + 2 * S, True))
        partial = H.parse_bar(bar_row(epoch(T0)), self.req(), receipt_ns=T0 + 10 * S, settle_delay_ns=0,
                              session_rule=None)
        self.assertFalse(partial.complete)                      # end 14:31 after receipt 14:30:10
        self.assertEqual((b.open, b.volume, b.wap, b.bar_count), (F("100.0"), F("1000"), F("100.25"), 42))

    def test_daily_rth_bar_ends_at_the_calendar_close_including_early_closes(self):
        r = self.req(bar_size="1 day", duration="5 D")
        b = H.parse_bar(bar_row("20261021"), r, receipt_ns=T0, settle_delay_ns=0, session_rule=self.rule)
        self.assertEqual((b.bar_date, b.bar_end_ns, b.complete), ("2026-10-21", utc_ns(2026, 10, 21, 20), True))
        early = H.parse_bar(bar_row("20261127"), r, receipt_ns=utc_ns(2026, 11, 28), settle_delay_ns=0,
                            session_rule=self.rule)
        self.assertEqual(early.bar_end_ns, utc_ns(2026, 11, 27, 18))                 # 13:00 EST
        today = H.parse_bar(bar_row("20261022"), r, receipt_ns=T0, settle_delay_ns=0, session_rule=self.rule)
        self.assertFalse(today.complete)
        all_hours = H.parse_bar(bar_row("20261021"), self.req(bar_size="1 day", duration="5 D", use_rth=False),
                                receipt_ns=T0, settle_delay_ns=0, session_rule=None)
        self.assertEqual(all_hours.bar_end_ns, utc_ns(2026, 10, 22, 4))               # next local midnight (EDT)

    def test_unavailable_values_are_null_never_zero(self):
        b = H.parse_bar(bar_row(epoch(T0 - 60 * S), o=-1.0, v="-1", wap="-1", n=-1), self.req(what_to_show="MIDPOINT"),
                        receipt_ns=T0, settle_delay_ns=0, session_rule=None)
        self.assertEqual((b.open, b.volume, b.wap, b.bar_count), (None, None, None, None))
        self.assertIn("open_non_positive", b.quality)

    def test_request_validation(self):
        self.assertTrue(self.req(bar_size="1 week").problems())
        self.assertTrue(self.req(what_to_show="OPTION_IMPLIED_VOLATILITY").problems())
        self.assertTrue(self.req(duration="2 days").problems())
        self.assertTrue(self.req(what_to_show="ADJUSTED_LAST", end_ns=T0).problems())
        self.assertEqual(self.req(end_ns=T0).end_date_time(), "20261022 14:30:00 UTC")
        self.assertEqual(self.req().end_date_time(), "")
        self.assertEqual(self.req(what_to_show="BID_ASK").weight, 2)


class BarStoreTests(unittest.TestCase):
    """Point-in-time correctness (no look-ahead) and bitemporal revisions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = H.BarStore(Path(self.tmp.name) / "research.sqlite")
        self.addCleanup(self.store.close)
        self.rule = H.SessionRule.from_calendar_file(XNYS)
        self.req = H.HistoricalRequest("INS-FXA", MD.ContractSpec(con_id=CON_A), "1 day", "5 D")
        self.rights = H.StorageRights.fixture_only()

    def result(self, receipt, closes):
        res = H.HistoricalResult(self.req, f"req-{receipt}", "COMPLETE", "fixture", receipt_ns=receipt)
        for day, close in closes.items():
            res.bars.append(H.parse_bar(bar_row(day, c=close), self.req, receipt_ns=receipt, settle_delay_ns=0,
                                        session_rule=self.rule))
        return res

    def as_of(self, t, **kw):
        return self.store.as_of("INS-FXA", bar_size="1 day", what_to_show="TRADES", use_rth=True, t_ns=t,
                                include_fixture=True, **kw)

    def test_no_bar_is_visible_before_its_availability_time(self):
        counts = self.store.store(self.result(T0, {"20261020": 100.5, "20261021": 101.5, "20261022": 102.5}),
                                  self.rights)
        self.assertEqual(counts, {"new": 2, "unchanged": 0, "revised": 0, "incomplete_not_stored": 1})
        close_21 = utc_ns(2026, 10, 21, 20)
        self.assertEqual([r["bar_date"] for r in self.as_of(close_21 - 1)], ["2026-10-20"])
        self.assertEqual([r["bar_date"] for r in self.as_of(close_21)], ["2026-10-20", "2026-10-21"])
        self.assertEqual(self.as_of(utc_ns(2026, 10, 20, 19, 59)), [])
        rows = self.as_of(close_21)
        # TRADES is split-adjusted on the basis of its fetch time (T0), which is after this decision time
        self.assertTrue(all(r["adjustment"] == "split_adjusted" and r["adjustment_basis_after_decision"]
                            for r in rows))
        self.assertFalse(self.as_of(T0)[0]["adjustment_basis_after_decision"])
        with self.assertRaises(H.UnadjustedUnavailable):
            self.as_of(T0, require_unadjusted=True)

    def test_audit_mode_sees_only_what_the_system_held(self):
        self.store.store(self.result(T0, {"20261020": 100.5, "20261021": 101.5}), self.rights)
        t2 = T0 + 3600 * S
        counts = self.store.store(self.result(t2, {"20261020": 100.5, "20261021": 101.75}), self.rights)
        self.assertEqual((counts["unchanged"], counts["revised"]), (1, 1))
        later = utc_ns(2026, 10, 23)
        self.assertEqual([r["close"] for r in self.as_of(later)], ["100.5", "101.75"])
        audit = self.as_of(later, knowledge_cutoff_ns=T0 + 1)
        self.assertEqual([r["close"] for r in audit], ["100.5", "101.5"])
        self.assertEqual(self.as_of(later, knowledge_cutoff_ns=T0 - 1), [])
        revised = [r for r in self.as_of(later) if r["bar_date"] == "2026-10-21"][0]
        self.assertIsNotNone(revised["supersedes"])
        self.assertIn("revised", revised["quality"])

    def test_fixture_rows_are_excluded_by_default_and_storage_needs_rights(self):
        self.store.store(self.result(T0, {"20261020": 100.5}), self.rights)
        self.assertEqual(self.store.as_of("INS-FXA", bar_size="1 day", what_to_show="TRADES", use_rth=True,
                                          t_ns=utc_ns(2026, 10, 23)), [])
        self.assertTrue(all(r["fixture"] == 1 and r["dataset_id"] == H.FIXTURE_DATASET_ID
                            for r in self.as_of(utc_ns(2026, 10, 23))))
        with tempfile.TemporaryDirectory() as empty:
            refused = H.StorageRights.from_registry(Path(empty))
        self.assertFalse(refused.permitted)
        self.assertIn("UNAUTHORIZED", refused.reason)
        with self.assertRaises(H.BarStorageRefused):
            self.store.store(self.result(T0, {"20261021": 1.0}), refused)

    def test_registry_entry_granting_storage_opens_the_gate(self):
        provider = {
            "provider_id": "fixture-ibkr", "name": "FIXTURE registry entry (test only)", "access": "test",
            "cost_model": "n/a", "budget": "n/a", "rate_limit": {"max_requests": 1, "per_seconds": 1.0, "source": "x"},
            "failure_behavior": "n/a", "sources": ["test"], "verified_on": "2026-10-22",
            "datasets": [{"dataset_id": H.DATASET_ID, "description": "FIXTURE", "endpoint": "n/a", "coverage": "n/a",
                          "history_depth": "n/a", "update_frequency": "n/a", "availability_rule": "bar end",
                          "allowed_uses": ["display", "storage"], "vintage_support": False,
                          "rights_source": "FIXTURE", "rights_verified_on": "2026-10-22"}]}
        with tempfile.TemporaryDirectory() as reg:
            (Path(reg) / "fixture.json").write_text(json.dumps(provider))
            rights = H.StorageRights.from_registry(Path(reg))
            self.assertTrue(rights.permitted)
            provider["datasets"][0]["allowed_uses"] = ["display"]
            (Path(reg) / "fixture.json").write_text(json.dumps(provider))
            self.assertFalse(H.StorageRights.from_registry(Path(reg)).permitted)
        repo_rights = H.StorageRights.from_registry()      # the repository registry as it stands
        if repo_rights.permitted:
            self.assertIn("verified", repo_rights.evidence)
        else:
            self.assertIn("UNAUTHORIZED", repo_rights.reason)


# =============================================================================== history client (through the session)
class HistoryClientTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.store = H.BarStore(Path(self.tmp.name) / "research.sqlite")
        self.addCleanup(self.store.close)
        self.rule = H.SessionRule.from_calendar_file(XNYS)
        self.client = self.history()

    def history(self, rights=None, **kw):
        return H.HistoryClient(self.session, store=self.store,
                               rights=rights if rights is not None else H.StorageRights.fixture_only(),
                               session_rules={"America/New_York": self.rule}, **kw)

    def respond(self, rows, end=True, error=None):
        def hook(host, reqId, *args):
            if error:
                host.wrapper.error(reqId, 0, *error, "")
                return
            for r in rows:
                host.wrapper.historicalData(reqId, SimpleNamespace(**r))
            if end:
                host.wrapper.historicalDataEnd(reqId, "20261022 14:00:00", "20261022 14:30:00")
        self.host.hooks["reqHistoricalData"] = hook

    def intraday(self, **kw):
        base = dict(instrument_id="INS-FXA", contract=MD.ContractSpec(con_id=CON_A), bar_size="1 min",
                    duration="300 S", what_to_show="TRADES")
        base.update(kw)
        return H.HistoricalRequest(**base)

    def test_complete_fetch_stores_complete_bars_with_lineage(self):
        self.respond([bar_row(epoch(T0 - (5 - i) * 60 * S)) for i in range(5)] + [bar_row(epoch(T0))])
        res = self.client.fetch(self.intraday())
        self.assertEqual(res.outcome, "COMPLETE", res.detail)
        call = [c for c in self.host.calls if c[0] == "reqHistoricalData"][0]
        self.assertEqual(call[3:], ("", "300 S", "1 min", "TRADES", 1, 2, False, []))
        self.assertEqual((call[2].conId, call[2].exchange), (CON_A, "SMART"))
        self.assertEqual(res.stored, {"new": 5, "unchanged": 0, "revised": 0, "incomplete_not_stored": 1})
        rows = self.store.as_of("INS-FXA", bar_size="1 min", what_to_show="TRADES", use_rth=True, t_ns=T0,
                                include_fixture=True)
        self.assertEqual(len(rows), 5)
        r = rows[-1]
        self.assertEqual((r["bar_spec_version"], r["timezone"], r["use_rth"], r["what_to_show"], r["adjustment"]),
                         (H.BAR_SPEC_VERSION, "America/New_York", 1, "TRADES", "split_adjusted"))
        self.assertIn("available = end + settle 0 s", r["availability_rule"])
        req_row = self.store.conn.execute("SELECT * FROM mkt_bar_request").fetchone()
        self.assertEqual((req_row["outcome"], req_row["bars_received"], req_row["fixture"]), ("COMPLETE", 6, 1))

    def test_without_registry_rights_bars_are_returned_but_not_stored(self):
        with tempfile.TemporaryDirectory() as empty:
            client = self.history(rights=H.StorageRights.from_registry(Path(empty)))
        self.respond([bar_row(epoch(T0 - 120 * S))])
        res = client.fetch(self.intraday())
        self.assertEqual((res.outcome, len(res.bars), res.stored), ("COMPLETE", 1, None))
        self.assertTrue(res.not_stored_reason.startswith("UNAUTHORIZED"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mkt_bar").fetchone()[0], 0)
        self.assertIn("NOT_STORED", self.store.conn.execute("SELECT detail FROM mkt_bar_request").fetchone()[0])

    def test_identical_repeat_within_15_s_is_refused_locally(self):
        self.respond([bar_row(epoch(T0 - 120 * S))])
        self.assertEqual(self.client.fetch(self.intraday()).outcome, "COMPLETE")
        self.clock.advance(5 * S)
        res = self.client.fetch(self.intraday())
        self.assertEqual(res.outcome, "PACING_REFUSED")
        self.assertIn("IDENTICAL_WITHIN_15S", res.detail)
        self.assertEqual(res.retry_after_ns, 10 * S)
        self.assertEqual(self.host.names().count("reqHistoricalData"), 1)

    def test_small_bars_older_than_six_months_are_a_coverage_gap_not_an_empty_success(self):
        res = self.client.fetch(self.intraday(bar_size="1 sec", duration="1800 S", end_ns=utc_ns(2026, 3, 1)))
        self.assertEqual(res.outcome, "COVERAGE_GAP")
        self.assertNotIn("reqHistoricalData", self.host.names())
        gaps = self.store.gaps("INS-FXA")
        self.assertEqual((len(gaps), gaps[0]["reason"]), (1, "SMALL_BARS_OLDER_THAN_SIX_MONTHS"))
        recent = self.client.fetch(self.intraday(bar_size="1 sec", duration="60 S", end_ns=T0 - 3600 * S))
        self.assertNotEqual(recent.outcome, "COVERAGE_GAP")
        old_daily = self.client.fetch(self.intraday(bar_size="1 day", duration="1 Y", end_ns=utc_ns(2026, 1, 2)))
        self.assertNotEqual(old_daily.outcome, "COVERAGE_GAP")

    def test_hmds_errors_no_data_permissions_pacing_and_timeout(self):
        self.respond([], error=(162, "Historical Market Data Service error message:HMDS query returned no data"))
        res = self.client.fetch(self.intraday())
        self.assertEqual(res.outcome, "NO_DATA")
        self.assertEqual(self.store.gaps("INS-FXA")[-1]["reason"], "IBKR_NO_DATA")
        self.respond([], error=(162, "Historical Market Data Service error message:No market data permissions"))
        self.assertEqual(self.client.fetch(self.intraday(duration="600 S")).outcome, "UNAUTHORIZED")
        self.respond([], error=(162, "Historical Market Data Service error message:Historical data request pacing "
                                     "violation"))
        self.assertEqual(self.client.fetch(self.intraday(duration="900 S")).outcome, "PACING_VIOLATION")
        blocked = self.client.fetch(self.intraday(duration="1200 S"))
        self.assertEqual(blocked.outcome, "PACING_REFUSED")
        self.assertIn("HMDS_PACING_BACKOFF", blocked.detail)
        self.clock.advance(61 * S)
        self.respond([bar_row(epoch(T0 - 120 * S))], end=False)
        res = self.client.fetch(self.intraday(duration="1500 S"))
        self.assertEqual(res.outcome, "INCOMPLETE")
        self.assertEqual(self.host.names()[-1], "cancelHistoricalData")

    def test_warnings_carrying_the_request_id_do_not_fail_it_and_clients_share_one_pacer(self):
        def hook(host, reqId, *args):
            host.wrapper.error(reqId, 0, 2176, "fixture warning", "")
            host.wrapper.historicalData(reqId, SimpleNamespace(**bar_row(epoch(T0 - 120 * S))))
            host.wrapper.historicalDataEnd(reqId, "a", "b")
        self.host.hooks["reqHistoricalData"] = hook
        res = self.client.fetch(self.intraday())
        self.assertEqual((res.outcome, res.errors[0]["code"]), ("COMPLETE", 2176))
        other = self.history()
        self.assertIs(other.pacer, self.client.pacer)
        self.assertEqual(other.fetch(self.intraday()).outcome, "PACING_REFUSED")    # identical within 15 s

    def test_rth_daily_without_session_rule_is_refused(self):
        client = H.HistoryClient(self.session, store=self.store, rights=H.StorageRights.fixture_only())
        res = client.fetch(self.intraday(bar_size="1 day", duration="5 D"))
        self.assertEqual(res.outcome, "REFUSED")
        self.assertIn("session rule", res.detail)

    # ---------------------------------------------------------------- adversarial review regressions
    def test_a_bar_open_when_requested_is_never_complete_even_if_the_answer_arrives_after_its_end(self):
        """FIXED: completeness was judged on the local receipt after the wait, so a late (held) response turned the
        partial bar IBKR had snapshotted into a stored, complete bar."""
        def late(host, reqId, *args):
            host.wrapper.historicalData(reqId, SimpleNamespace(**bar_row(epoch(T0 - 90 * S))))   # ends T0 - 30 s
            host.wrapper.historicalData(reqId, SimpleNamespace(**bar_row(epoch(T0 - 30 * S))))   # open at T0
            self.clock.advance(45 * S)                  # IBKR answers 45 s after the request
            host.wrapper.historicalDataEnd(reqId, "a", "b")
        self.host.hooks["reqHistoricalData"] = late
        res = self.client.fetch(self.intraday(duration="120 S"))
        self.assertEqual(res.outcome, "COMPLETE", res.detail)
        self.assertEqual((res.sent_ns, res.receipt_ns), (T0, T0 + 45 * S))
        self.assertEqual([b.complete for b in res.bars], [True, False])
        self.assertEqual(res.stored, {"new": 1, "unchanged": 0, "revised": 0, "incomplete_not_stored": 1})

    def test_a_bar_cut_off_by_the_request_end_is_never_complete(self):
        """FIXED: with endDateTime inside a bar, the bar IBKR truncates there was stored as a full, complete bar."""
        end = utc_ns(2026, 10, 22, 14, 0, 30)
        self.respond([bar_row(epoch(end - 90 * S)), bar_row(epoch(end - 30 * S))])
        res = self.client.fetch(self.intraday(duration="120 S", end_ns=end))
        self.assertEqual([b.complete for b in res.bars], [True, False])
        self.assertEqual((res.stored["new"], res.stored["incomplete_not_stored"]), (1, 1))

    def test_no_security_definition_is_a_recorded_survivorship_gap(self):
        """FIXED: IBKR 200 (for example a security no longer trading) was a bare ERROR with no gap, so the series
        was indistinguishable from one never fetched (CAP-MKT-05 survivorship)."""
        self.respond([], error=(200, "No security definition has been found for the request"))
        res = self.client.fetch(self.intraday())
        self.assertEqual(res.outcome, "NO_SECURITY_DEFINITION")
        self.assertIn("survivorship", res.detail)
        gaps = [(g["reason"], g["fixture"]) for g in self.store.gaps("INS-FXA")]
        self.assertEqual(gaps, [("IBKR_NO_SECURITY_DEFINITION", 1)])

    def test_a_local_per_second_refusal_is_labelled_as_such_not_as_an_ibkr_error(self):
        """FIXED: a request refused by the session's per-second scheduler was reported as outcome ERROR."""
        self.session._pacer.acquire = lambda cls, timeout_s: cls != "query"
        res = self.client.fetch(self.intraday())
        self.assertEqual(res.outcome, "PACING_REFUSED", res.detail)
        self.assertNotIn("reqHistoricalData", self.host.names())


# =============================================================================== contracts
def details(con_id=CON_A, symbol="FXA", primary="NASDAQ", currency="USD", long_name="FIXTURE ALPHA INC",
            isin="ZZ0000000017", rules="9001,9001,9001", exchanges="SMART,NASDAQ,ARCA", stock_type="COMMON",
            min_tick=0.0001, contract_cls=None, cd_cls=None, tag_cls=None):
    c = (contract_cls or FakeContract)()
    c.conId, c.symbol, c.secType, c.exchange, c.primaryExchange, c.currency = (con_id, symbol, "STK", "SMART",
                                                                               primary, currency)
    cd = cd_cls() if cd_cls else SimpleNamespace()
    cd.contract, cd.longName, cd.minTick, cd.marketRuleIds, cd.validExchanges = c, long_name, min_tick, rules, exchanges
    cd.timeZoneId, cd.stockType, cd.minSize, cd.sizeIncrement = "US/Eastern", stock_type, Decimal("1"), Decimal("1")
    tag = tag_cls or (lambda t, v: SimpleNamespace(tag=t, value=v))
    cd.secIdList = [tag("ISIN", isin)] if isin else None
    return cd


def increments(*pairs, cls=None):
    out = []
    for low, inc in pairs:
        p = cls() if cls else SimpleNamespace()
        p.lowEdge, p.increment = low, inc
        out.append(p)
    return out


RULE_9001 = increments((0.0, 0.0001), (1.0, 0.01))
QUERY = C.ListingQuery(symbol="FXA", currency="USD", primary_exchange="NASDAQ", master_isin="ZZ0000000017",
                       listing_mic="XNAS", issuer_name="Fixture Alpha, Inc.")


class ContractEvaluationTests(unittest.TestCase):
    def evaluate(self, dets, query=QUERY, prior=None, rules=None, settlement_days=1):
        return C.evaluate(query, dets, rules={9001: RULE_9001} if rules is None else rules, instrument_id="INS-FXA",
                          now_ns=T0, settlement_days=settlement_days, prior=prior, fixture=True)

    def test_verified_listing_produces_a_valid_instrument_proposal_and_mapping(self):
        res = self.evaluate([details()])
        self.assertEqual(res.status, "VERIFIED", res.reasons)
        self.assertFalse(res.blocks_routing)
        inst = Instrument.from_json(res.instrument)
        self.assertEqual((inst.venue_mic, inst.asset_class.value, inst.symbol, inst.attributes["ibkr.conId"]),
                         ("XNAS", "EQUITY", "FXA", CON_A))
        self.assertEqual(res.mapping["external_id"], str(CON_A))
        self.assertEqual((res.mapping["version"], res.mapping["verified_at_ns"]), (1, T0))
        # price-dependent tick schedule from MarketRuleIds (MinTick alone would allow 101.2501)
        self.assertEqual(inst.tick_for(F("0.5")), F("0.0001"))
        self.assertEqual(inst.tick_for(F("101.25")), F("0.01"))
        self.assertTrue(inst.is_valid_price(Price("USD", F("0.5001"))))
        self.assertFalse(inst.is_valid_price(Price("USD", F("101.2501"))))
        self.assertTrue(inst.is_valid_price(Price("USD", F("101.25"))))
        self.assertEqual(res.checks["isin"], "pass")
        self.assertTrue(res.checks["issuer_name"].startswith("pass"))

    def test_more_than_one_match_is_ambiguous_with_no_mapping(self):
        res = self.evaluate([details(), details(con_id=CON_B, primary="ARCA")])
        self.assertEqual((res.status, res.mapping, res.instrument, res.blocks_routing), ("AMBIGUOUS", None, None, True))
        self.assertEqual(len(res.matches), 2)
        self.assertEqual(self.evaluate([]).status, "UNRESOLVED")

    def test_conflicts_block_routing(self):
        cases = {"isin": details(isin="ZZ9999999999"), "currency": details(currency="CAD"),
                 "mic": details(primary="ARCA"), "name": details(long_name="UNRELATED HOLDINGS PLC")}
        for name, cd in cases.items():
            with self.subTest(check=name):
                res = self.evaluate([cd])
                self.assertEqual((res.status, res.mapping), ("CONFLICT", None), res.reasons)
        hidden = self.evaluate([details(isin=None)])
        self.assertEqual((hidden.status, hidden.checks["isin"]), ("VERIFIED", "not visible in SecIdList"))

    def test_missing_rule_unmapped_exchange_or_settlement_is_incomplete(self):
        self.assertEqual(self.evaluate([details()], rules={}).status, "INCOMPLETE")
        q = dataclasses.replace(QUERY, primary_exchange="TSE", listing_mic=None)
        self.assertEqual(self.evaluate([details(primary="TSE")], query=q).status, "INCOMPLETE")
        self.assertEqual(self.evaluate([details()], settlement_days=None).status, "INCOMPLETE")
        self.assertEqual(self.evaluate([details(stock_type="ETN")]).status, "INCOMPLETE")
        bad = self.evaluate([details()], rules={9001: increments((0.5, 0.01))})
        self.assertIn("not 0", bad.reasons[-1])

    def test_rules_that_differ_by_exchange_use_the_primary_exchanges_rule(self):
        res = self.evaluate([details(rules="9002,9001,9002")], rules={9001: RULE_9001,
                                                                     9002: increments((0.0, 0.05))})
        self.assertEqual((res.status, res.market_rule_id), ("VERIFIED", 9001))
        self.assertTrue(res.warnings)

    def test_changed_mapping_blocks_until_owner_remap_creates_a_new_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = C.ContractStore(Path(tmp) / "research.sqlite")
            self.addCleanup(store.close)
            first = self.evaluate([details()])
            store.record("INS-FXA", first)
            prior = store.current_mapping("INS-FXA")
            changed = self.evaluate([details(con_id=CON_B)], prior=prior)
            store.record("INS-FXA", changed)
            self.assertEqual((changed.status, changed.blocks_routing), ("CHANGED", True))
            mapping = store.approve_remap("INS-FXA", CON_B, by="owner", note="fixture corporate action", now_ns=T0)
            self.assertEqual(mapping["version"], 2)
            again = self.evaluate([details(con_id=CON_B)], prior=store.current_mapping("INS-FXA"))
            self.assertEqual((again.status, again.mapping["version"]), ("VERIFIED", 2))
            history = store.conn.execute("SELECT status, con_id, mapping_version FROM ibkr_contract_resolution ORDER BY "
                                         "resolution_id").fetchall()
            self.assertEqual([tuple(r) for r in history], [("VERIFIED", CON_A, 1), ("CHANGED", CON_B, None),
                                                           ("APPROVED_REMAP", CON_B, 2)])

    def test_name_similarity(self):
        self.assertEqual(C.name_similarity("APPLE INC", "Apple Inc."), 1.0)
        self.assertGreaterEqual(C.name_similarity("FIXTURE ALPHA INC", "Fixture Alpha Corporation Class A"), 0.6)
        self.assertLess(C.name_similarity("FIXTURE ALPHA INC", "Beta Mining PLC"), 0.6)

    # ---------------------------------------------------------------- adversarial review regressions
    def test_a_longer_name_of_another_issuer_fails_the_name_check(self):
        """FIXED: a prefix shortcut scored 1.0 for any LongName that starts with the issuer's name."""
        self.assertLess(C.name_similarity("APPLE HOSPITALITY REIT INC", "Apple Inc."), C.NAME_MATCH_THRESHOLD)
        q = dataclasses.replace(QUERY, issuer_name="Fixture Inc.", master_isin=None)
        res = self.evaluate([details(long_name="FIXTURE HOSPITALITY REIT INC", isin=None)], query=q)
        self.assertEqual((res.status, res.mapping), ("CONFLICT", None), res.reasons)
        self.assertTrue(res.checks["issuer_name"].startswith("fail"))
        # legal-form and share-class words still normalize away
        self.assertEqual(C.name_similarity("FIXTURE ALPHA INC", "Fixture Alpha Corporation Class A"), 1.0)

    def test_an_owner_remap_of_a_fixture_listing_stays_fixture(self):
        """FIXED: approve_remap wrote fixture = 0, so a fixture listing's remap surfaced unlabelled as real data."""
        with tempfile.TemporaryDirectory() as tmp:
            store = C.ContractStore(Path(tmp) / "research.sqlite")
            self.addCleanup(store.close)
            store.record("INS-FXA", self.evaluate([details()]))
            store.approve_remap("INS-FXA", CON_B, by="owner", note="fixture corporate action", now_ns=T0)
            row = store.conn.execute("SELECT status, fixture, record FROM ibkr_contract_resolution ORDER BY "
                                     "resolution_id DESC LIMIT 1").fetchone()
            self.assertEqual((row["status"], row["fixture"], json.loads(row["record"])["fixture"]),
                             ("APPROVED_REMAP", 1, True))


class ContractResolverTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.store = C.ContractStore(Path(self.tmp.name) / "research.sqlite")
        self.addCleanup(self.store.close)
        self.resolver = C.ContractResolver(self.session, store=self.store)

    def answer(self, dets, rule_pairs=None, error=None):
        def on_details(host, reqId, contract):
            if error:
                host.wrapper.error(reqId, 0, *error, "")
                return
            for cd in dets:
                host.wrapper.contractDetails(reqId, cd)
            host.wrapper.contractDetailsEnd(reqId)

        def on_rule(host, rule_id):
            if rule_pairs is not None:
                host.wrapper.marketRule(rule_id, rule_pairs)
        self.host.hooks["reqContractDetails"] = on_details
        self.host.hooks["reqMarketRule"] = on_rule

    def test_resolution_requests_details_then_the_market_rule(self):
        self.answer([details()], RULE_9001)
        res = self.resolver.resolve(QUERY, instrument_id="INS-FXA", settlement_days=1, fixture=True)
        self.assertEqual(res.status, "VERIFIED", res.reasons)
        sent = [c for c in self.host.calls if c[0] in ("reqContractDetails", "reqMarketRule")]
        self.assertEqual([c[0] for c in sent], ["reqContractDetails", "reqMarketRule"])
        contract = sent[0][2]
        self.assertEqual((contract.symbol, contract.secType, contract.exchange, contract.primaryExchange,
                          contract.currency), ("FXA", "STK", "SMART", "NASDAQ", "USD"))
        self.assertEqual(sent[1][1], 9001)
        self.answer([details()], RULE_9001)
        self.resolver.resolve(QUERY, instrument_id="INS-FXA", settlement_days=1, fixture=True)
        self.assertEqual(self.host.names().count("reqMarketRule"), 1)       # rules are cached
        self.assertEqual(len(self.store.latest()), 1)

    def test_no_security_definition_ambiguity_and_refusals(self):
        self.answer([], error=(200, "No security definition has been found for the request"))
        self.assertEqual(self.resolver.resolve(QUERY, instrument_id="INS-1", settlement_days=1).status, "UNRESOLVED")
        self.answer([], error=(200, "The contract description specified for FXA is ambiguous."))
        self.assertEqual(self.resolver.resolve(QUERY, instrument_id="INS-2", settlement_days=1).status, "AMBIGUOUS")
        sent = self.host.names().count("reqContractDetails")
        refused = self.resolver.resolve(C.ListingQuery(symbol="FXA"), instrument_id="INS-3", settlement_days=1)
        self.assertEqual(refused.status, "REFUSED")
        self.assertEqual(self.host.names().count("reqContractDetails"), sent)   # never sent
        self.answer([details()], None)                                          # rule never arrives
        self.assertEqual(self.resolver.resolve(QUERY, instrument_id="INS-4", settlement_days=1).status, "INCOMPLETE")


# =============================================================================== routes
class FakeCore:
    def __init__(self, quotes=None, fail=False):
        self.quotes, self.fail = quotes or {}, fail

    def query(self, name, **kw):
        if self.fail:
            raise OSError("core socket missing")
        assert name == "quotes"
        return self.quotes


class RouteTests(SessionCase):
    def setUp(self):
        super().setUp()
        from unknown.api.routes import marketdata as R
        self.R = R
        self.ctx = SimpleNamespace(paths=Paths(self.home), core=FakeCore())

    def get(self, *parts, **q):
        return self.R.handle(self.ctx, ["marketdata", *parts], {k: str(v) for k, v in q.items()}, "acct", None)

    def test_stale_flag_flips_exactly_at_each_budget(self):
        quote = {"bid": "10.00", "ask": "10.02", "currency": "USD", "source": "ibkr:live:non_consolidated",
                 "receipt_time_ns": T0, "event_time_ns": None, "delayed": False}
        budgets = {"display": 5000, "pre_trade": 2000}
        at = lambda ms: self.R.quote_analytics(quote, budgets, T0 + ms * 1_000_000)["stale"]   # noqa: E731
        self.assertEqual(at(2000), {"display": False, "pre_trade": False})
        self.assertEqual(at(2001), {"display": False, "pre_trade": True})
        self.assertEqual(at(5001), {"display": True, "pre_trade": True})
        self.assertIsNone(self.R.quote_analytics(quote, {}, T0)["stale"])
        crossed = self.R.quote_analytics({**quote, "bid": "10.03"}, budgets, T0)
        self.assertEqual((crossed["quote_state"], crossed["mid"], crossed["missing_reason"]),
                         ("crossed", None, "crossed quote (bid > ask)"))

    def test_unavailable_reasons_without_sources(self):
        st = self.get("status")
        self.assertFalse(st["available"])
        self.assertIn("not running", st["unavailable_reason"])
        for path in (("bars",), ("bars", "series"), ("contracts",)):
            out = self.get(*path, instrument="INS-FXA")
            self.assertFalse(out["available"])
            self.assertIn("unavailable_reason", out)
        with self.assertRaises(LookupError):
            self.get("nope")
        with self.assertRaises(ValueError):
            self.get("bars")

    def test_status_joins_core_quotes_with_formula_lineage(self):
        self.connect()
        req = self.subscribe()
        self.host.wrapper.marketDataType(req, 1)
        self.host.wrapper.tickPrice(req, 1, 101.25, attrib())
        self.host.wrapper.tickPrice(req, 2, 101.27, attrib())
        self.session.maintain()
        self.ctx.core = FakeCore({"INS-FXA": self.book.get("INS-FXA").to_json(T0 + 500 * 1_000_000)})
        self.enterContext(mock.patch.object(self.R, "_now_ns", return_value=T0 + 500 * 1_000_000))
        st = self.get("status")
        self.assertTrue(st["available"])
        s = st["subscriptions"][0]
        a = s["analytics"]
        self.assertEqual((s["status"], s["consolidation"], a["mid"], a["spread"], a["spread_bps"], a["quote_state"]),
                         ("LIVE", "non_consolidated", "101.260", "0.02", "1.9751", "normal"))
        self.assertTrue(a["live"])
        self.assertIn("mkt.spread_bps@1", a["formulas"]["spread_bps"])
        self.assertIn("display", a["stale"])
        self.assertEqual(st["line_budget"]["in_use"], 1)
        self.ctx.core = FakeCore(fail=True)
        s = self.get("status")["subscriptions"][0]
        self.assertIsNone(s["analytics"])
        self.assertIn("core unreachable", s["analytics_missing_reason"])

    def _store_bars(self, store, instrument, days, rights):
        rule = H.SessionRule.from_calendar_file(XNYS)
        req = H.HistoricalRequest(instrument, MD.ContractSpec(con_id=CON_A), "1 day", "5 D")
        res = H.HistoricalResult(req, f"r-{instrument}", "COMPLETE", "test", receipt_ns=T0)
        res.bars = [H.parse_bar(bar_row(d), req, receipt_ns=T0, settle_delay_ns=0, session_rule=rule) for d in days]
        store.store(res, rights)

    def test_bars_route_is_point_in_time_and_never_serves_fixtures(self):
        research = Paths(self.home).root / "research"
        research.mkdir(parents=True)
        store = H.BarStore(research / "research.sqlite")
        self.addCleanup(store.close)
        self._store_bars(store, "INS-FXA", ("20261020", "20261021"), TEST_RIGHTS)
        self._store_bars(store, "INS-FIX", ("20261020", "20261021"), H.StorageRights.fixture_only())
        hidden = self.get("bars", instrument="INS-FIX", as_of_ns=utc_ns(2026, 10, 23))   # ADR-0020: never served
        self.assertEqual((hidden["count"], hidden["reason"]), (0, "NO_DATA"))
        self.assertFalse(hasattr(self.R, "FIXTURE_LABEL"))
        out = self.get("bars", instrument="INS-FXA", include_fixture=1, as_of_ns=utc_ns(2026, 10, 21, 12))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["excluded_not_yet_available"], 1)
        bar = out["bars"][0]
        self.assertEqual((bar["bar_date"], bar["formula"], bar["units"]["price"]), ("2026-10-20", H.BAR_SPEC_VERSION, "USD"))
        self.assertNotIn("label", bar)
        self.assertNotIn("fixture", bar)
        none = self.get("bars", instrument="INS-FXA", as_of_ns=utc_ns(2026, 10, 19))
        self.assertTrue(none["missing_reason"].startswith("NOT_YET_PUBLISHED"))
        self.assertEqual(none["reason"], "NOT_REPORTED")
        audit = self.get("bars", instrument="INS-FXA", as_of_ns=utc_ns(2026, 10, 23), knowledge_cutoff_ns=T0 - 1)
        self.assertEqual((audit["mode"], audit["count"]), ("audit", 0))
        series = self.get("bars", "series")["series"]
        self.assertEqual([(r["instrument_id"], r["current_bars"]) for r in series], [("INS-FXA", 2)])
        self.assertNotIn("fixture", series[0])

    def test_contracts_route_shows_the_verification_table(self):
        research = Paths(self.home).root / "research"
        research.mkdir(parents=True)
        store = C.ContractStore(research / "research.sqlite")
        self.addCleanup(store.close)
        store.record("INS-FXA", C.evaluate(QUERY, [details()], rules={9001: RULE_9001}, instrument_id="INS-FXA",
                                           now_ns=T0, settlement_days=1))
        store.record("INS-FXB", C.evaluate(QUERY, [details(), details(con_id=CON_B)], rules={},
                                           instrument_id="INS-FXB", now_ns=T0, settlement_days=1))
        store.record("INS-FIX", C.evaluate(QUERY, [details()], rules={9001: RULE_9001}, instrument_id="INS-FIX",
                                           now_ns=T0, settlement_days=1, fixture=True))
        out = self.get("contracts", include_fixture=1)          # the option no longer exists: fixtures never served
        rows = {r["instrument_id"]: r for r in out["contracts"]}
        self.assertEqual((rows["INS-FXA"]["status"], rows["INS-FXA"]["blocks_routing"], rows["INS-FXA"]["con_id"]),
                         ("VERIFIED", False, CON_A))
        self.assertEqual((rows["INS-FXB"]["status"], rows["INS-FXB"]["blocks_routing"]), ("AMBIGUOUS", True))
        self.assertEqual(set(rows), {"INS-FXA", "INS-FXB"})
        self.assertEqual(out["verification_pass_rate"]["value"], "1/2")

    # ---------------------------------------------------------------- adversarial review regressions
    def _live_status(self):
        self.connect()
        req = self.subscribe()
        self.host.wrapper.marketDataType(req, 1)
        self.host.wrapper.tickPrice(req, 1, 101.25, attrib())
        self.host.wrapper.tickPrice(req, 2, 101.27, attrib())
        self.session.maintain()
        self.ctx.core = FakeCore({"INS-FXA": self.book.get("INS-FXA").to_json(T0)})
        return req

    def test_a_stale_status_file_never_reports_a_subscription_live(self):
        """FIXED: a status file left by a session that stopped was still served as status LIVE, live: true."""
        self._live_status()
        stale_at = T0 + (self.R.STATUS_STALE_AFTER_MS + 1) * 1_000_000
        with mock.patch.object(self.R, "_now_ns", return_value=stale_at):
            st = self.get("status")
        s = st["subscriptions"][0]
        self.assertTrue(st["status_file_stale"])
        self.assertEqual((s["status"], s["live"], s.get("reported_status"), s.get("reported_live")),
                         ("STATUS_FILE_STALE", False, "LIVE", True))
        self.assertFalse(s["analytics"]["live"])
        with mock.patch.object(self.R, "_now_ns", return_value=T0 + self.R.STATUS_STALE_AFTER_MS * 1_000_000):
            fresh = self.get("status")["subscriptions"][0]       # exactly at the limit: not stale
        self.assertEqual((fresh["status"], fresh["live"]), ("LIVE", True))

    def test_status_route_joins_quote_analytics_only_to_the_current_subscription(self):
        """FIXED: an earlier, cancelled record of a re-subscribed instrument received the current quote's analytics."""
        self.connect()
        first = self.subscribe()
        self.session.unsubscribe("INS-FXA")
        self.session.maintain()
        current = self._live_status()
        with mock.patch.object(self.R, "_now_ns", return_value=T0):
            subs = {s["req_id"]: s for s in self.get("status")["subscriptions"]}
        self.assertIsNone(subs[first]["analytics"])
        self.assertIn("earlier subscription record", subs[first]["analytics_missing_reason"])
        self.assertEqual((subs[current]["current"], subs[current]["analytics"]["mid"]), (True, "101.260"))

    def test_bars_route_reports_truncation_and_coverage_gaps(self):
        """FIXED: a row limit cut the result silently, and an empty series with recorded gaps read 'never fetched'."""
        research = Paths(self.home).root / "research"
        research.mkdir(parents=True)
        store = H.BarStore(research / "research.sqlite")
        self.addCleanup(store.close)
        self._store_bars(store, "INS-FXA", ("20261019", "20261020", "20261021"), TEST_RIGHTS)
        later = utc_ns(2026, 10, 23)
        cut = self.get("bars", instrument="INS-FXA", as_of_ns=later, limit=2)
        self.assertEqual((cut["count"], cut["truncated"], [b["bar_date"] for b in cut["bars"]]),
                         (2, True, ["2026-10-19", "2026-10-20"]))
        self.assertIn("truncation_note", cut)
        whole = self.get("bars", instrument="INS-FXA", as_of_ns=later, limit=3)
        self.assertEqual((whole["count"], whole["truncated"]), (3, False))
        gone = H.HistoricalRequest("INS-GONE", MD.ContractSpec(con_id=CON_B), "1 day", "5 D")
        store.record_gap(gone, start_ns=None, end_ns=None, reason="IBKR_NO_SECURITY_DEFINITION", detail="test",
                         recorded_ns=T0, request_id="r2")
        empty = self.get("bars", instrument="INS-GONE", as_of_ns=later)
        self.assertEqual(empty["reason"], "NO_DATA")
        self.assertTrue(empty["missing_reason"].startswith("COVERAGE_GAP"), empty["missing_reason"])
        self.assertIn("IBKR_NO_SECURITY_DEFINITION", empty["missing_reason"])

    def test_contracts_last_verified_is_the_last_passing_check_not_the_last_attempt(self):
        """FIXED: 'last_verified_ns' showed the time of a later FAILED attempt; a fixture remap is never served."""
        research = Paths(self.home).root / "research"
        research.mkdir(parents=True)
        store = C.ContractStore(research / "research.sqlite")
        self.addCleanup(store.close)
        later = T0 + 86_400 * S
        ev = lambda iid, dets, at: C.evaluate(QUERY, dets, rules={9001: RULE_9001}, instrument_id=iid,  # noqa: E731
                                              now_ns=at, settlement_days=1)
        store.record("INS-FXA", ev("INS-FXA", [details()], T0))
        store.record("INS-FXA", ev("INS-FXA", [details(isin="ZZ9999999999")], later))
        store.record("INS-FXB", ev("INS-FXB", [details(isin="ZZ9999999999")], later))
        rows = {r["instrument_id"]: r for r in self.get("contracts")["contracts"]}
        a = rows["INS-FXA"]
        self.assertEqual((a["status"], a["last_resolved_ns"], a["last_verified_ns"]), ("CONFLICT", later, T0))
        self.assertIsNone(rows["INS-FXB"]["last_verified_ns"])
        store.record("INS-FIX", C.evaluate(QUERY, [details()], rules={9001: RULE_9001}, instrument_id="INS-FIX",
                                           now_ns=T0, settlement_days=1, fixture=True))
        store.approve_remap("INS-FIX", CON_B, by="owner", note="test", now_ns=later)
        ids = [r["instrument_id"] for r in self.get("contracts")["contracts"]]
        self.assertEqual(sorted(ids), ["INS-FXA", "INS-FXB"])               # a fixture remap inherits the flag: hidden


# =============================================================================== official SDK
class SdkTests(SessionCase):
    """Same flows with the official ibapi classes; also pins every method and callback signature."""

    def setUp(self):
        if not HAVE_IBAPI:
            self.skipTest(SKIP_REASON)
        from unknown.broker.ibkr.adapter import load_sdk
        type(self).host_cls = host_class(EClient)
        type(self).sdk = load_sdk()
        super().setUp()

    def tearDown(self):
        type(self).host_cls = PureHost
        type(self).sdk = PURE_SDK

    @staticmethod
    def params(fn):
        return [p.name for p in inspect.signature(fn).parameters.values()]

    def test_every_eclient_method_used_exists_with_this_signature(self):
        for name, params in MD.ECLIENT_METHODS.items():
            self.assertEqual(self.params(getattr(EClient, name)), params, name)
            if name in self.host_cls.__dict__:
                self.assertEqual(self.params(getattr(self.host_cls, name)), params, f"FakeHost.{name}")

    def test_wrapper_overrides_match_ewrapper(self):
        cls = MD._md_wrapper_class(EWrapper)
        overridden = sorted(n for n in cls.__dict__ if not n.startswith("_"))
        self.assertEqual(overridden, sorted(MD.EWRAPPER_CALLBACKS))
        for name in overridden:
            self.assertEqual(self.params(getattr(cls, name)), self.params(getattr(EWrapper, name)), name)

    def test_tick_ids_match_ticktype_enum(self):
        names = {"bid": "BID", "ask": "ASK", "last": "LAST", "high": "HIGH", "low": "LOW", "close": "CLOSE",
                 "open": "OPEN", "bid_size": "BID_SIZE", "ask_size": "ASK_SIZE", "last_size": "LAST_SIZE",
                 "volume": "VOLUME", "last_timestamp": "LAST_TIMESTAMP", "halted": "HALTED"}
        for table, prefix in ((MD.PRICE_TICKS, ""), (MD.SIZE_TICKS, ""), (MD.STRING_TICKS, ""),
                              (MD.GENERIC_TICKS, ""), (MD.DELAYED_PRICE_TICKS, "DELAYED_"),
                              (MD.DELAYED_SIZE_TICKS, "DELAYED_"), (MD.DELAYED_STRING_TICKS, "DELAYED_"),
                              (MD.DELAYED_GENERIC_TICKS, "DELAYED_")):
            for tick_id, field_name in table.items():
                self.assertEqual(getattr(TickTypeEnum, prefix + names[field_name]), tick_id, (prefix, field_name))

    def test_quotes_bars_and_contracts_with_official_objects(self):
        self.connect()
        req = self.subscribe()
        contract = [c for c in self.host.calls if c[0] == "reqMktData"][0][2]
        self.assertIsInstance(contract, Contract)
        w = self.host.wrapper
        self.assertIsInstance(w, EWrapper)
        w.marketDataType(req, 1)
        w.tickPrice(req, TickTypeEnum.BID, 101.25, TickAttrib())
        w.tickSize(req, TickTypeEnum.BID_SIZE, Decimal("300"))
        w.tickPrice(req, TickTypeEnum.ASK, -1.0, TickAttrib())
        w.tickSize(req, TickTypeEnum.ASK_SIZE, Decimal("0"))
        w.tickReqParams(req, 0.01, "9c0001", 3)
        q = self.book.get("INS-FXA")
        self.assertEqual((q.bid, q.ask, q.source, q.delayed), (F("101.25"), None, "ibkr:live:non_consolidated", False))
        s = self.session.subscription("INS-FXA")
        self.assertEqual((s["fields"]["ask"]["null_reason"], s["min_tick"]), (MD.NO_DATA_REASON, "0.01"))

        def bars(host, reqId, *args):
            for i in range(3):
                b = BarData()
                b.date, b.open, b.high, b.low, b.close = epoch(T0 - (3 - i) * 60 * S), 100.0, 101.0, 99.0, 100.5
                b.volume, b.wap, b.barCount = Decimal("1200"), Decimal("100.3"), 12
                host.wrapper.historicalData(reqId, b)
            host.wrapper.historicalDataEnd(reqId, "a", "b")
        self.host.hooks["reqHistoricalData"] = bars
        store = H.BarStore(Path(self.tmp.name) / "research.sqlite")
        self.addCleanup(store.close)
        res = H.HistoryClient(self.session, store=store, rights=H.StorageRights.fixture_only()).fetch(
            H.HistoricalRequest("INS-FXA", MD.ContractSpec(con_id=CON_A), "1 min", "180 S"))
        self.assertEqual((res.outcome, res.stored["new"]), ("COMPLETE", 3))
        self.assertEqual(res.bars[0].volume, F("1200"))

        cd = details(contract_cls=Contract, cd_cls=ContractDetails, tag_cls=TagValue)
        self.host.hooks["reqContractDetails"] = lambda host, reqId, c: (host.wrapper.contractDetails(reqId, cd),
                                                                        host.wrapper.contractDetailsEnd(reqId))
        self.host.hooks["reqMarketRule"] = lambda host, rid: host.wrapper.marketRule(
            rid, increments((0.0, 0.0001), (1.0, 0.01), cls=PriceIncrement))
        result = C.ContractResolver(self.session).resolve(QUERY, instrument_id="INS-FXA", settlement_days=1)
        self.assertEqual(result.status, "VERIFIED", result.reasons)
        self.assertEqual(result.tick_rules, [{"from_price": "0", "tick": "0.0001"}, {"from_price": "1", "tick": "0.01"}])


if __name__ == "__main__":
    unittest.main()
