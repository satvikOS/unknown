"""Owner broker-administration actions (ADR-0012): clearing a latched adapter entry block and attesting an
execution-history gap are recorded by the core with fresh owner authentication, then applied to the adapter
by the service only after the record commits and never on a duplicate."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, Harness  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402
from unknown.service.core import CoreService  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402

S = 1_000_000_000


class FakeAdapter:
    """The adapter-side surface the service calls (the IBKR adapter's clear_entry_block / acknowledge_history_gap)."""

    def __init__(self):
        self.blocks = {"SHORT_SALE_SIGNAL": "IBKR reported a short-sale flag"}
        self.acks: list[tuple] = []

    def clear_entry_block(self, name, *, by, note):
        return self.blocks.pop(name, None) is not None

    def acknowledge_history_gap(self, through_day, *, by, note):
        self.acks.append((through_day, by))

    def anomalies(self):
        return [{"kind": "ENTRY_BLOCK", "detail": "fixture"}]

    def entry_blocks(self):
        return dict(self.blocks)


class CoreRecordTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.bootstrap()

    def test_records_with_fresh_authentication_and_a_note(self):
        h = self.h
        r = h.owner_cmd("broker_admin_action", account_key=ACCOUNT, action="clear_entry_block", name="SHORT_SALE_SIGNAL",
                        note="flag raised by a manual test order in TWS")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(r.body, {"action": "clear_entry_block", "args": {"name": "SHORT_SALE_SIGNAL"}})
        self.assertEqual(h.owner_cmd("broker_admin_action", account_key=ACCOUNT, action="clear_entry_block",
                                     name="SHORT_SALE_SIGNAL", note="").rejected_code, "NOTE_REQUIRED")
        stale = Principal("owner", Role.OWNER, authenticated_at_ns=h.clock.now_ns() - 3600 * S)
        self.assertEqual(h.cmd("broker_admin_action", stale, account_key=ACCOUNT, action="clear_entry_block",
                               name="X", note="n").rejected_code, "OWNER_REAUTHENTICATION_REQUIRED")
        self.assertEqual(h.owner_cmd("broker_admin_action", account_key=ACCOUNT, action="drop_table",
                                     note="n").rejected_code, "INVALID_FIELD")
        self.assertEqual(h.owner_cmd("broker_admin_action", account_key=ACCOUNT, action="clear_entry_block",
                                     name="lower case", note="n").rejected_code, "INVALID_FIELD")

    def test_history_gap_attestation_requires_a_fresh_reconciliation(self):
        h = self.h
        self.assertFalse(h.control()["needs_reconciliation"])
        r = h.owner_cmd("broker_admin_action", account_key=ACCOUNT, action="acknowledge_history_gap",
                        through_day="2026-10-19", note="reconciled from the 2026-10-19 activity statement")
        self.assertTrue(r.accepted, r.body)
        self.assertTrue(h.control()["needs_reconciliation"])
        self.assertEqual(h.owner_cmd("broker_admin_action", account_key=ACCOUNT, action="acknowledge_history_gap",
                                     through_day="2099-01-01", note="n").rejected_code, "INVALID_FIELD")
        # today (2026-10-20 in the risk-day zone) is still trading: attesting it would hide later gaps
        self.assertEqual(h.owner_cmd("broker_admin_action", account_key=ACCOUNT, action="acknowledge_history_gap",
                                     through_day="2026-10-20", note="n").rejected_code, "INVALID_FIELD")
        self.assertEqual(h.verify(), [])

    def test_only_the_owner_may_issue_it(self):
        h = self.h
        for principal in (h.desk.system(), h.strategy, h.agent):
            r = h.cmd("broker_admin_action", principal, account_key=ACCOUNT, action="clear_entry_block", name="X", note="n")
            self.assertEqual(r.rejected_code, "UNAUTHORIZED")


class ServiceApplyTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.bootstrap()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.adapter = FakeAdapter()
        self.svc = CoreService(paths=Paths(Path(tmp.name)), account_key=ACCOUNT, deployment_id="test", broker=self.adapter)
        self.svc.desk = self.h.desk

    def send(self, command_id, **payload):
        req = {"op": "command", "command_id": command_id, "kind": "broker_admin_action",
               "principal": {"id": "owner", "role": "OWNER", "authenticated_at_ns": self.h.clock.now_ns()},
               "payload": {"account_key": ACCOUNT, **payload} if "account_key" not in payload else payload}
        return self.svc._handle(req, frozenset({"OWNER"}))

    def test_applies_after_commit_and_not_on_duplicate(self):
        r = self.send("cli.admin.1", action="clear_entry_block", name="SHORT_SALE_SIGNAL", note="reviewed")
        self.assertTrue(r["accepted"])
        self.assertEqual(r["body"]["adapter"], {"applied": True})
        self.assertEqual(self.adapter.blocks, {})
        self.adapter.blocks["SHORT_SALE_SIGNAL"] = "raised again"
        again = self.send("cli.admin.1", action="clear_entry_block", name="SHORT_SALE_SIGNAL", note="reviewed")
        self.assertTrue(again["duplicate"])
        self.assertNotIn("adapter", again["body"])
        self.assertIn("SHORT_SALE_SIGNAL", self.adapter.blocks, "a duplicate never re-applies")

    def test_rejected_record_is_never_applied(self):
        r = self.send("cli.admin.2", action="clear_entry_block", name="SHORT_SALE_SIGNAL", note="")
        self.assertFalse(r["accepted"])
        self.assertIn("SHORT_SALE_SIGNAL", self.adapter.blocks)

    def test_history_gap_reaches_the_adapter_as_a_date(self):
        r = self.send("cli.admin.3", action="acknowledge_history_gap", through_day="2026-10-19", note="statement")
        self.assertEqual(r["body"]["adapter"], {"applied": True})
        self.assertEqual(self.adapter.acks, [(date(2026, 10, 19), "owner")])

    def test_action_for_another_account_is_refused_before_recording(self):
        with self.assertRaises(ValueError):
            self.send("cli.admin.5", action="clear_entry_block", name="SHORT_SALE_SIGNAL", note="n",
                      account_key="OTHER-ACCOUNT")
        self.assertIn("SHORT_SALE_SIGNAL", self.adapter.blocks)

    def test_alerts_require_the_owner_token(self):
        with self.assertRaises(PermissionError):
            self.svc._handle({"op": "query", "name": "broker_alerts"}, frozenset({"AGENT"}))

    def test_simulator_without_the_method_reports_not_applied(self):
        self.svc.broker = self.h.broker
        r = self.send("cli.admin.4", action="clear_entry_block", name="X", note="n")
        self.assertTrue(r["accepted"])
        self.assertFalse(r["body"]["adapter"]["applied"])

    def test_alerts_query_exposes_adapter_anomalies_and_blocks(self):
        out = self.svc._handle({"op": "query", "name": "broker_alerts"}, frozenset({"OWNER"}))
        self.assertEqual(out["entry_blocks"], {"SHORT_SALE_SIGNAL": "IBKR reported a short-sale flag"})
        self.assertEqual(out["anomalies"][0]["kind"], "ENTRY_BLOCK")


if __name__ == "__main__":
    unittest.main()
