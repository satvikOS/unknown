"""Saved screens and screen runs (CAP-MKT-10): ``<UNKNOWN_HOME>/screener/screener.sqlite``.

Append-only: a save adds a version of a screen; a run is stored once with its manifest and result
hash, so a run can be reopened and re-verified later. This store is research-side only; the money
core never reads it.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

from ..domain.clock import ns_to_iso
from .engine import compile_definition, sha

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS screen (
  screen_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS screen_version (
  screen_id TEXT NOT NULL REFERENCES screen(screen_id), version INTEGER NOT NULL, name TEXT NOT NULL, definition TEXT NOT NULL,
  definition_hash TEXT NOT NULL, created_ns INTEGER NOT NULL, PRIMARY KEY (screen_id, version)
);
CREATE TABLE IF NOT EXISTS screen_run (
  run_id TEXT PRIMARY KEY, screen_id TEXT, version INTEGER, definition_hash TEXT NOT NULL, as_of_ns INTEGER NOT NULL,
  knowledge_cutoff_ns INTEGER NOT NULL, result TEXT NOT NULL, result_hash TEXT NOT NULL, created_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS screen_run_by_screen ON screen_run(screen_id, created_ns);
CREATE TRIGGER IF NOT EXISTS screen_version_no_update BEFORE UPDATE ON screen_version
  BEGIN SELECT RAISE(ABORT, 'screen_version is append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_run_no_update BEFORE UPDATE ON screen_run
  BEGIN SELECT RAISE(ABORT, 'screen_run is append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_run_no_delete BEFORE DELETE ON screen_run
  BEGIN SELECT RAISE(ABORT, 'screen_run is append-only'); END;
"""


def path_for(root: Path) -> Path:
    return Path(root) / "screener" / "screener.sqlite"


class ScreenStore:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(str(self.path))
            self.conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), isolation_level=None)
            self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def save(self, name: str, definition: dict, now_ns: int, screen_id: str | None = None) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 120:
            raise ValueError("a screen needs a name of 1-120 characters")
        spec = compile_definition(definition)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if screen_id:
                if self.conn.execute("SELECT 1 FROM screen WHERE screen_id=?", (screen_id,)).fetchone() is None:
                    raise LookupError(f"screen {screen_id} not found")
            else:
                screen_id = f"scr-{secrets.token_hex(6)}"
                self.conn.execute("INSERT INTO screen VALUES (?,?,?)", (screen_id, name, now_ns))
            version = self.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM screen_version WHERE screen_id=?", (screen_id,)).fetchone()[0]
            self.conn.execute("INSERT INTO screen_version VALUES (?,?,?,?,?,?)",
                              (screen_id, version, name, json.dumps(spec, sort_keys=True), sha(spec), now_ns))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(screen_id)

    def screens(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT v.*, (SELECT COUNT(*) FROM screen_run r WHERE r.screen_id=v.screen_id) AS runs FROM screen_version v "
            "WHERE v.version=(SELECT MAX(version) FROM screen_version w WHERE w.screen_id=v.screen_id) ORDER BY v.created_ns DESC").fetchall()
        return [self._version(r) | {"runs": r["runs"]} for r in rows]

    def _version(self, r) -> dict:
        return {"screen_id": r["screen_id"], "version": r["version"], "name": r["name"], "definition": json.loads(r["definition"]),
                "definition_hash": r["definition_hash"], "saved_at": ns_to_iso(r["created_ns"])}

    def get(self, screen_id: str, version: int | None = None) -> dict:
        rows = self.conn.execute("SELECT * FROM screen_version WHERE screen_id=? ORDER BY version", (screen_id,)).fetchall()
        if not rows:
            raise LookupError(f"screen {screen_id} not found")
        chosen = rows[-1] if version is None else next((r for r in rows if r["version"] == version), None)
        if chosen is None:
            raise LookupError(f"screen {screen_id} has no version {version}")
        runs = self.conn.execute("SELECT run_id, version, as_of_ns, knowledge_cutoff_ns, result_hash, created_ns FROM screen_run "
                                 "WHERE screen_id=? ORDER BY created_ns DESC LIMIT 50", (screen_id,)).fetchall()
        return {**self._version(chosen), "versions": [self._version(r) for r in rows],
                "runs": [{"run_id": r["run_id"], "version": r["version"], "as_of": ns_to_iso(r["as_of_ns"]),
                          "knowledge_cutoff": ns_to_iso(r["knowledge_cutoff_ns"]), "result_hash": r["result_hash"],
                          "run_at": ns_to_iso(r["created_ns"])} for r in runs]}

    def record_run(self, result: dict, now_ns: int, screen_id: str | None = None, version: int | None = None) -> str:
        run_id = f"run-{secrets.token_hex(8)}"
        m = result["manifest"]
        self.conn.execute("INSERT INTO screen_run VALUES (?,?,?,?,?,?,?,?,?)",
                          (run_id, screen_id, version, m["definition_hash"], int(m["as_of_ns"]), int(m["knowledge_cutoff_ns"]),
                           json.dumps(result, sort_keys=True), result["result_hash"], now_ns))
        return run_id

    def run(self, run_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
        if r is None:
            raise LookupError(f"run {run_id} not found")
        return {"run_id": run_id, "screen_id": r["screen_id"], "version": r["version"], "run_at": ns_to_iso(r["created_ns"]),
                **json.loads(r["result"])}
