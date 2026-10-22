"""Static checks on the agent layer source (CAP-MODEL-01, CAP-MODEL-05, CAP-AGT-03, ADR-0016 section 1)."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[1] / "src" / "unknown" / "agents"
MODEL_ID = re.compile(r"(gpt-|\bo[0-9]-|claude-|grok-|gemini-|mistral-|ministral-)", re.IGNORECASE)
PRICE = re.compile(r"\$\s?\d")
ALLOWED_INTERNAL = ("unknown.domain", "unknown.data.registry", "unknown.service.paths", "unknown.agents")
# Narrow per-file additions: the worker talks to the core only through the socket client (with the AGENT
# token), and the evidence builders read the point-in-time research store (read-only, research side).
ALLOWED_PER_FILE = {
    "worker.py": ("unknown.service.ipc", "unknown.data.pit"),
    "pit_evidence.py": ("unknown.data.pit",),
    "submission.py": ("unknown.data.pit",),
}
FORBIDDEN_INTERNAL = ("unknown.broker", "unknown.core", "unknown.gateway", "unknown.risk", "unknown.reconcile",
                      "unknown.policy", "unknown.store", "unknown.api", "unknown.market")


def sources():
    return sorted(AGENTS.rglob("*.py"))


def absolute(module: str | None, level: int, path: Path) -> str:
    if level == 0:
        return module or ""
    pkg = path.relative_to(AGENTS.parents[1]).with_suffix("").parts[:-1]
    base = list(pkg[: len(pkg) - (level - 1)])
    return ".".join(base + ([module] if module else []))


class StaticTest(unittest.TestCase):
    def test_no_model_id_literals_in_application_source(self):
        hits = []
        for p in sources():
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if MODEL_ID.search(line):
                    hits.append(f"{p.name}:{i}: {line.strip()}")
        self.assertEqual(hits, [], "model ids belong in owner-approved configuration, not code")

    def test_no_price_literals_in_application_source(self):
        hits = [f"{p.name}:{i}" for p in sources() for i, line in enumerate(p.read_text().splitlines(), 1) if PRICE.search(line)]
        self.assertEqual(hits, [])

    def test_agents_never_import_execution_or_ledger_code(self):
        bad = []
        for p in sources():
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.ImportFrom):
                    names = [absolute(node.module, node.level, p)]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    continue
                allowed = ALLOWED_INTERNAL + ALLOWED_PER_FILE.get(p.name, ()) if p.parent == AGENTS else ALLOWED_INTERNAL
                for n in names:
                    if n.startswith(FORBIDDEN_INTERNAL) or (n.startswith("unknown") and not n.startswith(allowed)):
                        bad.append(f"{p.name}: {n}")
        self.assertEqual(bad, [])

    def test_sdk_import_is_lazy(self):
        for p in sources():
            tree = ast.parse(p.read_text())
            for node in tree.body:  # module level only
                if isinstance(node, ast.Import):
                    self.assertNotIn("anthropic", [a.name for a in node.names], p.name)
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertNotEqual(node.module, "anthropic", p.name)

    def test_no_float_literals_in_money_modules(self):
        for name in ("budget.py", "tariffs.py", "config.py"):
            tree = ast.parse((AGENTS / name).read_text())
            floats = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
            self.assertEqual(floats, [], name)


if __name__ == "__main__":
    unittest.main()
