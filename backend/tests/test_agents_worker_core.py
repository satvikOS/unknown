"""Model worker against the real core service (ADR-0013 socket, AGENT token; ADR-0005 command-id dedup).

The worker delivers over ``CoreClient`` with ``Paths.token("agent")`` to a ``CoreService`` running the
simulated broker in a thread. A crash after the core accepted the proposal, and a core that is down and
then restarted, both end with exactly one recorded proposal in the core's ``proposals`` table."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_worker import Crash, WorkerHarness, answer, message  # noqa: E402
from unknown.domain.clock import SystemClock  # noqa: E402
from unknown.service.core import CoreService, _simulated_broker  # noqa: E402
from unknown.store.db import connect  # noqa: E402

ACCOUNT = "SIM-1"


class WorkerWithRealCoreTest(WorkerHarness, unittest.TestCase):
    def start_core(self) -> CoreService:
        svc = CoreService(paths=self.paths, account_key=ACCOUNT, deployment_id="test",
                          broker=_simulated_broker(SystemClock(), "300.00"))
        thread = threading.Thread(target=svc.run, daemon=True)
        thread.start()
        self.assertTrue(svc.ready.wait(20), "core did not start")
        self._core_threads.append((svc, thread))
        return svc

    def stop_core(self, svc: CoreService) -> None:
        for s, t in list(self._core_threads):
            if s is svc:
                s.shutdown()
                t.join(20)
                self._core_threads.remove((s, t))

    def setUp(self):
        super().setUp()
        self._core_threads: list = []
        self.addCleanup(lambda: [self.stop_core(s) for s, _ in list(self._core_threads)])

    def proposals(self) -> list[dict]:
        conn = connect(self.paths.db(ACCOUNT), readonly=True)
        try:
            rows = conn.execute("SELECT proposal_id, author, row FROM proposals ORDER BY rowid").fetchall()
            recorded = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'ResearchProposalRecorded'").fetchone()[0]
        finally:
            conn.close()
        out = [{"proposal_id": r["proposal_id"], "author": r["author"], **json.loads(r["row"])} for r in rows]
        self.assertEqual(recorded, len(out))
        return out

    def test_crash_after_acceptance_and_core_restart_each_record_one_proposal(self):
        core = self.start_core()
        w = self.worker(core_factory=None)                       # the real CoreClient with the agent token
        jid = self.submit(w)
        self.client.messages.outcomes = [message(answer(w.jobs.get(jid)["inputs"]["evidence"]["spans"]))]
        original = w.jobs.mark_delivered
        w.jobs.mark_delivered = lambda *a, **kw: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            w.step()
        w.jobs.mark_delivered = original
        self.assertEqual(len(self.proposals()), 1)
        w.close()
        self.workers.remove(w)
        restarted = self.worker(core_factory=None)
        restarted.start()
        restarted.step()
        job = restarted.jobs.get(jid)
        self.assertEqual(job["delivery"], "DELIVERED")
        self.assertTrue(job["core_result"]["duplicate"])
        rows = self.proposals()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_id"], job["core_result"]["body"]["proposal_id"])
        self.assertEqual((rows[0]["author"], rows[0]["body"]), ("agent.research", job["proposal"]))

        # core down: the next job's proposal is kept and retried; after a restart it is recorded once
        self.stop_core(core)
        jid2 = self.submit(restarted, question="And in fiscal 2024?")
        self.client.messages.outcomes = [message(answer(restarted.jobs.get(jid2)["inputs"]["evidence"]["spans"]))]
        restarted.step()
        job2 = restarted.jobs.get(jid2)
        self.assertEqual((job2["state"], job2["delivery"]), ("SUCCEEDED", "PENDING"))
        self.assertIn("core unreachable", job2["delivery_error"])
        self.start_core()
        self.clock.advance(2 * 1_000_000_000)
        restarted.step()
        self.assertEqual(restarted.jobs.get(jid2)["delivery"], "DELIVERED")
        rows = self.proposals()
        self.assertEqual([r["body"]["job_id"] for r in rows], [jid, jid2])
        self.assertEqual(restarted.audit.verify(), [])


if __name__ == "__main__":
    unittest.main()
