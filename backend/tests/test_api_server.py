"""Terminal API gaps, exercised over HTTP against a real core service and API (in-process threads, a real Unix
socket, a real SQLite database, a temporary runtime home and a free TCP port):

1. a reused command id with another payload or principal is HTTP 409 COMMAND_ID_CONFLICT, never 503;
2. GET /api/commands/<id> returns the owner's stored command result, so an unknown outcome is resolved
   without resending (and a lost reply is HTTP 504 COMMAND_OUTCOME_UNKNOWN, never "not executed"; a failure
   after the core recorded the command returns the stored result with ``post_commit_error``);
3. the step-up window is the active policy's reauth_max_age_s, as the core checks it (15 minutes only
   without a policy), the shorter of the API account's and the commanded account's (a payload field the
   core ignores cannot lengthen it);
4. every *_ns integer also comes as an exact *_ns_str decimal string;
5. path segments are percent-decoded after splitting, so ids with reserved characters are addressable;
6. the state stream sends ``id: <seq>`` and ``retry:``, and resumes from Last-Event-ID;
7. the capability register has an ETag, answers If-None-Match with 304 and gzips on request;
8. the core logs one line, never a traceback, when a socket client disconnects mid-request;
9. plugin GET handlers map ValueError to 400 (and every other failure to a JSON error, not a dropped connection).
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import http.client
import io
import json
import logging
import secrets
import select
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from unknown.api import queries
from unknown.api import server as api_server
from unknown.api.auth import write_owner_record
from unknown.api.server import ApiContext, accepts_gzip, etag_matches, make_server, path_parts
from unknown.domain.clock import SystemClock
from unknown.domain.errors import CommandConflict
from unknown.service.core import CoreService, _simulated_broker
from unknown.service.ipc import CoreClient, CoreError, CoreSocketServer, CoreUnavailable, OutcomeUnknown
from unknown.service.paths import Paths

REPO = Path(__file__).resolve().parents[2]
PASSPHRASE = "correct horse battery staple, api tests"
ACCOUNT = "SIM-1"
SHORT_ACCOUNT = "SIM-2"
NS = 1_000_000_000
MINUTE = 60 * NS


def _policy(reauth_max_age_s: int, name: str) -> dict:
    body = json.loads((REPO / "config" / "policies" / "simulated-default.v1.json").read_text())
    body["name"] = name
    body["account_limits"]["reauth_max_age_s"] = reauth_max_age_s
    return body


def _hex() -> str:
    return secrets.token_hex(6)


class Stack:
    """One core service and one API server sharing a temporary runtime home."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unk-api-"))
        self.paths = Paths(self.tmp / "home")
        self.paths.ensure()
        write_owner_record(self.paths.secrets / "owner.json", PASSPHRASE)
        self.core = CoreService(paths=self.paths, account_key=ACCOUNT, deployment_id="test",
                                broker=_simulated_broker(SystemClock(), "300.00"))
        self.core_thread = threading.Thread(target=self.core.run, name="test-core", daemon=True)
        self.core_thread.start()
        if not self.core.ready.wait(30):
            raise RuntimeError("core did not start")
        self.ctx = ApiContext(self.paths, ACCOUNT, None, None, False)
        self.server = make_server(self.ctx, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.api_thread = threading.Thread(target=self.server.serve_forever, name="test-api", daemon=True)
        self.api_thread.start()
        self.client = CoreClient(self.paths.socket, self.paths.token("api"))
        self.policy_hash = self._bootstrap(ACCOUNT, _policy(3600, "api-tests-3600"))
        self.short_policy_hash = self._bootstrap(SHORT_ACCOUNT, _policy(60, "api-tests-60"))

    def _bootstrap(self, account: str, policy: dict) -> str:
        owner = {"id": "owner", "role": "OWNER", "authenticated_at_ns": time.time_ns()}
        r = self.client.command(f"test.register.{account}", "register_account",
                                {"account_key": account, "provider": "simulated", "mode": "SIMULATED", "base_currency": "USD"},
                                owner)
        assert r["accepted"], r
        r = self.client.command(f"test.publish.{account}", "publish_policy", {"body": policy}, owner)
        assert r["accepted"], r
        policy_hash = r["body"]["policy_hash"]
        r = self.client.command(f"test.activate.{account}", "activate_policy",
                                {"account_key": account, "policy_hash": policy_hash}, owner)
        assert r["accepted"], r
        return policy_hash

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.api_thread.join(10)
        self.core.shutdown()
        self.core_thread.join(30)
        shutil.rmtree(self.tmp, ignore_errors=True)


class LiveCase(unittest.TestCase):
    stack: Stack

    @classmethod
    def setUpClass(cls):
        cls.stack = Stack()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    # ------------------------------------------------------------ HTTP helpers
    def request(self, method: str, path: str, body=None, *, cookie=None, csrf=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.stack.port, timeout=60)
        h = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = cookie
        if csrf:
            h["X-CSRF-Token"] = csrf
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw)
        conn.close()
        return out

    def call(self, method: str, path: str, body=None, **kw):
        status, headers, raw = self.request(method, path, body, **kw)
        return status, (json.loads(raw) if raw else None), headers

    def login(self) -> tuple[str, str]:
        status, data, headers = self.call("POST", "/api/auth/login", {"passphrase": PASSPHRASE})
        self.assertEqual(status, 200, data)
        return headers["set-cookie"].split(";")[0], data["csrf_token"]

    def age_session(self, cookie: str, age_ns: int) -> int:
        """Make the session's last authentication ``age_ns`` old; returns the new authenticated_at_ns."""
        session = self.stack.ctx.auth.sessions[cookie.split("=", 1)[1]]
        session.authenticated_at_ns = time.time_ns() - age_ns
        return session.authenticated_at_ns

    def command(self, cookie, csrf, kind, payload, command_id=None):
        body = {"kind": kind, "payload": payload}
        if command_id is not None:
            body["command_id"] = command_id
        return self.call("POST", "/api/commands", body, cookie=cookie, csrf=csrf)


