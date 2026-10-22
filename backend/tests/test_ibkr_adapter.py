"""Tests for the IBKR TWS API paper adapter (BRK-02), driven WITHOUT a network.

The adapter talks to ``FakeHost``, a subclass of the official ``ibapi.client.EClient``
that overrides only the socket-facing methods: it records every request and answers
through the adapter's real EWrapper callbacks with objects built from ibapi's own
classes (Contract, Order, OrderState, Execution, CommissionAndFeesReport), in the
field shapes documented by IBKR (pages cited in the research record R3-R7).

Adapter tests need the official SDK (TWS API 10.51.01) and skip cleanly without it:

    PYTHONPATH=src <ibkr-venv>/bin/python -m unittest tests.test_ibkr_adapter -v

Config, order-id store, pacing and mapping tests are pure and always run. Every
account id, order id and price here is a TEST FIXTURE, not account data.
Passing these tests is not evidence of IBKR compatibility (not paper-verified).
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from unknown.broker.ibkr import mapping as M
from unknown.broker.ibkr.config import IbkrConfig, IbkrConfigError, _inside_git_work_tree, config_from_dict, load_config
from unknown.broker.ibkr.ids import OrderIdStateError, OrderIdStore
from unknown.broker.ibkr.pacing import PacingScheduler, max_requests_per_second
from unknown.broker.port import (AccountMode, BrokerOrder, BrokerOrderStatus, CommissionEvent, ConnectivityEvent,
                                 ExecutionEvent, OrderStatusEvent, SubmitStatus)
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed

try:
    import ibapi  # noqa: F401  (official SDK from the owner's local installation; never vendored)
    from ibapi.client import EClient
    from ibapi.commission_and_fees_report import CommissionAndFeesReport
    from ibapi.const import UNSET_DOUBLE
    from ibapi.contract import Contract
    from ibapi.execution import Execution, ExecutionFilter
    from ibapi.order import Order
    from ibapi.order_cancel import OrderCancel
    from ibapi.order_state import OrderState
    from ibapi.wrapper import EWrapper
    HAVE_IBAPI = True
except ImportError:
    HAVE_IBAPI = False

SKIP_REASON = ("ibapi (official IBKR TWS API 10.51.01 Python client) is not importable in this interpreter; "
               "adapter tests skipped. Run: PYTHONPATH=src <ibkr-venv>/bin/python -m unittest tests.test_ibkr_adapter -v")

F = Fixed.parse
SRC = Path(__file__).resolve().parents[1] / "src"
ACCOUNT = "DUFIXTURE1"          # fixture paper account id (not a real account)
CLIENT_ID = 17
AAPL, MSFT = 265598, 272093     # fixture conIds
# Thursday 2026-10-22 10:30 America/New_York (14:30 UTC, EDT); fixture time only.
T0 = utc_ns(2026, 10, 22, 14, 30)
DAY_NS = 86_400 * 10**9
_LOG = logging.getLogger("unknown.broker.ibkr")
_LOG_LEVEL = _LOG.level


def setUpModule():
    _LOG.setLevel(logging.ERROR)     # anomalies are asserted through adapter.anomalies(), not log output


def tearDownModule():
    _LOG.setLevel(_LOG_LEVEL)


def base_config(state_dir, **over) -> dict:
    data = {"host": "127.0.0.1", "port": 4002, "client_id": CLIENT_ID, "expected_account_id": ACCOUNT,
            "mode": "PAPER", "host_time_zone": "America/New_York", "state_dir": str(state_dir),
            "connect_timeout_s": 1.0, "submit_timeout_s": 0.3, "cancel_timeout_s": 0.3, "request_timeout_s": 0.3,
            "pacing_wait_s": 0.05, "market_data_lines": 400}
    data.update(over)
    return data


def broker_order(cid="C1", side="BUY", qty="10", limit="101.25", provider_ref=str(AAPL), tif="DAY", **kw):
    return BrokerOrder(client_order_id=cid, order_ref=f"mac-dev:1:{cid}", instrument_id="INS-AAPL",
                       provider_ref=provider_ref, side=side, quantity=F(qty), limit_price=F(limit), currency="USD",
                       time_in_force=tif, **kw)


# =============================================================================== pure tests (always run)
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_live_mode_is_refused(self):
        with self.assertRaisesRegex(IbkrConfigError, "only PAPER"):
            config_from_dict(base_config(self.tmp.name, mode="LIVE"))

    def test_documented_live_ports_are_refused(self):
        for port, host_type in ((4001, "GATEWAY"), (7496, "TWS")):
            with self.assertRaisesRegex(IbkrConfigError, "LIVE default"):
                config_from_dict(base_config(self.tmp.name, port=port, host_type=host_type))

    def test_port_must_match_paper_profile_unless_attested(self):
        with self.assertRaisesRegex(IbkrConfigError, "PAPER profile port 4002"):
            config_from_dict(base_config(self.tmp.name, port=4010))
        cfg = config_from_dict(base_config(self.tmp.name, port=4010, custom_paper_port=4010))
        self.assertEqual(cfg.profile_port, 4010)
        self.assertEqual(config_from_dict(base_config(self.tmp.name, port=7497, host_type="TWS")).profile_port, 7497)

    def test_remote_host_client_zero_and_unknown_keys_are_refused(self):
        with self.assertRaisesRegex(IbkrConfigError, "loopback"):
            config_from_dict(base_config(self.tmp.name, host="10.0.0.5"))
        with self.assertRaisesRegex(IbkrConfigError, "client_id"):
            config_from_dict(base_config(self.tmp.name, client_id=0))
        with self.assertRaisesRegex(IbkrConfigError, "unknown configuration keys"):
            config_from_dict(base_config(self.tmp.name, pasword="x"))
        with self.assertRaisesRegex(IbkrConfigError, "lastNDays maximum 7"):
            config_from_dict(base_config(self.tmp.name, execution_last_n_days=8))

    def test_pacing_profile_from_market_data_lines(self):
        cfg = config_from_dict(base_config(self.tmp.name, market_data_lines=100))
        self.assertEqual(cfg.max_requests_per_second, 50)       # R8: 100 lines / 2
        self.assertEqual(config_from_dict(base_config(self.tmp.name, market_data_lines=125)).max_requests_per_second, 62)
        self.assertEqual(cfg.execution_history_days, 1)          # R5: IB Gateway, since midnight

    def test_config_file_must_live_outside_git(self):
        root = Path(self.tmp.name)
        if _inside_git_work_tree(root / "x"):
            self.skipTest("temporary directory is itself inside a git work tree")
        (root / "repo" / ".git").mkdir(parents=True)
        inside = root / "repo" / "ibkr.json"
        inside.write_text(json.dumps(base_config(root / "state")))
        with self.assertRaisesRegex(IbkrConfigError, "inside a git work tree"):
            load_config(inside)
        outside = root / "ibkr.json"
        outside.write_text(json.dumps(base_config(root / "state")))
        self.assertEqual(load_config(outside).expected_account_id, ACCOUNT)
        bad_state = root / "ibkr2.json"
        bad_state.write_text(json.dumps(base_config(root / "repo" / "state")))
        with self.assertRaisesRegex(IbkrConfigError, "state_dir"):
            load_config(bad_state)


class ImportBoundaryTests(unittest.TestCase):
    def _run(self, code: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)

    def test_package_import_never_imports_ibapi(self):
        r = self._run("import sys, unknown.broker.ibkr; print('ibapi' in sys.modules)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")

    def test_constructor_raises_a_clear_error_when_ibapi_is_absent(self):
        code = ("import sys; sys.modules['ibapi'] = None\n"
                "from unknown.broker.ibkr import IbkrAdapter, IbkrSdkUnavailable, config_from_dict\n"
                f"cfg = config_from_dict({base_config('/nonexistent-ibkr-state')!r})\n"
                "try:\n    IbkrAdapter(cfg, instrument_resolver=lambda c: None)\n"
                "except IbkrSdkUnavailable as e:\n    print('RAISED', 'download2.interactivebrokers.com' in str(e))\n")
        r = self._run(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "RAISED True")
        self.assertFalse(Path("/nonexistent-ibkr-state").exists())

    def test_core_never_imports_ibapi_or_the_adapter(self):
        pattern = re.compile(r"^\s*(from|import)\s+[\w.]*(ibapi|ibkr)\b", re.M)
        offenders = []
        for path in (SRC / "unknown").rglob("*.py"):
            if "ibkr" in path.relative_to(SRC / "unknown").parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_no_ibapi_source_is_vendored_in_the_repository(self):
        repo = SRC.parents[1]
        hits = []
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "venv", ".claude", "__pycache__")]
            if Path(root).name == "ibapi" and {"wrapper.py", "client.py"} & set(files):
                hits.append(root)
        self.assertEqual(hits, [])


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.pacer = PacingScheduler(50, 20, monotonic_ns=lambda: self.now, sleep=lambda s: None)

    def test_r_max_is_floor_of_half_the_lines(self):
        self.assertEqual(max_requests_per_second(100), 50)
        self.assertEqual(max_requests_per_second(201), 100)

    def test_never_more_than_r_max_in_any_one_second_window(self):
        sent = []
        for step in range(3000):                       # 3 s in 1 ms steps, every class hammering
            self.now = step * 1_000_000
            for cls in ("order", "query", "cancel", "reconcile"):
                if self.pacer.try_acquire(cls):
                    sent.append(self.now)
        for i, t in enumerate(sent):
            in_window = sum(1 for u in sent[i:] if u < t + 1_000_000_000)
            self.assertLessEqual(in_window, 50)

    def test_reserved_share_keeps_cancels_flowing_when_entries_saturate(self):
        granted_orders = sum(self.pacer.try_acquire("order") for _ in range(100))
        self.assertEqual(granted_orders, 40)           # 20% of 50 held back
        self.assertTrue(all(self.pacer.try_acquire("cancel") for _ in range(10)))
        self.assertFalse(self.pacer.try_acquire("cancel"))
        self.now += 1_000_000_001
        self.assertTrue(self.pacer.try_acquire("order"))

    def test_error_100_penalty_halts_non_critical_classes_only(self):
        self.pacer.penalize(10)
        self.assertFalse(self.pacer.try_acquire("order"))
        self.assertFalse(self.pacer.try_acquire("query"))
        self.assertTrue(self.pacer.try_acquire("reconcile"))
        self.now += 10 * 10**9
        self.assertTrue(self.pacer.try_acquire("order"))


class OrderIdStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = T0

    def store(self, **kw):
        return OrderIdStore(Path(self.tmp.name), account_id=ACCOUNT, client_id=CLIENT_ID, now_ns=lambda: self.now, **kw)

    def test_allocation_is_persisted_before_return_and_survives_restart(self):
        s = self.store()
        a, anomaly = s.allocate("C1", 100)
        self.assertEqual((a.order_id, anomaly), (100, None))
        on_disk = json.loads(s.path.read_text())
        self.assertEqual(on_disk["allocations"]["C1"]["order_id"], 100)
        self.assertNotIn(ACCOUNT, s.path.read_text())          # only a hash of the account id is stored
        self.assertEqual(s.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(s.allocate("C1", 500)[0].order_id, 100)   # idempotent per client order id
        restarted = self.store()
        b, anomaly = restarted.allocate("C2", 50)               # host's nextValidId regressed
        self.assertEqual(b.order_id, 101)
        self.assertIn("ID_REGRESSION", anomaly)
        self.assertEqual(restarted.allocate("C3", 300)[0].order_id, 300)

    def test_prepare_transmit_marks_before_send_and_replaces_stale_unsent_ids(self):
        s = self.store()
        s.allocate("C1", 100)
        s.allocate("C2", 100)
        c2, _ = s.prepare_transmit("C2", 100, {"quantity": "5"})
        self.assertEqual(c2.order_id, 101)
        self.assertTrue(c2.transmitted)
        c1, _ = s.prepare_transmit("C1", 100, {"quantity": "5"})
        self.assertEqual(c1.order_id, 102)                      # 100 was never sent and is now below 101
        self.assertEqual(c1.superseded_order_ids, (100,))
        with self.assertRaises(OrderIdStateError):
            s.prepare_transmit("C1", 100, {})
        self.assertTrue(self.store().get("C1").transmitted)

    def test_corrupt_or_foreign_state_is_never_silently_reset(self):
        s = self.store()
        s.path.write_text("{not json")
        with self.assertRaises(OrderIdStateError):
            self.store()
        s.path.write_text(json.dumps({"version": 1, "scope": {"account_sha256_16": "x", "client_id": 1}}))
        with self.assertRaisesRegex(OrderIdStateError, "different account"):
            self.store()

    def test_pruning_never_lowers_the_next_id(self):
        s = self.store()
        s.allocate("C1", 100)
        self.now += 30 * DAY_NS
        s2 = self.store(retention_days=14)
        self.assertIsNone(s2.get("C1"))
        self.assertEqual(s2.allocate("C2", 1)[0].order_id, 101)


class MappingTests(unittest.TestCase):
    def test_status_table(self):
        n = M.normalize_status
        S = BrokerOrderStatus
        self.assertEqual(n("PendingSubmit", F("0"), rejection_seen=False)[0], S.PENDING_SUBMIT)
        self.assertEqual(n("PreSubmitted", F("0"), rejection_seen=False)[0], S.ACKNOWLEDGED)
        self.assertEqual(n("Submitted", F("3"), rejection_seen=False)[0], S.PARTIALLY_FILLED)
        self.assertEqual(n("Filled", F("10"), rejection_seen=False)[0], S.FILLED)
        self.assertEqual(n("PendingCancel", F("0"), rejection_seen=False)[0], S.PENDING_CANCEL)
        self.assertEqual(n("PreCancelled", F("0"), rejection_seen=False)[0], S.PENDING_CANCEL)
        self.assertEqual(n("Cancelled", F("0"), rejection_seen=False)[0], S.CANCELED)
        self.assertEqual(n("Cancelled", F("0"), rejection_seen=True)[0], S.REJECTED)
        self.assertEqual(n("Cancelled", F("4"), rejection_seen=True)[0], S.CANCELED)   # fills: not a rejection
        self.assertEqual(n("Inactive", F("0"), rejection_seen=False)[0], S.INACTIVE)
        status, note = n("WarnState", F("0"), rejection_seen=False)
        self.assertEqual(status, S.ACKNOWLEDGED)
        self.assertIn("WARNING", note)
        self.assertEqual(n("ApiPending", F("0"), rejection_seen=False)[0], None)

    def test_exec_id_revision(self):
        self.assertEqual(M.parse_exec_id("0000e0d5.6554a3c6.01.01"), ("0000e0d5.6554a3c6.01", 1, 2))
        self.assertEqual(M.parse_exec_id("0000e0d5.6554a3c6.01.02"), ("0000e0d5.6554a3c6.01", 2, 2))
        self.assertEqual(M.parse_exec_id("0001f4e8.6554b2a1.02.01.01")[1], 1)   # 5-segment combo leg id
        self.assertEqual(M.parse_exec_id("nodots"), ("nodots", None, 0))

    def test_exec_time_formats(self):
        ny = utc_ns(2026, 10, 22, 14, 31, 5)
        self.assertEqual(M.parse_exec_time("20261022 10:31:05 America/New_York", "UTC"),
                         (ny, "suffix:America/New_York"))
        self.assertEqual(M.parse_exec_time("20261022 10:31:05 US/Eastern", "UTC")[0], ny)
        self.assertEqual(M.parse_exec_time("20261022-14:31:05", "America/New_York"), (ny, "utc"))
        self.assertEqual(M.parse_exec_time("20261022  10:31:05", "America/New_York"),
                         (ny, "assumed:America/New_York"))
        for bad in ("20261022 10:31:05 Mars/Olympus", "1729607465", ""):
            with self.assertRaises(ValueError):
                M.parse_exec_time(bad, "America/New_York")

    def test_unset_values_map_to_none_never_zero(self):
        self.assertIsNone(M.float_to_fixed(M.UNSET_DOUBLE))
        self.assertIsNone(M.float_to_fixed(float("nan")))
        self.assertIsNone(M.decimal_to_fixed(M.UNSET_DECIMAL))
        self.assertIsNone(M.summary_value_to_fixed("1.7976931348623157E308"))
        self.assertIsNone(M.summary_value_to_fixed(""))
        self.assertEqual(M.float_to_fixed(101.25), F("101.25"))
        self.assertEqual(M.decimal_to_fixed(Decimal("100")).to_str(), "100")
        self.assertEqual(M.summary_value_to_fixed("998500.00"), F("998500.00"))
        self.assertEqual(M.fixed_to_order_float(F("101.25")), 101.25)

    def test_foreign_orders_never_share_our_order_id_namespace(self):
        self.assertEqual(M.broker_order_key(17, 17, 100, 9001), "100")
        self.assertEqual(M.broker_order_key(17, 5, 100, 9002), "perm:9002")
        self.assertEqual(M.broker_order_key(17, 0, 0, 0), "client:0:order:0")


# =============================================================================== fake host (needs ibapi)
if HAVE_IBAPI:
    def ib_contract(con_id=AAPL, symbol="AAPL", currency="USD"):
        c = Contract()
        c.conId, c.symbol, c.secType, c.exchange, c.primaryExchange, c.currency = (con_id, symbol, "STK", "SMART",
                                                                                   "NASDAQ", currency)
        return c

    def ib_order(order_id, *, client_id=CLIENT_ID, perm_id=0, action="BUY", qty="10", lmt=101.25, order_ref=""):
        o = Order()
        o.orderId, o.clientId, o.permId, o.action = order_id, client_id, perm_id, action
        o.totalQuantity, o.orderType, o.lmtPrice, o.tif, o.orderRef = Decimal(qty), "LMT", lmt, "DAY", order_ref
        return o

    def ib_state(status, completed_status=""):
        s = OrderState()
        s.status, s.completedStatus = status, completed_status
        return s

    def ib_execution(exec_id, order_id, *, side="BOT", shares="10", price=101.25, perm_id=9001, client_id=CLIENT_ID,
                     when="20261022 10:31:05 America/New_York", account=ACCOUNT, order_ref=""):
        e = Execution()
        e.execId, e.time, e.acctNumber, e.exchange, e.side = exec_id, when, account, "ISLAND", side
        e.shares, e.price, e.permId, e.clientId, e.orderId = Decimal(shares), price, perm_id, client_id, order_id
        e.cumQty, e.avgPrice, e.orderRef = Decimal(shares), price, order_ref
        return e

    def ib_commission(exec_id, amount=1.0, currency="USD", realized=None):
        r = CommissionAndFeesReport()
        r.execId, r.commissionAndFees, r.currency = exec_id, amount, currency
        r.realizedPNL = UNSET_DOUBLE if realized is None else realized   # R5: unset double -> null
        return r

    class FakeHost(EClient):
        """EClient with the socket replaced: records requests, answers via EWrapper callbacks."""

        def __init__(self, wrapper, *, accounts=ACCOUNT, next_valid_id=100, handshake=True, reachable=True,
                     server_time_ms=None):
            EClient.__init__(self, wrapper)
            self.calls: list[tuple] = []
            self.accounts, self.nvid, self.handshake, self.reachable = accounts, next_valid_id, handshake, reachable
            self.connected = False
            self.on_place = None
            self.on_cancel = None
            self.raise_on_place = None
            self.raise_on = set()
            self.executions: list[tuple] = []      # (contract, execution, commission report or None)
            self.open_orders: list[tuple] = []     # (orderId, contract, order, status, filled, remaining)
            self.other_orders: list[tuple] = []    # only via reqAllOpenOrders
            self.completed: list[tuple] = []       # (contract, order, state)
            self.summary: list[tuple] = []         # (account, tag, value, currency)
            self.positions: list[tuple] = []       # (account, contract, position, avgCost)
            self.silent = set()                    # request names that never get their End callback
            self.server_time_ms = server_time_ms   # host clock answer to reqCurrentTimeInMillis (None: no answer)

        def connect(self, host, port, clientId):
            self.calls.append(("connect", host, port, clientId))
            if not self.reachable:
                self.wrapper.error(-1, 0, 502, "Couldn't connect to TWS. Confirm that \"Enable ActiveX and Socket "
                                               "Clients\" is enabled and connection port is the same as \"Socket Port\"")
                return
            self.host, self.port, self.clientId = host, port, clientId
            self.serverVersion_, self.connTime = 222, "20261022 10:30:00 America/New_York"
            self.connected = True
            if self.handshake:
                self.wrapper.connectAck()
                self.wrapper.managedAccounts(self.accounts)
                self.wrapper.nextValidId(self.nvid)

        def isConnected(self):
            return self.connected

        def run(self):
            return None     # callbacks are delivered synchronously by this fake

        def disconnect(self):
            self.calls.append(("disconnect",))
            if self.connected:
                self.connected = False
                self.wrapper.connectionClosed()

        def placeOrder(self, orderId, contract, order):
            self.calls.append(("placeOrder", orderId, contract, order))
            if self.raise_on_place:
                raise self.raise_on_place
            if self.on_place:
                self.on_place(self, orderId, contract, order)

        def cancelOrder(self, orderId, orderCancel):
            self.calls.append(("cancelOrder", orderId, orderCancel))
            if self.on_cancel:
                self.on_cancel(self, orderId)

        def reqIds(self, numIds):
            self.calls.append(("reqIds", numIds))
            self.nvid += 10
            self.wrapper.nextValidId(self.nvid)

        def reqCurrentTimeInMillis(self):
            self.calls.append(("reqCurrentTimeInMillis",))
            if self.server_time_ms is not None:
                self.wrapper.currentTimeInMillis(self.server_time_ms)

        def reqExecutions(self, reqId, execFilter):
            self.calls.append(("reqExecutions", reqId, execFilter))
            for contract, execution, report in list(self.executions):
                self.wrapper.execDetails(reqId, contract, execution)
                if report is not None:
                    self.wrapper.commissionAndFeesReport(report)
            if "reqExecutions" not in self.silent:
                self.wrapper.execDetailsEnd(reqId)

        def _feed(self, rows):
            for order_id, contract, order, status, filled, remaining in rows:
                self.wrapper.openOrder(order_id, contract, order, ib_state(status))
                self.wrapper.orderStatus(order_id, status, Decimal(filled), Decimal(remaining), 0.0, order.permId, 0,
                                         0.0, order.clientId, "", UNSET_DOUBLE)

        def reqOpenOrders(self):
            self.calls.append(("reqOpenOrders",))
            self._feed(self.open_orders)
            if "reqOpenOrders" not in self.silent:
                self.wrapper.openOrderEnd()

        def reqAllOpenOrders(self):
            self.calls.append(("reqAllOpenOrders",))
            self._feed(self.open_orders + self.other_orders)
            if "reqAllOpenOrders" not in self.silent:
                self.wrapper.openOrderEnd()

        def reqCompletedOrders(self, apiOnly):
            self.calls.append(("reqCompletedOrders", apiOnly))
            for contract, order, state in self.completed:
                self.wrapper.completedOrder(contract, order, state)
            self.wrapper.completedOrdersEnd()

        def reqAccountSummary(self, reqId, groupName, tags):
            self.calls.append(("reqAccountSummary", reqId, groupName, tags))
            for row in self.summary:
                self.wrapper.accountSummary(reqId, *row)
            self.wrapper.accountSummaryEnd(reqId)

        def cancelAccountSummary(self, reqId):
            self.calls.append(("cancelAccountSummary", reqId))
            if "cancelAccountSummary" in self.raise_on:
                raise OSError("simulated send failure")

        def reqPositions(self):
            self.calls.append(("reqPositions",))
            for row in self.positions:
                self.wrapper.position(*row)
            if "reqPositions" not in self.silent:
                self.wrapper.positionEnd()

        def cancelPositions(self):
            self.calls.append(("cancelPositions",))

        def names(self):
            return [c[0] for c in self.calls]

    # --------------------------------------------------------------- scripted host responses
    def accept(status="PreSubmitted", perm_base=9000):
        def respond(host, order_id, contract, order):
            order.clientId, order.permId = CLIENT_ID, perm_base + order_id
            host.wrapper.openOrder(order_id, contract, order, ib_state(status))
            host.wrapper.orderStatus(order_id, status, Decimal(0), order.totalQuantity, 0.0, order.permId, 0, 0.0,
                                     CLIENT_ID, "", UNSET_DOUBLE)
        return respond

    def reject(code, text):
        def respond(host, order_id, contract, order):
            host.wrapper.error(order_id, 0, code, text, "")
            host.wrapper.orderStatus(order_id, "Cancelled", Decimal(0), order.totalQuantity, 0.0, 0, 0, 0.0,
                                     CLIENT_ID, "", UNSET_DOUBLE)
        return respond

    def status(host, order_id, status_text, filled="0", remaining="10", perm_id=9100, client_id=CLIENT_ID,
               why_held=""):
        host.wrapper.orderStatus(order_id, status_text, Decimal(filled), Decimal(remaining), 0.0, perm_id, 0, 0.0,
                                 client_id, why_held, UNSET_DOUBLE)


class AdapterCase(unittest.TestCase):
    """Builds an adapter wired to FakeHost instances (one per connect)."""

    host_kwargs: dict = {}

    def setUp(self):
        if not HAVE_IBAPI:
            self.skipTest(SKIP_REASON)
        from unknown.broker.ibkr import IbkrAdapter
        self.IbkrAdapter = IbkrAdapter
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualClock(T0)
        self.instruments = {AAPL: "INS-AAPL", MSFT: "INS-MSFT"}
        self.hosts: list = []
        self.adapter = self.make_adapter()

    def make_adapter(self, host_kwargs=None, **cfg_over):
        kwargs = dict(self.host_kwargs if host_kwargs is None else host_kwargs)

        def factory(wrapper):
            host = FakeHost(wrapper, **kwargs)
            self.hosts.append(host)
            return host

        cfg = config_from_dict(base_config(self.tmp.name, **cfg_over))
        return self.IbkrAdapter(cfg, instrument_resolver=self.instruments.get, clock=self.clock,
                                client_factory=factory)

    @property
    def host(self):
        return self.hosts[-1]

    def connect(self):
        state = self.adapter.connect()
        self.assertTrue(state.authenticated, state.auth_required_reason)
        self.adapter.drain_events()
        return state

    def events(self, kind=None):
        evs = self.adapter.drain_events()
        return [e for e in evs if kind is None or isinstance(e, kind)]


# =============================================================================== connection and identity
class ConnectionTests(AdapterCase):
    def test_handshake_verifies_paper_identity_and_sends_only_the_clock_query(self):
        state = self.adapter.connect()
        self.assertTrue(state.connected and state.authenticated)
        self.assertIsNone(state.auth_required_reason)
        # R2: nothing before nextValidId; after READY only the host clock query (preflight skew evidence).
        self.assertEqual(self.host.calls, [("connect", "127.0.0.1", 4002, CLIENT_ID), ("reqCurrentTimeInMillis",)])
        (ready,) = self.events(ConnectivityEvent)
        self.assertTrue(ready.connected and ready.authenticated)
        self.assertNotIn(ACCOUNT, ready.detail)                  # account ids are masked in observations
        caps = self.adapter.capabilities()
        self.assertIs(caps.account_mode, AccountMode.PAPER)
        self.assertEqual((caps.order_types, caps.time_in_force), (("LIMIT",), ("DAY",)))
        self.assertFalse(caps.short_selling or caps.margin or caps.fractional_quantities or caps.outside_regular_hours)
        self.assertEqual(caps.execution_history_days, 1)
        self.assertEqual(caps.settlement_evidence, "accountSummary SettledCash (≈3 min cadence)")
        self.assertEqual(caps.pacing[0].max_requests, 200)        # 400 lines / 2 in this fixture
        self.assertEqual(caps.extensions["ibkr.pacing_reserved_requests_per_second"], 40)
        self.assertEqual(caps.order_reference_max_length, 0)

    def test_host_clock_is_measured_after_ready_and_ages_out(self):
        """Preflight's clock-skew check needs the host's time (fails closed without it): measured with
        reqCurrentTimeInMillis after READY, carried forward on the local clock, re-requested when older than
        30 s and withheld when older than 120 s."""
        ahead_ms = T0 // 1_000_000 + 1500                                   # host clock 1.5 s ahead
        adapter = self.make_adapter(host_kwargs={"server_time_ms": ahead_ms})
        state = adapter.connect()
        self.assertEqual(state.server_time_ns, T0 + 1_500_000_000)
        self.clock.advance(10 * 1_000_000_000)
        self.assertEqual(adapter.session().server_time_ns, T0 + 11_500_000_000)
        self.assertEqual(self.host.names().count("reqCurrentTimeInMillis"), 1)
        self.host.server_time_ms = None                                     # host stops answering
        self.clock.advance(31 * 1_000_000_000)
        adapter.session()                                                   # stale: asks again
        self.assertEqual(self.host.names().count("reqCurrentTimeInMillis"), 2)
        self.clock.advance(100 * 1_000_000_000)
        self.assertIsNone(adapter.session().server_time_ns, "a 141 s old measurement is not evidence")

    def test_host_that_never_reports_its_clock_gives_no_server_time(self):
        state = self.adapter.connect()
        self.assertIsNone(state.server_time_ns)

    def test_unexpected_managed_accounts_latch_disconnected_unsafe(self):
        adapter = self.make_adapter(host_kwargs={"accounts": f"{ACCOUNT},DUFIXTURE2"})
        state = adapter.connect()
        self.assertFalse(state.connected or state.authenticated)
        self.assertTrue(state.auth_required_reason.startswith("DISCONNECTED_UNSAFE"))
        self.assertEqual(self.host.names(), ["connect", "disconnect"])   # zero subscriptions issued (R2)
        from unknown.broker.ibkr import IbkrNotReady
        with self.assertRaises(IbkrNotReady):
            adapter.capabilities()
        result = adapter.submit(broker_order())
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertTrue(result.reason.startswith("NOT_TRANSMITTED"))
        adapter.connect()
        self.assertEqual(len(self.hosts), 1)                     # latched: no new socket was opened

    def test_missing_next_valid_id_reports_auth_required_and_blocks_requests(self):
        adapter = self.make_adapter(host_kwargs={"handshake": False}, connect_timeout_s=0.2)
        state = adapter.connect()
        self.assertTrue(state.connected)
        self.assertFalse(state.authenticated)
        self.assertIn("AUTH_REQUIRED: manual login in TWS/IB Gateway (2FA); no automation", state.auth_required_reason)
        self.assertEqual(adapter.submit(broker_order()).status, SubmitStatus.REJECTED)
        self.assertEqual(adapter.lookup("C1").found, None)
        with self.assertRaises(ConnectionError):
            adapter.executions_since(None)
        from unknown.broker.ibkr import IbkrNotReady
        with self.assertRaises(IbkrNotReady):
            adapter.allocate_broker_order_id("C1")
        self.assertEqual(self.host.names(), ["connect"])        # nothing sent before nextValidId (R2)

    def test_host_down_maps_502(self):
        adapter = self.make_adapter(host_kwargs={"reachable": False})
        state = adapter.connect()
        self.assertFalse(state.connected)
        self.assertIn("502", state.auth_required_reason)

    def test_live_profile_never_opens_a_socket(self):
        cfg = IbkrConfig(host="127.0.0.1", port=4001, client_id=CLIENT_ID, expected_account_id=ACCOUNT,
                         host_time_zone="America/New_York", mode="LIVE", state_dir=Path(self.tmp.name))
        with self.assertRaisesRegex(IbkrConfigError, "LIVE"):
            self.IbkrAdapter(cfg, instrument_resolver=self.instruments.get,
                             client_factory=lambda w: self.fail("no client may be created for LIVE"))

    def test_client_id_in_use_and_identity_change_latch(self):
        self.connect()
        self.host.wrapper.error(-1, 0, 326, "Unable to connect as the client id is already in use.", "")
        state = self.adapter.session()
        self.assertFalse(state.authenticated)
        self.assertIn("already in use", state.auth_required_reason)
        adapter = self.make_adapter()
        adapter.connect()
        self.hosts[-1].wrapper.managedAccounts("DUFIXTURE9")
        self.assertTrue(adapter.session().auth_required_reason.startswith("DISCONNECTED_UNSAFE"))

    def test_weekly_login_failure_message_reports_auth_required(self):
        self.connect()
        # R9: message text from daily-weekly-reauthentication.md; the error code is not documented.
        self.host.wrapper.error(-1, 0, 0, "Login failed = Soft token=0 received instead of expected permanent for "
                                          "zdc1.ibllc.com:4001 (SSL)", "")
        state = self.adapter.session()
        self.assertFalse(state.authenticated)
        self.assertIn("AUTH_REQUIRED", state.auth_required_reason)
        result = self.adapter.submit(broker_order())
        self.assertTrue(result.reason.startswith("NOT_TRANSMITTED") and "AUTH_REQUIRED" in result.reason)
        self.host.wrapper.error(-1, 0, 1102, "Connectivity between IB and TWS has been restored- data maintained.", "")
        self.assertTrue(self.adapter.session().authenticated)


# =============================================================================== order ids
class OrderIdTests(AdapterCase):
    def test_allocate_persists_before_returning_and_submit_uses_the_id(self):
        self.connect()
        self.assertEqual(self.adapter.allocate_broker_order_id("C1"), "100")
        state_file = next(Path(self.tmp.name).glob("ibkr-order-ids-*.json"))
        self.assertEqual(json.loads(state_file.read_text())["allocations"]["C1"]["order_id"], 100)
        self.assertEqual(self.adapter.allocate_broker_order_id("C1"), "100")
        self.assertEqual(self.adapter.allocate_broker_order_id("C2"), "101")
        self.host.on_place = accept()
        result = self.adapter.submit(broker_order("C1"))
        self.assertEqual((result.status, result.broker_order_id), (SubmitStatus.ACCEPTED, "100"))
        self.assertEqual(self.host.calls[-1][1], 100)

    def test_restart_after_crash_never_reuses_an_id_and_never_resends(self):
        self.connect()
        self.host.raise_on_place = OSError("socket write failed")
        first = self.adapter.submit(broker_order("C1"))
        self.assertEqual((first.status, first.broker_order_id), (SubmitStatus.UNKNOWN, "100"))
        restarted = self.make_adapter()               # same state dir; host reports a stale nextValidId 100
        restarted.connect()
        self.assertIn("ID_REGRESSION", [a["kind"] for a in restarted.anomalies()])
        again = restarted.submit(broker_order("C1"))
        self.assertEqual(again.status, SubmitStatus.UNKNOWN)
        self.assertIn("DUPLICATE_SUBMIT", again.reason)
        self.assertNotIn("placeOrder", self.host.names())       # never re-sent
        self.host.on_place = accept()
        self.assertEqual(restarted.submit(broker_order("C2")).broker_order_id, "101")

    def test_stale_unsent_allocation_is_replaced(self):
        self.connect()
        self.adapter.allocate_broker_order_id("C1")
        self.adapter.allocate_broker_order_id("C2")
        self.host.on_place = accept()
        self.assertEqual(self.adapter.submit(broker_order("C2")).broker_order_id, "101")
        late = self.adapter.submit(broker_order("C1"))
        self.assertEqual(late.broker_order_id, "102")
        self.assertEqual(late.raw["superseded_order_ids"], [100])

    def test_duplicate_order_id_103_rejects_and_resyncs_before_next_entry(self):
        self.connect()
        self.host.on_place = reject(103, "Duplicate order id")
        result = self.adapter.submit(broker_order("C1"))
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertIn("DUPLICATE_ORDER_ID", self.adapter.entry_blocks())
        self.host.on_place = accept()
        nxt = self.adapter.submit(broker_order("C2"))
        self.assertIn("reqIds", self.host.names())
        self.assertEqual((nxt.status, nxt.broker_order_id), (SubmitStatus.ACCEPTED, "110"))


# =============================================================================== submit
class SubmitTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_accepted_limit_day_order_is_built_from_sdk_objects(self):
        self.host.on_place = accept("Submitted")
        result = self.adapter.submit(broker_order("C1"))
        self.assertEqual((result.status, result.broker_order_id), (SubmitStatus.ACCEPTED, "100"))
        _, order_id, contract, order = self.host.calls[-1]
        self.assertIsInstance(contract, Contract)
        self.assertIsInstance(order, Order)
        self.assertEqual((contract.conId, contract.secType, contract.exchange, contract.currency),
                         (AAPL, "STK", "SMART", "USD"))
        self.assertEqual((order.action, order.totalQuantity, order.orderType, order.lmtPrice, order.tif),
                         ("BUY", Decimal("10"), "LMT", 101.25, "DAY"))
        self.assertEqual((order.orderRef, order.outsideRth, order.transmit, order.account),
                         ("mac-dev:1:C1", False, True, ACCOUNT))
        (ev,) = self.events(OrderStatusEvent)
        self.assertEqual((ev.client_order_id, ev.order_ref, ev.broker_order_id, ev.status),
                         ("C1", "mac-dev:1:C1", "100", BrokerOrderStatus.ACKNOWLEDGED))
        self.assertEqual((ev.filled_quantity, ev.remaining_quantity), (F("0"), F("10")))

    def test_symbol_contract_when_provider_ref_is_not_a_conid(self):
        self.host.on_place = accept()
        self.adapter.submit(broker_order("C1", provider_ref="AAPL"))
        contract = self.host.calls[-1][2]
        self.assertEqual((contract.conId, contract.symbol, contract.exchange), (0, "AAPL", "SMART"))

    def test_error_201_then_cancelled_is_rejected(self):
        self.host.on_place = reject(201, "Order rejected - reason:The contract is not available for trading")
        result = self.adapter.submit(broker_order("C1"))
        self.assertEqual(result.status, SubmitStatus.REJECTED)
        self.assertIn("IBKR 201", result.reason)
        (ev,) = self.events(OrderStatusEvent)
        self.assertEqual(ev.status, BrokerOrderStatus.REJECTED)

    def test_error_202_price_check_is_rejected(self):
        self.host.on_place = reject(202, "Order cancelled - Reason: price too far through the market")
        self.assertEqual(self.adapter.submit(broker_order("C1")).status, SubmitStatus.REJECTED)

    def test_timeout_is_unknown_and_never_retried(self):
        result = self.adapter.submit(broker_order("C1"))
        self.assertEqual((result.status, result.broker_order_id), (SubmitStatus.UNKNOWN, "100"))
        self.assertIn("timeout", result.reason)
        self.assertEqual(self.host.names().count("placeOrder"), 1)

    def test_socket_loss_during_wait_is_unknown(self):
        self.host.on_place = lambda host, *a: host.disconnect()
        result = self.adapter.submit(broker_order("C1"))
        self.assertEqual(result.status, SubmitStatus.UNKNOWN)
        self.assertIn("socket", result.reason)

    def test_pending_submit_only_is_accepted_at_the_deadline_and_inactive_is_unknown(self):
        self.host.on_place = accept("PendingSubmit")
        self.assertEqual(self.adapter.submit(broker_order("C1")).status, SubmitStatus.ACCEPTED)
        self.host.on_place = accept("Inactive")
        self.assertEqual(self.adapter.submit(broker_order("C2")).status, SubmitStatus.UNKNOWN)

    def test_local_validation_rejects_without_transmitting(self):
        for bad in (broker_order("X1", side="SSHORT"), broker_order("X2", tif="GTC"),
                    broker_order("X3", qty="1.5"), broker_order("X4", outside_regular_hours=True),
                    broker_order("X5", limit="0.123456789012345678")):
            result = self.adapter.submit(bad)
            self.assertEqual(result.status, SubmitStatus.REJECTED, bad)
            self.assertTrue(result.reason.startswith("NOT_TRANSMITTED"))
        self.assertNotIn("placeOrder", self.host.names())

    def test_upstream_loss_blocks_new_entries_until_restored(self):
        self.host.wrapper.error(-1, 0, 1100, "Connectivity between IB and the TWS has been lost.", "")
        result = self.adapter.submit(broker_order("C1"))
        self.assertTrue(result.status is SubmitStatus.REJECTED and result.reason.startswith("NOT_TRANSMITTED"))
        self.assertNotIn("placeOrder", self.host.names())
        self.host.wrapper.error(-1, 0, 1102, "Connectivity between IB and TWS has been restored- data maintained.", "")
        self.host.on_place = accept()
        self.assertEqual(self.adapter.submit(broker_order("C1")).status, SubmitStatus.ACCEPTED)

    def test_short_sale_signal_blocks_entries_until_owner_clears(self):
        self.host.on_place = reject(426, "None of the accounts have enough shares.")
        self.assertEqual(self.adapter.submit(broker_order("C1", side="SELL")).status, SubmitStatus.REJECTED)
        self.assertIn("SHORT_SALE_SIGNAL", self.adapter.entry_blocks())
        self.assertTrue(self.adapter.submit(broker_order("C2")).reason.startswith("NOT_TRANSMITTED"))
        self.assertTrue(self.adapter.clear_entry_block("SHORT_SALE_SIGNAL", by="owner", note="test"))
        self.host.on_place = accept()
        self.assertEqual(self.adapter.submit(broker_order("C3")).status, SubmitStatus.ACCEPTED)

    def test_acknowledgement_from_another_thread(self):
        def respond_later(host, order_id, contract, order):
            threading.Timer(0.05, accept(), args=(host, order_id, contract, order)).start()
        self.host.on_place = respond_later
        self.adapter._cfg = config_from_dict(base_config(self.tmp.name, submit_timeout_s=2.0))
        started = time.monotonic()
        self.assertEqual(self.adapter.submit(broker_order("C1")).status, SubmitStatus.ACCEPTED)
        self.assertLess(time.monotonic() - started, 1.5)


# =============================================================================== cancel
class CancelTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.host.on_place = accept()
        self.assertEqual(self.adapter.submit(broker_order("C1")).status, SubmitStatus.ACCEPTED)
        self.adapter.drain_events()

    def test_cancel_uses_the_order_cancel_signature_and_is_accepted(self):
        self.host.on_cancel = lambda host, oid: status(host, oid, "PendingCancel", perm_id=9100)
        result = self.adapter.cancel("C1", "100")
        self.assertEqual(result.status, SubmitStatus.ACCEPTED)
        name, order_id, order_cancel = self.host.calls[-1]
        self.assertEqual((name, order_id), ("cancelOrder", 100))
        self.assertIsInstance(order_cancel, OrderCancel)
        status(self.host, 100, "Cancelled", perm_id=9100)
        self.host.wrapper.error(100, 0, 202, "Order Canceled - reason:", "")
        statuses = [e.status for e in self.events(OrderStatusEvent)]
        self.assertEqual(statuses, [BrokerOrderStatus.PENDING_CANCEL, BrokerOrderStatus.CANCELED])  # not REJECTED

    def test_cancel_rejections_and_timeouts(self):
        self.host.on_cancel = lambda host, oid: host.wrapper.error(oid, 0, 10148, "OrderId 100 that needs to be "
                                                                              "cancelled cannot be cancelled", "")
        self.assertEqual(self.adapter.cancel("C1", None).status, SubmitStatus.REJECTED)
        self.host.on_cancel = lambda host, oid: host.wrapper.error(oid, 0, 10147, "OrderId 100 that needs to be "
                                                                              "cancelled is not found.", "")
        self.assertEqual(self.adapter.cancel("C1", None).status, SubmitStatus.REJECTED)
        self.host.on_cancel = None
        self.assertEqual(self.adapter.cancel("C1", None).status, SubmitStatus.UNKNOWN)
        self.host.on_cancel = lambda host, oid: status(host, oid, "Filled", filled="10", remaining="0")
        late = self.adapter.cancel("C1", None)
        self.assertEqual(late.status, SubmitStatus.REJECTED)
        self.assertIn("Filled", late.reason)

    def test_cancel_of_unknown_or_mismatched_order_is_not_sent(self):
        n = self.host.names().count("cancelOrder")
        self.assertTrue(self.adapter.cancel("NOPE", None).reason.startswith("NOT_TRANSMITTED"))
        self.assertTrue(self.adapter.cancel("C1", "999").reason.startswith("NOT_TRANSMITTED"))
        self.assertEqual(self.host.names().count("cancelOrder"), n)


# =============================================================================== status events
class StatusEventTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.host.on_place = accept()
        self.adapter.submit(broker_order("C1"))
        self.adapter.drain_events()

    def test_identical_status_messages_produce_one_event(self):
        for _ in range(5):
            status(self.host, 100, "Submitted", filled="3", remaining="7")
        (ev,) = self.events(OrderStatusEvent)
        self.assertEqual((ev.status, ev.filled_quantity, ev.remaining_quantity),
                         (BrokerOrderStatus.PARTIALLY_FILLED, F("3"), F("7")))
        status(self.host, 100, "Filled", filled="10", remaining="0")
        self.assertEqual([e.status for e in self.events(OrderStatusEvent)], [BrokerOrderStatus.FILLED])

    def test_warn_inactive_and_unknown_statuses(self):
        status(self.host, 100, "WarnState")
        status(self.host, 100, "Inactive")
        status(self.host, 100, "ApiPending")
        evs = self.events(OrderStatusEvent)
        self.assertEqual([e.status for e in evs], [BrokerOrderStatus.ACKNOWLEDGED, BrokerOrderStatus.INACTIVE])
        self.assertIn("WARNING", evs[0].reason)
        self.assertIn("UNREPRESENTABLE_STATUS", [a["kind"] for a in self.adapter.anomalies()])

    def test_foreign_order_status_is_namespaced(self):
        status(self.host, 3, "Submitted", perm_id=777, client_id=5)
        (ev,) = self.events(OrderStatusEvent)
        self.assertEqual((ev.client_order_id, ev.broker_order_id), (None, "perm:777"))

    def test_locate_hold_blocks_entries(self):
        status(self.host, 100, "PreSubmitted", why_held="locate")
        self.assertIn("SHORT_SALE_SIGNAL", self.adapter.entry_blocks())


# =============================================================================== executions and commissions
class ExecutionTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.host.on_place = accept()
        self.adapter.submit(broker_order("C1"))
        self.adapter.drain_events()

    def push(self, execution, contract=None):
        self.host.wrapper.execDetails(-1, contract or ib_contract(), execution)

    def test_live_execution_and_commission_rows(self):
        self.push(ib_execution("0000e0d5.6554a3c6.01.01", 100, perm_id=9100))
        self.host.wrapper.commissionAndFeesReport(ib_commission("0000e0d5.6554a3c6.01.01", 1.0))
        ev_exec, ev_comm = self.events()
        row = ev_exec.execution
        self.assertEqual(row.exec_key, f"ibkr:{ACCOUNT}:0000e0d5.6554a3c6.01.01")
        self.assertEqual((row.client_order_id, row.order_ref, row.broker_order_id, row.instrument_id),
                         ("C1", "mac-dev:1:C1", "100", "INS-AAPL"))
        self.assertEqual((row.side, row.quantity, row.price, row.currency), ("BUY", F("10"), F("101.25"), "USD"))
        self.assertEqual(row.exec_time_ns, utc_ns(2026, 10, 22, 14, 31, 5))
        self.assertEqual((row.provider_ref, row.correction_of), (str(AAPL), None))
        self.assertEqual(row.raw["evidence_level"], "BROKER_PAPER")
        self.assertNotIn(ACCOUNT, json.dumps(row.raw))
        comm = ev_comm.commission
        self.assertEqual((comm.exec_key, comm.amount, comm.currency), (row.exec_key, F("1.0"), "USD"))
        self.assertIsNone(comm.raw["realizedPNL"])               # unset double -> null, never 0

    def test_commission_before_execution_is_held_and_joined(self):
        self.host.wrapper.commissionAndFeesReport(ib_commission("0000e0d5.6554a3c6.02.01", 0.35))
        self.assertEqual(self.events(), [])
        self.push(ib_execution("0000e0d5.6554a3c6.02.01", 100, side="SLD", shares="1"))
        ev_exec, ev_comm = self.events()
        self.assertIsInstance(ev_exec, ExecutionEvent)
        self.assertEqual(ev_exec.execution.side, "SELL")
        self.assertIsInstance(ev_comm, CommissionEvent)
        self.assertEqual(ev_comm.commission.amount, F("0.35"))

    def test_corrections_link_to_the_latest_prior_revision(self):
        self.push(ib_execution("0000e0d5.6554a3c6.01.01", 100))
        self.push(ib_execution("0000e0d5.6554a3c6.01.02", 100, price=101.2))
        self.push(ib_execution("0000e0d5.6554a3c6.01.02", 100, price=101.2))      # duplicate delivery
        first, second = [e.execution for e in self.events(ExecutionEvent)]
        self.assertIsNone(first.correction_of)
        self.assertEqual(second.correction_of, first.exec_key)
        self.push(ib_execution("0000e0d5.6554a3c6.07.03", 100))                   # earlier revisions never seen
        (third,) = [e.execution for e in self.events(ExecutionEvent)]
        self.assertEqual(third.correction_of, f"ibkr:{ACCOUNT}:0000e0d5.6554a3c6.07.02")
        self.assertTrue(third.raw["correction_of_presumed"])

    def test_manual_and_foreign_executions_are_external(self):
        self.push(ib_execution("0001aaaa.6554a3c6.01.01", 0, perm_id=0, client_id=0))
        self.push(ib_execution("0001bbbb.6554a3c6.01.01", 100, perm_id=5555, client_id=5, order_ref="OTHER:1:x"))
        manual, foreign = [e.execution for e in self.events(ExecutionEvent)]
        self.assertIsNone(manual.client_order_id)
        self.assertEqual(manual.broker_order_id, "client:0:order:0:exec:0001aaaa.6554a3c6.01")
        self.assertEqual((foreign.client_order_id, foreign.broker_order_id, foreign.order_ref),
                         (None, "perm:5555", "OTHER:1:x"))
        self.assertEqual(sum(a["kind"] == "EXTERNAL_EXECUTION" for a in self.adapter.anomalies()), 2)

    def test_unparseable_execution_is_held_and_makes_history_incomplete(self):
        self.push(ib_execution("0000e0d5.6554a3c6.09.01", 100, when="20261022 10:31:05 Mars/Olympus"))
        self.assertEqual(self.events(ExecutionEvent), [])
        batch = self.adapter.executions_since(None)
        self.assertFalse(batch.complete)

    def test_executions_since_backfills_and_converges(self):
        self.host.executions = [(ib_contract(), ib_execution("0000e0d5.6554a3c6.01.01", 100),
                                 ib_commission("0000e0d5.6554a3c6.01.01", 1.0))]
        batch = self.adapter.executions_since(None)
        _, req_id, exec_filter = self.host.calls[-1]
        self.assertIsInstance(exec_filter, ExecutionFilter)
        self.assertEqual((exec_filter.clientId, exec_filter.acctCode, exec_filter.lastNDays), (0, "", 2**31 - 1))
        self.assertTrue(batch.complete)
        self.assertEqual((len(batch.executions), len(batch.commissions)), (1, 1))
        again = self.adapter.executions_since(batch.watermark)    # the reconciler's convergence check
        self.assertTrue(again.complete)
        self.assertEqual((again.executions, again.commissions), ((), ()))
        self.push(ib_execution("0000e0d5.6554a3c6.01.02", 100, shares="10", price=101.2))
        third = self.adapter.executions_since(again.watermark)
        self.assertEqual([e.provider_exec_id for e in third.executions], ["0000e0d5.6554a3c6.01.02"])
        self.assertEqual(len(self.adapter.executions_since(None).executions), 2)   # overlap is fine

    def test_missing_exec_details_end_is_incomplete(self):
        self.host.silent.add("reqExecutions")
        self.assertFalse(self.adapter.executions_since(None).complete)

    def test_history_gap_across_midnight_requires_owner_acknowledgement(self):
        other_instance_same_day = "ibkr1:feedfeedfeed:5:5:20261022"
        self.assertTrue(self.adapter.executions_since(other_instance_same_day).complete)
        yesterday = "ibkr1:feedfeedfeed:5:5:20261021"
        self.assertFalse(self.adapter.executions_since(yesterday).complete)
        self.assertIn("HISTORY_GAP", [a["kind"] for a in self.adapter.anomalies()])
        self.assertFalse(self.adapter.executions_since("17").complete)          # unrecognized watermark
        self.adapter.acknowledge_history_gap(date(2026, 10, 21), by="owner", note="statement reconciled")
        self.assertTrue(self.adapter.executions_since(yesterday).complete)

    def test_same_instance_coverage_survives_midnight_only_without_a_disconnect(self):
        wm = self.adapter.executions_since(None).watermark
        self.clock.advance(DAY_NS)                                               # continuous connection
        self.assertTrue(self.adapter.executions_since(wm).complete)
        self.host.disconnect()
        self.clock.advance(DAY_NS)
        self.connect()
        self.assertFalse(self.adapter.executions_since(wm).complete)             # executions before midnight lost


# =============================================================================== snapshots, open orders, lookup
class SnapshotTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.host.summary = [(ACCOUNT, "AccountType", "INDIVIDUAL", ""),          # fixture strings (R11 open question)
                             (ACCOUNT, "NetLiquidation", "1001012.50", "USD"),
                             (ACCOUNT, "TotalCashValue", "999000.00", "USD"),
                             (ACCOUNT, "SettledCash", "998500.00", "USD"),
                             (ACCOUNT, "AvailableFunds", "999000.00", "USD")]
        self.host.positions = [(ACCOUNT, ib_contract(), Decimal("10"), 101.25),
                               (ACCOUNT, ib_contract(MSFT, "MSFT"), Decimal("0"), 0.0),
                               (ACCOUNT, ib_contract(4242, "ZZZZ"), Decimal("3"), UNSET_DOUBLE)]

    def test_complete_snapshot_maps_settled_and_total_cash(self):
        snap = self.adapter.account_snapshot()
        self.assertTrue(snap.complete, snap.raw["incomplete_reasons"])
        (cash,) = snap.cash
        self.assertEqual((cash.currency, cash.settled, cash.total), ("USD", F("998500.00"), F("999000.00")))
        self.assertIn("SettledCash", cash.semantics)
        self.assertEqual([(p.instrument_id, p.provider_ref, p.quantity, p.average_cost) for p in snap.positions],
                         [("INS-AAPL", str(AAPL), F("10"), F("101.25")), (None, "4242", F("3"), None)])
        names = self.host.names()
        req = next(c for c in self.host.calls if c[0] == "reqAccountSummary")
        self.assertEqual(req[2], "All")
        self.assertEqual(set(req[3].split(",")),
                         {"AccountType", "NetLiquidation", "TotalCashValue", "SettledCash", "AvailableFunds"})
        self.assertIn(("cancelAccountSummary", req[1]), self.host.calls)
        self.assertIn("cancelPositions", names)
        self.assertEqual(snap.raw["summary"]["AvailableFunds:USD"], "999000.00")   # kept in raw, never as cash

    def test_incomplete_cases(self):
        self.host.silent.add("reqPositions")
        self.assertFalse(self.adapter.account_snapshot().complete)
        self.host.silent.clear()
        self.host.summary[3] = (ACCOUNT, "SettledCash", "1.7976931348623157E308", "USD")
        self.assertFalse(self.adapter.account_snapshot().complete)

    def test_account_type_mismatch_is_incomplete(self):
        summary = list(self.host.summary)
        adapter = self.make_adapter(expected_account_type="CASH-FIXTURE")
        adapter.connect()
        self.hosts[-1].summary = summary
        snap = adapter.account_snapshot()
        self.assertFalse(snap.complete)
        self.assertTrue(any("ACCOUNT_TYPE_MISMATCH" in r for r in snap.raw["incomplete_reasons"]))

    def test_never_a_third_account_summary_subscription(self):
        from unknown.broker.ibkr import IbkrNotReady
        self.host.raise_on.add("cancelAccountSummary")
        self.adapter.account_snapshot()
        self.adapter.account_snapshot()
        with self.assertRaises(IbkrNotReady):
            self.adapter.account_snapshot()
        self.assertEqual(self.host.names().count("reqAccountSummary"), 2)


class OpenOrderAndLookupTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.host.on_place = accept()
        self.adapter.submit(broker_order("C1"))
        self.adapter.drain_events()
        ours = ib_order(100, perm_id=9100, order_ref="mac-dev:1:C1")
        self.ours_row = (100, ib_contract(), ours, "Submitted", "4", "6")

    def test_open_orders_report_ours_and_foreign(self):
        self.host.open_orders = [self.ours_row]
        self.host.other_orders = [
            (3, ib_contract(MSFT, "MSFT"), ib_order(3, client_id=5, perm_id=9200, order_ref="OTHER:1:x"), "PreSubmitted",
             "0", "5"),
            (0, ib_contract(), ib_order(0, client_id=0, perm_id=9300, action="SELL"), "Submitted", "0", "2"),
            (7, ib_contract(), ib_order(7, client_id=5, perm_id=9400), "Filled", "1", "0")]
        rows, complete = self.adapter.open_orders()
        self.assertTrue(complete)
        self.assertEqual([c for c in self.host.names() if c.startswith("req")][-2:], ["reqOpenOrders",
                                                                                      "reqAllOpenOrders"])
        by_id = {r.broker_order_id: r for r in rows}
        self.assertEqual(sorted(by_id), ["100", "perm:9200", "perm:9300"])            # Filled is not open
        ours = by_id["100"]
        self.assertEqual((ours.client_order_id, ours.order_ref, ours.status, ours.filled_quantity,
                          ours.remaining_quantity), ("C1", "mac-dev:1:C1", BrokerOrderStatus.PARTIALLY_FILLED,
                                                     F("4"), F("6")))
        self.assertEqual((by_id["perm:9200"].client_order_id, by_id["perm:9200"].order_ref,
                          by_id["perm:9200"].instrument_id), (None, "OTHER:1:x", "INS-MSFT"))
        self.assertEqual(by_id["perm:9300"].side, "SELL")

    def test_open_orders_incomplete_without_end(self):
        self.host.silent.add("reqAllOpenOrders")
        self.assertFalse(self.adapter.open_orders()[1])

    def test_lookup_tri_state(self):
        self.assertEqual(self.adapter.lookup("NEVER").found, None)                   # no allocation record
        self.adapter.allocate_broker_order_id("C9")
        self.assertIs(self.adapter.lookup("C9").found, False)                         # allocated, never sent
        self.assertEqual(self.adapter.lookup("C1").found, None)                       # sent, no trace anywhere
        self.host.open_orders = [self.ours_row]
        found = self.adapter.lookup("C1")
        self.assertEqual((found.found, found.status, found.filled_quantity), (True, BrokerOrderStatus.PARTIALLY_FILLED,
                                                                              F("4")))

    def test_lookup_completed_orders_by_perm_id(self):
        done = ib_order(0, client_id=0, perm_id=9100)
        done.filledQuantity = Decimal("10")
        self.host.completed = [(ib_contract(), done, ib_state("Filled", "Filled Size: 10"))]
        found = self.adapter.lookup("C1")
        self.assertEqual((found.found, found.status, found.filled_quantity, found.remaining_quantity),
                         (True, BrokerOrderStatus.FILLED, F("10"), F("0")))
        self.assertIn(("reqCompletedOrders", True), self.host.calls)

    def test_lookup_from_executions_only(self):
        self.host.executions = [(ib_contract(), ib_execution("0000e0d5.6554a3c6.01.01", 100, shares="10"), None)]
        found = self.adapter.lookup("C1")
        self.assertEqual((found.found, found.status), (True, BrokerOrderStatus.FILLED))

    def test_lookup_when_disconnected_is_unknown(self):
        self.host.disconnect()
        self.assertIsNone(self.adapter.lookup("C1").found)


# =============================================================================== connectivity and threads
class ConnectivityTests(AdapterCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_system_codes_map_to_connectivity_events(self):
        w = self.host.wrapper
        w.error(-1, 0, 2104, "Market data farm connection is OK:usfarm", "")
        w.error(-1, 0, 2158, "Sec-def data farm connection is OK:secdefnj", "")
        self.assertEqual(self.events(), [])                                    # informational codes never alert
        w.error(-1, 0, 1100, "Connectivity between IB and the TWS has been lost.", "")
        (down,) = self.events(ConnectivityEvent)
        self.assertFalse(down.connected or down.authenticated)
        self.assertIn("UPSTREAM_DOWN 1100", down.detail)
        self.assertFalse(self.adapter.session().authenticated)
        w.error(-1, 0, 1101, "Connectivity between IB and TWS has been restored- data lost.", "")
        (up,) = self.events(ConnectivityEvent)
        self.assertTrue(up.connected and up.authenticated)
        self.assertIn("data lost", up.detail)
        w.error(-1, 0, 2110, "Connectivity between TWS and server is broken. It will be restored automatically.", "")
        self.assertIn("UPSTREAM_DOWN 2110", self.events(ConnectivityEvent)[0].detail)

    def test_connection_closed_then_reconnect(self):
        self.host.disconnect()
        (down,) = self.events(ConnectivityEvent)
        self.assertIn("SOCKET_DOWN", down.detail)
        state = self.adapter.session()
        self.assertFalse(state.connected or state.authenticated)
        with self.assertRaises(ConnectionError):
            self.adapter.open_orders()
        with self.assertRaises(ConnectionError):
            self.adapter.account_snapshot()
        self.assertTrue(self.adapter.connect().authenticated)
        self.assertEqual(len(self.hosts), 2)

    def test_socket_port_reset_1300_drops_the_session(self):
        self.host.wrapper.error(-1, 0, 1300, "TWS socket port has been reset and this connection is being dropped.", "")
        self.assertFalse(self.adapter.session().connected)

    def test_events_drain_fifo_under_concurrent_callbacks(self):
        self.host.on_place = accept()
        self.adapter.submit(broker_order("C1"))
        self.adapter.drain_events()

        def pump():
            for i in range(300):
                status(self.host, 100, "Submitted", filled=str(i), remaining=str(300 - i))
        worker = threading.Thread(target=pump)
        worker.start()
        seen = []
        while worker.is_alive():
            seen += self.adapter.drain_events(limit=7)
        worker.join()
        seen += self.adapter.drain_events()
        self.assertEqual([int(e.filled_quantity.to_str()) for e in seen], list(range(300)))


# =============================================================================== SDK conformance
class SdkConformanceTests(unittest.TestCase):
    def setUp(self):
        if not HAVE_IBAPI:
            self.skipTest(SKIP_REASON)

    @staticmethod
    def params(fn):
        return [p.name for p in inspect.signature(fn).parameters.values()]

    def test_every_eclient_method_the_adapter_calls_exists_with_this_signature(self):
        expected = {"connect": ["self", "host", "port", "clientId"], "disconnect": ["self"], "isConnected": ["self"],
                    "run": ["self"], "serverVersion": ["self"], "twsConnectionTime": ["self"],
                    "placeOrder": ["self", "orderId", "contract", "order"],
                    "cancelOrder": ["self", "orderId", "orderCancel"], "reqIds": ["self", "numIds"],
                    "reqExecutions": ["self", "reqId", "execFilter"], "reqOpenOrders": ["self"],
                    "reqAllOpenOrders": ["self"], "reqCompletedOrders": ["self", "apiOnly"],
                    "reqAccountSummary": ["self", "reqId", "groupName", "tags"],
                    "cancelAccountSummary": ["self", "reqId"], "reqPositions": ["self"], "cancelPositions": ["self"],
                    "reqCurrentTimeInMillis": ["self"]}
        for name, params in expected.items():
            self.assertEqual(self.params(getattr(EClient, name)), params, name)
            if name in FakeHost.__dict__:
                self.assertEqual(self.params(getattr(FakeHost, name)), params, f"FakeHost.{name}")

    def test_adapter_wrapper_overrides_match_ewrapper(self):
        from unknown.broker.ibkr.adapter import _wrapper_class
        cls = _wrapper_class(EWrapper)
        overridden = [n for n in cls.__dict__ if not n.startswith("_")]
        self.assertEqual(len(overridden), 19)
        for name in overridden:
            self.assertTrue(hasattr(EWrapper, name), name)
            self.assertEqual(self.params(getattr(cls, name)), self.params(getattr(EWrapper, name)), name)

    def test_sdk_pin_is_enforced(self):
        from unknown.broker.ibkr import IbkrSdkMismatch, load_sdk
        sdk = load_sdk()
        self.assertEqual(sdk.version, ibapi.__version__)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IbkrSdkMismatch):
                load_sdk(config_from_dict(base_config(tmp, sdk_version_pin="9.81.1")))
            with self.assertRaises(IbkrSdkMismatch):
                load_sdk(config_from_dict(base_config(tmp, sdk_sha256_pin="0" * 64)))
            self.assertEqual(load_sdk(config_from_dict(base_config(tmp, sdk_sha256_pin=sdk.fingerprint))).version,
                             sdk.version)


if __name__ == "__main__":
    unittest.main()
