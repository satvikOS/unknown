"""IB Gateway drop and return (CAP-IBKR-07, OPS-03), as seen live on 2026-10-24: weekend maintenance sends IBKR 1100,
then the Gateway closes the API socket and is unreachable (502) for a while, then comes back. The core (order
adapter), the core's market-data maintainer and the market-data bus source must each report the outage in health
and reconnect with backoff when the Gateway returns, without a process restart and without logging in.

The market-data and core-supervision tests use the pure fake IBKR host of test_ibkr_marketdata / test_service_supervisor;
the order-adapter test uses the official ibapi classes (IBKR venv) through the FakeHost of test_ibkr_adapter.
Accounts, conIds and times are TEST FIXTURES."""
from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unknown.broker.ibkr.mdsource import IbkrSource  # noqa: E402
from unknown.broker.ibkr.reconnect import ReconnectBackoff  # noqa: E402
from unknown.broker.ibkr.serve import MarketDataMaintainer  # noqa: E402
from unknown.broker.port import BrokerSessionState  # noqa: E402
from unknown.marketdata import config as C  # noqa: E402
from unknown.service import core as service_core  # noqa: E402
from unknown.service.core import RECONNECT_MIN_S, CoreService  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402

from test_ibkr_marketdata import CON_A, PureHost, SessionCase, attrib  # noqa: E402

import test_ibkr_adapter as TA  # noqa: E402

UPSTREAM_LOST = "Connectivity between IB and Trader Workstation has been lost."


def setUpModule():
    logging.getLogger("unknown.broker.ibkr").setLevel(logging.CRITICAL)
    logging.getLogger("unknown.ibkr.serve").setLevel(logging.CRITICAL)
    logging.getLogger("unknown.service").setLevel(logging.CRITICAL)


class Gateway:
    """IB Gateway as the tests see it: up (accepting API sockets, logged in) or down (maintenance or restart)."""

    def __init__(self):
        self.up = True
        self.handshake = True


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def gateway_host(gateway: Gateway):
    class GatewayHost(PureHost):
        def connect(self, host, port, clientId):
            if not gateway.up:
                self.calls.append(("connect", host, port, clientId))
                self.wrapper.error(-1, 0, 502, "Couldn't connect to TWS.", "")
                return
            self.handshake = gateway.handshake
            super().connect(host, port, clientId)

        def reqTickByTickData(self, reqId, contract, tickType, numberOfTicks, ignoreSize):
            self._hook("reqTickByTickData", reqId, tickType)

        def cancelTickByTickData(self, reqId):
            self._hook("cancelTickByTickData", reqId)

    return GatewayHost


class GatewayCase(SessionCase):
    def setUp(self):
        self.gateway = Gateway()
        self.host_cls = gateway_host(self.gateway)
        self.mono = Mono()
        super().setUp()

    def backoff(self) -> ReconnectBackoff:
        return ReconnectBackoff(monotonic=self.mono, now_ns=self.clock.now_ns)

    def drop(self, *, upstream_first=True):
        """Weekend maintenance: 1100 on the open socket, then the Gateway closes it and stays down."""
        if upstream_first:
            self.w.error(-1, 0, 1100, UPSTREAM_LOST, "")
        self.gateway.up = False
        self.host.disconnect()

    def live(self, req_id):
        self.w.marketDataType(req_id, 1)
        self.w.tickPrice(req_id, 1, 101.25, attrib())
        self.w.tickSize(req_id, 0, Decimal("300"))
        self.w.tickPrice(req_id, 2, 101.27, attrib())
        self.w.tickSize(req_id, 3, Decimal("200"))


# =============================================================================== the read-only market-data session
class SessionDropAndReturnTests(GatewayCase):
    def test_1100_then_closed_socket_then_return_is_live_again_without_a_restart(self):
        self.connect()
        req = self.subscribe()
        self.live(req)
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "LIVE")
        self.drop()
        state = self.session.connection_state()
        self.assertEqual((state["connected"], state["upstream_down"]), (False, 1100))
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "STALE")
        self.assertIn("502", self.session.connect()["reason"])          # still in maintenance
        self.gateway.up = True
        state = self.session.connect()
        # The 1100 belonged to the old socket: carried over, it would keep every quote stale forever.
        self.assertEqual((state["verified_paper"], state["upstream_down"], state["connections"]), (True, None, 2))
        self.assertEqual(self.session.maintain()["resubscribed"], ["INS-FXA"])
        self.assertIn("reqMktData", self.host.names())
        self.live(req)
        self.assertEqual(self.session.subscription("INS-FXA")["status"], "LIVE")

    def test_a_socket_closed_without_a_callback_is_replaced_on_connect(self):
        self.connect()
        self.host.connected = False                                   # EReader died; no connectionClosed delivered
        state = self.session.connect()
        self.assertTrue(state["verified_paper"], state)
        self.assertEqual(len(self.hosts), 2)

    def test_a_socket_opened_before_login_is_replaced_once_the_gateway_is_logged_in(self):
        self.gateway.handshake = False                                # API socket accepted, no account yet
        self.assertIn("AUTH_REQUIRED", self.session.connect()["reason"])
        self.gateway.handshake = True
        state = self.session.connect()
        self.assertTrue(state["verified_paper"], state)
        self.assertEqual(self.hosts[0].names()[-1], "disconnect")

    def test_identity_latch_is_never_reconnected(self):
        self.session = self.make_session(host_kwargs={"accounts": "DUOTHER1"})
        self.assertTrue(self.session.connect()["reason"].startswith("DISCONNECTED_UNSAFE"))
        self.session.connect()
        self.assertEqual(len(self.hosts), 1)                          # the latch is never retried


