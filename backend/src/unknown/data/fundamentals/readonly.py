"""Read-only access to the research store for API routes (ADR-0013: read paths never write).

``ReadOnlyResearchStore`` opens the SQLite file with ``mode=ro`` and skips schema creation, so the
point-in-time read methods of ``ResearchStore`` (``as_of``, ``latest_as_of``, ``history``,
``resolve``, ``conflicts``) work while every write raises ``sqlite3.OperationalError``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..pit import ResearchStore


class ReadOnlyResearchStore(ResearchStore):
    def __init__(self, path: str | Path, raw_dir: str | Path | None = None):  # noqa: D401 - no super().__init__ (it writes)
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"research store {p} does not exist")
        self.path = str(p)
        self.conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.raw_dir = Path(raw_dir) if raw_dir else p.parent / "raw"


def research_paths(root: Path) -> tuple[Path, Path]:
    research = Path(root) / "research"
    return research / "research.sqlite", research / "raw"
