"""Data-use gate: dataset rights x provider data policy x owner data policy (CAP-MODEL-09/10, CAP-AGT-08)."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import DELETE, MODEL, SECOND, TempDirCase, owner, registry  # noqa: E402
from unknown.agents.gate import DataUseGate, load_provider_policies  # noqa: E402

H = "a" * 64


class GateTest(TempDirCase, unittest.TestCase):
    def gate(self, pol="default", policies=None):
        return DataUseGate(registry(), owner() if pol == "default" else pol, policies or load_provider_policies(), self.clock.now_ns)

    def decide(self, gate, datasets=("fixture.public",), provider="anthropic", model=MODEL, feature="generate"):
        return gate.evaluate(provider=provider, model_id=model, feature=feature, dataset_ids=datasets, request_hash=H)

    def test_allows_public_dataset_and_records_decision(self):
        g = self.gate()
        d = self.decide(g)
        self.assertTrue(d.allow, d.reasons)
        self.assertEqual(d.request_hash, H)
        self.assertEqual(g.decisions[-1], d)
        self.assertTrue(self.decide(g, feature="count_tokens").allow)

    def test_dataset_without_model_processing_rights_is_blocked(self):
        d = self.decide(self.gate(), datasets=("fixture.public", "fixture.restricted"))
        self.assertFalse(d.allow)
        self.assertIn("rights:fixture.restricted:model_processing_cloud_not_granted", d.reasons)
        self.assertFalse(self.decide(self.gate(), datasets=("unregistered.ds",)).allow)
        self.assertFalse(self.decide(self.gate(), datasets=("fixture.restricted",), feature="count_tokens").allow)

    def test_unknown_facts_block(self):
        self.assertFalse(self.decide(self.gate(None)).allow)
        self.assertIn("owner_data_policy:absent", self.decide(self.gate(owner(data_policy=DELETE))).reasons)
        no_fact = owner(providers__anthropic__account={"zdr_enabled": False})
        self.assertIn("account_fact:anthropic:training_permission_granted:unknown", self.decide(self.gate(no_fact)).reasons)
        self.assertFalse(self.decide(self.gate(), model="claude-unclassified-9").allow)
        blank = copy.deepcopy(load_provider_policies())
        blank["providers"]["anthropic"] = {}
        self.assertFalse(self.decide(self.gate(policies=blank)).allow)
        self.assertFalse(self.decide(self.gate(owner(providers__anthropic__enabled=False))).allow)

    def test_retention_ceiling(self):
        none_kept = owner(data_policy__default__max_provider_retention_days=0)
        self.assertTrue(self.decide(self.gate(none_kept)).allow)  # non-covered model: not retained by default
        covered = self.decide(self.gate(none_kept), model="claude-fable-5-1")
        self.assertFalse(covered.allow)
        self.assertTrue(any(r.startswith("retention:") for r in covered.reasons))
        self.assertFalse(self.decide(self.gate(none_kept), feature="batch").allow)
        unlimited = owner(data_policy__default__max_provider_retention_days="unlimited")
        self.assertTrue(self.decide(self.gate(unlimited), model="claude-fable-5-1").allow)

    def test_training_and_residency(self):
        trained = owner(providers__anthropic__account={"training_permission_granted": True, "zdr_enabled": False})
        self.assertIn("training:anthropic:off_unless_permission:not_satisfied", self.decide(self.gate(trained)).reasons)
        eu_only = owner(data_policy__default__allowed_processing_regions=["eu"])
        self.assertFalse(self.decide(self.gate(eu_only)).allow)
        india = owner(data_policy__default__allowed_processing_regions=["india"])
        self.assertFalse(self.decide(self.gate(india)).allow)
        us_only = owner(data_policy__default__allowed_processing_regions=["us"], data_policy__default__allowed_storage_regions=["us"],
                        providers__anthropic__region="us")
        self.assertTrue(self.decide(self.gate(us_only)).allow)
        self.assertFalse(self.decide(self.gate(owner(providers__anthropic__region="eu"))).allow)

    def test_other_providers(self):
        base = owner().providers["anthropic"]
        def with_provider(pid, region="global", **account):
            doc_owner = owner(**{f"providers__{pid}": {"enabled": True, "api_key_env": "X_KEY", "region": region,
                                                       "account": account}})
            return self.gate(doc_owner)
        g = with_provider("gemini", training_permission_granted=False)
        self.assertIn("paid_tier:gemini:not_recorded", self.decide(g, provider="gemini", model="gm").reasons)
        g = with_provider("gemini", paid_tier=True)
        d = self.decide(g, provider="gemini", model="gm")
        self.assertFalse(d.allow)  # Gemini abuse-log retention is not quantified: blocks a finite ceiling
        self.assertTrue(any("unknown_vs_ceiling" in r for r in d.reasons))
        zero = owner(data_policy__default__max_provider_retention_days=0,
                     providers__xai={"enabled": True, "api_key_env": "X_KEY", "region": "global",
                                     "account": {"training_permission_granted": False, "zdr_enabled": False}})
        self.assertFalse(self.decide(self.gate(zero), provider="xai", model="grok-4.7").allow)
        zdr = owner(data_policy__default__max_provider_retention_days=0,
                    providers__xai={"enabled": True, "api_key_env": "X_KEY", "region": "global",
                                    "account": {"training_permission_granted": False, "zdr_enabled": True}})
        self.assertTrue(self.decide(self.gate(zdr), provider="xai", model="grok-4.7").allow)
        us = owner(providers__xai={"enabled": True, "api_key_env": "X_KEY", "region": "us",
                                   "account": {"training_permission_granted": False, "zdr_enabled": False}})
        self.assertIn("region:xai:us:model_grok-9_not_served", self.decide(self.gate(us), provider="xai", model="grok-9").reasons)
        oa = owner(providers__openai={"enabled": True, "api_key_env": "X_KEY", "region": "global",
                                      "account": {"training_opt_in": False}})
        self.assertTrue(self.decide(self.gate(oa), provider="openai", model="gpt-6.1-sol").allow)
        oa_eu = owner(providers__openai={"enabled": True, "api_key_env": "X_KEY", "region": "eu",
                                         "account": {"training_opt_in": False}})
        self.assertIn("region:openai:eu:requires_zdr_enabled", self.decide(self.gate(oa_eu), provider="openai", model="m").reasons)
        self.assertIsNotNone(base)

    def test_stale_policy_blocks(self):
        self.clock.advance(40 * 24 * 3600 * SECOND)
        d = self.decide(self.gate())
        self.assertFalse(d.allow)
        self.assertTrue(any(r.startswith("provider_policy:stale") for r in d.reasons))

    def test_policy_version_changes_with_owner_policy(self):
        a = self.gate().policy_version
        b = self.gate(owner(data_policy__default__max_provider_retention_days=0)).policy_version
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
