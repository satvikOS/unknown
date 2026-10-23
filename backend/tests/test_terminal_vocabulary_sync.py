"""The terminal mirrors some backend vocabularies as constants (terminal/src/workspaces/research/model.ts). A drift
(found 2026-10-22: the 'shadow' promotion status was missing, so shadow artifacts sorted first and showed the old
lifecycle) is caught here, from the backend side, without a JavaScript toolchain."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from unknown.research import promotion, registry

MODEL_TS = Path(__file__).resolve().parents[2] / "terminal" / "src" / "workspaces" / "research" / "model.ts"


def ts_tuple(name: str) -> tuple[str, ...]:
    m = re.search(rf"export const {name} = \[(.*?)\] as const;", MODEL_TS.read_text(), re.S)
    if m is None:
        raise AssertionError(f"{name} not found in {MODEL_TS}")
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


class TerminalVocabularySyncTests(unittest.TestCase):
    def test_artifact_statuses_match_research_promotion(self):
        self.assertEqual(ts_tuple("ARTIFACT_STATUSES"), tuple(promotion.STATUSES))

    def test_segments_match_research_registry(self):
        self.assertEqual(ts_tuple("SEGMENTS"), tuple(registry.SEGMENTS))


if __name__ == "__main__":
    unittest.main()
