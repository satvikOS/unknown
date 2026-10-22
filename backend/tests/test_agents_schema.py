"""Local JSON-Schema subset validator and provider compatibility checker (CAP-MODEL-02)."""
from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unknown.agents.common import CONTRACTS  # noqa: E402
from unknown.agents.runner import load_output_schema  # noqa: E402
from unknown.agents.schema import (Enforcement, SchemaError, SchemaRejected, Validator, compile_for,  # noqa: E402
                                   parse_model_json)

OBJ = {"type": "object", "additionalProperties": False, "required": ["a"], "properties": {"a": {"type": "string"}}}


class ValidatorTest(unittest.TestCase):
    def test_research_proposal_contract(self):
        v = Validator(json.loads((CONTRACTS / "research_proposal.schema.json").read_text()))
        ok = {"schema_version": 1, "job_id": "j", "hypothesis": "h", "evidence": ["e"], "horizon_seconds": 60,
              "assumptions": [], "abstain": False, "reason": "r"}
        self.assertEqual(v.errors(ok), [])
        for mutate in (dict(schema_version=2), dict(schema_version=True), dict(job_id=""), dict(horizon_seconds=0),
                       dict(horizon_seconds=31_536_001), dict(horizon_seconds=True), dict(evidence=["x" * 501]),
                       dict(evidence=["e"] * 51), dict(abstain=1), dict(extra=1), dict(hypothesis="h" * 4001)):
            with self.subTest(mutate=mutate):
                self.assertTrue(v.errors({**ok, **mutate}))
        self.assertTrue(v.errors({k: x for k, x in ok.items() if k != "reason"}))

    def test_keywords(self):
        s = {"type": "object", "additionalProperties": False, "required": ["n", "s", "e", "c", "arr", "opt"],
             "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 10},
                            "s": {"type": "string", "minLength": 2, "maxLength": 3},
                            "e": {"enum": ["x", 1, True]}, "c": {"const": 1},
                            "arr": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "number"}},
                            "opt": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
        v = Validator(s)
        good = {"n": 5, "s": "ab", "e": True, "c": 1, "arr": [1, Decimal("2.5")], "opt": None}
        self.assertEqual(v.errors(good), [])
        for bad in (dict(n=0), dict(n=True), dict(s="a"), dict(s="abcd"), dict(e=False), dict(e=2), dict(c=True),
                    dict(arr=[]), dict(arr=[1, 2, 3]), dict(arr=[1.5]), dict(opt=3), dict(n=5.0)):
            with self.subTest(bad=bad):
                self.assertTrue(v.errors({**good, **bad}))

    def test_refs_and_combinators(self):
        s = {"$defs": {"pos": {"type": "integer", "minimum": 1}}, "type": "object", "additionalProperties": False,
             "required": ["a"], "properties": {"a": {"$ref": "#/$defs/pos"}, "b": {"allOf": [{"type": "string"}, {"maxLength": 2}]},
                                               "c": {"not": {"type": "null"}},
                                               "d": {"if": {"type": "integer"}, "then": {"minimum": 5}, "else": {"type": "string"}}}}
        v = Validator(s)
        self.assertEqual(v.errors({"a": 2, "b": "ok", "c": 1, "d": 7}), [])
        self.assertTrue(v.errors({"a": 0}))
        self.assertTrue(v.errors({"a": 1, "b": "long"}))
        self.assertTrue(v.errors({"a": 1, "c": None}))
        self.assertTrue(v.errors({"a": 1, "d": 3}))
        self.assertTrue(v.errors({"a": 1, "d": True}))

    def test_unsupported_keywords_fail_closed(self):
        for s in ({"type": "string", "pattern": "^a"}, {"type": "string", "format": "date"},
                  {"type": "array", "items": [{"type": "string"}]}, {"type": "object", "additionalProperties": {"type": "string"}},
                  {"$ref": "https://example.invalid/s.json"}, {"type": "integer", "minimum": 1.5},
                  {"type": "object", "properties": {"x": True}}, {"type": "number", "maximum": 2.5}):
            with self.subTest(s=s), self.assertRaises(SchemaError):
                Validator(s)

    def test_model_json_parsing_is_exact(self):
        self.assertEqual(parse_model_json('{"x": 0.1}')["x"], Decimal("0.1"))
        with self.assertRaises(ValueError):
            parse_model_json('{"x": NaN}')


