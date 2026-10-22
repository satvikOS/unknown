"""research_proposal schema version 2 (finding BR-DAT-12; book Ch. 19 line 13; ADR-0016 sections 1 and 7).

The contract (``contracts/research_proposal.schema.json``) keeps version 1 and adds version 2: typed evidence
references, instrument and entity ids, the job's as-of instant, an observation-time window, an optional forecast
block and abstain-dependent requirements. The core validates version 2 strictly and records it with a label; it
still grants nothing.

Every number, id and text below is a TEST FIXTURE."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402
from support import T0 as CORE_T0  # noqa: E402
from unknown.agents.common import CONTRACTS  # noqa: E402
from unknown.agents.schema import Validator  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.store.schema import PROJECTION_TABLES  # noqa: E402

CONTRACT = Validator(json.loads((CONTRACTS / "research_proposal.schema.json").read_text()))
SECOND = 1_000_000_000
DAY = 86_400 * SECOND
AS_OF = CORE_T0 - 3600 * SECOND          # the job's pinned instant, one hour before the core's clock
AVAILABLE = AS_OF - DAY
OBSERVED = AVAILABLE - 30 * DAY
INSTRUMENT = "FIX-EQ-1"                  # registered by Harness.bootstrap
ENTITY = "SEC-CIK-0000000001"


# ---------------------------------------------------------------------------- fixtures
def ref(i: int = 0, **kw) -> dict:
    r = {"span_id": f"S{i + 1}", "dataset_id": "sec.companyfacts", "ref_type": "observation", "ref_id": f"obs:{i + 1}",
         "content_hash": format(i + 1, "064x"), "observed_at_ns": OBSERVED, "available_at_ns": AVAILABLE}
    r.update(kw)
    return r


def v1(**kw) -> dict:
    body = {"schema_version": 1, "job_id": "job-fixture-1", "hypothesis": "fixture hypothesis (test only)",
            "evidence": ["fixture evidence"], "horizon_seconds": 3600, "assumptions": ["fixture assumption"],
            "abstain": False, "reason": "fixture reason"}
    body.update(kw)
    return body


def v2(**kw) -> dict:
    body = {"schema_version": 2, "job_id": "job-fixture-2", "as_of_ns": AS_OF, "hypothesis": "fixture hypothesis (test only)",
            "claims": [{"text": "fixture claim", "quote": "fixture quote", "evidence_indexes": [0]}],
            "evidence": [ref(0)], "instrument_ids": [INSTRUMENT], "entity_ids": [ENTITY],
            "observation_window": {"from_ns": OBSERVED, "to_ns": OBSERVED}, "horizon_seconds": 86_400,
            "assumptions": ["fixture assumption"], "forecast": None, "abstain": False, "reason": "fixture reason"}
    body.update(kw)
    return body


def abstaining(**kw) -> dict:
    return v2(**{"hypothesis": "", "claims": [], "evidence": [], "observation_window": None, "horizon_seconds": None,
                 "abstain": True, "reason": "fixture: the evidence is insufficient", **kw})


def forecast(**kw) -> dict:
    f = {"target": {"subject_id": INSTRUMENT, "quantity": "return", "definition": "simple total return over the horizon"},
         "horizon_seconds": 30 * 86_400, "unit": "fraction",
         "distribution": {"kind": "quantiles", "points": [{"p": "0.1", "value": "-0.08"}, {"p": "0.5", "value": "0.01"},
                                                          {"p": "0.9", "value": "0.09"}]},
         "calibration": {"method": "isotonic regression (fixture)", "version": "cal-fixture-1"},
         "uncertainty": {"observation_count": 1200, "effective_sample_size": "310.5",
                         "training_window": {"from_ns": OBSERVED - 365 * DAY, "to_ns": OBSERVED},
                         "evaluation_window": {"from_ns": OBSERVED, "to_ns": AVAILABLE},
                         "interval_method": "stationary block bootstrap (fixture)", "data_version": "pit-fixture-1"},
         "invalidation_conditions": ["fixture: the cited revenue is restated"]}
    f.update(kw)
    return f


def nested(base: dict, path: str, value) -> dict:
    """A deep copy of ``base`` with the dotted ``path`` set (list indexes are integers)."""
    out = copy.deepcopy(base)
    target = out
    parts = [int(p) if p.isdigit() else p for p in path.split(".")]
    for p in parts[:-1]:
        target = target[p]
    target[parts[-1]] = value
    return out


def projections(h: Harness) -> dict:
    return {t: [tuple(r) for r in h.desk.store.query(f"SELECT * FROM {t} ORDER BY 1")] for t in PROJECTION_TABLES}


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.bootstrap()

    def submit(self, body, **extra):
        return self.h.cmd("submit_research_proposal", self.h.agent, proposal=body, **extra)

    def count(self) -> int:
        return self.h.desk.store.query("SELECT COUNT(*) AS n FROM proposals")[0]["n"]


# ============================================================================ the contract and the core
class ProposalVersionOneStillAcceptedTest(CoreCase):
    def test_v1_is_still_accepted_and_recorded_unchanged(self):
        """Proposals already waiting in a worker's outbox were built as version 1; the core still records them, with
        the same row and event shape as before (no label)."""
        body = v1()
        self.assertEqual(CONTRACT.errors(body), [])
        r = self.submit(body)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(set(r.body), {"proposal_id"})
        row = self.h.row("proposals", "proposal_id", r.body["proposal_id"])
        self.assertEqual(set(row), {"proposal_id", "author", "body", "recorded_at_ns"})
        self.assertEqual(row["body"], body)
        for bad in (v1(schema_version=3), v1(evidence=[{"dataset_id": "x"}]), v1(extra=1)):
            with self.subTest(bad=bad):
                self.assertTrue(CONTRACT.errors(bad))
                self.assertEqual(self.submit(bad).rejected_code, "PROPOSAL_INVALID")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.h.verify(), [])


class ProposalVersionTwoValidationTest(CoreCase):
    def test_valid_v2_proposals_are_recorded_with_the_core_label(self):
        """Contract and core agree on every valid case; the label is assigned by the core, not by the author."""
        uncalibrated = forecast(calibration={"method": None, "version": None},
                                uncertainty={"observation_count": None, "effective_sample_size": None, "training_window": None,
                                             "evaluation_window": None, "interval_method": None, "data_version": None})
        cases = {
            "qualitative": (v2(), "QUALITATIVE"),
            "abstention": (abstaining(), "ABSTENTION"),
            "abstention_keeps_cited_evidence": (v2(abstain=True, reason="fixture: contradictory filings"), "ABSTENTION"),
            "forecast_quantified": (v2(forecast=forecast()), "FORECAST_QUANTIFIED"),
            "forecast_uncalibrated": (v2(forecast=uncalibrated), "FORECAST_UNCALIBRATED"),
            "forecast_interval_on_entity": (v2(forecast=forecast(
                target={"subject_id": ENTITY, "quantity": "fundamental", "definition": "next fiscal-year revenue"},
                unit="USD", distribution={"kind": "interval", "level": "0.9", "lower": "1100000", "upper": "1400000"})),
                "FORECAST_QUANTIFIED"),
            "observed_unknown_window_uses_available": (v2(evidence=[ref(0, observed_at_ns=None)],
                                                          observation_window={"from_ns": AVAILABLE, "to_ns": AVAILABLE}),
                                                       "QUALITATIVE"),
            "document_span_ref": (v2(evidence=[ref(0, ref_type="document_span", ref_id="0000000001-25-000010#p12")]),
                                  "QUALITATIVE"),
            "no_instruments": (v2(instrument_ids=[]), "QUALITATIVE"),
            "as_of_equals_core_clock": (v2(as_of_ns=CORE_T0, observation_window={"from_ns": OBSERVED, "to_ns": CORE_T0}),
                                        "QUALITATIVE"),
            "limits": (v2(hypothesis="h" * 4000, reason="r" * 2000, assumptions=["a" * 500] * 30,
                          evidence=[ref(i) for i in range(50)],
                          claims=[{"text": "t" * 1000, "quote": "q" * 1000, "evidence_indexes": list(range(10))}] * 20,
                          instrument_ids=[INSTRUMENT], entity_ids=[f"E-{i}" for i in range(20)], horizon_seconds=31_536_000),
                       "QUALITATIVE"),
        }
        for name, (body, label) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(CONTRACT.errors(body), [])
                r = self.submit(body)
                self.assertTrue(r.accepted, r.body)
                self.assertEqual((r.body["schema_version"], r.body["label"]), (2, label))
                row = self.h.row("proposals", "proposal_id", r.body["proposal_id"])
                self.assertEqual((row["body"], row["label"], row["author"]), (body, label, "agent.research"))
        self.assertEqual(self.count(), len(cases))
        self.assertEqual(self.h.verify(), [])

    def test_invalid_v2_proposals_are_rejected_and_record_nothing(self):
        """The core rejects every case. ``schema`` says whether the JSON contract alone also rejects it; the rest
        (timestamps, hashes, identifiers, the instrument master, indexes, ordering, units) only the core can check."""
        f = forecast()
        cases = {
            # shape and types (the contract sees these too)
            "missing_key": ({k: v for k, v in v2().items() if k != "observation_window"}, True),
            "extra_key_order": (v2(order={"side": "BUY", "quantity": "10", "limit_price": "9.00"}), True),
            "as_of_bool": (v2(as_of_ns=True), True),
            "as_of_string": (v2(as_of_ns=str(AS_OF)), True),
            "as_of_negative": (v2(as_of_ns=-1), True),
            "job_id_empty": (v2(job_id=""), True),
            "hypothesis_4001": (v2(hypothesis="h" * 4001), True),
            "evidence_51": (v2(evidence=[ref(i) for i in range(51)]), True),
            "evidence_ref_extra_key": (v2(evidence=[ref(0, note="trust me")]), True),
            "evidence_ref_missing_hash": (v2(evidence=[{k: v for k, v in ref(0).items() if k != "content_hash"}]), True),
            "evidence_ref_type_unknown": (v2(evidence=[ref(0, ref_type="rumour")]), True),
            "evidence_hash_63": (v2(evidence=[ref(0, content_hash="a" * 63)]), True),
            "evidence_available_string": (v2(evidence=[ref(0, available_at_ns=str(AVAILABLE))]), True),
            "claims_21": (v2(claims=[{"text": "t", "quote": None, "evidence_indexes": [0]}] * 21), True),
            "claim_without_evidence": (v2(claims=[{"text": "t", "quote": None, "evidence_indexes": []}]), True),
            "claim_index_bool": (v2(claims=[{"text": "t", "quote": None, "evidence_indexes": [False]}]), True),
            "instruments_21": (v2(instrument_ids=[f"I-{i}" for i in range(21)]), True),
            "instruments_duplicate": (v2(instrument_ids=[INSTRUMENT, INSTRUMENT]), True),
            "horizon_zero": (v2(horizon_seconds=0), True),
            "horizon_too_long": (v2(horizon_seconds=31_536_001), True),
            "window_extra_key": (v2(observation_window={"from_ns": OBSERVED, "to_ns": OBSERVED, "tz": "UTC"}), True),
            "assumption_501": (v2(assumptions=["a" * 501]), True),
            # abstain-dependent requirements
            "not_abstaining_without_evidence": (v2(evidence=[], claims=[]), True),
            "not_abstaining_without_claims": (v2(claims=[]), True),
            "not_abstaining_without_hypothesis": (v2(hypothesis=""), True),
            "not_abstaining_without_window": (v2(observation_window=None), True),
            "not_abstaining_without_horizon": (v2(horizon_seconds=None), True),
            "abstaining_without_reason": (abstaining(reason=""), True),
            "abstaining_with_forecast": (abstaining(forecast=forecast()), True),
            "blank_hypothesis": (v2(hypothesis="   "), False),
            "blank_abstention_reason": (abstaining(reason="  "), False),
            # timestamps (core only)
            "as_of_in_the_future": (v2(as_of_ns=self.h.clock.now_ns() + 1), False),
            "window_reversed": (v2(observation_window={"from_ns": OBSERVED + 1, "to_ns": OBSERVED}), False),
            "window_after_as_of": (v2(observation_window={"from_ns": OBSERVED, "to_ns": AS_OF + 1}), False),
            "available_after_as_of": (v2(evidence=[ref(0, available_at_ns=AS_OF + 1)]), False),
            "observed_after_as_of": (v2(evidence=[ref(0, observed_at_ns=AS_OF + 1)],
                                        observation_window={"from_ns": OBSERVED, "to_ns": AS_OF}), False),
            "observed_outside_window": (v2(observation_window={"from_ns": OBSERVED + 1, "to_ns": AS_OF}), False),
            # identity and syntax (core only)
            "instrument_unknown": (v2(instrument_ids=["NOT-REGISTERED-1"]), False),
            "instrument_bad_syntax": (v2(instrument_ids=["FIX EQ 1"]), False),
            "entity_bad_syntax": (v2(entity_ids=["<script>"]), False),
            "hash_uppercase": (v2(evidence=[ref(0, content_hash="A" * 64)]), False),
            "hash_not_hex": (v2(evidence=[ref(0, content_hash="z" * 64)]), False),
            "dataset_id_bad": (v2(evidence=[ref(0, dataset_id="SEC Companyfacts")]), False),
            "ref_id_bad": (v2(evidence=[ref(0, ref_id="obs 1; drop table")]), False),
            "span_id_bad": (v2(evidence=[ref(0, span_id="S 1")]), False),
            "duplicate_reference": (v2(evidence=[ref(0), ref(0, span_id="S9")]), False),
            "claim_index_out_of_range": (v2(claims=[{"text": "t", "quote": None, "evidence_indexes": [1]}]), False),
            "claim_text_blank": (v2(claims=[{"text": " ", "quote": None, "evidence_indexes": [0]}]), False),
            # forecast block
            "forecast_missing_invalidation": (v2(forecast=forecast(invalidation_conditions=[])), True),
            "forecast_trade_instruction": (v2(forecast=nested(f, "target.quantity", "buy")), True),
            "forecast_order_field": (v2(forecast=forecast(side="BUY")), True),
            "forecast_number_not_string": (v2(forecast=nested(f, "distribution.points.0.value", 1)), True),
            "forecast_single_point": (v2(forecast=nested(f, "distribution.points", [{"p": "0.5", "value": "0"}])), True),
            "forecast_unknown_kind": (v2(forecast=forecast(distribution={"kind": "vibes", "points": []})), True),
            "forecast_count_negative": (v2(forecast=nested(f, "uncertainty.observation_count", -1)), True),
            "forecast_subject_not_named": (v2(forecast=nested(f, "target.subject_id", "OTHER-1")), False),
            "forecast_unit_missing_currency": (v2(forecast=forecast(unit="dollars")), False),
            "forecast_p_not_increasing": (v2(forecast=nested(f, "distribution.points.1.p", "0.1")), False),
            "forecast_values_decreasing": (v2(forecast=nested(f, "distribution.points.2.value", "-0.5")), False),
            "forecast_p_one": (v2(forecast=nested(f, "distribution.points.2.p", "1")), False),
            "forecast_not_a_decimal": (v2(forecast=nested(f, "distribution.points.0.value", "NaN")), False),
            "forecast_exponent": (v2(forecast=nested(f, "distribution.points.0.value", "1e-3")), False),
            "forecast_interval_reversed": (v2(forecast=forecast(distribution={"kind": "interval", "level": "0.9",
                                                                             "lower": "2", "upper": "1"})), False),
            "forecast_level_one": (v2(forecast=forecast(distribution={"kind": "interval", "level": "1",
                                                                     "lower": "1", "upper": "2"})), False),
            "forecast_training_after_as_of": (v2(forecast=nested(f, "uncertainty.training_window",
                                                                {"from_ns": OBSERVED, "to_ns": AS_OF + 1})), False),
            "forecast_negative_effective_sample": (v2(forecast=nested(f, "uncertainty.effective_sample_size", "-1")), False),
        }
        # Core-only cases: the rejection names the check that caught it (not an unrelated earlier one).
        hints = {
            "blank_hypothesis": "hypothesis: required", "blank_abstention_reason": "reason: an abstention needs a reason",
            "as_of_in_the_future": "as_of_ns: is in the future", "window_reversed": "from_ns is after to_ns",
            "window_after_as_of": "observation_window: to_ns is after", "available_after_as_of": "available_at_ns: is after",
            "observed_after_as_of": "observed_at_ns: is after", "observed_outside_window": "outside observation_window",
            "instrument_unknown": "not in the instrument master", "instrument_bad_syntax": "instrument_ids[0]",
            "entity_bad_syntax": "entity_ids[0]", "hash_uppercase": "content_hash", "hash_not_hex": "content_hash",
            "dataset_id_bad": "dataset_id", "ref_id_bad": "ref_id", "span_id_bad": "span_id",
            "duplicate_reference": "duplicate evidence reference", "claim_index_out_of_range": "evidence_indexes[0]",
            "claim_text_blank": "claims[0].text", "forecast_subject_not_named": "subject_id",
            "forecast_unit_missing_currency": "forecast.unit", "forecast_p_not_increasing": "probabilities must increase",
            "forecast_values_decreasing": "values must not decrease", "forecast_p_one": "points[2].p",
            "forecast_not_a_decimal": "not a plain decimal", "forecast_exponent": "not a plain decimal",
            "forecast_interval_reversed": "lower is above upper", "forecast_level_one": "distribution.level",
            "forecast_training_after_as_of": "training_window", "forecast_negative_effective_sample": "effective_sample_size",
        }
        self.assertEqual(set(hints), {n for n, (_, schema_rejects) in cases.items() if not schema_rejects})
        for name, (body, schema_rejects) in cases.items():
            with self.subTest(case=name):
                r = self.submit(body)
                self.assertEqual(r.rejected_code, "PROPOSAL_INVALID", f"{name} accepted: {r.body}")
                self.assertEqual(bool(CONTRACT.errors(body)), schema_rejects, CONTRACT.errors(body))
                if name in hints:
                    self.assertIn(hints[name], r.body["message"])
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.h.verify(), [])

    def test_a_v2_proposal_grants_nothing(self):
        """AUT-04 / ADR-0007: a recorded v2 proposal, even a quantified forecast with order fields riding alongside,
        changes only the proposals table: no order, hold, outbox row, reservation, policy or balance."""
        before = projections(self.h)
        r = self.submit(v2(forecast=forecast()), account_key=ACCOUNT, instrument_id=INSTRUMENT, side="BUY", quantity="10",
                        limit_price="9.00", order_id="AGENT-1")
        self.assertTrue(r.accepted, r.body)
        (seq,) = r.events
        ev = self.h.desk.store.query("SELECT type, payload FROM events WHERE seq=?", (seq,))[0]
        self.assertEqual(ev["type"], "ResearchProposalRecorded")
        self.assertEqual(list(json.loads(ev["payload"])["changes"]), ["proposals"])
        after = projections(self.h)
        for table in PROJECTION_TABLES:
            if table != "proposals":
                self.assertEqual(after[table], before[table], table)
        self.assertIsNone(self.h.row("orders", "order_id", "AGENT-1"))
        self.assertEqual(self.h.pump().sent, 0)
        self.assertEqual(self.h.verify(), [])

    def test_replay_reproduces_the_recorded_v2_rows(self):
        self.assertTrue(self.submit(v2(forecast=forecast())).accepted)
        self.assertTrue(self.submit(abstaining()).accepted)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(self.h.db_path, Path(d) / "replayed.sqlite"), [])
        self.assertEqual(self.h.verify(), [])


if __name__ == "__main__":
    unittest.main()
