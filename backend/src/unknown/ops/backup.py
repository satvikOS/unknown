"""Online backups, restore drills and restores (ADR-0003 #6, ADR-0010, runbook RB-10).

``take_backup`` copies a live core database with SQLite's online backup from a
separate read-only connection (WAL lets it run while the core writes; it never
takes the writer lease). Each copy is marked inside itself as a backup, so
opening it anywhere is detected as a restore: the stop latches and in-flight
orders become UNKNOWN until reconciled. A JSON sidecar records the source
epoch, last event sequence, integrity check and SHA-256 of the copy.

``restore_drill`` proves a backup is usable without touching live state: in a
temporary directory it checks the SHA-256 and SQLite integrity, verifies the
hash chain, replays the event log into an empty database and compares every
projection, then opens the copy as the core would and checks that the restore
was detected and the stop is latched.

``restore_backup`` puts a backup in place for a real restore, only while no core
holds the lease, and moves the old database with its ``-wal``/``-shm`` aside so a
stale write-ahead log can never be replayed onto the restored file."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import ledger
from ..core.desk import open_desk
from ..core.replay import replay_and_compare
from ..domain.clock import SystemClock
from ..domain.ids import IdGenerator
from ..service.paths import Paths
from ..store.db import backup_database, lease_alive

PARTIAL_MAX_AGE_S = 3600


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _name_pattern(account_key: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(account_key)}-\d{{8}}T\d{{12}}Z\.sqlite\Z")


def _taken_at(path: Path) -> int:
    try:
        return int(json.loads(path.with_suffix(".json").read_text())["taken_at_ns"])
    except (OSError, ValueError, KeyError):
        return -1


def backups_for(paths: Paths, account_key: str) -> list[Path]:
    """This account's backups only (exact name match: ``A`` never matches ``A-2``), oldest first."""
    pattern = _name_pattern(account_key)
    found = [p for p in paths.backups.glob("*.sqlite") if pattern.match(p.name)] if paths.backups.is_dir() else []
    return sorted(found, key=lambda p: (_taken_at(p), p.name))


