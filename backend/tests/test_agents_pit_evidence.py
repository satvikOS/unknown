"""Evidence spans from the point-in-time store and research-job submission (ADR-0015, ADR-0016)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import (FIX_ENTITY, FIX_TICKER, INGESTED_AT, SECOND, T0, TempDirCase,  # noqa: E402
                                  fixture_observation, owner, research_store)
from unknown.agents.pit_evidence import (EvidenceError, EvidenceSelection, derive, manifest_matches,  # noqa: E402
                                         resolve_identifiers, selection, span_hash)
from unknown.agents.prompts import PROMPT_TEMPLATE_HASH  # noqa: E402
from unknown.agents.submission import SubmissionError, SubmissionRefused, build_research_job  # noqa: E402
from unknown.data.pit import ResearchStore  # noqa: E402
from unknown.domain.canonical import sha256_hex  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402

FIELDS = ("us-gaap:Revenues", "us-gaap:Assets")


class EvidenceTest(TempDirCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "research" / "research.sqlite"
        self.store = research_store(self.path)
        self.addCleanup(self.store.close)

    def test_spans_are_point_in_time_classified_and_quotable(self):
        ev = derive(self.store, selection(self.store, [FIX_ENTITY], FIELDS, T0))
        texts = [s.text for s in ev.spans]
        self.assertEqual([s.span_id for s in ev.spans], ["S1", "S2", "S3", "S4"])
        self.assertIn("concept=us-gaap:Revenues period=2023-10-01..2024-09-28 (annual) unit=USD value=1000000.", texts[0])
        self.assertIn("period=2024-09-29..2025-09-27 (annual) unit=USD value=1200000.", texts[1])   # restatement not yet available
        self.assertIn("period=2025-03-30..2025-06-28 (quarterly) unit=USD value=290000.", texts[2])
        self.assertIn("concept=us-gaap:Assets period=2025-09-27 (instant) unit=USD value=5000000.", texts[3])
        self.assertFalse(any("880000" in t for t in texts))                                           # year-to-date: never a span
        self.assertTrue(texts[0].startswith("Fixture Industries Inc. (SEC-CIK-0000000001)"))
        self.assertIn("10-K accession 0000000001-24-000010", texts[0])
        self.assertEqual({s.dataset_id for s in ev.spans}, {"sec.companyfacts"})
        for s in ev.spans:
            self.assertEqual(s.content_hash, span_hash(s.span_id, s.dataset_id, s.text))
        self.assertEqual([(f.concept, f.period, f.unit, f.value) for f in ev.facts][1],
                         ("us-gaap:Revenues", "2024-09-29..2025-09-27", "USD", Fixed(1200000)))

    def test_pinned_evidence_ignores_later_knowledge_and_later_publications(self):
        sel = selection(self.store, [FIX_ENTITY], FIELDS, T0)
        before = derive(self.store, sel)
        rw = ResearchStore(self.path)          # a silent correction of the FY2024 10-K, recorded after the pinned instant
        from unknown.domain.clock import utc_ns
        rw.record(fixture_observation("us-gaap:Revenues", "2023-10-01", "2024-09-28", "1000001", utc_ns(2024, 11, 1, 21),
                                      "0000000001-24-000010"), T0 + 3600 * SECOND)
        rw.close()
        self.assertEqual(derive(self.store, sel), before)
        later = derive(self.store, EvidenceSelection(sel.entity_ids, sel.fields, T0 + 2 * 86_400 * SECOND))
        later_texts = " ".join(s.text for s in later.spans)
        self.assertIn("value=1000001.", later_texts)            # the correction, once known
        self.assertIn("value=1150000.", later_texts)            # the 10-K/A, once available
        self.assertIsNotNone(manifest_matches(before.manifest(), later))
        self.assertIsNone(manifest_matches(before.manifest(), derive(self.store, sel)))

    def test_identifiers_resolve_to_entities(self):
        self.assertEqual(resolve_identifiers(self.store, [FIX_ENTITY], T0), (FIX_ENTITY,))
        self.assertEqual(resolve_identifiers(self.store, [FIX_TICKER.lower(), FIX_ENTITY], T0), (FIX_ENTITY,))
        with self.assertRaises(EvidenceError):
            resolve_identifiers(self.store, ["NOPE"], T0)
        with self.assertRaises(EvidenceError):
            resolve_identifiers(self.store, [FIX_TICKER], INGESTED_AT - SECOND)      # alias not valid yet
        rw = ResearchStore(self.path)
        rw.upsert_entity("SEC-CIK-0000000002", "issuer", "Other", {})
        rw.add_alias("ticker:NYSE", FIX_TICKER, "SEC-CIK-0000000002", INGESTED_AT, "fixture")
        rw.close()
        with self.assertRaises(EvidenceError):
            resolve_identifiers(self.store, [FIX_TICKER], T0)                        # ambiguous across exchanges
        for bad in (["a b"], ["1", "2", "3", "4", "5", "6"], FIX_TICKER):
            with self.assertRaises(EvidenceError):
                resolve_identifiers(self.store, bad, T0)
        for fields in (["Revenues"], [], ["us-gaap:Rev enues"]):
            with self.assertRaises(EvidenceError):
                selection(self.store, [FIX_ENTITY], fields, T0)

    def test_selection_round_trip(self):
        sel = selection(self.store, [FIX_TICKER], None, T0)
        self.assertEqual(EvidenceSelection.from_dict(sel.to_dict()), sel)
        with self.assertRaises(EvidenceError):
            EvidenceSelection.from_dict({**sel.to_dict(), "extra": 1})


class SubmissionTest(TempDirCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "research" / "research.sqlite"
        research_store(self.path).close()

    def build(self, body, policy="default", **kw):
        return build_research_job(body, policy=owner() if policy == "default" else policy, research_path=self.path,
                                  now_ns=T0, **kw)

    def test_envelope_binds_the_task_and_every_span(self):
        env, inputs = self.build({"question": "Did revenue grow in fiscal 2025?", "issuer_ids": [FIX_TICKER],
                                  "fields": list(FIELDS)}, job_id="rj-test")
        spans = inputs["evidence"]["spans"]
        self.assertEqual(len(spans), 4)
        self.assertEqual(env.input_hashes, (sha256_hex(inputs["task"]), *(s["content_hash"] for s in spans)))
        self.assertEqual(inputs["task_hash"], sha256_hex(inputs["task"]))
        self.assertTrue(inputs["task"].startswith("Did revenue grow in fiscal 2025?"))
        self.assertEqual((env.role, env.output_schema, env.prompt_template_hash, env.allowed_tools, env.max_tool_calls),
                         ("research", "research_answer", PROMPT_TEMPLATE_HASH, (), 0))
        self.assertEqual(env.spend_cap, owner().caps.per_job)
        self.assertEqual(env.deadline_ns, T0 + 3600 * SECOND)
        self.assertEqual(inputs["evidence"]["selection"], {"entity_ids": [FIX_ENTITY], "fields": list(FIELDS), "as_of_ns": T0})
        again, _ = self.build({"question": "Did revenue grow in fiscal 2025?", "issuer_ids": [FIX_ENTITY], "fields": list(FIELDS)})
        self.assertEqual(again.dedupe_key(), env.dedupe_key())          # same question and evidence: same CAP-AGT-01 key
        other, _ = self.build({"question": "Different question", "issuer_ids": [FIX_ENTITY], "fields": list(FIELDS)})
        self.assertNotEqual(other.dedupe_key(), env.dedupe_key())

    def test_question_without_issuers_has_no_spans(self):
        env, inputs = self.build({"question": "What is a 10-K?", "spend_cap": "0.50", "max_output_tokens": 1000})
        self.assertEqual(inputs["evidence"], {"selection": None, "spans": []})
        self.assertEqual(env.input_hashes, (sha256_hex(inputs["task"]),))
        self.assertEqual(str(env.spend_cap.amount.to_str()), "0.50")
        self.assertEqual(inputs["max_output_tokens"], 1000)

    def test_refusals(self):
        with self.assertRaises(SubmissionRefused):
            self.build({"question": "q"}, policy=None)
        with self.assertRaises(SubmissionRefused):
            self.build({"question": "q"}, policy=owner(caps__per_day="0"))
        with self.assertRaises(SubmissionRefused):
            build_research_job({"question": "q", "issuer_ids": [FIX_TICKER]}, policy=owner(),
                               research_path=self.tmp / "missing.sqlite", now_ns=T0)
        bad = [{"question": ""}, {"question": "q" * 4001}, {"question": "q", "extra": 1}, {"question": "q", "role": "Bad"},
               {"question": "q", "spend_cap": 2}, {"question": "q", "spend_cap": "2.01"}, {"question": "q", "spend_cap": "0"},
               {"question": "q", "max_output_tokens": 10}, {"question": "q", "deadline_seconds": 10 ** 9},
               {"question": "q", "issuer_ids": ["NOPE"]}, {"question": "q", "fields": ["us-gaap:Revenues"]}, ["question"]]
        for body in bad:
            with self.subTest(body=str(body)[:60]):
                with self.assertRaises(SubmissionError):
                    self.build(body)


if __name__ == "__main__":
    unittest.main()