# ---------------------------------------------------------------------------------------------- 1
class CommandConflictTests(LiveCase):
    def test_reused_id_with_another_payload_is_409_with_a_stable_code(self):
        cookie, csrf = self.login()
        cid = f"ui.test.conflict.{_hex()}"
        status, first, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "no-such-session-1"}, cid)
        self.assertEqual(status, 200, first)
        self.assertEqual((first["accepted"], first["body"]["code"]), (False, "UNKNOWN_SESSION"))
        status, err, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "no-such-session-2"}, cid)
        self.assertEqual(status, 409, err)
        self.assertEqual(err["code"], "COMMAND_ID_CONFLICT")
        self.assertEqual((err["command_id"], err["outcome"], err["difference"]), (cid, "NOT_EXECUTED", "payload"))
        self.assertNotIn("core unavailable", err["error"])
        status, again, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "no-such-session-1"}, cid)
        self.assertEqual(status, 200, again)
        self.assertTrue(again["duplicate"])

    def test_reused_id_from_another_principal_is_409_and_its_result_is_never_disclosed(self):
        cookie, csrf = self.login()
        cid = f"ui.test.principal.{_hex()}"
        payload = {"session_id": "no-such-session"}
        other = {"id": "owner.other-client", "role": "OWNER", "authenticated_at_ns": time.time_ns()}
        self.stack.client.command(cid, "suspend_session", payload, other)
        status, err, _ = self.command(cookie, csrf, "suspend_session", payload, cid)
        self.assertEqual(status, 409, err)
        self.assertEqual((err["code"], err["difference"]), ("COMMAND_ID_CONFLICT", "principal"))
        status, lookup, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual(status, 404, lookup)
        self.assertEqual(lookup["code"], "COMMAND_NOT_FOUND")
        self.assertNotIn("body", lookup)

    def test_other_core_refusals_are_typed_not_core_unavailable(self):
        cookie, csrf = self.login()
        status, err, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "x"}, "bad id with spaces")
        self.assertEqual(status, 400, err)
        self.assertEqual((err["code"], err["outcome"]), ("COMMAND_INVALID", "NOT_EXECUTED"))
        status, err, _ = self.command(cookie, csrf, "broker_admin_action",
                                      {"account_key": "ANOTHER-ACCOUNT", "action": "clear_entry_block", "note": "n"})
        self.assertEqual(status, 400, err)   # the core's ValueError: it acts only on its own account
        status, err, _ = self.command(cookie, csrf, ["not", "a", "kind"], {})
        self.assertEqual((status, err["code"]), (403, "COMMAND_NOT_AVAILABLE"))


