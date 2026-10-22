"""Replay the event log into an empty database and compare projections (ADR-0005 #3).

Used by tests, by restore drills and by the operator's audit command: if a
replay of the stored events does not reproduce every projection exactly, the
projections cannot be trusted and the account must stay stopped."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..store.db import Tx, connect, migrate
from ..store.schema import PROJECTION_TABLES
from .projector import apply_changes
from ..domain.canonical import canonical_json, sha256_hex
import json

GENESIS = "0" * 64
# Every column of an event except the chain fields themselves: re-attributing an event (its command,
# principal, account, session, order, times or writer epoch) breaks the chain, not only editing its payload.
CHAINED_COLUMNS = ("seq", "event_id", "type", "schema_version", "command_id", "correlation_id", "causation_id",
                   "principal_id", "account_key", "session_id", "order_id", "instrument_id", "event_time_ns",
                   "receipt_time_ns", "recorded_time_ns", "writer_epoch", "payload_hash")


def chain_hash(prev_hash: str, fields: dict) -> str:
    return sha256_hex(prev_hash + canonical_json({c: fields[c] for c in CHAINED_COLUMNS}))


def verify_chain(conn: sqlite3.Connection) -> list[str]:
    """Recompute the hash chain; return problems (diagnostic, not a security boundary)."""
    problems = []
    prev = GENESIS
    cols = ", ".join(CHAINED_COLUMNS)
    for r in conn.execute(f"SELECT {cols}, payload, prev_hash, chain_hash FROM events ORDER BY seq"):
        if sha256_hex(r["payload"]) != r["payload_hash"]:
            problems.append(f"event {r['seq']} payload hash mismatch")
        if r["prev_hash"] != prev:
            problems.append(f"event {r['seq']} prev_hash mismatch")
        if r["chain_hash"] != chain_hash(prev, dict(r)):
            problems.append(f"event {r['seq']} chain hash mismatch")
        prev = r["chain_hash"]
    return problems


def replay_into(source: sqlite3.Connection, target_path: str | Path) -> sqlite3.Connection:
    target = connect(target_path)
    migrate(target, 0)
    target.execute("BEGIN IMMEDIATE")
    try:
        tx = Tx(target)
        cols = [c[1] for c in source.execute("PRAGMA table_info(events)")]
        for r in source.execute(f"SELECT {', '.join(cols)} FROM events ORDER BY seq"):
            target.execute(f"INSERT INTO events({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", tuple(r))
            apply_changes(tx, r["seq"], json.loads(r["payload"])["changes"])
        target.execute("COMMIT")
    except Exception:
        target.execute("ROLLBACK")
        raise
    return target


def table_dump(conn: sqlite3.Connection, table: str) -> list[tuple]:
    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
    return [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid")]


def compare_projections(a: sqlite3.Connection, b: sqlite3.Connection) -> list[str]:
    """Byte-for-byte comparison of every projection table, including row order (rowid)."""
    diffs = []
    for table in PROJECTION_TABLES:
        da, db = table_dump(a, table), table_dump(b, table)
        if da != db:
            diffs.append(f"{table}: {len(da)} rows live vs {len(db)} replayed; first difference "
                         f"{next(((x, y) for x, y in zip(da, db) if x != y), (len(da), len(db)))}")
    return diffs


def replay_and_compare(source_path: str | Path, target_path: str | Path) -> list[str]:
    source = connect(source_path, readonly=True)
    try:
        problems = verify_chain(source)
        target = replay_into(source, target_path)
        try:
            return problems + compare_projections(source, target)
        finally:
            target.close()
    finally:
        source.close()
