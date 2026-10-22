"""Durable research job store (CAP-AGT-01; ADR-0016 section 2): durability, idempotent submission,
dedupe, claims under concurrency limits, the state machine and the proposal outbox."""
from __future__ import annotations

import os
import stat
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import SECOND, TempDirCase, envelope, sha  # noqa: E402
from unknown.agents.envelope import JobState  # noqa: E402
from unknown.agents.jobstore import (DEFAULT_DEDUPE_WINDOW_NS, JobStore, JobStoreError, StateConflict,  # noqa: E402
                                     store_state_for)

INPUTS = {"task": "Assess revenue.", "horizon_seconds": 3600, "max_output_tokens": 300}
PROPOSAL = {"schema_version": 1, "job_id": "job-1", "hypothesis": "h", "evidence": [], "horizon_seconds": 3600,
            "assumptions": [], "abstain": True, "reason": "fixture"}


class JobStoreTest(TempDirCase, unittest.TestCase):
    def store(self, name: str = "jobs.sqlite") -> JobStore:
        s = JobStore(self.tmp / "agents" / name, clock_ns=self.clock.now_ns)
        self.addCleanup(s.close)
        return s

    # ------------------------------------------------------------------ durability
    def test_a_submitted_job_survives_reopening_and_the_files_are_private(self):
        path = self.tmp / "agents" / "jobs.sqlite"
        s = JobStore(path, clock_ns=self.clock.now_ns)
        env = envelope()
        self.assertEqual(s.submit(env, INPUTS, submitted_by="owner").created, True)
        s.close()
        again = JobStore(path, clock_ns=self.clock.now_ns)
        self.addCleanup(again.close)
        job = again.get("job-1")
        self.assertEqual((job["state"], job["role"], job["submitted_by"]), ("QUEUED", "research", "owner"))
        self.assertEqual(job["envelope"], env.to_dict())
        self.assertEqual(job["inputs"], INPUTS)
        self.assertEqual([e["kind"] for e in again.events("job-1")], ["queued"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)

    def test_a_running_claim_survives_a_restart_of_the_store(self):
        path = self.tmp / "agents" / "jobs.sqlite"
        s = JobStore(path, clock_ns=self.clock.now_ns)
        s.submit(envelope(), INPUTS, submitted_by="owner")
        s.claim_next("w1", max_global=1, max_per_role={"research": 1})
        s.close()
        again = JobStore(path, clock_ns=self.clock.now_ns)
        self.addCleanup(again.close)
        running = again.running_jobs()
        self.assertEqual([(j["job_id"], j["worker_id"], j["start_attempts"]) for j in running], [("job-1", "w1", 1)])

    # ------------------------------------------------------------------ idempotent submission
    def test_resubmitting_the_same_job_is_idempotent_and_reusing_its_id_is_refused(self):
        s = self.store()
        env = envelope()
        first = s.submit(env, INPUTS, submitted_by="owner")
        second = s.submit(env, INPUTS, submitted_by="owner")
        self.assertEqual((second.job_id, second.created, second.duplicate_of), ("job-1", False, None))
        self.assertTrue(first.created)
        with self.assertRaises(JobStoreError):
            s.submit(env, {**INPUTS, "task": "something else"}, submitted_by="owner")
        with self.assertRaises(JobStoreError):
            s.submit(envelope(max_turns=1), INPUTS, submitted_by="owner")
        self.assertEqual(len(s.list()), 1)

    def test_an_equivalent_job_is_returned_while_live_and_inside_the_window(self):
        s = self.store()
        s.submit(envelope(job_id="job-1"), INPUTS, submitted_by="owner")
        dup = s.submit(envelope(job_id="job-2"), INPUTS, submitted_by="owner")      # same dedupe key, different id
        self.assertEqual((dup.job_id, dup.created, dup.duplicate_of, dup.state), ("job-1", False, "job-1", "QUEUED"))
        self.assertIsNone(s.get("job-2"))
        self.assertIn("duplicate_submission", [e["kind"] for e in s.events("job-1")])
        other = s.submit(envelope(job_id="job-3", input_hashes=(sha("different"),)), INPUTS, submitted_by="owner")
        self.assertTrue(other.created)
        # a failed job does not block a resubmission
        s.fail("job-1", from_states=("QUEUED",), outcome="denied_by_policy", reason="fixture")
        self.assertTrue(s.submit(envelope(job_id="job-4"), INPUTS, submitted_by="owner").created)
        # a succeeded job dedupes only inside the window
        s.claim_next("w", max_global=5, max_per_role=None)
        s.claim_next("w", max_global=5, max_per_role=None)
        s.finish("job-4", "w", state="SUCCEEDED", outcome="succeeded", reason="ok", proposal=PROPOSAL)
        self.assertEqual(s.submit(envelope(job_id="job-5"), INPUTS, submitted_by="owner").duplicate_of, "job-4")
        self.clock.advance(DEFAULT_DEDUPE_WINDOW_NS + SECOND)
        later = envelope(job_id="job-6", created_ns=self.clock.now_ns(), deadline_ns=self.clock.now_ns() + 600 * SECOND)
        self.assertTrue(s.submit(later, INPUTS, submitted_by="owner").created)

    def test_past_deadlines_and_child_jobs_are_refused(self):
        s = self.store()
        self.clock.advance(601 * SECOND)
        with self.assertRaises(JobStoreError):
            s.submit(envelope(), INPUTS, submitted_by="owner")
        now = self.clock.now_ns()
        with self.assertRaises(JobStoreError):
            s.submit(envelope(job_id="c", parent_job_id="p", created_ns=now, deadline_ns=now + SECOND), INPUTS, submitted_by="owner")
        self.assertEqual(s.list(), [])

    # ------------------------------------------------------------------ claims and concurrency
    def test_claims_respect_global_and_per_role_limits(self):
        s = self.store()
        s.submit(envelope(job_id="r1"), INPUTS, submitted_by="owner")
        self.clock.advance(SECOND)
        s.submit(envelope(job_id="r2", input_hashes=(sha("2"),)), INPUTS, submitted_by="owner")
        self.clock.advance(SECOND)
        s.submit(envelope(job_id="q1", role="quant", input_hashes=(sha("3"),)), INPUTS, submitted_by="owner")
        limits = {"research": 1}
        self.assertEqual(s.claim_next("w", max_global=5, max_per_role=limits)["job_id"], "r1")   # oldest first
        self.assertIsNone(s.claim_next("w", max_global=5, max_per_role=limits))  # research at limit; quant has no limit
        self.assertIsNone(s.claim_next("w", max_global=1, max_per_role=None))    # global limit
        self.assertEqual(s.claim_next("w", max_global=5, max_per_role=None)["job_id"], "r2")

    def test_two_handles_racing_never_claim_the_same_job(self):
        a, b = self.store(), self.store()
        for i in range(6):
            a.submit(envelope(job_id=f"j{i}", input_hashes=(sha(str(i)),)), INPUTS, submitted_by="owner")
        claimed: list[str] = []
        lock = threading.Lock()

        def drain(store, name):
            while True:
                job = store.claim_next(name, max_global=100, max_per_role=None)
                if job is None:
                    return
                with lock:
                    claimed.append(job["job_id"])

        threads = [threading.Thread(target=drain, args=(st, f"w{i}")) for i, st in enumerate((a, b, a, b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(claimed), [f"j{i}" for i in range(6)])

    def test_overdue_queued_jobs_expire_and_are_never_claimed(self):
        s = self.store()
        s.submit(envelope(), INPUTS, submitted_by="owner")
        self.clock.advance(600 * SECOND)
        self.assertIsNone(s.claim_next("w", max_global=1, max_per_role=None))
        self.assertEqual(s.expire_overdue(), ["job-1"])
        job = s.get("job-1")
        self.assertEqual((job["state"], job["outcome"]), ("EXPIRED", "deadline_expired"))

    # ------------------------------------------------------------------ state machine
    def test_finish_needs_the_claiming_worker_and_stores_the_proposal_for_delivery(self):
        s = self.store()
        s.submit(envelope(), INPUTS, submitted_by="owner")
        with self.assertRaises(StateConflict):
            s.finish("job-1", "w", state="SUCCEEDED", outcome="succeeded", reason="ok")    # still queued
        s.claim_next("w", max_global=1, max_per_role=None)
        with self.assertRaises(StateConflict):
            s.finish("job-1", "another-worker", state="SUCCEEDED", outcome="succeeded", reason="ok")
        with self.assertRaises(JobStoreError):
            s.finish("job-1", "w", state="RUNNING", outcome="x", reason="x")
        job = s.finish("job-1", "w", state="SUCCEEDED", outcome="succeeded", reason="ok", proposal=PROPOSAL)
        self.assertEqual((job["state"], job["delivery"], job["proposal"]), ("SUCCEEDED", "PENDING", PROPOSAL))
        self.assertIsNotNone(job["output_hash"])
        with self.assertRaises(StateConflict):
            s.finish("job-1", "w", state="FAILED", outcome="x", reason="twice")

    def test_cancel(self):
        s = self.store()
        s.submit(envelope(job_id="q"), INPUTS, submitted_by="owner")
        s.submit(envelope(job_id="r", input_hashes=(sha("r"),)), INPUTS, submitted_by="owner")
        self.assertEqual(s.cancel("q", by="owner")["state"], "CANCELLED")
        self.assertTrue(s.cancel_requested("q"))
        s.claim_next("w", max_global=1, max_per_role=None)
        self.assertFalse(s.cancel_requested("r"))
        running = s.cancel("r", by="owner")
        self.assertEqual((running["state"], running["cancel_requested"]), ("RUNNING", True))
        self.assertTrue(s.cancel_requested("r"))
        s.finish("r", "w", state="CANCELLED", outcome="cancelled_by_owner", reason="discarded", discarded_output_hash="ab" * 32)
        with self.assertRaises(StateConflict):
            s.cancel("r", by="owner")
        with self.assertRaises(LookupError):
            s.cancel("nope", by="owner")
        self.assertEqual(s.get("r")["delivery"], "NONE")

    def test_requeue_and_fail_from_recovery(self):
        s = self.store()
        s.submit(envelope(), INPUTS, submitted_by="owner")
        s.claim_next("w", max_global=1, max_per_role=None)
        s.requeue("job-1", "worker restarted before any provider call")
        job = s.get("job-1")
        self.assertEqual((job["state"], job["worker_id"], job["start_attempts"]), ("QUEUED", None, 1))
        s.claim_next("w2", max_global=1, max_per_role=None)
        self.assertEqual(s.get("job-1")["start_attempts"], 2)
        s.fail("job-1", from_states=("RUNNING",), outcome="worker_restarted", reason="worker restarted")
        self.assertEqual(s.get("job-1")["state"], "FAILED")
        with self.assertRaises(StateConflict):
            s.requeue("job-1", "again")
        with self.assertRaises(JobStoreError):
            s.fail("job-1", from_states=("SUCCEEDED",), outcome="x", reason="x")

    # ------------------------------------------------------------------ outbox
    def test_delivery_is_retried_until_delivered_and_never_dropped(self):
        s = self.store()
        s.submit(envelope(), INPUTS, submitted_by="owner")
        s.claim_next("w", max_global=1, max_per_role=None)
        s.finish("job-1", "w", state="SUCCEEDED", outcome="succeeded", reason="ok", proposal=PROPOSAL)
        self.assertEqual([j["job_id"] for j in s.due_deliveries()], ["job-1"])
        self.assertEqual(s.mark_delivery_failed("job-1", "core unreachable", retry_at_ns=self.clock.now_ns() + 4 * SECOND), 1)
        self.assertEqual(s.due_deliveries(), [])
        self.clock.advance(4 * SECOND)
        job = s.due_deliveries()[0]
        self.assertEqual((job["delivery"], job["delivery_attempts"], job["proposal"]), ("PENDING", 1, PROPOSAL))
        s.mark_delivered("job-1", {"accepted": True, "duplicate": False, "body": {"proposal_id": "prop-1"}})
        job = s.get("job-1")
        self.assertEqual((job["delivery"], job["delivery_attempts"], job["delivery_error"]), ("DELIVERED", 2, ""))
        self.assertEqual(s.list()[0]["proposal_id"], "prop-1")
        with self.assertRaises(StateConflict):
            s.mark_delivered("job-1", {"accepted": True})

    def test_a_rejected_or_conflicting_delivery_stops_but_keeps_the_proposal(self):
        s = self.store()
        for jid in ("a", "b"):
            s.submit(envelope(job_id=jid, input_hashes=(sha(jid),)), INPUTS, submitted_by="owner")
            s.claim_next("w", max_global=1, max_per_role=None)
            s.finish(jid, "w", state="FAILED", outcome="budget_exhausted", reason="x", proposal={**PROPOSAL, "job_id": jid})
        s.mark_delivery_failed("a", "rejected", final="REJECTED", core_result={"accepted": False, "body": {"code": "X"}})
        s.mark_delivery_failed("b", "CommandConflict", final="CONFLICT")
        self.assertEqual(s.due_deliveries(), [])
        self.assertEqual((s.get("a")["delivery"], s.get("a")["proposal"]["job_id"]), ("REJECTED", "a"))
        self.assertEqual((s.get("b")["delivery"], s.get("b")["proposal"]["job_id"]), ("CONFLICT", "b"))
        with self.assertRaises(JobStoreError):
            s.mark_delivery_failed("a", "x")

    def test_runner_statuses_map_to_store_states(self):
        self.assertEqual(store_state_for(JobState.SUCCEEDED), "SUCCEEDED")
        self.assertEqual(store_state_for(JobState.CANCELLED_BY_OWNER), "CANCELLED")
        self.assertEqual(store_state_for(JobState.DEADLINE_EXPIRED), "EXPIRED")
        for st in (JobState.BUDGET_EXHAUSTED, JobState.SCHEMA_INVALID, JobState.DENIED_BY_POLICY, JobState.PROVIDER_UNAVAILABLE,
                   JobState.EXCEEDED_TURNS, JobState.EXCEEDED_TOOL_CALLS):
            self.assertEqual(store_state_for(st), "FAILED")
        with self.assertRaises(JobStoreError):
            store_state_for(JobState.RUNNING)

    def test_read_only_handle_refuses_writes(self):
        s = self.store()
        s.submit(envelope(), INPUTS, submitted_by="owner")
        ro = JobStore(s.path, clock_ns=self.clock.now_ns, readonly=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.get("job-1")["state"], "QUEUED")
        with self.assertRaises(JobStoreError):
            ro.cancel("job-1", by="owner")


if __name__ == "__main__":
    unittest.main()
