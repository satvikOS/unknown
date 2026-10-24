"""launchd rendering for the Mac deployment (scripts/deploy/macos/render_launchd.py, docs/deploy/macos.md): the core,
API, backup, news poller and both AI workers; the API under the IBKR venv when the IBKR market-data source is
configured; the AI workers under the agents venv, skipped with the reason when it does not exist. No IB Gateway job
(the owner logs in; nothing automates a broker login)."""
from __future__ import annotations

import importlib.util
import io
import json
import plistlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "macos" / "render_launchd.py"
_spec = importlib.util.spec_from_file_location("render_launchd", SCRIPT)
RL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RL)


class RenderLaunchdTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.out = self.root / "agents"

    def venv(self, name: str) -> Path:
        p = self.home / "venvs" / name / "bin" / "python"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        return p

    def ibkr_source(self, enabled=True):
        cfg = self.home / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "marketdata.json").write_text(json.dumps(
            {"sources": {"IBKR": {"enabled": enabled, "client_id": 19, "lines": 40}},
             "routes": [{"match": "*", "sources": ["IBKR"]}]}))

    def render(self, *extra, broker="simulated", python="/usr/bin/python3"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = RL.main(["--account", "SIM-1", "--deployment", "mac-dev", "--broker", broker, "--python", python,
                            "--home", str(self.home), "--out", str(self.out), *extra])
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        return code

    def plist(self, label: str) -> dict:
        with open(self.out / f"{label}.plist", "rb") as f:
            return plistlib.load(f)

    def test_all_processes_with_their_interpreters(self):
        agents_py = self.venv("agents")
        self.assertEqual(self.render(), 0)
        labels = sorted(p.stem for p in self.out.glob("*.plist"))
        self.assertEqual(labels, sorted(RL.CORE_LABELS + RL.AI_LABELS))
        self.assertEqual(self.plist("com.unknown.news")["ProgramArguments"][1:4], ["-m", "unknown.news.poller", "run"])
        self.assertEqual(self.plist("com.unknown.analyst")["ProgramArguments"][:3],
                         [str(agents_py), "-m", "unknown.agents.analyst"])
        self.assertEqual(self.plist("com.unknown.ai")["ProgramArguments"][:3],
                         [str(agents_py), "-m", "unknown.agents.worker"])
        self.assertEqual(self.plist("com.unknown.api")["ProgramArguments"][0], "/usr/bin/python3")
        for label in labels:
            body = self.plist(label)
            self.assertEqual(body["EnvironmentVariables"]["UNKNOWN_HOME"], str(self.home))
            if label != "com.unknown.backup":
                self.assertEqual(body["KeepAlive"], {"SuccessfulExit": False})
        self.assertNotIn("gateway", " ".join(labels).lower())
        self.assertIn("log in yourself", self.stdout)

    def test_ai_workers_are_skipped_with_the_reason_without_the_agents_venv(self):
        self.assertEqual(self.render(), 0)
        self.assertFalse((self.out / "com.unknown.ai.plist").exists())
        self.assertIn("skipped com.unknown.analyst, com.unknown.ai", self.stdout)

    def test_api_runs_under_the_ibkr_venv_when_the_ibkr_source_is_configured(self):
        self.ibkr_source()
        self.assertEqual(self.render(), 2)                      # no IBKR venv yet: refused, nothing written
        self.assertIn("ibkr_venv.sh", self.stderr)
        self.assertFalse(self.out.exists())
        ibkr_py = self.venv("ibkr")
        self.assertEqual(self.render(), 0)
        self.assertEqual(self.plist("com.unknown.api")["ProgramArguments"][0], str(ibkr_py))
        self.assertEqual(self.plist("com.unknown.core")["ProgramArguments"][0], "/usr/bin/python3")

    def test_disabled_ibkr_source_keeps_the_api_on_the_given_interpreter(self):
        self.ibkr_source(enabled=False)
        self.assertEqual(self.render(), 0)
        self.assertEqual(self.plist("com.unknown.api")["ProgramArguments"][0], "/usr/bin/python3")

    def test_ibkr_core_keeps_its_interpreter_for_the_api(self):
        ibkr_py = self.venv("ibkr")
        self.assertEqual(self.render(broker="ibkr", python=str(ibkr_py)), 0)
        core = self.plist("com.unknown.core")["ProgramArguments"]
        self.assertEqual(core[:3], [str(ibkr_py), "-m", "unknown.broker.ibkr.serve"])
        self.assertEqual(self.plist("com.unknown.api")["ProgramArguments"][0], str(ibkr_py))

    def test_relative_interpreters_are_refused(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.render("--agents-python", "venv/bin/python")


if __name__ == "__main__":
    unittest.main()