# =============================================================================== the core's market-data maintainer
class FakeSvc:
    instrument_details = {str(CON_A): ("INS-FXA", "USD")}


class MaintainerTests(GatewayCase):
    def test_drop_is_reported_in_health_and_reconnects_with_backoff_when_the_gateway_returns(self):
        m = MarketDataMaintainer(self.session, FakeSvc(), backoff=self.backoff())
        self.assertTrue(m.step()["connected"])
        self.assertEqual(m.health()["status"], "CONNECTED")
        self.live(self.session.subscription("INS-FXA")["req_id"])
        self.w.error(-1, 0, 1100, UPSTREAM_LOST, "")
        h = m.health()
        self.assertEqual((h["status"], h["upstream_down"]), ("UPSTREAM_DOWN", 1100))
        self.assertIn("1100", h["reason"])
        self.drop(upstream_first=False)
        self.assertFalse(m.step()["connected"])                      # immediate attempt: Gateway still down
        h = m.health()
        self.assertEqual((h["status"], h["reconnect"]["failed_attempts"], h["reconnect"]["next_attempt_in_s"]),
                         ("RECONNECTING", 1, RECONNECT_MIN_S))
        self.assertIn("502", h["reason"])
        hosts = len(self.hosts)
        m.step()                                                      # inside the backoff window: no attempt
        self.assertEqual(len(self.hosts), hosts)
        self.mono.t += RECONNECT_MIN_S
        m.step()
        self.assertEqual((len(self.hosts), m.health()["reconnect"]["backoff_s"]), (hosts + 1, 4 * RECONNECT_MIN_S))
        self.gateway.up = True
        self.mono.t += 2 * RECONNECT_MIN_S
        out = m.step()
        self.assertTrue(out["connected"], out)
        self.assertEqual(out["maintenance"]["resubscribed"], ["INS-FXA"])
        h = m.health()
        self.assertEqual((h["status"], h["upstream_down"], h["reconnect"]["reconnects"]), ("CONNECTED", None, 1))

    def test_core_health_includes_the_maintainer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = CoreService(paths=Paths(Path(tmp.name)), account_key="IBKR-PAPER-1", deployment_id="test",
                          broker=SocketBroker())
        self.assertIsNone(svc._market_data_health())
        m = MarketDataMaintainer(self.session, FakeSvc(), backoff=self.backoff())
        svc.market_data_health = m.health
        self.assertEqual(svc._market_data_health()["status"], "RECONNECTING")
        m.step()
        self.assertEqual(svc._market_data_health()["status"], "CONNECTED")


# =============================================================================== the market-data bus source
class Rec:
    def __init__(self):
        self.ev = []

    def quote(self, sid, key, fields, **kw):
        self.ev.append(("quote", key))

    def trade(self, sid, key, **kw):
        self.ev.append(("trade", key))

    def depth(self, sid, key, **kw):
        self.ev.append(("depth", key))

    def status(self, sid, key, state, reason):
        self.ev.append(("status", key, state, reason))


class BusSourceTests(GatewayCase):
    def test_drop_and_return_reconnects_with_backoff_and_reopens_time_and_sales(self):
        src = IbkrSource(self.session, lines=40, venue_lookup=C.venue, tick_by_tick=True, run_thread=False,
                         backoff=self.backoff())
        rec = Rec()
        src.start(rec)
        self.assertTrue(src.step()["connected"])
        self.assertTrue(src.subscribe("AAPL.XNAS").ok)
        self.assertIn("reqTickByTickData", self.host.names())
        self.assertEqual(src.describe()["state"], "running")
        self.w.error(-1, 0, 1100, UPSTREAM_LOST, "")
        self.assertEqual((src.describe()["state"], src.describe()["upstream_down"]), ("upstream_down", 1100))
        self.drop(upstream_first=False)
        self.assertIn(("status", "AAPL.XNAS", "STALE"), [e[:3] for e in rec.ev])
        self.assertFalse(src.step()["connected"])
        d = src.describe()
        self.assertEqual((d["state"], d["reconnect"]["failed_attempts"]), ("reconnecting", 1))
        self.assertIn("502", d["reason"])
        hosts = len(self.hosts)
        src.step()
        self.assertEqual(len(self.hosts), hosts)                     # backoff: no attempt before it is due
        self.gateway.up = True
        self.mono.t += RECONNECT_MIN_S
        out = src.step()
        self.assertTrue(out["connected"], out)
        self.assertEqual(out["reopened"], ["AAPL.XNAS"])
        self.assertEqual(out["maintenance"]["resubscribed"], ["AAPL.XNAS"])
        self.assertEqual([n for n in self.host.names() if n.startswith("req")].count("reqTickByTickData"), 1)
        d = src.describe()
        self.assertEqual((d["state"], d["reason"], d["reconnect"]["reconnects"]), ("running", None, 1))