# ---------------------------------------------------------------------------------------------- 2
class _LossyProxy:
    """A core socket stand-in that forwards each request to the real core and then drops the reply, as a
    connection lost after the core executed the command would."""

    def __init__(self, target: Path, path: Path):
        self.target, self.path = str(target), str(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(4)
        self.forwarded = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile("rb").readline()
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as up:
                    up.connect(self.target)
                    up.sendall(line)
                    up.makefile("rb").readline()   # the core's reply, discarded
                self.forwarded += 1

    def close(self):
        self.sock.close()


class CommandResultTests(LiveCase):
    def test_returns_status_kind_body_and_event_seqs(self):
        cookie, csrf = self.login()
        cid = f"ui.test.result.{_hex()}"
        status, sent, _ = self.command(cookie, csrf, "publish_policy", {"body": _policy(1800, f"result-{_hex()}")}, cid)
        self.assertEqual(status, 200, sent)
        self.assertTrue(sent["accepted"])
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual(status, 200, got)
        self.assertEqual((got["command_id"], got["kind"], got["status"], got["accepted"]),
                         (cid, "publish_policy", "ACCEPTED", True))
        self.assertEqual(got["body"], sent["body"])
        self.assertEqual(got["events"], sent["events"])
        self.assertTrue(got["events"])
        self.assertEqual(got["source"], "core")
        self.assertEqual(got["recorded_time_ns_str"], str(got["recorded_time_ns"]))
        status, events, _ = self.call("GET", f"/api/events?after={got['events'][0] - 1}&limit=5", cookie=cookie)
        self.assertEqual([e["command_id"] for e in events if e["seq"] in got["events"]], [cid] * len(got["events"]))

    def test_a_rejection_is_a_stored_result_too(self):
        cookie, csrf = self.login()
        cid = f"ui.test.rejected.{_hex()}"
        _, sent, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "nope"}, cid)
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual(status, 200, got)
        self.assertEqual((got["status"], got["accepted"], got["body"]["code"]), ("REJECTED", False, "UNKNOWN_SESSION"))
        self.assertEqual(got["events"], sent["events"])

    def test_an_unstored_refusal_under_the_same_id_is_not_part_of_its_events(self):
        cookie, csrf = self.login()
        cid = f"ui.test.refused-later.{_hex()}"
        _, sent, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "nope"}, cid)
        # the same principal reuses the id for a kind it may not issue: refused before dedup, recorded, never stored
        refused = self.stack.client.command(cid, "broker_execution", {}, {"id": "owner", "role": "OWNER"})
        self.assertEqual((refused["accepted"], refused["body"]["code"]), (False, "UNAUTHORIZED"))
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual(status, 200, got)
        self.assertEqual((got["kind"], got["events"]), ("suspend_session", sent["events"]))
        self.assertNotIn(refused["events"][0], got["events"])
        self.assertGreater(refused["events"][0], max(sent["events"]))

    def test_an_unknown_id_is_a_definitive_404_from_the_core(self):
        cookie, _ = self.login()
        status, got, _ = self.call("GET", f"/api/commands/ui.never.sent.{_hex()}", cookie=cookie)
        self.assertEqual(status, 404, got)
        self.assertEqual((got["code"], got["source"], got["definitive"]), ("COMMAND_NOT_FOUND", "core", True))

    def test_an_id_this_api_is_still_forwarding_is_never_a_definitive_not_found(self):
        """Review 2026-10-22: a POST another API thread has received but not yet handed to the core is not in the
        core's queue, so the core's "not found" would be wrong. While this API forwards the id, the lookup says so
        and is never definitive; afterwards the core's answer is final again."""
        cookie, _ = self.login()
        cid = f"ui.inflight.{_hex()}"
        self.stack.ctx.begin_forward(cid)
        try:
            status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
            self.assertEqual((status, got["code"], got["definitive"], got.get("in_flight")), (404, "COMMAND_NOT_FOUND", False, True))
        finally:
            self.stack.ctx.end_forward(cid)
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual((status, got["definitive"]), (404, True))

    def test_requires_the_owner_session(self):
        status, got, _ = self.call("GET", "/api/commands/anything")
        self.assertEqual((status, got["code"]), (401, "LOGIN_REQUIRED"))

    def test_without_the_core_the_database_answers_and_not_found_is_provisional(self):
        cookie, csrf = self.login()
        cid = f"ui.test.dbfallback.{_hex()}"
        self.command(cookie, csrf, "suspend_session", {"session_id": "nope"}, cid)
        real = self.stack.ctx.core
        self.stack.ctx.core = CoreClient(self.stack.tmp / "no-core.sock", "token")
        self.addCleanup(setattr, self.stack.ctx, "core", real)
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual((status, got["source"], got["status"]), (200, "database", "REJECTED"))
        status, got, _ = self.call("GET", f"/api/commands/ui.never.{_hex()}", cookie=cookie)
        self.assertEqual((status, got["source"], got["definitive"]), (404, "database", False))

    def test_a_lost_reply_is_504_outcome_unknown_and_the_lookup_resolves_it_without_resending(self):
        cookie, csrf = self.login()
        proxy = _LossyProxy(self.stack.paths.socket, self.stack.tmp / "lossy.sock")
        self.addCleanup(proxy.close)
        real = self.stack.ctx.core
        self.stack.ctx.core = CoreClient(proxy.path, self.stack.paths.token("api"))
        cid = f"ui.test.lost.{_hex()}"
        try:
            status, err, _ = self.command(cookie, csrf, "publish_policy", {"body": _policy(1700, f"lost-{_hex()}")}, cid)
        finally:
            self.stack.ctx.core = real
        self.assertEqual(status, 504, err)
        self.assertEqual((err["code"], err["outcome"], err["command_id"]), ("COMMAND_OUTCOME_UNKNOWN", "UNKNOWN", cid))
        self.assertEqual(proxy.forwarded, 1)
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual(status, 200, got)
        self.assertEqual((got["status"], got["kind"]), ("ACCEPTED", "publish_policy"))
        self.assertIn("policy_hash", got["body"])


class IpcTypingTests(unittest.TestCase):
    """The socket's error typing, without HTTP: what the API maps to status codes."""

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


