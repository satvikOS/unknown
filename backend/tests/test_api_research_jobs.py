"""Terminal API research-job routes (ADR-0013 owner session + CSRF; ADR-0016): the API writes only the job
store, never imports provider code, and shows the proposal the core recorded after the worker ran the job."""
from __future__ import annotations

import ast
import http.client
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import FIX_TICKER, TempDirCase, owner_doc, research_store, write_private  # noqa: E402
from test_agents_worker import WorkerHarness, answer, message  # noqa: E402
from unknown.agents.jobstore import JobStore, default_jobs_path  # noqa: E402
from unknown.api.auth import write_owner_record  # noqa: E402
from unknown.api.routes import research_jobs  # noqa: E402
from unknown.api.server import ApiContext, make_server  # noqa: E402
from unknown.domain.clock import SystemClock  # noqa: E402
from unknown.service.core import CoreService, _simulated_broker  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402
from unknown.store.db import connect, migrate  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"
PASSPHRASE = "correct horse battery staple fixture"
ACCOUNT = "SIM-1"
FIELDS = ["us-gaap:Revenues", "us-gaap:Assets"]


class ApiServerMixin:
    def start_api(self, home: Path) -> None:
        paths = Paths(home)
        paths.ensure()
        paths.token("api", create=True)
        write_owner_record(paths.secrets / "owner.json", PASSPHRASE)
        if not paths.db(ACCOUNT).exists():
            conn = connect(paths.db(ACCOUNT))
            migrate(conn, 0)
            conn.close()
        self.api_paths = paths
        original_now = research_jobs.NOW
        research_jobs.NOW = self.clock.now_ns          # submissions pin evidence at the test's manual clock
        self.addCleanup(setattr, research_jobs, "NOW", original_now)
        server = make_server(ApiContext(paths, ACCOUNT, None, None, False), "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.port = server.server_address[1]

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(10)
            for store in research_jobs._stores.values():
                store.close()
            research_jobs._stores.clear()
        self.addCleanup(stop)

    def call(self, method: str, path: str, body=None, *, cookie=None, csrf=None) -> tuple[int, dict, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        out = (resp.status, data, dict(resp.getheaders()))
        conn.close()
        return out

    def login(self) -> tuple[str, str]:
        status, data, headers = self.call("POST", "/api/auth/login", {"passphrase": PASSPHRASE})
        self.assertEqual(status, 200, data)
        cookie = headers["Set-Cookie"].split(";")[0]
        return cookie, data["csrf_token"]


class ResearchJobRoutesTest(ApiServerMixin, TempDirCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        write_private(self.home / "config" / "agents.json", json.dumps(owner_doc()))
        research_store(self.home / "research" / "research.sqlite").close()
        self.start_api(self.home)

    def store(self) -> JobStore:
        s = JobStore(default_jobs_path(self.home), clock_ns=self.clock.now_ns, readonly=True)
        self.addCleanup(s.close)
        return s

    def test_every_research_route_needs_the_owner_session_and_posts_need_csrf(self):
        body = {"question": "Did revenue grow?", "issuer_ids": [FIX_TICKER], "fields": FIELDS}
        self.assertEqual(self.call("GET", "/api/research/jobs")[0], 401)
        self.assertEqual(self.call("GET", "/api/research/jobs/rj-x")[0], 401)
        self.assertEqual(self.call("POST", "/api/research/jobs", body)[0], 401)
        self.assertEqual(self.call("POST", "/api/research/jobs/rj-x/cancel", {})[0], 401)
        cookie, csrf = self.login()
        self.assertEqual(self.call("POST", "/api/research/jobs", body, cookie=cookie)[0], 403)
        self.assertEqual(self.call("POST", "/api/research/jobs", body, cookie=cookie, csrf="wrong")[0], 403)
        self.assertEqual(self.call("POST", "/api/research/jobs", body, cookie="unk_session=forged", csrf=csrf)[0], 401)
        self.assertFalse(default_jobs_path(self.home).exists())          # nothing was written by refused requests
        status, data, _ = self.call("POST", "/api/research/jobs", body, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, data)
        job = self.store().get(data["job_id"])
        self.assertEqual((job["state"], job["submitted_by"]), ("QUEUED", "owner"))

    def test_submit_list_detail_and_cancel(self):
        cookie, csrf = self.login()
        body = {"question": "Did revenue grow in fiscal 2025?", "issuer_ids": [FIX_TICKER], "fields": FIELDS}
        status, made, _ = self.call("POST", "/api/research/jobs", body, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, made)
        self.assertEqual((made["created"], made["state"], made["evidence_spans"]), (True, "QUEUED", 4))
        self.assertEqual(made["spend_cap"], {"currency": "USD", "amount": "2.00"})
        status, again, _ = self.call("POST", "/api/research/jobs", body, cookie=cookie, csrf=csrf)
        self.assertEqual((again["created"], again["duplicate_of"]), (False, made["job_id"]))
        status, listing, _ = self.call("GET", "/api/research/jobs", cookie=cookie)
        self.assertEqual([j["job_id"] for j in listing["jobs"]], [made["job_id"]])
        self.assertEqual(listing["jobs"][0]["question"], body["question"])
        status, queued, _ = self.call("GET", "/api/research/jobs?state=QUEUED", cookie=cookie)
        self.assertEqual(len(queued["jobs"]), 1)
        status, detail, _ = self.call("GET", f"/api/research/jobs/{made['job_id']}", cookie=cookie)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["job"]["state"], "QUEUED")
        self.assertEqual(len(detail["evidence"]["spans"]), 4)
        self.assertEqual(detail["request"]["issuer_ids"], [FIX_TICKER])
        self.assertEqual((detail["audit"], detail["audit_chain"]["ok"], detail["spend"]["ledger"]), ([], True, "absent"))
        self.assertIsNone(detail["recorded_proposal"])
        status, cancelled, _ = self.call("POST", f"/api/research/jobs/{made['job_id']}/cancel", {"reason": "changed my mind"},
                                         cookie=cookie, csrf=csrf)
        self.assertEqual((status, cancelled["state"]), (200, "CANCELLED"))
        self.assertEqual(self.call("POST", f"/api/research/jobs/{made['job_id']}/cancel", {}, cookie=cookie, csrf=csrf)[0], 409)
        self.assertEqual(self.call("POST", "/api/research/jobs/rj-none/cancel", {}, cookie=cookie, csrf=csrf)[0], 404)
        self.assertEqual(self.call("GET", "/api/research/jobs/rj-none", cookie=cookie)[0], 404)

    def test_bad_requests_and_disabled_paid_calls(self):
        cookie, csrf = self.login()
        cases = [({"question": "q", "unknown": 1}, 400), ({}, 400), ({"question": "q", "spend_cap": 1}, 400),
                 ({"question": "q", "issuer_ids": ["NOPE"]}, 400), ({"question": "q", "spend_cap": "5.00"}, 400)]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/research/jobs", body, cookie=cookie, csrf=csrf)[0], expected)
        self.assertEqual(self.call("GET", "/api/research/jobs?state=DONE", cookie=cookie)[0], 400)
        self.assertEqual(self.call("POST", "/api/research/jobs/rj-x/cancel", {"why": "x"}, cookie=cookie, csrf=csrf)[0], 400)
        (self.home / "config" / "agents.json").unlink()
        status, data, _ = self.call("POST", "/api/research/jobs", {"question": "q"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 409)
        self.assertIn("paid model calls are disabled", data["error"])
        self.assertEqual(self.call("POST", "/api/research/nothing", {}, cookie=cookie, csrf=csrf)[0], 404)


class ApiNeverHoldsProviderCodeTest(unittest.TestCase):
    FORBIDDEN = ("unknown.agents.providers", "unknown.agents.runner", "unknown.agents.worker", "unknown.agents.tools",
                 "anthropic")

    def test_loading_every_api_route_imports_no_provider_runner_or_worker_module(self):
        code = ("import json, sys\n"
                "import unknown.api.server as s\n"
                "s._plugin_routes(); s._plugin_post_routes()\n"
                "print(json.dumps(sorted(sys.modules)))\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                             env={**os.environ, "PYTHONPATH": str(SRC)}, cwd=str(SRC.parent))
        self.assertEqual(out.returncode, 0, out.stderr)
        loaded = json.loads(out.stdout)
        self.assertIn("unknown.api.routes.research_jobs", loaded)
        self.assertEqual([m for m in loaded if m.startswith(self.FORBIDDEN)], [])

    def test_route_sources_never_touch_keys_or_providers(self):
        for path in (SRC / "unknown" / "api" / "routes").glob("*.py"):
            text = path.read_text()
            for word in ("api_key", "resolve_api_key", "providers", "secrets", "token("):
                self.assertNotIn(word, text, f"{path.name} mentions {word}")
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(node.module.split(".")[-1] in ("runner", "worker", "providers"), path.name)


class TerminalToCoreEndToEndTest(ApiServerMixin, WorkerHarness, unittest.TestCase):
    """The owner asks in the terminal API; the worker runs the job under the caps; the core records the proposal;
    the terminal shows it."""

    def test_question_to_recorded_proposal(self):
        core = CoreService(paths=self.paths, account_key=ACCOUNT, deployment_id="test",
                           broker=_simulated_broker(SystemClock(), "300.00"))
        thread = threading.Thread(target=core.run, daemon=True)
        thread.start()
        self.assertTrue(core.ready.wait(20))
        self.addCleanup(lambda: (core.shutdown(), thread.join(20)))
        self.start_api(self.home)
        cookie, csrf = self.login()
        status, made, _ = self.call("POST", "/api/research/jobs", {"question": "Did revenue grow in fiscal 2025?",
                                                                     "issuer_ids": [FIX_TICKER], "fields": FIELDS,
                                                                     "max_output_tokens": 2000},
                                    cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, made)
        jid = made["job_id"]
        w = self.worker(core_factory=None)              # real CoreClient with the agent token
        w.start()
        spans = w.jobs.get(jid)["inputs"]["evidence"]["spans"]
        self.client.messages.outcomes = [message(answer(spans))]
        self.assertTrue(w.step())
        status, detail, _ = self.call("GET", f"/api/research/jobs/{jid}", cookie=cookie)
        self.assertEqual(status, 200, detail)
        self.assertEqual((detail["job"]["state"], detail["job"]["delivery"]), ("SUCCEEDED", "DELIVERED"), detail["job"]["reason"])
        recorded = detail["recorded_proposal"]
        self.assertEqual(recorded["author"], "agent.research")
        self.assertEqual(recorded["body"], detail["proposal"])
        self.assertFalse(recorded["body"]["abstain"])
        self.assertTrue(detail["audit_chain"]["ok"])
        self.assertIn("proposal_delivered", [a["kind"] for a in detail["audit"]])
        self.assertEqual(detail["spend"]["ledger"], "present")
        self.assertEqual([r["state"] for r in detail["spend"]["reservations"] if r["purpose"] == "generate"], ["SETTLED"])


if __name__ == "__main__":
    unittest.main()