def _sweep(paths: Paths, account_key: str, now_s: float) -> None:
    for partial in paths.backups.glob(f"{glob_escape(account_key)}-*.partial"):
        if now_s - partial.stat().st_mtime > PARTIAL_MAX_AGE_S:
            partial.unlink(missing_ok=True)


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def take_backup(paths: Paths, account_key: str, *, keep: int = 96, now_ns: int | None = None) -> dict:
    """Back up ``<data>/<account>.sqlite`` into ``<backups>/<account>-<UTC time>.sqlite`` (mode 600); keep the newest ``keep``."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    source = paths.db(account_key)
    if not source.exists():
        raise FileNotFoundError(f"no core database at {source}")
    paths.ensure()
    now_ns = now_ns if now_ns is not None else time.time_ns()
    _sweep(paths, account_key, time.time())
    stamp = datetime.fromtimestamp(now_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = paths.backups / f"{account_key}-{stamp}.sqlite"
    tmp = target.with_suffix(".partial")
    old_umask = os.umask(0o077)   # the copy is created mode 600, never briefly readable by others
    try:
        meta = backup_database(source, tmp, now_ns)
        if meta["integrity_check"] != "ok":
            raise RuntimeError(f"backup failed its integrity check: {meta['integrity_check']}")
        meta = {**meta, "account_key": account_key, "file": target.name, "sha256": _sha256(tmp),
                "bytes": tmp.stat().st_size}
        target.with_suffix(".json").write_text(json.dumps(meta, indent=2, sort_keys=True))
        os.replace(tmp, target)
    finally:
        os.umask(old_umask)
        tmp.unlink(missing_ok=True)
    for old in backups_for(paths, account_key)[:-keep]:
        if old != target:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
    return meta


def restore_drill(backup: Path, account_key: str, *, require_sidecar: bool = True) -> dict:
    """Prove ``backup`` restores: integrity, chain and replay equivalence, restore detection and a latched stop."""
    backup = Path(backup)
    report: dict = {"backup": str(backup), "problems": []}
    sidecar = backup.with_suffix(".json")
    if sidecar.exists():
        expected = json.loads(sidecar.read_text()).get("sha256")
        report["sha256_matches_sidecar"] = expected == _sha256(backup)
        if not report["sha256_matches_sidecar"]:
            report["problems"].append("backup file does not match its recorded SHA-256")
    elif require_sidecar:
        report["sha256_matches_sidecar"] = None
        report["problems"].append("no sidecar: the backup's SHA-256 cannot be checked")
    with tempfile.TemporaryDirectory(prefix="unknown-restore-drill-") as d:
        home = Paths(Path(d))
        home.ensure()
        copy = home.db(account_key)
        shutil.copyfile(backup, copy)
        conn = sqlite3.connect(str(copy))
        try:
            report["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            has_origin = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_origin'").fetchone()
            origin = conn.execute("SELECT source_max_seq FROM backup_origin ORDER BY rowid DESC LIMIT 1").fetchone() if has_origin else None
            max_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0] if report["integrity_check"] == "ok" else None
        except sqlite3.DatabaseError as exc:
            report["integrity_check"], origin, max_seq = f"{type(exc).__name__}: {exc}", None, None
        finally:
            conn.close()
        if report["integrity_check"] != "ok":
            report["problems"].append(f"SQLite integrity check: {report['integrity_check']}")
            report["ok"] = False
            return report
        if origin is None:
            report["problems"].append("no backup_origin marker: not a backup taken by this tool")
        elif origin[0] != max_seq:
            report["problems"].append(f"marker claims event seq {origin[0]} but the copy holds {max_seq}")
        replay_problems = replay_and_compare(copy, Path(d) / "replay.sqlite")
        report["replay_problems"] = replay_problems[:50]
        report["problems"] += [f"replay: {p}" for p in replay_problems[:50]]
        desk = open_desk(copy, SystemClock(), IdGenerator(lambda: time.time_ns() // 1_000_000),
                         deployment_id="restore-drill", holder="restore-drill", takeover=True)
        try:
            with desk.store.read() as tx:
                control = {r["account_key"]: json.loads(r["row"]) for r in tx.all("SELECT account_key, row FROM control")}
                kinds = sorted({json.loads(r["row"])["kind"] for r in tx.all("SELECT row FROM exceptions WHERE status='OPEN'")})
                unknown = tx.one("SELECT COUNT(*) AS n FROM orders WHERE json_extract(row,'$.state')='UNKNOWN'")["n"]
                verify = ledger.verify(tx)
        finally:
            desk.store.close()
        c = control.get(account_key)
        report.update(restore_detected="RESTORED_FROM_BACKUP" in kinds, open_exception_kinds=kinds, unknown_orders=unknown,
                      stop_latched=bool(c and c["stop_latched"]), latch_reason=c.get("latch_reason") if c else None,
                      ledger_problems=verify[:50])
        if c is None:
            report["problems"].append(f"account {account_key} not found in the backup")
        elif not report["restore_detected"] or not report["stop_latched"]:
            report["problems"].append("the restore was not detected or the stop is not latched")
        report["problems"] += [f"ledger: {p}" for p in verify[:50]]
    report["ok"] = not report["problems"]
    return report


def restore_backup(paths: Paths, account_key: str, backup: Path, *, now_ns: int | None = None) -> dict:
    """Put ``backup`` in place as ``<data>/<account>.sqlite`` for a real restore (RB-10).

    Refuses while a core holds the lease. The current database and its ``-wal``/``-shm`` move together into
    ``<data>/replaced-<account>-<UTC time>/``. The backup is copied with SQLite's backup API into a temporary
    file, checked, fsynced and renamed into place. Starting the core then detects the restore and latches."""
    backup = Path(backup)
    live = paths.db(account_key)
    now_ns = now_ns if now_ns is not None else time.time_ns()
    if live.exists() and lease_alive(live, now_ns):
        raise RuntimeError("a core process holds the writer lease: stop it (launchctl bootout ...) before restoring")
    check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        raise RuntimeError(f"backup fails integrity_check: {ok}")
    stamp = datetime.fromtimestamp(now_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    aside = paths.data / f"replaced-{account_key}-{stamp}"
    moved = []
    for suffix in ("", "-wal", "-shm"):
        f = Path(f"{live}{suffix}")
        if f.exists():
            aside.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(f, aside / f.name)
            moved.append(f.name)
    tmp = paths.data / f".{account_key}.restoring"
    tmp.unlink(missing_ok=True)
    old_umask = os.umask(0o077)
    try:
        src = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("restored copy fails integrity_check")
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, live)
    finally:
        os.umask(old_umask)
        tmp.unlink(missing_ok=True)
    return {"restored": str(live), "from": str(backup), "moved_aside": moved, "aside_dir": str(aside) if moved else None,
            "next": "start the core: it detects the restore and latches the stop; then reconcile, resolve, green start"}
