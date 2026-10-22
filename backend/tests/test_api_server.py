"""Core socket error typing and disconnect logging (the transport under the terminal API)."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import secrets
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unknown.domain.clock import SystemClock
from unknown.domain.errors import CommandConflict
from unknown.service.core import CoreService, _simulated_broker
from unknown.service.ipc import CoreClient, CoreError, CoreSocketServer, CoreUnavailable, OutcomeUnknown
from unknown.service.paths import Paths

ACCOUNT = "SIM-1"


class IpcTypingTests(unittest.TestCase):
    """The socket's error typing: what a client (the API) maps to outcomes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unk-ipc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def serve(self, dispatch) -> CoreClient:
        srv = CoreSocketServer(self.tmp / "c.sock", {"tok": frozenset({"OWNER"})}, dispatch)
        srv.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return CoreClient(self.tmp / "c.sock", "tok", timeout_s=10)

    def test_error_types_survive_the_socket(self):
        def dispatch(req, roles):
            raise {"conflict": CommandConflict("command id x reused with a different payload"),
                   "value": ValueError("bad"), "perm": PermissionError("no")}[req["name"]]
        client = self.serve(dispatch)
        for name, expected in (("conflict", "CommandConflict"), ("value", "ValueError"), ("perm", "PermissionError")):
            with self.assertRaises(CoreError) as cm:
                client.query(name)
            self.assertEqual(cm.exception.error_type, expected)
        self.assertTrue(str(cm.exception).startswith("PermissionError: "), "message format kept for older readers")

    def test_a_missing_socket_is_core_unavailable_and_still_an_oserror(self):
        with self.assertRaises(CoreUnavailable) as cm:
            CoreClient(self.tmp / "absent.sock", "tok").query("health")
        self.assertIsInstance(cm.exception, OSError)

    def test_the_core_wait_timing_out_is_outcome_unknown(self):
        svc = CoreService(paths=Paths(self.tmp / "h"), account_key=ACCOUNT, deployment_id="test", broker=None)
        svc.request_wait_s = 0.2     # nothing drains the queue: the request stays queued
        client = self.serve(svc._enqueue)
        with self.assertRaises(OutcomeUnknown) as cm:
            client.command("ui.x.1", "stop", {}, {"id": "owner", "role": "OWNER"})
        self.assertIn("still queued", str(cm.exception))
        self.assertEqual(svc.requests.qsize(), 1)

    def test_a_reply_that_never_comes_is_outcome_unknown(self):
        path = self.tmp / "mute.sock"
        mute = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        mute.bind(str(path))
        mute.listen(1)
        self.addCleanup(mute.close)

        def swallow():
            conn, _ = mute.accept()
            conn.makefile("rb").readline()
            conn.close()
        threading.Thread(target=swallow, daemon=True).start()
        with self.assertRaises(OutcomeUnknown):
            CoreClient(path, "tok").command("ui.x.2", "stop", {}, {"id": "owner", "role": "OWNER"})


class CoreDisconnectLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unk-core-"))
        self.paths = Paths(self.tmp / "home")
        self.core = CoreService(paths=self.paths, account_key=ACCOUNT, deployment_id="test",
                                broker=_simulated_broker(SystemClock(), "300.00"))
        thread = threading.Thread(target=self.core.run, daemon=True)
        thread.start()
        self.assertTrue(self.core.ready.wait(30))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: (self.core.shutdown(), thread.join(30)))

    def test_a_client_that_hangs_up_before_the_reply_costs_one_log_line_and_no_traceback(self):
        stderr = io.StringIO()
        cid = f"ui.test.hangup.{secrets.token_hex(4)}"
        with contextlib.redirect_stderr(stderr), self.assertLogs("unknown.core.ipc", logging.WARNING) as logs:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:   # connect and leave without a request
                s.connect(str(self.paths.socket))
            request = {"token": self.paths.token("api"), "op": "command", "command_id": cid,
                       "kind": "suspend_session", "payload": {"session_id": "nope"},
                       "principal": {"id": "owner", "role": "OWNER"}}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(str(self.paths.socket))
                s.sendall((json.dumps(request) + "\n").encode())
            deadline = time.monotonic() + 10
            while not any(cid in m for m in logs.output) and time.monotonic() < deadline:
                time.sleep(0.05)
        lines = [m for m in logs.output if cid in m]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("disconnected before the reply", lines[0])
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(any("Traceback" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