# ---------------------------------------------------------------------------------------------- 3
class StepUpWindowTests(LiveCase):
    def preflight_blockers(self, authenticated_at_ns: int) -> list[str]:
        return self.stack.client.query("preflight", authenticated_at_ns=authenticated_at_ns)["blockers"]

    def new_policy(self) -> dict:
        return {"body": _policy(1200, f"stepup-{_hex()}")}

    def test_the_active_policy_sets_the_window_as_the_core_checks_it(self):
        cookie, csrf = self.login()
        auth = self.age_session(cookie, 20 * MINUTE)   # past the old fixed 15 minutes, inside the policy's 3600 s
        status, ok, _ = self.command(cookie, csrf, "publish_policy", self.new_policy())
        self.assertEqual(status, 200, ok)
        self.assertNotIn("OWNER_REAUTHENTICATION_REQUIRED", self.preflight_blockers(auth))
        auth = self.age_session(cookie, 61 * MINUTE)
        status, err, _ = self.command(cookie, csrf, "publish_policy", self.new_policy())
        self.assertEqual(status, 428, err)
        self.assertEqual(err["error"], "re-authentication required for this command")
        self.assertEqual((err["code"], err["outcome"]), ("REAUTH_REQUIRED", "NOT_EXECUTED"))
        self.assertEqual((err["step_up"]["max_age_s"], err["step_up"]["basis"], err["step_up"]["policy_hash"]),
                         (3600, "policy", self.stack.policy_hash))
        self.assertIn("OWNER_REAUTHENTICATION_REQUIRED", self.preflight_blockers(auth))

    def test_the_policy_of_the_named_account_governs(self):
        cookie, csrf = self.login()
        payload = {"account_key": SHORT_ACCOUNT, "amount": "1.00"}
        self.age_session(cookie, 90 * NS)
        status, err, _ = self.command(cookie, csrf, "authorize_capital", payload)
        self.assertEqual(status, 428, err)
        self.assertEqual((err["step_up"]["max_age_s"], err["step_up"]["policy_hash"]), (60, self.stack.short_policy_hash))
        self.age_session(cookie, 30 * NS)
        status, ok, _ = self.command(cookie, csrf, "authorize_capital", payload)
        self.assertEqual(status, 200, ok)   # the core decides the command itself

    def test_fifteen_minutes_only_without_an_active_policy(self):
        cookie, csrf = self.login()
        payload = {"account_key": "SIM-NO-POLICY", "amount": "1.00"}
        self.age_session(cookie, 16 * MINUTE)
        status, err, _ = self.command(cookie, csrf, "authorize_capital", payload)
        self.assertEqual(status, 428, err)
        self.assertEqual((err["step_up"]["max_age_s"], err["step_up"]["policy_hash"]), (900, None))
        self.assertTrue(err["step_up"]["basis"].startswith("fallback"))
        self.age_session(cookie, 14 * MINUTE)
        status, ok, _ = self.command(cookie, csrf, "authorize_capital", payload)
        self.assertEqual(status, 200, ok)
        self.assertFalse(ok["accepted"])   # the core rejects the unknown account

    def test_the_session_view_reports_the_window(self):
        cookie, _ = self.login()
        auth = self.age_session(cookie, 5 * MINUTE)
        status, s, _ = self.call("GET", "/api/auth/session", cookie=cookie)
        self.assertEqual(status, 200, s)
        self.assertEqual((s["step_up"]["max_age_s"], s["step_up"]["basis"], s["step_up"]["account_key"]),
                         (3600, "policy", ACCOUNT))
        self.assertEqual(s["step_up"]["expires_at_ns"], auth + 3600 * NS)
        self.assertEqual(s["step_up"]["expires_at_ns_str"], str(auth + 3600 * NS))

    def test_the_harness_cap_can_only_shorten_the_window(self):
        cookie, csrf = self.login()
        self.addCleanup(setattr, api_server, "STEP_UP_MAX_AGE_NS", api_server.STEP_UP_MAX_AGE_NS)
        api_server.STEP_UP_MAX_AGE_NS = 5 * NS
        self.age_session(cookie, 10 * NS)
        status, err, _ = self.command(cookie, csrf, "publish_policy", self.new_policy())
        self.assertEqual((status, err["step_up"]["max_age_s"]), (428, 5))
        api_server.STEP_UP_MAX_AGE_NS = 10 * 3600 * NS
        self.age_session(cookie, 61 * MINUTE)
        status, err, _ = self.command(cookie, csrf, "publish_policy", self.new_policy())
        self.assertEqual((status, err["step_up"]["max_age_s"], err["step_up"]["basis"]), (428, 3600, "policy"))