# =============================================================================== the core (order adapter supervision)
class SocketBroker:
    """A socket-owning broker whose session the test sets (see test_service_supervisor)."""

    def __init__(self):
        self.state = BrokerSessionState(False, False, "NOT_CONNECTED", None, None)
        self.connects = 0

    def session(self):
        return self.state

    def connect(self):
        self.connects += 1
        return self.state


class CoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = Paths(Path(tmp.name))
        self.mono = Mono()
        original = service_core.time.monotonic
        service_core.time.monotonic = self.mono
        self.addCleanup(setattr, service_core.time, "monotonic", original)


class CoreHealthTests(CoreCase):
    def test_health_reports_reason_attempts_and_next_attempt(self):
        broker = SocketBroker()
        svc = CoreService(paths=self.paths, account_key="IBKR-PAPER-1", deployment_id="test", broker=broker)
        broker.state = BrokerSessionState(False, False, "SOCKET_DOWN: connectionClosed", None, None)
        svc._supervise_connection()
        bc = svc.broker_connection()
        self.assertEqual((bc["state"], bc["reason"], bc["attempts"], bc["next_attempt_in_s"]),
                         ("reconnecting", "SOCKET_DOWN: connectionClosed", 1, RECONNECT_MIN_S))
        self.assertIsNotNone(bc["down_since_ns"])
        broker.state = BrokerSessionState(True, False, "UPSTREAM_DOWN: IBKR 1100", None, None)
        self.assertEqual(svc.broker_connection()["state"], "waiting_for_gateway")
        broker.state = BrokerSessionState(False, False, "DISCONNECTED_UNSAFE: managed accounts changed", None, None)
        self.assertEqual(svc.broker_connection()["state"], "latched")
        broker.state = BrokerSessionState(True, True, None, None, None)
        svc._supervise_connection()
        bc = svc.broker_connection()
        self.assertEqual((bc["state"], bc["reason"], bc["attempts"], bc["down_since_ns"]), ("connected", None, 0, None))

    def test_simulator_has_no_connection_view(self):
        class NoSocket:
            def session(self):
                return BrokerSessionState(True, True, None, None, None)
        svc = CoreService(paths=self.paths, account_key="SIM-1", deployment_id="test", broker=NoSocket())
        self.assertIsNone(svc.broker_connection())


@unittest.skipUnless(TA.HAVE_IBAPI, TA.SKIP_REASON)
class AdapterDropAndReturnTests(CoreCase):
    """The core supervising the real IbkrAdapter over the official ibapi classes [IBKR venv]."""

    def test_core_reconnects_the_adapter_after_1100_and_a_gateway_restart(self):
        from unknown.broker.ibkr import IbkrAdapter
        from unknown.broker.ibkr.config import config_from_dict
        from unknown.domain.clock import ManualClock
        gateway = Gateway()
        hosts = []

        def factory(wrapper):
            host = TA.FakeHost(wrapper, reachable=gateway.up)
            hosts.append(host)
            return host

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        adapter = IbkrAdapter(config_from_dict(TA.base_config(tmp.name)), instrument_resolver={}.get,
                              clock=ManualClock(TA.T0), client_factory=factory)
        svc = CoreService(paths=self.paths, account_key="IBKR-PAPER-1", deployment_id="test", broker=adapter)
        svc._supervise_connection()
        self.assertEqual(svc.broker_connection()["state"], "connected")
        hosts[-1].wrapper.error(-1, 0, 1100, UPSTREAM_LOST, "")
        bc = svc.broker_connection()
        self.assertEqual(bc["state"], "waiting_for_gateway")
        self.assertIn("UPSTREAM_DOWN", bc["reason"])
        gateway.up = False
        hosts[-1].disconnect()                                        # the Gateway restarts: socket closed
        svc._supervise_connection()
        bc = svc.broker_connection()
        self.assertEqual((bc["state"], bc["attempts"]), ("reconnecting", 1))
        self.assertIn("HOST_DOWN", bc["reason"])
        svc._supervise_connection()
        self.assertEqual(len(hosts), 2)                               # inside the backoff window
        gateway.up = True
        self.mono.t += RECONNECT_MIN_S
        svc._supervise_connection()
        bc = svc.broker_connection()
        self.assertEqual((bc["state"], bc["reason"], len(hosts)), ("connected", None, 3))
        self.assertTrue(adapter.session().authenticated)


if __name__ == "__main__":
    unittest.main()
