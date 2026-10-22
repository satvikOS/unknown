"""SQLite store: durability settings, migrations, transactions and the writer
lease with fencing epoch (ADR-0003, ADR-0011)."""
from __future__ import annotations

import socket
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..domain.clock import Clock, NS_PER_S
from ..domain.errors import LeaseLost
from .schema import MIGRATIONS


class LeaseHeld(RuntimeError):
    """Another live writer holds the lease; refuse to start (ADR-0011)."""


class Tx:
    """Thin wrapper over a connection inside an open transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def run(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    target = str(path)
    if readonly:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, isolation_level=None, timeout=5)
    else:
        conn = sqlite3.connect(target, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if not readonly and target != ":memory:":
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError(f"could not enable WAL (got {mode})")
    conn.execute("PRAGMA synchronous=FULL")
    if sys.platform == "darwin":
        # macOS fsync does not flush the drive cache; F_FULLFSYNC does.
        conn.execute("PRAGMA fullfsync=ON")
        conn.execute("PRAGMA checkpoint_fullfsync=ON")
    return conn


def migrate(conn: sqlite3.Connection, now_ns: int) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                 "applied_at_ns INTEGER NOT NULL, description TEXT NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, description, ddl in MIGRATIONS:
        if version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _statements(ddl):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (version, now_ns, description))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return max([v for v, _, _ in MIGRATIONS], default=0)


def _statements(ddl: str) -> list[str]:
    out, buf = [], []
    for line in ddl.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


class Store:
    """Owns the single read-write connection and the writer lease."""

    LEASE_STALE_AFTER_NS = 30 * NS_PER_S

    def __init__(self, path: str | Path, clock: Clock, *, holder: str, takeover: bool = False):
        self.path = str(path)
        self.clock = clock
        self.holder = holder
        self.host = socket.gethostname()
        self.conn = connect(self.path)
        migrate(self.conn, clock.now_ns())
        self.epoch = self._acquire_lease(takeover)

    # ------------------------------------------------------------------ lease
    def _acquire_lease(self, takeover: bool) -> int:
        now = self.clock.now_ns()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT holder, host, epoch, heartbeat_at_ns FROM writer_lease WHERE id=1").fetchone()
            if row is None:
                epoch = 1
                self.conn.execute("INSERT INTO writer_lease VALUES (1,?,?,?,?,?)", (self.holder, self.host, epoch, now, now))
            else:
                alive = now - row["heartbeat_at_ns"] < self.LEASE_STALE_AFTER_NS
                if row["holder"] != self.holder and alive and not takeover:
                    raise LeaseHeld(f"writer lease held by {row['holder']}@{row['host']} (epoch {row['epoch']})")
                epoch = row["epoch"] + 1
                self.conn.execute("UPDATE writer_lease SET holder=?, host=?, epoch=?, acquired_at_ns=?, heartbeat_at_ns=? WHERE id=1",
                                  (self.holder, self.host, epoch, now, now))
            self.conn.execute("COMMIT")
            return epoch
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def release_lease(self) -> None:
        """Clean shutdown: mark the lease stale so the next start need not wait or take over."""
        with self.write() as tx:
            tx.run("UPDATE writer_lease SET heartbeat_at_ns=0 WHERE id=1 AND holder=? AND epoch=?", (self.holder, self.epoch))

    def heartbeat(self) -> None:
        with self.write() as tx:
            tx.run("UPDATE writer_lease SET heartbeat_at_ns=? WHERE id=1", (self.clock.now_ns(),))

    def _check_lease(self) -> None:
        row = self.conn.execute("SELECT epoch, holder FROM writer_lease WHERE id=1").fetchone()
        if row is None or row["epoch"] != self.epoch or row["holder"] != self.holder:
            raise LeaseLost(f"writer lease epoch {self.epoch} is no longer current")

    # ----------------------------------------------------------- transactions
    @contextmanager
    def write(self) -> Iterator[Tx]:
        """BEGIN IMMEDIATE + fencing check; commit on success, roll back on any error."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._check_lease()
            yield Tx(self.conn)
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    @contextmanager
    def read(self) -> Iterator[Tx]:
        """Consistent snapshot read on the writer connection."""
        self.conn.execute("BEGIN")
        try:
            yield Tx(self.conn)
        finally:
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def backup_to(self, target: str | Path) -> None:
        """Database-consistent online backup (ADR-0003 #6). The copy is marked as a backup inside the
        database itself, so opening it anywhere is detected as a restore (stop latched, orders looked up)."""
        _backup(self.conn, target, self.epoch, self.clock.now_ns())

    def backup_origin(self) -> dict | None:
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_origin'").fetchone() is None:
            return None
        r = self.conn.execute("SELECT source_epoch, source_max_seq, taken_at_ns FROM backup_origin ORDER BY rowid DESC LIMIT 1").fetchone()
        return dict(r) if r is not None else None

    def clear_backup_origin(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS backup_origin")

    def event_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def close(self) -> None:
        self.conn.close()


def _backup(source: sqlite3.Connection, target: str | Path, epoch: int, now_ns: int) -> dict:
    """Copy ``source`` to ``target`` with SQLite's online backup and mark the copy as a backup inside itself."""
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
        # Read the marker facts from the copy itself, so they describe exactly the snapshot it holds.
        seq = dest.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        row = dest.execute("SELECT epoch FROM writer_lease WHERE id=1").fetchone()
        epoch = row[0] if row else epoch
        dest.execute("CREATE TABLE IF NOT EXISTS backup_origin (source_epoch INTEGER NOT NULL, "
                     "source_max_seq INTEGER NOT NULL, taken_at_ns INTEGER NOT NULL)")
        dest.execute("INSERT INTO backup_origin VALUES (?,?,?)", (epoch, seq, now_ns))
        dest.commit()
        ok = dest.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dest.close()
    return {"source_epoch": epoch, "source_max_seq": seq, "taken_at_ns": now_ns, "integrity_check": ok}


def backup_database(source_path: str | Path, target: str | Path, now_ns: int) -> dict:
    """Online backup taken by another process (WAL allows it while the core writes). Never takes the lease."""
    src = connect(source_path, readonly=True)
    try:
        return _backup(src, target, 0, now_ns)
    finally:
        src.close()


def lease_alive(path: str | Path, now_ns: int) -> bool:
    """Whether a core process currently holds the writer lease of ``path`` (heartbeat newer than the stale bound)."""
    conn = connect(path, readonly=True)
    try:
        row = conn.execute("SELECT heartbeat_at_ns FROM writer_lease WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None and now_ns - row["heartbeat_at_ns"] < Store.LEASE_STALE_AFTER_NS