class StepUpTargetTests(LiveCase):
    """The step-up window is the shortest of the API account's policy and the policy of the account the core will
    act on, found where the core's handler finds it. A payload field the handler ignores cannot widen it."""

    def system_command(self, kind: str, payload: dict) -> dict:
        """Run a SYSTEM command on the core's loop thread (as the gateway would); test plumbing only."""
        req = {"op": "command", "command_id": f"sys.test.{kind}.{_hex()}", "kind": kind, "payload": payload,
               "principal": {"id": "system.test", "role": "SYSTEM"}}
        return self.stack.core._enqueue(req, frozenset({"SYSTEM"}))

    def open_case_on_short_account(self) -> str:
        r = self.system_command("broker_order_status", {"account_key": SHORT_ACCOUNT, "broker_order_id": f"B-{_hex()}",
                                                        "status": "ACKNOWLEDGED"})
        self.assertTrue(r["accepted"], r)
        cookie, _ = self.login()
        status, cases, _ = self.call("GET", f"/api/exceptions?account={SHORT_ACCOUNT}", cookie=cookie)
        self.assertEqual(status, 200, cases)
        return cases[0]["case_id"]

    def test_an_account_key_the_handler_ignores_cannot_widen_the_window(self):
        cookie, csrf = self.login()
        self.addCleanup(setattr, self.stack.ctx, "account_key", self.stack.ctx.account_key)
        self.stack.ctx.account_key = SHORT_ACCOUNT   # an API whose own account's policy allows 60 s
        self.age_session(cookie, 5 * MINUTE)
        for payload in ({"body": _policy(1300, f"widen-{_hex()}")},
                        {"body": _policy(1300, f"widen-{_hex()}"), "account_key": "SIM-NO-POLICY"},
                        {"body": _policy(1300, f"widen-{_hex()}"), "account_key": ACCOUNT}):
            status, err, _ = self.command(cookie, csrf, "publish_policy", payload)
            self.assertEqual(status, 428, (payload.get("account_key"), err))
            self.assertEqual((err["step_up"]["max_age_s"], err["step_up"]["account_key"]), (60, SHORT_ACCOUNT))

    def test_resolve_exception_is_governed_by_the_cases_account(self):
        case_id = self.open_case_on_short_account()
        cookie, csrf = self.login()
        self.age_session(cookie, 5 * MINUTE)   # inside the API account's 3600 s, past the case account's 60 s
        for extra in ({}, {"account_key": ACCOUNT}, {"account_key": "SIM-NO-POLICY"}):
            payload = {"case_id": case_id, "action": "acknowledge", "note": "reviewed", **extra}
            status, err, _ = self.command(cookie, csrf, "resolve_exception", payload)
            self.assertEqual(status, 428, (extra, err))
            self.assertEqual((err["step_up"]["max_age_s"], err["step_up"]["account_key"],
                              err["step_up"]["policy_hash"]), (60, SHORT_ACCOUNT, self.stack.short_policy_hash))
        self.age_session(cookie, 20 * NS)
        status, ok, _ = self.command(cookie, csrf, "resolve_exception",
                                     {"case_id": case_id, "action": "acknowledge", "note": "reviewed"})
        self.assertEqual(status, 200, ok)
        self.assertTrue(ok["accepted"], ok)
        status, verify, _ = self.call("GET", "/api/audit/verify", cookie=cookie)
        self.assertEqual((status, verify["chain_problems"], verify["ledger_problems"]), (200, [], []))

    def test_a_target_account_never_lengthens_the_api_accounts_window(self):
        cookie, csrf = self.login()
        self.addCleanup(setattr, self.stack.ctx, "account_key", self.stack.ctx.account_key)
        self.stack.ctx.account_key = SHORT_ACCOUNT
        self.age_session(cookie, 5 * MINUTE)
        status, err, _ = self.command(cookie, csrf, "authorize_capital", {"account_key": ACCOUNT, "amount": "1.00"})
        self.assertEqual(status, 428, err)
        self.assertEqual((err["step_up"]["max_age_s"], err["step_up"]["account_key"]), (60, SHORT_ACCOUNT))


class PostCommitFailureTests(LiveCase):
    """A failure after the core committed a command (the restore marker is written after every commit) is not
    "not executed": the API returns the stored result. A failure before the commit stays NOT_EXECUTED, and a
    core that cannot tell is OUTCOME_UNKNOWN."""

    def fail_after_commit_of(self, cid: str, exc: Exception, *, unreadable: bool = False) -> None:
        store = self.stack.core.desk.store
        marker, query = store.write_marker, store.query
        armed = {"marker": True, "query": False}

        def failing_marker(*a, **kw):
            if armed["marker"] and store.conn.execute("SELECT 1 FROM commands WHERE command_id=?", (cid,)).fetchone():
                armed["marker"], armed["query"] = False, unreadable
                raise exc
            return marker(*a, **kw)

        def failing_query(*a, **kw):
            if armed["query"]:
                armed["query"] = False
                raise sqlite3.OperationalError("disk I/O error")
            return query(*a, **kw)
        store.write_marker, store.query = failing_marker, failing_query
        self.addCleanup(setattr, store, "write_marker", marker)
        self.addCleanup(setattr, store, "query", query)

    def test_a_failure_after_the_commit_returns_the_stored_result(self):
        cookie, csrf = self.login()
        for exc in (PermissionError(13, "Permission denied"), OSError(28, "No space left on device")):
            cid = f"ui.test.postcommit.{_hex()}"
            self.fail_after_commit_of(cid, exc)
            with self.assertLogs("unknown", logging.ERROR) as logs:
                status, got, _ = self.command(cookie, csrf, "publish_policy", {"body": _policy(1400, f"pc-{_hex()}")}, cid)
            self.assertEqual(status, 200, got)
            self.assertTrue(any(cid in m and "was recorded" in m for m in logs.output), logs.output)
            self.assertEqual((got["command_id"], got["accepted"], got["duplicate"]), (cid, True, False))
            self.assertIn(type(exc).__name__, got["post_commit_error"])
            status, stored, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
            self.assertEqual((status, stored["status"], stored["body"]), (200, "ACCEPTED", got["body"]))
            self.assertEqual(stored["events"], got["events"])
            self.assertTrue(got["events"])

    def test_a_failure_after_a_duplicate_is_still_the_duplicate(self):
        cookie, csrf = self.login()
        cid = f"ui.test.postcommit-dup.{_hex()}"
        payload = {"body": _policy(1450, f"pcd-{_hex()}")}
        status, first, _ = self.command(cookie, csrf, "publish_policy", payload, cid)
        self.assertEqual(status, 200, first)
        self.fail_after_commit_of(cid, OSError(28, "No space left on device"))
        with self.assertLogs("unknown", logging.ERROR):
            status, again, _ = self.command(cookie, csrf, "publish_policy", payload, cid)
        self.assertEqual(status, 200, again)
        self.assertEqual((again["duplicate"], again["body"], again["events"]), (True, first["body"], []))

    def test_a_failure_before_the_commit_is_not_executed(self):
        cookie, csrf = self.login()
        handlers = self.stack.core.desk._handlers
        original = handlers["publish_policy"]

        def broken(ctx, p):
            original(ctx, p)
            raise RuntimeError("handler bug after its writes")
        handlers["publish_policy"] = broken
        self.addCleanup(handlers.__setitem__, "publish_policy", original)
        cid = f"ui.test.prefault.{_hex()}"
        with self.assertLogs("unknown.api", logging.WARNING):
            status, err, _ = self.command(cookie, csrf, "publish_policy", {"body": _policy(1500, f"pf-{_hex()}")}, cid)
        self.assertEqual((status, err["code"], err["outcome"]), (500, "CORE_FAULT", "NOT_EXECUTED"))
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual((status, got["definitive"]), (404, True))

    def test_a_core_that_cannot_read_its_store_after_the_failure_reports_outcome_unknown(self):
        cookie, csrf = self.login()
        cid = f"ui.test.postcommit-unknown.{_hex()}"
        self.fail_after_commit_of(cid, OSError(5, "Input/output error"), unreadable=True)
        with self.assertLogs("unknown.core", logging.ERROR):
            status, err, _ = self.command(cookie, csrf, "publish_policy", {"body": _policy(1550, f"pu-{_hex()}")}, cid)
        self.assertEqual((status, err["code"], err["outcome"]), (504, "COMMAND_OUTCOME_UNKNOWN", "UNKNOWN"))
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual((status, got["status"]), (200, "ACCEPTED"))