class CompatibilityTest(unittest.TestCase):
    def test_project_schema_compiles_for_every_provider(self):
        s = load_output_schema("research_answer", 1)
        for p in ("openai", "anthropic", "xai", "gemini"):
            with self.subTest(provider=p):
                c = compile_for(p, s)
                self.assertTrue(c.local.is_valid(json.loads('{"hypothesis":"h","claims":[],"assumptions":[],"abstain":true,"reason":"r","tool_requests":[]}')))

    def test_rejections_without_network(self):
        with self.assertRaises(SchemaRejected):
            compile_for("openai", {**OBJ, "allOf": [{"required": ["a"]}]})
        recursive = {"$defs": {"node": {"type": "object", "additionalProperties": False, "required": ["next"],
                                        "properties": {"next": {"anyOf": [{"$ref": "#/$defs/node"}, {"type": "null"}]}}}},
                     "$ref": "#/$defs/node"}
        Validator(recursive)  # the local validator handles recursion
        with self.assertRaises(SchemaRejected) as ctx:
            compile_for("anthropic", recursive)
        self.assertIn("recursive", str(ctx.exception))
        with self.assertRaises(SchemaRejected) as ctx:
            compile_for("xai", {"type": "array", "items": [{"type": "string"}]})
        self.assertTrue(any(f.keyword == "items" and f.enforcement is Enforcement.REJECTED and "xAI" in f.reason
                            for f in ctx.exception.findings))
        with self.assertRaises(SchemaRejected):  # OpenAI strict: every property required
            compile_for("openai", {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}})
        with self.assertRaises(SchemaRejected):  # Anthropic: additionalProperties must be false
            compile_for("anthropic", {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]})

    def test_best_effort_and_not_enforced_move_to_local_validation(self):
        s = {**OBJ, "if": {"required": ["a"]}, "then": {"properties": {"a": {"minLength": 2}}}}
        c = compile_for("xai", s)
        self.assertTrue(any(f.keyword == "if" and f.enforcement is Enforcement.BEST_EFFORT for f in c.findings))
        self.assertFalse(c.local.is_valid({"a": "x"}))
        g = compile_for("gemini", {**OBJ, "properties": {"a": {"type": "string", "maxLength": 3}}})
        self.assertTrue(any(f.keyword == "maxLength" and f.enforcement is Enforcement.NOT_ENFORCED for f in g.findings))
        self.assertFalse(g.local.is_valid({"a": "long"}))

    def test_anthropic_strips_unsupported_constraints_but_validates_them(self):
        s = {"type": "object", "additionalProperties": False, "required": ["n", "l"],
             "properties": {"n": {"type": "integer", "minimum": 1}, "l": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 3}}}}
        c = compile_for("anthropic", s)
        text = json.dumps(c.provider_schema)
        for kw in ("minimum", "maxItems", "maxLength"):
            self.assertNotIn(kw, text)
        self.assertIn("maxItems", json.dumps(s))  # the canonical schema is untouched
        self.assertFalse(c.local.is_valid({"n": 0, "l": []}))
        self.assertFalse(c.local.is_valid({"n": 1, "l": ["a", "b", "c"]}))

    def test_limits(self):
        many = {"type": "object", "additionalProperties": False, "required": [f"p{i}" for i in range(30)],
                "properties": {f"p{i}": {"anyOf": [{"type": "string"}, {"type": "null"}]} for i in range(30)}}
        with self.assertRaises(SchemaRejected):
            compile_for("anthropic", many)  # 30 union-typed parameters > 16
        big_enum = {"type": "string", "enum": [f"v{i}" for i in range(1001)]}
        with self.assertRaises(SchemaRejected):
            compile_for("openai", {**OBJ, "properties": {"a": big_enum}})
        self.assertTrue(any(f.enforcement is Enforcement.BEST_EFFORT
                            for f in compile_for("xai", {**OBJ, "properties": {"a": {"type": "string", "maxLength": 5000}}}).findings))


if __name__ == "__main__":
    unittest.main()
