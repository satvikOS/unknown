"""Shared constants and helpers for the agent layer."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
CONFIG_MODELS = REPO / "config" / "models"
CONTRACTS = REPO / "contracts"


def connect_durable(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """SQLite connection with the core store's durability settings (``store/db.py``): WAL, ``synchronous=FULL``
    and, on macOS, ``fullfsync`` (plain fsync there does not flush the drive cache). A writable database is
    created with mode 0600 in a 0700 directory. ``readonly`` never creates the file.

    Used by the agent job store and audit trail; the agents package does not import ``unknown.store``."""
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=10,
                               check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except PermissionError:  # pragma: no cover - directory owned elsewhere
            pass
        if not path.exists():
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            conn.close()
            raise RuntimeError(f"could not enable WAL on {path} (got {mode})")
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=FULL")
    if sys.platform == "darwin":
        conn.execute("PRAGMA fullfsync=ON")
        conn.execute("PRAGMA checkpoint_fullfsync=ON")
    return conn