# ---------------------------------------------------------------------------------------------- 4
def _ns_fields(value, found: list) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and k.endswith("_ns") and (v is None or (isinstance(v, int) and not isinstance(v, bool))):
                found.append((k, v, value.get(k + "_str", "<missing>")))
            _ns_fields(v, found)
    elif isinstance(value, list):
        for v in value:
            _ns_fields(v, found)


class NsStringTests(LiveCase):
    def test_every_ns_integer_has_an_exact_decimal_string_sibling(self):
        cookie, csrf = self.login()
        cid = f"ui.test.ns.{_hex()}"
        self.command(cookie, csrf, "suspend_session", {"session_id": "nope"}, cid)
        found: list = []
        for path in ("/api/auth/session", "/api/overview", "/api/events?limit=100", "/api/policies", "/api/health",
                     "/api/accounts", f"/api/commands/{cid}"):
            status, body, _ = self.call("GET", path, cookie=cookie)   # Python's json keeps big integers exact
            self.assertEqual(status, 200, (path, body))
            _ns_fields(body, found)
        self.assertGreater(len(found), 50)
        for key, value, text in found:
            self.assertEqual(text, None if value is None else str(value), key)
        self.assertTrue(any(v is not None and v > 2 ** 53 for _, v, _ in found))

    def test_the_transform(self):
        src = {"a_ns": 2 ** 60 + 1, "b_ns": None, "flag_ns": True, "c_ns": 5, "c_ns_str": "kept", "x": [{"t_ns": 7}],
               "f_ns": 1.5, "s_ns": "9", "nested": ({"u_ns": 1},)}
        out = queries.with_ns_strings(src)
        self.assertEqual(out["a_ns_str"], str(2 ** 60 + 1))
        self.assertIsNone(out["b_ns_str"])
        self.assertNotIn("flag_ns_str", out)
        self.assertEqual(out["c_ns_str"], "kept")
        self.assertEqual(out["x"][0]["t_ns_str"], "7")
        self.assertNotIn("f_ns_str", out)
        self.assertNotIn("s_ns_str", out)
        self.assertEqual(out["nested"][0]["u_ns_str"], "1")
        self.assertNotIn("a_ns_str", src, "the input is not modified")


# ---------------------------------------------------------------------------------------------- 5
class PathDecodingTests(LiveCase):
    def test_an_id_with_reserved_characters_is_addressable(self):
        cookie, csrf = self.login()
        cid = f"ui.test/a+b@c:{_hex()}"
        status, sent, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "nope"}, cid)
        self.assertEqual(status, 200, sent)
        for encoded in (quote(cid, safe=""), quote(cid, safe=":@+")):   # fully encoded, and as the terminal encodes
            status, got, _ = self.call("GET", f"/api/commands/{encoded}", cookie=cookie)
            self.assertEqual(status, 200, (encoded, got))
            self.assertEqual(got["command_id"], cid)

    def test_an_encoded_slash_stays_inside_its_segment(self):
        cookie, _ = self.login()
        status, got, _ = self.call("GET", "/api/sessions/a%2Fb", cookie=cookie)
        self.assertEqual((status, got["error"]), (404, "no such session"))
        status, got, _ = self.call("GET", "/api/sessions/a/b", cookie=cookie)
        self.assertEqual((status, got["error"]), (404, "unknown endpoint"))
        status, got, _ = self.call("GET", "/api/orders/%6Eo-such-order", cookie=cookie)
        self.assertEqual((status, got["error"]), (404, "no such order"))

    def test_undecodable_segments_are_400(self):
        cookie, _ = self.login()
        status, got, _ = self.call("GET", "/api/sessions/%FF", cookie=cookie)
        self.assertEqual((status, got["code"]), (400, "BAD_REQUEST"))

    def test_split_then_decode(self):
        self.assertEqual(path_parts("/api/commands/a%2Fb%2Bc+d%3A%40"), ["commands", "a/b+c+d:@"])
        self.assertEqual(path_parts("/api//x/"), ["x"])


