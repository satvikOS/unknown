"""GET /api/export/rights (CAP-UX-07): the rights records the terminal's export primitive gates columns on."""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from unknown.api.routes import export_rights
from unknown.data.registry import RegistryError


class ExportRightsTest(unittest.TestCase):
    def call(self):
        out = export_rights.rights(types.SimpleNamespace(paths=None), ["export", "rights"], {}, "acct", None)
        json.dumps(out, allow_nan=False)
        return out

    def test_records_and_aliases_come_from_the_registry(self):
        out = self.call()
        self.assertEqual(out["registry"], "AVAILABLE")
        sec = out["records"]["sec-edgar"]["datasets"]["sec.submissions"]
        self.assertIn("storage", sec["allowed_uses"])
        self.assertTrue(sec["rights_source"])
        self.assertEqual(out["aliases"]["marketdata:ALPACA"], {"record": "alpaca", "dataset": "alpaca.stock_stream"})
        self.assertNotIn("marketdata:IBKR", out["aliases"])        # no rights record: the terminal excludes it
        self.assertTrue(any(k.startswith("news:") for k in out["aliases"]))
        self.assertEqual(out["records"]["nse-india"]["datasets"]["nse.website"]["allowed_uses"], [])

    def test_unreadable_registry_fails_closed(self):
        with mock.patch.object(export_rights, "load_registry", side_effect=RegistryError("bad record")):
            out = self.call()
        self.assertEqual((out["registry"], out["reason"], out["records"]), ("UNAVAILABLE", "ERROR", {}))

    def test_unknown_path(self):
        with self.assertRaises(LookupError):
            export_rights.rights(None, ["export", "rights", "x"], {}, "acct", None)


if __name__ == "__main__":
    unittest.main()
