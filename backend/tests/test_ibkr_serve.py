"""IBKR service wiring (ADR-0014): the market-data maintainer connects the read-only session, subscribes every
instrument with an ``ibkr`` provider mapping (by conId, in its trading currency) and runs maintenance; it never
reconnects an identity-unsafe session. The service copies mappings with currencies for it, off SQLite."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, FIXTURE_INSTRUMENT, Harness  # noqa: E402
from unknown.broker.ibkr.marketdata import SubscribeResult  # noqa: E402
from unknown.broker.ibkr.serve import MarketDataMaintainer  # noqa: E402
from unknown.service.core import CoreService  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402


class FakeSession:
    def __init__(self, verified_after_connect=True, reason=None):
        self.state = {"connected": False, "verified_paper": False, "reason": reason or "NOT_CONNECTED"}
        self.verified_after_connect = verified_after_connect
        self.connects, self.subscribed, self.maintained = 0, [], 0

    def connection_state(self):
        return dict(self.state)

    def connect(self):
        self.connects += 1
        if self.verified_after_connect:
            self.state = {"connected": True, "verified_paper": True, "reason": None}
        return self.state

    def subscribe(self, instrument_id, contract):
        self.subscribed.append((instrument_id, contract.con_id, contract.currency))
        return SubscribeResult(True, instrument_id, 1)

    def maintain(self):
        self.maintained += 1
        return {"cancelled": [], "resubscribed": []}


class FakeSvc:
    def __init__(self, details):
        self.instrument_details = details


class MaintainerTests(unittest.TestCase):
    def test_connects_then_subscribes_mapped_instruments_by_conid(self):
        session = FakeSession()
        out = MarketDataMaintainer(session, FakeSvc({"265598": ("INS-AAPL", "USD"), "SYMBOL-ONLY": ("INS-X", "USD")})).step()
        self.assertTrue(out["connected"])
        self.assertEqual(session.connects, 1)
        self.assertEqual(session.subscribed, [("INS-AAPL", 265598, "USD")])   # non-numeric ids are never guessed
        self.assertEqual(session.maintained, 1)

    def test_identity_unsafe_session_is_never_reconnected(self):
        session = FakeSession(reason="DISCONNECTED_UNSAFE: managed accounts differ")
        out = MarketDataMaintainer(session, FakeSvc({"1": ("INS-1", "USD")})).step()
        self.assertFalse(out["connected"])
        self.assertEqual((session.connects, session.subscribed), (0, []))

    def test_unverified_after_connect_subscribes_nothing(self):
        session = FakeSession(verified_after_connect=False)
        out = MarketDataMaintainer(session, FakeSvc({"1": ("INS-1", "USD")})).step()
        self.assertFalse(out["connected"])
        self.assertEqual(session.subscribed, [])


class InstrumentDetailsTests(unittest.TestCase):
    def test_service_copies_ibkr_mappings_with_currency(self):
        h = Harness()
        self.addCleanup(h.close)
        h.bootstrap()
        r = h.owner_cmd("register_instrument", instrument=FIXTURE_INSTRUMENT.to_json(),
                        mappings=[{"provider": "ibkr", "external_id": "265598"}])
        self.assertTrue(r.accepted, r.body)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = CoreService(paths=Paths(Path(tmp.name)), account_key=ACCOUNT, deployment_id="t", broker=h.broker)
        svc.desk = h.desk
        svc.refresh_instrument_map("ibkr")
        self.assertEqual(svc.instrument_map, {"265598": FIXTURE_INSTRUMENT.instrument_id})
        self.assertEqual(svc.instrument_details, {"265598": (FIXTURE_INSTRUMENT.instrument_id, FIXTURE_INSTRUMENT.currency)})
        self.assertEqual(r.body.get("mappings_added"), 1)

    def test_mapping_registration_is_idempotent_and_validated(self):
        h = Harness()
        self.addCleanup(h.close)
        h.bootstrap()
        add = dict(instrument=FIXTURE_INSTRUMENT.to_json(), mappings=[{"provider": "ibkr", "external_id": "265598"}])
        self.assertEqual(h.owner_cmd("register_instrument", **add).body.get("mappings_added"), 1)
        self.assertTrue(h.owner_cmd("register_instrument", **add).body.get("unchanged"))
        bad = h.owner_cmd("register_instrument", instrument=FIXTURE_INSTRUMENT.to_json(),
                          mappings=[{"provider": "ibkr", "external_id": None}])
        self.assertEqual(bad.rejected_code, "INVALID_FIELD")
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