# ---------------------------------------------------------------------------------------------- 6
class SseReader:
    """A minimal Server-Sent Events reader over a raw socket (so a read can time out and continue)."""

    def __init__(self, port: int, cookie: str, headers: dict | None = None, query: str = ""):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        lines = [f"GET /api/stream{query} HTTP/1.1", "Host: 127.0.0.1", f"Cookie: {cookie}", "Accept: text/event-stream"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.buf = b""
        head = self._until(b"\r\n\r\n", 10)
        self.status = int(head.split(b" ", 2)[1])

    def _until(self, marker: bytes, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while marker not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.sock], [], [], left)[0]:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        block, self.buf = self.buf.split(marker, 1)
        return block

    def block(self, timeout: float) -> dict | None:
        """The next SSE block as {field: value} (comments skipped), or None on timeout."""
        while True:
            raw = self._until(b"\n\n", timeout)
            if raw is None:
                return None
            fields = {}
            for line in raw.decode().split("\n"):
                if line and not line.startswith(":"):
                    name, _, value = line.partition(":")
                    fields[name] = value[1:] if value.startswith(" ") else value
            if fields:
                return fields

    def event(self, timeout: float) -> dict | None:
        while True:
            b = self.block(timeout)
            if b is None or "event" in b:
                return b

    def close(self):
        self.sock.close()


class StreamTests(LiveCase):
    def latest_seq(self, cookie) -> int:
        return self.call("GET", "/api/health", cookie=cookie)[1]["latest_seq"]

    def open(self, cookie, **kw) -> SseReader:
        r = SseReader(self.stack.port, cookie, **kw)
        self.addCleanup(r.close)
        self.assertEqual(r.status, 200)
        return r

    def test_ids_and_retry_hint(self):
        cookie, _ = self.login()
        r = self.open(cookie)
        self.assertEqual(r.block(5), {"retry": str(api_server.SSE_RETRY_MS)})
        ev = r.event(5)
        self.assertEqual(ev["event"], "seq")
        self.assertEqual(ev["id"], str(json.loads(ev["data"])["seq"]))
        self.assertGreaterEqual(int(ev["id"]), self.latest_seq(cookie) - 50)

    def test_last_event_id_resumes_without_resending(self):
        cookie, csrf = self.login()
        seq = self.latest_seq(cookie)
        r = self.open(cookie, headers={"Last-Event-ID": str(seq)})
        self.assertIn("retry", r.block(5))
        first = r.event(1.0)
        if first is not None:   # only a newer event (the core may record one meanwhile), never the one we saw
            self.assertGreater(int(first["id"]), seq)
        _, sent, _ = self.command(cookie, csrf, "suspend_session", {"session_id": "nope"})
        target = max(sent["events"])
        deadline = time.monotonic() + 10
        seen = int(first["id"]) if first else seq
        while seen < target and time.monotonic() < deadline:
            ev = r.event(5)
            self.assertIsNotNone(ev)
            self.assertGreater(int(ev["id"]), seen)
            seen = int(ev["id"])
        self.assertGreaterEqual(seen, target)

    def test_a_last_event_id_ahead_of_the_log_or_unreadable_gets_the_current_seq_at_once(self):
        cookie, _ = self.login()
        for headers, query in (({"Last-Event-ID": "999999999"}, ""), ({"Last-Event-ID": "abc"}, ""),
                               ({}, "?last_event_id=999999999")):
            r = self.open(cookie, headers=headers, query=query)
            ev = r.event(5)
            self.assertIsNotNone(ev, headers or query)
            self.assertLess(int(ev["id"]), 999999999)

    def test_query_parameter_resume(self):
        cookie, _ = self.login()
        seq = self.latest_seq(cookie)
        r = self.open(cookie, query=f"?last_event_id={seq}")
        ev = r.event(1.0)
        self.assertTrue(ev is None or int(ev["id"]) > seq)


