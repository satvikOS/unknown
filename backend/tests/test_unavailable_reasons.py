"""The one reason vocabulary for unavailable values (ADR-0020, docs/design/unavailable-values.md) and the real-data-only
boundary: product code imports no test double and offers no generated source."""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

from unknown.api.routes import meta
from unknown.domain import unavailable as U

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src"
DOC = ROOT / "docs" / "design" / "unavailable-values.md"


class VocabularyTests(unittest.TestCase):
    def test_the_owner_codes_are_in_the_vocabulary(self):
        for code in ("NOT_ENTITLED", "NO_SOURCE", "NOT_REPORTED", "NOT_APPLICABLE", "STALE", "ERROR"):
            self.assertIn(code, U.REASONS)

    def test_the_design_doc_lists_exactly_the_vocabulary(self):
        documented = re.findall(r"^\| `([A-Z_]+)` \|", DOC.read_text(encoding="utf-8"), flags=re.M)
        self.assertEqual(documented, list(U.REASONS))

    def test_detail_codes_classify_and_unknown_is_an_error(self):
        self.assertEqual(U.classify("NOT_YET_PUBLISHED: no bar yet"), "NOT_REPORTED")
        self.assertEqual(U.classify("NOT_SUBSCRIBED"), "NOT_ENTITLED")
        self.assertEqual(U.classify("STALE"), "STALE")
        self.assertEqual(U.classify("something new"), "ERROR")
        self.assertEqual(U.classify(None), "ERROR")
        with self.assertRaises(ValueError):
            U.unavailable("ZERO")
        self.assertEqual(U.unavailable("NO_SOURCE", "x"), {"value": None, "reason": "NO_SOURCE", "detail": "x"})

    def test_the_route_serves_the_vocabulary(self):
        out = meta.handle(None, ["meta", "unavailable-reasons"], {}, "SIM-1", None)
        self.assertEqual([r["code"] for r in out["reasons"]], list(U.REASONS))
        self.assertEqual(out["glyph"], "—")
        with self.assertRaises(LookupError):
            meta.handle(None, ["meta", "nope"], {}, "SIM-1", None)


class RealDataOnlyBoundaryTests(unittest.TestCase):
    def test_no_generated_data_module_remains_in_product_code(self):
        for gone in ("marketdata/synthetic.py", "marketdata/synthetic_factory.py", "service/fixture_market.py",
                     "research/fixtures.py", "research/study.py"):
            self.assertFalse((SRC / "unknown" / gone).exists(), gone)
        text = "\n".join(p.read_text(encoding="utf-8") for p in (SRC / "unknown").rglob("*.py"))
        for needle in ("SyntheticSource", "SyntheticBarsSource", "FixtureMarket", "include_fixture=", "--fixture-market",
                       "synthetic_bars(", "from support", "import support"):
            self.assertNotIn(needle, text, needle)

    def test_entry_points_cannot_import_a_test_double(self):
        code = ("import sys, unknown.api.server, unknown.service.core, unknown.cli, unknown.marketdata.bus, "
                "unknown.charts.service; print(sorted(m for m in sys.modules if m.split('.')[0] in "
                "('support', 'fake_marketdata', 'fake_market', 'research_fixtures', 'research_study')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": str(SRC)},
                             check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
