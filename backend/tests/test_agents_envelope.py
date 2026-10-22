"""Job envelope validation, depth limit and scheduler behaviour (CAP-AGT-01)."""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import SECOND, T0, envelope, sha  # noqa: E402
from unknown.agents.envelope import EnvelopeError, JobEnvelope, JobRegistry, JobState, SchedulerError  # noqa: E402
from unknown.domain.clock import ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.money import Money  # noqa: E402


class EnvelopeValidationTest(unittest.TestCase):
    def test_valid_envelope_round_trips(self):
        env = envelope(allowed_tools=("get_span",), parent_job_id=None)
        self.assertEqual(JobEnvelope.from_dict(env.to_dict()), env)

    def test_rejects_bad_fields(self):
        bad = [
            dict(job_id="bad id with spaces"), dict(job_id="x" * 121), dict(role="Research"), dict(schema_version=0),
            dict(schema_version=True), dict(input_hashes=("nothex",)), dict(input_hashes=(sha("a"), sha("a"))),
            dict(allowed_tools=("Get-Span",)), dict(allowed_tools=("a", "a")), dict(deadline_ns=T0),
            dict(max_turns=0), dict(max_turns=51), dict(max_tool_calls=-1), dict(retry_budget=11), dict(token_cap=0),
            dict(spend_cap=Money("USD", Fixed(0))), dict(output_schema="Research Answer"), dict(parent_job_id="job-1"),
            dict(prompt_template_hash="abc"), dict(max_turns=2.0),
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(EnvelopeError):
                envelope(**kw)

    def test_from_dict_is_strict(self):
        d = envelope().to_dict()
        with self.assertRaises(EnvelopeError):
            JobEnvelope.from_dict({**d, "extra": 1})
        with self.assertRaises(EnvelopeError):
            JobEnvelope.from_dict({k: v for k, v in d.items() if k != "token_cap"})
        with self.assertRaises(EnvelopeError):
            JobEnvelope.from_dict({**d, "spend_cap": {"currency": "USD", "amount": 2.0}})
        with self.assertRaises(EnvelopeError):
            JobEnvelope.from_dict({**d, "spend_cap": {"currency": "USD", "amount": "-1"}})

    def test_dedupe_key_ignores_order_and_identity(self):
        a = envelope(job_id="a", input_hashes=(sha("1"), sha("2")))
        b = envelope(job_id="b", input_hashes=(sha("2"), sha("1")), deadline_ns=T0 + 999 * SECOND)
        c = envelope(job_id="c", input_hashes=(sha("1"), sha("2")), role="critic")
        d = envelope(job_id="d", input_hashes=(sha("1"), sha("2")), output_schema="other_answer")
        self.assertEqual(a.dedupe_key(), b.dedupe_key())
        self.assertNotEqual(a.dedupe_key(), c.dedupe_key())
        self.assertNotEqual(a.dedupe_key(), d.dedupe_key())


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(T0)
        self.reg = JobRegistry(self.clock.now_ns, max_global=2, max_per_role={"research": 1, "critic": 1},
                               dedupe_window_ns=3600 * SECOND)

    def test_depth_limit_and_child_cannot_widen(self):
        root = envelope(job_id="root", allowed_tools=("get_span",))
        self.assertTrue(self.reg.submit(root).accepted)
        child = envelope(job_id="child", parent_job_id="root", allowed_tools=("get_span",), input_hashes=(sha("c"),))
        self.assertTrue(self.reg.submit(child).accepted)
        self.assertEqual(self.reg.record("child").depth, 1)
        grandchild = envelope(job_id="grandchild", parent_job_id="child", input_hashes=(sha("g"),))
        sub = self.reg.submit(grandchild)
        self.assertFalse(sub.accepted)
        self.assertIn("depth", sub.reason)
        self.assertNotIn("grandchild", self.reg.records)
        for kw in (dict(allowed_tools=("get_span", "get_xbrl_fact")), dict(deadline_ns=T0 + 601 * SECOND),
                   dict(token_cap=200_001), dict(spend_cap=Money("USD", Fixed.parse("2.01")))):
            with self.subTest(kw=kw):
                widen = envelope(job_id="w", parent_job_id="root", input_hashes=(sha(str(kw)),), **kw)
                self.assertFalse(self.reg.submit(widen).accepted)
        self.assertFalse(self.reg.submit(envelope(job_id="orphan", parent_job_id="nope", input_hashes=(sha("o"),))).accepted)

    def test_dedupe_returns_one_execution_and_shared_output(self):
        a = envelope(job_id="a")
        b = envelope(job_id="b")
        self.assertTrue(self.reg.submit(a).accepted)
        sub = self.reg.submit(b)
        self.assertEqual(sub.duplicate_of, "a")
        self.assertEqual(self.reg.start_next(), a)
        self.assertIsNone(self.reg.start_next())  # the duplicate never runs
        self.reg.complete("a", JobState.SUCCEEDED, output_hash=sha("out"))
        self.assertEqual(self.reg.output_for("a"), self.reg.output_for("b"))
        self.assertEqual(self.reg.output_for("b"), sha("out"))
        self.assertEqual(sum(1 for e in self.reg.events if e["kind"] == "started"), 1)

    def test_dedupe_window_and_failed_canonical(self):
        self.reg.submit(envelope(job_id="a"))
        self.clock.advance(3601 * SECOND)
        self.assertIsNone(self.reg.submit(envelope(job_id="b", deadline_ns=T0 + 7200 * SECOND, created_ns=T0)).duplicate_of)
        reg = JobRegistry(self.clock.now_ns, max_global=1, max_per_role={"research": 1}, dedupe_window_ns=3600 * SECOND)
        reg.submit(envelope(job_id="x", deadline_ns=T0 + 7200 * SECOND))
        reg.try_start("x")
        reg.complete("x", JobState.PROVIDER_UNAVAILABLE)
        self.assertIsNone(reg.submit(envelope(job_id="y", deadline_ns=T0 + 7200 * SECOND)).duplicate_of)

    def test_same_job_id_with_different_envelope_conflicts(self):
        self.reg.submit(envelope(job_id="a"))
        self.assertTrue(self.reg.submit(envelope(job_id="a")).accepted)
        with self.assertRaises(SchedulerError):
            self.reg.submit(envelope(job_id="a", max_turns=2))

    def test_concurrency_per_role_and_global(self):
        for i in range(3):
            self.reg.submit(envelope(job_id=f"r{i}", input_hashes=(sha(f"r{i}"),)))
        self.reg.submit(envelope(job_id="c0", role="critic", input_hashes=(sha("c0"),)))
        self.reg.submit(envelope(job_id="q0", role="quant", input_hashes=(sha("q0"),)))
        self.assertTrue(self.reg.try_start("r0"))
        self.assertFalse(self.reg.try_start("r1"))          # role limit 1
        self.assertFalse(self.reg.try_start("q0"))          # role without a configured limit never runs
        self.assertTrue(self.reg.try_start("c0"))
        self.reg.submit(envelope(job_id="c1", role="critic", input_hashes=(sha("c1"),)))
        self.assertFalse(self.reg.try_start("c1"))          # global limit 2
        self.reg.complete("r0", JobState.SUCCEEDED, output_hash=sha("o"))
        self.assertTrue(self.reg.try_start("r1"))

    def test_expiry_discards_late_output_and_queued_jobs_expire(self):
        self.reg.submit(envelope(job_id="late"))
        self.reg.submit(envelope(job_id="queued", role="critic", input_hashes=(sha("q"),)))
        self.reg.try_start("late")
        self.clock.advance(601 * SECOND)
        rec = self.reg.complete("late", JobState.SUCCEEDED, output_hash=sha("late-out"))
        self.assertEqual(rec.state, JobState.DEADLINE_EXPIRED)
        self.assertIsNone(rec.output_hash)
        self.assertEqual(rec.discarded_output_hash, sha("late-out"))
        self.assertIn("queued", self.reg.expire_overdue())
        self.assertEqual(self.reg.record("queued").state, JobState.DEADLINE_EXPIRED)
        self.assertTrue(any(e["kind"] == "discarded" for e in self.reg.events))

    def test_relevance_window_marks_research_only(self):
        self.reg.submit(envelope(job_id="rel", relevance_end_ns=T0 + 10 * SECOND))
        self.reg.try_start("rel")
        self.clock.advance(11 * SECOND)
        self.assertTrue(self.reg.complete("rel", JobState.SUCCEEDED, output_hash=sha("x")).research_only)

    def test_cancellation(self):
        self.reg.submit(envelope(job_id="q"))
        self.assertEqual(self.reg.cancel("q"), JobState.CANCELLED_BY_OWNER)
        self.reg.submit(envelope(job_id="run", input_hashes=(sha("run"),)))
        self.reg.try_start("run")
        self.reg.cancel("run")
        self.assertTrue(self.reg.is_cancelled("run"))
        rec = self.reg.complete("run", JobState.SUCCEEDED, output_hash=sha("o"))
        self.assertEqual(rec.state, JobState.CANCELLED_BY_OWNER)
        self.assertEqual(rec.discarded_output_hash, sha("o"))

    def test_deadline_already_passed_is_refused(self):
        self.clock.advance(700 * SECOND)
        self.assertFalse(self.reg.submit(envelope(job_id="old")).accepted)


if __name__ == "__main__":
    unittest.main()
