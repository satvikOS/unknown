"""Reconnect supervision in the core service (OPS-03, ADR-0012): an adapter that owns a socket is
reconnected with capped exponential backoff; an identity-unsafe latch is never retried; adapters
without ``connect`` (the simulator) are left alone. The core never logs in for the owner."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unknown.broker.port import BrokerSessionState  # noqa: E402
from unknown.service import core as service_core  # noqa: E402
from unknown.service.core import RECONNECT_MAX_S, RECONNECT_MIN_S, CoreService  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402


class FakeSocketBroker:
    def __init__(self):
        self.connects = 0
        self.state = BrokerSessionState(False, False, "NOT_CONNECTED", None, None)

    def session(self):
        return self.state

    def connect(self):
        self.connects += 1
        return self.state


class FakeMonotonic:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.broker = FakeSocketBroker()
        self.svc = CoreService(paths=Paths(Path(tmp.name)), account_key="IBKR-PAPER-1", deployment_id="test",
                               broker=self.broker)
        self.clock = FakeMonotonic()
        original = service_core.time.monotonic
        service_core.time.monotonic = self.clock
        self.addCleanup(setattr, service_core.time, "monotonic", original)

    def test_reconnects_with_capped_exponential_backoff(self):
        self.svc._supervise_connection()
        self.assertEqual(self.broker.connects, 1)
        self.svc._supervise_connection()                      # inside the backoff window: no attempt
        self.assertEqual(self.broker.connects, 1)
        delays = []
        for _ in range(12):
            wait = self.svc._next_reconnect_at - self.clock.t
            delays.append(wait)
            self.clock.t += wait
            self.svc._supervise_connection()
        self.assertEqual(self.broker.connects, 13)
        self.assertEqual(delays[0], RECONNECT_MIN_S)
        self.assertEqual(delays[1], RECONNECT_MIN_S * 2)
        self.assertEqual(max(delays), RECONNECT_MAX_S)

    def test_connected_session_resets_backoff_and_makes_no_attempt(self):
        self.svc._supervise_connection()
        self.clock.t += 100
        self.broker.state = BrokerSessionState(True, True, None, None, None)
        self.svc._supervise_connection()
        self.assertEqual(self.broker.connects, 1)
        self.assertEqual(self.svc._reconnect_backoff_s, RECONNECT_MIN_S)

    def test_fresh_disconnect_after_a_long_outage_reconnects_promptly(self):
        for _ in range(8):                                    # a long earlier outage: backoff at the cap
            self.clock.t = self.svc._next_reconnect_at if hasattr(self.svc, "_next_reconnect_at") else self.clock.t
            self.svc._supervise_connection()
        self.assertEqual(self.svc._reconnect_backoff_s, RECONNECT_MAX_S)
        self.broker.state = BrokerSessionState(True, True, None, None, None)
        self.svc._supervise_connection()                      # connected again
        self.broker.state = BrokerSessionState(False, False, "SOCKET_DOWN", None, None)
        n = self.broker.connects
        self.svc._supervise_connection()                      # the very next cycle after the drop tries at once
        self.assertEqual(self.broker.connects, n + 1)

    def test_identity_unsafe_latch_is_never_retried(self):
        self.broker.state = BrokerSessionState(False, False, "DISCONNECTED_UNSAFE: managed accounts differ", None, None)
        for _ in range(3):
            self.clock.t += RECONNECT_MAX_S
            self.svc._supervise_connection()
        self.assertEqual(self.broker.connects, 0)

    def test_logged_out_host_is_not_reconnected_while_the_socket_is_up(self):
        # Connected but not authenticated (AUTH_REQUIRED): reconnecting cannot help; the owner must log in.
        self.broker.state = BrokerSessionState(True, False, "AUTH_REQUIRED", None, None)
        self.svc._supervise_connection()
        self.assertEqual(self.broker.connects, 0)

    def test_adapter_without_connect_is_left_alone(self):
        class NoSocket:
            def session(self):
                raise AssertionError("not consulted")
        svc = CoreService(paths=self.svc.paths, account_key="SIM-1", deployment_id="test", broker=NoSocket())
        svc._supervise_connection()


if __name__ == "__main__":
    unittest.main()