# ---------------------------------------------------------------------------------------------- 7
class CapabilitiesTests(LiveCase):
    def setUp(self):
        self.path = self.stack.tmp / f"register-{_hex()}.json"
        self.write({"capabilities": [{"id": "CAP-TEST-1", "status": "proposed"}] * 200, "generated": "2026-10-22"})
        self.stack.ctx.capabilities_path = self.path
        self.addCleanup(setattr, self.stack.ctx, "capabilities_path", None)
        self.cookie, _ = self.login()

    def write(self, obj) -> bytes:
        raw = json.dumps(obj, indent=2).encode()
        self.path.write_bytes(raw)
        return raw

    def get(self, **headers):
        return self.request("GET", "/api/capabilities", cookie=self.cookie, headers=headers)

    def test_etag_is_the_files_sha256_and_if_none_match_gives_304(self):
        raw = self.path.read_bytes()
        status, headers, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body, raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(headers["etag"], f'W/"{digest}"')
        self.assertEqual(headers["cache-control"], "private, no-cache")
        self.assertNotIn("content-encoding", headers)
        for tag in (headers["etag"], f'"{digest}"', f'"other", W/"{digest}"', "*"):
            status, h304, body = self.get(**{"If-None-Match": tag})
            self.assertEqual((status, body), (304, b""), tag)
            self.assertEqual(h304["etag"], headers["etag"])
        status, _, body = self.get(**{"If-None-Match": '"stale"'})
        self.assertEqual((status, body), (200, raw))

    def test_a_changed_file_gets_a_new_etag(self):
        _, first, _ = self.get()
        time.sleep(0.01)
        raw = self.write({"capabilities": [], "generated": "2026-10-23"})
        status, headers, body = self.get(**{"If-None-Match": first["etag"]})
        self.assertEqual((status, body), (200, raw))
        self.assertEqual(headers["etag"], f'W/"{hashlib.sha256(raw).hexdigest()}"')

    def test_gzip_when_accepted(self):
        raw = self.path.read_bytes()
        status, headers, body = self.get(**{"Accept-Encoding": "br;q=1.0, gzip;q=0.8"})
        self.assertEqual((status, headers["content-encoding"], headers["vary"]), (200, "gzip", "Accept-Encoding"))
        self.assertEqual(gzip.decompress(body), raw)
        self.assertLess(len(body), len(raw))
        self.assertEqual(int(headers["content-length"]), len(body))
        status, headers, body = self.get(**{"Accept-Encoding": "gzip;q=0, *"})
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(body, raw)

    def test_missing_and_invalid_registers(self):
        self.path.write_text("{not json")
        status, headers, body = self.get()
        self.assertEqual((status, json.loads(body)["code"]), (500, "CAPABILITIES_INVALID"))
        self.path.unlink()
        status, _, body = self.get()
        self.assertEqual((status, json.loads(body)["capabilities"]), (200, []))

    def test_negotiation_helpers(self):
        self.assertTrue(accepts_gzip("gzip, deflate, br"))
        self.assertTrue(accepts_gzip("*"))
        self.assertFalse(accepts_gzip("gzip;q=0"))
        self.assertFalse(accepts_gzip("gzip;q=0, *;q=1"))
        self.assertFalse(accepts_gzip(None))
        self.assertFalse(accepts_gzip("identity"))
        self.assertTrue(etag_matches('W/"ab"', "ab"))
        self.assertFalse(etag_matches('"abc"', "ab"))


# ---------------------------------------------------------------------------------------------- 8
class DisconnectLoggingTests(LiveCase):
    def test_a_client_that_hangs_up_before_the_reply_costs_one_log_line_and_no_traceback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertLogs("unknown.core.ipc", logging.WARNING) as logs:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:   # connect and leave without a request
                s.connect(str(self.stack.paths.socket))
            cid = f"ui.test.hangup.{_hex()}"
            request = {"token": self.stack.paths.token("api"), "op": "command", "command_id": cid,
                       "kind": "suspend_session", "payload": {"session_id": "nope"},
                       "principal": {"id": "owner", "role": "OWNER"}}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(str(self.stack.paths.socket))
                s.sendall((json.dumps(request) + "\n").encode())
            deadline = time.monotonic() + 10
            while not any(cid in m for m in logs.output) and time.monotonic() < deadline:
                time.sleep(0.05)
        lines = [m for m in logs.output if cid in m]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("disconnected before the reply", lines[0])
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(any("Traceback" in m for m in logs.output))
        # the command itself was executed and is recoverable by its id
        cookie, _ = self.login()
        status, got, _ = self.call("GET", f"/api/commands/{cid}", cookie=cookie)
        self.assertEqual((status, got["status"]), (200, "REJECTED"))

    def test_the_api_logs_a_browser_disconnect_as_one_line(self):
        with self.assertLogs("unknown.api", logging.INFO) as logs:
            try:
                raise BrokenPipeError(32, "Broken pipe")
            except BrokenPipeError:
                self.stack.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIn("disconnected", logs.output[0])


# ---------------------------------------------------------------------------------------------- 9
class PluginErrorTests(LiveCase):
    def setUp(self):
        def handler(ctx, parts, q, acct, conn):
            what = parts[1] if len(parts) > 1 else ""
            if what == "value":
                raise ValueError("bad parameter x")
            if what == "lookup":
                raise LookupError("no such thing")
            if what == "permission":
                raise PermissionError("not for you")
            if what == "crash":
                raise RuntimeError("plugin bug")
            return {"ok": True, "at_ns": 2 ** 61}
        original = api_server._plugin_routes()
        api_server._PLUGINS = [(("zz-test",), handler), *original]
        self.addCleanup(setattr, api_server, "_PLUGINS", original)
        self.cookie, _ = self.login()

    def test_value_error_is_400_and_other_failures_are_json_errors(self):
        for what, status, code in (("value", 400, "BAD_REQUEST"), ("lookup", 404, "NOT_FOUND"),
                                   ("permission", 403, "FORBIDDEN"), ("crash", 500, "INTERNAL_ERROR")):
            with self.assertLogs("unknown.api", logging.INFO):
                got = self.call("GET", f"/api/zz-test/{what}", cookie=self.cookie)
            self.assertEqual((got[0], got[1]["code"]), (status, code), what)
        status, body, _ = self.call("GET", "/api/zz-test/fine", cookie=self.cookie)
        self.assertEqual((status, body["at_ns_str"]), (200, str(2 ** 61)))

    def test_a_real_plugin_value_error_is_400(self):
        status, body, _ = self.call("GET", "/api/portfolio/whatif", cookie=self.cookie)
        self.assertEqual((status, body["code"]), (400, "BAD_REQUEST"))
        self.assertIn("missing parameters", body["error"])


if __name__ == "__main__":
    unittest.main()
