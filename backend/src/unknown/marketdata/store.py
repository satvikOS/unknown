"""Tick store: time & sales history outside the core database, with bounded retention (CAP-MKT-03).

One SQLite file at ``<UNKNOWN_HOME>/marketdata/ticks.sqlite`` (never the core's event-sourced database). Trades are
appended by a writer thread in batches; retention is bounded by age (``retention_s``) and by rows per key
(``max_rows_per_key``), pruned every ``prune_every_s``. Every row keeps its source and data type. Rows are display data for the owner; the provider's storage terms apply (see
config/providers).
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  key TEXT NOT NULL, seq INTEGER NOT NULL, price TEXT NOT NULL, size TEXT, exchange TEXT, conditions TEXT NOT NULL,
  ts_source_ns INTEGER, ts_recv_ns INTEGER NOT NULL, source TEXT NOT NULL, data_type TEXT NOT NULL, feed TEXT);
CREATE INDEX IF NOT EXISTS trades_key_time ON trades (key, ts_recv_ns);
"""
MAX_QUERY = 5000


class TickStore:
    def __init__(self, path: Path, *, retention_s: int = 3 * 86_400, max_rows_per_key: int = 100_000,
                 prune_every_s: float = 60.0, time_ns=time.time_ns):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.retention_s = retention_s
        self.max_rows_per_key = max_rows_per_key
        self.prune_every_s = prune_every_s
        self._time_ns = time_ns
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = threading.Event()
        self.written = 0
        self.pruned = 0
        self.write_errors = 0
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        self._thread = threading.Thread(target=self._run, name="md-tickstore", daemon=True)
        self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def append(self, trade: dict) -> None:
        self._q.put(trade)

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._q.put(None)
        self._thread.join(timeout)

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every trade appended before this call is written (tests and shutdown)."""
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout)

    def _run(self) -> None:
        conn = self._connect()
        last_prune = time.monotonic()
        try:
            while True:
                item = self._q.get()
                batch, events, stop = [], [], False
                while True:
                    if item is None:
                        stop = True
                    elif isinstance(item, threading.Event):
                        events.append(item)
                    else:
                        batch.append(item)
                    if len(batch) >= 2000:
                        break
                    try:
                        item = self._q.get_nowait()
                    except queue.Empty:
                        break
                if batch:
                    self._write(conn, batch)
                if time.monotonic() - last_prune >= self.prune_every_s:
                    self.prune(conn)
                    last_prune = time.monotonic()
                for e in events:
                    e.set()
                if stop:
                    return
        finally:
            conn.close()

    def _write(self, conn: sqlite3.Connection, batch: list[dict]) -> None:
        rows = [(t["key"], t["seq"], t["price"], t["size"], t.get("exchange"), json.dumps(t.get("conditions") or []),
                 _int(t.get("ts_source_ns")), _int(t["ts_recv_ns"]), t["source"], t["data_type"], t.get("feed"))
                for t in batch]
        try:
            with conn:
                conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.written += len(rows)
        except sqlite3.Error:
            self.write_errors += len(rows)

    def prune(self, conn: sqlite3.Connection | None = None) -> int:
        own = conn is None
        conn = conn or self._connect()
        try:
            cutoff = self._time_ns() - self.retention_s * 1_000_000_000
            with conn:
                n = conn.execute("DELETE FROM trades WHERE ts_recv_ns < ?", (cutoff,)).rowcount
                for (key,) in conn.execute("SELECT key FROM trades GROUP BY key HAVING COUNT(*) > ?",
                                           (self.max_rows_per_key,)).fetchall():
                    n += conn.execute(
                        "DELETE FROM trades WHERE key = ? AND rowid NOT IN (SELECT rowid FROM trades WHERE key = ? "
                        "ORDER BY ts_recv_ns DESC, seq DESC LIMIT ?)", (key, key, self.max_rows_per_key)).rowcount
            self.pruned += n
            return n
        finally:
            if own:
                conn.close()

    def trades(self, key: str, *, before_ns: int | None = None, limit: int = 500) -> list[dict]:
        """Newest first; page back with ``before_ns`` = the oldest ts_recv_ns of the previous page."""
        limit = max(1, min(int(limit), MAX_QUERY))
        conn = self._connect()
        try:
            sql = "SELECT * FROM trades WHERE key = ?"
            args: list = [key]
            if before_ns is not None:
                sql += " AND ts_recv_ns < ?"
                args.append(int(before_ns))
            sql += " ORDER BY ts_recv_ns DESC, seq DESC LIMIT ?"
            args.append(limit)
            cur = conn.execute(sql, args)
            names = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                r = dict(zip(names, row))
                r["conditions"] = json.loads(r["conditions"])
                for k in ("ts_source_ns", "ts_recv_ns"):
                    r[k] = None if r[k] is None else str(r[k])
                out.append(r)
            return out
        finally:
            conn.close()

    def stats(self) -> dict:
        return {"path_name": self.path.name, "retention_s": self.retention_s, "max_rows_per_key": self.max_rows_per_key,
                "written": self.written, "pruned": self.pruned, "write_errors": self.write_errors}


def _int(v):
    return None if v is None else int(v)
