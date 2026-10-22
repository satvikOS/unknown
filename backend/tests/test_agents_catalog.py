"""Runtime model catalog: diff, unreviewed ids, approvals, disappearance (CAP-MODEL-01, CAP-MODEL-11)."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import SECOND, TempDirCase, write_private  # noqa: E402
from unknown.agents.catalog import (Approval, Capability, Catalog, CatalogError, CatalogSnapshot, ModelRow,  # noqa: E402
                                    diff, load_approvals)


def snap(clock, *rows, provider="anthropic"):
    return CatalogSnapshot.success(provider, clock.now_ns(), "/v1/models", json.dumps([r.model_id for r in rows]).encode(), rows)


def row(mid, **kw):
    base = dict(lifecycle="active", max_input_tokens=1_000_000,
                capabilities={"structured_outputs": Capability(True, "api", "2026-10-22")})
    base.update(kw)
    return ModelRow("anthropic", mid, **base)


def approval(mid, roles=("research",), provider="anthropic", until=None):
    return Approval(provider, mid, frozenset(roles), "owner", "2026-10-22", "v1", until)


class CatalogTest(TempDirCase, unittest.TestCase):
    def catalog(self, *approvals):
        return Catalog(tuple(approvals), self.clock.now_ns, 24 * 3600 * SECOND, store_dir=self.tmp / "catalog")

    def test_shipped_approvals_file_is_empty(self):
        self.assertEqual(load_approvals(), ())

    def test_new_ids_are_unreviewed_until_approved(self):
        cat = self.catalog(approval("model-a"))
        d = cat.ingest(snap(self.clock, row("model-a"), row("model-b")))
        self.assertEqual(d.added, ("model-a", "model-b"))
        self.assertEqual(cat.status("anthropic", "model-b"), "unreviewed")
        self.assertEqual(cat.routable("anthropic", "model-b", "research"), (False, "unreviewed"))
        self.assertEqual(cat.routable("anthropic", "model-a", "research"), (True, "ok"))
        self.assertEqual(cat.routable("anthropic", "model-a", "critic"), (False, "not_approved_for_role"))
        self.assertTrue(list((self.tmp / "catalog" / "anthropic").glob("*.json")))

    def test_diff_reports_changes(self):
        cat = self.catalog()
        cat.ingest(snap(self.clock, row("m1"), row("m2")))
        self.clock.advance(SECOND)
        d = cat.ingest(snap(self.clock, row("m1", max_input_tokens=200_000), row("m3")))
        self.assertEqual(d.added, ("m3",))
        self.assertEqual(d.removed, ("m2",))
        self.assertEqual(d.changed["m1"]["max_input_tokens"], (1_000_000, 200_000))
        self.assertEqual(diff(None, snap(self.clock, row("x"))).added, ("x",))

    def test_approved_id_missing_becomes_non_routable_with_alert(self):
        cat = self.catalog(approval("m1"))
        cat.ingest(snap(self.clock, row("m1")))
        self.assertTrue(cat.routable("anthropic", "m1", "research")[0])
        cat.ingest(snap(self.clock, row("m2")))
        self.assertEqual(cat.routable("anthropic", "m1", "research"), (False, "missing_from_snapshot"))
        self.assertEqual(cat.status("anthropic", "m1"), "missing")
        self.assertTrue(any(a["kind"] == "approved_model_missing" for a in cat.alerts))

    def test_failed_refresh_keeps_last_good_and_auth_failure_blocks(self):
        cat = self.catalog(approval("m1"))
        cat.ingest(snap(self.clock, row("m1")))
        self.assertIsNone(cat.ingest(CatalogSnapshot.failure("anthropic", self.clock.now_ns(), "/v1/models", 503, "overloaded")))
        self.assertTrue(cat.routable("anthropic", "m1", "research")[0])
        cat.ingest(CatalogSnapshot.failure("anthropic", self.clock.now_ns(), "/v1/models", 401, "bad key"))
        self.assertEqual(cat.routable("anthropic", "m1", "research"), (False, "credentials_invalid"))
        cat.ingest(snap(self.clock, row("m1")))
        self.assertTrue(cat.routable("anthropic", "m1", "research")[0])

    def test_staleness_lifecycle_retirement_and_capabilities(self):
        cat = self.catalog(approval("m1"), approval("dep"), approval("ret"), approval("old"),
                           approval("unk"), approval("unk2", until=date(2026, 12, 31)))
        cat.ingest(snap(self.clock, row("m1"), row("dep", lifecycle="deprecated"), row("ret", retires_at="2026-10-01T00:00:00Z"),
                        row("old", lifecycle="retired"), row("unk", lifecycle="unknown"), row("unk2", lifecycle="unknown")))
        self.assertEqual(cat.routable("anthropic", "dep", "research"), (False, "lifecycle_deprecated"))
        self.assertEqual(cat.routable("anthropic", "old", "research"), (False, "lifecycle_retired"))
        self.assertEqual(cat.routable("anthropic", "ret", "research"), (False, "past_retirement_date"))
        self.assertEqual(cat.routable("anthropic", "unk", "research"), (False, "lifecycle_unknown_without_owner_review"))
        self.assertTrue(cat.routable("anthropic", "unk2", "research")[0])
        self.assertEqual(cat.routable("anthropic", "m1", "research", required_caps=("batch",)), (False, "capability_unverified:batch"))
        self.clock.advance(25 * 3600 * SECOND)
        self.assertEqual(cat.routable("anthropic", "m1", "research"), (False, "catalog_stale"))

    def test_aliases_cannot_be_pinned(self):
        cat = self.catalog(approval("grok-x"))
        cat.ingest(snap(self.clock, row("grok-x-2026"), row("grok-x"), ModelRow("anthropic", "grok-x-2026b", aliases=("grok-x",),
                                                                                  lifecycle="active")))
        self.assertEqual(cat.routable("anthropic", "grok-x", "research"), (False, "alias_not_pinnable"))
        p = write_private(self.tmp / "approved.json", json.dumps({"schema_version": 1, "approvals": [
            {"provider": "xai", "model_id": "grok-x-latest", "roles": ["research"], "approved_by": "owner",
             "approved_at": "2026-10-22", "policy_version": "v1"}]}))
        with self.assertRaises(CatalogError):
            load_approvals(p)

    def test_fingerprint_change_alerts(self):
        cat = self.catalog()
        cat.ingest(snap(self.clock, row("m", fingerprint="fp1")))
        cat.ingest(snap(self.clock, row("m", fingerprint="fp2")))
        self.assertTrue(any(a["kind"] == "fingerprint_changed" for a in cat.alerts))


if __name__ == "__main__":
    unittest.main()
